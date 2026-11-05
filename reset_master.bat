@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo The virtual environment was not found. Run run.bat once first.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" reset_master.py
echo.
pause
