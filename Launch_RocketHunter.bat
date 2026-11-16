@echo off
title Rocket Hunter v4.30 Desktop Launcher
cd /d "%~dp0"

echo ======================================================================
echo          ROCKET HUNTER v4.30 - DESKTOP CONTROL CENTER
echo ======================================================================
echo.

if exist "RocketHunter.exe" (
    echo Launching RocketHunter.exe...
    start "" "RocketHunter.exe"
    if %ERRORLEVEL% equ 0 exit /b 0
)

echo RocketHunter.exe could not be started directly (or is blocked by AppLocker/WDAC).
echo Falling back to Python launcher...
python scripts\desktop_launcher.py
if %ERRORLEVEL% neq 0 (
    echo.
    echo [!] Could not launch Rocket Hunter. Please ensure Python is installed or unblock RocketHunter.exe.
    pause
)
