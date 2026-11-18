"""Bind the broker to a pinned process handle without real Windows exits."""
from __future__ import annotations

import ctypes
from pathlib import Path
import queue
import sys
import threading
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "host-custody"))
from syn_host_custody import owner


CREATED = 0x01DB39917C8823AF  # Deliberately exceeds exact IEEE-754 integer range.


class ProcessAPI:
    def __init__(self, *, created=CREATED, initial=owner.WAIT_TIMEOUT):
        self.created = created
        self.initial = initial
        self.handle = object()
        self.events = []
        self.results = queue.Queue()
        self.waiting = threading.Event()
        self.in_wait = False
        self.open_error = None
        self.creation_error = None
        self.close_error = None

    def open_process(self, pid):
        self.events.append(("open", pid))
        if self.open_error:
            raise self.open_error
        return self.handle

    def get_creation_time(self, handle):
        assert handle is self.handle
        self.events.append(("created", handle))
        if self.creation_error:
            raise self.creation_error
        return self.created

    def wait(self, handle, milliseconds):
        assert handle is self.handle
        self.events.append(("wait", handle, milliseconds))
        if milliseconds == 0:
            if isinstance(self.initial, Exception):
                raise self.initial
            return self.initial
        assert milliseconds == 250
        self.in_wait = True
        self.waiting.set()
        try:
            try:
                value = self.results.get(timeout=.05)
            except queue.Empty:
                value = owner.WAIT_TIMEOUT
            if isinstance(value, Exception):
                raise value
            return value
        finally:
            self.in_wait = False
            self.events.append(("wait-returned", handle))

    def close(self, handle):
        assert handle is self.handle
        assert not self.in_wait, "Process handle closed while a watcher was waiting on it"
        self.events.append(("close", handle))
        if self.close_error:
            error, self.close_error = self.close_error, None
            raise error


class ExitRecorder:
    def __init__(self):
        self.codes = []
        self.called = threading.Event()

    def __call__(self, code):
        self.codes.append(code)
        self.called.set()


def monitor(api=None):
    api, exits = api or ProcessAPI(), ExitRecorder()
    return owner.OwnerProcess(1234, CREATED, api=api, exit_process=exits), api, exits


def test_pinned_live_owner_is_opened_once_and_close_joins_before_release():
    watch, api, exits = monitor()
    assert watch.start() is watch
    assert api.waiting.wait(2)
    assert watch.start() is watch
    watch.close()
    watch.close()
    assert sum(event[0] == "open" for event in api.events) == 1
    assert sum(event[0] == "close" for event in api.events) == 1
    assert api.events[-1] == ("close", api.handle)
    assert watch._thread is None and watch._handle is None
    assert not exits.codes
    with pytest.raises(owner.OwnerProcessError, match="closed"):
        watch.start()


def test_reused_pid_is_rejected_before_any_watch_or_broker_work():
    watch, api, exits = monitor(ProcessAPI(created=CREATED + 1))
    with pytest.raises(owner.OwnerProcessError, match="PID was reused"):
        watch.start()
    assert [event[0] for event in api.events] == ["open", "created", "close"]
    assert watch._handle is None and watch._thread is None
    assert not exits.codes


@pytest.mark.parametrize("result", [owner.WAIT_OBJECT_0, 128, 0xFFFFFFFF])
def test_dead_or_unverifiable_owner_cannot_start(result):
    watch, api, exits = monitor(ProcessAPI(initial=result))
    with pytest.raises(owner.OwnerProcessError):
        watch.start()
    assert watch._handle is None and watch._thread is None
    assert sum(event[0] == "close" for event in api.events) == 1
    assert not exits.codes


@pytest.mark.parametrize("where", ["open", "creation", "initial-wait"])
def test_native_start_error_releases_any_acquired_process_handle(where):
    api = ProcessAPI()
    failure = OSError("Synthetic process API failure")
    if where == "open":
        api.open_error = failure
    elif where == "creation":
        api.creation_error = failure
    else:
        api.initial = failure
    watch, api, exits = monitor(api)
    with pytest.raises(OSError) as caught:
        watch.start()
    assert caught.value is failure
    assert sum(event[0] == "close" for event in api.events) == (0 if where == "open" else 1)
    assert not exits.codes


@pytest.mark.parametrize("result,code", [
    (owner.WAIT_OBJECT_0, 0), (0xFFFFFFFF, 2), (128, 2),
    (OSError("WaitForSingleObject failed"), 2),
])
def test_owner_death_or_monitor_failure_terminates_broker(result, code):
    watch, api, exits = monitor()
    watch.start()
    try:
        assert api.waiting.wait(2)
        api.results.put(result)
        assert exits.called.wait(2)
        assert exits.codes == [code]
        assert sum(event[0] == "open" for event in api.events) == 1
    finally:
        watch.close()
    assert api.events[-1] == ("close", api.handle)


