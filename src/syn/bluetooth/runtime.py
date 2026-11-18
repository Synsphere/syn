"""Alternate bounded Syn beacon bursts with unfiltered OS advertisement scans."""
from __future__ import annotations

import asyncio
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
import logging
import random
import time
from uuid import uuid4

from .backend import BackendError, default_backend, validate_frame
from .config import BluetoothConfig
from .models import Adapter, ReceivedAdvertisement
from .protocol import BeaconDecoder, build_frames, canonical_node_ref


logger = logging.getLogger(__name__)


class PersistenceFailure(RuntimeError):
    """Radio activity is fenced after a native ledger transaction fails."""


class CleanupFailure(RuntimeError):
    """Do not start another session until native ownership has been released."""


@dataclass
class _Observation:
    payload: dict
    observed_at: datetime
    due: float


class BluetoothRuntime:
    """One half-duplex worker per controller, with a bounded observation writer.

    OS start acknowledgements are recorded as such, never as proof of reception.
    A failing ledger stops radio activity. Shutdown first releases hardware and
    then drains received reports, joining any in-flight database transaction.
    """

    def __init__(self, sessions, node_ref: str | None, config: BluetoothConfig | None = None,
                 *, store=None, backend=None, random_source=None):
        self.config = config if config is not None else BluetoothConfig()
        self.node_ref = node_ref
        self.store = store
        self._frames = ()
        if self.config.enabled:
            if node_ref is None:
                raise ValueError("Bluetooth requires a prepared node identity")
            self.node_ref = node_ref = canonical_node_ref(node_ref)
            self._frames = build_frames(node_ref, self.config.formats,
                                        company_id=self.config.companyId)
            if self.store is None:
                from .store import BluetoothStore
                self.store = BluetoothStore(sessions)
        self._provided_backend = backend
        self._backend = None
        self._native_source = None
        self._native_seen = {}
        self._rng = random_source or random.SystemRandom()
        self._decoder = BeaconDecoder()
        self._lifecycle = asyncio.Lock()
        self._database = asyncio.Semaphore(1)
        self._stop = asyncio.Event()
        self._writer_stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._manager = self._writer = None
        self._workers = {}
        self._adapters = {}
        self._pending: dict[str, _Observation] = {}
        self._loss_pending = Counter()
        self._counts = Counter()
        self._session_id = uuid4().hex
        self._running = False
        self._state = "stopped" if self.config.enabled else "disabled"
        self._last_error = None
        self._backend_error = None
        self._availability_note = None
        self._retry_at = None
        self._last_event_ref = None
        self._metadata = {}
        self.fault = None

    async def _db(self, method, *args, **kwargs):
        async with self._database:
            operation = asyncio.create_task(asyncio.to_thread(method, *args, **kwargs))
            try:
                return await asyncio.shield(operation)
            except asyncio.CancelledError:
                # Worker threads cannot be cancelled. Do not release ownership
                # until the transaction has committed or rolled back.
                while not operation.done():
                    try:
                        await asyncio.shield(operation)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not operation.cancelled():
                    failure = operation.exception()
                    if failure is not None:
                        self._fail(failure)
                raise

    async def _record(self, kind, payload, *, observed_at=None, operation_id=None):
        try:
            result = await self._db(self.store.record, self.node_ref, kind, payload,
                observed_at=observed_at,
                operation_id=operation_id or self._session_id + ":" + uuid4().hex)
            self._counts["events_recorded"] += 1
            self._last_event_ref = result.get("eventRef")
            return result
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._fail(error)
            raise PersistenceFailure("Bluetooth ledger transaction failed") from error

    def _fail(self, error):
        if self.fault is None:
            self.fault = error
            self._last_error = f"{type(error).__name__}: {str(error)[:240]}"
            logger.error("Bluetooth service fenced: %s", self._last_error)
        self._state = "failed"
        self._stop.set()
        self._wake.set()

    async def start(self):
        async with self._lifecycle:
            if self._running or not self.config.enabled:
                return
            if self.fault is not None:
                raise RuntimeError("Restart the node after resolving its Bluetooth ledger failure") from self.fault
            self._metadata = await self._db(self.store.prepare, self.node_ref)
            self._stop.clear()
            self._writer_stop.clear()
            self._session_id = uuid4().hex
            self._decoder = BeaconDecoder()
            self._running = True
            self._state = "starting"
            self._writer = asyncio.create_task(self._write_observations(), name="syn-bluetooth-observations")
            self._manager = asyncio.create_task(self._manage(), name="syn-bluetooth-adapters")

    async def stop(self):
        async with self._lifecycle:
            shutdown = asyncio.create_task(self._shutdown())
            try:
                await asyncio.shield(shutdown)
            except asyncio.CancelledError:
                while not shutdown.done():
                    try:
                        await asyncio.shield(shutdown)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not shutdown.cancelled() and shutdown.exception() is not None:
                    self._fail(shutdown.exception())
                raise

    async def _shutdown(self):
        self._stop.set()
        if self._manager is not None:
            self._manager.cancel()
            await asyncio.gather(self._manager, return_exceptions=True)
            self._manager = None
        # No new native callbacks may arrive after the backend has closed.
        self._writer_stop.set()
        self._wake.set()
        if self._writer is not None:
            await asyncio.gather(self._writer, return_exceptions=True)
            self._writer = None
        self._running = False
        self._retry_at = None
        if self.fault is None:
            self._state = "stopped" if self.config.enabled else "disabled"

    def check(self):
        if self.fault is not None:
            raise RuntimeError("Bluetooth observation service failed") from self.fault

    def status(self):
        return deepcopy({"enabled": self.config.enabled, "running": self._running,
            "state": "failed" if self.fault is not None else self._state, "node_ref": self.node_ref,
            "beacon_ref": self._metadata.get("beaconRef"),
            "mode": "advertise-then-listen", "air_reception_verified": False,
            "identity_authentication": "unverified-advertised-claims",
            "monitor_scope": "os-visible-bluetooth-advertisements",
            "adapters": list(self._adapters.values()),
            "pending_observations": len(self._pending),
            "pending_loss_reports": sum(self._loss_pending.values()),
            "counts": dict(self._counts), "last_event_ref": self._last_event_ref,
            "last_error": self._last_error,
            "availability_note": self._availability_note,
            "retry_in_seconds": None if self._retry_at is None else
                round(max(0.0, self._retry_at - time.monotonic()), 1)})

    async def _pause(self, seconds):
        if seconds <= 0:
            return self._stop.is_set()
        try:
            await asyncio.wait_for(self._stop.wait(), seconds)
            return True
        except TimeoutError:
            return False

    async def _manage(self):
        retry_delay = self.config.retrySeconds
        try:
            while not self._stop.is_set():
                backend = self._provided_backend or default_backend(timeout=self.config.backendTimeoutSeconds)
                self._backend = backend
                try:
                    await backend.start()
                    while not self._stop.is_set():
                        adapters = await backend.adapters()
                        self._account_native_losses(backend)
                        await self._reconcile(backend, adapters)
                        if self._backend_error is not None:
                            logger.info("Bluetooth backend recovered")
                        self._backend_error = self._last_error = None
                        self._retry_at = None
                        retry_delay = self.config.retrySeconds
                        usable = any(adapter.can_scan or self._adapters[key]["active_formats"]
                                     for key, (adapter, _) in self._workers.items())
                        self._state = "active" if usable else "waiting-for-adapter"
                        note = None if usable else (
                            "No Bluetooth adapter is visible to this OS. WSL needs a Linux-visible controller; "
                            "Windows Bluetooth is not bridged." if not adapters else
                            "No adapter currently supports scanning or Syn advertising. Check adapter power, "
                            "rfkill and permissions in the OS running Syn.")
                        if note != self._availability_note:
                            if note is not None:
                                logger.info("Bluetooth waiting: %s", note)
                            else:
                                logger.info("Bluetooth adapter available")
                        self._availability_note = note
                        await self._pause(self.config.inventoryRefreshSeconds)
                except (BackendError, OSError, TimeoutError) as error:
                    self._state = "unavailable"
                    self._availability_note = None
                    self._last_error = f"{type(error).__name__}: {' '.join(str(error).split())[:240]}"
                    self._counts["backend_failures"] += 1
                    if self._last_error != self._backend_error:
                        logger.info("Bluetooth backend unavailable: %s; retrying with backoff up to 300 seconds",
                                    self._last_error)
                    self._backend_error = self._last_error
                finally:
                    # stop() can arrive while a ledger failure is already
                    # releasing workers. A second cancellation must not skip
                    # backend.close() and leave a native publisher running.
                    cleanup = asyncio.create_task(self._close_backend(backend))
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        while not cleanup.done():
                            try:
                                await asyncio.shield(cleanup)
                            except asyncio.CancelledError:
                                continue
                            except Exception:
                                break
                        if not cleanup.cancelled() and cleanup.exception() is not None:
                            self._fail(cleanup.exception())
                        raise
                self._retry_at = time.monotonic() + retry_delay
                await self._pause(retry_delay)
                self._retry_at = None
                retry_delay = min(300, retry_delay * 2)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._fail(error)

    async def _close_backend(self, backend):
        tasks = [task for _, task in self._workers.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._workers.clear()
        self._decoder = BeaconDecoder()
        for state in self._adapters.values():
            state["state"] = "stopped"
        try:
            await backend.close()
        except Exception as error:
            self._fail(error)
        finally:
            self._account_native_losses(backend)
            self._backend = None

    def _account_native_losses(self, backend):
        if self._native_source is not backend:
            self._native_source, self._native_seen = backend, {}
        for counter, reason in (("dropped_reports", "backend-queue-overflow-or-stop"),
                                ("invalid_reports", "invalid-native-report")):
            value = getattr(backend, counter, 0)
            if type(value) is not int or value < 0:
                continue
            change = max(0, value - self._native_seen.get(counter, 0))
            self._native_seen[counter] = value
            if change:
                self._counts["backend_" + counter] += change
                self._counts["reports_lost"] += change
                self._loss_pending[reason] += change
                self._wake.set()

    async def _reconcile(self, backend, adapters):
        # A backend must use a stable key for a physical controller and a new
        # epoch on reconnect. Duplicated inventory entries never get two loops.
        selected = {}
        for adapter in adapters:
            if adapter.key not in selected and len(selected) < self.config.maxAdapters:
                selected[adapter.key] = adapter
        for key, (old, task) in list(self._workers.items()):
            new = selected.get(key)
            if new is None or new.connection_epoch != old.connection_epoch:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                del self._workers[key]
                self._adapters.pop(key, None)
                self._decoder.forget_adapter(key)
                if self.fault is None:
                    await self._record("discovery", {"adapter": old.to_dict(), "present": False})
        for key, adapter in selected.items():
            if key in self._workers:
                continue
            frames, rejected = self._compatible_frames(adapter)
            state = {"adapter": adapter.to_dict(), "state": "starting",
                     "transmissions_started": 0, "reports_received": 0,
                     "active_formats": sorted({frame.format for frame in frames}),
                     "unavailable_formats": rejected, "last_error": None}
            self._adapters[key] = state
            await self._record("discovery", {"adapter": adapter.to_dict(), "present": True,
                                             "activeFormats": state["active_formats"],
                                             "unavailableFormats": rejected})
            task = asyncio.create_task(self._run_adapter(backend, adapter, frames, state),
                                       name="syn-bluetooth-" + key[:48])
            self._workers[key] = (adapter, task)

    def _compatible_frames(self, adapter):
        compatible, rejected = [], {}
        for frame in self._frames:
            try:
                validate_frame(adapter, frame)
                compatible.append(frame)
            except BackendError as error:
                rejected[frame.format] = str(error)[:240]
        # Prefer service-data identity on capable controllers. Windows' native
        # publisher supports the iBeacon identity transport instead.
        if any(frame.format == "syn" for frame in compatible):
            compatible = [frame for frame in compatible if frame.format != "syn-ibeacon"]
        if not any(frame.format in {"syn", "syn-ibeacon"} for frame in compatible):
            for frame in compatible:
                rejected[frame.format] = "A complete Syn identity transport is unavailable on this adapter"
            compatible = []
        return tuple(compatible), rejected

    async def _listen(self, backend, adapter, seconds, state):
        if not adapter.can_scan:
            state["state"] = "waiting-no-scan-support"
            await self._pause(seconds)
            return
        handle = None
        accepting = True

        def receive(report):
            if accepting:
                self._received(adapter, state, report)

        try:
            state["state"] = "starting-scan"
            handle = await backend.start_scanning(adapter, receive)
            state["state"] = "listening"
            # Check native handles while listening so an aborted scanner is
            # noticed without pretending that a full listen window occurred.
            remaining = seconds
            while remaining > 0 and not self._stop.is_set():
                if not handle.active:
                    raise BackendError("Native Bluetooth scanner stopped unexpectedly")
                step = min(remaining, .25)
                await self._pause(step)
                remaining -= step
        finally:
            accepting = False
            if handle is not None:
                try:
                    await backend.stop_scanning(adapter, handle)
                except Exception as error:
                    if self.fault is None:
                        await self._record("discovery", {"adapter": adapter.to_dict(),
                            "operationError": "Unable to confirm scanner shutdown",
                            "error": f"{type(error).__name__}: {str(error)[:240]}"})
                    raise CleanupFailure("Unable to confirm Bluetooth scanner shutdown") from error
                finally:
                    self._account_native_losses(backend)

    async def _transmit(self, backend, adapter, frame, state, cycle_id):
        operation = self._session_id + ":" + uuid4().hex
        payload = {"adapter": adapter.to_dict(), "cycleId": cycle_id,
                   "frameId": frame.frame_id, "format": frame.format,
                   "advertisingDataHex": frame.advertising_data.hex(),
                   "requestedDurationSeconds": self.config.frameDurationSeconds,
                   "airReceptionVerified": False}
        await self._record("transmission-requested", payload, operation_id=operation + ":requested")
        handle = None
        started = False
        interrupted = False
        try:
            if self._stop.is_set():
                return
            state["state"] = "starting-advertisement"
            handle = await backend.advertise(adapter, frame)
            if not handle.active:
                raise BackendError("Native Bluetooth advertiser did not acknowledge an active session")
            started = True
            await self._record("transmission-started", {**payload, "evidenceKind": "backend-callback",
                                "backendEvidence": handle.details}, operation_id=operation + ":started")
            state["state"] = "advertising"
            state["transmissions_started"] += 1
            self._counts["transmissions_started"] += 1
            remaining = self.config.frameDurationSeconds
            while remaining > 0 and not self._stop.is_set():
                if not handle.active:
                    raise BackendError("Native Bluetooth advertiser stopped unexpectedly")
                step = min(remaining, .1)
                await self._pause(step)
                remaining -= step
        except asyncio.CancelledError:
            interrupted = True
            raise
        except (BackendError, OSError, TimeoutError) as error:
            interrupted = True
            await self._record("transmission-failed", {**payload, "started": started,
                "error": f"{type(error).__name__}: {str(error)[:240]}"}, operation_id=operation + ":failed")
            raise
        finally:
            if handle is not None:
                try:
                    await backend.stop_advertising(adapter, handle)
                except Exception as error:
                    if self.fault is None:
                        await self._record("transmission-failed", {**payload, "started": started,
                            "phase": "stop", "error": f"{type(error).__name__}: {str(error)[:240]}"},
                            operation_id=operation + ":stop-failed")
                    raise CleanupFailure("Unable to confirm Bluetooth advertiser shutdown") from error
                if self.fault is None:
                    await self._record("transmission-stopped", {**payload, "started": started,
                        "evidenceKind": "backend-callback", "backendEvidence": handle.details,
                        "reason": "shutdown" if self._stop.is_set() else
                                  "interrupted" if interrupted else "slot-ended"},
                        operation_id=operation + ":stopped")

    async def _run_adapter(self, backend, adapter, frames, state):
        try:
            delay = self._rng.uniform(0, self.config.startupJitterSeconds)
            while not self._stop.is_set():
                try:
                    if delay:
                        await self._listen(backend, adapter, delay, state)
                        delay = 0
                    cycle_id = uuid4().hex
                    for frame in frames:
                        if self._stop.is_set():
                            break
                        await self._transmit(backend, adapter, frame, state, cycle_id)
                    if not self._stop.is_set():
                        await self._listen(backend, adapter,
                            self.config.listenSeconds + self._rng.uniform(0, self.config.cycleJitterSeconds), state)
                    state["last_error"] = None
                except (BackendError, OSError, TimeoutError) as error:
                    state["state"] = "retrying"
                    state["last_error"] = f"{type(error).__name__}: {str(error)[:240]}"
                    await self._record("discovery", {"adapter": adapter.to_dict(), "present": True,
                        "operationError": state["last_error"]})
                    # A transmitter failure need not make a usable receiver idle.
                    try:
                        await self._listen(backend, adapter, self.config.retrySeconds, state)
                    except (BackendError, OSError, TimeoutError):
                        await self._pause(self.config.retrySeconds)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._fail(error)
        finally:
            state["state"] = "stopped"

    def _received(self, adapter, state, report: ReceivedAdvertisement):
        if self._stop.is_set():
            return
        self._counts["reports_received"] += 1
        state["reports_received"] += 1
        try:
            if report.adapter_key != adapter.key:
                raise ValueError("Report belongs to a different receiver")
            now = asyncio.get_running_loop().time()
            identity = self._decoder.observe(report, now_monotonic=now)
            data = report.to_dict()
            comparable = {key: value for key, value in data.items()
                          if key not in {"observedAt", "rssi", "details"}}
            key = sha256(json.dumps({"advertisement": comparable, "identity": identity,
                "connectionEpoch": adapter.connection_epoch},
                sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
            observation = self._pending.get(key)
            if observation is None:
                if len(self._pending) >= self.config.maxPendingObservations:
                    self._lose("queue-full")
                    return
                payload = {"adapter": adapter.to_dict(), "advertisement": data,
                    "identity": identity, "reportCount": 0,
                    "firstSeenAt": data["observedAt"], "lastSeenAt": data["observedAt"],
                    "rssiMinimum": report.rssi, "rssiMaximum": report.rssi,
                    "lastRssi": report.rssi,
                    "airReceptionVerified": False,
                    "evidenceKind": "os-advertisement-report"}
                # Validate radio-influenced input before the database writer.
                # Oversized/malformed reports are accounted as loss, not DB faults.
                from .store import validate_payload
                payload = validate_payload(payload)
                observation = _Observation(payload, report.observed_at,
                    now + self.config.observationWindowSeconds)
                self._pending[key] = observation
            payload = observation.payload
            payload["reportCount"] += 1
            payload["lastSeenAt"] = data["observedAt"]
            payload["lastRssi"] = report.rssi
            if report.rssi is not None:
                for name, fn in (("rssiMinimum", min), ("rssiMaximum", max)):
                    payload[name] = report.rssi if payload[name] is None else fn(payload[name], report.rssi)
            self._wake.set()
        except Exception:
            # Malformed OS/vendor data cannot escape into a native event callback
            # or silently vanish. No untrusted object repr is persisted here.
            self._lose("invalid-report")

    def _lose(self, reason):
        self._loss_pending[reason] += 1
        self._counts["reports_lost"] += 1
        self._wake.set()

    async def _write_observations(self):
        try:
            while not self._writer_stop.is_set() or self._pending or self._loss_pending:
                if self.fault is not None:
                    return
                now = asyncio.get_running_loop().time()
                ready = [key for key, item in self._pending.items()
                         if self._writer_stop.is_set() or item.due <= now]
                for key in ready:
                    item = self._pending.pop(key)
                    await self._record("reception", item.payload, observed_at=item.observed_at)
                    self._counts["reports_recorded"] += item.payload["reportCount"]
                    self._counts["observations_recorded"] += 1
                if self._loss_pending:
                    losses, self._loss_pending = self._loss_pending, Counter()
                    await self._record("loss", {"reportCount": sum(losses.values()),
                        "reasons": dict(losses), "queueLimit": self.config.maxPendingObservations,
                        "scope": "reports-delivered-by-os"})
                    self._counts["losses_recorded"] += sum(losses.values())
                if self._writer_stop.is_set():
                    continue
                self._wake.clear()
                delay = max(.01, min((item.due - asyncio.get_running_loop().time()
                                     for item in self._pending.values()), default=.25))
                try:
                    await asyncio.wait_for(self._wake.wait(), delay)
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._fail(error)
