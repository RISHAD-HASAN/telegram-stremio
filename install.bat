@echo off
title Telegram-Stremio Ultimate - Installer
cd /d "%~dp0"

echo ========================================================
echo  Installing Dependencies for Telegram-Stremio Ultimate
echo ========================================================
echo.

set "PY_CMD="

REM 1. Check if virtual environment python exists
if exist "%~dp0.venv\Scripts\python.exe" (
    set "PY_CMD=%~dp0.venv\Scripts\python.exe"
    goto :FoundPython
)

REM 2. Check system python
where python >nul 2>nul
if %errorlevel% equ 0 (
    set "PY_CMD=python"
    goto :FoundPython
)

REM 3. Check py launcher
where py >nul 2>nul
if %errorlevel% equ 0 (
    set "PY_CMD=py -3"
    goto :FoundPython
)

REM 4. Check common Windows installation paths
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
    set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    goto :FoundPython
)
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    goto :FoundPython
)
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
    set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    goto :FoundPython
)
if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" (
    set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    goto :FoundPython
)
if exist "C:\Python313\python.exe" (
    set "PY_CMD=C:\Python313\python.exe"
    goto :FoundPython
)
if exist "C:\Python312\python.exe" (
    set "PY_CMD=C:\Python312\python.exe"
    goto :FoundPython
)
if exist "C:\Python311\python.exe" (
    set "PY_CMD=C:\Python311\python.exe"
    goto :FoundPython
)
if exist "C:\Program Files\Python313\python.exe" (
    set "PY_CMD=C:\Program Files\Python313\python.exe"
    goto :FoundPython
)
if exist "C:\Program Files\Python312\python.exe" (
    set "PY_CMD=C:\Program Files\Python312\python.exe"
    goto :FoundPython
)
if exist "C:\Program Files\Python311\python.exe" (
    set "PY_CMD=C:\Program Files\Python311\python.exe"
    goto :FoundPython
)

echo [ERROR] Python was not found on your system!
echo.
echo Please install Python 3.10 or newer from:
echo https://www.python.org/downloads/
echo.
echo NOTE: Make sure to check "Add python.exe to PATH" during installation.
echo.
pause
exit /b 1

:FoundPython
echo [INFO] Found Python: %PY_CMD%
"%PY_CMD%" --version
echo.

echo [INFO] Upgrading pip...
"%PY_CMD%" -m pip install --upgrade pip
echo.

echo [INFO] Installing required dependencies...
"%PY_CMD%" -m pip install -r "%~dp0requirements.txt"

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Package installation failed with error code %errorlevel%.
    echo Please review the error messages above.
    echo.
    pause
    exit /b %errorlevel%
)

echo.
echo ========================================================
echo  Dependencies installed successfully!
echo.
echo  Next steps:
echo   1. Copy sample_config.env to config.env and fill in:
echo      API_ID, API_HASH, BOT_TOKEN, and DATABASE
echo   2. Double-click start.bat to run the server
echo ========================================================
echo.
pause
