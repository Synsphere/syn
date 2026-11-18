"""Configure all supported libp2p transport adapters without altering custody.

py-libp2p 0.8.1 builds adapters from ``listen_addrs`` *instead* of the
``enable_*`` options when listen addresses are provided. Transport creation
therefore intentionally happens without listen addresses; the caller then
passes its validated addresses to ``host.run``. A local UDP listener is never
proof of a publicly reachable UDP endpoint.
"""
from __future__ import annotations

from importlib.util import find_spec


def webrtc_dependencies_installed() -> bool:
    # aiortc's platform-specific media dependencies are not in the base libp2p
    # wheel. Missing dependencies are a reported degraded capability, never a
    # reason to disable TCP or QUIC or to install packages during startup.
    return all(find_spec(name) is not None for name in ("aiortc", "aioice", "av"))


def enabled_listeners(config, *, webrtc_ready: bool):
    """Return configured listener strings, preserving the original TCP binds."""
    values = list(config.listenAddresses)
    if config.quicEnabled:
        values.extend(config.quicListenAddresses)
    if config.webRtcEnabled and webrtc_ready:
        values.extend(config.webRtcListenAddresses)
    if config.webTransportEnabled:
        values.extend(config.webTransportListenAddresses)
    return values


def create_host(factory, key_pair, config, *, webrtc_ready=None, **kwargs):
    """Build TCP, QUIC, WebRTC Direct, WebTransport and WS as separate adapters.

    The upstream constructor ignores enable_* whenever it can infer transports
    from listen_addrs, so intentionally pass None. No generic relay/WebRTC
    circuit transport is enabled: only WebRTC *Direct* is supported here.
    """
    if webrtc_ready is None:
        webrtc_ready = webrtc_dependencies_installed()
    webrtc = bool(config.webRtcEnabled and webrtc_ready)
    host = factory(key_pair=key_pair, listen_addrs=None,
        enable_tcp=True, enable_quic=config.quicEnabled,
        enable_webrtc=webrtc, enable_webtransport=config.webTransportEnabled,
        enable_websocket=config.webSocketEnabled, **kwargs)
    status = {"tcp": "enabled", "quic": "enabled" if config.quicEnabled else "disabled",
              "webRtcDirect": ("enabled" if webrtc else
                               "missing-optional-dependency" if config.webRtcEnabled else "disabled"),
              "webTransport": "enabled" if config.webTransportEnabled else "disabled",
              "webSocket": "enabled" if config.webSocketEnabled else "disabled",
              "dns4": "enabled" if config.dns4Enabled else "disabled"}
    return host, status


def registered_transports(host) -> list[str]:
    """Describe installed adapters, not requested configuration."""
    manager = host.get_network().transport_manager
    return sorted({type(transport).__name__ for transport in manager.get_transports()})
