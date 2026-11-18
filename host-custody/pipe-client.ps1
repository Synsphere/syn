# The WSL process submits one bounded JSON request on stdin; this Windows
# bridge forwards it to a pipe restricted to the currently logged-in user.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidatePattern('^SynHostCustody-[a-f0-9]{32}$')][string] $Pipe,
    # Keep the launcher's fast health probe; runtime calls explicitly allow
    # a bounded queue wait behind another request's native approval dialog.
    [ValidateRange(100, 300000)][int] $ConnectTimeoutMilliseconds = 750,
    [ValidateRange(100, 300000)][int] $ResponseTimeoutMilliseconds = 30000
)
$ErrorActionPreference = 'Stop'
$failure = 'input-invalid'
$client = [System.IO.Pipes.NamedPipeClientStream]::new('.', $Pipe, [System.IO.Pipes.PipeDirection]::InOut, [System.IO.Pipes.PipeOptions]::None)
try {
    $streamIn = [System.IO.StreamReader]::new([Console]::OpenStandardInput(), [Text.Encoding]::UTF8)
    $value = $streamIn.ReadLine()
    if ([string]::IsNullOrWhiteSpace($value) -or [Text.Encoding]::UTF8.GetByteCount($value) -gt 140000) {
        throw 'Host custody request is empty or oversized.'
    }
    $failure = 'connect-failed'
    try { $client.Connect($ConnectTimeoutMilliseconds) }
    catch [System.TimeoutException] { $failure = 'connect-timeout'; throw }
    catch [System.UnauthorizedAccessException] { $failure = 'connect-denied'; throw }
    $elapsed = [Diagnostics.Stopwatch]::StartNew()
    $writer = [System.IO.StreamWriter]::new($client, [System.Text.UTF8Encoding]::new($false), 4096, $true)
    $writer.NewLine = "`n"
    $failure = 'write-failed'
    # Each request is submitted once. A write/read failure is ambiguous and
    # must never trigger a second submission, including for vault mutations.
    $pendingWrite = $writer.WriteLineAsync($value)
    if (-not $pendingWrite.Wait($ResponseTimeoutMilliseconds)) {
        $failure = 'write-timeout'
        throw 'Host custody request write exceeded its deadline.'
    }
    $remaining = $ResponseTimeoutMilliseconds - [int]$elapsed.ElapsedMilliseconds
    if ($remaining -le 0 -or -not $writer.FlushAsync().Wait($remaining)) {
        $failure = 'write-timeout'
        throw 'Host custody request flush exceeded its deadline.'
    }
    $failure = 'read-failed'
    $reader = [System.IO.StreamReader]::new($client, [Text.Encoding]::UTF8)
    $remaining = $ResponseTimeoutMilliseconds - [int]$elapsed.ElapsedMilliseconds
    $pending = $reader.ReadLineAsync()
    if ($remaining -le 0 -or -not $pending.Wait($remaining)) {
        $failure = 'read-timeout'
        throw 'Host custody response exceeded its deadline.'
    }
    $result = $pending.GetAwaiter().GetResult()
    $failure = 'response-invalid'
    if (-not $result -or [Text.Encoding]::UTF8.GetByteCount($result) -gt 140000) {
        throw 'Host custody broker returned invalid response.'
    }
    [Console]::Out.WriteLine($result)
} catch {
    [Console]::Error.WriteLine("SYN_HOST_CUSTODY_FAILURE:$failure")
    exit 4
} finally {
    $client.Dispose()
}
