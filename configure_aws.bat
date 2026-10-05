@echo off
setlocal
set "PATH=C:\Program Files\Amazon\AWSCLIV2;%PATH%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\configure_aws.ps1"
pause
