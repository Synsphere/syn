"""Default-on transport capability policy and explicit per-feature opt-outs."""
import pytest
from syn.config import P2PConfig, loadConfig


def test_default_capabilities_are_enabled():
    config = P2PConfig()
    assert config.enabled and config.mdnsEnabled and config.upnpEnabled
    assert config.quicEnabled and config.webRtcEnabled and config.webTransportEnabled
    assert config.webSocketEnabled and config.dns4Enabled
    assert config.publicGossipsubVersion == "1.2"


def test_default_udp_transport_addresses_are_canonical():
    config = P2PConfig()
    assert config.webRtcListenAddresses == ("/ip4/0.0.0.0/udp/0/webrtc-direct",)
    assert config.webTransportListenAddresses == (
        "/ip4/0.0.0.0/udp/0/quic-v1/webtransport",
    )
    assert P2PConfig(
        webRtcListenAddresses=("/ip6/::/udp/0/webrtc-direct",),
        webTransportListenAddresses=("/ip6/::/udp/0/quic-v1/webtransport",),
    ).webTransportEnabled


def test_config_without_p2p_settings_inherits_defaults(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[database]\nurl = "sqlite:///test"\n')
    actual = loadConfig(config).p2p
    assert actual.quicEnabled and actual.webRtcEnabled and actual.webTransportEnabled
    assert actual.dns4Enabled and actual.webSocketEnabled


def test_transport_opt_outs_are_independent():
    config = P2PConfig(quicEnabled=False, webRtcEnabled=False,
                       webTransportEnabled=False, webSocketEnabled=False,
                       dns4Enabled=False)
    assert not any((config.quicEnabled, config.webRtcEnabled,
                    config.webTransportEnabled, config.webSocketEnabled, config.dns4Enabled))
    assert config.enabled and config.mdnsEnabled and config.upnpEnabled


@pytest.mark.parametrize("field,invalid", [
    ("webRtcEnabled", 1), ("webTransportEnabled", "true"),
    ("webSocketEnabled", None), ("dns4Enabled", 0),
    ("webRtcListenAddresses", ["/ip4/0.0.0.0/udp/0/webrtc-direct"]),
    ("webTransportListenAddresses", ("/ip4/0.0.0.0/tcp/123",)),
    ("webRtcListenAddresses", ("/ip4/0.0.0.0/udp/65536/webrtc-direct",)),
    ("webTransportListenAddresses", ("/ip4/0.0.0.0/udp/4001/quic-v1",)),
])
def test_invalid_transports_rejected(field, invalid):
    with pytest.raises(ValueError, match="p2p\\."):
        P2PConfig(**{field: invalid})
