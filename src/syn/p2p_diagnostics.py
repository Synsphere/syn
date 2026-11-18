"""Name libp2p connection-close codes without changing transport behavior.

py-libp2p 0.7 only names Yamux's original 0/1/2 close codes. Go-libp2p
also uses the shared connection-error code space (core/network/conn.go).
In particular, 4101 / 0x1005 is ordinary connection-manager trimming.
The filter is scoped to the managed host's thread and removed on shutdown.
"""
from __future__ import annotations

from contextlib import contextmanager
import logging
import re
from threading import get_ident

from .network_diagnostics import ConnectionNoiseFilter


_YAMUX_LOGGER = "libp2p.stream_muxer.yamux.yamux"
_CLOSE_MESSAGE = re.compile(
    r"Received GO_AWAY for peer ([1-9A-HJ-NP-Za-km-z]{1,128})"
    r"\s*with unknown error code: ([0-9]{1,10})\Z"
)
_CLOSE_REASONS = {
    0x1000: (logging.WARNING, "protocol negotiation failed"),
    0x1001: (logging.WARNING, "remote resource limit exceeded"),
    0x1002: (logging.WARNING, "remote rate limit"),
    0x1003: (logging.WARNING, "protocol violation"),
    0x1004: (logging.DEBUG, "connection replaced"),
    0x1005: (logging.DEBUG, "connection trimmed by remote peer"),
    0x1006: (logging.INFO, "remote peer shutting down"),
    0x1007: (logging.WARNING, "connection rejected by remote policy"),
}


class _ConnectionCloseFilter(logging.Filter):
    def __init__(self):
        super().__init__()
        self.thread = get_ident()

    def filter(self, record):
        if record.name != _YAMUX_LOGGER or record.thread != self.thread:
            return True
        match = _CLOSE_MESSAGE.fullmatch(record.getMessage())
        if match is None:
            return True
        code = int(match[2])
        reason = _CLOSE_REASONS.get(code)
        if reason is None:
            return True
        record.levelno, description = reason
        record.levelname = logging.getLevelName(record.levelno)
        record.msg = "Remote libp2p connection closed: %s (peer=%s, code=0x%04x)"
        record.args = (description, match[1], code)
        # Logger level was checked before this upstream ERROR became DEBUG.
        # Ancestor loggers do not recheck their own level during propagation.
        return logging.getLogger(_YAMUX_LOGGER).isEnabledFor(record.levelno)


@contextmanager
def connection_diagnostics():
    logger = logging.getLogger(_YAMUX_LOGGER)
    selected = _ConnectionCloseFilter()
    logger.addFilter(selected)
    # py-libp2p emits retryable dial/security failures through its ordinary
    # logging handlers. Filter only Syn's libp2p thread and only known phrases;
    # expose aggregated counts in /p2p/status instead of flooding the console.
    noise = ConnectionNoiseFilter()
    handlers = tuple(dict.fromkeys((*logging.getLogger().handlers,
                                    *logging.getLogger("libp2p").handlers,
                                    *logger.handlers)))
    for handler in handlers:
        handler.addFilter(noise)
    try:
        yield
    finally:
        for handler in handlers:
            handler.removeFilter(noise)
        logger.removeFilter(selected)
