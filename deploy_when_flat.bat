@echo off
setlocal
set "PATH=C:\Program Files\Amazon\AWSCLIV2;%LOCALAPPDATA%\Programs\Amazon\SessionManagerPlugin\bin;%PATH%"
echo ==========================================================
echo   AutoSL: Switch to Latest Copy When No Trades Active
echo ==========================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\deploy_when_flat.ps1" %*
pause
