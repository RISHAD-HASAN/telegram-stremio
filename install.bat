@echo off
setlocal enabledelayedexpansion
title Telegram Stremio - Dependencies Installer
cd /d "%~dp0"

echo ========================================================
echo  Installing dependencies for Telegram Stremio
echo ========================================================
echo.

set "PY_CMD="

REM 1. Check virtual environment python
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" --version >nul 2>nul
    if !errorlevel! equ 0 (
        set "PY_CMD=%~dp0.venv\Scripts\python.exe"
        goto :RunInstall
    )
)

REM 2. Check standard python command
python --version >nul 2>nul
if !errorlevel! equ 0 (
    set "PY_CMD=python"
    goto :RunInstall
)

REM 3. Check py launcher
py -3 --version >nul 2>nul
if !errorlevel! equ 0 (
    set "PY_CMD=py -3"
    goto :RunInstall
)

REM 4. Check explicit Windows paths
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
    set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    goto :RunInstall
)
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    goto :RunInstall
)
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
    set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    goto :RunInstall
)
if exist "C:\Python313\python.exe" (
    set "PY_CMD=C:\Python313\python.exe"
    goto :RunInstall
)
if exist "C:\Python312\python.exe" (
    set "PY_CMD=C:\Python312\python.exe"
    goto :RunInstall
)
if exist "C:\Program Files\Python313\python.exe" (
    set "PY_CMD=C:\Program Files\Python313\python.exe"
    goto :RunInstall
)
if exist "C:\Program Files\Python312\python.exe" (
    set "PY_CMD=C:\Program Files\Python312\python.exe"
    goto :RunInstall
)

echo [ERROR] Python was not found on your system!
echo Please install Python 3.11, 3.12 or 3.13 from https://www.python.org/downloads/
echo Make sure to check "Add Python to PATH" during installation.
echo.
pause
exit /b 1

:RunInstall
echo [INFO] Found Python: %PY_CMD%
%PY_CMD% --version
echo.

echo [INFO] Upgrading pip...
%PY_CMD% -m pip install --upgrade pip
echo.

echo [INFO] Installing required dependencies...
%PY_CMD% -m pip install -r "%~dp0requirements.txt"

if !errorlevel! neq 0 (
    echo.
    echo [ERROR] Dependency installation failed with code !errorlevel!.
    echo.
    pause
    exit /b !errorlevel!
)

echo.
echo ========================================================
echo  Dependencies installed successfully!
echo.
echo  Next steps:
echo   1. Ensure MongoDB service is running (net start MongoDB)
echo   2. Double-click start.bat to run the server
echo ========================================================
echo.
pause
