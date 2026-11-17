@echo off
chcp 65001 >nul 2>&1
title Panel Monitor - Server Dashboard
cd /d "%~dp0"

set "PY=C:\Users\Lenovo\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not exist "%PY%" set "PY=python"

echo Starting Panel Monitor, please keep this window open...
echo.
"%PY%" panel_monitor.py

echo.
echo [Panel Monitor stopped] Press any key to close.
pause >nul
