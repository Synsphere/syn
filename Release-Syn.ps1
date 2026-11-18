#Requires -Version 5.1
<#
.SYNOPSIS
    Prepare, stage and publish the official signed Syn release from Windows 10.
.DESCRIPTION
    Run from Windows PowerShell 5.1 (or PowerShell 7) with git, uv and gh.
    Leave this script OUTSIDE the Syn working tree: the release requires a clean git tree.

    Prepare updates the two co-versioned packages, the exact syn-client dependency,
    the Actions default release tag and uv.lock. It never creates a git tag.
    Stage dispatches Syn's existing protected CI workflow; it signs and uploads
    a seven-asset draft, but does NOT publish it.
    Publish reads a successful STAGING RUN's retained receipt, downloads and
    verifies the complete draft, verifies the Ed25519 release-authority signature,
    requires repository release immutability, and publishes after confirmation.
    It dispatches the repository's independently pinned published-byte verifier.

.EXAMPLE
    .\Release-Syn.ps1 -Phase Prepare -RepoPath 'C:\Code\syn' -Commit -Push
.EXAMPLE
    .\Release-Syn.ps1 -Phase Stage -RepoPath 'C:\Code\syn'
.EXAMPLE
    .\Release-Syn.ps1 -Phase Publish -RepoPath 'C:\Code\syn' -StageRunId 123456789
.EXAMPLE
    .\Release-Syn.ps1 -Phase Verify -RepoPath 'C:\Code\syn' -StageRunId 123456789
#>
[CmdletBinding()]
param(
    [ValidateSet('Prepare', 'Stage', 'Publish', 'Verify', 'Status')]
    [string]$Phase = 'Prepare',

    [ValidatePattern('^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$')]
    [string]$Version = '0.11.0',

    [string]$RepoPath = (Get-Location).Path,
    [switch]$Commit,
    [switch]$Push,
    [long]$StageRunId = 0,
    [switch]$ConfirmPublish
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Repository = 'Synsphere/syn'
$Tag = 'v' + $Version
$FileList = @('pyproject.toml', 'packages/syn-client/pyproject.toml',
              '.github/workflows/release.yml', 'uv.lock')
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Run-Tool {
    param([string]$Name, [string[]]$Arguments)
    $output = @(& $Name @Arguments)
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw ('{0} failed with exit code {1}. See the command output above.' -f $Name, $exitCode)
    }
    return ([string]::Join("`n", [string[]]$output)).Trim()
}

function Need-Tool {
    param([string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw ('Required executable not found: {0}. Install it and ensure it is in PATH.' -f $Name)
    }
}

function Read-Text {
    param([string]$Relative)
    $path = Join-Path $script:Root $Relative
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw ('Missing release source file: {0}' -f $Relative)
    }
    return [System.IO.File]::ReadAllText($path)
}

function Save-Text {
    param([string]$Relative, [string]$Content)
    [System.IO.File]::WriteAllText((Join-Path $script:Root $Relative), $Content, $script:Utf8NoBom)
}

function Replace-Once {
    param([string]$Content, [string]$Pattern, [string]$Replacement, [string]$Label)
    $regex = New-Object System.Text.RegularExpressions.Regex($Pattern,
                 [System.Text.RegularExpressions.RegexOptions]::Multiline)
    if ($regex.Matches($Content).Count -ne 1) {
        throw ('Refusing a speculative edit: expected exactly one {0} in complete source.' -f $Label)
    }
    return $regex.Replace($Content, $Replacement, 1)
}

function Project-Version {
    param([string]$Relative)
    $match = [regex]::Match((Read-Text $Relative),
        '(?m)^version[ \t]*=[ \t]*"(?<version>[0-9]+\.[0-9]+\.[0-9]+)"[ \t]*\r?$')
    if (-not $match.Success) { throw ('Cannot identify exact version in {0}' -f $Relative) }
    return $match.Groups['version'].Value
}

