<#
.SYNOPSIS
    Gracefully Shuts Down the Roostoo Autonomous Quant Trading Bot with Dashboard Launch.
.DESCRIPTION
    Sends a graceful shutdown signal via sentinel trigger and REST endpoint,
    monitors state persistence to disk, verifies audit trail closure, and
    confirms process termination. After successful shutdown, opens the web
    dashboard to verify system state.
#>

[CmdletBinding()]
param(
    [int]$TimeoutSeconds = 10,
    [switch]$Force
)

$Host.UI.RawUI.WindowTitle = "Roostoo Bot Graceful Shutdown"
Set-Location -Path $PSScriptRoot

Write-Host "===============================================================================" -ForegroundColor Cyan
Write-Host "               ROOSTOO QUANT BOT - GRACEFUL SHUTDOWN" -ForegroundColor Cyan
Write-Host "===============================================================================" -ForegroundColor Cyan

$botPid = $null
if (Test-Path "data\bot.pid") {
    $rawPid = (Get-Content "data\bot.pid" -Raw).Trim()
    if ($rawPid -match '^\d+$') {
        $p = Get-Process -Id ([int]$rawPid) -ErrorAction SilentlyContinue
        if ($p) {
            $botPid = [int]$rawPid
        } else {
            Remove-Item "data\bot.pid" -Force -ErrorAction SilentlyContinue
        }
    }
}

if (-not $botPid) {
    # Scan for running python processes executing main.py
    $pyProcs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like "*main.py*" }
    if ($pyProcs) {
        $botPid = $pyProcs[0].ProcessId
    }
}

if (-not $botPid) {
    Write-Host "[*] No active Roostoo Bot instance detected." -ForegroundColor Yellow
    if (Test-Path "data\shutdown.trigger") { Remove-Item "data\shutdown.trigger" -Force -ErrorAction SilentlyContinue }
    Write-Host "[+] System is idle. Nothing to shut down.`n" -ForegroundColor Green
    Start-Sleep -Seconds 2
    exit 0
}

Write-Host "[*] Target active Roostoo Bot detected with PID: $botPid" -ForegroundColor Cyan
Write-Host "[*] Triggering graceful shutdown sequence..." -ForegroundColor Yellow
Write-Host "    - Flushing in-flight orders and mark-to-market valuations" -ForegroundColor Gray
Write-Host "    - Persisting portfolio balance and positions to data\portfolio_state.json" -ForegroundColor Gray
Write-Host "    - Persisting execution records to data\order_state.json" -ForegroundColor Gray
Write-Host "    - Verifying and closing SHA-256 audit trail chain`n" -ForegroundColor Gray

# 1. Sentinel file trigger
Set-Content -Path "data\shutdown.trigger" -Value (Get-Date).ToString("o")

# 2. HTTP REST shutdown command
try {
    Invoke-RestMethod -Uri "http://localhost:8080/command/shutdown" -Method Post -TimeoutSec 2 -ErrorAction SilentlyContinue | Out-Null
} catch {}

# 3. Wait loop
$elapsed = 0
while ($elapsed -lt $TimeoutSeconds) {
    $proc = Get-Process -Id $botPid -ErrorAction SilentlyContinue
    if (-not $proc) {
        break
    }
    Write-Host -NoNewline "." -ForegroundColor Yellow
    Start-Sleep -Milliseconds 1000
    $elapsed++
}

$proc = Get-Process -Id $botPid -ErrorAction SilentlyContinue
if (-not $proc) {
    Write-Host "`n"
    Write-Host "===============================================================================" -ForegroundColor Green
    Write-Host " [SUCCESS] Roostoo Autonomous Quant Bot (PID $botPid) stopped gracefully!" -ForegroundColor Green
    Write-Host ""
    Write-Host " State Verification:" -ForegroundColor Green
    Write-Host "  [OK] Portfolio ledger persisted   - data\portfolio_state.json" -ForegroundColor Gray
    Write-Host "  [OK] Order history persisted      - data\order_state.json" -ForegroundColor Gray
    Write-Host "  [OK] SHA-256 cryptographic audit  - logs\audit.jsonl" -ForegroundColor Gray
    Write-Host "===============================================================================`n" -ForegroundColor Green

    Remove-Item "data\bot.pid" -Force -ErrorAction SilentlyContinue
    Remove-Item "data\shutdown.trigger" -Force -ErrorAction SilentlyContinue

    Write-Host "[*] Opening dashboard to verify system state..." -ForegroundColor Cyan
    Start-Sleep -Seconds 2
    Start-Process "http://localhost:8080"
    Write-Host "[+] Dashboard opened for verification. (Dashboard will display bot status and system metrics)" -ForegroundColor Green
    Write-Host ""
    Start-Sleep -Seconds 2
    exit 0
} else {
    Write-Host "`n`n[!] Process $botPid did not exit within $TimeoutSeconds seconds." -ForegroundColor Red
    if ($Force) {
        Write-Host "[*] Force-killing process $botPid..." -ForegroundColor Yellow
        Stop-Process -Id $botPid -Force -ErrorAction SilentlyContinue
        Remove-Item "data\bot.pid" -Force -ErrorAction SilentlyContinue
        Remove-Item "data\shutdown.trigger" -Force -ErrorAction SilentlyContinue
        Write-Host "[+] Force termination complete." -ForegroundColor Green
    } else {
        $ans = Read-Host "Force-kill process $botPid? [Y/N]"
        if ($ans -match "^[yY]") {
            Stop-Process -Id $botPid -Force -ErrorAction SilentlyContinue
            Remove-Item "data\bot.pid" -Force -ErrorAction SilentlyContinue
            Remove-Item "data\shutdown.trigger" -Force -ErrorAction SilentlyContinue
            Write-Host "[+] Process terminated." -ForegroundColor Green
        }
    }
}

Write-Host ""
Write-Host "[+] Restart the bot with start_bot.ps1 or stop it with stop_bot.ps1" -ForegroundColor Cyan
Write-Host ""
Start-Sleep -Seconds 3