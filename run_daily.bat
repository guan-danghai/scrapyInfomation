@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [%date% %time%] 每日跑批开始
python run_pipeline.py
echo [%date% %time%] 每日跑批结束
pause
