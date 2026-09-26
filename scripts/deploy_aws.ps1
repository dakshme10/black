<#
.SYNOPSIS
    One-click Local -> GitHub -> AWS Deployment Script for AutoSL Bot
.DESCRIPTION
    1. Validates local git state
    2. Runs full regression test suite locally (55 tests)
    3. Pushes committed changes to GitHub
    4. Triggers safe remote deployment script on AWS EC2
    5. Displays remote deployment status and active commit SHA
#>

[CmdletBinding()]
param (
    [string]$Branch = "master",
    [switch]$SkipLocalTests = $false
)

$ErrorActionPreference = "Stop"

Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host "  AUTOSL ONE-CLICK DEPLOYMENT: LOCAL -> GITHUB -> AWS EC2" -ForegroundColor Cyan
Write-Host "======================================================================" -ForegroundColor Cyan

# 1. Check Git Status
$gitStatus = git status --porcelain
if ($gitStatus) {
    Write-Host "[!] Warning: You have uncommitted local changes:" -ForegroundColor Yellow
    git status -s
    $confirm = Read-Host "Do you want to continue without committing these? (y/N)"
    if ($confirm -ne 'y' -and $confirm -ne 'Y') {
        Write-Host "[-] Deployment cancelled. Please commit your changes first." -ForegroundColor Red
        exit 1
    }
}

$localCommit = (git rev-parse --short=8 HEAD).Trim()
Write-Host "[+] Local Commit to Deploy: $localCommit (Branch: $Branch)" -ForegroundColor Green

# 2. Run Local Test Suite
if (-not $SkipLocalTests) {
    Write-Host "`n[+] Step 1/3: Running local test suite with pytest..." -ForegroundColor Cyan
    python -m pytest
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[-] CRITICAL: Local tests failed! Aborting deployment to protect production." -ForegroundColor Red
        exit 1
    }
    Write-Host "[+] Local tests passed (0 failures)." -ForegroundColor Green
} else {
    Write-Host "[!] Skipping local tests as requested." -ForegroundColor Yellow
}

# 3. Push to GitHub
Write-Host "`n[+] Step 2/3: Pushing commits to GitHub (origin $Branch)..." -ForegroundColor Cyan
git push origin $Branch
if ($LASTEXITCODE -ne 0) {
    Write-Host "[-] Failed to push to GitHub. Aborting." -ForegroundColor Red
    exit 1
}
Write-Host "[+] Pushed to GitHub successfully." -ForegroundColor Green

# 4. Trigger AWS Remote Deployment
Write-Host "`n[+] Step 3/3: Triggering automated remote deployment pipeline on AWS EC2..." -ForegroundColor Cyan
$remoteCmd = "bash /home/ubuntu/autosl/scripts/deploy_aws.sh $Branch"
ssh -o BatchMode=yes aws-shoonya $remoteCmd

if ($LASTEXITCODE -ne 0) {
    Write-Host "[-] AWS deployment encountered an error! Check logs above." -ForegroundColor Red
    exit 1
}

Write-Host "`n======================================================================" -ForegroundColor Green
Write-Host " [SUCCESS] Deployment pipeline finished! Production is running commit: $localCommit" -ForegroundColor Green
Write-Host "======================================================================" -ForegroundColor Green
