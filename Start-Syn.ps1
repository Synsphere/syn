# Start Syn from Windows without requiring Python or uv on Windows.
# Provisions supported WSL, Linux packages, uv, keyring and local PostgreSQL as needed.
[CmdletBinding()]
param(
    [switch] $ApiOnly,
    [string] $Distribution,
    [string] $Config,
    [string] $OperatorConfig,
    [string] $BindHost = "127.0.0.1",
    [int] $BindPort = 8000,
    [string] $LogLevel = "info",
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
if ($OperatorConfig) {
    $nativeOperator = [System.IO.Path]::GetFullPath($OperatorConfig)
    $linuxOperator = & $wsl.Source @wslArgs '--exec' 'wslpath' '-a' $nativeOperator
    if ($LASTEXITCODE -ne 0 -or -not $linuxOperator) {
        Write-Error 'WSL could not translate the operator config path.'
        exit 2
    }
    $serverArgs += @('--operator-config', (($linuxOperator | Select-Object -First 1).Trim()))
}
$serverArgs += @('--host', $BindHost, '--port', [string]$BindPort, '--log-level', $LogLevel)
if ($SynArguments) { $serverArgs += $SynArguments }

function Install-WindowsUv {
    $uv = Get-Command uv.exe -ErrorAction SilentlyContinue
    if ($null -ne $uv) { return $uv.Source }
    $answer = Read-Host 'Install signed-version Windows uv for Syn host custody? [y/N]'
    if ($answer -notmatch '^(y|yes)$') {
        throw 'Windows uv is required to run host custody; no Linux root-key fallback was used.'
    }
    $installer = Join-Path ([IO.Path]::GetTempPath()) ('syn-uv-' + [guid]::NewGuid().ToString('N') + '.ps1')
    try {
        Invoke-WebRequest -UseBasicParsing -Uri 'https://astral.sh/uv/0.13.8/install.ps1' -OutFile $installer
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer
        if ($LASTEXITCODE -ne 0) { throw 'Windows uv installer returned an error.' }
    } finally { Remove-Item -LiteralPath $installer -ErrorAction SilentlyContinue }
    foreach ($candidate in @("$HOME\.local\bin\uv.exe", "$HOME\.cargo\bin\uv.exe")) {
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    $uv = Get-Command uv.exe -ErrorAction SilentlyContinue
    if ($null -eq $uv) { throw 'Windows uv installer did not provide an executable.' }
    return $uv.Source
}

$hostBroker = $null
$hostBrokerStarted = $false
$brokerError = $null
$brokerOutput = $null
$custodyEnvironment = @{}
foreach ($name in @('SYN_HOST_CUSTODY_PIPE', 'SYN_HOST_CUSTODY_CLIENT_WIN', 'SYN_HOST_CUSTODY_POWERSHELL', 'WSLENV')) {
    $custodyEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
$status = 1
try {
    if (-not $ApiOnly) {
        $hostProject = Join-Path $PSScriptRoot 'host-custody'
        $client = Join-Path $hostProject 'pipe-client.ps1'
        if (-not (Test-Path -LiteralPath (Join-Path $hostProject 'syn_host_custody\broker.py')) -or
            -not (Test-Path -LiteralPath $client)) {
            throw 'Host custody implementation is missing from this Syn checkout.'
        }
        $uv = Install-WindowsUv
        # uv provisions/selects the environment, but is not the lock-owning
        # process. Launch Python directly so cleanup owns the actual broker PID.
        $pythonOutput = & $uv 'run' '--project' $hostProject '--' 'python' '-c' 'import sys; print(sys.executable)'
        if ($LASTEXITCODE -ne 0) { throw 'Could not prepare the Windows host custody Python environment.' }
        $pythonLines = @($pythonOutput | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        if ($pythonLines.Count -ne 1 -or -not [IO.Path]::IsPathRooted($pythonLines[0]) -or
            -not (Test-Path -LiteralPath $pythonLines[0] -PathType Leaf)) {
            throw 'Windows uv did not return one existing absolute Python interpreter path.'
        }
        $brokerPython = $pythonLines[0].Trim()
        $launcherProcess = [System.Diagnostics.Process]::GetCurrentProcess()
        try {
            $launcherCreated = $launcherProcess.StartTime.ToUniversalTime().ToFileTimeUtc().ToString([Globalization.CultureInfo]::InvariantCulture)
        } finally { $launcherProcess.Dispose() }
        $pipeName = 'SynHostCustody-' + [guid]::NewGuid().ToString('N')
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $brokerPython
        $psi.WorkingDirectory = $hostProject
        $psi.Arguments = '-m syn_host_custody.broker --pipe ' + $pipeName +
            ' --owner-pid ' + $PID + ' --owner-created ' + $launcherCreated
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.RedirectStandardError = $true
        $psi.RedirectStandardOutput = $true
        $hostBroker = New-Object System.Diagnostics.Process
        $hostBroker.StartInfo = $psi
        if (-not $hostBroker.Start()) { throw 'Could not start Syn Windows host custody broker.' }
        $hostBrokerStarted = $true
        # Drain redirected streams immediately; a diagnostic must not block the
        # broker on a full pipe, or wait on an unrelated uv child at shutdown.
        $brokerError = $hostBroker.StandardError.ReadToEndAsync()
        $brokerOutput = $hostBroker.StandardOutput.ReadToEndAsync()

        # Never pass the root secret or a generic signing key into WSL. Only
        # the pipe handle and fixed command bridge are forwarded using WSLENV.
        $env:SYN_HOST_CUSTODY_PIPE = $pipeName
        $env:SYN_HOST_CUSTODY_CLIENT_WIN = $client
        $env:SYN_HOST_CUSTODY_POWERSHELL = (Get-Command powershell.exe).Source
        $sharing = 'SYN_HOST_CUSTODY_PIPE/u:SYN_HOST_CUSTODY_CLIENT_WIN/u:SYN_HOST_CUSTODY_POWERSHELL/pu'
        $env:WSLENV = if ($env:WSLENV) { $env:WSLENV + ':' + $sharing } else { $sharing }

        $healthy = $false
        for ($attempt = 0; $attempt -lt 90; $attempt++) {
            if ($hostBroker.HasExited) {
                $diagnostic = $brokerError.GetAwaiter().GetResult()
                if ($diagnostic.Length -gt 4000) { $diagnostic = $diagnostic.Substring(0, 4000) }
                throw "Host custody broker exited: $diagnostic"
            }
            try {
                $reply = '{"op":"health"}' | & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $client -Pipe $pipeName 2>$null
                if ($LASTEXITCODE -eq 0 -and ($reply -join '') -match '"ready":true') {
                    $healthy = $true
                    break
                }
            } catch { }
            Start-Sleep -Seconds 1
        }
        if (-not $healthy) { throw 'Host custody broker did not pass health checks.' }
        Write-Host 'Syn: Windows host custody broker ready; root identity stays on the host.'
    }
    Write-Host 'Syn: starting the native WSL runtime.'
    & $wsl.Source @wslArgs '--exec' 'sh' $linuxBootstrap @serverArgs
    $status = $LASTEXITCODE
} finally {
    try {
        if ($null -ne $hostBroker) {
            try {
                if ($hostBrokerStarted) {
                    if (-not $hostBroker.HasExited) {
                        # This is the exact Python process created above, not a uv
                        # wrapper or a newly looked-up PID. The watcher also covers
                        # abrupt loss of this launcher/console.
                        try { $hostBroker.Kill() }
                        catch { if (-not $hostBroker.HasExited) { throw } }
                    }
                    if (-not $hostBroker.WaitForExit(5000)) {
                        Write-Warning 'Windows host custody did not confirm shutdown; inspect scripts\Stop-SynHostCustody.ps1.'
                    }
                }
            } finally { $hostBroker.Dispose() }
        }
    } finally {
        # Direct .ps1 runs can return to an interactive shell: don't leave that
        # shell pointing at this invocation's now-closed custody pipe.
        foreach ($name in $custodyEnvironment.Keys) {
            [Environment]::SetEnvironmentVariable($name, $custodyEnvironment[$name], 'Process')
        }
    }
}
exit $status
