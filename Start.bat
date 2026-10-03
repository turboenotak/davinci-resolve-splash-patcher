@echo off
rem Windows launcher: finds Python and hands over to start.py (checks Pillow, starts the patcher)
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python 3.10 or newer is required.
    echo Download it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
    pause
    exit /b 1
)

python start.py %*
