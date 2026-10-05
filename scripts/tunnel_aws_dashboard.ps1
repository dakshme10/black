param (
    [string]$InstanceId = "i-04f4f4f5fbd5b1813",
    [string]$Region = "ap-southeast-2",
    [int]$RemotePort = 8080,
    [int]$LocalPort = 8080
)

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  AWS SSM PORT FORWARDING: COMPETITION BOT DASHBOARD" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "Target Instance : $InstanceId"
Write-Host "Target Region   : $Region"
Write-Host "Mapping         : localhost:$LocalPort -> remote:$RemotePort"
Write-Host ""

# Check if port is already in use locally
$existingConn = Get-NetTCPConnection -LocalPort $LocalPort -ErrorAction SilentlyContinue
if ($existingConn) {
    Write-Warning "Port $LocalPort is currently occupied by process ID $($existingConn.OwningProcess[0])."
    Write-Host "You may want to terminate that process or use -LocalPort 8081."
    exit 1
}

# Check if AWS CLI is available
$awsCmd = Get-Command aws -ErrorAction SilentlyContinue
if (-not $awsCmd) {
    # Check default install path
    if (Test-Path "C:\Program Files\Amazon\AWSCLIV2\aws.exe") {
        $env:PATH += ";C:\Program Files\Amazon\AWSCLIV2"
    } else {
        Write-Error "AWS CLI is not found in PATH or standard installation directory."
        exit 1
    }
}

# Check session-manager-plugin
$ssmPlugin = Get-Command session-manager-plugin -ErrorAction SilentlyContinue
if (-not $ssmPlugin) {
    if (Test-Path "$env:LOCALAPPDATA\Programs\Amazon\SessionManagerPlugin\bin\session-manager-plugin.exe") {
        $env:PATH += ";$env:LOCALAPPDATA\Programs\Amazon\SessionManagerPlugin\bin"
    } elseif (Test-Path "C:\Program Files\Amazon\SessionManagerPlugin\bin\session-manager-plugin.exe") {
        $env:PATH += ";C:\Program Files\Amazon\SessionManagerPlugin\bin"
    }
}

Write-Host "Starting SSM Port Forwarding session..." -ForegroundColor Green
Write-Host "Once active, open: http://localhost:$LocalPort in your browser." -ForegroundColor Yellow
Write-Host "Press Ctrl+C to close the tunnel." -ForegroundColor Gray
Write-Host ""
# Auto-open browser after 2 seconds
Start-Job -ScriptBlock { param($port) Start-Sleep -Seconds 2; Start-Process "http://localhost:$port" } -ArgumentList $LocalPort | Out-Null

while ($true) {
    aws ssm start-session `
        --region $Region `
        --target $InstanceId `
        --document-name AWS-StartPortForwardingSession `
        --parameters "portNumber=[`"$RemotePort`"],localPortNumber=[`"$LocalPort`"]"

    Write-Host "`n[*] Session closed or timed out due to inactivity." -ForegroundColor Yellow
    Write-Host "[+] Re-establishing port forward in 3 seconds... (Press Ctrl+C to terminate)" -ForegroundColor Cyan
    Start-Sleep -Seconds 3
}

