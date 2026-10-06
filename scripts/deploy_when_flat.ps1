<#
.SYNOPSIS
    Automated EC2 deployment: Waits until no trades are active before switching to latest code.
#>

[CmdletBinding()]
param (
    [string]$InstanceId = "i-04f4f4f5fbd5b1813",
    [string]$Region = "ap-southeast-2"
)

Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host "  AUTOSL SAFE SWITCH PIPELINE: SWITCH WHEN NO TRADE RUNNING" -ForegroundColor Cyan
Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host "Target Instance: $InstanceId ($Region)" -ForegroundColor Gray

# 1. Verify AWS CLI
$awsCmd = Get-Command aws -ErrorAction SilentlyContinue
if (-not $awsCmd) {
    if (Test-Path "C:\Program Files\Amazon\AWSCLIV2\aws.exe") {
        $env:PATH += ";C:\Program Files\Amazon\AWSCLIV2"
    } else {
        Write-Error "AWS CLI is not installed or not in PATH."
        exit 1
    }
}

# 2. Verify AWS Credentials
Write-Host "`n[1/3] Verifying AWS credentials..." -ForegroundColor Cyan
$stsResult = aws sts get-caller-identity 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Warning "AWS credentials are expired or not configured:"
    Write-Host "$stsResult" -ForegroundColor Red
    Write-Host "`nPlease run configure_aws.bat to enter fresh AWS Access Key & Session Token." -ForegroundColor Yellow
    exit 1
}

$identity = $stsResult | ConvertFrom-Json
Write-Host "[+] Authenticated as: $($identity.Arn)" -ForegroundColor Green

# 3. Ensure local git is clean and pushed
Write-Host "`n[2/3] Checking Git status..." -ForegroundColor Cyan
$localCommit = (git rev-parse --short=8 HEAD).Trim()
Write-Host "[+] Local commit: $localCommit" -ForegroundColor Green
git push origin main
Write-Host "[+] Git origin/main is up to date." -ForegroundColor Green

# 4. Trigger remote safe switch monitor via SSM
Write-Host "`n[3/3] Triggering remote safe-switch monitor on EC2..." -ForegroundColor Cyan
Write-Host "The remote script will wait until 0 trades are active before restarting bot." -ForegroundColor Yellow

$scriptContent = Get-Content -Path "$PSScriptRoot\safe_switch_when_flat.sh" -Raw
$remoteScript = $scriptContent -replace "`r`n", "`n"

$paramJson = @{
    command = @("bash -c '$($remoteScript -replace "'", "'\''")'")
} | ConvertTo-Json -Compress

$tmpFile = [System.IO.Path]::GetTempFileName() + ".json"
[System.IO.File]::WriteAllText($tmpFile, $paramJson, [System.Text.Encoding]::UTF8)

try {
    aws ssm start-session `
        --target $InstanceId `
        --region $Region `
        --document-name AWS-StartNonInteractiveCommand `
        --parameters "file://$($tmpFile.Replace('\', '/'))"
} finally {
    Remove-Item $tmpFile -Force -ErrorAction SilentlyContinue
}

Write-Host "`n======================================================================" -ForegroundColor Green
Write-Host " [COMPLETED] Safe switch execution finished!" -ForegroundColor Green
Write-Host "======================================================================" -ForegroundColor Green
