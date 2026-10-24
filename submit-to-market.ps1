# Submit dsh-super-cu to the awesome-dsh-plugin index (the list the DSH plugin
# market reads from), without cloning the 200 MB index repository.
#
#   ./submit-to-market.ps1 -DryRun     # show what would be uploaded / created
#   ./submit-to-market.ps1             # fork + upload entry + open the PR
#
# Requires: gh (authenticated), and HTTPS_PROXY if github.com is unreachable
# directly (e.g. 127.0.0.1:7890).
#
# NOTE: the index CI requires the submitted repo to be >= 1 day old. Running
# this earlier than that gets the PR closed automatically; it is safe to resubmit.

[CmdletBinding()]
param(
  [switch]$DryRun,
  [string]$Proxy = "http://127.0.0.1:7890",
  [string]$Upstream = "awesome-dsh-plugin/awesome-dsh-plugin"
)

$ErrorActionPreference = 'Stop'
if ($Proxy) { $env:HTTPS_PROXY = $Proxy; $env:HTTP_PROXY = $Proxy }

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$entry = Get-ChildItem (Join-Path $root '.market') -Filter '*.yml' | Select-Object -First 1
if (-not $entry) { throw ".market/*.yml entry file not found" }
$target = "data/plugins/$($entry.Name)"

$me = (gh api user --jq .login).Trim()
$owner = $me
$repoName = $Upstream.Split('/')[1]
$fork = "$owner/$repoName"
Write-Host "owner : $owner"
Write-Host "entry : $($entry.Name) -> $Upstream/$target"

if ($DryRun) {
  Write-Host "--- entry content ---"
  Get-Content $entry.FullName -Raw -Encoding UTF8
  Write-Host "--- would do ---"
  Write-Host "1) gh api -X POST repos/$Upstream/forks  (if $fork missing)"
  Write-Host "2) upload $target to $fork (contents API)"
  Write-Host "3) gh pr create -R $Upstream --head ${owner}:main"
  return
}

# 1) fork (no-op if it already exists)
$forkExists = $true
try { gh api "repos/$fork" --jq .name | Out-Null } catch { $forkExists = $false }
if (-not $forkExists) {
  Write-Host "forking $Upstream ..."
  gh api -X POST "repos/$Upstream/forks" -f default_branch_only=false --jq .full_name
  Start-Sleep -Seconds 5
}

# 2) upload the entry file to the fork (create or update, one commit)
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes((Get-Content $entry.FullName -Raw -Encoding UTF8)))
$sha = $null
try { $sha = (gh api "repos/$fork/contents/$target" --jq .sha 2>$null).Trim() } catch { $sha = $null }
$args = @('api', '-X', 'PUT', "repos/$fork/contents/$target",
          '-f', "message=Add $($entry.BaseName) to the index",
          '-f', "content=$b64",
          '-f', 'branch=main',
          '--jq', '.commit.sha')
if ($sha) { $args += @('-f', "sha=$sha") }
$commit = & gh @args
Write-Host "uploaded, commit=$commit"

# 3) open the PR
$body = @"
Adds `suntham-t/super-cu`, a DSH bundle for Windows desktop control.

- `package.json` declares `dsh.bundle.patch: ./cordis.patch.yml` next to the manifest
- connects the official `@deepseek-ai/dsh-mcp-client` row over stdio to a persistent
  Python UI-Automation server (`src/server.py`)
- exposes two agent tools: `cu_show` (compact semantic snapshot, stable element ids)
  and `cu_do` (batched click / set / key / hold / drag / launch / wait / assert)
- repo has the `dsh-plugin` topic

One YAML entry, as the contributing guide asks.
"@
gh pr create -R $Upstream --head "${owner}:main" `
  --title "Add suntham-t/super-cu (Windows computer use over UI Automation)" `
  --body $body
