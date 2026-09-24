# AWS EC2 & DOCKER PRODUCTION DEPLOYMENT GUIDE

This document provides step-by-step instructions for provisioning, configuring, and operating the Roostoo Autonomous Quant Bot on an AWS EC2 instance using Docker and Docker Compose.

---

## 1. AWS EC2 Instance Architecture

### Recommended Specifications:
- **Instance Type**: `t3.small` (2 vCPU, 2 GB RAM) or `t3.medium` (2 vCPU, 4 GB RAM)
- **AMI**: Ubuntu 22.04 LTS (x86_64) or Amazon Linux 2023
- **Storage**: 20 GB gp3 EBS Volume
- **Security Group Rules**:
  - Inbound: Port 22 (SSH) restricted strictly to your trusted admin IP address.
  - Inbound: No open HTTP/HTTPS ports required (the bot operates purely as an autonomous outbound REST client).
  - Outbound: All traffic or Port 443 (HTTPS) to connect to `https://mock-api.roostoo.com`.

---

## 2. Server Provisioning & Docker Installation

SSH into your freshly provisioned EC2 instance:

```bash
ssh -i /path/to/your-key.pem ubuntu@<EC2-PUBLIC-IP>
```

Update system packages and install Docker:

```bash
# Update package repositories
sudo apt-get update && sudo apt-get upgrade -y

# Install prerequisite packages
sudo apt-get install -y ca-certificates curl gnupg lsb-release git

# Add Docker's official GPG key and repository
sudo mkdir -p /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Install Docker Engine and Docker Compose
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

# Enable Docker to start on boot
sudo systemctl enable docker
sudo systemctl start docker

# Add ubuntu user to docker group (avoids needing sudo)
sudo usermod -aG docker $USER
newgrp docker
```

Verify Docker installation:

```bash
docker --version
docker compose version
```

---

## 3. Repository Deployment & Configuration

Clone the repository to the EC2 instance:

```bash
cd ~
git clone <YOUR-GIT-REPOSITORY-URL> roostoo-bot
cd roostoo-bot
```

### Configure Environment Variables
Copy `.env.example` to `.env`:

```bash
cp .env.example .env
chmod 600 .env  # Restrict permissions to owner only
nano .env
```

Set your competition credentials and ensure `DRY_RUN` is initially set to `true`:

```ini
# Roostoo API Credentials
ROOSTOO_API_KEY="your_actual_api_key"
ROOSTOO_SECRET_KEY="your_actual_secret_key"
ROOSTOO_BASE_URL="https://mock-api.roostoo.com"

# Execution Mode Gate
DRY_RUN=true
LIVE_TRADING_ENABLED=false

# Portfolio Constraints
INITIAL_WALLET_USD=100000.0
MIN_CASH_RESERVE_PCT=0.05
MAX_RISK_PER_TRADE_PCT=0.01
MAX_DRAWDOWN_PCT=0.06
ROLLING_24H_DRAWDOWN_PCT=0.035

# Audit
ENABLE_HASH_CHAIN=true
```

---

## 4. Pre-Flight Verification & Unit Tests

Run the automated test suite inside a clean container environment to verify system integrity before live execution:

```bash
# Build the production Docker image
docker build -t roostoo-quant-bot:latest .

# Run all 28 automated unit tests inside the container
docker run --rm roostoo-quant-bot:latest -m pytest tests/ -v
```

All 28 tests must pass ($100\%$ pass rate).

---

## 5. Paper / Dry-Run Validation

Run a single-cycle status check to verify API connectivity and server-time synchronization:

```bash
docker run --rm --env-file .env roostoo-quant-bot:latest --status
```

Start the bot in continuous `DRY_RUN` mode using Docker Compose:

```bash
# Start container in detached background mode
docker compose up -d

# Verify container is running and healthy
docker compose ps

# Follow live terminal logs
docker compose logs -f roostoo-bot
```

Observe for at least 30 minutes to verify:
- Market data polling from Roostoo mock exchange.
- Zero API rate limit errors (HTTP 429).
- Clean decision logs written to `logs/audit_trail.jsonl`.
- Valid hash-chain sequence integrity.

---

## 6. Live-Trading Deployment Gate (Section 33)

Only after successful dry-run validation, enable real order execution:

1. Stop the dry-run container:
   ```bash
   docker compose down
   ```

2. Edit `.env` to enable live execution:
   ```ini
   DRY_RUN=false
   LIVE_TRADING_ENABLED=true
   ```

3. Launch the production live bot:
   ```bash
   docker compose up -d --build
   ```

4. Verify live startup reconciliation in the container logs:
   ```bash
   docker compose logs -n 50 roostoo-bot
   ```

Confirm that the startup output displays:
```text
MODE = LIVE
Safety Gate Status: All live safety gates verified and passing.
[+] Executing Startup Reconciliation against Roostoo exchange truth...
    Reconciliation Success: True
```

---

## 7. Container Health & Auto-Recovery

The bot is configured with Docker's native healthcheck and restart policy:
- `restart: unless-stopped`: Automatically restarts the container if an unhandled crash or EC2 reboot occurs.
- `healthcheck`: Runs `python main.py --status` every 30 seconds.
- Persistent volumes `./data` and `./logs` ensure that all order states, cash balances, and audit records survive restarts without loss of state.
