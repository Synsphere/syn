# Runtime network recovery

This update addresses the reported startup and connection problems in Syn's
existing primitives. It includes UPnP, WSL networking guidance, DHT/GossipSub
retry handling, Bluetooth prerequisites and Cardano connection diagnostics.

## What changed

- UPnP discovery and mapping run in the background. An exception whose message
  is `Success` is an inconclusive discovery result, never a fabricated device
  count. Only verified mappings for active, LAN-accessible IPv4 TCP listeners
  contribute automatic public addresses. Syn retries failures, renews mappings
  and checks its ownership before cleanup.
- Failed DHT peers receive bounded retry backoff. Public GossipSub connection
  attempts are limited to group providers or inbound participants with relevant
  topic evidence; a DHT bootstrap connection alone is insufficient. Ordinary
  remote connection-manager trimming is identified without an alarming unknown
  error-code message.
- WSL provisioning installs the system D-Bus needed by BlueZ even when Windows
  holds Operator credentials. Service startup is bounded; missing Bluetooth
  prerequisites receive specific diagnostics and retry backoff.
- Cardano status distinguishes connection, handshake and later protocol stages,
  records the negotiated version and exposes the next retry delay. Error text
  remains bounded and excludes raw database statements and peer payloads.

## Inspect the UPnP result

With Syn running, inspect the API on the configured local address, for example:

```sh
curl --fail --silent http://127.0.0.1:8000/p2p/status
```

The response's `upnp` object includes:

| Field | Meaning |
| --- | --- |
| `state`, `reason` | Current mapping/discovery outcome and explanation. |
| `devices` | Discovery count; `null` means no trustworthy count was obtained. |
| `environment.wsl`, `environment.networkingMode` | Whether WSL was detected and its reported networking mode. |
| `multicastInterface`, `discoverDelayMs` | Selected interface override and discovery timeout. |
| `lanAddress`, `externalIp` | Addresses reported by the selected gateway. |
| `mappings` | Requested TCP mappings, targets, verification, ownership and lease duration. |
| `inboundReachabilityVerified` | Whether external reachability has been independently established. |

`mapped` means the router mapping was verified. It does **not** prove an Internet
peer can connect; this implementation leaves `inboundReachabilityVerified` false.
An external address alone is insufficient. `no-ipv4-listener` means the selected
gateway cannot reach an eligible IPv4 TCP listener; IPv6-only and loopback-only
listeners are not mapped by this IPv4 UPnP implementation. For other failures,
read `reason` before changing configuration. Discovery and failed mappings retry
without holding up the node's listener startup.

The reported DrayTek Vigor already has UPnP and Connection Control enabled on
WAN2. Keep that confirmed setting and investigate the host networking path.
DrayTek documents `upnp nat` for inspecting the router's actual mapping table in
its [UPnP guide](https://draytek.com/support/knowledge-base/how-to-set-up-upnp).
Check for a mapping to this machine and its actual TCP listener port.

## WSL: inspect first, then enable mirrored networking

Inside the distribution running Syn, inspect the active mode:

```sh
wslinfo --networking-mode
```

`nat` or `virtioproxy` means router UPnP alone cannot expose the WSL listener.
If `wslinfo` is missing, Syn reports an unknown mode; update/check WSL before
assuming the distribution is directly reachable from the LAN.

For Windows 11 22H2 or later with a current WSL 2 installation, mirrored
networking supports multicast and direct LAN access. See Microsoft's
[WSL networking guide](https://learn.microsoft.com/en-us/windows/wsl/networking#mirrored-mode-networking).

From the Syn checkout in **Windows PowerShell**, inspect the helper's report:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Set-SynWslNetworking.ps1
```

Apply the proposed mode change explicitly:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Set-SynWslNetworking.ps1 -Apply -TcpPort 4001
```

The helper preserves unrelated `.wslconfig` settings and makes an exact backup
before replacing an existing file. It supports `-WhatIf`, rejects ambiguous
settings and reports the backup path. The setting affects all WSL 2
distributions. The helper does not stop WSL or create firewall/router rules.

Save work and stop Syn and other WSL workloads, then deliberately restart WSL:

```powershell
wsl --shutdown
```

Restart Syn and check `wslinfo --networking-mode` and `/p2p/status` again. A saved
configuration value is not evidence that the running VM has switched modes.

For predictable troubleshooting, an optional fixed listener in the **active**
Syn configuration is:

```toml
[p2p]
upnpEnabled = true
listenAddresses = ["/ip4/0.0.0.0/tcp/4001"]
```

Merge these settings into the existing `[p2p]` section. `-TcpPort` only selects
the helper's example port; it does not edit Syn's configuration. Automatic UPnP
also supports dynamically allocated listener ports.

If a firewall blocks inbound connections, the helper prints a matching
`New-NetFirewallHyperVRule` command for review and execution in administrator
PowerShell. That command is specific to the chosen TCP port. See Microsoft's
[Hyper-V firewall documentation](https://learn.microsoft.com/en-us/windows/security/operating-system-security/network-security/windows-firewall/hyper-v-firewall).
Use the actual listener port and verify access from another machine.

## If mirrored networking is unavailable

Keep a fixed IPv4 TCP listener and configure the complete forwarding path:

1. Forward the chosen Windows TCP port to the current WSL IPv4 address and
   listener port using Windows `netsh interface portproxy`.
2. Permit that Windows TCP port through the applicable host firewall policy.
3. Map the router's external TCP port to the **Windows LAN IPv4 address** and
   forwarded port, not the private WSL VM address.
4. Configure Syn's `announceAddresses` with the actual public IP/TCP endpoint,
   without a `/p2p/<peer-id>` suffix. Disable automatic UPnP with
   `upnpEnabled = false` when managing the forwarding manually.

Microsoft provides the [Windows-to-WSL forwarding procedure](https://learn.microsoft.com/en-us/windows/wsl/networking#accessing-a-wsl-2-distribution-from-your-local-area-network-lan).
Check the current distribution address after restarts and update the forwarding
if it changed. Preserve other applications' existing portproxy and router rules.
Test the endpoint from outside the LAN before relying on the announcement.
The provided Syn helper does not configure this fallback automatically.

## Native Linux and interface selection

If discovery selects a VPN or virtual adapter, set the LAN IPv4 address assigned
to the computer running Syn, not the router address:

```toml
[p2p]
upnpEnabled = true
upnpMulticastInterface = "192.168.1.50" # replace with this computer's LAN IPv4
upnpDiscoveryDelayMilliseconds = 2000
```

The discovery delay accepts integer values from 100 to 10000 milliseconds.
Increasing it can accommodate a slow response; it does not bridge WSL NAT or
override a firewall. A verified mapping can still be blocked by host firewall
policy or an upstream NAT, so check the full inbound route when mapping succeeds
but external connections fail.

## Validation boundary

Automated tests cover simulated discovery, mapping, ownership and cleanup, peer
retry behaviour and protocol failures. Native Windows helper tests require
Windows PowerShell. The recovery patch has not been tested against the reported
DrayTek, a live Windows/WSL host, physical Bluetooth hardware or a live Cardano
peer in this environment. A router acknowledgement remains distinct from an
external connection test.
