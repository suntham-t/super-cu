# One-time setup for the dsh-super-cu bundle (Windows / PowerShell 5.1+).
#
#   ./setup.ps1                 # auto-detect python, write absolute paths
#   ./setup.ps1 -Python "C:\Python313\python.exe"
#   ./setup.ps1 -WhatIf         # only print what would change
#
# It rewrites cordis.patch.yml tokens with this machine's real paths.
# Then install the bundle in DSH: Plugin Manager -> install from folder -> this directory.

[CmdletBinding()]
param(
  [string]$Python = "",
  [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$patch = Join-Path $root 'cordis.patch.yml'
$server = Join-Path $root 'src\server.py'

if (-not (Test-Path $server)) { throw "src\server.py not found under $root" }

if (-not $Python) {
  $cmd = Get-Command python -ErrorAction SilentlyContinue
  if ($cmd) { $Python = $cmd.Source } else { throw "python not found on PATH; pass -Python <path>" }
}
if (-not (Test-Path $Python)) { throw "python not found: $Python" }

Write-Host "python : $Python"
Write-Host "server : $server"
& $Python -c "import sys; print('python ok', sys.version.split()[0])"

$missing = @()
try { & $Python -c "import PIL" 2>$null } catch { $missing += 'Pillow' }
if ($LASTEXITCODE -ne 0) { $missing += 'Pillow' }
& $Python -c "import uiautomation" 2>$null
if ($LASTEXITCODE -ne 0) { $missing += 'uiautomation' }
if ($missing.Count -gt 0) {
  Write-Host ""
  Write-Host "Installing missing python packages: $($missing -join ', ')"
  if (-not $WhatIf) { & $Python -m pip install --quiet $missing }
}

$text = Get-Content $patch -Raw -Encoding UTF8
$new = $text.Replace('__SUPER_CU_PYTHON__', $Python.Replace('\', '\\')).Replace('__SUPER_CU_SERVER__', $server.Replace('\', '\\'))
if ($WhatIf) {
  Write-Host "--- would write ---"
  Write-Host $new
  return
}
if ($new -ne $text) {
  Set-Content -Path $patch -Value $new -Encoding UTF8 -NoNewline
  Write-Host "patched: $patch"
} else {
  Write-Host "patch already up to date"
}

Write-Host ""
Write-Host "Next: DSH -> 插件 / Plugins -> 安装本地插件 (install from folder) -> $root"
Write-Host "Then start a new session; the tools mcp__supercu__cu_show / mcp__supercu__cu_do appear."
