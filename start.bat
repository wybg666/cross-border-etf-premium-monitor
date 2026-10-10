@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set "PYTHON_CMD=.venv\Scripts\python.exe"
    goto check_dependencies
)
py -3 --version >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=py -3"
    goto check_dependencies
)
python --version >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=python"
    goto check_dependencies
)
echo Python was not found. Install Python 3.10 or newer first.
pause
exit /b 1

:check_dependencies
%PYTHON_CMD% -c "import sys; assert sys.version_info >= (3,10); from zoneinfo import ZoneInfo; ZoneInfo('Asia/Shanghai')" >nul 2>&1
if errorlevel 1 (
    echo Python 3.10+ and timezone data are required.
    echo To install dependencies, run: %PYTHON_CMD% -m pip install -r requirements.txt
    echo No dependencies have been installed automatically.
    pause
    exit /b 1
)
%PYTHON_CMD% run.py --open-browser
if errorlevel 1 (
    pause
    exit /b 1
)
endlocal
