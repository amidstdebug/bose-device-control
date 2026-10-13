<#
.SYNOPSIS
  Starts a new version SERIES. You don't need this for normal releases: every green push to main that changes the
  app is released automatically as <series>.<build>, e.g. 1.4.57.

  Use it when you want a new minor/major number (1.4 -> 1.5, 1.x -> 2.0): it sets the series in bose_app.py, commits
  and (with -Push) pushes, and the normal pipeline releases it as 1.5.<build>.

.EXAMPLE
  .\scripts\release.ps1 -Version 1.5.0          # sets the series locally (commit only)
  .\scripts\release.ps1 -Version 1.5.0 -Push    # and pushes
#>
param(
    [Parameter(Mandatory)][ValidatePattern('^\d+\.\d+\.0$')][string]$Version,
    [switch]$Push,
    [switch]$SkipTests
)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)

function Invoke-Git {
    # native commands don't honour $ErrorActionPreference, so check every exit code
    & git.exe @args
    if ($LASTEXITCODE -ne 0) { throw "git $($args -join ' ') failed (exit $LASTEXITCODE)" }
}

if (git status --porcelain) { throw "Working tree is not clean. Commit or stash first." }
$branch = (git rev-parse --abbrev-ref HEAD).Trim()
if ($branch -ne 'main') { throw "Run this from main (you are on $branch)." }
Invoke-Git fetch origin
if (git rev-list HEAD..origin/main) { throw "origin/main has commits you don't have. Pull first." }

$py = if (Test-Path .\.venv\Scripts\python.exe) { '.\.venv\Scripts\python.exe' } else { 'python' }
if (-not $SkipTests) {
    Write-Host "Running tests (a few minutes)..."
    & $py -m pytest tests -q
    if ($LASTEXITCODE -ne 0) { throw "Tests failed; not continuing." }
}

$file = Join-Path (Get-Location) 'bose_app.py'
$text = [IO.File]::ReadAllText($file)
if ($text -notmatch '(?m)^APP_VERSION = "([^"]+)"') { throw "Could not find APP_VERSION in bose_app.py" }
$current = $Matches[1]
if ($current -eq $Version) { throw "The series is already $Version." }
$new = [regex]::Replace($text, '(?m)^APP_VERSION = "[^"]+"', "APP_VERSION = `"$Version`"")
# UTF-8 *without* a BOM (Windows PowerShell 5.1's Set-Content -Encoding utf8 would add one)
[IO.File]::WriteAllText($file, $new, (New-Object Text.UTF8Encoding $false))
Invoke-Git add bose_app.py
Invoke-Git commit -m "Start version series $Version"
Write-Host "Series $current -> $Version committed"

$remote = (git remote get-url origin) -replace '^(git@github\.com:|https://github\.com/)', '' -replace '\.git$', ''
if ($Push) {
    Invoke-Git push origin main
    Write-Host "Pushed. CI runs, then the release is published automatically."
    Write-Host "Watch: https://github.com/$remote/actions   Result: https://github.com/$remote/releases"
} else {
    Write-Host "Committed locally only. To publish:  git push origin main"
}
