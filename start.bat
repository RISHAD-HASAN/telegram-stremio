@echo off
setlocal enabledelayedexpansion
title Telegram Stremio Server
cd /d "%~dp0"

echo ========================================================
echo  Starting Telegram Stremio server
echo ========================================================
echo.

if not exist "%~dp0config.env" (
    echo [ERROR] config.env not found in: "%~dp0"
    echo.
    if exist "%~dp0sample_config.env" (
        echo Creating config.env from sample_config.env...
        copy "%~dp0sample_config.env" "%~dp0config.env" >nul
        echo Created config.env. Please open config.env in Notepad and fill in your credentials!
    ) else (
        echo Please ensure you have extracted all files from the zip.
    )
    echo.
    pause
    exit /b 1
)

REM 1. Check if virtual environment python exists
if exist "%~dp0.venv\Scripts\python.exe" (
    echo [INFO] Using virtual environment Python...
    "%~dp0.venv\Scripts\python.exe" -m Backend
    goto :ServerEnd
)

REM 2. Check standard python command
python --version >nul 2>nul
if !errorlevel! equ 0 (
    echo [INFO] Running server with system Python...
    python -m Backend
    goto :ServerEnd
)

REM 3. Check py launcher
py -3 --version >nul 2>nul
if !errorlevel! equ 0 (
    echo [INFO] Running server with py -3...
    py -3 -m Backend
    goto :ServerEnd
)

REM 4. Check explicit Windows Python installation paths
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
    echo [INFO] Found Python 3.13 in AppData...
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" -m Backend
    goto :ServerEnd
)
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    echo [INFO] Found Python 3.12 in AppData...
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" -m Backend
    goto :ServerEnd
)
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
    echo [INFO] Found Python 3.11 in AppData...
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" -m Backend
    goto :ServerEnd
)
if exist "C:\Python313\python.exe" (
    echo [INFO] Found Python 3.13 in C:\Python313...
    "C:\Python313\python.exe" -m Backend
    goto :ServerEnd
)
if exist "C:\Python312\python.exe" (
    echo [INFO] Found Python 3.12 in C:\Python312...
    "C:\Python312\python.exe" -m Backend
    goto :ServerEnd
)
if exist "C:\Program Files\Python313\python.exe" (
    echo [INFO] Found Python 3.13 in Program Files...
    "C:\Program Files\Python313\python.exe" -m Backend
    goto :ServerEnd
)
if exist "C:\Program Files\Python312\python.exe" (
    echo [INFO] Found Python 3.12 in Program Files...
    "C:\Program Files\Python312\python.exe" -m Backend
    goto :ServerEnd
)

echo [ERROR] Python was not found on your system!
echo Please install Python 3.11, 3.12 or 3.13 from https://www.python.org/downloads/
echo Make sure to check "Add Python to PATH" during installation.
echo.

:ServerEnd
echo.
echo ========================================================
echo Server process has stopped.
echo ========================================================
echo.
pause
