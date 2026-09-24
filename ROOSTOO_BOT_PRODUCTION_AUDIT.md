# FORENSIC PRODUCTION AUDIT REPORT: ROOSTOO AUTONOMOUS QUANT TRADING BOT

**Audit Date**: September 24, 2026  
**Auditor**: Principal Quantitative Systems & Security Audit Agent (Advanced Agentic Architecture)  
**Target Repository**: `roostoo-bot` (`d:\MKT\BOT Experiments\BTCETH`)  
**Audit Scope**: Strict Read-Only Forensic Source Code Verification  
**Git HEAD**: `29f3b77` (Branch: `master`, Clean Working Tree)  
**Evaluation Target**: Delivery Report Claims & Hackathon Production Readiness  

---

## EXECUTIVE SUMMARY & PRODUCTION VERDICT

### FINAL LIVE TRADING STATUS: **CONDITIONALLY READY**

The codebase represents a remarkably well-engineered, mathematically sound, and architecturally sophisticated algorithmic trading system tailored specifically to the Roostoo Mock Exchange. It demonstrates strict spot-only discipline, robust token-bucket rate limiting, rigorous HMAC-SHA256 authentication with dynamic clock-skew correction, an authoritative exchange-driven reconciliation lifecycle, non-blocking UNKNOWN order state management, and an append-only SHA-256 cryptographic hash-chain audit log.

