# PRODUCTION OPERATIONS & RUNBOOK

This operational runbook explains daily monitoring, live dashboard telemetry, audit trail verification, error recovery procedures, and emergency stop protocols for the autonomous trading bot.

---

## 1. Daily Monitoring Commands

### 1.1 View Real-Time Observability Dashboard
To view live terminal telemetry:

```bash
docker compose logs -f --tail=30 roostoo-bot
```

### 1.2 Inspect Recent Audit Trail Decisions
To view the latest structured strategy decisions:

```bash
tail -n 20 logs/audit_trail.jsonl | jq .
```

### 1.3 Inspect API Request Latencies & Rate Limiting
To inspect HTTP latency and retry behavior:

```bash
tail -n 20 logs/api_requests.jsonl | jq .
```

---

## 2. Dashboard Telemetry Interpretation

The live terminal dashboard renders periodically with the following telemetry:

```text
+----------------------------------------------------------------------+
|              ROOSTOO AUTONOMOUS QUANT BOT STATUS                     |
+----------------------------------------------------------------------+
| MODE:                LIVE                                            |
| BOT STATUS:          RUNNING AUTONOMOUSLY                            |
| LAST MARKET UPDATE:  15:30:00 UTC                                    |
| LAST API REQUEST:    /v3/ticker @ 15:30:00 UTC                       |
| PORTFOLIO EQUITY:    $  105,820.40 USD                               |
| AVAILABLE CASH:      $   45,529.38 USD                               |
| OPEN POSITIONS:      1                                               |
| GROSS EXPOSURE:       52.40% (Limit: 100%)                           |
| TOTAL PNL:           $   +5,820.40 USD ( +5.82%)                     |
| CURRENT DRAWDOWN:      0.82% (Limit: 6.0%)                           |
| ROLLING 24H DD:        0.45% (Freeze: 3.5%)                          |
| COMPOSITE SCORE:      7.7400 (Sharpe: 2.02, Sortino: 4.84)           |
| ACTIVE STRATEGY:     VALUE_AREA                                      |
| LAST SIGNAL:         BUY BTC/USD via VALUE_AREA (conf=0.85)          |
| LAST ORDER:          BUY BTC/USD qty=0.62 px=84,200.00 (FILLED)      |
| LAST ERROR:          None                                            |
+----------------------------------------------------------------------+
```

### Key Health Indicators:
- **MODE**: Must read `LIVE` during competition, `DRY_RUN` during testing.
- **PORTFOLIO EQUITY**: Total value of free cash + locked cash + open positions.
- **AVAILABLE CASH**: Cash available after reserving the mandatory $5.0\%$ cash cushion.
- **GROSS EXPOSURE**: Must strictly stay $\le 100\%$. Any value $> 100\%$ indicates leverage, which triggers an immediate risk alert.
- **CURRENT DRAWDOWN**: Peak-to-trough drawdown relative to historical high. If $\ge 6.0\%$, trading permanently stops.
- **ROLLING 24H DD**: Rolling 24-hour drawdown. If $\ge 3.5\%$, trading freezes for 6 hours.

---

## 2.1 Interactive Web Telemetry Dashboard

The bot embeds an institutional-grade, dark-themed single-page web dashboard powered by FastAPI and real-time WebSocket broadcasting:

- **URL**: `http://<ec2-ip-or-localhost>:8080/`
- **Port Configuration**: Configurable in `config/config.yaml` or via environment variable `ROOSTOO_DASHBOARD_PORT=8080` (or CLI `--port 8080`).
- **Security / Auth**: Optional Bearer token authentication via `ROOSTOO_DASHBOARD_TOKEN` (bypassed if left unset).
- **Core Workspace Tabs**:
  1. **Live Market & Strategies**: Real-time ticker feeds, quantitative regime detection, and microstructure metrics for Strategy A (Volume Profile 70% VAH/VAL/POC with visual price position gauge), Strategy B (Liquidity Sweeps & MSS with displacement flags and FVG detection), Strategy C (CVD delta & absorption status), and active prioritized signal evaluation.
  2. **Positions**: Live open positions table with real-time unrealized PnL ($ and %), trailing stop ratchet level (+1.0R breakeven, +2.0R trailing ATR), TP1/TP2 targets, duration, and one-click "De-Risk to Cash" controls.
  3. **Execution & Orders**: Detailed order ledger showing Client Order IDs, execution types (Limit/Market), filled prices, fees, and execution status.
  4. **Cryptographic Audit Trail**: Live SHA-256 hash-chained decision audit feed with one-click full chain re-verification.
  5. **Telemetry & Logs**: Real-time console log viewer, token-bucket rate limiter telemetry, and safety parameters.
