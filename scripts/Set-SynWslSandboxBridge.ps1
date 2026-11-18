# Opt-in Windows 10 WSL2 UPnP/UDP/multicast host bridge. Separate from custody.
# Inspect is read-only; Provision and Cleanup explicitly request approval.
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [ValidateSet('Inspect', 'Provision', 'Start', 'Cleanup')][string]$Action = 'Inspect',
    [Parameter(Mandatory=$true)][string]$LanAddress,
    [string]$Distribution = '',
    [ValidateRange(1024,65535)][int]$FirstPort = 4001,
    [ValidateRange(1024,65535)][int]$LastPort = 4099,
    [ValidateRange(1,64)][int]$MaxMappings = 16,
    [ValidateRange(60,3600)][int]$MaxLeaseSeconds = 1800,
    [switch]$EnableMdns,
    [string[]]$MulticastGroup = @()
)
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'Run this helper on the Windows host, not inside WSL.'
}
if ($FirstPort -gt $LastPort -or $LastPort - $FirstPort -gt 1023) {
    throw 'The approved mapping range must contain at most 1024 ports.'
}
$chosenLan = $null
if (-not [Net.IPAddress]::TryParse($LanAddress, [ref]$chosenLan) -or
    $chosenLan.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) {
    throw 'LanAddress must be a real IPv4 address assigned to the Windows LAN interface.'
}
$windowsAdapter = @(Get-NetIPAddress -AddressFamily IPv4 -IPAddress $LanAddress -ErrorAction SilentlyContinue)
if ($windowsAdapter.Count -ne 1 -or $windowsAdapter[0].InterfaceAlias -match 'WSL|vEthernet|Loopback|VPN') {
    throw 'LanAddress must match exactly one physical Windows LAN interface, not WSL/VPN.'
}
$wsl = Get-Command 'wsl.exe' -ErrorAction Stop
$wslArgs = @()
if ($Distribution) { $wslArgs += @('--distribution', $Distribution) }
$wslAddr = @(& $wsl.Source @wslArgs '--exec' 'hostname' '-I') -join ' '
if ($LASTEXITCODE -ne 0) { throw 'WSL hostname -I failed.' }
$wslIPs = @($wslAddr.Split(' ', [System.StringSplitOptions]::RemoveEmptyEntries) |
    Where-Object { $_ -match '^\d+\.\d+\.\d+\.\d+$' } | Select-Object -Unique)
