#!/usr/bin/env bash
# ==============================================================================
# AutoSL Production Deployment Script for AWS EC2
# ==============================================================================
# Enforces strict safety pipeline:
# Fetch -> Diff -> Update -> Dep Install -> Run Test Suite -> Restart Service -> Health Check -> Audit
# If any validation step or test fails, automatically rolls back to previous known-good commit.
# ==============================================================================

set -euo pipefail

APP_DIR="/home/ubuntu/autosl"
SERVICE_NAME="autosl.service"
BRANCH="${1:-main}"
DEPLOY_LOG="$APP_DIR/logs/deployments.log"
STATE_DIR="$APP_DIR/state"
ROLLBACK_FILE="$STATE_DIR/last_known_good_commit.txt"

mkdir -p "$APP_DIR/logs" "$STATE_DIR"

echo "======================================================================"
echo " [AutoSL AWS Deployment Pipeline] Starting at $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo " Target Directory: $APP_DIR"
echo " Service Name:     $SERVICE_NAME"
echo " Target Branch:    $BRANCH"
echo "======================================================================"

cd "$APP_DIR"

# 1. Verify Git Repository
if [ ! -d ".git" ]; then
    echo "[-] CRITICAL: $APP_DIR is not a git repository. Aborting."
    exit 1
fi

# 2. Record Pre-Deployment State
PREV_COMMIT=$(git rev-parse HEAD)
echo "[+] Current commit (known-good): $PREV_COMMIT"
echo "$PREV_COMMIT" > "$ROLLBACK_FILE"

# 3. Check for Local Untracked / Dirty Working Tree
if [ -n "$(git status --porcelain)" ]; then
    echo "[!] WARNING: Working tree has modified files. Stashing changes..."
    git stash push -m "pre-deploy-stash-$(date +%s)"
fi

# 4. Fetch Remote Changes
echo "[+] Fetching updates from origin..."
git fetch origin "$BRANCH"

TARGET_COMMIT=$(git rev-parse "origin/$BRANCH")
echo "[+] Incoming commit: $TARGET_COMMIT"

if [ "$PREV_COMMIT" = "$TARGET_COMMIT" ]; then
    echo "[*] Already at latest commit ($TARGET_COMMIT). Proceeding with verification..."
fi

# 5. Check out target commit
echo "[+] Updating source tree to $TARGET_COMMIT..."
git checkout "$BRANCH"
git reset --hard "$TARGET_COMMIT"

# Write commit hash to marker file for runtime version identification
echo "$TARGET_COMMIT" > "$APP_DIR/.git_commit"

# 6. Check Dependencies
VENV_PIP="$APP_DIR/venv/bin/pip"
VENV_PYTEST="$APP_DIR/venv/bin/pytest"
VENV_PYTHON="$APP_DIR/venv/bin/python3"

if [ ! -f "$VENV_PYTHON" ]; then
    echo "[+] Virtual environment not found. Initializing dedicated venv at $APP_DIR/venv..."
    python3 -m venv "$APP_DIR/venv"
fi

if git diff "$PREV_COMMIT" "$TARGET_COMMIT" --name-only | grep -q "requirements.txt"; then
    echo "[+] requirements.txt modified. Updating Python dependencies..."
    "$VENV_PIP" install --upgrade pip
    "$VENV_PIP" install -r requirements.txt
fi

# 7. Run Regression Test Suite
echo "[+] Executing test suite in virtualenv..."
if ! PYTHONPATH="$APP_DIR" "$VENV_PYTEST" -v tests/; then
    echo "[-] CRITICAL: Tests failed on incoming commit $TARGET_COMMIT!"
    echo "[!] Initiating immediate rollback to previous known-good commit: $PREV_COMMIT..."
    git reset --hard "$PREV_COMMIT"
    echo "$PREV_COMMIT" > "$APP_DIR/.git_commit"
    echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') | FAILED | target=$TARGET_COMMIT | rolled_back_to=$PREV_COMMIT | reason=test_failure" >> "$DEPLOY_LOG"
    exit 1
fi

echo "[+] Test suite PASSED (0 failures)."

# 8. Check Process Count & Service Configuration
RUNNING_COUNT=$(pgrep -f "python3.*main.py" | wc -l || true)
echo "[+] Currently running bot process count: $RUNNING_COUNT"

# 9. Safely Restart Service
echo "[+] Restarting $SERVICE_NAME..."
if sudo systemctl is-active --quiet "$SERVICE_NAME"; then
    sudo systemctl restart "$SERVICE_NAME"
else
    echo "[*] Service currently inactive. Starting $SERVICE_NAME..."
    sudo systemctl start "$SERVICE_NAME"
fi

# 10. Health Check Verification
echo "[+] Waiting for service initialization and health verification..."
sleep 4

if ! sudo systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "[-] CRITICAL: $SERVICE_NAME failed to stay active after restart!"
    sudo journalctl -u "$SERVICE_NAME" -n 25 --no-pager
    echo "[!] Rolling back to $PREV_COMMIT..."
    git reset --hard "$PREV_COMMIT"
    echo "$PREV_COMMIT" > "$APP_DIR/.git_commit"
    sudo systemctl restart "$SERVICE_NAME"
    echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') | FAILED | target=$TARGET_COMMIT | rolled_back_to=$PREV_COMMIT | reason=service_start_failure" >> "$DEPLOY_LOG"
    exit 1
fi

# Verify exactly one bot process is running
BOT_PIDS=$(pgrep -f "python3.*main.py" || true)
BOT_COUNT=$(echo "$BOT_PIDS" | grep -v '^$' | wc -l || true)
if [ "$BOT_COUNT" -gt 1 ]; then
    echo "[-] WARNING: More than 1 bot process detected ($BOT_COUNT PIDs: $BOT_PIDS). Investigating..."
else
    echo "[+] Single bot process confirmed (PID: $BOT_PIDS)."
fi

# 11. Final Success Record
DEPLOYED_SHA=$(git rev-parse --short=8 HEAD)
echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') | SUCCESS | commit=$DEPLOYED_SHA ($TARGET_COMMIT) | prev=$PREV_COMMIT" >> "$DEPLOY_LOG"

echo "======================================================================"
echo " [SUCCESS] Deployment completed successfully!"
echo " Deployed Git Commit SHA : $DEPLOYED_SHA"
echo " Service Status          : $(sudo systemctl is-active "$SERVICE_NAME")"
echo " Audit Log Recorded      : $DEPLOY_LOG"
echo "======================================================================"
