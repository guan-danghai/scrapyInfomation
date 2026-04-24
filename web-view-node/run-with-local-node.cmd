@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

set "NODE_HOME=%~dp0..\tools\nodejs"

if not exist "%NODE_HOME%\node.exe" (
  echo Portable Node not found at:
  echo   %NODE_HOME%
  echo Run from repo root: powershell -NoProfile -ExecutionPolicy Bypass -File tools\install-node-portable.ps1
  exit /b 1
)

set "PATH=%NODE_HOME%;%PATH%"

if not exist "%~dp0node_modules\" (
  echo First run: npm install...
  call "%NODE_HOME%\npm.cmd" install
  if errorlevel 1 exit /b 1
)

echo Starting server...
call "%NODE_HOME%\npm.cmd" start
exit /b %ERRORLEVEL%
