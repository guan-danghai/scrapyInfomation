# Portable Node.js LTS (win-x64 zip) -> tools/nodejs (no system PATH change)
# Run from repo root: powershell -NoProfile -ExecutionPolicy Bypass -File tools\install-node-portable.ps1

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path $PSScriptRoot -Parent
$DestDir = Join-Path $ProjectRoot 'tools\nodejs'
$StageDir = Join-Path $ProjectRoot 'tools\_node_stage'
$IndexUrl = 'https://nodejs.org/dist/index.json'

Write-Host "Project root: $ProjectRoot"
Write-Host 'Fetching Node LTS version from nodejs.org...'

$all = Invoke-RestMethod -Uri $IndexUrl -UseBasicParsing
$lts = $all | Where-Object { $_.lts -and $_.lts -ne $false } | Select-Object -First 1
if (-not $lts) { throw 'Could not resolve LTS from index.json' }

$ver = $lts.version.TrimStart('v')
$file = "node-v$ver-win-x64.zip"
$zipUrl = "https://nodejs.org/dist/v$ver/$file"
$zipPath = Join-Path $ProjectRoot "tools\$file"

Write-Host "Will install: v$ver"
Write-Host "Download: $zipUrl"

if (Test-Path $DestDir) {
    Write-Host "Removing old $DestDir"
    Remove-Item -Recurse -Force $DestDir
}
if (Test-Path $StageDir) { Remove-Item -Recurse -Force $StageDir }
New-Item -ItemType Directory -Path (Split-Path $zipPath) -Force | Out-Null

Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -UseBasicParsing
Write-Host 'Extracting...'
Expand-Archive -Path $zipPath -DestinationPath $StageDir -Force

$inner = Get-ChildItem -Path $StageDir -Directory | Select-Object -First 1
if (-not $inner) { throw 'No folder inside zip' }

New-Item -ItemType Directory -Path (Split-Path $DestDir -Parent) -Force | Out-Null
Move-Item -Path $inner.FullName -Destination $DestDir -Force

Remove-Item -Recurse -Force $StageDir -ErrorAction SilentlyContinue
Remove-Item -Force $zipPath -ErrorAction SilentlyContinue

$nodeExe = Join-Path $DestDir 'node.exe'
if (-not (Test-Path $nodeExe)) { throw "Missing $nodeExe" }

& $nodeExe -v
Write-Host ''
Write-Host "Done. Node is only under: $DestDir"
Write-Host 'Start web UI: web-view-node\run-with-local-node.cmd'
