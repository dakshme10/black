import json

script = """LOG=/home/ssm-user/deploy.log
echo "[1] Starting deployment at $(date)" > "$LOG"
cd /home/ssm-user/black >> "$LOG" 2>&1
echo "[2] Fetching origin main..." >> "$LOG"
git fetch origin main >> "$LOG" 2>&1
echo "[3] Resetting to origin/main..." >> "$LOG"
git checkout main >> "$LOG" 2>&1
git reset --hard origin/main >> "$LOG" 2>&1
echo "[4] Commit: $(git rev-parse --short=8 HEAD)" >> "$LOG"
echo "[5] Running pytest..." >> "$LOG"
source venv/bin/activate >> "$LOG" 2>&1
pytest -q tests/ >> "$LOG" 2>&1
echo "[6] Restarting bot..." >> "$LOG"
curl -s -X POST http://localhost:8080/command/shutdown >> "$LOG" 2>&1 || true
sleep 3
tmux kill-session -t btceth >> "$LOG" 2>&1 || true
pkill -f "python.*main.py" >> "$LOG" 2>&1 || true
sleep 2
tmux new-session -d -s btceth "cd /home/ssm-user/black && source venv/bin/activate && python main.py --live"
sleep 6
echo "[7] Tmux output:" >> "$LOG"
tmux capture-pane -t btceth -p | tail -n 25 >> "$LOG" 2>&1
echo "[8] Deployment finished at $(date)" >> "$LOG"
cat "$LOG"
"""

cmd = f"bash -c '{script}'"
data = {"command": [cmd]}

with open("scratch/deploy_exec.json", "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)

print("scratch/deploy_exec.json written successfully.")
