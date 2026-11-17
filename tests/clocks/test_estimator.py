import math
import pytest

from syn.clocks.config import ClockConfig
from syn.clocks.estimator import advance, initial, project, statistics_for
from syn.clocks.types import ClockReading

WALL, MONO = 1_790_000_000_000_000_000, 100_000_000_000
CONFIG = ClockConfig(gpsdHosts=())


def sample(seconds=0, offset=10, error=2, source="ntp:test", **kw):
    return ClockReading(source, "ntp", WALL+int(seconds*1e9), MONO+int(seconds*1e9), offset, error, **kw)


def test_initial_is_zero_unknown_not_host_resolution():
    state = initial("a", WALL, MONO)
    est = project(state, MONO, CONFIG)
    assert est["utcOffsetMs"] == 0
    assert est["precisionMs"] is None and est["uncertaintyMs"] is None
    assert est["systemResolutionMs"] > 0


def test_drift_is_separate_from_jitter():
    state = initial("a", WALL, MONO)
    for i in range(8):
        state, est, stats, step = advance(state, sample(i*10, 10+i*.1), CONFIG)
    assert stats["driftPpm"] == pytest.approx(10)
    assert stats["jitterMs"] == pytest.approx(0, abs=1e-12)
    assert step is None
    assert est["utcOffsetMs"] == pytest.approx(10.7)


def test_residual_jitter_measured_after_drift():
    stats = statistics_for([[i*10**9, i*.02+(.1 if i%2 else -.1)] for i in range(20)])
    assert stats["driftPpm"] == pytest.approx(20, abs=3)
    assert .09 < stats["jitterMs"] < .12


def test_window_is_bounded():
    state = initial("a", WALL, MONO)
    for i in range(100):
        state, *_ = advance(state, sample(i), CONFIG)
    assert len(state["sources"]["ntp:test"]["samples"]) == CONFIG.windowSize


def test_unknown_accuracy_cannot_displace_ntp():
    state, *_ = advance(initial("a", WALL, MONO), sample(), CONFIG)
    state, est, *_ = advance(state, sample(1, 90000, None, "rtc:unknown", resolutionMs=.000001), CONFIG)
    assert est["source"] == "ntp:test"
    assert est["utcOffsetMs"] == 10


def test_best_source_selected_and_conflict_inflates_error():
    state, *_ = advance(initial("a", WALL, MONO), sample(), CONFIG)
    state, est, *_ = advance(state, sample(1, 200, .001, "ptp:disciplined"), CONFIG)
    assert est["source"] == "ptp:disciplined"
    assert est["status"] == "disagreement"
    assert est["uncertaintyMs"] >= 190


def test_expired_source_becomes_holdover_with_growing_uncertainty():
    state, est, *_ = advance(initial("a", WALL, MONO), sample(), CONFIG)
    late = project(state, MONO+400*10**9, CONFIG)
    later = project(state, MONO+800*10**9, CONFIG)
    assert late["status"] == "holdover"
    assert later["uncertaintyMs"] > late["uncertaintyMs"] > est["uncertaintyMs"]


def test_wall_step_invalidates_old_offsets():
    state, *_ = advance(initial("a", WALL, MONO), sample(), CONFIG)
    reading = ClockReading("system:realtime", "system", WALL+20*10**9, MONO+10**9)
    state, est, stats, step = advance(state, reading, CONFIG)
    assert step == 19000
    assert state["epoch"] == 1 and est["precisionMs"] is None
    assert est["utcOffsetMs"] == 0


def test_out_of_order_sample_does_not_rewind_anchor_or_latest():
    state, *_ = advance(initial("a", WALL, MONO), sample(10, 10), CONFIG)
    state, est, *_ = advance(state, sample(1, -900), CONFIG)
    assert state["anchorMonotonicNs"] == MONO+10*10**9
    assert est["utcOffsetMs"] == 10


def test_failed_sample_does_not_replace_good_offset():
    state, *_ = advance(initial("a", WALL, MONO), sample(), CONFIG)
    state, est, stats, _ = advance(state, sample(1, None, None, valid=False), CONFIG)
    assert est["utcOffsetMs"] == 10 and stats["failures"] == 1


def test_gps_qualification_needs_three_distinct_3d_epochs():
    state = initial("a", WALL, MONO)
    for i in range(3):
        reading = ClockReading("gps:a", "gps", WALL+i*10**9, MONO+i*10**9, 20, 1000,
            metadata={"requiresStableFix":True,"fix":"3d","referenceNs":WALL+i*10**9})
        state, est, *_ = advance(state, reading, CONFIG)
        assert (est["status"] == "referenced") == (i == 2)


def test_pps_records_period_jitter_not_absolute_utc_accuracy():
    state = initial("a", WALL, MONO)
    for i in range(5):
        reading = ClockReading("pps:a", "pps", WALL+i*10**9, MONO+i*10**9,
            metadata={"pulseSystemNs":WALL+i*10**9+(i%2)*100000})
        state, est, stats, _ = advance(state, reading, CONFIG)
    assert stats["periodJitterMs"] > 0
    assert est["precisionMs"] is None


@pytest.mark.parametrize("value", [math.nan, math.inf, -1, True])
def test_invalid_uncertainty_rejected(value):
    with pytest.raises(ValueError):
        sample(error=value)


@pytest.mark.parametrize("settings", [{"ntpServers":[]}, {"ntpSeconds":0}, {"timeoutSeconds":math.inf},
    {"windowSize":2}, {"chrony":"yes"}, {"ntpServers":["https://secret@bad"]},
    {"unknown":1}, {"ptpDevices":[{"path":"/etc/passwd"}]},
    {"rtcDevices":[{"path":"/dev/rtc0","timescale":"TAI"}]}, {"gpsdHosts":["foo.invalid:2947"]}])
def test_config_invalid(settings):
    with pytest.raises(ValueError):
        ClockConfig.from_dict(settings)


def test_config_round_trip():
    assert ClockConfig.from_dict(CONFIG.to_dict()) == CONFIG