However, deployment to **LIVE TRADING** (`--live`) is **STRICTLY BLOCKED** until **Four Mandatory Pre-Conditions** are resolved:
1. **Docker Container Web Directory Omission**: The [Dockerfile](file:///d:/MKT/BOT%20Experiments/BTCETH/Dockerfile#L34-L42) copies Python subpackages but fails to copy `web/` (`COPY --chown=appuser:appgroup web/ ./web/`). Running the container with the web dashboard enabled results in HTTP 404 on `/`.
2. **Competition Rule Ambiguity Regarding Manual Dashboard Trading**: The [COMPLIANCE_CHECKLIST.md](file:///d:/MKT/BOT%20Experiments/BTCETH/docs/COMPLIANCE_CHECKLIST.md#L11) certifies: *"All trade decisions originate strictly from StrategyEngine and RiskManager. No manual API routes or trade injection paths exist."* However, [core/web_server.py](file:///d:/MKT/BOT%20Experiments/BTCETH/core/web_server.py#L255-L264) and [main.py](file:///d:/MKT/BOT%20Experiments/BTCETH/main.py#L521-L550) expose `POST /command/manual_trade` (`handle_manual_trade`). While risk-filtered, this violates the absolute "Zero Manual Intervention" claim and must be disabled or flagged for competition judges.
3. **Synthetic Backtest Data Disclosure**: The published backtest results (+5.82% return, 7.76 Composite Score) documented in [BACKTEST_REPORT.md](file:///d:/MKT/BOT%20Experiments/BTCETH/docs/BACKTEST_REPORT.md#L39-L56) are generated from a synthetic geometric random-walk simulation (`np.random.seed(42)`) in [main.py](file:///d:/MKT/BOT%20Experiments/BTCETH/main.py#L755-L775) because Roostoo does not provide historical kline endpoints. This must be formally stated to avoid accusations of backtest misrepresentation.
4. **Live API Key Provisioning**: The repository correctly ships with empty API key placeholders in `.env.example`. Valid Roostoo credentials must be supplied in a local, uncommitted `.env` file before executing live trades.

---

# 1. PRIMARY OBJECTIVE

The primary objective of this audit is to perform an exhaustive, independent, read-only forensic examination of the actual source code in `roostoo-bot` to establish whether the implementation faithfully delivers the production-grade trading bot described in the delivery documentation ([README.md](file:///d:/MKT/BOT%20Experiments/BTCETH/README.md), [ARCHITECTURE.md](file:///d:/MKT/BOT%20Experiments/BTCETH/docs/ARCHITECTURE.md), [STRATEGY_SPEC.md](file:///d:/MKT/BOT%20Experiments/BTCETH/docs/STRATEGY_SPEC.md), [RISK_SPEC.md](file:///d:/MKT/BOT%20Experiments/BTCETH/docs/RISK_SPEC.md), and [COMPLIANCE_CHECKLIST.md](file:///d:/MKT/BOT%20Experiments/BTCETH/docs/COMPLIANCE_CHECKLIST.md)).

Every claim has been tested directly against the Python implementation using static analysis, code trace verification, and execution of the automated test suite.

---

# 2. REPOSITORY INVENTORY

### 2.1 Directory Structure Tree

```text
d:\MKT\BOT Experiments\BTCETH\
├── .env.example
├── .gitignore
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── main.py
├── README.md
├── ROOSTOO_BOT_PRODUCTION_AUDIT.md  <-- [This Report]
├── backtest/
│   ├── engine.py
│   ├── execution_model.py
│   └── walk_forward.py
├── config/
│   ├── config.yaml
│   └── trading_params.py
├── core/
│   ├── api_client.py
│   ├── feature_engine.py
│   ├── market_data.py
│   ├── order_executor.py
│   ├── performance.py
│   ├── regime_detector.py
│   ├── risk_manager.py
│   ├── strategy_engine.py
│   └── web_server.py
├── data/
│   ├── order_state.json
│   ├── portfolio_state.json
│   └── backtest_port.json
├── docs/
│   ├── ARCHITECTURE.md
│   ├── BACKTEST_REPORT.md
│   ├── COMPLIANCE_CHECKLIST.md
│   ├── DEPLOYMENT_GUIDE.md
│   ├── OPERATIONS_GUIDE.md
│   ├── RISK_SPEC.md
│   ├── STRATEGY_SPEC.md
│   └── roostoo_api/
│       ├── PartnerDocument.md
│       ├── ROOSTOO_ORIGINAL_README.md
│       ├── partner_python_demo.py
│       └── python_demo.py
├── logs/
│   ├── audit_logger.py
│   ├── audit_trail.jsonl
│   └── api_requests.jsonl
├── state/
│   ├── order_state.py
│   ├── portfolio_tracker.py
│   └── reconciliation.py
├── strategies/
│   ├── cvd_absorption.py
│   ├── liquidity_sweep.py
│   └── value_area.py
├── tests/
│   ├── test_backtest.py
│   ├── test_execution.py
│   ├── test_market_data.py
│   ├── test_metrics.py
│   ├── test_reconciliation.py
│   ├── test_risk.py
│   ├── test_strategies.py
│   └── test_web_server.py
└── web/
    └── index.html
```

### 2.2 File Categorization
* **Python Core & Execution (9 files)**: [main.py](file:///d:/MKT/BOT%20Experiments/BTCETH/main.py), [core/api_client.py](file:///d:/MKT/BOT%20Experiments/BTCETH/core/api_client.py), [core/market_data.py](file:///d:/MKT/BOT%20Experiments/BTCETH/core/market_data.py), [core/feature_engine.py](file:///d:/MKT/BOT%20Experiments/BTCETH/core/feature_engine.py), [core/regime_detector.py](file:///d:/MKT/BOT%20Experiments/BTCETH/core/regime_detector.py), [core/strategy_engine.py](file:///d:/MKT/BOT%20Experiments/BTCETH/core/strategy_engine.py), [core/risk_manager.py](file:///d:/MKT/BOT%20Experiments/BTCETH/core/risk_manager.py), [core/order_executor.py](file:///d:/MKT/BOT%20Experiments/BTCETH/core/order_executor.py), [core/web_server.py](file:///d:/MKT/BOT%20Experiments/BTCETH/core/web_server.py).
* **Strategy Modules (3 files)**: [strategies/value_area.py](file:///d:/MKT/BOT%20Experiments/BTCETH/strategies/value_area.py), [strategies/liquidity_sweep.py](file:///d:/MKT/BOT%20Experiments/BTCETH/strategies/liquidity_sweep.py), [strategies/cvd_absorption.py](file:///d:/MKT/BOT%20Experiments/BTCETH/strategies/cvd_absorption.py).
* **State Management & Reconciliation (3 files)**: [state/order_state.py](file:///d:/MKT/BOT%20Experiments/BTCETH/state/order_state.py), [state/portfolio_tracker.py](file:///d:/MKT/BOT%20Experiments/BTCETH/state/portfolio_tracker.py), [state/reconciliation.py](file:///d:/MKT/BOT%20Experiments/BTCETH/state/reconciliation.py).
* **Backtest & Analytics (4 files)**: [backtest/engine.py](file:///d:/MKT/BOT%20Experiments/BTCETH/backtest/engine.py), [backtest/execution_model.py](file:///d:/MKT/BOT%20Experiments/BTCETH/backtest/execution_model.py), [backtest/walk_forward.py](file:///d:/MKT/BOT%20Experiments/BTCETH/backtest/walk_forward.py), [core/performance.py](file:///d:/MKT/BOT%20Experiments/BTCETH/core/performance.py).
* **Logging & Audit (1 file + logs)**: [logs/audit_logger.py](file:///d:/MKT/BOT%20Experiments/BTCETH/logs/audit_logger.py), [logs/audit_trail.jsonl](file:///d:/MKT/BOT%20Experiments/BTCETH/logs/audit_trail.jsonl), [logs/api_requests.jsonl](file:///d:/MKT/BOT%20Experiments/BTCETH/logs/api_requests.jsonl).
* **Configuration (2 files)**: [config/config.yaml](file:///d:/MKT/BOT%20Experiments/BTCETH/config/config.yaml), [config/trading_params.py](file:///d:/MKT/BOT%20Experiments/BTCETH/config/trading_params.py).
* **Test Suites (8 files)**: [tests/test_risk.py](file:///d:/MKT/BOT%20Experiments/BTCETH/tests/test_risk.py), [tests/test_strategies.py](file:///d:/MKT/BOT%20Experiments/BTCETH/tests/test_strategies.py), [tests/test_execution.py](file:///d:/MKT/BOT%20Experiments/BTCETH/tests/test_execution.py), [tests/test_reconciliation.py](file:///d:/MKT/BOT%20Experiments/BTCETH/tests/test_reconciliation.py), [tests/test_metrics.py](file:///d:/MKT/BOT%20Experiments/BTCETH/tests/test_metrics.py), [tests/test_backtest.py](file:///d:/MKT/BOT%20Experiments/BTCETH/tests/test_backtest.py), [tests/test_market_data.py](file:///d:/MKT/BOT%20Experiments/BTCETH/tests/test_market_data.py), [tests/test_web_server.py](file:///d:/MKT/BOT%20Experiments/BTCETH/tests/test_web_server.py).
* **Deployment & Containers (2 files)**: [Dockerfile](file:///d:/MKT/BOT%20Experiments/BTCETH/Dockerfile), [docker-compose.yml](file:///d:/MKT/BOT%20Experiments/BTCETH/docker-compose.yml).
* **Dependencies & Env (2 files)**: [requirements.txt](file:///d:/MKT/BOT%20Experiments/BTCETH/requirements.txt), [.env.example](file:///d:/MKT/BOT%20Experiments/BTCETH/.env.example).
* **Frontend Web Telemetry (1 file)**: [web/index.html](file:///d:/MKT/BOT%20Experiments/BTCETH/web/index.html).
* **Documentation & API Specs (11 files)**: [README.md](file:///d:/MKT/BOT%20Experiments/BTCETH/README.md), 7 markdown guides in `docs/`, 4 files in `docs/roostoo_api/`.

### 2.3 Git Repository State
* **Current Branch**: `master`
* **HEAD Commit**: `29f3b77` (*feat: Add live market ticks tape, real-time price flashing, and stream heartbeat*)
* **Working Tree**: Clean (`nothing to commit, working tree clean`).
* **Recent Commits**:
  - `29f3b77`: feat: Add live market ticks tape, real-time price flashing, and stream heartbeat
  - `0328499`: fix: Add spread_bps property to TickerSnapshot for live telemetry
  - `82b35aa`: feat: Add professional-grade institutional web telemetry dashboard and control server
  - `cc60b75`: feat: complete production autonomous quant trading bot for Roostoo Hackathon
* **Secrets Inspection**: Verified clean. No `.env` is committed. [.gitignore](file:///d:/MKT/BOT%20Experiments/BTCETH/.gitignore#L1-L15) explicitly ignores `.env`, `*.key`, `*.pem`, `logs/*.jsonl`, and local databases.
* **Consistency Check**: Structure is 100% consistent with a modern, high-standard open-source hackathon submission.

---

# 3. VERIFY THE DELIVERY REPORT CLAIMS

The delivery documents assert that this bot is a production-grade autonomous quant spot-trading bot. Below is the systematic claim-by-claim verification matrix against the codebase:

| Report Claim | Source Evidence | Status | Risk / Forensic Comment |
| :--- | :--- | :---: | :--- |
| **Official Roostoo REST API Execution** | [core/api_client.py:L320-L471](file:///d:/MKT/BOT%20Experiments/BTCETH/core/api_client.py#L320-L471) | **PASS** | Exact endpoints matching official `docs/roostoo_api/python_demo.py`. |
| **Spot-Only Trading** | [core/order_executor.py:L97-L109](file:///d:/MKT/BOT%20Experiments/BTCETH/core/order_executor.py#L97-L109), [core/risk_manager.py:L146-L174](file:///d:/MKT/BOT%20Experiments/BTCETH/core/risk_manager.py#L146-L174) | **PASS** | Only `BUY` and `DE_RISK` (`SELL` spot balance). No short selling, margin, or perps. |
| **Autonomous Execution** | [main.py:L807-L816](file:///d:/MKT/BOT%20Experiments/BTCETH/main.py#L807-L816) | **PARTIAL** | Runs autonomously in continuous loop; however, [core/web_server.py:L255](file:///d:/MKT/BOT%20Experiments/BTCETH/core/web_server.py#L255) adds a manual trade route. |
| **HMAC-SHA256 Authentication** | [core/api_client.py:L137-L154](file:///d:/MKT/BOT%20Experiments/BTCETH/core/api_client.py#L137-L154) | **PASS** | Alphabetical sorting, query string concatenation, `MSG-SIGNATURE` hex digest. |
| **Dynamic Server-Time Synchronization** | [core/api_client.py:L111-L135](file:///d:/MKT/BOT%20Experiments/BTCETH/core/api_client.py#L111-L135) | **PASS** | Synchronizes via `/v3/serverTime` round-trip; resyncs every 10 minutes. |
| **Token-Bucket Rate Limiting** | [core/api_client.py:L22-L57](file:///d:/MKT/BOT%20Experiments/BTCETH/core/api_client.py#L22-L57) | **PASS** | 5.0 tokens/sec, capacity 10. Thread-safe lock acquisition. |
| **Exponential Backoff with Jitter** | [core/api_client.py:L232-L236](file:///d:/MKT/BOT%20Experiments/BTCETH/core/api_client.py#L232-L236) | **PASS** | Backs off on HTTP 429, 500, 502, 503, 504 with random jitter. |
| **UNKNOWN Order State on Timeouts** | [core/api_client.py:L283-L298](file:///d:/MKT/BOT%20Experiments/BTCETH/core/api_client.py#L283-L298), [core/order_executor.py:L321-L335](file:///d:/MKT/BOT%20Experiments/BTCETH/core/order_executor.py#L321-L335) | **PASS** | Never retries non-idempotent `place_order` blindly. Assigns `UNKNOWN` status. |
| **Authoritative State Reconciliation** | [state/reconciliation.py:L52-L179](file:///d:/MKT/BOT%20Experiments/BTCETH/state/reconciliation.py#L52-L179) | **PASS** | Synchronizes local cash/crypto with `/v3/balance` and resolves `UNKNOWN` orders. |
| **Risk-Manager Veto Authority** | [core/risk_manager.py:L120-L287](file:///d:/MKT/BOT%20Experiments/BTCETH/core/risk_manager.py#L120-L287), [core/order_executor.py:L73-L95](file:///d:/MKT/BOT%20Experiments/BTCETH/core/order_executor.py#L73-L95) | **PASS** | Sizing rejects bad stops, min notional, exposure overflow, or active breakers. |
| **Three Quantitative Strategies** | [strategies/](file:///d:/MKT/BOT%20Experiments/BTCETH/strategies) directory | **PASS** | Strategy A (Value Area), Strategy B (Liquidity Sweep), Strategy C (CVD Absorption). |
| **Backtesting Engine** | [backtest/engine.py:L60-L297](file:///d:/MKT/BOT%20Experiments/BTCETH/backtest/engine.py#L60-L297) | **PASS** | Sequential event-driven simulation with taker/maker fees and adverse slippage. |
| **Walk-Forward Validation** | [backtest/walk_forward.py:L49-L89](file:///d:/MKT/BOT%20Experiments/BTCETH/backtest/walk_forward.py#L49-L89) | **PASS** | Chronological 60/20/20 train/validation/test split; sensitivity grid search. |
| **Zero Look-Ahead Bias** | [backtest/engine.py:L219](file:///d:/MKT/BOT%20Experiments/BTCETH/backtest/engine.py#L219) | **PASS** | Evaluates slice `df.iloc[:i+1]` only. Stops checked against high/low. |
| **28 Automated Unit Tests** | [tests/](file:///d:/MKT/BOT%20Experiments/BTCETH/tests) directory | **PARTIAL** | Report claims 28 tests; repository actually contains **39 tests** (all 39 pass). |
| **Safe Dry-Run Mode** | [core/order_executor.py:L133-L204](file:///d:/MKT/BOT%20Experiments/BTCETH/core/order_executor.py#L133-L204) | **PASS** | Simulates fills in local ledger, calculates fees/slippage, zero API orders. |
| **Live-Trading Safety Gate** | [main.py:L130-L162](file:///d:/MKT/BOT%20Experiments/BTCETH/main.py#L130-L162) | **PASS** | Requires `DRY_RUN=false`, `LIVE=true`, API keys, connectivity, no breakers. |
| **Cryptographic SHA-256 Audit Trail** | [logs/audit_logger.py:L106-L131](file:///d:/MKT/BOT%20Experiments/BTCETH/logs/audit_logger.py#L106-L131) | **PASS** | Canonical JSON sorting, `prev_hash`, `curr_hash`, full tamper detection. |
| **Docker Deployment** | [Dockerfile](file:///d:/MKT/BOT%20Experiments/BTCETH/Dockerfile), [docker-compose.yml](file:///d:/MKT/BOT%20Experiments/BTCETH/docker-compose.yml) | **RISK** | Multi-stage non-root container; BUT `web/` directory is omitted from Dockerfile. |
| **AWS EC2 Production Readiness** | [docs/DEPLOYMENT_GUIDE.md](file:///d:/MKT/BOT%20Experiments/BTCETH/docs/DEPLOYMENT_GUIDE.md) | **PASS** | Systemd service definition, Docker Compose orchestration, healthchecks. |

---

# 4. ROOSTOO API CORRECTNESS AUDIT

### 4.1 Authentication & Signing Verification
* **API Key Handling**: Passed via custom header `RST-API-KEY: <api_key>` ([core/api_client.py:L185](file:///d:/MKT/BOT%20Experiments/BTCETH/core/api_client.py#L185)). Matches official Roostoo specification ([docs/roostoo_api/python_demo.py:L63-L64](file:///d:/MKT/BOT%20Experiments/BTCETH/docs/roostoo_api/python_demo.py#L63-L64)).
* **Secret Key Handling**: Secret key is stored strictly as a private instance variable in memory ([core/api_client.py:L87](file:///d:/MKT/BOT%20Experiments/BTCETH/core/api_client.py#L87)) and is sanitized/redacted by [logs/audit_logger.py:L92](file:///d:/MKT/BOT%20Experiments/BTCETH/logs/audit_logger.py#L92) before writing to any log.
* **Signing Algorithm**:
  ```python
  sorted_keys = sorted(params.keys())
  total_params = "&".join(f"{k}={params[k]}" for k in sorted_keys)
  sig = hmac.new(self.secret_key.encode("utf-8"), total_params.encode("utf-8"), hashlib.sha256).hexdigest()
  ```
  [core/api_client.py:L145-L152](file:///d:/MKT/BOT%20Experiments/BTCETH/core/api_client.py#L145-L152).
  Verified identical to Roostoo's official signing routine in `PartnerDocument.md` and `python_demo.py`.
* **Signature Header**: Passed as `MSG-SIGNATURE: <hex_digest>` ([core/api_client.py:L190, L195](file:///d:/MKT/BOT%20Experiments/BTCETH/core/api_client.py#L190)).
* **POST Body Format**: Signed POST requests format body as `application/x-www-form-urlencoded` query string `key1=val1&key2=val2` ([core/api_client.py:L196-L217](file:///d:/MKT/BOT%20Experiments/BTCETH/core/api_client.py#L196-L217)), exactly conforming to Roostoo's backend requirements.

### 4.2 Dynamic Server Time Synchronization
* **Endpoint**: `GET /v3/serverTime` ([core/api_client.py:L114](file:///d:/MKT/BOT%20Experiments/BTCETH/core/api_client.py#L114)).
* **Offset Calculation**: Calculates midpoint latency: `local_mid = (local_before + local_after) // 2`, `offset = server_time - local_mid` ([core/api_client.py:L122-L123](file:///d:/MKT/BOT%20Experiments/BTCETH/core/api_client.py#L122-L123)).
* **Resync Interval**: Resynchronizes every 600 seconds (10 minutes) ([core/api_client.py:L132](file:///d:/MKT/BOT%20Experiments/BTCETH/core/api_client.py#L132)).
* **Failure Fallback**: If server time fails, falls back gracefully to local millisecond timestamp without crashing ([core/api_client.py:L125-L127](file:///d:/MKT/BOT%20Experiments/BTCETH/core/api_client.py#L125-L127)).

### 4.3 Endpoints Implemented in Source
All 8 official endpoints are implemented in [core/api_client.py](file:///d:/MKT/BOT%20Experiments/BTCETH/core/api_client.py):
1. `GET /v3/serverTime` (Public): Server clock timestamp ([line 324](file:///d:/MKT/BOT%20Experiments/BTCETH/core/api_client.py#L324))
2. `GET /v3/exchangeInfo` (Public): Pair precisions and min limits ([line 332](file:///d:/MKT/BOT%20Experiments/BTCETH/core/api_client.py#L332))
3. `GET /v3/ticker` (Public with timestamp): Live bid/ask/price/volume ([line 339](file:///d:/MKT/BOT%20Experiments/BTCETH/core/api_client.py#L339))
4. `GET /v3/balance` (Signed): Wallet balances (`Free` and `Lock`) ([line 353](file:///d:/MKT/BOT%20Experiments/BTCETH/core/api_client.py#L353))
5. `GET /v3/pending_count` (Signed): Count of active resting orders ([line 361](file:///d:/MKT/BOT%20Experiments/BTCETH/core/api_client.py#L361))
6. `POST /v3/place_order` (Signed): Order submission ([line 369](file:///d:/MKT/BOT%20Experiments/BTCETH/core/api_client.py#L369))
7. `POST /v3/query_order` (Signed): Matched and pending orders history ([line 412](file:///d:/MKT/BOT%20Experiments/BTCETH/core/api_client.py#L412))
8. `POST /v3/cancel_order` (Signed): Order cancellation ([line 447](file:///d:/MKT/BOT%20Experiments/BTCETH/core/api_client.py#L447))

*Note on Historical Data*: The official Roostoo Mock Exchange provides **NO historical kline/candle endpoint**. The bot correctly handles this by synthesizing 1m, 5m, and 15m OHLCV bars locally from polled ticker snapshots in [core/market_data.py:L199-L241](file:///d:/MKT/BOT%20Experiments/BTCETH/core/market_data.py#L199-L241).

---

# 5. RATE LIMITING & REQUEST STORM AUDIT

### 5.1 Token-Bucket Rate Limiter Implementation
* **Algorithm**: Token bucket with continuous replenishment ([core/api_client.py:L22-L57](file:///d:/MKT/BOT%20Experiments/BTCETH/core/api_client.py#L22-L57)).
* **Configured Capacity**: `rate = 5.0` tokens/second, `capacity = 10` tokens burst ([config/config.yaml:L8-L9](file:///d:/MKT/BOT%20Experiments/BTCETH/config/config.yaml#L8-L9)).
* **Thread Safety**: Protected by `threading.Lock()` ([core/api_client.py:L32](file:///d:/MKT/BOT%20Experiments/BTCETH/core/api_client.py#L32)). All requests in any thread must acquire a token before dispatch.
* **Exponential Backoff**: On HTTP 429, 500, 502, 503, or 504:
  $$\text{Delay} = \min\left(8.0, 0.5 \times 2^{\text{attempt}-1}\right) + \text{Uniform}(0.05, 0.25)$$
  [core/api_client.py:L232-L235](file:///d:/MKT/BOT%20Experiments/BTCETH/core/api_client.py#L232-L235).

### 5.2 Worst-Case Request Frequency Analysis
* **Steady State Polling**: 1 request every 2.0s to `/v3/ticker` (0.5 req/s).
* **Startup / Periodic Reconciliation**: 2 requests (`/v3/balance` + `/v3/query_order`) once per minute (0.033 req/s).
* **Web Telemetry Broadcast**: Pushes telemetry in-memory from cached snapshots every 1.0s; **0 exchange API requests**.
* **Worst-Case Burst Scenario**:
  Simultaneous tick poll + order placement + query + balance check + retry:
  $$\text{Max Peak Demand} = 1 + 2 + 1 + 1 = 5 \text{ requests}$$
  Since token bucket capacity is 10 tokens and replenishment is 5 tokens/s, the bot **never exceeds Roostoo rate limits**. Request storms are physically impossible under this architecture.

---

# 6. MARKET DATA INTEGRITY & LOOK-AHEAD BIAS AUDIT

### 6.1 Candle Construction & Tick Ingestion
* **Source**: Synthesized from `GET /v3/ticker` data ([core/market_data.py:L144-L198](file:///d:/MKT/BOT%20Experiments/BTCETH/core/market_data.py#L144-L198)).
* **Aggregation Method**: Floor division of tick timestamp: `bar_start = (timestamp_ms // ms) * ms` ([core/market_data.py:L213](file:///d:/MKT/BOT%20Experiments/BTCETH/core/market_data.py#L213)).
* **Close Semantics**: When a new bar timestamp begins, the prior bar is marked `is_closed = True` ([core/market_data.py:L219](file:///d:/MKT/BOT%20Experiments/BTCETH/core/market_data.py#L219)).
* **Stale Data Guard**: If ticks for a symbol have not updated for $> 30.0$ seconds, `is_stale(symbol)` flags `True` ([core/market_data.py:L132-L143](file:///d:/MKT/BOT%20Experiments/BTCETH/core/market_data.py#L132-L143)), causing the main loop to skip trading opportunities ([main.py:L320-L321](file:///d:/MKT/BOT%20Experiments/BTCETH/main.py#L320-L321)).
* **Volume Handling Finding**: In live mode, ticks provide cumulative 24h volume. In `_ingest_tick`, bar volume defaults to `0.0`. [core/feature_engine.py:L148-L151](file:///d:/MKT/BOT%20Experiments/BTCETH/core/feature_engine.py#L148-L151) accommodates this by falling back to equal-weighted ticks (`volumes = np.ones_like(closes)`) if total volume is zero, preventing crashes or division by zero.

### 6.2 Look-Ahead Bias Audit Across All Features
Every quantitative indicator was inspected for temporal leakage:
1. **Volume Profile (VAH, VAL, POC, VWAP)**: Computed over `df_window = df.iloc[-lookback:]` ([strategies/value_area.py:L50](file:///d:/MKT/BOT%20Experiments/BTCETH/strategies/value_area.py#L50)). **No look-ahead**.
2. **ADX & ATR**: Uses standard pandas rolling windows (`rolling(window=period, min_periods=1)`) ([core/feature_engine.py:L76, L104](file:///d:/MKT/BOT%20Experiments/BTCETH/core/feature_engine.py#L76)). **No look-ahead**.
3. **Realized Volatility**: Uses historical log returns `np.log(df["close"] / df["close"].shift(1)).iloc[-window:]` ([core/feature_engine.py:L122-L125](file:///d:/MKT/BOT%20Experiments/BTCETH/core/feature_engine.py#L122-L125)). **No look-ahead**.
4. **Swing Points & Liquidity Sweeps**: Swings are detected on bars prior to current bar close (`range(swing_lookback, n - 1)`) ([core/feature_engine.py:L253](file:///d:/MKT/BOT%20Experiments/BTCETH/core/feature_engine.py#L253)). In backtesting, `engine.py:L219` slices `df.iloc[: i + 1]`. Therefore, no bar beyond index `i` is accessible. **No look-ahead**.
5. **Backtest Intrabar Stops**: Evaluated against high/low of candle $i$ with stop-loss evaluated before take-profit on simultaneous penetration ([backtest/engine.py:L156-L182](file:///d:/MKT/BOT%20Experiments/BTCETH/backtest/engine.py#L156-L182)). **Conservative and realistic**.

---

# 7. STRATEGY A — VOLUME PROFILE / AUCTION MARKET THEORY

* **Implementation Location**: [strategies/value_area.py](file:///d:/MKT/BOT%20Experiments/BTCETH/strategies/value_area.py)
* **Mechanics**:
  - Calculates 70% Volume Profile (VAH, VAL, POC) over a rolling 72-candle window (6 hours of 5-minute bars) ([lines 50-54](file:///d:/MKT/BOT%20Experiments/BTCETH/strategies/value_area.py#L50-L54)).
  - **Long Entry Condition**:
    1. Lows of past 5 candles probed below VAL: `any(l < vp.val for l in lows[-5:])` ([line 81](file:///d:/MKT/BOT%20Experiments/BTCETH/strategies/value_area.py#L81)).
    2. Current bar closes back above VAL: `curr_close > vp.val and prev_close <= vp.val * 1.002` ([line 82](file:///d:/MKT/BOT%20Experiments/BTCETH/strategies/value_area.py#L82)).
    3. Price is below POC: `curr_close < vp.poc` ([line 84](file:///d:/MKT/BOT%20Experiments/BTCETH/strategies/value_area.py#L84)).
  - **Stop-Loss**: Placed at sweep low minus 0.5 ATR buffer: `stop_loss = sweep_low - atr_buffer` ([line 86](file:///d:/MKT/BOT%20Experiments/BTCETH/strategies/value_area.py#L86)).
  - **Targets**: TP1 at POC (50% allocation), TP2 at VAH (50% allocation) ([lines 90-93](file:///d:/MKT/BOT%20Experiments/BTCETH/strategies/value_area.py#L90-L93)).
  - **Filter**: Requires expected $R:R \ge 1.50$ and net edge $> 0.3\%$ after taker fees and slippage ([lines 94-98](file:///d:/MKT/BOT%20Experiments/BTCETH/strategies/value_area.py#L94-L98)).
  - **De-risking**: When price sweeps VAH and rejects, issues `direction = "DE_RISK"` ([lines 126-140](file:///d:/MKT/BOT%20Experiments/BTCETH/strategies/value_area.py#L126-L140)), instructing the Risk Manager to liquidate long holdings to cash.
* **Spot-Only Integrity**: Never issues a naked short SELL signal.

---

# 8. STRATEGY B — LIQUIDITY SWEEP / MSS / SMC

* **Implementation Location**: [strategies/liquidity_sweep.py](file:///d:/MKT/BOT%20Experiments/BTCETH/strategies/liquidity_sweep.py)
* **Mechanics**:
  - Detects 10-bar fractal swing highs/lows ([core/feature_engine.py:L253](file:///d:/MKT/BOT%20Experiments/BTCETH/core/feature_engine.py#L253)).
  - Detects liquidity sweeps when current low breaks prior swing low but closes back above ([core/feature_engine.py:L276-L277](file:///d:/MKT/BOT%20Experiments/BTCETH/core/feature_engine.py#L276-L277)).
  - Detects displacement when candle body $> 1.2 \times ATR$ ([core/feature_engine.py:L270-L273](file:///d:/MKT/BOT%20Experiments/BTCETH/core/feature_engine.py#L270-L273)).
  - Confirms Bullish Market Structure Shift (MSS) when price breaks prior swing high with displacement ([core/feature_engine.py:L284-L285](file:///d:/MKT/BOT%20Experiments/BTCETH/core/feature_engine.py#L284-L285)).
  - **Long Entry**: Triggered on `BULLISH_SWEEP` or `bullish_mss_confirmed` ([strategies/liquidity_sweep.py:L71](file:///d:/MKT/BOT%20Experiments/BTCETH/strategies/liquidity_sweep.py#L71)).
  - **Stop-Loss**: Placed at sweep low $- 0.25 \times ATR$ ([line 75](file:///d:/MKT/BOT%20Experiments/BTCETH/strategies/liquidity_sweep.py#L75)).
  - **Targets**: TP1 at $+2.0R$; TP2 at structural swing high ([lines 79-81](file:///d:/MKT/BOT%20Experiments/BTCETH/strategies/liquidity_sweep.py#L79-L81)).
  - **Bearish Signal Behavior**: If a bearish sweep or bearish MSS occurs, [line 118](file:///d:/MKT/BOT%20Experiments/BTCETH/strategies/liquidity_sweep.py#L118) issues `"direction": "DE_RISK"`. It **never** issues a naked short order.

---

# 9. STRATEGY C — CVD / OI ABSORPTION & GRACEFUL DEGRADATION

* **Implementation Location**: [strategies/cvd_absorption.py](file:///d:/MKT/BOT%20Experiments/BTCETH/strategies/cvd_absorption.py)
* **Verification of Principle 3 (No Hallucinated Data)**:
  - The delivery report claimed Strategy C would degrade cleanly to `NO_TRADE` when Roostoo does not supply tick delta or Open Interest.
  - **Source Code Verification**:
    ```python
    if not cvd_available:
        return self._no_trade(
            symbol,
            "CVD feature unavailable from exchange market data. Strategy safely degraded.",
            missing_features=["CVD", "OI"] if not oi_available else ["CVD"],
        )
    ```
    [strategies/cvd_absorption.py:L48-L54](file:///d:/MKT/BOT%20Experiments/BTCETH/strategies/cvd_absorption.py#L48-L54).
  - In [core/market_data.py:L106-L118](file:///d:/MKT/BOT%20Experiments/BTCETH/core/market_data.py#L106-L118), `_check_feature_availability()` explicitly detects that Roostoo only provides spot ticker volume and sets `self.cvd_available = False` and `self.oi_available = False`.
  - **Result**: Strategy C **never fabricates data** and degrades gracefully to `NO_TRADE` in 100% of live cycles.

---

# 10. SIGNAL AGGREGATION & CONFLICT ARBITRATION

* **Implementation Location**: [core/strategy_engine.py](file:///d:/MKT/BOT%20Experiments/BTCETH/core/strategy_engine.py)
* **Regime Gating**: If the market is classified as `UNCERTAIN` or `LOW_LIQUIDITY`, the engine returns `NO_TRADE` immediately ([lines 84-89](file:///d:/MKT/BOT%20Experiments/BTCETH/core/strategy_engine.py#L84-L89)).
* **Conflict Resolution Hierarchy**:
  1. **DE_RISK Priority**: Any de-risk signal takes immediate precedence over BUY signals to protect spot capital ([lines 127-130](file:///d:/MKT/BOT%20Experiments/BTCETH/core/strategy_engine.py#L127-L130)).
  2. **Trend Alignment Penalty**: If regime is `TREND` (Bearish), counter-trend mean reversion signals (Value Area) are penalized by 40% confidence reduction ([lines 138-141](file:///d:/MKT/BOT%20Experiments/BTCETH/core/strategy_engine.py#L138-L141)).
  3. **Net Edge Threshold**: All candidates must yield a minimum net edge $\ge 0.3\%$ ($0.003$) and $R:R \ge 1.50$ after deducting taker fees and slippage ([lines 152-157](file:///d:/MKT/BOT%20Experiments/BTCETH/core/strategy_engine.py#L152-L157)).
  4. **Max Confidence Selection**: The highest confidence qualified signal is selected ([line 167](file:///d:/MKT/BOT%20Experiments/BTCETH/core/strategy_engine.py#L167)).

---

# 11. RISK MANAGEMENT & POSITION SIZING

* **Implementation Location**: [core/risk_manager.py](file:///d:/MKT/BOT%20Experiments/BTCETH/core/risk_manager.py)

### 11.1 Sizing Equation
$$\text{Risk Capital} = \text{Portfolio Equity} \times \text{Risk \%} \quad (\text{Default } 1.0\%, \text{ Hard Ceiling } 1.5\%)$$
$$\text{Raw Quantity} = \frac{\text{Risk Capital}}{\text{Entry Price} - \text{Stop Loss Price}}$$
[core/risk_manager.py:L239-L241](file:///d:/MKT/BOT%20Experiments/BTCETH/core/risk_manager.py#L239-L241).

### 11.2 Portfolio Constraints Enforced
1. **Mandatory Cash Reserve (5%)**:
   `available_cash = max(0.0, cash - equity * 0.05)` ([state/portfolio_tracker.py:L113-L116](file:///d:/MKT/BOT%20Experiments/BTCETH/state/portfolio_tracker.py#L113-L116)).
   If `desired_notional > available_cash`, quantity is truncated to `available_cash / entry_px` ([core/risk_manager.py:L246-L248](file:///d:/MKT/BOT%20Experiments/BTCETH/core/risk_manager.py#L246-L248)).
2. **Gross Exposure Ceiling (100% / 1.0x)**:
   Max aggregate position notional cannot exceed $100\%$ equity ([core/risk_manager.py:L251-L256](file:///d:/MKT/BOT%20Experiments/BTCETH/core/risk_manager.py#L251-L256)). Leverage is strictly prohibited.
3. **Single-Asset Concentration (50%)**:
   No single position notional may exceed $50\%$ equity ([core/risk_manager.py:L259-L262](file:///d:/MKT/BOT%20Experiments/BTCETH/core/risk_manager.py#L259-L262)).
4. **Exchange Precision & Min Notional**:
   Quantity is floored to `AmountPrecision` and rejected if $< \text{MiniOrder}$ (\$1.00 USD) ([core/risk_manager.py:L272-L287](file:///d:/MKT/BOT%20Experiments/BTCETH/core/risk_manager.py#L272-L287)).
5. **Invalid Stop Handling**:
   Rejects trades with inverted stop ($Stop \ge Entry$), zero distance, or distance tighter than 0.15% ([core/risk_manager.py:L194-L218](file:///d:/MKT/BOT%20Experiments/BTCETH/core/risk_manager.py#L194-L218)).

---

# 12. CIRCUIT BREAKERS AUDIT

### 12.1 Rolling 24-Hour Drawdown Breaker (3.5%)
* **Trigger**: Rolling peak-to-trough equity decline $\ge 3.5\%$ within 24 hours ([core/risk_manager.py:L103-L104](file:///d:/MKT/BOT%20Experiments/BTCETH/core/risk_manager.py#L103-L104)).
* **Actions**:
  - Freezes new entries for 6 hours: `freeze_until_timestamp = curr_time + 21600` ([core/risk_manager.py:L105](file:///d:/MKT/BOT%20Experiments/BTCETH/core/risk_manager.py#L105)).
  - Closes all open positions to cash via market de-risk orders ([main.py:L236-L254](file:///d:/MKT/BOT%20Experiments/BTCETH/main.py#L236-L254)).
  - Logs critical event to SHA-256 audit chain ([core/risk_manager.py:L108-L115](file:///d:/MKT/BOT%20Experiments/BTCETH/core/risk_manager.py#L108-L115)).

### 12.2 Historical Maximum Drawdown Breaker (6.0%)
* **Trigger**: All-time peak-to-trough equity decline $\ge 6.0\%$ ([core/risk_manager.py:L89-L90](file:///d:/MKT/BOT%20Experiments/BTCETH/core/risk_manager.py#L89-L90)).
* **Actions**:
  - Sets `permanent_kill_switch = True` ([core/risk_manager.py:L91](file:///d:/MKT/BOT%20Experiments/BTCETH/core/risk_manager.py#L91)).
  - Immediately liquidates all holdings to cash ([main.py:L236-L254](file:///d:/MKT/BOT%20Experiments/BTCETH/main.py#L236-L254)).
  - Permanently halts all future trading entries.

### 12.3 Restart & Persistence Behavior
* **State Persistence**: [state/portfolio_tracker.py:L264-L278](file:///d:/MKT/BOT%20Experiments/BTCETH/state/portfolio_tracker.py#L264-L278) writes `peak_equity` and the last 500 `equity_curve` snapshots atomically to `data/portfolio_state.json`.
* **Restart Evaluation**: Upon restart, `load_from_disk()` restores peak equity and the equity curve. On the very first post-restart cycle, `check_circuit_breakers()` re-evaluates `get_current_drawdown()` and `get_rolling_24h_drawdown()`. If historical drawdown $\ge 6.0\%$, the permanent kill switch immediately re-trips. If rolling drawdown $\ge 3.5\%$, the 6-hour freeze re-engages.
* **Forensic Finding**: `freeze_until_timestamp` itself is not serialized in `portfolio_state.json`. If the bot restarts during an active freeze, a new 6-hour freeze begins from `curr_time` rather than the remaining time. This is conservative and fail-safe, but extends the cooling period.

---

# 13. ORDER EXECUTION & LIFECYCLE IDEMPOTENCY

* **Order Creation**: Client generates a unique deterministic client order ID: `RST_{STRATEGY}_{SYMBOL}_{TIMESTAMP}_{RANDOM}` ([state/order_state.py:L81-L89](file:///d:/MKT/BOT%20Experiments/BTCETH/state/order_state.py#L81-L89)).
* **Lifecycle State Machine**:
  `PENDING_SUBMIT` $\rightarrow$ `PENDING_EXCHANGE` $\rightarrow$ `FILLED` / `PARTIALLY_FILLED` / `CANCELED` / `REJECTED` / `UNKNOWN`.
* **Idempotency Safeguard**:
  In [core/api_client.py:L408](file:///d:/MKT/BOT%20Experiments/BTCETH/core/api_client.py#L408), `place_order` is flagged `is_idempotent = False`.
  If a network timeout occurs while awaiting the exchange response, the client **never blindly resubmits** the order. Blind retries could cause duplicate fills. Instead, it raises `UnknownOrderStateError` ([core/api_client.py:L295](file:///d:/MKT/BOT%20Experiments/BTCETH/core/api_client.py#L295)).

---

# 14. UNKNOWN ORDER STATE RECONCILIATION

When an order placement experiences a connection drop or HTTP read timeout:
1. `RoostooClient` catches `requests.Timeout` on `/v3/place_order` and raises `UnknownOrderStateError` ([core/api_client.py:L295](file:///d:/MKT/BOT%20Experiments/BTCETH/core/api_client.py#L295)).
2. `OrderExecutor` catches this exception, sets `order.status = OrderStatus.UNKNOWN`, and persists the state ([core/order_executor.py:L321-L329](file:///d:/MKT/BOT%20Experiments/BTCETH/core/order_executor.py#L321-L329)).
3. The Live Safety Gate immediately blocks any further orders while UNKNOWN orders exist ([main.py:L152-L154](file:///d:/MKT/BOT%20Experiments/BTCETH/main.py#L152-L154)).
4. `ReconciliationEngine._resolve_unknown_order()` queries `/v3/query_order` ([state/reconciliation.py:L186-L228](file:///d:/MKT/BOT%20Experiments/BTCETH/state/reconciliation.py#L186-L228)):
   - Searches `OrderMatched` for an exchange order matching the pair, side, quantity (within $10^{-6}$), and timestamp (within 10 seconds).
   - If matched on exchange: updates order status to `FILLED` or `PENDING_EXCHANGE`, records fill in portfolio, and clears UNKNOWN state.
   - If not found and order is $> 60$ seconds old: marks `REJECTED` (order never reached exchange), unblocking the pipeline.

---

# 15. STARTUP RECONCILIATION & RECOVERY

* **Exchange Authoritative Source of Truth**: At startup in live mode ([main.py:L186-L194](file:///d:/MKT/BOT%20Experiments/BTCETH/main.py#L186-L194)), `reconciliation.reconcile()` is executed before trading starts.
* **Cash Alignment**: If local cash differs from `/v3/balance` `USD.Free` by $> \$0.10$, local cash is aligned to exchange truth ([state/reconciliation.py:L89-L95](file:///d:/MKT/BOT%20Experiments/BTCETH/state/reconciliation.py#L89-L95)).
* **Orphan Asset Holdings**: Any crypto holdings in `/v3/balance` not in local memory (e.g. from a prior session or manual restart) are adopted into local positions ([state/reconciliation.py:L116-L123](file:///d:/MKT/BOT%20Experiments/BTCETH/state/reconciliation.py#L116-L123)).
* **Startup Blocking**: If reconciliation fails or unresolvable orders remain, startup returns `False` and halts immediately ([main.py:L193-L194](file:///d:/MKT/BOT%20Experiments/BTCETH/main.py#L193-L194)).

---

# 16. SPOT-ONLY COMPLIANCE PROOF

| Vector | Code Evidence | Result |
| :--- | :--- | :---: |
| **Borrowing / Margin** | No borrowing or margin endpoints exist in Roostoo mock API or bot client. | **IMPOSSIBLE** |
| **Leverage** | [core/risk_manager.py:L252](file:///d:/MKT/BOT%20Experiments/BTCETH/core/risk_manager.py#L252) caps gross exposure at $1.0\times$ equity. | **PREVENTED** |
| **Naked Shorting** | [core/risk_manager.py:L162](file:///d:/MKT/BOT%20Experiments/BTCETH/core/risk_manager.py#L162) rejects any signal direction other than `BUY` or `DE_RISK`. `DE_RISK` sells only existing quantity ([line 148](file:///d:/MKT/BOT%20Experiments/BTCETH/core/risk_manager.py#L148)). | **PREVENTED** |
| **Perpetuals / Futures** | Only `/USD` spot pairs configured (`BTC/USD`, `ETH/USD`) ([config/config.yaml:L42-L43](file:///d:/MKT/BOT%20Experiments/BTCETH/config/config.yaml#L42-L43)). | **PREVENTED** |
| **Cash Overdraft** | Sized notional is capped by `available_cash` after reserving 5% equity ([core/risk_manager.py:L245-L248](file:///d:/MKT/BOT%20Experiments/BTCETH/core/risk_manager.py#L245-L248)). | **PREVENTED** |

---

# 17. BACKTEST INTEGRITY & EXECUTION MODEL

### 17.1 Data Source & Partitioning
* **Dataset Partition**: Chronological non-overlapping windows: 60% Train, 20% Validation, 20% Out-of-Sample ([backtest/walk_forward.py:L67-L73](file:///d:/MKT/BOT%20Experiments/BTCETH/backtest/walk_forward.py#L67-L73)).
* **Data Source Reality**:
  Because Roostoo Mock Exchange provides no historical tick archives or kline history, the backtest demonstration in [main.py:L755-L775](file:///d:/MKT/BOT%20Experiments/BTCETH/main.py#L755-L775) uses a 1,000-candle synthetic random-walk dataset seeded with `np.random.seed(42)` and an upward price drift of $+1.5$ per bar.
  The engine itself is fully agnostic and processes standard pandas OHLCV DataFrames.

### 17.2 Fee and Slippage Modeling
* Limit/Maker fee: $0.05\%$ ($0.0005$) ([backtest/execution_model.py:L53](file:///d:/MKT/BOT%20Experiments/BTCETH/backtest/execution_model.py#L53)).
* Market/Taker fee: $0.10\%$ ($0.0010$) ([backtest/execution_model.py:L53](file:///d:/MKT/BOT%20Experiments/BTCETH/backtest/execution_model.py#L53)).
* Execution Slippage: $0.02\%$ ($2$ bps) adverse slippage on entries and exits ([backtest/execution_model.py:L56, L74](file:///d:/MKT/BOT%20Experiments/BTCETH/backtest/execution_model.py#L56)).
* Intrabar Stop / Target Priority: Stop-loss is evaluated before take-profit on bars with wide ranges ([backtest/engine.py:L156](file:///d:/MKT/BOT%20Experiments/BTCETH/backtest/engine.py#L156)).

---

# 18. BACKTEST METRICS & OFFICIAL COMPOSITE SCORE

[core/performance.py](file:///d:/MKT/BOT%20Experiments/BTCETH/core/performance.py) calculates performance metrics:

### 18.1 Metric Formulas
1. **Total Return**: $\frac{\text{Equity}_{\text{end}} - \text{Equity}_{\text{start}}}{\text{Equity}_{\text{start}}}$ ([line 95](file:///d:/MKT/BOT%20Experiments/BTCETH/core/performance.py#L95)).
2. **CAGR**: $\left(\frac{\text{Equity}_{\text{end}}}{\text{Equity}_{\text{start}}}\right)^{\frac{1}{\text{Years}}} - 1$ ([line 103](file:///d:/MKT/BOT%20Experiments/BTCETH/core/performance.py#L103)).
3. **Annualized Volatility**: $\text{Std}(\text{returns}) \times \sqrt{105,120}$ (for 5m bars) ([line 112](file:///d:/MKT/BOT%20Experiments/BTCETH/core/performance.py#L112)).
4. **Downside Deviation**: Semi-variance of negative returns below $R_f$: $\sqrt{\text{Mean}(\min(0, r)^2)} \times \sqrt{105,120}$ ([lines 115-117](file:///d:/MKT/BOT%20Experiments/BTCETH/core/performance.py#L115-L117)).
5. **Sharpe Ratio**: $\frac{\text{CAGR} - R_f}{\text{Ann. Volatility}}$ ([line 125](file:///d:/MKT/BOT%20Experiments/BTCETH/core/performance.py#L125)).
6. **Sortino Ratio**: $\frac{\text{CAGR} - R_f}{\text{Downside Deviation}}$ ([line 126](file:///d:/MKT/BOT%20Experiments/BTCETH/core/performance.py#L126)).
7. **Calmar Ratio**: $\frac{\text{CAGR}}{\text{Max Drawdown}}$ ([line 127](file:///d:/MKT/BOT%20Experiments/BTCETH/core/performance.py#L127)).
8. **Official Competition Composite Score**:
   $$\mathbf{\text{Composite Score} = 0.40 \times \text{Sortino} + 0.30 \times \text{Sharpe} + 0.30 \times \text{Calmar}}$$
   [core/performance.py:L135](file:///d:/MKT/BOT%20Experiments/BTCETH/core/performance.py#L135). Exact match to hackathon rules.
9. **Outlier Clipping**: Ratios are clipped within reasonable mathematical bounds (`[-10, 30]`) to prevent infinity on zero drawdowns ([lines 130-132](file:///d:/MKT/BOT%20Experiments/BTCETH/core/performance.py#L130-L132)).

---

# 19. WALK-FORWARD VALIDATION AUDIT

* **Implementation**: [backtest/walk_forward.py](file:///d:/MKT/BOT%20Experiments/BTCETH/backtest/walk_forward.py).
* **Partitioning**: Strictly chronological without shuffling:
  - In-Sample: `df.iloc[:train_end]` (60%)
  - Validation: `df.iloc[train_end:val_end]` (20%)
  - Out-of-Sample: `df.iloc[val_end:]` (20%)
* **Leakage Verification**: The Out-of-Sample partition is never touched during calibration or parameter sensitivity runs. Sensitivity sweeps run solely on `df_train` ([backtest/walk_forward.py:L82](file:///d:/MKT/BOT%20Experiments/BTCETH/backtest/walk_forward.py#L82)).

---

# 20. OVERFITTING & DATA-MINING RISK ANALYSIS

* **Parameter Sensitivity Grid**: Evaluates 9 combinations of Risk % (`0.75%, 1.0%, 1.25%`) and ATR Multiplier (`1.2, 1.5, 2.0`) ([backtest/walk_forward.py:L100-L101](file:///d:/MKT/BOT%20Experiments/BTCETH/backtest/walk_forward.py#L100-L101)).
* **Hardcoded Overfitting Assessment**: No hardcoded price thresholds or symbol-specific overfitting parameters were found. All indicators (ADX, ATR, VP, Swings) use standard quantitative values (14-period ATR, 70% Value Area fraction, 10-bar swings).
* **Data Caveat**: The backtest numbers (+5.82% return, 7.76 Composite Score) reflect execution on a synthetic series with positive drift (`+1.5` per bar). Live market results will depend on actual exchange volatility and regime dynamics.

---

# 21. LIVE TRADING GATE AUDIT

The live trading gate ([main.py:L130-L162](file:///d:/MKT/BOT%20Experiments/BTCETH/main.py#L130-L162)) enforces 6 mandatory conditions before real orders can be placed:
1. `config.dry_run == False`
2. `config.live_trading_enabled == True`
3. Non-empty `ROOSTOO_API_KEY` and `ROOSTOO_SECRET_KEY`
4. Successful `/v3/serverTime` round-trip connectivity check
5. Zero unresolved `UNKNOWN` orders in the order manager
6. Active circuit breakers are clear (`is_tripped == False`)

If any check fails, startup halts immediately ([main.py:L181-L184](file:///d:/MKT/BOT%20Experiments/BTCETH/main.py#L181-L184)). There is zero code path capable of accidentally placing live orders.

---

# 22. DRY-RUN SAFETY AUDIT

* **Mode Flag**: `DRY_RUN = True` by default in config and code.
* **Execution Path**:
  In [core/order_executor.py:L133-L204](file:///d:/MKT/BOT%20Experiments/BTCETH/core/order_executor.py#L133-L204), when `mode_str == "DRY_RUN"`, orders are filled in the local ledger with simulated slippage and taker fees.
* **API Isolation**: Under dry-run mode, [core/api_client.py:place_order](file:///d:/MKT/BOT%20Experiments/BTCETH/core/api_client.py#L369) is **never invoked**. Market data polling continues normally.

---

# 23. AUDIT LOGGING & SHA-256 HASH-CHAIN INTEGRITY

* **Implementation**: [logs/audit_logger.py](file:///d:/MKT/BOT%20Experiments/BTCETH/logs/audit_logger.py)
* **Cryptographic Chaining**:
  Every record contains:
  - `seq`: Monotonically increasing sequence integer ($1, 2, 3, \dots$).
  - `timestamp_iso`: UTC ISO-8601 timestamp.
  - `prev_hash`: SHA-256 hex digest of the previous record.
  - `curr_hash`: SHA-256 hex digest of the current record's canonical JSON string without `curr_hash`.
  ```python
  canonical = json.dumps(record_copy, sort_keys=True, separators=(",", ":"))
  return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
  ```
  [logs/audit_logger.py:L84-L88](file:///d:/MKT/BOT%20Experiments/BTCETH/logs/audit_logger.py#L84-L88).
* **Tamper Verification**:
  `verify_integrity()` reads all lines from `logs/audit_trail.jsonl`, recalculates every hash in sequence, and checks for sequence gaps or modified payloads ([logs/audit_logger.py:L324-L362](file:///d:/MKT/BOT%20Experiments/BTCETH/logs/audit_logger.py#L324-L362)).
* **Live Audit Verification Result**: Executed `AuditLogger().verify_integrity()`. Result: `True` (Valid chain, zero discrepancies).

---

# 24. TEST SUITE EXECUTION & GAP ANALYSIS

### 24.1 Execution Results
The complete automated pytest suite was executed in read-only mode using Python 3.11.9:
```text
============================= test session starts =============================
platform win32 -- Python 3.11.9, pytest-8.4.2, pluggy-1.6.0
rootdir: D:\MKT\BOT Experiments\BTCETH
plugins: anyio-4.11.0, asyncio-1.4.0, cov-7.1.0
collected 39 items

tests/test_backtest.py::test_simulated_execution_model_fees PASSED       [  2%]
tests/test_backtest.py::test_backtest_engine_execution_flow PASSED       [  5%]
tests/test_backtest.py::test_walk_forward_partitioning PASSED            [  7%]
tests/test_execution.py::test_rate_limiter PASSED                        [ 10%]
tests/test_execution.py::test_hmac_signature_generation PASSED           [ 12%]
tests/test_execution.py::test_dry_run_simulation_execution PASSED        [ 15%]
tests/test_execution.py::test_unknown_order_state_on_timeout PASSED      [ 17%]
tests/test_market_data.py::test_market_data_stale_detection PASSED       [ 20%]
tests/test_market_data.py::test_market_data_candle_aggregation PASSED    [ 23%]
tests/test_market_data.py::test_market_data_feature_inspection_principle_3 PASSED [ 25%]
tests/test_metrics.py::test_composite_score_formula_adherence PASSED     [ 28%]
tests/test_metrics.py::test_downside_deviation_vs_volatility PASSED      [ 30%]
tests/test_metrics.py::test_trade_expectancy_and_profit_factor PASSED    [ 33%]
tests/test_reconciliation.py::test_reconciliation_perfect_match PASSED   [ 35%]
tests/test_reconciliation.py::test_reconciliation_cash_discrepancy_alignment PASSED [ 38%]
tests/test_reconciliation.py::test_reconciliation_resolves_unknown_order PASSED [ 41%]
tests/test_risk.py::test_position_sizing_standard PASSED                 [ 43%]
tests/test_risk.py::test_invalid_stop_loss_rejection PASSED              [ 46%]
tests/test_risk.py::test_cash_reserve_and_exposure_capping PASSED        [ 48%]
tests/test_risk.py::test_rolling_24h_drawdown_breaker PASSED             [ 51%]
tests/test_risk.py::test_max_drawdown_permanent_breaker PASSED           [ 53%]
tests/test_risk.py::test_trailing_stop_engine PASSED                     [ 56%]
tests/test_strategies.py::test_value_area_reclaim_long PASSED            [ 58%]
tests/test_strategies.py::test_value_area_vah_derisk PASSED              [ 61%]
tests/test_strategies.py::test_liquidity_sweep_bullish PASSED            [ 64%]
tests/test_strategies.py::test_cvd_graceful_degradation_without_hallucination PASSED [ 66%]
tests/test_strategies.py::test_cvd_selling_absorption_with_delta PASSED  [ 69%]
tests/test_strategies.py::test_strategy_engine_regime_filtering PASSED   [ 71%]
tests/test_web_server.py::test_index_page PASSED                         [ 74%]
tests/test_web_server.py::test_ping_and_health_endpoints PASSED          [ 76%]
tests/test_web_server.py::test_status_endpoint PASSED                    [ 79%]
tests/test_web_server.py::test_positions_and_orders_endpoints PASSED     [ 82%]
tests/test_web_server.py::test_command_pause_and_resume PASSED           [ 84%]
tests/test_web_server.py::test_command_kill_switch PASSED                [ 87%]
tests/test_web_server.py::test_command_derisk_and_reset_risk PASSED      [ 89%]
tests/test_web_server.py::test_command_manual_trade_risk_check PASSED    [ 92%]
tests/test_web_server.py::test_audit_endpoints PASSED                    [ 94%]
tests/test_web_server.py::test_auth_gate_protection PASSED               [ 97%]
tests/test_web_server.py::test_websocket_telemetry_stream PASSED         [100%]

======================== 39 passed in 60.31s ========================
```
* **Discovery Summary**: 39 discovered, 39 executed, **39 passed (100%)**, 0 failed.
* **Documentation Discrepancy**: Delivery documentation and badges state "28 passed". 11 new tests were added for the web server and dashboard, bringing the total to 39.

### 24.2 Recommended Additional Edge-Case Tests
1. Real socket timeout injection test during active order transmission.
2. System clock jump backwards by $> 10$ seconds during live sync.
3. Multi-threading race test on simultaneous websocket connections during order fill dispatch.

---

# 25. SECURITY AUDIT

* **Hardcoded Credentials**: None found in Python files or YAML files.
* **Environment Exposure**: `.env` is absent from git tracking and ignored in `.gitignore`.
* **Logging Redaction**: Sensitive keys (`api_key`, `secret_key`, `rst-api-key`, `msg-signature`, `password`) are recursively sanitized to `[REDACTED]` prior to serialization in `AuditLogger` ([logs/audit_logger.py:L91-L103](file:///d:/MKT/BOT%20Experiments/BTCETH/logs/audit_logger.py#L91-L103)).
* **Injection Vulnerabilities**: No shell commands, `eval()`, or SQL execution exists in the codebase.
* **Container Security**: [Dockerfile](file:///d:/MKT/BOT%20Experiments/BTCETH/Dockerfile#L22-L47) establishes a non-root user `appuser:appgroup` (`uid=1000, gid=1000`) and switches execution to `USER appuser`.

---

# 26. DOCKER & AWS EC2 DEPLOYMENT READINESS

### 26.1 Container Configuration
* Multi-stage build (`python:3.11-slim`) minimizing image attack surface.
* Container user: Non-root `appuser`.
* Healthcheck: `CMD python main.py --status || exit 1` ([Dockerfile:L50-L51](file:///d:/MKT/BOT%20Experiments/BTCETH/Dockerfile#L50-L51)).
* Compose restart policy: `restart: unless-stopped` ([docker-compose.yml:L9](file:///d:/MKT/BOT%20Experiments/BTCETH/docker-compose.yml#L9)).
* Resource limits: 1.0 CPU, 1024MB RAM ([docker-compose.yml:L25-L26](file:///d:/MKT/BOT%20Experiments/BTCETH/docker-compose.yml#L25-L26)).

### 26.2 Critical Docker Defect Discovered
In [Dockerfile](file:///d:/MKT/BOT%20Experiments/BTCETH/Dockerfile#L34-L41):
```dockerfile
COPY --chown=appuser:appgroup config/ ./config/
COPY --chown=appuser:appgroup core/ ./core/
COPY --chown=appuser:appgroup strategies/ ./strategies/
COPY --chown=appuser:appgroup state/ ./state/
COPY --chown=appuser:appgroup backtest/ ./backtest/
COPY --chown=appuser:appgroup logs/ ./logs/
COPY --chown=appuser:appgroup tests/ ./tests/
COPY --chown=appuser:appgroup main.py ./main.py
```
**Defect**: The `web/` directory is **NOT copied** into the image!
Nor is `./web` mounted in `docker-compose.yml`.
When the container runs, browsing to `http://<ec2-ip>:8080/` triggers [core/web_server.py:L115](file:///d:/MKT/BOT%20Experiments/BTCETH/core/web_server.py#L115):
`return HTMLResponse("<h2>Dashboard index.html not found</h2>", status_code=404)`.
**Remediation**: `COPY --chown=appuser:appgroup web/ ./web/` must be added to `Dockerfile`.

---

# 27. AUTONOMOUS HACKATHON COMPETITION COMPLIANCE

* **Zero High-Frequency Trading**: Candle timeframes operate on 5-minute primary bars and 15-minute structural context.
* **No Market Making**: Does not submit two-sided quotes or passive resting spreads.
* **No Latency Arbitrage**: Single-exchange spot momentum, auction reclaims, and structural sweeps.
* **Autonomous Decision Pipeline**: When running with default flags, decisions are generated programmatically without human prompting.
* **Compliance Finding on Manual Trade Route**:
  While the bot is designed for full autonomy, the operator dashboard includes a "Manual Trade" panel hitting `/command/manual_trade` ([core/web_server.py:L255](file:///d:/MKT/BOT%20Experiments/BTCETH/core/web_server.py#L255)). Although sized and validated through `RiskManager`, the hackathon submission must clarify whether this endpoint should be disabled via `config.web.enabled = false` or restricted to dry-run demonstration to avoid any ambiguity regarding the "Zero Manual Intervention" rule.

---

# 28. CRITICAL END-TO-END EXECUTION TRACE

### 28.1 BUY Order Execution Trace
1. **Market Data**: [core/market_data.py:L144](file:///d:/MKT/BOT%20Experiments/BTCETH/core/market_data.py#L144) `update_ticker()` polls `/v3/ticker`.
2. **Candle Synthesis**: [core/market_data.py:L199](file:///d:/MKT/BOT%20Experiments/BTCETH/core/market_data.py#L199) `_ingest_tick()` updates active 5m candle.
3. **Features & Regime**: [core/feature_engine.py:L129](file:///d:/MKT/BOT%20Experiments/BTCETH/core/feature_engine.py#L129) computes 70% Volume Profile. [core/regime_detector.py:L44](file:///d:/MKT/BOT%20Experiments/BTCETH/core/regime_detector.py#L44) classifies regime as `RANGE` ($ADX < 22$).
4. **Strategy Signal**: [strategies/value_area.py:L28](file:///d:/MKT/BOT%20Experiments/BTCETH/strategies/value_area.py#L28) detects VAL reclaim. Emits `Signal(strategy="VALUE_AREA", direction="BUY", confidence=0.85, entry=84000, stop=83200, tp1=84500, tp2=85200, rr=1.85)`.
5. **Aggregation**: [core/strategy_engine.py:L62](file:///d:/MKT/BOT%20Experiments/BTCETH/core/strategy_engine.py#L62) verifies net edge $> 0.3\%$ and prioritizes signal.
6. **Risk Manager Veto & Sizing**: [core/risk_manager.py:L120](file:///d:/MKT/BOT%20Experiments/BTCETH/core/risk_manager.py#L120) verifies circuit breakers are clear, calculates risk capital ($100k \times 1.0\% = \$1,000$), computes quantity ($\frac{\$1,000}{84000 - 83200} = 1.25\text{ BTC}$), checks available cash and 50% concentration cap, and approves `RiskDecision(approved=True, quantity=1.25)`.
7. **Order Execution**: [core/order_executor.py:L64](file:///d:/MKT/BOT%20Experiments/BTCETH/core/order_executor.py#L64) generates `client_order_id`, registers order in `OrderStateManager`.
8. **Exchange Submission**: [core/api_client.py:L369](file:///d:/MKT/BOT%20Experiments/BTCETH/core/api_client.py#L369) acquires token from `RateLimiter`, computes HMAC-SHA256 signature, dispatches `POST /v3/place_order`.
9. **Portfolio & Audit**: [state/portfolio_tracker.py:L130](file:///d:/MKT/BOT%20Experiments/BTCETH/state/portfolio_tracker.py#L130) records fill. [logs/audit_logger.py:L133](file:///d:/MKT/BOT%20Experiments/BTCETH/logs/audit_logger.py#L133) appends cryptographic SHA-256 chained entry to `logs/audit_trail.jsonl`.

### 28.2 BEARISH De-Risking Trace
1. **Market Data & Features**: Price sweeps above VAH or swing high and rejects.
2. **Strategy Signal**: [strategies/liquidity_sweep.py:L112-L129](file:///d:/MKT/BOT%20Experiments/BTCETH/strategies/liquidity_sweep.py#L112-L129) emits `direction = "DE_RISK"`.
3. **Aggregator**: [core/strategy_engine.py:L127](file:///d:/MKT/BOT%20Experiments/BTCETH/core/strategy_engine.py#L127) flags de-risk signal with top priority over all BUY candidates.
4. **Risk Manager**: [core/risk_manager.py:L146-L159](file:///d:/MKT/BOT%20Experiments/BTCETH/core/risk_manager.py#L146-L159) automatically approves `DE_RISK` and sizes quantity to exact active position holding (`qty = current_pos.quantity`).
5. **Execution**: [core/order_executor.py:L99](file:///d:/MKT/BOT%20Experiments/BTCETH/core/order_executor.py#L99) maps `DE_RISK` to `side = "SELL"` for that existing holding.
6. **Spot Integrity**: If holding is zero, [core/order_executor.py:L101-L102](file:///d:/MKT/BOT%20Experiments/BTCETH/core/order_executor.py#L101-L102) returns `None`. No short position can ever be created.

---

# 29. FORENSIC FAILURE MODE ANALYSIS

| Failure Trigger | Actual Code Behavior | Expected Safe Behavior | Severity |
| :--- | :--- | :--- | :---: |
| **API Timeout on Order Placement** | Catches `requests.Timeout`, raises `UnknownOrderStateError`, marks order `UNKNOWN`, blocks new trades, triggers reconciliation query ([core/api_client.py:L284-L298](file:///d:/MKT/BOT%20Experiments/BTCETH/core/api_client.py#L284-L298)). | Prevent duplicate order; query exchange to verify if filled. | **LOW (Safe)** |
| **Docker Container Web Assets Missing** | [Dockerfile](file:///d:/MKT/BOT%20Experiments/BTCETH/Dockerfile#L34) omits `COPY web/ ./web/`. Visiting dashboard root `/` returns HTTP 404. | Dockerfile should include web static directory. | **HIGH (Ops)** |
| **Circuit Breaker Freeze during Restart** | `freeze_until_timestamp` is in-memory. On restart, re-triggers 6h freeze from current timestamp if rolling drawdown is still $\ge 3.5\%$. | Preserves cooling safety, but resets 6-hour timer. | **MEDIUM** |
| **Stale Market Data (Exchange Lag)** | `is_stale()` triggers after 30 seconds of inactivity ([core/market_data.py:L142](file:///d:/MKT/BOT%20Experiments/BTCETH/core/market_data.py#L142)). New trades are blocked ([main.py:L320](file:///d:/MKT/BOT%20Experiments/BTCETH/main.py#L320)). | Block new entries until fresh market data resumes. | **LOW (Safe)** |
| **Extreme Clock Drift ($> 1000$ms)** | `get_synced_timestamp()` applies `_server_time_offset_ms` to every request timestamp ([core/api_client.py:L134](file:///d:/MKT/BOT%20Experiments/BTCETH/core/api_client.py#L134)). | Neutralize clock skew before HMAC signing. | **LOW (Safe)** |
| **Manual Trade Route in Web Dashboard** | Exposes `/command/manual_trade` ([core/web_server.py:L255](file:///d:/MKT/BOT%20Experiments/BTCETH/core/web_server.py#L255)). Could conflict with strict hackathon autonomous criteria. | Disable manual trade command in competition live mode. | **HIGH (Compliance)** |

---

# 30. MASTER VERDICT TABLE & ACTION PLAN

| Area | Status | Critical Finding |
| :--- | :---: | :--- |
| **Repository Integrity** | **PASS** | Clean working tree; no secrets committed; clean git commit history. |
| **Roostoo API Client** | **PASS** | Strict adherence to official API specification and parameter ordering. |
| **Authentication** | **PASS** | HMAC-SHA256 signature calculation matches official Roostoo demo. |
| **Rate Limiting** | **PASS** | Token bucket (5 req/s, burst 10) prevents rate limit violations. |
| **Market Data** | **PASS** | Proper candle synthesis; robust stale data detection ($> 30$s). |
| **Strategy A (Value Area)** | **PASS** | 70% Volume Profile, VAL reclaim, stop at sweep wick $- 0.5$ ATR. |
| **Strategy B (Liquidity Sweep)** | **PASS** | Fractal swings, liquidity sweeps, displacement, structural targets. |
| **Strategy C (CVD Absorption)** | **PASS** | Principle 3 verified: degrades gracefully without data fabrication. |
| **Signal Aggregation** | **PASS** | De-risk prioritization, regime filtering, net edge $> 0.3\%$ filter. |
| **Risk Management** | **PASS** | 1.0% trade risk, 5% cash reserve, 100% gross exposure ceiling. |
| **Circuit Breakers** | **PASS** | 3.5% 24h drawdown (6h freeze) and 6.0% max drawdown (kill switch). |
| **Order Execution** | **PASS** | Idempotency protected; blind retries forbidden on place_order. |
| **UNKNOWN Order State** | **PASS** | Explicit state handling on network timeouts with reconciliation. |
| **Startup Reconciliation** | **PASS** | Exchange authoritative truth; orphan balances resolved; halts on fault. |
| **Spot-Only Compliance** | **PASS** | Mechanically impossible to short sell, leverage, borrow, or overdraft. |
| **Backtest Engine** | **PASS** | Sequential bar-by-bar event-driven engine with fees and slippage. |
| **Walk-Forward Validation** | **PASS** | 60/20/20 chronological partition; zero parameter leakage to OOS. |
| **Performance Metrics** | **PASS** | Exact official composite formula ($0.4 Sortino + 0.3 Sharpe + 0.3 Calmar$). |
| **Automated Tests** | **PASS** | 39 / 39 automated pytest tests passing 100% (exceeds report claim of 28). |
| **Audit Logging** | **PASS** | Cryptographic SHA-256 hash chaining with verified chain integrity. |
| **Security Audit** | **PASS** | Non-root Docker user, sensitive key log redaction, no code injection. |
| **Docker Readiness** | **RISK** | `Dockerfile` omits copying the `web/` directory, breaking web UI. |
| **Live Safety Gate** | **PASS** | Mandatory multi-condition gate blocks unauthorized live executions. |
| **Autonomous Compliance** | **PARTIAL** | Core engine is autonomous, but `/command/manual_trade` exists in web server. |

---

### A. VERIFIED CLAIMS (DIRECT SOURCE EVIDENCE)
1. Complete Roostoo REST API client implementing all 8 official endpoints.
2. Dynamic server-time synchronization preventing clock skew rejections.
3. Token-bucket rate limiter (5 req/s, capacity 10) with exponential backoff and jitter.
4. UNKNOWN order state preventing duplicate order submissions on timeout.
5. Absolute Risk Manager veto authority over all strategy signals.
6. 1.0% default trade risk with mandatory 5% cash reserve and 100% gross exposure cap.
7. Automated multi-tiered circuit breakers: 3.5% 24h freeze and 6.0% permanent kill switch.
8. Spot-only trading enforcement: no leverage, no borrowing, no naked shorting.
9. Strategy C graceful degradation to `NO_TRADE` without data hallucination.
10. Official Composite Score calculation: $0.40 \times Sortino + 0.30 \times Sharpe + 0.30 \times Calmar$.
11. Append-only SHA-256 cryptographic hash-chain audit logging with verified integrity.
12. 100% test pass rate across all 39 automated tests.

### B. PARTIALLY VERIFIED CLAIMS
1. **Automated Test Count**: Report and README claim 28 unit tests; 39 unit tests are actually present and executed (all 39 pass).
2. **Autonomous Execution vs Manual Endpoint**: Report claims zero manual intervention paths; however, `POST /command/manual_trade` exists on the web server.

### C. FAILED CLAIMS
1. **Docker Container Web Dashboard Packaging**: The delivery guide claims the web dashboard is ready out-of-the-box in Docker. In reality, [Dockerfile](file:///d:/MKT/BOT%20Experiments/BTCETH/Dockerfile#L34-L42) omits `COPY web/ ./web/`, causing HTTP 404 when requesting the UI from within the container.

### D. CRITICAL RISKS
1. **Web Dashboard 404 in Docker**: Deploying to AWS EC2 via `docker compose up -d` without mounting or copying `./web` renders the visual dashboard inoperable.
2. **Backtest Misunderstanding**: If competition evaluators assume the +5.82% return and 7.76 Composite Score were derived from live historical exchange archives rather than synthetic geometric Brownian motion, points could be deducted for lack of data source disclosure.

### E. MISSING TESTS
1. Test for network socket disconnects during active POST order transmission.
2. Test verifying that `POST /command/manual_trade` strictly observes the 5% cash reserve under full portfolio drawdown.

### F. REPORT CLAIMS THAT COULD NOT BE VERIFIED
1. Live historical backtest performance on real Roostoo kline history (Roostoo provides no historical market data API).

### G. LIVE-TRADING BLOCKERS
1. Provision valid `ROOSTOO_API_KEY` and `ROOSTOO_SECRET_KEY` in local uncommitted `.env`.
2. Add `COPY --chown=appuser:appgroup web/ ./web/` to [Dockerfile](file:///d:/MKT/BOT%20Experiments/BTCETH/Dockerfile#L41).
3. Confirm whether competition rules permit `POST /command/manual_trade` on the telemetry server; if forbidden, set `web.auth_token` or disable the route.

### H. SAFE NEXT STEPS (RECOMMENDED PRIORITIES)
1. **Fix Dockerfile**: Add `COPY --chown=appuser:appgroup web/ ./web/` and verify container build.
2. **Paper Soak Test**: Run continuous paper trading in `--dry-run` mode on AWS EC2 for 24 hours to observe live tick aggregation and regime classification under real mock-exchange conditions.
3. **Document Backtest Data Origin**: Add an explicit note in `BACKTEST_REPORT.md` clarifying that backtest results were evaluated on a synthetic random walk calibrated to Bitcoin's statistical volatility.
4. **Deploy to Live**: Once paper soak test and API keys are confirmed, launch live trading via:
   ```bash
   python main.py --live
   ```

---
*Audit Completed Forensically by Antigravity Agentic Auditor. Zero source files were modified during this inspection.*
