# Start Syn from Windows without requiring Python or uv on Windows.
# Provisions supported WSL, Linux packages, uv, keyring and local PostgreSQL as needed.
[CmdletBinding()]
param(
    [switch] $ApiOnly,
    [string] $Distribution,
    [string] $Config,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $SynArguments
)

$ErrorActionPreference = 'Stop'
function Offer-WslInstallation {
    if ($null -eq $wsl) {
        Write-Error 'Windows Subsystem for Linux is unavailable. Enable WSL in Windows Features and restart Windows.'
        exit 2
    }
    Write-Host 'Syn requires an installed Linux WSL distribution for secure native operator custody.'
    $targetDistro = if ($Distribution) { $Distribution } else { 'Ubuntu' }
    $choice = Read-Host "Install $targetDistro using Windows WSL (administrator approval may be required)? [y/N]"
    if ($choice -notmatch '^(y|yes)$') {
        Write-Error 'WSL installation was declined; no operator identity was created.'
        exit 2
    }
    try {
        $install = Start-Process -FilePath $wsl.Source -Verb RunAs -Wait -PassThru -ArgumentList @('--install', '-d', $targetDistro)
    } catch {
        Write-Error "WSL installation could not start: $_"
        exit 2
    }
    if ($install.ExitCode -ne 0) {
        Write-Error "Windows WSL installer returned status $($install.ExitCode)."
        exit $install.ExitCode
    }
    Write-Host "$targetDistro installation requested. If Windows asks for a restart, restart first. Then launch Start-Syn.cmd again."
    exit 0
}

$wsl = Get-Command 'wsl.exe' -ErrorAction SilentlyContinue
if ($null -eq $wsl) { Offer-WslInstallation }

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
    Offer-WslInstallation
}
$linuxBootstrap = ($linuxBootstrap | Select-Object -First 1).Trim()
if (-not $linuxBootstrap.StartsWith('/')) {
    Write-Error 'WSL returned an invalid bootstrap script path.'
    exit 2
}

$serverArgs = @()
if ($ApiOnly) { $serverArgs += '--api-only' }
if ($Config) {
    # Explicit paths may refer to a new private configuration not created yet.
    $resolved = [System.IO.Path]::GetFullPath($Config)
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
