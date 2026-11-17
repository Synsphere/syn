# Start Syn from Windows without requiring Python or uv on Windows.
# Uses the existing WSL distribution and bootstraps Linux uv if absent.
[CmdletBinding()]
param(
    [switch] $ApiOnly,
    [string] $Distribution,
    [string] $Config,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $SynArguments
)

$ErrorActionPreference = 'Stop'
$wsl = Get-Command 'wsl.exe' -ErrorAction SilentlyContinue
if ($null -eq $wsl) {
    Write-Error 'WSL is required for native Syn operator custody. Run wsl --install once from an elevated PowerShell, restart Windows if requested, and launch Start-Syn.cmd again.'
    exit 2
}

$bootstrap = Join-Path $PSScriptRoot 'scripts\bootstrap-wsl.sh'
if (-not (Test-Path -LiteralPath $bootstrap -PathType Leaf)) {
    Write-Error "Missing Syn bootstrap script: $bootstrap"
    exit 2
}

$wslArgs = @()
if ($Distribution) { $wslArgs += @('-d', $Distribution) }

# Arguments are passed as an argv array, never interpolated into sh code.
$linuxBootstrap = & $wsl.Source @wslArgs '--exec' 'wslpath' '-a' $bootstrap
if ($LASTEXITCODE -ne 0 -or -not $linuxBootstrap) {
    Write-Error 'WSL has no usable Linux distribution, or wslpath is missing. Complete initial WSL setup (wsl --install -d Ubuntu) and retry.'
    exit 2
}
$linuxBootstrap = ($linuxBootstrap | Select-Object -First 1).Trim()
if (-not $linuxBootstrap.StartsWith('/')) {
    Write-Error 'WSL returned an invalid bootstrap script path.'
    exit 2
}

$serverArgs = @()
if ($ApiOnly) { $serverArgs += '--api-only' }
if ($Config) {
    $resolved = (Resolve-Path -LiteralPath $Config).Path
    $linuxConfig = & $wsl.Source @wslArgs '--exec' 'wslpath' '-a' $resolved
    if ($LASTEXITCODE -ne 0 -or -not $linuxConfig) {
        Write-Error 'WSL could not translate the requested Syn config path.'
        exit 2
    }
    $serverArgs += @('--config', (($linuxConfig | Select-Object -First 1).Trim()))
}
if ($SynArguments) { $serverArgs += $SynArguments }

Write-Host 'Syn: starting the native WSL runtime.'
& $wsl.Source @wslArgs '--exec' 'sh' $linuxBootstrap @serverArgs
exit $LASTEXITCODE