def test_context_cleanup_does_not_terminate_its_broker():
    watch, api, exits = monitor()
    with pytest.raises(ValueError, match="Server failed"):
        with watch:
            assert api.waiting.wait(2)
            raise ValueError("Server failed")
    assert not exits.codes
    assert api.events[-1] == ("close", api.handle)


def test_thread_start_failure_does_not_leave_owner_handle_open(monkeypatch):
    watch, api, exits = monitor()
    def fail(_):
        raise RuntimeError("Cannot create watcher thread")
    monkeypatch.setattr(owner.threading.Thread, "start", fail)
    with pytest.raises(RuntimeError, match="Cannot create watcher thread"):
        watch.start()
    assert sum(event[0] == "close" for event in api.events) == 1
    assert watch._handle is None
    assert not exits.codes


def test_unjoined_watcher_retains_process_handle_for_later_cleanup():
    watch, api, exits = monitor()
    calls = []
    worker = SimpleNamespace(join=lambda timeout: calls.append(timeout), is_alive=lambda: True)
    watch._handle, watch._thread = api.handle, worker
    with pytest.raises(owner.OwnerProcessError, match="remains owned"):
        watch.close()
    assert calls == [2]
    assert watch._handle is api.handle
    assert not any(event[0] == "close" for event in api.events)
    worker.is_alive = lambda: False
    watch.close()
    assert watch._handle is None
    assert api.events[-1] == ("close", api.handle)
    assert not exits.codes


def test_interrupted_start_after_thread_is_waiting_joins_before_handle_close(monkeypatch):
    watch, api, exits = monitor()
    original = owner.threading.Thread.start
    def interrupted(worker):
        original(worker)
        assert api.waiting.wait(2)
        raise RuntimeError("Interrupted after native thread startup")
    monkeypatch.setattr(owner.threading.Thread, "start", interrupted)
    with pytest.raises(RuntimeError, match="Interrupted after"):
        watch.start()
    assert not api.in_wait
    assert watch._thread is None and watch._handle is None
    assert api.events[-1] == ("close", api.handle)
    assert not exits.codes


def test_failed_close_retains_handle_and_does_not_allow_restart():
    watch, api, exits = monitor()
    watch.start()
    api.close_error = OSError("CloseHandle failed")
    with pytest.raises(OSError, match="CloseHandle"):
        watch.close()
    assert watch._handle is api.handle
    with pytest.raises(owner.OwnerProcessError, match="closed"):
        watch.start()
    watch.close()
    assert watch._handle is None
    assert not exits.codes


@pytest.mark.parametrize("pid,created", [
    (True, CREATED), (0, CREATED), (-1, CREATED), (2**32, CREATED),
    ("1234", CREATED), (1234, True), (1234, 0), (1234, -1),
    (1234, 2**64), (1234, float(CREATED)),
])
def test_owner_pin_rejects_ambiguous_or_unbounded_values(pid, created):
    api = ProcessAPI()
    with pytest.raises(ValueError):
        owner.OwnerProcess(pid, created, api=api, exit_process=ExitRecorder())
    assert not api.events


def test_native_api_keeps_exact_filetime_and_requests_only_noninheritable_query_wait_rights(monkeypatch):
    calls = []
    class Function:
        def __init__(self, implementation):
            self.implementation = implementation
        def __call__(self, *args):
            return self.implementation(*args)

    def opened(access, inherited, pid):
        calls.append(("open", access, inherited, pid))
        return 9876

    def times(handle, created, exited, kernel, user):
        assert handle == 9876
        created._obj.dwHighDateTime = CREATED >> 32
        created._obj.dwLowDateTime = CREATED & 0xFFFFFFFF
        return 1

    def wait(handle, timeout):
        calls.append(("wait", handle, timeout))
        return owner.WAIT_TIMEOUT

    def close(handle):
        calls.append(("close", handle))
        return 1

    kernel = SimpleNamespace(OpenProcess=Function(opened), GetProcessTimes=Function(times),
        WaitForSingleObject=Function(wait), CloseHandle=Function(close))
    monkeypatch.setattr(owner, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kw: kernel, raising=False)
    api = owner.WindowsProcessAPI()
    handle = api.open_process(1234)
    assert api.get_creation_time(handle) == CREATED
    assert api.wait(handle, 250) == owner.WAIT_TIMEOUT
    api.close(handle)
    assert calls == [("open", 0x101000, False, 1234), ("wait", 9876, 250), ("close", 9876)]
