# Optional Windows 10 / WSL2 sandbox networking bridge

Target source revision: `0b40e5b8bacf9bc7191ed4bae36776712c8d5b5a`.

This is a separate **experimental opt-in** host service, not an update to the
existing `Set-SynWslIngress.ps1` TCP portproxy and not an official Syn release.

## Capabilities

- WSL-local virtual UPnP Internet Gateway Device (IGD): SSDP M-SEARCH discovery
  and bounded WANIPConnection SOAP control.
- Windows host-side HMAC-authenticated forwarding requests with freshness and
  replay protection; only one pinned WSL IPv4 endpoint and an explicit external
  port range are eligible.
- The Windows host sets up ephemeral TCP or UDP forwarding sockets and requests
  matching router UPnP mappings, with ownership readback, capped leases, cleanup
  and explicit orphan removal. Existing non-Syn router mappings and occupied
  local ports are not overwritten.
- **Optional**, independently enabled IPv4 multicast reflector for mDNS
  (`224.0.0.251:5353`) and up to seven additional approved multicast
  `GROUP:PORT` pairs. Includes authenticated transport envelopes, replay
  checks, rate limits and duplicate suppression.

The bridge has its **own key**. It must never use the Syn Operator identity,
host custody broker credentials, a root wallet seed, or Agent credentials.

## Important limits

- Windows `netsh interface portproxy` only forwards TCP. This module implements
  a separate UDP application proxy for QUIC; it does not change `netsh` rules.
- The Windows launcher requires administrator approval for provisioning,
  starting and cleanup. It deliberately does **not** create Windows firewall
  rules or silently change router configuration on start.
- Use a **new port range**. TCP port 4001 may already be occupied by Syn's
  existing portproxy. A binding conflict is refused, not replaced. For example,
  reserve only TCP/UDP `42000–42010` for experimental sandbox mappings.
- The WSL virtual IGD replies to applications on the pinned WSL interface.
  Multicast and SSDP may not reach isolated network namespaces without an
  explicitly designed namespace relay. This does not give isolated Linux
  sandboxes an automatic route to the virtual IGD.
- This is not an Ethernet bridge: IPv4/UDP multicast sender addresses, TTL,
  socket scope, DNS-SD interface metadata, and some discovery payloads cannot
  be preserved transparently. Verify mDNS and other multicast tools individually.
- UDP forwarding preserves separate application-layer return sessions, but
  QUIC migration between client addresses and Internet reachability are not
  proven by a local UDP test.
- The host broker does not establish per-process authenticated identity. Its
  mapping policy is **per pinned WSL endpoint**, and any local application
  that can access the virtual IGD can request mappings within that endpoint's
  approved policy. To isolate mutually untrusted sandboxes, provision distinct
  bridge endpoints/keys/port ranges.
- No automatic public QUIC announcements. A successful router mapping is
  **not** an external reachability test.
- Do not run an mDNS reflector if another multicast program has a conflicting
  UDP 5353 socket; opt-out by default.
- Stop the bridge and run explicit cleanup if interrupted. Router leases also
  expire according to the caps.

## Start on Windows 10 (PowerShell)

Use an elevated PowerShell for the **separate bridge only**. Do not elevate
the Syn root-custody launcher. Replace the example LAN address with the
actual Windows LAN IPv4; it must belong to a physical LAN adapter.

```powershell
cd C:\Users\Kieran\source\Syn-position-geodetic
.\scripts\Set-SynWslSandboxBridge.ps1 -Action Inspect -LanAddress 192.168.1.50 -FirstPort 42000 -LastPort 42010
.\scripts\Set-SynWslSandboxBridge.ps1 -Action Provision -LanAddress 192.168.1.50 -FirstPort 42000 -LastPort 42010
.\scripts\Set-SynWslSandboxBridge.ps1 -Action Start -LanAddress 192.168.1.50 -FirstPort 42000 -LastPort 42010
```

The `Provision` action creates a separate Windows ProgramData key and copies it
to a Linux `synbridge` service account under mode `0600`. Run it once.
`Start` is foreground-only, so stop it with Ctrl+C. It requires Windows `uv`
and downloads `miniupnpc` if needed.

In a **separate WSL terminal**, start the sandbox-facing gateway:

```bash
bash scripts/Start-SynWslSandboxBridge.sh
```

The default virtual IGD listens on `WSL_IPV4:48879`, announces over SSDP
`239.255.255.250:1900` and sends authenticated host control requests to
`WINDOWS_WSL_GATEWAY:48777`. Its advertised SSDP LOCATION points to the WSL
address, not to the actual router. Confirm that the sandbox application's
network namespace can reach both SSDP and HTTP. On Windows 10 WSL NAT the
host-side service must remain running for active mappings.

For opt-in mDNS, enable it **on both sides**, for example:

```powershell
.\scripts\Set-SynWslSandboxBridge.ps1 -Action Start -LanAddress 192.168.1.50 -FirstPort 42000 -LastPort 42010 -EnableMdns
```

```bash
bash scripts/Start-SynWslSandboxBridge.sh --mdns
```

For one extra approved multicast group, add
`-MulticastGroup '239.255.255.250:1900'` on Windows and
`--multicast-group 239.255.255.250:1900` inside WSL **only after disabling
the WSL virtual IGD's own SSDP responder** (`--without-ssdp`). Normally keep
the virtual IGD's SSDP rather than reflecting raw SSDP, since it is required
for request translation and ownership. Avoid doubling up discovery traffic.

After shutting down the bridge, optionally clean up only exact-tagged router
mappings (will not delete unrelated mappings):

```powershell
.\scripts\Set-SynWslSandboxBridge.ps1 -Action Cleanup -LanAddress 192.168.1.50 -FirstPort 42000 -LastPort 42010
```

## Testing and status

```bash
python -m pytest -q tests/test_wsl_network_bridge.py
python -m compileall -q scripts/syn_wsl_*py
```

Offline tests cover authenticated control, replay protection, ownership,
lease expiry, multi-client UDP return paths, TCP proxying, virtual IGD SOAP,
and multicast packet validation. A real Windows 10, DrayTek IGD, mDNS client,
external QUIC handshake, Windows firewall and WSL namespace acceptance test
are **not** covered by the offline suite.

A follow-up milestone should add operator-owned native daemon lifecycle,
Windows firewall-rule review/installation, per-sandbox identity isolation,
DNS-SD service-data rewriting where necessary, and independent WAN QUIC probes.
