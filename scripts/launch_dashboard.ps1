<#
.SYNOPSIS
    Launches AutoSL Web Telemetry Dashboard on http://localhost:8080 via secure SSH tunnel to AWS EC2
#>

Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host "  AUTOSL DASHBOARD LAUNCHER (AWS EC2 -> Localhost:8080)" -ForegroundColor Cyan
Write-Host "======================================================================" -ForegroundColor Cyan

# Check if port 8080 is already active
$portActive = Get-NetTCPConnection -LocalPort 8080 -ErrorAction SilentlyContinue

if (-not $portActive) {
    Write-Host "[+] Establishing secure SSH tunnel to AWS EC2 (port 8080)..." -ForegroundColor Green
    $tunnelProcess = Start-Process ssh -ArgumentList "-N -L 8080:127.0.0.1:8080 -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 aws-btceth" -PassThru -WindowStyle Hidden
    Start-Sleep -Seconds 2
} else {
    Write-Host "[*] Port 8080 tunnel is already active." -ForegroundColor Yellow
}

Write-Host "[+] Launching Dashboard at http://localhost:8080 ..." -ForegroundColor Green
Start-Process "http://localhost:8080"

Write-Host "`n[SUCCESS] Dashboard is live at http://localhost:8080" -ForegroundColor Cyan
Write-Host "Press Ctrl+C or close this window when done.`n"
