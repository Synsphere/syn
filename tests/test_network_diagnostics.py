"""Bound only known benign noise and expose counters."""
import importlib.util
import logging
from pathlib import Path

source = Path(__file__).resolve().parents[1]/'src'/'syn'/'network_diagnostics.py'
spec = importlib.util.spec_from_file_location('test_network_diagnostics_module', source)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def record(message):
    return logging.LogRecord('libp2p.test', logging.ERROR, __file__, 1, message, (), None)


def test_retains_first_failure_but_binds_repeats():
    now = [100.0]
    key = 'securityNegotiation'
    mod._LAST_REPORTED.clear()
    mod._COUNTS.clear()
    gate = mod.ConnectionNoiseFilter(now=lambda:now[0])
    msg = record('failed to upgrade security for peer 12D3Koo...')
    assert gate.filter(msg) is True
    assert gate.filter(msg) is False
    assert mod.diagnostics_snapshot()['suppressedCategories'][key] == 1
    assert gate.filter(record('Serious unrelated internal error')) is True
    now[0] = 160.0
    assert gate.filter(msg) is True


def test_does_not_lose_unknown_multiselect_errors():
    mod._LAST_REPORTED.clear()
    mod._COUNTS.clear()
    gate = mod.ConnectionNoiseFilter(now=lambda:100.0)
    assert gate.filter(record('MultiselectClient handshake: read failed: fail to read'))
    assert not gate.filter(record('MultiselectClient handshake: read failed: fail to read'))
    assert gate.filter(record('protocol missing handler'))
    assert mod.diagnostics_snapshot()['suppressedCategories']['multistreamRead'] == 1
