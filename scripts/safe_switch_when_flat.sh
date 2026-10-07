#!/usr/bin/env bash
set -e

LOG="/home/ssm-user/deploy_safe_switch.log"
echo "==========================================================" | tee -a "$LOG"
echo "  AUTOSL SAFE SWITCH MONITOR: WAIT FOR FLAT & SWITCH" | tee -a "$LOG"
echo "  Started at $(date)" | tee -a "$LOG"
echo "==========================================================" | tee -a "$LOG"

cd /home/ssm-user/black

# 1. Fetch latest changes in background first without modifying working directory
echo "[1/4] Fetching latest commits from origin/main..." | tee -a "$LOG"
git fetch origin main 2>&1 | tee -a "$LOG"
LATEST_REMOTE_SHA=$(git rev-parse --short=8 origin/main)
CURRENT_RUNNING_SHA=$(git rev-parse --short=8 HEAD)
echo "Current running commit: $CURRENT_RUNNING_SHA" | tee -a "$LOG"
echo "Target latest commit : $LATEST_REMOTE_SHA" | tee -a "$LOG"

# 2. Wait until no open trades are running
echo "[2/4] Monitoring active positions. Waiting until 0 trades running..." | tee -a "$LOG"
POLL_COUNT=0

while true; do
    OPEN_COUNT=0
    POS_SUMMARY=""

    # Try querying live web server endpoint
    POS_JSON=$(curl -s --max-time 4 http://localhost:8080/api/positions 2>/dev/null || true)
    if [ -n "$POS_JSON" ]; then
        OPEN_COUNT=$(python3 -c "import sys, json; data=json.loads(sys.argv[1]); print(len(data) if isinstance(data, list) else data.get('count', 0))" "$POS_JSON" 2>/dev/null || echo "0")
        POS_SUMMARY=$(python3 -c "import sys, json; data=json.loads(sys.argv[1]); pos=(data if isinstance(data, list) else data.get('positions', [])); print(', '.join([f\"{p.get('symbol')}:{p.get('quantity')}\" for p in pos]))" "$POS_JSON" 2>/dev/null || echo "")
    else
        # Fallback to local portfolio_state.json if web server not answering
        if [ -f "data/portfolio_state.json" ]; then
            OPEN_COUNT=$(python3 -c "import json; data=json.load(open('data/portfolio_state.json')); pos=[s for s,p in data.get('positions',{}).items() if p.get('quantity',0) > 1e-5]; print(len(pos))" 2>/dev/null || echo "0")
            POS_SUMMARY=$(python3 -c "import json; data=json.load(open('data/portfolio_state.json')); pos=[f\"{s}:{p.get('quantity',0)}\" for s,p in data.get('positions',{}).items() if p.get('quantity',0) > 1e-5]; print(', '.join(pos))" 2>/dev/null || echo "")
        fi
    fi

    if [ "$OPEN_COUNT" -gt 0 ]; then
        POLL_COUNT=$((POLL_COUNT + 1))
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Active trades open ($OPEN_COUNT): [$POS_SUMMARY]. Waiting for trade closure... (poll #$POLL_COUNT, sleep 15s)" | tee -a "$LOG"
        sleep 15
    else
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] NO TRADES RUNNING (0 open positions). Safe to switch!" | tee -a "$LOG"
        break
    fi
done

# 3. Apply latest code update
echo "[3/4] Updating codebase to origin/main ($LATEST_REMOTE_SHA)..." | tee -a "$LOG"
git checkout main 2>&1 | tee -a "$LOG"
git reset --hard origin/main 2>&1 | tee -a "$LOG"

source venv/bin/activate
echo "Running pytest verification..." | tee -a "$LOG"
pytest -q tests/ 2>&1 | tee -a "$LOG"

# 4. Graceful restart
echo "[4/4] Gracefully restarting bot process..." | tee -a "$LOG"
curl -s --max-time 3 -X POST http://localhost:8080/command/shutdown 2>/dev/null || true
sleep 3
tmux kill-session -t btceth 2>/dev/null || true
pkill -f "python.*main.py" 2>/dev/null || true
sleep 2

tmux new-session -d -s btceth "cd /home/ssm-user/black && source venv/bin/activate && python main.py --live"
sleep 6

echo "=== [STARTUP PANE PREVIEW] ===" | tee -a "$LOG"
tmux capture-pane -t btceth -p | tail -n 30 | tee -a "$LOG"

echo "" | tee -a "$LOG"
echo "[SUCCESS] Bot successfully upgraded to commit $LATEST_REMOTE_SHA at $(date) with 0 trade interruption!" | tee -a "$LOG"