function Assert-Clean {
    if ((Run-Tool 'git' @('status', '--porcelain', '--untracked-files=normal')) -ne '') {
        throw 'Git working tree is dirty. Commit/stash your work; keep Release-Syn.ps1 outside the checkout.'
    }
}

function Assert-Master {
    $branch = Run-Tool 'git' @('branch', '--show-current')
    if ($branch -cne 'master') { throw ('Release requires local master, not {0}.' -f $branch) }
    $origin = Run-Tool 'git' @('remote', 'get-url', 'origin')
    if ($origin -notmatch '^(https://github\.com/|git@github\.com:|ssh://git@github\.com/)(?i:Synsphere/syn)(\.git)?/?$') {
        throw 'Remote origin is not canonical Synsphere/syn.'
    }
}

function Assert-Remote-Head {
    [void](Run-Tool 'git' @('fetch', '--quiet', 'origin', 'master'))
    $local = Run-Tool 'git' @('rev-parse', 'HEAD')
    $remote = Run-Tool 'git' @('rev-parse', 'refs/remotes/origin/master')
    if ($local -cne $remote) {
        throw 'Local master must equal origin/master. Merge/pull and retry; no force push is attempted.'
    }
    return $local
}

function Assert-Prepared-Version {
    $runtime = Project-Version 'pyproject.toml'
    $client = Project-Version 'packages/syn-client/pyproject.toml'
    if ($runtime -cne $Version -or $client -cne $Version) {
        throw ('Expected co-released Syn and syn-client {0}, got {1} / {2}.' -f $Version, $runtime, $client)
    }
    $source = Read-Text 'pyproject.toml'
    if ($source -notmatch ([regex]::Escape('syn-client[governance]==') + [regex]::Escape($Version))) {
        throw 'Syn dependency is not pinned to the co-released syn-client version.'
    }
    $workflow = Read-Text '.github/workflows/release.yml'
    if ($workflow -notmatch ('(?m)^\s*default:\s*' + [regex]::Escape($Tag) + '\s*$')) {
        throw 'Release workflow does not have the selected default tag.'
    }
    [void](Run-Tool 'uv' @('lock', '--check'))
}

function Check-Git-Tag {
    $tags = Run-Tool 'git' @('ls-remote', '--tags', 'origin', ('refs/tags/' + $Tag))
    if ($tags) { throw ('Remote tag already exists: {0}; no release replacement allowed.' -f $Tag) }
}

function Get-Authority {
    $authority = (Read-Text 'release/authority.json' | ConvertFrom-Json)
    if ($authority.profile -cne 'syn.release-authority/1' -or
        $authority.release_key_sha256 -notmatch '^[0-9a-f]{64}$' -or
        $authority.public_key -notmatch '^[0-9a-f]{64}$') {
        throw 'Repository release authority is missing or invalid.'
    }
    $keyBytes = New-Object byte[] 32
    for ($i = 0; $i -lt 32; $i++) {
        $keyBytes[$i] = [Convert]::ToByte($authority.public_key.Substring($i * 2, 2), 16)
    }
    $hasher = [System.Security.Cryptography.SHA256]::Create()
    try {
        $fingerprint = ([BitConverter]::ToString($hasher.ComputeHash($keyBytes))).Replace('-', '').ToLowerInvariant()
    }
    finally { $hasher.Dispose() }
    if ($fingerprint -cne $authority.release_key_sha256) {
        throw 'Release authority fingerprint does not match its public key.'
    }
    return $authority
}

function GitHub-Json {
    param([string]$Path)
    return (Run-Tool 'gh' @('api', ('repos/' + $script:Repository + '/' + $Path)) | ConvertFrom-Json)
}

