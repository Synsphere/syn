"""Windows-LAN UPnP IGD mapping for Syn's Windows 10 -> WSL2 TCP proxy.

Run only from the Windows host. Never map WSL's 172.x private address on the
router. All router mutations are explicitly requested, read back and owned.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import sys


class IngressError(RuntimeError):
    pass


def _ipv4(value: str) -> str:
    try:
        ip = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as exc:
        raise IngressError("Gateway did not report a valid IPv4 LAN address") from exc
    if ip.is_loopback or ip.is_multicast or ip.is_unspecified or ip.is_link_local:
        raise IngressError("Gateway selected a non-routable Windows LAN interface")
    return str(ip)


def _mapping(raw):
    if raw is None:
        return None
    if not isinstance(raw, (tuple, list)) or len(raw) < 2:
        raise IngressError("Router returned an unrecognised port-mapping record")
    try:
        client, port = _ipv4(str(raw[0])), int(raw[1])
    except (ValueError, TypeError) as exc:
        raise IngressError("Router returned an invalid port-mapping record") from exc
    return {"client": client, "port": port,
            "description": str(raw[2]) if len(raw) > 2 else ""}


def _read_mapping(upnp, tcp_port: int):
    try:
        return _mapping(upnp.getspecificportmapping(tcp_port, "TCP"))
    except Exception as exc:
        # MiniUPnPc may return None *or* raise the numeric IGD code 714 for
        # NoSuchEntryInArray. Never infer absence from arbitrary error text.
        if exc.args and type(exc.args[0]) is int and exc.args[0] == 714:
            return None
        raise IngressError("Router refused mapping readback; cannot establish ownership") from None


def manage(action: str, tcp_port: int, *, upnp, lease_seconds: int = 3600,
           expected_lan: str | None = None) -> dict:
    """Discover an IGD and inspect/renew/delete *only* a Syn-owned TCP rule."""
    if action not in {"inspect", "map", "remove"}:
        raise ValueError("Unknown UPnP action")
    if not 1024 <= tcp_port <= 65535 or not 60 <= lease_seconds <= 7200:
        raise ValueError("Unsupported TCP port or lease")
    # Some MiniUPnPc errors contain the word 'Success'; never infer discovery
    # success from exception text or a standalone claimed external IP.
    count = upnp.discover()
    if type(count) is not int or count <= 0:
        raise IngressError("No UPnP Internet Gateway Device was discovered on Windows LAN")
    upnp.selectigd()
    lan = _ipv4(str(upnp.lanaddr))
    if expected_lan is not None and lan != _ipv4(expected_lan):
        raise IngressError("Router selected a different LAN interface; check VPN/WAN binding")
    public_ip = str(upnp.externalipaddress())
    try:
        is_public = ipaddress.IPv4Address(public_ip).is_global
    except ipaddress.AddressValueError:
        is_public = False
    tag = f"Syn WSL TCP {tcp_port}"
    existing = _read_mapping(upnp, tcp_port)
    owned = existing == {"client": lan, "port": tcp_port, "description": tag}
    if action == "inspect":
        return {"action": action, "lanAddress": lan, "externalIp": public_ip,
                "publicIp": is_public, "mapping": existing, "owned": owned,
                "mappingVerified": owned}
    if existing is not None and not owned:
        raise IngressError("A different router mapping already owns this TCP port; refusing overwrite")
    if action == "remove":
        if owned:
            if not upnp.deleteportmapping(tcp_port, "TCP"):
                raise IngressError("Router refused removal of Syn-owned TCP mapping")
            if _read_mapping(upnp, tcp_port) is not None:
                raise IngressError("Router mapping remained after deletion")
        return {"action": action, "lanAddress": lan, "externalIp": public_ip,
                "publicIp": is_public, "mappingVerified": False, "removed": owned}
    if not is_public:
        raise IngressError("Router external IPv4 is not public; CGNAT/upstream NAT may prevent inbound access")
    # Extend leases on refresh. Never overwrite an unrelated mapping; readback
    # confirms the internal Windows IPv4 and exact destination port.
    result = upnp.addportmapping(tcp_port, "TCP", lan, tcp_port, tag, "", lease_seconds)
    if result is False:
        raise IngressError("Router rejected the UPnP TCP mapping")
    actual = _read_mapping(upnp, tcp_port)
    if actual != {"client": lan, "port": tcp_port, "description": tag}:
        raise IngressError("UPnP mapping readback did not match the requested Windows endpoint")
    return {"action": action, "lanAddress": lan, "externalIp": public_ip,
            "publicIp": is_public, "mappingVerified": True, "leaseSeconds": lease_seconds,
            "destination": f"{lan}:{tcp_port}"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Windows host UPnP mapping for Syn WSL NAT")
    parser.add_argument("action", choices=("inspect", "map", "remove"))
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--lease-seconds", type=int, default=3600)
    parser.add_argument("--multicast-interface", help="Windows LAN IPv4 used for SSDP discovery")
    parser.add_argument("--expected-lan", help="Expected Windows LAN IPv4")
    arguments = parser.parse_args(argv)
    try:
        import miniupnpc
        igd = (miniupnpc.UPnP(multicastif=arguments.multicast_interface)
               if arguments.multicast_interface else miniupnpc.UPnP())
        igd.discoverdelay = 2000
        result = manage(arguments.action, arguments.port, upnp=igd,
                        lease_seconds=arguments.lease_seconds,
                        expected_lan=arguments.expected_lan)
        print(json.dumps({"ok": True, **result}, sort_keys=True))
    except Exception as exc:
        # Do not echo exception text: a router or malicious UPnP responder may
        # place untrusted bytes/credentials in an error message.
        print(json.dumps({"ok": False, "errorType": type(exc).__name__,
                          "message": str(exc) if isinstance(exc, (IngressError, ValueError)) else
                          "Windows host UPnP operation failed"}, sort_keys=True))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
