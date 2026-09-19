@echo off
title Telegram-Stremio Ultimate Server
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
chcp 65001 >nul

echo ========================================================
echo  Starting Telegram-Stremio Ultimate Server
echo ========================================================
echo.

if not exist "%~dp0config.env" (
    echo [ERROR] config.env not found!
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

set "PY_CMD="

if exist "%~dp0.venv\Scripts\python.exe" (
    set "PY_CMD=%~dp0.venv\Scripts\python.exe"
    goto :FoundPython
)

where python >nul 2>nul
if %errorlevel% equ 0 (
    set "PY_CMD=python"
    goto :FoundPython
)

where py >nul 2>nul
if %errorlevel% equ 0 (
    set "PY_CMD=py -3"
    goto :FoundPython
)

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
echo Please install Python 3.10+ and make sure "Add python.exe to PATH" is checked.
echo.
pause
exit /b 1

:FoundPython
echo [INFO] Running server with: %PY_CMD%
echo.
"%PY_CMD%" -m Backend

if %errorlevel% neq 0 (
    echo.
    echo ========================================================
    echo [ERROR] Server exited with error code %errorlevel%.
    echo If MongoDB failed to connect, make sure your MongoDB service is running.
    echo Check log.txt or the error details above.
    echo ========================================================
    pause
)
