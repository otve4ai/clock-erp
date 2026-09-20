[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$remoteName = 'origin'
$mainBranch = 'main'
$expectedRepositoryName = 'clock-erp'
$expectedRepositorySlug = 'otve4ai/clock-erp'
$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))

function Write-NotReadyAndExit {
    param([string]$Message)

    Write-Output ''
    Write-Output "NOT READY - $Message"
    Write-Output 'No automatic changes were made.'
    exit 1
}

function Protect-OutputText {
    param([string]$Text)

    if ([string]::IsNullOrWhiteSpace($Text)) {
        return ''
    }

    $safeText = $Text -replace '(?i)(https?://)[^/\s@]+@', '${1}[credentials-redacted]@'
    $safeText = $safeText -replace '(?i)([?&](?:access_token|token|password|secret)=)[^&\s]+', '${1}[redacted]'
    return $safeText
}

function Get-SafeRemoteUrl {
    param([string]$Url)

    return Protect-OutputText -Text $Url.Trim()
}

function Test-ExpectedOrigin {
    param([string]$Url)

    $escapedSlug = [regex]::Escape($script:expectedRepositorySlug)
    $httpsPattern = "^https?://(?:[^/@]+@)?(?:www\.)?github\.com/$escapedSlug(?:\.git)?/?$"
    $scpPattern = "^(?:[^@/]+@)?(?:github\.com|ssh\.github\.com):$escapedSlug(?:\.git)?/?$"
    $sshPattern = "^ssh://(?:[^/@]+@)?(?:github\.com|ssh\.github\.com)(?::\d+)?/$escapedSlug(?:\.git)?/?$"

    return $Url -match $httpsPattern -or $Url -match $scpPattern -or $Url -match $sshPattern
}

function Invoke-Git {
    param([string[]]$Arguments)

    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $rawLines = @(& $script:gitCommand -C $script:repositoryRoot @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    $stdoutLines = @(
        $rawLines |
            Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] } |
            ForEach-Object { $_.ToString() }
    )
    $stderrLines = @(
        $rawLines |
            Where-Object { $_ -is [System.Management.Automation.ErrorRecord] } |
            ForEach-Object { $_.ToString() }
    )
    $text = $stdoutLines -join [Environment]::NewLine
    $errorText = $stderrLines -join [Environment]::NewLine

    return [pscustomobject]@{
        ExitCode = $exitCode
        Lines = $stdoutLines
        Text = $text.Trim()
        ErrorText = $errorText.Trim()
    }
}

function Get-GitPath {
    param([string]$Name)

    $result = Invoke-Git -Arguments @('rev-parse', '--git-path', $Name)
    if ($result.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($result.Text)) {
        return $null
    }

    if ([System.IO.Path]::IsPathRooted($result.Text)) {
        return $result.Text
    }

    return [System.IO.Path]::GetFullPath((Join-Path $script:repositoryRoot $result.Text))
}

Write-Output 'RELEASE PREFLIGHT'

$git = Get-Command git -ErrorAction SilentlyContinue
if ($null -eq $git) {
    Write-NotReadyAndExit -Message 'Git was not found. Install or restore Git, then run the check again.'
}
$gitCommand = $git.Source

$insideRepository = Invoke-Git -Arguments @('rev-parse', '--is-inside-work-tree')
if ($insideRepository.ExitCode -ne 0 -or $insideRepository.Text -ne 'true') {
    Write-NotReadyAndExit -Message 'the project directory is not a Git working tree.'
}

