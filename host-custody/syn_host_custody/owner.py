"""Bind a Windows broker to one exact launcher process, including abrupt exit.

The process creation FILETIME prevents PID reuse from adopting an unrelated
process. Keep the opened process handle for the whole broker lifetime. A
process-local watcher is intentionally independent of the named-pipe loop,
which may be blocked waiting for its next WSL client.
"""
from __future__ import annotations

import os
import threading


WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
POLL_MILLISECONDS = 250


class OwnerProcessError(RuntimeError):
    """The intended Windows launcher is unavailable or cannot be verified."""


class WindowsProcessAPI:
    def __init__(self):
        if os.name != "nt":
            raise OSError("Host custody owner monitoring requires Windows")
        import ctypes
        from ctypes import wintypes
        self.ctypes, self.wintypes = ctypes, wintypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
        self.kernel.GetProcessTimes.restype = wintypes.BOOL
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL

    def open_process(self, pid):
        # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION; not inheritable.
        handle = self.kernel.OpenProcess(0x00100000 | 0x1000, False, pid)
        if not handle:
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        return handle

    def get_creation_time(self, handle):
        values = [self.wintypes.FILETIME() for _ in range(4)]
        if not self.kernel.GetProcessTimes(handle, *(self.ctypes.byref(value) for value in values)):
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        return (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime

    def wait(self, handle, milliseconds):
        result = self.kernel.WaitForSingleObject(handle, milliseconds)
        if result == 0xFFFFFFFF:
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        return result

    def close(self, handle):
        if not self.kernel.CloseHandle(handle):
            raise self.ctypes.WinError(self.ctypes.get_last_error())


class OwnerProcess:
    def __init__(self, pid: int, creation_time: int, *, api=None, exit_process=None):
        if type(pid) is not int or not 1 <= pid <= 0xFFFFFFFF:
            raise ValueError("Launcher PID must be a positive Windows process ID")
        if type(creation_time) is not int or not 1 <= creation_time < 2**64:
            raise ValueError("Launcher creation time must be a positive FILETIME")
        self.pid, self.creation_time = pid, creation_time
        self.api = api
        self._exit = exit_process or os._exit
        self._handle = None
        self._thread = None
        self._stop = threading.Event()
        self._closed = False

    def start(self):
        if self._closed:
            raise OwnerProcessError("Launcher monitor is closed")
        if self._thread is not None:
            return self
        self.api = self.api or WindowsProcessAPI()
        try:
            self._handle = self.api.open_process(self.pid)
            if self.api.get_creation_time(self._handle) != self.creation_time:
                raise OwnerProcessError("Launcher PID was reused; refusing a different process owner")
            if self.api.wait(self._handle, 0) != WAIT_TIMEOUT:
                raise OwnerProcessError("Syn launcher has already exited")
            watcher = threading.Thread(target=self._watch, name="syn-host-custody-owner", daemon=True)
            self._thread = watcher
            watcher.start()
            return self
        except BaseException:
            self.close()
            raise

    def _watch(self):
        while not self._stop.is_set():
            try:
                result = self.api.wait(self._handle, POLL_MILLISECONDS)
            except BaseException:
                if not self._stop.is_set():
                    self._exit(2)
                return
            if self._stop.is_set():
                return
            if result == WAIT_TIMEOUT:
                continue
            # Terminating this process releases its vault handle and named pipe
            # even if the server thread is blocked in ConnectNamedPipe/ReadFile.
            # Vault persistence already uses an atomic temporary-file replace.
            self._exit(0 if result == WAIT_OBJECT_0 else 2)
            return

    def close(self):
        self._closed = True
        self._stop.set()
        if self._thread is not None:
            try:
                self._thread.join(2)
            except RuntimeError:
                if self._thread.ident is not None:
                    raise
                # Thread.start failed before the Python target began. If an
                # interrupted native bootstrap runs late, _watch sees _stop
                # before ever waiting on or reading this process handle.
            if self._thread.is_alive():
                # Closing a process handle while another thread waits on it is
                # undefined on Windows. Retain ownership if the wait misbehaves.
                raise OwnerProcessError("Launcher monitor did not stop; process handle remains owned")
            self._thread = None
        if self._handle is not None:
            self.api.close(self._handle)
            self._handle = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()
