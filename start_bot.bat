@echo off
setlocal enabledelayedexpansion

:: Start Roostoo Autonomous Quant Bot with Dashboard Launch
:: Double-click to start the bot and automatically open the web dashboard

title Roostoo Autonomous Quant Bot [STARTING]

cd /d "%~dp0"

echo ===============================================================================
echo                ROOSTOO AUTONOMOUS QUANT TRADING BOT
echo ===============================================================================

:: 1. Detect Python Executable
set "PYTHON_EXE="
if exist ".venv\Scripts\python.exe" (
    set "PYTHON_EXE=.venv\Scripts\python.exe"
    echo [*] Using virtual environment Python: .venv\Scripts\python.exe
) else if exist "venv\Scripts\python.exe" (
    set "PYTHON_EXE=venv\Scripts\python.exe"
    echo [*] Using virtual environment Python: venv\Scripts\python.exe
) else (
    where python >nul 2>nul
    if %errorlevel% equ 0 (
        set "PYTHON_EXE=python"
        echo [*] Using system Python from PATH
    ) else (
        echo [!] ERROR: Python executable was not found in PATH or virtual environments.
        echo Please install Python 3.10+ and add it to your system PATH.
        echo.
        pause
        exit /b 1
    )
)

:: 2. Check if Bot is Already Running
if exist "data\bot.pid" (
    set /p RUNNING_PID=<"data\bot.pid"
    if defined RUNNING_PID (
        tasklist /FI "PID eq !RUNNING_PID!" 2>nul | find /I "!RUNNING_PID!" >nul
        if !errorlevel! equ 0 (
            echo.
            echo [!] WARNING: Bot is ALREADY RUNNING with PID !RUNNING_PID!.
            echo.
            echo     Web Dashboard:  http://localhost:8080
            echo     To Stop Bot:    Double-click stop_bot.bat
            echo.
            echo Press 'O' to open the Web Dashboard in your browser, or any key to exit...
            choice /C OC /N /T 5 /D C >nul
            if !errorlevel! equ 1 (
                start http://localhost:8080
            )
            exit /b 0
        ) else (
            :: Stale PID file
            del "data\bot.pid" 2>nul
        )
    )
)

:: 3. Prepare Working Directories & Clean Sentinel Triggers
if not exist "data" mkdir "data"
if not exist "logs" mkdir "logs"
if exist "data\shutdown.trigger" del "data\shutdown.trigger" 2>nul

:: 4. Start Autonomous Bot in new console window
echo [*] Initializing autonomous bot engine...
echo [*] Telemetry Dashboard will be hosted at: http://localhost:8080
echo [*] Opening dashboard in browser...
echo ===============================================================================
echo.

:: Launch bot in a separate console window
start "Roostoo Autonomous Quant Bot" /min %PYTHON_EXE% main.py --dry-run

:: Wait briefly for bot to start, then open dashboard
timeout /t 3 /nobreak >nul 2>nul
start http://localhost:8080

:: Title and waiting for bot termination
title Roostoo Autonomous Quant Bot [RUNNING]

echo ===============================================================================
echo.
echo Bot process launched. Web dashboard opened at http://localhost:8080
echo To stop the bot gracefully, double-click stop_bot.bat
echo.
echo Press Ctrl+C in this window or close this console to exit.
echo.

:: Wait for bot to exit by polling for python.exe with main.py
:WAIT_FOR_EXIT
set "BOT_FOUND="
for /f "tokens=2" %%A in ('tasklist /FI "IMAGENAME eq python.exe" /FO LIST 2^>nul ^| find /I "PID:"') do (
    wmic process where "ProcessId=%%A and CommandLine like '%%main.py%%'" get ProcessId 2>nul | find "%%A" >nul
    if !errorlevel! equ 0 set "BOT_FOUND=1"
)
if defined BOT_FOUND (
    ping -n 2 127.0.0.1 >nul
    goto WAIT_FOR_EXIT
)

:BOT_EXITED
echo.
echo ===============================================================================
echo [+] Bot terminated.
echo.

title Roostoo Autonomous Quant Bot [STOPPED]
echo ===============================================================================

echo.
pause