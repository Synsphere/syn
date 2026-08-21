param(
    [string]$RepositoryRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
$apiRoot = 'https://www.moltbook.com/api/v1'
$credentialPath = Join-Path $env:USERPROFILE '.config\moltbook\credentials.json'
$archiveDirectory = Join-Path $RepositoryRoot 'synny\reports\moltbook-posts'
$synnyId = 'be582e7c-39d1-42e8-a432-a0f9106f71d6'

if (-not (Test-Path -LiteralPath $credentialPath)) {
    throw "Moltbook credential store is unavailable."
}

$apiKey = (Get-Content -LiteralPath $credentialPath -Raw | ConvertFrom-Json).api_key
if ([string]::IsNullOrWhiteSpace($apiKey)) {
    throw "Moltbook API key is unavailable."
}

Add-Type -AssemblyName System.Net.Http
$httpClient = [System.Net.Http.HttpClient]::new()
$httpClient.DefaultRequestHeaders.Authorization = [System.Net.Http.Headers.AuthenticationHeaderValue]::new('Bearer', $apiKey)

function Invoke-MoltbookGet {
    param([string]$Uri)

    $response = $httpClient.GetAsync($Uri).GetAwaiter().GetResult()
    $bytes = $response.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
    $body = [System.Text.Encoding]::UTF8.GetString($bytes)
    if (-not $response.IsSuccessStatusCode) {
        throw "Moltbook GET failed ($([int]$response.StatusCode)): $Uri"
    }
    return $body | ConvertFrom-Json
}

$identity = Invoke-MoltbookGet -Uri "$apiRoot/agents/me"
if (-not $identity.success -or $identity.agent.id -ne $synnyId) {
    throw "Authenticated Moltbook identity is not synny."
}

function Get-AllPostIds {
    $terms = @(
        'Syn', 'owner', 'daily', 'truth', 'mark', 'knowledge', 'meaning', 'trust',
        'identity', 'participation', 'authority', 'unknown', 'proximity', 'memory',
        'signature', 'ranked', 'learning'
    )
    $posts = @{}

    foreach ($term in $terms) {
        $escaped = [uri]::EscapeDataString($term)
        $search = Invoke-MoltbookGet -Uri "$apiRoot/search?q=$escaped&type=posts&limit=100&author=synny"
        foreach ($result in $search.results) {
            if ($result.type -eq 'post' -and $result.author.id -eq $synnyId) {
                $posts[$result.id] = $true
            }
        }
    }

    $authorFeed = Invoke-MoltbookGet -Uri "$apiRoot/posts?author=synny&limit=100"
    foreach ($post in $authorFeed.posts) {
        if ($post.author_id -eq $synnyId) {
            $posts[$post.id] = $true
        }
    }

    if (Test-Path -LiteralPath $archiveDirectory) {
        foreach ($file in Get-ChildItem -LiteralPath $archiveDirectory -Filter '*.md' -File) {
            if ($file.BaseName -match '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') {
                $posts[$file.BaseName] = $true
            }
        }
    }

    return @($posts.Keys | Sort-Object)
}

function Get-AllComments {
    param([string]$PostId)

    $comments = @()
    $cursor = $null
    do {
        $uri = "$apiRoot/posts/$PostId/comments?sort=old&limit=100"
        if ($cursor) {
            $uri += '&cursor=' + [uri]::EscapeDataString($cursor)
        }
        $page = Invoke-MoltbookGet -Uri $uri
        $comments += @($page.comments)
        $cursor = if ($page.has_more) { $page.next_cursor } else { $null }
    } while ($cursor)

    return $comments
}

function New-FencedBlock {
    param([AllowEmptyString()][string]$Text)

    $longest = 0
    foreach ($match in [regex]::Matches($Text, '`+')) {
        $longest = [Math]::Max($longest, $match.Length)
    }
    $fence = '`' * [Math]::Max(3, $longest + 1)
    return "$fence`n$Text`n$fence"
}

function Add-CommentTree {
    param(
        [System.Text.StringBuilder]$Builder,
        [object[]]$Comments,
        [int]$HeadingLevel = 3
    )

    foreach ($comment in $Comments) {
        $authorName = if ($comment.author.name) { $comment.author.name } else { $comment.author_id }
        $heading = '#' * [Math]::Min(6, $HeadingLevel)
        [void]$Builder.AppendLine("$heading Reply by @$authorName")
        [void]$Builder.AppendLine()
        [void]$Builder.AppendLine("- Comment ID: ``$($comment.id)``")
        [void]$Builder.AppendLine("- Parent ID: ``$($comment.parent_id)``")
        [void]$Builder.AppendLine("- Created: $($comment.created_at)")
        [void]$Builder.AppendLine("- Updated: $($comment.updated_at)")
        [void]$Builder.AppendLine("- State: verification=``$($comment.verification_status)``; deleted=``$($comment.is_deleted)``; spam=``$($comment.is_spam)``")
        [void]$Builder.AppendLine("- Snapshot: $($comment.upvotes) upvotes; $($comment.downvotes) downvotes")
        [void]$Builder.AppendLine()
        [void]$Builder.AppendLine((New-FencedBlock -Text ([string]$comment.content)))
        [void]$Builder.AppendLine()

        if ($comment.replies.Count -gt 0) {
            Add-CommentTree -Builder $Builder -Comments @($comment.replies) -HeadingLevel ($HeadingLevel + 1)
        }
    }
}

function Get-CommentTreeCount {
    param([object[]]$Comments)

    $count = 0
    foreach ($comment in $Comments) {
        $count += 1
        if ($comment.replies.Count -gt 0) {
            $count += Get-CommentTreeCount -Comments @($comment.replies)
        }
    }
    return $count
}

$postIds = Get-AllPostIds
if ($postIds.Count -ne [int]$identity.agent.posts_count) {
    throw "Discovered $($postIds.Count) unique posts, but the authenticated account reports $($identity.agent.posts_count). Archive not rewritten."
}

New-Item -ItemType Directory -Path $archiveDirectory -Force | Out-Null
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
$observedAt = [DateTimeOffset]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ss.fffZ')

foreach ($postId in $postIds) {
    $response = Invoke-MoltbookGet -Uri "$apiRoot/posts/$postId"
    $post = $response.post
    if ($post.author.id -ne $synnyId -and $post.author_id -ne $synnyId) {
        throw "Post $postId is not attributed to synny."
    }
    $comments = @(Get-AllComments -PostId $postId)
    $returnedCommentCount = Get-CommentTreeCount -Comments $comments
    $submoltName = if ($post.submolt.name) { $post.submolt.name } else { $post.submolt_name }

    $builder = [System.Text.StringBuilder]::new()
    [void]$builder.AppendLine("# $($post.title)")
    [void]$builder.AppendLine()
    [void]$builder.AppendLine("Canonical post: https://www.moltbook.com/post/$postId  ")
    [void]$builder.AppendLine("Post ID: ``$postId``  ")
    [void]$builder.AppendLine("Author: ``synny``  ")
    [void]$builder.AppendLine("Submolt: ``m/$submoltName``  ")
    [void]$builder.AppendLine("Archive observed: $observedAt")
    [void]$builder.AppendLine()
    [void]$builder.AppendLine('## API-visible history and state')
    [void]$builder.AppendLine()
    [void]$builder.AppendLine("- Created: $($post.created_at)")
    [void]$builder.AppendLine("- Updated: $($post.updated_at)")
    [void]$builder.AppendLine("- Verification: ``$($post.verification_status)``")
    [void]$builder.AppendLine("- Deleted: ``$($post.is_deleted)``")
    [void]$builder.AppendLine("- Spam: ``$($post.is_spam)``")
    [void]$builder.AppendLine("- Locked: ``$($post.is_locked)``")
    [void]$builder.AppendLine("- Pinned: ``$($post.is_pinned)``")
    [void]$builder.AppendLine("- Snapshot: $($post.upvotes) upvotes; $($post.downvotes) downvotes; $($post.comment_count) comments")
    [void]$builder.AppendLine("- Reply-tree entries returned: $returnedCommentCount")
    [void]$builder.AppendLine()
    [void]$builder.AppendLine('Moltbook exposes the current text and `created_at`/`updated_at` values, not prior edit revisions. This section therefore records the complete history exposed by the API, not an inferred revision history.')
    if ($returnedCommentCount -ne [int]$post.comment_count) {
        [void]$builder.AppendLine()
        [void]$builder.AppendLine("**Visibility warning:** the post reports $($post.comment_count) comments, but the comment-tree API returned $returnedCommentCount. The archive preserves every returned reply but does not invent the missing records.")
    }
    [void]$builder.AppendLine()
    [void]$builder.AppendLine('## Full post text')
    [void]$builder.AppendLine()
    [void]$builder.AppendLine((New-FencedBlock -Text ([string]$post.content)))
    [void]$builder.AppendLine()
    [void]$builder.AppendLine('## Replies')
    [void]$builder.AppendLine()
    if ($comments.Count -eq 0) {
        [void]$builder.AppendLine('_No replies were returned by the Moltbook comment-tree API at the archive cutoff._')
    } else {
        Add-CommentTree -Builder $builder -Comments $comments
    }

    $target = Join-Path $archiveDirectory "$postId.md"
    [System.IO.File]::WriteAllText($target, $builder.ToString(), $utf8NoBom)
}

$expected = @($postIds | ForEach-Object { "$_.md" })
$unexpected = @(Get-ChildItem -LiteralPath $archiveDirectory -Filter '*.md' -File | Where-Object { $_.Name -notin $expected })
if ($unexpected.Count -gt 0) {
    throw "Unexpected archive files exist and were not deleted: $($unexpected.Name -join ', ')"
}

[pscustomobject]@{
    post_count = $postIds.Count
    archive_directory = $archiveDirectory
    observed_at = $observedAt
} | ConvertTo-Json -Compress

$httpClient.Dispose()
