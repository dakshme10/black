#!/usr/bin/env bash
# ==============================================================================
# AutoSL Production Rollback Script for AWS EC2
# ==============================================================================
# Rolls back the production service to the previous known-good commit.
# ==============================================================================

set -euo pipefail

APP_DIR="/home/ubuntu/autosl"
SERVICE_NAME="autosl.service"
ROLLBACK_FILE="$APP_DIR/state/last_known_good_commit.txt"
DEPLOY_LOG="$APP_DIR/logs/deployments.log"

cd "$APP_DIR"

if [ ! -f "$ROLLBACK_FILE" ]; then
    echo "[-] No rollback record found at $ROLLBACK_FILE"
    exit 1
fi

TARGET_COMMIT=$(cat "$ROLLBACK_FILE" | tr -d '[:space:]')
CURRENT_COMMIT=$(git rev-parse HEAD)

if [ "$TARGET_COMMIT" = "$CURRENT_COMMIT" ]; then
    echo "[!] Target rollback commit is identical to current commit ($CURRENT_COMMIT)."
    exit 0
fi

echo "[!] Initiating rollback from $CURRENT_COMMIT to $TARGET_COMMIT..."
git reset --hard "$TARGET_COMMIT"
echo "$TARGET_COMMIT" > "$APP_DIR/.git_commit"

echo "[+] Running test suite on rollback target..."
"$APP_DIR/venv/bin/pytest" -v tests/

echo "[+] Restarting $SERVICE_NAME..."
sudo systemctl restart "$SERVICE_NAME"
sleep 3

if sudo systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "[SUCCESS] Rollback completed. Running commit: $(git rev-parse --short=8 HEAD)"
    echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') | ROLLBACK_SUCCESS | from=$CURRENT_COMMIT | to=$TARGET_COMMIT" >> "$DEPLOY_LOG"
else
    echo "[-] CRITICAL: Service failed to start after rollback!"
    sudo journalctl -u "$SERVICE_NAME" -n 25 --no-pager
    exit 1
fi