if ($wslIPs.Count -ne 1) { throw 'Expected one WSL IPv4 address; examine hostname -I.' }
$sandboxIP = $wslIPs[0]
$route = @(& $wsl.Source @wslArgs '--exec' 'ip' '-4' 'route' 'show' 'default') -join ' '
if ($LASTEXITCODE -ne 0 -or $route -notmatch '\bvia\s+(\d+\.\d+\.\d+\.\d+)\b') {
    throw 'Could not identify the Windows host-side WSL default gateway.'
}
$hostIP = $Matches[1]
$virtualAdapter = @(Get-NetIPAddress -AddressFamily IPv4 -IPAddress $hostIP -ErrorAction SilentlyContinue)
if ($virtualAdapter.Count -ne 1 -or $virtualAdapter[0].InterfaceAlias -notmatch 'WSL|vEthernet') {
    throw 'The WSL default gateway is not assigned to a verified Windows virtual interface.'
}
$secretPath = Join-Path $env:ProgramData 'Syn\WslBridge\secret.hex'
$hostScript = Join-Path $PSScriptRoot 'syn_wsl_host_bridge.py'
$sandboxScript = Join-Path $PSScriptRoot 'syn_wsl_sandbox_igd.py'
if (-not (Test-Path -LiteralPath $hostScript -PathType Leaf) -or
    -not (Test-Path -LiteralPath $sandboxScript -PathType Leaf)) {
    throw 'The Syn network-bridge Python scripts are missing.'
}
$present = Test-Path -LiteralPath $secretPath -PathType Leaf
$receipt = [pscustomobject]@{
    WindowsLan = $LanAddress
    WindowsWslGateway = $hostIP
    SandboxIp = $sandboxIP
    ControlPort = 48777
    MulticastPort = 48778
    SecretProvisioned = $present
    PortRange = "$FirstPort-$LastPort"
    MdnsEnabled = [bool]$EnableMdns
    MulticastGroups = @($MulticastGroup)
    Distribution = $Distribution
}
if ($Action -eq 'Inspect') { $receipt; return }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "$Action requires explicit elevated Windows PowerShell approval. Do not elevate the root-custody launcher."
}
if ($Action -eq 'Provision') {
    if ($present) { throw 'Bridge key already provisioned; refusing to replace an active identity.' }
    if (-not $PSCmdlet.ShouldProcess('Windows ProgramData + WSL bridge account', 'Create isolated bridge key and restricted Linux service account')) { return }
    $directory = Split-Path -Parent $secretPath
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
    & icacls.exe $directory '/inheritance:r' '/grant:r' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-18:(OI)(CI)F' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not restrict the bridge directory ACL.' }
    [byte[]]$bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    $hex = ([BitConverter]::ToString($bytes)).Replace('-', '').ToLowerInvariant()
    [IO.File]::WriteAllText($secretPath, $hex, [Text.Encoding]::ASCII)
    & icacls.exe $secretPath '/inheritance:r' '/grant:r' '*S-1-5-32-544:F' '*S-1-5-18:F' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not restrict bridge key ACL. Stop and inspect.' }
    $wslPath = @(& $wsl.Source @wslArgs '--exec' 'wslpath' '-a' $secretPath) | Select-Object -First 1
    if ($LASTEXITCODE -ne 0 -or -not $wslPath) { throw 'WSL could not locate the key file.' }
    # No key bytes appear in a process command line or the terminal.
    $install = 'set -eu; id synbridge >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin synbridge; mkdir -p /var/lib/syn-wsl-bridge; cp "$1" /var/lib/syn-wsl-bridge/secret.hex; chown synbridge /var/lib/syn-wsl-bridge /var/lib/syn-wsl-bridge/secret.hex; chmod 0700 /var/lib/syn-wsl-bridge; chmod 0600 /var/lib/syn-wsl-bridge/secret.hex'
    & $wsl.Source @wslArgs '--user' 'root' '--exec' 'sh' '-c' $install 'syn-bridge' ([string]$wslPath)
    if ($LASTEXITCODE -ne 0) { throw 'WSL bridge-account provisioning failed.' }
    Write-Host 'Bridge key provisioned. Separate from Syn identity and Windows host custody.'
    Write-Host 'The host and WSL virtual gateway must run as distinct processes.'
    return
}
if (-not $present) { throw 'Provision the isolated bridge key first.' }
$uv = Get-Command uv.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
$uvPath = if ($uv) { $uv.Source } else { Join-Path $env:USERPROFILE '.local\bin\uv.exe' }
if (-not (Test-Path -LiteralPath $uvPath -PathType Leaf)) {
    throw 'Windows uv is needed for miniupnpc 2.3.3. Start Syn once to install uv.'
}
$program = @('run','--no-project','--python','3.13','--with','miniupnpc==2.3.3',
             'python', $hostScript, '--windows-lan', $LanAddress, '--windows-wsl', $hostIP,
             '--sandbox-ip', $sandboxIP, '--secret', $secretPath,
             '--port-min', [string]$FirstPort, '--port-max', [string]$LastPort,
             '--max-mappings', [string]$MaxMappings, '--max-lease', [string]$MaxLeaseSeconds)
if ($EnableMdns) { $program += '--mdns' }
foreach ($group in $MulticastGroup) { $program += @('--multicast-group', $group) }
if ($Action -eq 'Cleanup') {
    if (-not $PSCmdlet.ShouldProcess("exact-owned mappings on $LanAddress", 'Remove stale, exact-tagged sandbox bridge mappings')) { return }
    & $uvPath @program '--cleanup-owned'
    if ($LASTEXITCODE -ne 0) { throw 'Bridge cleanup reported an error.' }
    return
}
if (-not $PSCmdlet.ShouldProcess("$LanAddress, sandbox $sandboxIP", 'Start foreground host-side UPnP/TCP/UDP bridge')) { return }
Write-Host 'The Windows bridge is foreground-only; Ctrl+C triggers exact-owner cleanup.'
Write-Host 'Windows firewall rules must be explicitly approved separately; this helper does not add them.'
& $uvPath @program
if ($LASTEXITCODE -ne 0) { throw 'Windows bridge terminated with a failure.' }
