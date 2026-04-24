# UTF-8 with BOM - Windows PowerShell 5.x reads string literals correctly.
$ErrorActionPreference = "Stop"
$Here = $PSScriptRoot
$NodeBin = Join-Path $Here "..\tools\nodejs"
$NodeExe = Join-Path $NodeBin "node.exe"

if (-not (Test-Path -LiteralPath $NodeExe)) {
    Write-Host "Portable Node not found. Run from repo root:" -ForegroundColor Yellow
    Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File tools\install-node-portable.ps1" -ForegroundColor Cyan
    exit 1
}

$NodeBin = (Resolve-Path -LiteralPath $NodeBin).Path
$env:Path = $NodeBin + ";" + $env:Path
Set-Location -LiteralPath $Here

$nm = Join-Path $Here "node_modules"
if (-not (Test-Path -LiteralPath $nm)) {
    Write-Host "First run: npm install..." -ForegroundColor Cyan
    & (Join-Path $NodeBin "npm.cmd") install
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$nv = & $NodeExe -v
Write-Host "Node $nv - starting server..." -ForegroundColor Green
& (Join-Path $NodeBin "npm.cmd") start
