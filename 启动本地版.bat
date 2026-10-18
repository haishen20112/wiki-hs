@echo off
REM WIKI.HS local editor launcher
REM Pure ASCII on purpose: cmd.exe codepage varies by Windows language.
cd /d "%~dp0"

python launcher.py --local
if errorlevel 1 (
    echo.
    echo ------------------------------------------------------------
    echo  Startup failed. See messages above.
    echo  If Python is missing, install it from:
    echo      https://www.python.org/downloads/
    echo  Remember to check "Add Python to PATH" during setup.
    echo ------------------------------------------------------------
)
echo.
pause
