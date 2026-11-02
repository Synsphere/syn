"""Fault-path ownership tests; real descriptor acceptance runs in native CI."""
import sys
from types import SimpleNamespace

import pytest

from syn.operator.custody import _NoninteractiveSecretService
from syn_client.operator_protocol import SynProtocolError


@pytest.mark.parametrize("action", ["get_password", "set_password"])
@pytest.mark.parametrize("fault", ["none", "absent", "collection", "locked", "duplicates", "search", "operation"])
def test_owned_connection_closes_on_success_and_every_custody_failure(monkeypatch, action, fault):
    closed = []
    connection = SimpleNamespace(close=lambda: closed.append(True))

    def fail():
        raise OSError("synthetic native failure")

    item = SimpleNamespace(is_locked=lambda: False,
                           get_secret=fail if fault == "operation" else lambda: b"fixture")
    matches = [] if fault == "absent" else [item, item] if fault == "duplicates" else [item]
    collection = SimpleNamespace(is_locked=lambda: fault == "locked",
                                 search_items=lambda _: fail() if fault == "search" else matches,
                                 create_item=lambda *a, **kw: fail() if fault == "operation" else None)

    def create(actual):
        assert actual is connection
        if fault == "collection":
            fail()
        return collection

    monkeypatch.setitem(sys.modules, "secretstorage",
                        SimpleNamespace(dbus_init=lambda: connection, Collection=create))
    arguments = ("syn.fixture", "account", "fixture") if action == "set_password" else ("syn.fixture", "account")
    if fault in {"collection", "locked", "duplicates", "search", "operation"}:
        with pytest.raises((OSError, SynProtocolError)):
            getattr(_NoninteractiveSecretService(), action)(*arguments)
    else:
        value = getattr(_NoninteractiveSecretService(), action)(*arguments)
        assert value == ("fixture" if action == "get_password" and fault != "absent" else None)
    assert closed == [True]
