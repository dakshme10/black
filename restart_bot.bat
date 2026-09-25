@echo off
title Roostoo Bot Restart

cd /d "%~dp0"

echo ===============================================================================
echo                ROOSTOO QUANT BOT - RESTART SEQUENCE
echo ===============================================================================
echo.

call "%~dp0stop_bot.bat"

echo.
echo [*] Waiting 2 seconds before restart...
ping -n 3 127.0.0.1 >nul

echo [*] Launching new bot instance...
start "Roostoo Autonomous Quant Bot" "%~dp0start_bot.bat" %*
echo [+] Bot restart command launched.
timeout /t 2 >nul 2>nul
