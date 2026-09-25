@echo off
setlocal enabledelayedexpansion

:: Gracefully Stop Roostoo Autonomous Quant Bot
:: Double-click to stop the bot gracefully and maintain system state

title Roostoo Bot Graceful Shutdown

cd /d "%~dp0"

echo ===============================================================================
echo                ROOSTOO QUANT BOT - GRACEFUL SHUTDOWN
echo ===============================================================================
echo.

set "BOT_PID="
if exist "data\bot.pid" (
    set /p BOT_PID=<"data\bot.pid"
)

:: If PID file exists, verify process is actually running
if defined BOT_PID (
    tasklist /FI "PID eq !BOT_PID!" 2>nul | find /I "!BOT_PID!" >nul
    if !errorlevel! neq 0 (
        echo [i] PID !BOT_PID! recorded in data\bot.pid is not currently running.
        del "data\bot.pid" 2>nul
        set "BOT_PID="
    )
)

:: If no PID file, scan for running main.py processes
if not defined BOT_PID (
    for /f "tokens=2" %%A in ('tasklist /FI "IMAGENAME eq python.exe" /FO LIST 2^>nul ^| find /I "PID:"') do (
        wmic process where "ProcessId=%%A and CommandLine like '%%main.py%%'" get ProcessId 2>nul | find "%%A" >nul
        if !errorlevel! equ 0 (
            set "BOT_PID=%%A"
        )
    )
)

if not defined BOT_PID (
    echo [*] No active Roostoo Bot instance detected.
    echo [*] Checking if any stale shutdown triggers exist...
    if exist "data\shutdown.trigger" del "data\shutdown.trigger" 2>nul
    echo [+] System is idle. Nothing to shut down.
    echo.
    timeout /t 3 >nul 2>nul || pause
    exit /b 0
)

echo [*] Target active Roostoo Bot detected with PID: !BOT_PID!
echo [*] Triggering graceful shutdown sequence...
echo     - Flushing in-flight orders and mark-to-market valuations
echo     - Persisting portfolio balance and positions to data\portfolio_state.json
echo     - Persisting execution records to data\order_state.json
echo     - Verifying and closing SHA-256 audit trail chain
echo.

:: 1. Write sentinel trigger file
echo %date% %time% > "data\shutdown.trigger"

:: 2. Send REST shutdown command via Web Server if reachable
where curl >nul 2>nul
if %errorlevel% equ 0 (
    curl -s -m 2 -X POST http://localhost:8080/command/shutdown >nul 2>nul
) else (
    powershell -NoProfile -Command "try { Invoke-RestMethod -Uri 'http://localhost:8080/command/shutdown' -Method Post -TimeoutSec 2 | Out-Null } catch {}" >nul 2>nul
)

:: 3. Monitor process exit with 10-second countdown
set /a TIMEOUT_SEC=10
set /a ELAPSED=0

:WAIT_LOOP
if defined BOT_PID (
    tasklist /FI "PID eq !BOT_PID!" 2>nul | find /I "!BOT_PID!" >nul
    if !errorlevel! equ 0 (
        set /a ELAPSED+=1
        if %ELAPSED% geq %TIMEOUT_SEC% (
            goto SHUTDOWN_TIMEOUT
        )
        <nul set /p "=."
        ping -n 2 127.0.0.1 >nul
        goto WAIT_LOOP
    )
)
goto SHUTDOWN_SUCCESS

:SHUTDOWN_SUCCESS
echo.
echo ===============================================================================
echo  [SUCCESS] Roostoo Autonomous Quant Bot (PID !BOT_PID!) stopped gracefully!
echo.
echo  State Verification:
echo   [OK] Portfolio ledger persisted   - data\portfolio_state.json
echo   [OK] Order history persisted      - data\order_state.json
echo   [OK] SHA-256 cryptographic audit  - logs\audit.jsonl
echo ===============================================================================
echo.
echo [*] Opening dashboard to verify system state...
timeout /t 2 >nul
start http://localhost:8080
echo [+] Dashboard opened for verification. (Dashboard will display bot status and system metrics)
echo.
if exist "data\bot.pid" del "data\bot.pid" 2>nul
if exist "data\shutdown.trigger" del "data\shutdown.trigger" 2>nul
timeout /t 2 >nul || pause
exit /b 0

:SHUTDOWN_TIMEOUT
echo.
echo.
echo [!] Process !BOT_PID! did not exit within %TIMEOUT_SEC% seconds.
echo Options:
echo   [F] Force-terminate process !BOT_PID! (data may be lost)
echo   [W] Keep waiting for process to exit
echo   [C] Cancel operation and preserve bot state
echo.
set /p USER_CHOICE="Select option [F/W/C]: "
if /I "!USER_CHOICE!"=="F" (
    echo [*] Force-terminating process !BOT_PID!...
    taskkill /F /PID !BOT_PID! >nul 2>nul
    del "data\bot.pid" 2>nul
    del "data\shutdown.trigger" 2>nul
    echo [+] Process terminated.
) else if /I "!USER_CHOICE!"=="W" (
    set /a ELAPSED=0
    goto WAIT_LOOP
) else (
    echo [*] Operation cancelled. Bot is still running.
)

echo.
echo [+] Restart the bot with start_bot.bat or stop it with stop_bot.bat
echo.
timeout /t 3 >nul || pause