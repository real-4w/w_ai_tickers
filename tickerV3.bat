@echo off
REM Start tickerV3.py in the background (no console window).
REM Stays running until you close the ticker, kill the process, or shut down the PC.
cd /d "%~dp0"

where pythonw >nul 2>&1
if errorlevel 1 (
    echo pythonw was not found on PATH.
    echo Install Python and tick "Add python.exe to PATH", then try again.
    pause
    exit /b 1
)

start "" /D "%~dp0" pythonw "%~dp0tickerV3.py"
exit /b 0
