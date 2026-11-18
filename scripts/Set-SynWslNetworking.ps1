# Inspect WSL networking; -Apply enables mirrored networking with a backup.
# This helper never shuts down WSL, changes a router, or modifies the firewall.
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [switch] $Apply,
    [ValidateRange(1, 65535)] [int] $TcpPort = 4001,
    [string] $ConfigPath = (Join-Path $env:USERPROFILE '.wslconfig')
)

$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'Run this helper in Windows PowerShell on the WSL host.'
}

function Get-SynMirroredConfig {
    param([AllowEmptyString()] [string] $Text)
    $newline = [regex]::Match($Text, "`r`n|`n|`r").Value
    if (-not $newline) { $newline = "`r`n" }
    $section = ''
    $sections = 0
    $modeLine = $null
    $modeValue = 'nat (default)'
    $insertAt = $Text.Length
    foreach ($line in [regex]::Matches($Text, '(?m)[^\r\n]+|(?:\r\n|\r|\n)')) {
        $value = $line.Value
        if ($value -match '^\s*\[([^\]\r\n]+)\]\s*(?:[;#].*)?$') {
            if ($section -ieq 'wsl2') { $insertAt = $line.Index }
            $section = $Matches[1].Trim()
            if ($section -ieq 'wsl2') {
                $sections++
                if ($sections -gt 1) { throw 'Multiple [wsl2] sections are ambiguous; review .wslconfig manually.' }
            }
        } elseif ($value -match '^\s*\[') {
            throw 'An unrecognized section header prevents a safe .wslconfig edit.'
        } elseif ($section -ieq 'wsl2' -and $value -match '^\s*networkingMode\b') {
            if ($null -ne $modeLine) { throw 'Duplicate networkingMode entries are ambiguous; review .wslconfig manually.' }
            $setting = [regex]::Match($value, '^(\s*networkingMode\s*=\s*)([^;#\r\n]*?)(\s*(?:[;#].*)?)$', 'IgnoreCase')
            if (-not $setting.Success) { throw 'The networkingMode entry is malformed; review .wslconfig manually.' }
            $modeValue = $setting.Groups[2].Value.Trim()
            $modeLine = $line
            $replacement = $setting.Groups[1].Value + 'mirrored' + $setting.Groups[3].Value
        }
    }
    if ($modeValue -ieq 'mirrored') { return @{ Mode = $modeValue; Text = $Text; Changed = $false } }
    if ($null -ne $modeLine) {
        $updated = $Text.Substring(0, $modeLine.Index) + $replacement + $Text.Substring($modeLine.Index + $modeLine.Length)
    } elseif ($sections -eq 1) {
        $prefix = $Text.Substring(0, $insertAt)
        if ($prefix -and $prefix -notmatch '[\r\n]$') { $prefix += $newline }
        $updated = $prefix + 'networkingMode=mirrored' + $newline + $Text.Substring($insertAt)
    } else {
        $updated = $Text
        if ($updated -and $updated -notmatch '[\r\n]$') { $updated += $newline }
        $updated += '[wsl2]' + $newline + 'networkingMode=mirrored' + $newline
    }
    return @{ Mode = $modeValue; Text = $updated; Changed = $true }
}