- **Interactive Command Controls**:
  - `KILL / DE-RISK`: Emergency market liquidation of all open positions with permanent circuit breaker activation.
  - `PAUSE / RESUME`: Operator hold switch to halt/resume entry scanning without affecting open position trailing stops.
  - `RECONCILE`: Force on-demand balance and position synchronization against exchange truth.
  - `RESET RISK`: Operator reset for the 6-hour cooling freeze after drawdown inspection.
  - `QUICK TRADE`: Sized trade execution through the central risk manager.

---


## 3. Cryptographic Audit Trail Verification

The bot includes built-in verification of the SHA-256 hash chain to prove that log records have not been altered, deleted, or inserted out of order:

```bash
docker compose exec roostoo-bot python -c "
from logs.audit_logger import AuditLogger
logger = AuditLogger()
valid, msg, count = logger.verify_integrity()
print(f'Audit Chain Status: {valid}')
print(f'Verification Report: {msg}')
print(f'Total Certified Events: {count}')
if not valid:
    exit(1)
"
```

Expected output:
```text
Audit Chain Status: True
Verification Report: All 142 audit records successfully verified. Chain is intact.
Total Certified Events: 142
```

---

## 4. Crash Recovery & UNKNOWN Order Resolution

If an unexpected server reboot or container restart occurs:
1. Docker automatically launches the container via `restart: unless-stopped`.
2. The bot initializes `OrderStateManager` and `PortfolioTracker`, loading disk records from `data/order_state.json` and `data/portfolio_state.json`.
3. If an order was mid-flight during the crash, its status is marked `UNKNOWN`.
4. The bot automatically executes `ReconciliationEngine.reconcile()`:
   - Queries Roostoo `/v3/query_order` by pair and timestamp window.
   - If the order exists on the exchange, aligns status to `FILLED` or `PENDING`.
   - If the order does not exist and exceeds 60 seconds of age, marks it `REJECTED`.
   - Synchronizes cash and position balances to exchange truth.
5. Normal autonomous trading resumes only after reconciliation confirms $100\%$ synchronization.

---

## 5. One-Click Windows Start/Stop Operations

For Windows desktop users, use the provided clickable batch scripts for convenient bot management:

### 5.1 Click Scripts

| Script | Action | Description |
|--------|--------|-------------|
| **`start_bot.bat`** | Start Bot + Launch Dashboard | Double-click to launch the autonomous bot in a new console window and automatically open the web dashboard at `http://localhost:8080`. The bot runs in the background with dry-run mode.

| **`stop_bot.bat`** | Graceful Shutdown | Double-click to gracefully stop the bot. Sends shutdown signal to bot's REST API, waits for graceful termination (10 seconds), then opens the dashboard to verify system state. Preserves portfolio and audit ledger.

| **`restart_bot.bat`** | Stop + Start | Runs `stop_bot.bat`, waits 2 seconds, then launches `start_bot.bat`. Useful for quick restarts without losing bot state.

### 5.2 Usage

1. **Starting the Bot**: Double-click `start_bot.bat` from your command prompt in the bot's root directory (`D:\MKT\BOT Experiments\BTCETH`).

2. **Stopping the Bot**: Double-click `stop_bot.bat` from the same directory while the bot is running.

3. **Automatic Browser Opening**: Both scripts automatically open the web dashboard URL after starting or stopping, so you can immediately verify bot status and system metrics.

4. **Script Output**: The scripts provide visual feedback and keep the console window open, making it easy to monitor execution.

### 5.3 Script Features

- **Automatic Python Detection**: Scripts detect if `.venv\Scripts\python.exe` or `venv\Scripts\python.exe` exists, or fall back to system PATH.
- **PID Management**: Track and manage bot process IDs using `data\bot.pid` file.
- **Stale PID Cleanup**: Automatically detect and remove stale PID files if the process is not running.
- **State Preservation**: Gracefully shutdown preserves portfolio and order state to `data\portfolio_state.json` and `data\order_state.json`.
- **Audit Trail Integrity**: Verifies and maintains cryptographic audit trail throughout shutdown.

---

## 6. Emergency Stop Runbook

If you need to halt the bot immediately and liquidate all positions to cash:

### Fast Graceful Stop:
```bash
docker compose down
```
Docker sends `SIGTERM`, allowing `main.py` 15 seconds to persist ledger states, close active handles, and verify hash integrity.

### Force Liquidation to Cash:
To manually liquidate open spot positions through the bot:
```bash
docker compose exec roostoo-bot python -c "
from main import load_config, RoostooAutonomousBot
from core.strategy_engine import Signal
config = load_config()
bot = RoostooAutonomousBot(config)
for sym, pos in list(bot.portfolio.positions.items()):
    if pos.quantity > 0:
        print(f'Liquidating {sym} ({pos.quantity:.6f})...')
        sig = Signal('EMERGENCY', sym, 'DE_RISK', 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 'Manual Emergency Liquidate', '', 0)
        dec = bot.risk_manager.evaluate_signal(sig)
        bot.executor.execute_decision(sig, dec, pos.current_price)
print('All positions liquidated to cash.')
"
```