$actualRootResult = Invoke-Git -Arguments @('rev-parse', '--show-toplevel')
if ($actualRootResult.ExitCode -ne 0) {
    Write-NotReadyAndExit -Message 'the Git repository root could not be determined.'
}
$actualRoot = [System.IO.Path]::GetFullPath($actualRootResult.Text)
if (-not $actualRoot.Equals($repositoryRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    Write-NotReadyAndExit -Message "the script is not inside the expected repository root: $(Protect-OutputText -Text $actualRoot)."
}

$repositoryName = Split-Path -Leaf $repositoryRoot
$blockers = New-Object System.Collections.Generic.List[string]
$notes = New-Object System.Collections.Generic.List[string]

if ($repositoryName -ne $expectedRepositoryName) {
    $blockers.Add("expected repository '$expectedRepositoryName', found '$repositoryName'.")
}

$remoteResult = Invoke-Git -Arguments @('remote', 'get-url', $remoteName)
$remoteUrl = ''
$safeRemoteUrl = '(not available)'
if ($remoteResult.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($remoteResult.Text)) {
    $blockers.Add("remote '$remoteName' is missing or has no URL.")
}
else {
    $remoteUrl = $remoteResult.Lines[0].Trim()
    $safeRemoteUrl = Get-SafeRemoteUrl -Url $remoteUrl
    if (-not (Test-ExpectedOrigin -Url $remoteUrl)) {
        $blockers.Add("remote '$remoteName' does not match the expected GitHub repository '$expectedRepositorySlug'.")
    }
}

$branchResult = Invoke-Git -Arguments @('symbolic-ref', '--quiet', '--short', 'HEAD')
$branch = '(detached HEAD)'
if ($branchResult.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($branchResult.Text)) {
    $branch = $branchResult.Text
}
else {
    $blockers.Add('HEAD is detached instead of pointing to a normal branch.')
}
if ($branch -eq $mainBranch) {
    $blockers.Add("the current branch is '$mainBranch'. Use a separate feature branch for a normal release.")
}

$headBeforeResult = Invoke-Git -Arguments @('rev-parse', '--verify', 'HEAD^{commit}')
if ($headBeforeResult.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($headBeforeResult.Text)) {
    Write-NotReadyAndExit -Message 'the current HEAD commit could not be determined.'
}
$headBefore = $headBeforeResult.Text

$statusResult = Invoke-Git -Arguments @('status', '--porcelain=v1', '--untracked-files=normal')
if ($statusResult.ExitCode -ne 0) {
    $blockers.Add('the working tree status could not be checked.')
}
$workingTreeChanges = @($statusResult.Lines | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($workingTreeChanges.Count -gt 0) {
    $blockers.Add("the working tree is not clean: $($workingTreeChanges.Count) changed path(s) found.")
}

$conflictsResult = Invoke-Git -Arguments @('diff', '--name-only', '--diff-filter=U')
$conflictingFiles = @($conflictsResult.Lines | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($conflictsResult.ExitCode -ne 0) {
    $blockers.Add('conflicting files could not be checked.')
}
elseif ($conflictingFiles.Count -gt 0) {
    $blockers.Add("there are $($conflictingFiles.Count) unresolved Git conflict(s).")
}

$mergeHeadPath = Get-GitPath -Name 'MERGE_HEAD'
$rebaseMergePath = Get-GitPath -Name 'rebase-merge'
$rebaseApplyPath = Get-GitPath -Name 'rebase-apply'
$cherryPickHeadPath = Get-GitPath -Name 'CHERRY_PICK_HEAD'
if ($null -ne $mergeHeadPath -and (Test-Path -LiteralPath $mergeHeadPath)) {
    $blockers.Add('an unfinished merge was detected.')
}
if (($null -ne $rebaseMergePath -and (Test-Path -LiteralPath $rebaseMergePath)) -or
    ($null -ne $rebaseApplyPath -and (Test-Path -LiteralPath $rebaseApplyPath))) {
    $blockers.Add('an unfinished rebase was detected.')
}
if ($null -ne $cherryPickHeadPath -and (Test-Path -LiteralPath $cherryPickHeadPath)) {
    $blockers.Add('an unfinished cherry-pick was detected.')
}

$originMain = '(not available)'
$ahead = $null
$behind = $null
$fetchSucceeded = $false

if (-not [string]::IsNullOrWhiteSpace($remoteUrl)) {
    $fetchResult = Invoke-Git -Arguments @(
        'fetch', '--quiet', $remoteName,
        'refs/heads/main:refs/remotes/origin/main'
    )
    if ($fetchResult.ExitCode -ne 0) {
        $fetchMessage = Protect-OutputText -Text $fetchResult.ErrorText
        if ([string]::IsNullOrWhiteSpace($fetchMessage)) {
            $fetchMessage = 'Git did not provide details.'
        }
        $blockers.Add("the current origin/main could not be fetched safely: $fetchMessage")
    }
    else {
        $fetchSucceeded = $true
    }
}

$headAfterResult = Invoke-Git -Arguments @('rev-parse', '--verify', 'HEAD^{commit}')
if ($headAfterResult.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($headAfterResult.Text)) {
    $blockers.Add('HEAD could not be read again after fetch.')
}
elseif ($headAfterResult.Text -ne $headBefore) {
    $blockers.Add("HEAD changed unexpectedly during the check: before $headBefore, after $($headAfterResult.Text).")
}

if ($fetchSucceeded) {
    $originMainResult = Invoke-Git -Arguments @('rev-parse', '--verify', 'refs/remotes/origin/main^{commit}')
    if ($originMainResult.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($originMainResult.Text)) {
        $blockers.Add('the current origin/main commit could not be determined after fetch.')
    }
    else {
        $originMain = $originMainResult.Text
        $countsResult = Invoke-Git -Arguments @(
            'rev-list', '--left-right', '--count', 'refs/remotes/origin/main...HEAD'
        )
        if ($countsResult.ExitCode -ne 0 -or $countsResult.Text -notmatch '^\s*(\d+)\s+(\d+)\s*$') {
            $blockers.Add('the current branch could not be compared with origin/main unambiguously.')
        }
        else {
            $behind = [int]$Matches[1]
            $ahead = [int]$Matches[2]

            if ($behind -gt 0 -and $ahead -gt 0) {
                $blockers.Add("the branch is behind origin/main by $behind commit(s) and has $ahead separate commit(s).")
            }
            elseif ($behind -gt 0) {
                $blockers.Add("the branch is behind origin/main by $behind commit(s).")
            }
            elseif ($ahead -eq 0) {
                $notes.Add('the branch matches origin/main; there are no release changes.')
            }
            else {
                $notes.Add('the branch contains the current origin/main.')
            }
        }
    }
}

Write-Output "Repository: $repositoryName"
Write-Output "Remote: $remoteName"
Write-Output "Remote URL: $safeRemoteUrl"
Write-Output "Branch: $branch"
if ($workingTreeChanges.Count -eq 0) {
    Write-Output 'Working tree: clean'
}
else {
    Write-Output 'Working tree: NOT CLEAN'
    Write-Output 'Changed files:'
    foreach ($change in $workingTreeChanges) {
        Write-Output "  $change"
    }
}
Write-Output "HEAD: $headBefore"
Write-Output "origin/main: $originMain"
if ($null -ne $ahead -and $null -ne $behind) {
    Write-Output "Ahead of main: $ahead"
    Write-Output "Behind main: $behind"
}
else {
    Write-Output 'Ahead of main: unknown'
    Write-Output 'Behind main: unknown'
}

foreach ($note in $notes) {
    Write-Output "INFO - $note"
}

if ($blockers.Count -gt 0) {
    Write-Output ''
    Write-Output 'Problems:'
    foreach ($blocker in $blockers) {
        Write-Output "  - $blocker"
    }
    Write-NotReadyAndExit -Message 'the local branch is not ready for PR/CI. Resolve each listed problem with a separate deliberate action.'
}

Write-Output ''
if ($ahead -eq 0) {
    Write-Output 'READY - the branch is current with main, but there are no new commits for a PR.'
}
else {
    Write-Output 'READY - the branch is current with main and ready for PR/CI.'
}
Write-Output 'No automatic changes were made.'
exit 0
