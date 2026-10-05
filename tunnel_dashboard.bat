@echo off
setlocal
set "PATH=C:\Program Files\Amazon\AWSCLIV2;%LOCALAPPDATA%\Programs\Amazon\SessionManagerPlugin\bin;%PATH%"
echo ==========================================================
echo   Connecting to AWS EC2 Sydney (i-04f4f4f5fbd5b1813)
echo   Port Forwarding: http://localhost:8080
echo ==========================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\tunnel_aws_dashboard.ps1" %*
pause
