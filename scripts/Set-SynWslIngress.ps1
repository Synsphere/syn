# Windows 10 compatible, operator-authorised Windows LAN -> WSL2 TCP ingress.
# No wallet, identity, or Windows custody broker access is required.
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [ValidateSet('Inspect', 'Apply', 'Remove', 'Watch')][string]$Action = 'Inspect',
    [ValidateRange(1024, 65535)][int]$Port = 4001,
    [string]$Distribution = '',
    [ValidateRange(60, 3600)][int]$LeaseSeconds = 1800,
    [ValidateRange(60, 1800)][int]$RenewSeconds = 600,
    [string]$LanAddress = ''
)
$ErrorActionPreference = 'Stop'
$RegistryPath = 'HKLM:\SOFTWARE\Syn\WslIngress'
$ReceiptName = 'Tcp' + $Port
$RuleName = 'Syn-Wsl-TCP-' + $Port
$UpnpScript = Join-Path $PSScriptRoot 'syn_windows_upnp.py'

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'This helper must run on the Windows host, not inside WSL.'
}

function Test-SynAdmin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
function Read-Receipt {
    $entry = Get-ItemProperty -LiteralPath $RegistryPath -Name $ReceiptName -ErrorAction SilentlyContinue
    if ($null -eq $entry) { return $null }
    try { return ($entry.$ReceiptName | ConvertFrom-Json) }
    catch { throw 'The Syn ingress ownership receipt is corrupt; no changes were made.' }
}
function Read-Proxy {
    $result = @()
    $output = & netsh.exe interface portproxy show v4tov4
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect existing Windows TCP proxy rules.' }
    foreach ($line in @($output)) {
        if ($line -match '^\s*(\d{1,3}(?:\.\d{1,3}){3})\s+(\d{1,5})\s+(\d{1,3}(?:\.\d{1,3}){3})\s+(\d{1,5})\s*$') {
            $result += [pscustomobject]@{Listen=$Matches[1];Port=[int]$Matches[2];Connect=$Matches[3];ConnectPort=[int]$Matches[4]}
        }
    }
    return @($result)
}
function Get-WslAddress {
    $args = @()
    if ($Distribution) { $args += @('--distribution', $Distribution) }
    $args += @('--exec', 'hostname', '-I')
    $raw = & wsl.exe @args
    if ($LASTEXITCODE -ne 0) { throw 'WSL could not report its IPv4 address.' }
    $found = @()
    foreach ($candidate in (($raw -join ' ') -split '\s+')) {
        if (-not $candidate) { continue }
        $parsed = $null
        if (-not [Net.IPAddress]::TryParse($candidate, [ref]$parsed) -or
            $parsed.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) { continue }
        $bytes = $parsed.GetAddressBytes()
        $private = ($bytes[0] -eq 10) -or
            ($bytes[0] -eq 172 -and $bytes[1] -ge 16 -and $bytes[1] -le 31) -or
            ($bytes[0] -eq 192 -and $bytes[1] -eq 168)
        if ($private) { $found += $parsed.ToString() }
    }
    $found = @($found | Select-Object -Unique)
    if ($found.Count -ne 1) {
        throw 'Expected exactly one private WSL IPv4 address; inspect wsl.exe --exec hostname -I.'
    }
    return $found[0]
}
function Invoke-SynUpnp([string]$Verb, [string]$Expected = '') {
    if (-not (Test-Path -LiteralPath $UpnpScript -PathType Leaf)) {
        throw 'Missing scripts/syn_windows_upnp.py.'
    }
    $uv = Get-Command uv.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    $uvPath = if ($uv) { $uv.Source } else { Join-Path $env:USERPROFILE '.local\bin\uv.exe' }
    if (-not (Test-Path -LiteralPath $uvPath -PathType Leaf)) {
        throw 'Windows uv is required for pinned miniupnpc 2.3.3. Start Syn once to install uv.'
    }
    $args = @('run', '--no-project', '--python', '3.13', '--with', 'miniupnpc==2.3.3',
              'python', $UpnpScript, $Verb, '--port', [string]$Port,
              '--lease-seconds', [string]$LeaseSeconds)
    if ($LanAddress) { $args += @('--multicast-interface', $LanAddress) }
    if ($Expected) { $args += @('--expected-lan', $Expected) }
    # uv's progress belongs on stderr; never pipe arbitrary router output into PowerShell.
    $result = & $uvPath @args
    if ($LASTEXITCODE -ne 0) {
        $detail = $null
        try { $detail = ($result | Select-Object -Last 1) | ConvertFrom-Json } catch { }
        if ($detail -and -not $detail.ok -and $detail.message) {
            # Only print the strictly bounded error emitted by our own Python helper.
            throw ("Windows UPnP $Verb failed: " + [string]$detail.message)
        }
        throw "Windows UPnP $Verb failed. Verify uv/Python and router discovery."
    }
    try { $parsed = ($result | Select-Object -Last 1) | ConvertFrom-Json }
    catch { throw 'The UPnP helper returned malformed JSON.' }
    if (-not $parsed.ok) { throw "Windows UPnP $Verb was not verified." }
    return $parsed
}
function Invoke-Netsh([string[]]$Arguments) {
    & netsh.exe @Arguments | Out-Null
    if ($LASTEXITCODE -ne 0) { throw ('Windows portproxy operation failed: ' + ($Arguments -join ' ')) }
}
function Require-OwnedProxy($receipt, $rules) {
    $occupied = @($rules | Where-Object { $_.Port -eq $Port })
    if (-not $receipt -and $occupied.Count -gt 0) {
        throw 'An existing Windows portproxy rule already uses this port; refusing takeover.'
    }
    if ($receipt) {
        $owned = @($occupied | Where-Object {
            $_.Listen -eq $receipt.lanAddress -and $_.Connect -eq $receipt.wslAddress -and
            $_.ConnectPort -eq $Port
        })
        if ($owned.Count -ne 1 -or $occupied.Count -ne 1) {
            throw 'Windows portproxy differs from the stored Syn ownership receipt; refusing modification.'
        }
    }
}
function Assert-FirewallOwnership($receipt) {
    $existing = Get-NetFirewallRule -Name $RuleName -ErrorAction SilentlyContinue
    if ($existing -and -not $receipt) { throw 'A firewall rule already uses the Syn ingress name.' }
    if ($receipt -and -not $existing) { throw 'A previously owned Syn firewall rule is missing.' }
    if ($existing) {
        $ports = $existing | Get-NetFirewallPortFilter
        $addresses = $existing | Get-NetFirewallAddressFilter
        if ($existing.Direction -ne 'Inbound' -or $existing.Action -ne 'Allow' -or
            $ports.Protocol -ne 'TCP' -or [string]$ports.LocalPort -ne [string]$Port -or
            [string]$addresses.LocalAddress -ne [string]$receipt.lanAddress) {
            throw 'Syn firewall rule differs from the stored ownership receipt.'
        }
    }
}
function Apply-Ingress {
    if (-not (Test-SynAdmin)) { throw 'Apply needs an elevated Windows PowerShell; do not elevate the Syn root-custody launcher.' }
    # A portproxy rule can exist in netsh while no TCP listener is active when
    # the IP Helper service is stopped. Refuse to claim working forwarding.
    $ipHelper = Get-Service -Name iphlpsvc -ErrorAction Stop
    if ($ipHelper.Status -ne 'Running') {
        throw 'Windows IP Helper (iphlpsvc) is stopped; start it before enabling TCP portproxy.'
    }
    $receipt = Read-Receipt
    $existing = @(Read-Proxy)
    Require-OwnedProxy $receipt $existing
    Assert-FirewallOwnership $receipt
    $wslIP = Get-WslAddress
    $igd = Invoke-SynUpnp 'inspect' $(if ($receipt) { [string]$receipt.lanAddress } else { '' })
    $lanIP = [string]$igd.lanAddress
    if ($LanAddress -and $lanIP -ne $LanAddress) { throw 'UPnP selected a different Windows LAN address.' }
    if ($receipt -and ($receipt.lanAddress -ne $lanIP -or $receipt.distribution -ne $Distribution)) {
        throw 'Ingress LAN interface or WSL distribution changed; remove the owned mapping first.'
    }
    $local = Get-NetIPAddress -AddressFamily IPv4 -IPAddress $lanIP -ErrorAction SilentlyContinue
    if (-not $local -or @($local | Where-Object { $_.InterfaceAlias -like '*vEthernet*' }).Count -gt 0) {
        throw 'UPnP did not select a verified Windows LAN adapter.'
    }
    $ready = Test-NetConnection -ComputerName $wslIP -Port $Port -InformationLevel Quiet -WarningAction SilentlyContinue
    if (-not $ready) { throw "Syn must already be listening on WSL $wslIP TCP $Port before applying ingress." }
    if (-not $igd.publicIp) { throw 'Router external IPv4 is not public; cannot verify inbound NAT address.' }
    if (-not $PSCmdlet.ShouldProcess("$lanIP`:$Port -> $wslIP`:$Port", 'Create/renew owned portproxy, firewall and UPnP mapping')) { return }
    $createdProxy = $false
    $createdFirewall = $false
    $oldProxyRemoved = $false
    try {
        if ($receipt -and $receipt.wslAddress -ne $wslIP) {
            Invoke-Netsh -Arguments @('interface','portproxy','delete','v4tov4',"listenaddress=$lanIP","listenport=$Port")
            $oldProxyRemoved = $true
        }
        if (-not $receipt -or $receipt.wslAddress -ne $wslIP) {
            Invoke-Netsh -Arguments @('interface','portproxy','add','v4tov4',"listenaddress=$lanIP","listenport=$Port",
                           "connectaddress=$wslIP","connectport=$Port")
            $createdProxy = $true
        }
        if (-not $receipt) {
            New-NetFirewallRule -Name $RuleName -DisplayName "Syn WSL TCP $Port" -Direction Inbound -Action Allow `
                -Profile Private -Protocol TCP -LocalAddress $lanIP -LocalPort $Port | Out-Null
            $createdFirewall = $true
        }
        $mapped = Invoke-SynUpnp 'map' $lanIP
        if (-not $mapped.mappingVerified -or $mapped.lanAddress -ne $lanIP) {
            throw 'Router did not verify an exact Windows LAN TCP mapping.'
        }
        if (-not (Test-Path -LiteralPath $RegistryPath)) { New-Item -Path $RegistryPath -Force | Out-Null }
        $value = [ordered]@{ port=$Port; lanAddress=$lanIP; wslAddress=$wslIP; distribution=$Distribution;
            firewallRule=$RuleName; externalIp=[string]$mapped.externalIp }
        New-ItemProperty -Path $RegistryPath -Name $ReceiptName -PropertyType String -Force `
            -Value ($value | ConvertTo-Json -Compress) | Out-Null
        [pscustomobject]@{ status='mapped'; externalIp=$mapped.externalIp; windowsLan=$lanIP; wslIp=$wslIP;
            port=$Port; nextRenewSeconds=$RenewSeconds; routerVerified=$mapped.mappingVerified;
            publicAddress="/ip4/$($mapped.externalIp)/tcp/$Port" }
    } catch {
        # A failed refresh must not orphan the previous owned proxy. Restore
        # the old WSL destination before preserving its original receipt.
        if ($createdProxy) {
            & netsh.exe interface portproxy delete v4tov4 "listenaddress=$lanIP" "listenport=$Port" | Out-Null
        }
        if ($oldProxyRemoved) {
            try {
                Invoke-Netsh -Arguments @('interface','portproxy','add','v4tov4',
                    "listenaddress=$lanIP","listenport=$Port",
                    "connectaddress=$($receipt.wslAddress)","connectport=$Port")
            } catch {
                Write-Warning 'Could not restore previous portproxy; inspect the stored Syn receipt manually.'
            }
        }
        if ($createdFirewall) { Remove-NetFirewallRule -Name $RuleName -ErrorAction SilentlyContinue }
        throw
    }
}
function Remove-Ingress {
    if (-not (Test-SynAdmin)) { throw 'Remove needs elevated Windows PowerShell.' }
    $receipt = Read-Receipt
    if (-not $receipt) { return [pscustomobject]@{ status='absent'; port=$Port } }
    Require-OwnedProxy $receipt @(Read-Proxy)
    Assert-FirewallOwnership $receipt
    if (-not $PSCmdlet.ShouldProcess("$($receipt.lanAddress):$Port", 'Remove Syn-owned UPnP, portproxy and firewall')) { return }
    $mapped = Invoke-SynUpnp 'remove' ([string]$receipt.lanAddress)
    Invoke-Netsh -Arguments @('interface','portproxy','delete','v4tov4',"listenaddress=$($receipt.lanAddress)","listenport=$Port")
    Remove-NetFirewallRule -Name $RuleName
    Remove-ItemProperty -Path $RegistryPath -Name $ReceiptName
    [pscustomobject]@{ status='removed'; port=$Port; routerRemoved=$mapped.removed }
}

if ($Action -eq 'Inspect') {
    $receipt = Read-Receipt
    $proxy = @(Read-Proxy | Where-Object { $_.Port -eq $Port })
    $igd = try { Invoke-SynUpnp 'inspect' $(if ($receipt) { [string]$receipt.lanAddress } else { '' }) }
           catch { [pscustomobject]@{ ok=$false; error=$_.Exception.Message } }
    $wslAddr = try { Get-WslAddress } catch { $null }
    $helper = Get-Service -Name iphlpsvc -ErrorAction SilentlyContinue
    [pscustomobject]@{ port=$Port; receipt=$receipt; proxy=$proxy; gateway=$igd; wslAddress=$wslAddr;
        ipHelper=$(if ($helper) { [string]$helper.Status } else { 'not-found' }) }
} elseif ($Action -eq 'Remove') {
    Remove-Ingress
} elseif ($Action -eq 'Apply') {
    Apply-Ingress
} else {
    Write-Host 'Syn WSL ingress monitor active; Ctrl+C stops renewal, not the owned mapping.'
    while ($true) {
        try { Apply-Ingress | Format-List }
        catch { Write-Warning ('Syn ingress refresh failed: ' + $_.Exception.Message) }
        Start-Sleep -Seconds $RenewSeconds
    }
}
