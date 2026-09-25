<#
.SYNOPSIS
    Starts the Roostoo Autonomous Quant Trading Bot with Dashboard Launch.
.DESCRIPTION
    Launches the autonomous trading engine in a new console window and
    automatically opens the web telemetry dashboard. Detects Python virtual
    environments or system Python, checks for running instances, and provides
    one-click start functionality.
#>

[CmdletBinding()]
param(
    [switch]$Live,
    [switch]$DryRun,
    [switch]$Backtest,
    [string]$Config = "config/config.yaml",
    [int]$Port = 8080
)

$Host.UI.RawUI.WindowTitle = "Roostoo Autonomous Quant Bot [STARTING]"
Set-Location -Path $PSScriptRoot

Write-Host "===============================================================================" -ForegroundColor Cyan
Write-Host "               ROOSTOO AUTONOMOUS QUANT TRADING BOT" -ForegroundColor Cyan
Write-Host "===============================================================================" -ForegroundColor Cyan

# 1. Detect Python
$pythonExe = $null
if (Test-Path ".venv\Scripts\python.exe") {
    $pythonExe = ".venv\Scripts\python.exe"
    Write-Host "[*] Using virtual environment Python: .venv\Scripts\python.exe" -ForegroundColor Green
} elseif (Test-Path "venv\Scripts\python.exe") {
    $pythonExe = "venv\Scripts\python.exe"
    Write-Host "[*] Using virtual environment Python: venv\Scripts\python.exe" -ForegroundColor Green
} else {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) {
        $pythonExe = $cmd.Source
        Write-Host "[*] Using system Python from PATH: $pythonExe" -ForegroundColor Green
    } else {
        Write-Host "[!] ERROR: Python 3.10+ was not found in PATH or virtual environments." -ForegroundColor Red
        Read-Host "Press Enter to exit..."
        exit 1
    }
}

# 2. Check if already running
if (Test-Path "data\bot.pid") {
    $runningPid = (Get-Content "data\bot.pid" -Raw).Trim()
    if ($runningPid -match '^\d+$') {
        $proc = Get-Process -Id ([int]$runningPid) -ErrorAction SilentlyContinue
        if ($proc) {
            Write-Host "`n[!] WARNING: Bot is ALREADY RUNNING with PID $runningPid." -ForegroundColor Yellow
            Write-Host "    Web Dashboard:  http://localhost:8080" -ForegroundColor Cyan
            Write-Host "    To Stop Bot:    Run '.\stop_bot.ps1' or 'stop_bot.bat'`n" -ForegroundColor Yellow
            $open = Read-Host "Open Dashboard in browser? [Y/N]"
            if ($open -match "^[yY]") {
                Start-Process "http://localhost:8080"
            }
            exit 0
        } else {
            Remove-Item "data\bot.pid" -Force -ErrorAction SilentlyContinue
        }
    }
}

# 3. Prepare directories and sentinel triggers
if (-not (Test-Path "data")) { New-Item -ItemType Directory -Path "data" | Out-Null }
if (-not (Test-Path "logs")) { New-Item -ItemType Directory -Path "logs" | Out-Null }
if (Test-Path "data\shutdown.trigger") { Remove-Item "data\shutdown.trigger" -Force -ErrorAction SilentlyContinue }

# 4. Build arguments
$argsList = @()
if ($DryRun) { $argsList += "--dry-run" }
if ($Live) { $argsList += "--live" }
if ($Backtest) { $argsList += "--backtest" }
if ($Config) { $argsList += "--config", $Config }
if ($Port) { $argsList += "--port", $Port }

Write-Host "[*] Launching autonomous trading engine..." -ForegroundColor Cyan
Write-Host "[*] Web Dashboard: http://localhost:$($Port)" -ForegroundColor Cyan
Write-Host "[*] Automatically opening dashboard in browser..." -ForegroundColor Cyan
Write-Host "===============================================================================`n" -ForegroundColor Cyan

# Launch bot in new console window
$botArgs = @("main.py", "--dry-run")
$botWindow = Start-Process -FilePath $pythonExe -ArgumentList $botArgs -PassThru -WindowStyle Minimized

# Wait for bot to start, then open dashboard
Start-Sleep -Seconds 3
Start-Process "http://localhost:8080"

$Host.UI.RawUI.WindowTitle = "Roostoo Autonomous Quant Bot [RUNNING]"

Write-Host "================================================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Bot process launched (PID: $($botWindow.Id)). Web dashboard opened at" -ForegroundColor Green
Write-Host "  http://localhost:8080"
Write-Host ""
Write-Host "  To stop the bot gracefully, run '.\stop_bot.ps1' or double-click stop_bot.bat" -ForegroundColor Yellow
Write-Host ""

# Wait for bot to exit
while (!$botWindow.HasExited) {
    Start-Sleep -Milliseconds 500
}

Write-Host "================================================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host " [+] Bot terminated with exit code: $($botWindow.ExitCode)"
Write-Host ""

$Host.UI.RawUI.WindowTitle = "Roostoo Autonomous Quant Bot [STOPPED]"
Write-Host "================================================================================"
Write-Host ""

Read-Host "Press Enter to exit..."