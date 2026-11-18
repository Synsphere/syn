param(
    [Parameter(Mandatory = $false)]
    [string]$RepoPath = ".",

    [Parameter(Mandatory = $false)]
    [switch]$NoPush
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

function Invoke-Git {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Args)
    & git @Args
    if ($LASTEXITCODE -ne 0) {
        throw "git $($Args -join ' ') failed with exit code $LASTEXITCODE"
    }
}

function Replace-ExactlyOnce {
    param(
        [string]$Text,
        [string]$Old,
        [string]$New,
        [string]$Path,
        [string]$Description
    )

    $first = $Text.IndexOf($Old, [System.StringComparison]::Ordinal)
    if ($first -lt 0) {
        throw "$Path: expected $Description was not found; refusing to guess."
    }

    $second = $Text.IndexOf($Old, $first + $Old.Length, [System.StringComparison]::Ordinal)
    if ($second -ge 0) {
        throw "$Path: $Description occurs more than once; refusing an ambiguous edit."
    }

    return $Text.Substring(0, $first) + $New + $Text.Substring($first + $Old.Length)
}

$repo = (Resolve-Path $RepoPath).Path
Push-Location $repo
try {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw "git is not available in PATH."
    }

    $inside = (& git rev-parse --is-inside-work-tree 2>$null)
    if ($LASTEXITCODE -ne 0 -or $inside.Trim() -ne "true") {
        throw "$repo is not a Git working tree."
    }

    $branch = (& git branch --show-current).Trim()
    if ($LASTEXITCODE -ne 0 -or $branch -ne "master") {
        throw "Expected local branch master, found '$branch'."
    }

    # Untracked files such as downloaded .patch files are harmless. Tracked
    # modifications are not: don't overwrite the user's work.
    $tracked = & git status --porcelain --untracked-files=no
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to read Git status."
    }
    if ($tracked) {
        throw "Tracked working-tree changes exist. Commit/stash them before running this repair."
    }

    Invoke-Git fetch origin master --prune

    $head = (& git rev-parse HEAD).Trim()
    $remote = (& git rev-parse origin/master).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to resolve HEAD/origin/master."
    }
    if ($head -ne $remote) {
        throw "Local master ($head) is not exactly origin/master ($remote). Refusing to edit the wrong revision."
    }

    $files = @(
        "tests/bluetooth/test_postgres.py",
        "tests/positions/test_postgres.py",
        "tests/test_cardano_postgres.py"
    )

    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)

    foreach ($relative in $files) {
        $full = Join-Path $repo $relative
        if (-not (Test-Path -LiteralPath $full -PathType Leaf)) {
            throw "Missing required file: $relative"
        }

        # Read the actual current file, then canonicalize line endings. This
        # avoids Windows CRLF differences making the transformation unreliable.
        $content = [System.IO.File]::ReadAllText($full)
        $content = $content.Replace("`r`n", "`n").Replace("`r", "`n")

        $createSchema = @'
            connection.execute(text(f'CREATE SCHEMA "{name}"'))
'@
        $createSchema = $createSchema.TrimEnd("`r", "`n")

        $createSchemaAndVersion = @'
            connection.execute(text(f'CREATE SCHEMA "{name}"'))
            connection.execute(text(f'CREATE TABLE "{name}"."alembicVersion" '
                '(version_num varchar(32) NOT NULL PRIMARY KEY)'))
'@
        $createSchemaAndVersion = $createSchemaAndVersion.TrimEnd("`r", "`n")

        $content = Replace-ExactlyOnce `
            -Text $content `
            -Old $createSchema `
            -New $createSchemaAndVersion `
            -Path $relative `
            -Description "isolated CREATE SCHEMA statement"

        $oldSearchPath = '{"options": f"-csearch_path={name}"}'
        $newSearchPath = '{"options": f"-csearch_path={name},public"}'

        $content = Replace-ExactlyOnce `
            -Text $content `
            -Old $oldSearchPath `
            -New $newSearchPath `
            -Path $relative `
            -Description "isolated PostgreSQL search_path"

        # Keep the repository's canonical LF policy and avoid a UTF-8 BOM.
        if (-not $content.EndsWith("`n")) {
            $content += "`n"
        }
        [System.IO.File]::WriteAllText($full, $content, $utf8NoBom)
    }

    Write-Host ""
    Write-Host "Checking generated changes..." -ForegroundColor Cyan

    Invoke-Git diff --check

    # Verify all expected changes exist before committing.
    foreach ($relative in $files) {
        $content = [System.IO.File]::ReadAllText((Join-Path $repo $relative))
        if ($content -notmatch 'CREATE TABLE "\{name\}"\."alembicVersion"') {
            throw "$relative: local alembicVersion guard was not written."
        }
        if ($content -notmatch 'csearch_path=\{name\},public') {
            throw "$relative: public extension schema was not added to search_path."
        }
    }

    # Syntax-only verification requires no database and catches accidental
    # quoting/indentation damage.
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        & python -m py_compile @files
        if ($LASTEXITCODE -ne 0) {
            throw "Python syntax verification failed."
        }
    } else {
        Write-Warning "python is not in PATH; skipping local py_compile. CI will still run."
    }

    Write-Host ""
    & git diff -- $files
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to display the generated diff."
    }

    Invoke-Git add -- $files
    Invoke-Git diff --cached --check

    $staged = (& git diff --cached --name-only)
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to inspect staged files."
    }

    $unexpected = @($staged | Where-Object { $_ -notin $files })
    if ($unexpected.Count -gt 0) {
        throw "Unexpected files are staged: $($unexpected -join ', ')"
    }
    if (@($staged).Count -ne 3) {
        throw "Expected exactly three staged fixture files; found $(@($staged).Count)."
    }

    Invoke-Git commit -m "test: expose PostgreSQL extensions to isolated schemas"

    $commit = (& git rev-parse HEAD).Trim()
    Write-Host ""
    Write-Host "Created commit $commit" -ForegroundColor Green

    if (-not $NoPush) {
        Invoke-Git push origin master
        Write-Host "Pushed master successfully." -ForegroundColor Green
    } else {
        Write-Host "NoPush selected; commit was created locally but not pushed." -ForegroundColor Yellow
    }
}
finally {
    Pop-Location
}
