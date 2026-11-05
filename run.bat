@echo off
setlocal
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto :nopython

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    %PY% -m venv .venv
    if errorlevel 1 goto :fail
)

echo Installing dependencies...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto :fail

".venv\Scripts\python.exe" init_env.py
if errorlevel 1 goto :fail

set "OPEN_BROWSER=1"
".venv\Scripts\python.exe" wsgi.py
if errorlevel 1 goto :fail
goto :eof

:nopython
echo Python 3.12 or newer was not found.
echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
pause
exit /b 1

:fail
echo.
echo Something went wrong. Read the messages above.
pause
exit /b 1