function Staging-Receipt {
    if ($StageRunId -le 0) { throw 'Specify -StageRunId from the successful release.yml staging run.' }
    $run = GitHub-Json ('actions/runs/' + $StageRunId)
    if ($run.repository.full_name -cne $script:Repository -or
        $run.name -cne 'Syn signed release staging' -or
        $run.event -cne 'workflow_dispatch' -or
        $run.head_branch -cne 'master' -or
        $run.status -cne 'completed' -or $run.conclusion -cne 'success' -or
        $run.path -notmatch '^\.github/workflows/release\.yml@refs/heads/master$' -or
        $run.head_sha -notmatch '^[0-9a-f]{40}$') {
        throw 'The selected run is not a successful official Syn release staging on master.'
    }
    $scratch = Join-Path ([System.IO.Path]::GetTempPath()) ('syn-stage-' + [guid]::NewGuid().ToString('N'))
    [void][System.IO.Directory]::CreateDirectory($scratch)
    try {
        [void](Run-Tool 'gh' @('run', 'download', [string]$StageRunId, '-R', $Repository,
                         '--name', ('syn-release-staging-' + $run.head_sha), '--dir', $scratch))
        $file = Join-Path $scratch 'release-staging-report.json'
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw 'Staging receipt was not retained.' }
        $receipt = ([System.IO.File]::ReadAllText($file) | ConvertFrom-Json)
        $authority = Get-Authority
        if ($receipt.profile -cne 'syn.release-publication-evidence/1' -or
            $receipt.repository -cne $Repository -or
            $receipt.tag -cne $Tag -or
            $receipt.source_commit -cne $run.head_sha -or
            $receipt.manifest_sha256 -notmatch '^[0-9a-f]{64}$' -or
            $receipt.release_key_sha256 -cne $authority.release_key_sha256 -or
            $receipt.published -ne $false -or
            @($receipt.assets).Count -ne 7) {
            throw 'Staging receipt does not match the requested version, source or public authority.'
        }
        return $receipt
    }
    finally {
        Remove-Item -LiteralPath $scratch -Force -Recurse -ErrorAction SilentlyContinue
    }
}

function Expected-Asset-Names {
    return @(
        ('syn-' + $Version + '.tar.gz'),
        ('syn-' + $Version + '-py3-none-any.whl'),
        ('syn_client-' + $Version + '.tar.gz'),
        ('syn_client-' + $Version + '-py3-none-any.whl'),
        ('syn-runtime-requirements-' + $Version + '.txt'),
        ('syn-release-' + $Version + '.json'),
        ('syn-release-' + $Version + '.sig.json')
    )
}

function Assert-Release-Metadata {
    param([object]$Receipt, [bool]$Published)
    $release = GitHub-Json ('releases/tags/' + $Tag)
    if ($release.tag_name -cne $Tag -or
        $release.target_commitish -cne $Receipt.source_commit -or
        [bool]$release.draft -eq $Published -or
        @($release.assets).Count -ne 7) {
        throw 'Release tag, target, draft state, or asset count differs from the approved staging receipt.'
    }
    $expected = @(Expected-Asset-Names)
    $actual = @($release.assets | ForEach-Object { $_.name })
    if (@($actual | Select-Object -Unique).Count -ne 7) { throw 'Duplicate release assets.' }
    foreach ($name in $expected) {
        if ($actual -cnotcontains $name -or @($Receipt.assets) -cnotcontains $name) {
            throw ('Missing or unexpected signed asset: {0}' -f $name)
        }
    }
    if ($Published -and $release.immutable -ne $true) {
        throw 'Published GitHub release is not immutable; do not treat it as qualified.'
    }
    return $release
}

