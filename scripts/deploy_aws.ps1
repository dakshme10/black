<#
.SYNOPSIS
    One-click Local -> GitHub -> AWS EC2 Deployment Script via AWS SSM
.DESCRIPTION
    1. Validates local git state
    2. Runs full regression test suite locally
    3. Pushes committed changes to GitHub
    4. Triggers remote update on EC2 instance (i-04f4f4f5fbd5b1813) via AWS Systems Manager (SSM)
    5. Verifies live bot process and 12-coin universe in tmux
#>

[CmdletBinding()]
param (
    [string]$Branch = "main",
    [string]$InstanceId = "i-04f4f4f5fbd5b1813",
    [string]$Region = "ap-southeast-2",
    [switch]$SkipLocalTests = $false
)

$ErrorActionPreference = "Stop"

Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host "  AUTOSL ONE-CLICK DEPLOYMENT: LOCAL -> GITHUB -> AWS EC2 (SSM)" -ForegroundColor Cyan
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
    python -m pytest tests/ -q
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

# 4. Trigger AWS Remote Deployment via SSM
Write-Host "`n[+] Step 3/3: Triggering automated remote deployment pipeline on AWS EC2 ($InstanceId)..." -ForegroundColor Cyan

$ssmDeployScript = @"
cd /home/ssm-user/black && \
git fetch origin $Branch && \
git checkout $Branch && \
git reset --hard origin/$Branch && \
source venv/bin/activate && \
pytest -q tests/ && \
(curl -s -X POST http://localhost:8080/command/shutdown || true) && \
sleep 3 && \
tmux kill-session -t btceth 2>/dev/null || true && \
pkill -f "python.*main.py" 2>/dev/null || true && \
sleep 2 && \
tmux new-session -d -s btceth "cd /home/ssm-user/black && source venv/bin/activate && python main.py --live" && \
sleep 5 && \
tmux capture-pane -t btceth -p | tail -n 25
"@

$paramJson = @{
    command = @("bash -c '$($ssmDeployScript -replace "'", "'\''")'")
} | ConvertTo-Json -Compress

$tmpFile = [System.IO.Path]::GetTempFileName() + ".json"
$paramJson | Out-File -FilePath $tmpFile -Encoding utf8

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
Write-Host " [SUCCESS] Deployment pipeline executed! Target commit: $localCommit" -ForegroundColor Green
Write-Host "======================================================================" -ForegroundColor Green
