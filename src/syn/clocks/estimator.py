"""Bounded, restart-fenced offset estimator with explicit uncertainty.

Offsets have sign UTC - raw realtime. Drift is the derivative of that correction
in ppm. Jitter is the sample standard deviation of residuals after removing a
linear trend, not clock resolution and not an assertion of absolute accuracy.
"""
from __future__ import annotations

from copy import deepcopy
import math
import statistics
import time

from .config import ClockConfig
from .types import ClockReading


def initial(session_id, wall, mono):
    return {"sessionId": session_id, "anchorSystemNs": wall, "anchorMonotonicNs": mono,
            "epoch": 0, "sources": {}, "selected": None, "lastEstimate": None}


def statistics_for(samples):
    if not samples:
        return {"samples": 0, "jitterMs": None, "driftPpm": None, "driftErrorPpm": None}
    origin = samples[0][0]
    xs = [(s[0]-origin)/1e9 for s in samples]
    ys = [s[1] for s in samples]
    xbar, ybar = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x-xbar)**2 for x in xs)
    # Do not report a frequency estimate from tightly bunched messages.
    slope = sum((x-xbar)*(y-ybar) for x,y in zip(xs, ys))/sxx if sxx and xs[-1] >= 1 and len(xs) >= 3 else None
    residual = [y-ybar-(slope or 0)*(x-xbar) for x,y in zip(xs, ys)]
    jitter = math.sqrt(sum(r*r for r in residual)/max(1, len(xs)-(2 if slope is not None else 1))) if len(xs) >= 2 else None
    drift_error = (jitter/math.sqrt(sxx)*1000) if slope is not None else None
    return {"samples": len(xs), "jitterMs": jitter, "driftPpm": slope*1000 if slope is not None else None,
            "driftErrorPpm": drift_error}


def advance(state, reading: ClockReading, config: ClockConfig):
    state = deepcopy(state)
    delta = reading.monotonicNs-state["anchorMonotonicNs"]
    step = False
    step_ms = None
    # Out-of-order observations must not move the clock-pair anchor backwards.
    if delta >= 0:
        step_ms = (reading.systemNs-state["anchorSystemNs"]-delta)/1e6
        step = abs(step_ms) > config.stepThresholdMs
        if step:
            state["sources"] = {}
            state["selected"] = None
            state["lastEstimate"] = None
            state["epoch"] += 1
        state["anchorSystemNs"], state["anchorMonotonicNs"] = reading.systemNs, reading.monotonicNs
    sources = state["sources"]
    if reading.source not in sources and len(sources) >= config.maxSources:
        oldest = min(sources, key=lambda s: sources[s]["lastAttemptNs"])
        del sources[oldest]
    source = sources.setdefault(reading.source, {"kind": reading.kind, "attempts": 0, "failures": 0,
        "lastAttemptNs": reading.monotonicNs, "lastGoodNs": None, "samples": [], "last": None, "stableFixes": 0, "lastReferenceNs": None, "pulseTimes": []})
    source["attempts"] += 1
    source["lastAttemptNs"] = max(source["lastAttemptNs"], reading.monotonicNs)
    usable = reading.valid and reading.offsetMs is not None
    if reading.metadata.get("requiresStableFix"):
        ref = reading.metadata.get("referenceNs")
        if not reading.valid or reading.metadata.get("fix") != "3d":
            source["stableFixes"] = 0
        elif ref is not None and ref != source["lastReferenceNs"]:
            source["stableFixes"] += 1
        source["lastReferenceNs"] = ref
        usable = usable and source["stableFixes"] >= 3
    pulse = reading.metadata.get("pulseSystemNs")
    if type(pulse) is int and pulse > 0:
        pulses = source["pulseTimes"]
        if not pulses or pulse > pulses[-1]:
            source["pulseTimes"] = (pulses+[pulse])[-config.windowSize:]

    if usable and (source["lastGoodNs"] is None or reading.monotonicNs > source["lastGoodNs"]):
        source["samples"].append([reading.monotonicNs, reading.offsetMs])
        source["samples"] = source["samples"][-config.windowSize:]
        source["lastGoodNs"] = reading.monotonicNs
        source["last"] = reading.to_dict()
    elif not usable:
        source["failures"] += 1
    source["statistics"] = statistics_for(source["samples"])
    pulses = source["pulseTimes"]
    periods = [(b-a)/1e6 for a,b in zip(pulses, pulses[1:])]
    source["statistics"]["periodJitterMs"] = statistics.stdev(periods) if len(periods) >= 2 else None
    estimate = project(state, state["anchorMonotonicNs"], config)
    state["selected"] = estimate["source"]
    state["lastEstimate"] = estimate
    return state, estimate, {"source": reading.source, "kind": reading.kind,
        **source["statistics"], "attempts": source["attempts"], "failures": source["failures"],
        "lastAttemptNs": source["lastAttemptNs"], "lastGoodNs": source["lastGoodNs"]}, step_ms if step else None


def project(state, now_ns, config):
    candidates = []
    for name, source in state["sources"].items():
        last = source["last"]
        if not last or last["uncertaintyMs"] is None:
            continue
        age = (now_ns-last["monotonicNs"])/1e9
        if not 0 <= age <= config.maxAgeSeconds:
            continue
        stats = source["statistics"]
        drift = stats["driftPpm"] or 0.0
        uncertainty = last["uncertaintyMs"] + (stats["jitterMs"] or 0.0)
        uncertainty += age*max(config.holdoverPpm, abs(drift), stats["driftErrorPpm"] or 0.0)/1000
        candidates.append((uncertainty, name, last["offsetMs"]+age*drift/1000, stats, age))
    if candidates:
        candidates.sort(key=lambda c: (c[0], c[1]))
        error, name, offset, stats, age = candidates[0]
        # References may be correlated (chrony may use the same NTP/GPS source).
        # Never average them as though independent or shrink the error via sqrt(N).
        disagreement = max(abs(c[2]-offset) for c in candidates)
        conflict = any(abs(c[2]-offset) > c[0]+error for c in candidates[1:])
        return {"utcOffsetMs": offset, "uncertaintyMs": max(error, disagreement) if conflict else error,
            "precisionMs": max(error, disagreement) if conflict else error,
            "status": "disagreement" if conflict else "referenced", "source": name,
            "driftPpm": stats["driftPpm"], "jitterMs": stats["jitterMs"],
            "sourceAgeSeconds": age, "disagreementMs": disagreement, "atMonotonicNs": now_ns,
            "systemResolutionMs": time.get_clock_info("time").resolution*1000}
    previous = state.get("lastEstimate")
    if previous and previous["uncertaintyMs"] is not None:
        age = max(0.0, (now_ns-previous["atMonotonicNs"])/1e9)
        drift = previous["driftPpm"] or 0.0
        uncertainty = previous["uncertaintyMs"]+age*max(config.holdoverPpm, abs(drift))/1000
        return {**previous, "utcOffsetMs": previous["utcOffsetMs"]+age*drift/1000,
            "uncertaintyMs": uncertainty, "precisionMs": uncertainty, "status": "holdover",
            "sourceAgeSeconds": (previous["sourceAgeSeconds"] or 0)+age, "atMonotonicNs": now_ns}
    return {"utcOffsetMs": 0.0, "uncertaintyMs": None, "precisionMs": None, "status": "unknown",
            "source": None, "driftPpm": None, "jitterMs": None, "sourceAgeSeconds": None,
            "disagreementMs": None, "atMonotonicNs": now_ns,
            "systemResolutionMs": time.get_clock_info("time").resolution*1000}