$os = Get-CimInstance -ClassName Win32_OperatingSystem
$build = 0
if (-not [int]::TryParse([string] $os.BuildNumber, [ref] $build)) {
    throw 'Could not determine the Windows host build; no configuration was changed.'
}
$supported = $build -ge 22621 -and $os.ProductType -eq 1
$target = [IO.Path]::GetFullPath($ConfigPath)
$exists = [IO.File]::Exists($target)
if ([IO.Directory]::Exists($target)) { throw 'ConfigPath names a directory, not a .wslconfig file.' }
$encoding = [Text.UTF8Encoding]::new($false, $true)
[byte[]] $original = @()
$text = ''
if ($exists) {
    $item = Get-Item -LiteralPath $target -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'Use the actual .wslconfig file, not a reparse point.'
    }
    if ($item.Length -gt 1048576) { throw '.wslconfig is unexpectedly large; no changes were made.' }
    $original = [IO.File]::ReadAllBytes($target)
    if ($original.Length -gt 1048576) { throw '.wslconfig grew during inspection; retry after other edits finish.' }
    $reader = [IO.StreamReader]::new([IO.MemoryStream]::new($original, $false), $encoding, $true)
    try {
        $text = $reader.ReadToEnd()
        $encoding = $reader.CurrentEncoding
    } finally { $reader.Dispose() }
    [byte[]] $roundTrip = $encoding.GetPreamble() + $encoding.GetBytes($text)
    if ([Convert]::ToBase64String($roundTrip) -cne [Convert]::ToBase64String($original)) {
        throw 'The .wslconfig encoding cannot be preserved safely; no changes were made.'
    }
}
$plan = Get-SynMirroredConfig -Text $text
$changed = $false
$backup = $null
if ($Apply) {
    if (-not $supported) { throw 'Mirrored networking requires Windows 11 22H2 or later (workstation build 22621+). No changes were made.' }
    if ($plan.Changed -and $PSCmdlet.ShouldProcess($target, 'Set [wsl2] networkingMode=mirrored and preserve a backup')) {
        $parent = [IO.Path]::GetDirectoryName($target)
        if (-not [IO.Directory]::Exists($parent)) { throw 'The configuration directory does not exist.' }
        $temp = Join-Path $parent ('.syn-wsl-' + [guid]::NewGuid().ToString('N') + '.tmp')
        try {
            [byte[]] $updatedBytes = $encoding.GetPreamble() + $encoding.GetBytes($plan.Text)
            [IO.File]::WriteAllBytes($temp, $updatedBytes)
            if ($exists) {
                if ([Convert]::ToBase64String([IO.File]::ReadAllBytes($target)) -cne [Convert]::ToBase64String($original)) {
                    throw '.wslconfig changed during inspection; no edit was applied.'
                }
                $backup = $target + '.syn-backup-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmss') + '-' + [guid]::NewGuid().ToString('N')
                [IO.File]::Replace($temp, $target, $backup)
            } else {
                # Move refuses an existing destination if another editor won.
                [IO.File]::Move($temp, $target)
            }
            $changed = $true
        } finally {
            if ([IO.File]::Exists($temp)) { [IO.File]::Delete($temp) }
        }
    }
}

[pscustomobject]@{
    WindowsBuild = $build
    HostSupportsMirrored = $supported
    ConfigPath = $target
    ConfiguredMode = $(if ($changed) { 'mirrored' } else { $plan.Mode })
    ChangeRequired = $plan.Changed -and -not $changed
    Changed = $changed
    Backup = $backup
}
Write-Host 'Mirrored mode needs a current WSL 2 installation. Check wsl --version and wsl --list --verbose; update WSL if needed.'
Write-Host 'Configuration changes affect all WSL 2 distributions and take effect after a WSL restart.'
Write-Host 'After saving work and stopping Syn and other WSL workloads, run wsl --shutdown yourself, then restart Syn. This helper does not stop WSL.'
Write-Host ('Use a matching fixed P2P listener in the active Syn config: listenAddresses = ["/ip4/0.0.0.0/tcp/' + $TcpPort + '"]')
Write-Host 'If inbound TCP is blocked, review this port-specific rule in an administrator PowerShell. This helper does not run it:'
Write-Host ('New-NetFirewallHyperVRule -Name "Syn-WSL-TCP-' + $TcpPort + '" -DisplayName "Syn WSL TCP ' + $TcpPort + '" -Direction Inbound -VMCreatorId ''{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}'' -Protocol TCP -LocalPorts ' + $TcpPort + ' -Action Allow')
Write-Host 'Mirrored networking supports multicast for SSDP discovery; router, VPN and firewall policies still affect reachability.'
