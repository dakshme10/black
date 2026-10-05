#!/usr/bin/env bash
set -e

echo "=== [1/5] Updating repository from origin/main ==="
cd /home/ssm-user/black
git fetch origin main
git checkout main
git reset --hard origin/main
CURRENT_SHA=$(git rev-parse --short=8 HEAD)
echo "Current commit: $CURRENT_SHA"

echo "=== [2/5] Running test suite in venv ==="
source venv/bin/activate
pytest -q tests/

echo "=== [3/5] Gracefully stopping existing bot process ==="
# Attempt graceful HTTP shutdown first
curl -s -X POST http://localhost:8080/command/shutdown || true
sleep 3

# Terminate existing tmux session / python main.py if still running
tmux kill-session -t btceth 2>/dev/null || true
pkill -f "python.*main.py" 2>/dev/null || true
sleep 2

echo "=== [4/5] Launching updated bot in tmux session 'btceth' ==="
tmux new-session -d -s btceth "cd /home/ssm-user/black && source venv/bin/activate && python main.py --live"
sleep 5

echo "=== [5/5] Verifying bot startup & universe ==="
tmux capture-pane -t btceth -p | tail -n 25

echo ""
echo "Deployment to commit $CURRENT_SHA completed successfully!"
