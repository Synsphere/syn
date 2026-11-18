# Inspect or stop one current-user Syn Windows host-custody broker.
# No vault file is opened, modified or deleted by this helper.
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [ValidateRange(1, 2147483647)]
    [int] $ProcessId
)

$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'Syn host-custody recovery must run in Windows PowerShell.'
}

if (-not ('Syn.HostCustody.RecoveryCommandLine' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
namespace Syn.HostCustody {
    public static class RecoveryCommandLine {
        [DllImport("shell32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        private static extern IntPtr CommandLineToArgvW(string text, out int count);
        [DllImport("kernel32.dll")]
        private static extern IntPtr LocalFree(IntPtr memory);
        public static string[] Parse(string text) {
            if (String.IsNullOrWhiteSpace(text) || text.Length > 32768)
                throw new ArgumentException("Invalid process command line.");
            int count;
            IntPtr memory = CommandLineToArgvW(text, out count);
            if (memory == IntPtr.Zero)
                throw new Win32Exception(Marshal.GetLastWin32Error());
            try {
                if (count < 1 || count > 128)
                    throw new ArgumentException("Process command line exceeds its bound.");
                string[] result = new string[count];
                for (int i = 0; i < count; i++)
                    result[i] = Marshal.PtrToStringUni(Marshal.ReadIntPtr(memory, i * IntPtr.Size));
                return result;
            } finally {
                LocalFree(memory);
            }
        }
    }
}
'@
}

$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value

function Get-SynBrokerRecord {
    param([Parameter(Mandatory = $true)] $NativeProcess)
    if ($NativeProcess.Name -notmatch '\Apython(?:w)?\.exe\z' -or
        -not ($NativeProcess.CreationDate -is [datetime])) {
        return $null
    }
    try {
        $arguments = [Syn.HostCustody.RecoveryCommandLine]::Parse($NativeProcess.CommandLine)
    } catch { return $null }
    # The executable must actually invoke this module. A -c string, script path,
    # uv wrapper or another module mentioning the broker text is not a match.
    if ($arguments.Count -lt 5 -or $arguments[1] -cne '-m' -or
        $arguments[2] -cne 'syn_host_custody.broker') {
        return $null
    }
    $pipeName = $null
    for ($index = 3; $index -lt $arguments.Count; $index++) {
        if ($arguments[$index] -ceq '--pipe') {
            if ($null -ne $pipeName -or $index + 1 -ge $arguments.Count) { return $null }
            $index++
            $pipeName = $arguments[$index]
        } elseif ($arguments[$index].StartsWith('--pipe=', [StringComparison]::Ordinal)) {
            if ($null -ne $pipeName) { return $null }
            $pipeName = $arguments[$index].Substring(7)
        }
    }
    if ($null -eq $pipeName -or $pipeName -cnotmatch '\ASynHostCustody-[a-f0-9]{32}\z') {
        return $null
    }
    try {
        $owner = Invoke-CimMethod -InputObject $NativeProcess -MethodName GetOwnerSid
        if ($owner.ReturnValue -ne 0 -or $owner.Sid -cne $currentSid) { return $null }
    } catch { return $null }
    return [pscustomobject]@{
        ProcessId = [int] $NativeProcess.ProcessId
        ParentProcessId = [int] $NativeProcess.ParentProcessId
        StartedAt = $NativeProcess.CreationDate.ToUniversalTime().ToString('o')
        Pipe = $pipeName
        ExecutablePath = $NativeProcess.ExecutablePath
        CommandLine = $NativeProcess.CommandLine
        CreationTime = $NativeProcess.CreationDate.ToUniversalTime()
    }
}

if (-not $PSBoundParameters.ContainsKey('ProcessId')) {
    # Listing is always read-only, including when several Syn launchers exist.
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
        ForEach-Object { Get-SynBrokerRecord $_ } |
        Where-Object { $null -ne $_ } |
        Select-Object ProcessId, ParentProcessId, StartedAt, Pipe, ExecutablePath, CommandLine
    return
}

$ownedProcess = $null
try {
    $ownedProcess = [Diagnostics.Process]::GetProcessById($ProcessId)
    # Open and retain the actual kernel process handle BEFORE querying CIM. All
    # later operations use this Process object, not a fresh PID-based lookup.
    $null = $ownedProcess.Handle
    if ($ownedProcess.HasExited) { throw "Process $ProcessId has already exited." }
    $started = $ownedProcess.StartTime.ToUniversalTime()
    $native = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId"
    if ($null -eq $native) { throw "Process $ProcessId has already exited." }
    $record = Get-SynBrokerRecord $native
    if ($null -eq $record) {
        throw "Process $ProcessId is not a verified current-user Syn host-custody broker; nothing was stopped."
    }
    # CIM_DATETIME has microsecond precision; truncate only the sub-microsecond
    # FILETIME component. Integer tick arithmetic avoids floating-point rounding.
    $nativeTicks = $started.Ticks - ($started.Ticks % 10)
    $cimTicks = $record.CreationTime.Ticks - ($record.CreationTime.Ticks % 10)
    if ($nativeTicks -ne $cimTicks) {
        throw "Process $ProcessId changed while it was inspected; nothing was stopped."
    }
    if ($ownedProcess.HasExited) { throw "Process $ProcessId has already exited." }
    $target = 'Syn host-custody broker PID ' + $ProcessId + ' (' + $record.Pipe + ')'
    if ($PSCmdlet.ShouldProcess($target, 'Stop this verified broker and release its vault lock')) {
        try {
            $ownedProcess.Kill()
        } catch {
            if (-not $ownedProcess.HasExited) { throw }
        }
        if (-not $ownedProcess.WaitForExit(5000)) {
            throw "Stop was requested for broker $ProcessId, but exit was not confirmed within five seconds."
        }
        [pscustomobject]@{ ProcessId = $ProcessId; Stopped = $true; Pipe = $record.Pipe }
    }
} finally {
    if ($null -ne $ownedProcess) { $ownedProcess.Dispose() }
}
