@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 采招-T1定时循环-勿关
python schedule_tminus1_loop.py --hour 9 --minute 46
pause