function Assert-Draft-Bytes {
    param([object]$Receipt, [object]$Release)
    $scratch = Join-Path ([System.IO.Path]::GetTempPath()) ('syn-draft-' + [guid]::NewGuid().ToString('N'))
    [void][System.IO.Directory]::CreateDirectory($scratch)
    try {
        [void](Run-Tool 'gh' @('release', 'download', $Tag, '-R', $Repository, '--dir', $scratch))
        $expected = @(Expected-Asset-Names)
        $actual = @(Get-ChildItem -LiteralPath $scratch -File | ForEach-Object { $_.Name })
        if ($actual.Count -ne 7) { throw 'Downloaded draft must contain exactly seven assets.' }
        foreach ($name in $expected) {
            if ($actual -cnotcontains $name) { throw ('Missing downloaded asset: {0}' -f $name) }
        }
        $manifestFile = Join-Path $scratch ('syn-release-' + $Version + '.json')
        $signatureFile = Join-Path $scratch ('syn-release-' + $Version + '.sig.json')
        $digest = (Get-FileHash -LiteralPath $manifestFile -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($digest -cne $Receipt.manifest_sha256) { throw 'Manifest digest differs from signed staging receipt.' }
        $manifest = ([System.IO.File]::ReadAllText($manifestFile) | ConvertFrom-Json)
        if ($manifest.profile -cne 'syn.release-manifest/1' -or
            $manifest.version -cne $Version -or
            $manifest.tag -cne $Tag -or
            $manifest.source_commit -cne $Receipt.source_commit -or
            @($manifest.artifacts).Count -ne 5) {
            throw 'Manifest does not match the pinned source/version or package count.'
        }
        $payloads = @(Expected-Asset-Names | Where-Object { $_ -notlike 'syn-release-*' })
        foreach ($item in $manifest.artifacts) {
            if ($payloads -cnotcontains $item.filename -or
                $item.sha256 -notmatch '^[0-9a-f]{64}$') {
                throw 'Manifest contains an unexpected payload.'
            }
            $file = Join-Path $scratch $item.filename
            $localSize = (Get-Item -LiteralPath $file).Length
            $localHash = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($localSize -ne $item.size -or $localHash -cne $item.sha256) {
                throw ('Package payload differs from signed manifest: {0}' -f $item.filename)
            }
        }
        foreach ($asset in $Release.assets) {
            if ((Get-Item -LiteralPath (Join-Path $scratch $asset.name)).Length -ne $asset.size) {
                throw 'GitHub-reported asset sizes differ from downloaded bytes.'
            }
        }
        # Native Windows PowerShell 5.1 has no Ed25519 verification API. Use
        # a short-lived Python with pinned cryptography, never any private key.
        $verifier = Join-Path $scratch 'verify_signature.py'
        $python = @'
import hashlib, json, sys
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
manifest_path, signature_path, authority_path, expected_source = sys.argv[1:]
manifest = Path(manifest_path).read_bytes()
envelope = json.loads(Path(signature_path).read_bytes())
authority = json.loads(Path(authority_path).read_bytes())
assert set(envelope) == {"profile", "manifest_sha256", "release_key_sha256", "signature"}
assert envelope["profile"] == "syn.release-signature/1"
assert envelope["manifest_sha256"] == hashlib.sha256(manifest).hexdigest()
assert envelope["release_key_sha256"] == authority["release_key_sha256"]
assert hashlib.sha256(bytes.fromhex(authority["public_key"])).hexdigest() == authority["release_key_sha256"]
assert json.loads(manifest)["source_commit"] == expected_source
Ed25519PublicKey.from_public_bytes(bytes.fromhex(authority["public_key"])).verify(
    bytes.fromhex(envelope["signature"]), b"syn.release-manifest/1\0" + manifest)
print("Verified signed Syn release manifest against repository authority")
'@
        [System.IO.File]::WriteAllText($verifier, $python, $script:Utf8NoBom)
        $authorityFile = Join-Path $script:Root 'release/authority.json'
        [void](Run-Tool 'uv' @('run', '--no-project', '--python', '3.12',
            '--with', 'cryptography==50.0.2', 'python', $verifier,
            $manifestFile, $signatureFile, $authorityFile, $Receipt.source_commit))
        Write-Host 'All seven assets and Ed25519 authority signature verified locally.' -ForegroundColor Green
    }
    finally {
        Remove-Item -LiteralPath $scratch -Force -Recurse -ErrorAction SilentlyContinue
    }
}

function Dispatch-Published-Verification {
    param([object]$Receipt)
    [void](Run-Tool 'gh' @('workflow', 'run', 'release.yml', '-R', $Repository,
        '--ref', 'master', '-f', 'operation=verify-published',
        '-f', ('tag=' + $Tag),
        '-f', ('source_commit=' + $Receipt.source_commit),
        '-f', ('manifest_sha256=' + $Receipt.manifest_sha256)))
    Write-Host 'Independent published-byte verification workflow dispatched.' -ForegroundColor Cyan
    Write-Host ('Check: https://github.com/{0}/actions/workflows/release.yml' -f $Repository)
}

function Prepare-Release {
    Assert-Clean
    $head = Assert-Remote-Head
    Check-Git-Tag
    $old = Project-Version 'pyproject.toml'
    $clientOld = Project-Version 'packages/syn-client/pyproject.toml'
    if ($old -cne $clientOld) { throw 'Syn and syn-client versions are already inconsistent.' }
    if ([version]$Version -le [version]$old) {
        throw ('New version {0} must exceed current version {1}.' -f $Version, $old)
    }
    $files = @{}
    foreach ($relative in $FileList) {
        $files[$relative] = [System.IO.File]::ReadAllBytes((Join-Path $script:Root $relative))
    }
    try {
        $top = Read-Text 'pyproject.toml'
        $top = Replace-Once $top ('^(version[ \t]*=[ \t]*")' + [regex]::Escape($old) + '(")') ('${1}' + $Version + '${2}') 'runtime version'
        $top = Replace-Once $top ('("syn-client\[governance\]==)' + [regex]::Escape($old) + '(")') ('${1}' + $Version + '${2}') 'exact syn-client dependency'
        Save-Text 'pyproject.toml' $top
        $client = Read-Text 'packages/syn-client/pyproject.toml'
        $client = Replace-Once $client ('^(version[ \t]*=[ \t]*")' + [regex]::Escape($old) + '(")') ('${1}' + $Version + '${2}') 'client version'
        Save-Text 'packages/syn-client/pyproject.toml' $client
        $workflow = Read-Text '.github/workflows/release.yml'
        $workflow = Replace-Once $workflow ('^([ \t]*default:[ \t]*v)' + [regex]::Escape($old) + '([ \t]*\r?)$') ('${1}' + $Version + '${2}') 'release workflow default tag'
        Save-Text '.github/workflows/release.yml' $workflow
        [void](Run-Tool 'uv' @('lock'))
        Assert-Prepared-Version
        $lock = Read-Text 'uv.lock'
        foreach ($name in @('syn', 'syn-client')) {
            $pattern = '(?m)^\[\[package\]\]\r?\nname = "' + [regex]::Escape($name) + '"\r?\nversion = "' + [regex]::Escape($Version) + '"'
            if ($lock -notmatch $pattern) { throw ('uv.lock still has an incorrect {0} version.' -f $name) }
        }
        [void](Run-Tool 'git' @('diff', '--check'))
    }
    catch {
        foreach ($relative in $FileList) {
            [System.IO.File]::WriteAllBytes((Join-Path $script:Root $relative), $files[$relative])
        }
        throw
    }
    Write-Host ('Updated Syn and syn-client from {0} to {1}; uv.lock regenerated.' -f $old, $Version) -ForegroundColor Green
    if ($Commit) {
        [void](Run-Tool 'git' (@('add', '--') + $FileList))
        [void](Run-Tool 'git' @('commit', '-m', ('release: prepare Syn ' + $Tag + ' (Windows host custody)')))
        $newHead = Run-Tool 'git' @('rev-parse', 'HEAD')
        Write-Host ('Release preparation committed: ' + $newHead)
        if ($Push) {
            [void](Run-Tool 'git' @('push', 'origin', 'master'))
            Write-Host 'Pushed master. Run -Phase Stage next.' -ForegroundColor Green
        }
        else { Write-Host 'Push master before staging: git push origin master' }
    }
    else { Write-Host 'Review git diff, commit the four version files, and push master before staging.' }
}

try {
    Need-Tool 'git'
    Need-Tool 'uv'
    if ($Phase -ne 'Prepare') { Need-Tool 'gh' }
    $script:Root = (Resolve-Path -LiteralPath $RepoPath -ErrorAction Stop).Path
    $repoTop = Run-Tool 'git' @('-C', $script:Root, 'rev-parse', '--show-toplevel')
    $normalizedTop = [System.IO.Path]::GetFullPath($repoTop).TrimEnd('\', '/')
    $normalizedRoot = [System.IO.Path]::GetFullPath($script:Root).TrimEnd('\', '/')
    if (-not [string]::Equals($normalizedTop, $normalizedRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'RepoPath must point to the root of the Syn working tree.'
    }
    Push-Location -LiteralPath $script:Root
    try {
        Assert-Master
        if ($Push -and (-not $Commit -or $Phase -ne 'Prepare')) {
            throw '-Push requires -Phase Prepare -Commit.'
        }
        if ($Commit -and $Phase -ne 'Prepare') { throw '-Commit is only valid for -Phase Prepare.' }
        switch ($Phase) {
            'Prepare' { Prepare-Release }
            'Stage' {
                Assert-Clean
                Assert-Prepared-Version
                $head = Assert-Remote-Head
                Check-Git-Tag
                $all = Run-Tool 'gh' @('release', 'list', '-R', $Repository, '-L', '100', '--json', 'tagName') | ConvertFrom-Json
                if (@($all | Where-Object { $_.tagName -ceq $Tag }).Count -gt 0) {
                    throw 'This version already has a GitHub release; refusing duplicate staging.'
                }
                [void](Run-Tool 'gh' @('workflow', 'run', 'release.yml', '-R', $Repository,
                    '--ref', 'master', '-f', 'operation=stage', '-f', ('tag=' + $Tag)))
                Write-Host ('Dispatched signed draft staging for {0} at {1}.' -f $Tag, $head) -ForegroundColor Green
                Write-Host ('Find the successful workflow run ID: gh run list -R {0} --workflow release.yml --limit 10' -f $Repository)
                Write-Host ('Then run: .\Release-Syn.ps1 -Phase Publish -RepoPath "{0}" -StageRunId RUN_ID' -f $script:Root)
            }
            'Publish' {
                Assert-Clean
                $receipt = Staging-Receipt
                $release = Assert-Release-Metadata $receipt $false
                Assert-Draft-Bytes $receipt $release
                # GitHub REST GET returns 200 only if immutability is enabled.
                [void](Run-Tool 'gh' @('api', ('repos/' + $Repository + '/immutable-releases')))
                Write-Host ('Verified draft: {0}; source {1}; manifest {2}' -f $Tag,
                    $receipt.source_commit, $receipt.manifest_sha256) -ForegroundColor Cyan
                if (-not $ConfirmPublish) {
                    $answer = Read-Host ('Type PUBLISH ' + $Tag + ' to make the immutable release public')
                    if ($answer -cne ('PUBLISH ' + $Tag)) { throw 'Publication cancelled.' }
                }
                [void](Run-Tool 'gh' @('release', 'edit', $Tag, '-R', $Repository, '--draft=false'))
                $published = Assert-Release-Metadata $receipt $true
                Write-Host ('Published immutable release: ' + $published.html_url) -ForegroundColor Green
                Dispatch-Published-Verification $receipt
            }
            'Verify' {
                $receipt = Staging-Receipt
                $published = Assert-Release-Metadata $receipt $true
                Dispatch-Published-Verification $receipt
            }
            'Status' {
                $head = Run-Tool 'git' @('rev-parse', 'HEAD')
                Write-Host ('Syn source: {0}; runtime version: {1}; client version: {2}' -f $head,
                    (Project-Version 'pyproject.toml'), (Project-Version 'packages/syn-client/pyproject.toml'))
                Write-Host ('Release workflow: https://github.com/{0}/actions/workflows/release.yml' -f $Repository)
                Write-Host ('Version tag: ' + $Tag)
            }
        }
    }
    finally { Pop-Location }
}
catch {
    Write-Error ('Syn release aborted: ' + $_.Exception.Message)
    exit 1
}
