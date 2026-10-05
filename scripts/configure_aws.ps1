<#
.SYNOPSIS
    Configures AWS CLI credentials cleanly with full support for long SSO session tokens.
#>

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  AWS CREDENTIALS CONFIGURATION (SSO / IAM / Temporary)" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "Please obtain fresh credentials from your AWS Access Portal." -ForegroundColor Yellow
Write-Host ""

$key = Read-Host "Enter AWS Access Key ID"
if (-not $key) {
    Write-Host "[-] Access Key cannot be empty." -ForegroundColor Red
    pause
    exit 1
}

$secret = Read-Host "Enter AWS Secret Access Key"
$token = Read-Host "Enter AWS Session Token (Press Enter if using permanent IAM keys)"
$region = Read-Host "Enter AWS Region [ap-southeast-2]"
if (-not $region) {
    $region = "ap-southeast-2"
}

# Apply via AWS CLI
aws configure set aws_access_key_id $key.Trim()
aws configure set aws_secret_access_key $secret.Trim()
if ($token -and $token.Trim()) {
    aws configure set aws_session_token $token.Trim()
} else {
    aws configure set aws_session_token ""
}
aws configure set region $region.Trim()
aws configure set output "json"

Write-Host "`n[+] Saved credentials. Verifying identity with AWS STS..." -ForegroundColor Green
aws sts get-caller-identity

if ($LASTEXITCODE -eq 0) {
    Write-Host "`n[SUCCESS] AWS credentials are valid and active!" -ForegroundColor Green
} else {
    Write-Host "`n[!] Verification failed. Please ensure the token was generated recently and not expired." -ForegroundColor Red
}
