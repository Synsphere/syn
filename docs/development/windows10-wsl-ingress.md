# Windows 10 WSL2 NAT ingress for Syn

For Windows 10 (including build 19045), WSL2 normally runs behind a second NAT.
Enabling UPnP on a DrayTek Vigor router does not open TCP connections to a WSL
listener. Windows must create the router mapping to its own physical LAN IPv4,
then forward that port into WSL2.

This is an opt-in host administration operation, **not** a change to Syn's root
identity custody. It does not write the Syn operator wallet or authorize peers.

## 1. Choose a fixed Syn P2P port

In your active `config.toml`, set:

```toml
[p2p]
listenAddresses = ["/ip4/0.0.0.0/tcp/4001"]
upnpEnabled = false  # WSL itself must not compete with host-side UPnP
```

Retain the other desired `[p2p]` settings. If using a non-default config file,
pass the same config to `Start-Syn.cmd`. A random `tcp/0` listener cannot
be reliably forwarded because the TCP port changes on every restart. Do not
forward the HTTP API on port 8000.

## 2. Launch Syn normally

Start `Start-Syn.cmd` from an **ordinary** terminal and confirm that
`GET http://127.0.0.1:8000/p2p/status` reports `/ip4/0.0.0.0/tcp/4001`.
Leave the process running. You do not need Windows 11 mirrored networking.

## 3. Inspect, then apply from an elevated Windows PowerShell

```powershell
.\scripts\Set-SynWslIngress.ps1 -Action Inspect -Port 4001
.\scripts\Set-SynWslIngress.ps1 -Action Apply -Port 4001
```

`-Action Apply` checks for Windows administrator rights and a running IP Helper
(`iphlpsvc`) service, verifies that the WSL listener is actually reachable
from Windows, selects the physical LAN adapter used by UPnP, refuses to
replace an unrelated `portproxy` or firewall rule, then creates and reads back
an exact TCP mapping on the router. It scopes
the Windows firewall to that LAN IPv4 on Private networks. On a VPN or unusual
multi-homed host, set `-LanAddress <LAN-IPv4>` explicitly.

The helper depends on Windows `uv.exe` and installs pinned `miniupnpc==2.3.3`
on first run. It never uses the Windows custody broker or WSL secrets.

If the router WAN address is private/CGNAT, the helper refuses to claim public
reachability; a second upstream NAT or ISP port sharing must be resolved first.
No external probe is performed: verified router mapping and local WSL
reachability are **not** proof that a remote Internet peer can connect.

## 4. Advertise only a verified public TCP endpoint

After mapping, the helper prints `publicAddress` (e.g. `/ip4/<public-IP>/tcp/4001`).
Copy that exact address to `[p2p].announceAddresses` and restart Syn. Do not
advertise the private WSL IP or the Windows 172.x virtual gateway. Validate an
inbound connection from a network outside your LAN before considering the node
publicly reachable. Syn intentionally does not trust a mutable local text file
or an unauthenticated API request as authority to advertise an IP address.

## 5. Renew and clean up

```powershell
# Elevated Windows PowerShell; keep this terminal running while Syn is online:
.\scripts\Set-SynWslIngress.ps1 -Action Watch -Port 4001

# On shutdown or when no longer publishing:
.\scripts\Set-SynWslIngress.ps1 -Action Remove -Port 4001
```

`Watch` rechecks the WSL IP (which may change at restart), keeps the exact router
mapping fresh, and never takes over another application's port. Windows
`netsh interface portproxy` forwards TCP only. `Remove` deletes **only** the
mapping, proxy, and firewall entries matching Syn's saved ownership receipt.

If Syn restarts while Windows ingress is offline, its manual announced address
may be stale until the mapping is restored; remove it from the config if WAN
availability becomes unreliable. For exact diagnostics, inspect
`Get-NetFirewallRule -Name Syn-Wsl-TCP-4001` and
`netsh interface portproxy show v4tov4`.

The helper is not silently launched as Administrator and does not provision an
unattended privileged Windows service. Host automation should be installed only
with an explicit Administrator decision and a separate service-security review.

## IPv6 outbound behaviour

Syn selects IPv4 for Amino DHT dial candidates when WSL lacks a usable global
IPv6 route. The route probe is local (UDP route selection, no transmitted
packet) and cached for 60 seconds. It periodically re-enables IPv6 when a route
appears. The IPv6 listener is not disabled unless the operator changes the
`listenAddresses` configuration. Expected multiselect and network-dial failures
are rate-limited, with counts in `/p2p/status.dialDiagnostics`; unexpected
errors continue to be logged.

## DNS, Cardano and P2P reachability diagnostics

WSL DNS lookup failures are **not** corrected by an inbound portproxy. Avoid
rewriting `/etc/resolv.conf` or installing a public DNS server automatically;
Windows VPN/adapter policy and per-network DNS must be diagnosed first.

```powershell
# Check DNS from Windows:
Resolve-DnsName backbone.mainnet.emurgornd.com -Type A
# Check DNS from the Syn WSL distribution (set -d if non-default):
wsl.exe --exec getent ahostsv4 backbone.mainnet.emurgornd.com
wsl.exe --exec cat /etc/resolv.conf
# Windows portproxy status and current WSL address:
.\scripts\Set-SynWslIngress.ps1 -Action Inspect -Port 4001
```

If Windows DNS works but WSL DNS fails, examine WSL `/etc/resolv.conf`, Windows
firewall/VPN DNS policy, WSL's actual default IPv4 route, and any generated
resolver configuration. If both fail, fix the upstream DNS path first. The
`/p2p/status.outboundFamilies` report shows the last local IPv6 route check;
this is a **dial hint**, not a guaranteed successful IPv6 remote connection.

This patch intentionally **does not upgrade libp2p or rewrite uv.lock**. It
focuses on Windows 10 NAT ingress, verified host UPnP and conservative outbound
family selection; dependency upgrades should be independently qualified.
