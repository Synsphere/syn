# The WSL process submits one bounded JSON request on stdin; this Windows
# bridge forwards it to a pipe restricted to the currently logged-in user.
[CmdletBinding()]
param([Parameter(Mandatory=$true)][ValidatePattern('^SynHostCustody-[a-f0-9]{32}$')][string] $Pipe)
$ErrorActionPreference = 'Stop'
$client = [System.IO.Pipes.NamedPipeClientStream]::new('.', $Pipe, [System.IO.Pipes.PipeDirection]::InOut, [System.IO.Pipes.PipeOptions]::None)
try {
    $client.Connect(750)
    $streamIn = [System.IO.StreamReader]::new([Console]::OpenStandardInput(), [Text.Encoding]::UTF8)
    $value = $streamIn.ReadLine()
    if ([string]::IsNullOrWhiteSpace($value) -or [Text.Encoding]::UTF8.GetByteCount($value) -gt 140000) {
        throw 'Host custody request is empty or oversized.'
    }
    $writer = [System.IO.StreamWriter]::new($client, [System.Text.UTF8Encoding]::new($false), 4096, $true)
    $writer.NewLine = "`n"
    $writer.WriteLine($value)
    $writer.Flush()
    $reader = [System.IO.StreamReader]::new($client, [Text.Encoding]::UTF8)
    $result = $reader.ReadLine()
    if (-not $result -or [Text.Encoding]::UTF8.GetByteCount($result) -gt 140000) {
        throw 'Host custody broker returned invalid response.'
    }
    [Console]::Out.WriteLine($result)
} catch {
    [Console]::Error.WriteLine("Syn host custody: $($_.Exception.Message)")
    exit 4
} finally {
    $client.Dispose()
}
