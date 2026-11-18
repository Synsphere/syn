# Syn live runtime health (WSL / Linux)

Syn now offers a **read-only, bounded** health report for an **already running**
node. The command uses existing local status endpoints; it never creates an
Operator, connects to the database directly, configures a firewall, modifies
UPnP mappings, or starts the API.

In one terminal start `Start-Syn.cmd` / Syn normally. In a second terminal,
**inside the same WSL distro**, run:

```sh
uv run --locked syn-server --check-runtime --host 127.0.0.1 --port 8000
```

Alternatively run `uv run --locked syn-server --check-runtime --strict-runtime`
when optional runtime degradation should return nonzero. The probe returns JSON
on stdout with `schema=syn.runtime-health/1`, an `overall` result and individual
checks. Exit codes: **0** locally ready or non-strict degraded, **1** degraded
when strict, **2** critical API/managed-host failure or invalid probe arguments.
Use `--runtime-timeout 3` for a slower WSL environment (0.1–10 seconds per
request). The probe accepts only localhost/127.0.0.1/::1; it never follows
redirects or inherits a proxy, includes no user input in outbound paths, and
caps each response at 256 KiB. It prints only enumerated status labels, not
peer IDs, IP addresses, configuration values, network exceptions, or secrets.

## What the report actually establishes

- `api=responding` means `/v1/capabilities` returned parseable JSON over the
  local API; `p2p=running` additionally confirms that `/p2p/status` reports the
  managed host thread live and a peer identity/listener list present.
- `tcp/quic/webRtcDirect/webTransport=locally-listening` means those listeners
  appear in the managed host's **reported local socket set**. A registered
  adapter is not evidence of socket binding; a listener is not evidence of
  Internet ingress. `dependency-missing` identifies WebRTC media-wheel gaps.
- `webSocket/dns4=configured-not-probed` means the
  feature is configured, **not** that a remote WebSocket or DNS lookup succeeded.
- `upnp=mapping-readback-verified` indicates local router mapping readback only,
  **not** a verified Internet path through Windows or WSL.
- `mdns=configured-not-probed` does not prove that multicast crossed the host;
  a WSL NAT report marks `wslBridge=external-bridge-check-required`. Bluetooth
  with no OS-visible adapter is degraded, not fatal. Cardano peer reachability
  and chain sync are not asserted by the local `running` flag.
- `overall=ready` is **local-service readiness**, **not** public connectivity,
  DNS reachability, radio reception, operator cryptographic attestation, or
  end-to-end database consistency.

If using Windows 10 NAT/virtioproxy, inspect the existing opt-in bridge on the
Windows host **without provisioning it**:

```powershell
.\scripts\Set-SynWslSandboxBridge.ps1 -Action Inspect -LanAddress <YOUR_WINDOWS_LAN_IPV4>
```

Provisioning (`-Action Provision` / `Start`) still requires explicit owner
approval and must follow [the WSL sandbox bridge guide](windows-wsl-sandbox-bridge.md).
No probe automatically opens a router mapping or forwards multicast packets.

### Public transport verification requires an independent vantage point

Run the existing pinned-peer witness **from another network** for each advertised
endpoint, using the exact `/p2p/<PeerID>` multiaddress and the exact transport:

```sh
uv run --locked python scripts/verify_syn_transport.py '/ip4/<PUBLIC_IP>/udp/<PORT>/quic-v1/p2p/<PeerID>'
uv run --locked python scripts/verify_syn_transport.py '/ip4/<PUBLIC_IP>/udp/<PORT>/webrtc-direct/p2p/<PeerID>'
uv run --locked python scripts/verify_syn_transport.py '/ip4/<PUBLIC_IP>/udp/<PORT>/quic-v1/webtransport/p2p/<PeerID>'
```

The witness uses a fresh challenge over a pinned libp2p session and does not
fallback between transports. A successful remote result is scoped to that
witness's network and timestamp; it is not proof of universal availability.
See [transport qualification](transport-qualification.md) and
[network recovery](runtime-network-recovery.md).

## CI regression coverage

`tests/test_runtime_health.py` is automatically exercised by the existing
`tests` application job on Python 3.12/3.13. It exercises synthetic local HTTP
responses, malformed/oversized responses, 503 readiness fences, disabled
features, WSL NAT, independent transport statuses, credential redaction and
strict/non-strict exit paths. These offline tests cannot establish a real
Windows firewall or external peer's reachability; use independent integration
witnesses for those claims.

### Optional authenticated WSL bridge control-plane probe

If the Windows-to-WSL sandbox bridge was **already provisioned** and its
independent 32-byte secret is available as an owner-only WSL file, verify the
bridge control plane without changing mappings:

```sh
python scripts/verify_syn_wsl_bridge.py --host <PINNED_WINDOWS_WSL_IPV4> \
    --secret-file /private/bridge-secret.hex
```

Only the existing `external-ip` read-only HMAC-authenticated operation is
used. Exit **0** confirms an authenticated response from that pinned host;
**2** means unavailable or invalid. It does **not** imply WAN ingress,
correct Windows firewall rules, or multicast delivery. The script never
prints the secret, external IP, router details or untrusted exception text.

If no bridge has been provisioned, do not create one merely to make this probe
succeed: the local `wslBridge=external-bridge-check-required` advisory is
expected under NAT.
