# AutoSL Quantitative Bot - Deployment & Synchronization Guide

This guide describes how the AutoSL bot is synchronized between **Local Windows**, **GitHub**, and **AWS EC2**.

---

## 1. Architectural Architecture & Flow

```text
       [ LOCAL WINDOWS ] (Primary Development & Testing)
               |
               | git commit + git push
               v
       [ GITHUB REPOSITORY ] (Authoritative Version-Controlled Truth)
               |
               | deploy_aws.sh (fetch -> test -> restart)
               v
       [ AWS EC2 INSTANCE ] (Production Runtime & Telemetry)
               |
               v
       [ RUNNING AUTOSL BOT ]
```

### The Cardinal Rule
* **Authority Flow:** `LOCAL -> GITHUB -> AWS`
* Code changes are **NEVER** made directly on AWS.
* AWS is a deployment target running a specific, verified Git commit SHA.
* Live exchange credentials stay strictly on AWS in `.env` and are **NEVER** pushed to GitHub.

---

## 2. Standard Workflow for Making Future Code Changes

Whenever you modify any code or parameters, follow these simple steps:

### Option A: One-Click Automatic Deployment (Recommended)

From your Windows PowerShell in the project directory:

```powershell
.\scripts\deploy_aws.ps1
```

This automated script will:
1. Check your Git status.
2. Run all local tests (`pytest`) — if tests fail, it stops immediately.
3. Push your verified commit to GitHub (`origin master`).
4. SSH into AWS EC2 and execute the remote deployment pipeline.
5. Test the code on AWS, reload the service, and verify single-process health.

---

### Option B: Step-by-Step Manual Deployment

If you prefer doing each step manually:

#### On Your Windows Computer:
1. Make your code or configuration edits.
2. Run the test suite:
   ```powershell
   python -m pytest
   ```
   *(Ensure all tests pass before continuing).*
3. Stage and commit your changes:
   ```powershell
   git add .
   git commit -m "feat/fix: describe your changes clearly"
   ```
4. Push to GitHub:
   ```powershell
   git push origin master
   ```

#### On AWS EC2:
1. Connect to EC2 via SSH:
   ```powershell
   ssh aws-btceth
   ```
2. Run the safe deployment script:
   ```bash
   bash /home/ubuntu/autosl/scripts/deploy_aws.sh
   ```
   *(The script fetches the commit, runs tests on AWS, and reloads the service only if tests pass).*

3. Check the service status:
   ```bash
   sudo systemctl status autosl
   ```

---

## 3. Production Service Management on AWS EC2

| Action | Command on EC2 |
| :--- | :--- |
| **Check Bot Status** | `sudo systemctl status autosl` |
| **View Live Logs** | `journalctl -u autosl -f` |
| **View Service Log File** | `tail -f /home/ubuntu/autosl/logs/autosl_service.log` |
| **Restart Bot Safely** | `sudo systemctl restart autosl` |
| **Stop Bot** | `sudo systemctl stop autosl` |
| **Start Bot** | `sudo systemctl start autosl` |
| **Rollback to Previous Commit** | `bash /home/ubuntu/autosl/scripts/rollback_aws.sh` |

---

## 4. Emergency Rollback Procedure

If a deployed commit causes runtime issues in production:

Run on AWS:
```bash
bash /home/ubuntu/autosl/scripts/rollback_aws.sh
```

This immediately restores the previous known-good Git commit SHA, re-runs tests, and safely reloads the systemd service.

---

## 5. Security & Configuration Separation

* **Source-Controlled Files (in GitHub):**
  * Core Python algorithms (`core/`, `strategies/`, `state/`)
  * Tests (`tests/`)
  * Web telemetry dashboard (`web/`, `core/web_server.py`)
  * Deployment and rollback scripts (`scripts/`)
  * Non-secret default configurations (`config/config.yaml`, `config/trading_params.py`)

* **Protected Files (NEVER in GitHub):**
  * `.env` (contains exchange API keys and live trading gate)
  * `*.key`, `*.pem`, `*.secret`
  * `logs/*.log`, `logs/*.jsonl`
  * `data/bot.pid`, `data/*.db`, `state/`
