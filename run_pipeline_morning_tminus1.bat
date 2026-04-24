@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist "logs" mkdir "logs"

set LOG=logs\pipeline_schedule.log
echo.>> "%LOG%"
echo ===== %date% %time% 开始 run_pipeline --t-minus-1 =====>> "%LOG%"

python run_pipeline.py --t-minus-1 >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%
echo ===== %date% %time% 结束 exit=%RC% =====>> "%LOG%"
exit /b %RC%
