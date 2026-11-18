"""Tests run in Syn's pinned libp2p environment."""
from multiaddr import Multiaddr

from syn.amino import dialable_tcp_addresses
from syn.outbound_families import OUTBOUND_FAMILIES


def test_dns_address_candidates_skip_unroutable_ipv6(monkeypatch):
    monkeypatch.setattr(OUTBOUND_FAMILIES, 'ipv6', lambda: False)
    candidates = [Multiaddr('/ip6/2606:4700:4700::1111/tcp/4001'),
                  Multiaddr('/ip4/8.8.8.8/tcp/4001')]
    assert [str(item) for item in dialable_tcp_addresses(candidates)] == ['/ip4/8.8.8.8/tcp/4001']


def test_ipv6_route_recovers_and_ipv4_remains_first(monkeypatch):
    monkeypatch.setattr(OUTBOUND_FAMILIES, 'ipv6', lambda: True)
    candidates = [Multiaddr('/ip6/2606:4700:4700::1111/tcp/4001'),
                  Multiaddr('/ip4/8.8.8.8/tcp/4001')]
    assert [str(item) for item in dialable_tcp_addresses(candidates)] == [
        '/ip4/8.8.8.8/tcp/4001', '/ip6/2606:4700:4700::1111/tcp/4001']
