import logging
from threading import get_ident

import pytest

from syn.p2p_diagnostics import connection_diagnostics


LOGGER = "libp2p.stream_muxer.yamux.yamux"
PEER = "12D3KooWSq8tyDHTNVFgxgnckYd4N9ZivqGxLpSHNchaDc2V8AT"


def record(code):
    return logging.LogRecord(LOGGER, logging.ERROR, __file__, 1,
        f"Received GO_AWAY for peer {PEER}with unknown error code: {code}", (), None)


def test_connection_trim_is_named_and_filter_lifetime_is_bounded(caplog):
    logger = logging.getLogger(LOGGER)
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    before = list(logger.filters)
    with connection_diagnostics():
        message = record(4101)
        assert logger.filter(message)
        assert message.levelno == logging.DEBUG
        assert "trimmed by remote peer" in message.getMessage()
        assert "code=0x1005" in message.getMessage()
    assert logger.filters == before
    outside = record(4101)
    logger.filter(outside)
    assert outside.levelno == logging.ERROR
    assert "unknown error" in outside.getMessage()


def test_normal_connection_cleanup_is_quiet_at_info_level(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    with connection_diagnostics():
        assert not logging.getLogger(LOGGER).filter(record(4101))


@pytest.mark.parametrize("code", [1, 2, 0x1003, 0x1007, 0xFFFF])
def test_faults_stay_visible_and_other_threads_are_unmodified(code):
    logger = logging.getLogger(LOGGER)
    with connection_diagnostics():
        message = record(code)
        assert logger.filter(message)
        assert message.levelno >= logging.WARNING
        other_thread = record(4101)
        other_thread.thread = get_ident() + 1
        logger.filter(other_thread)
        assert other_thread.levelno == logging.ERROR
        assert "unknown error" in other_thread.getMessage()


def test_filter_does_not_reclassify_unrelated_errors():
    logger = logging.getLogger(LOGGER)
    message = logging.LogRecord(LOGGER, logging.ERROR, __file__, 1,
        "Invalid Yamux frame: %s", (4101,), None)
    with connection_diagnostics():
        logger.filter(message)
    assert message.levelno == logging.ERROR
    assert message.getMessage() == "Invalid Yamux frame: 4101"
