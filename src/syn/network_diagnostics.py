"""Bound expected remote libp2p dial noise without hiding fatal host errors.

Scoped to the managed Trio host thread by connection_diagnostics(). No global
logging levels are changed, and an uncategorized error is always logged.
"""
from __future__ import annotations

from collections import Counter
import logging
from threading import Lock, get_ident
import time

# Never include peer-supplied error strings or host addresses in the summary.
_PATTERNS = (
    ("multiselectclient handshake: read failed", "multistreamRead"),
    ("failed to upgrade security for peer", "securityNegotiation"),
    ("failed to open tcp stream", "tcpDial"),
)
_COUNTS: Counter[str] = Counter()
_LAST_REPORTED: dict[str, float] = {}
_LOCK = Lock()
_REPORT_INTERVAL = 60.0


def diagnostics_snapshot() -> dict:
    with _LOCK:
        return {"suppressedCategories": dict(_COUNTS), "reportIntervalSeconds": _REPORT_INTERVAL}


class ConnectionNoiseFilter(logging.Filter):
    """Log first error per category, then at most once per 60 seconds.

    The underlying protocol attempts are unaffected. Failure counts remain
    visible in /p2p/status. The first incident in each category is not hidden.
    """

    def __init__(self, *, now=time.monotonic):
        super().__init__()
        self.thread = get_ident()
        self._clock = now

    def filter(self, record: logging.LogRecord) -> bool:
        if record.thread != self.thread:
            return True
        text = record.getMessage().lower()
        category = next((label for phrase, label in _PATTERNS if phrase in text), None)
        if category is None:
            return True
        with _LOCK:
            now = self._clock()
            previous = _LAST_REPORTED.get(category)
            if previous is None or now - previous >= _REPORT_INTERVAL:
                _LAST_REPORTED[category] = now
                return True
            _COUNTS[category] += 1
            return False
