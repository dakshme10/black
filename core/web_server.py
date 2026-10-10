"""
Web Dashboard Server for Roostoo Autonomous Quant Trading Bot.
Provides high-performance WebSocket real-time telemetry streaming, REST command endpoints,
and serves an institutional-grade dark-themed single-page trading dashboard.
Guided by the AutoSL dashboard architecture.
"""

from __future__ import annotations

import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
import json
import logging
import os
from threading import Thread
import time
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.middleware.base import BaseHTTPMiddleware
import uvicorn

from core.performance import PerformanceEngine
from core.regime_detector import MarketRegime
from core.strategy_engine import Signal

logger = logging.getLogger(__name__)

_WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


class _AuthGate(BaseHTTPMiddleware):
    """
    Bearer-token security gate for the dashboard.
    Bypassed when auth_token is empty. When set, accepts ?token= or Authorization: Bearer.
    """

    def __init__(self, app, token: str):
        super().__init__(app)
        self._token = token

    async def dispatch(self, request: Request, call_next):
        if not self._token:
            return await call_next(request)

        path = request.url.path
        if path in ("/api/ping", "/api/health", "/api/login", "/login"):
            return await call_next(request)

        supplied = request.query_params.get("token", "")
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()

        if supplied and supplied == self._token:
            return await call_next(request)

        if path.startswith("/api/"):
            return JSONResponse(status_code=401, content={"error": "Unauthorized"})

        login_html = (
            "<!doctype html><html><body style='background:#0a0e17;color:#e2e8f0;"
            "font-family:monospace;display:flex;align-items:center;justify-content:center;height:100vh;'>"
            "<div style='background:#0e1422;padding:30px;border-radius:8px;border:1px solid #1e293b;text-align:center;'>"
            "<h2 style='color:#38bdf8;'>ROOSTOO QUANT DASHBOARD</h2>"
            "<p style='color:#94a3b8;font-size:13px;'>Enter dashboard authentication token:</p>"
            "<form onsubmit=\"location.href='/?token='+encodeURIComponent(document.getElementById('t').value);return false\">"
            "<input id='t' type='password' autofocus style='background:#0a0e17;border:1px solid #334155;color:#fff;padding:8px;border-radius:4px;width:240px;'><br><br>"
            "<button style='background:#0284c7;color:#fff;border:none;padding:8px 20px;border-radius:4px;cursor:pointer;'>Unlock</button>"
            "</form></div></body></html>"
        )
        return HTMLResponse(content=login_html, status_code=401)


class WebServer:
    """
    Asynchronous web server running FastAPI with WebSocket broadcasting for real-time telemetry.
    """

    def __init__(self, bot, host: str = "0.0.0.0", port: int = 8080, auth_token: str = ""):
        self.bot = bot
        self.host = host
        self.port = port
        self.auth_token = auth_token
        self.broadcast_interval = getattr(bot.config.web, "broadcast_interval_seconds", 1.0)

        self.app = FastAPI(title="Roostoo Quant Bot Dashboard")
        self.active_connections: List[WebSocket] = []
        self._ws_lock = asyncio.Lock()

        # Dedicated private thread pool for telemetry data serialization so dashboard never stalls bot thread
        self._dash_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="roostoo_dash")
        self._last_payload: Optional[Dict[str, Any]] = None

        if self.auth_token:
            self.app.add_middleware(_AuthGate, token=self.auth_token)

        self._setup_routes()

        self._server_thread: Optional[Thread] = None
        self._server: Optional[uvicorn.Server] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.stop_event = asyncio.Event()

    def _setup_routes(self):
        @self.app.get("/")
        async def index():
            index_path = os.path.join(_WEB_DIR, "index.html")
            if os.path.exists(index_path):
                with open(index_path, "r", encoding="utf-8") as f:
                    return HTMLResponse(f.read())
            return HTMLResponse("<h2>Dashboard index.html not found</h2>", status_code=404)

        @self.app.get("/api/ping")
        async def ping():
            return {"ok": True, "time": datetime.now(timezone.utc).isoformat()}

        @self.app.get("/api/health")
        async def health():
            try:
                st = self.bot.client.get_server_time()
                exchange_ok = st > 0
            except Exception:
                exchange_ok = False

            return {
                "bot_running": self.bot.is_running,
                "paused": getattr(self.bot, "paused", False),
                "mode": "LIVE" if self.bot.config.is_live else "DRY_RUN",
                "git_commit": getattr(self.bot, "git_commit", "unknown"),
                "exchange_connected": exchange_ok,
                "server_time_drift_ms": self.bot.client.time_drift_ms,
                "open_positions": len([p for p in self.bot.portfolio.positions.values() if p.quantity > 0]),
                "audit_verified": self.bot.logger.verify_integrity()[0] if hasattr(self.bot, "logger") else True,
            }

        @self.app.get("/api/status")
        async def get_status():
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(self._dash_executor, self._prepare_data)
            return JSONResponse(data)

        @self.app.get("/api/positions")
        async def get_positions():
            positions = []
            for sym, pos in self.bot.portfolio.positions.items():
                if pos.quantity > 0:
                    positions.append(self._pos_to_dict(pos))
            return JSONResponse(positions)

        @self.app.get("/api/orders")
        async def get_orders():
            orders = self.bot.order_manager.get_history(limit=100)
            return JSONResponse([o.to_dict() for o in orders])

        @self.app.get("/api/trade-log")
        async def get_trade_log():
            orders = self.bot.order_manager.get_history(limit=200)
            fills = [o.to_dict() for o in orders if o.filled_quantity > 0 or o.status.value == "FILLED"]
            return JSONResponse(fills)

        @self.app.get("/api/trade-log.csv")
        async def get_trade_log_csv():
            trade_csv = getattr(getattr(self.bot, "logger", None), "trade_log_file", None)
            if trade_csv and os.path.exists(trade_csv):
                with open(trade_csv, "r", encoding="utf-8") as f:
                    return PlainTextResponse(f.read(), media_type="text/csv")
            return PlainTextResponse(
                "timestamp_ist,timestamp_utc,event,symbol,side,order_type,quantity,price,filled_qty,filled_price,notional_usd,commission,client_order_id,exchange_order_id,status\n",
                media_type="text/csv",
            )

        @self.app.get("/api/logs")
        async def get_logs():
            if hasattr(self.bot, "logger") and hasattr(self.bot.logger, "get_recent_events"):
                return JSONResponse(self.bot.logger.get_recent_events(limit=100))
            return JSONResponse([])

        @self.app.get("/api/audit-trail")
        async def get_audit_trail(limit: int = 50):
            records = []
            audit_file = self.bot.config.audit.audit_file
            if os.path.exists(audit_file):
                with open(audit_file, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                    for line in reversed(lines[-limit:]):
                        try:
                            records.append(json.loads(line.strip()))
                        except Exception:
                            continue
            valid, msg, count = self.bot.logger.verify_integrity()
            return JSONResponse({
                "integrity_valid": valid,
                "integrity_message": msg,
                "total_records": count,
                "records": records,
            })

        @self.app.get("/api/verify-audit")
        async def verify_audit():
            valid, msg, count = self.bot.logger.verify_integrity()
            return {"valid": valid, "message": msg, "count": count}

        @self.app.get("/api/config")
        async def get_config():
            cfg_dict = {
                "exchange": self.bot.config.exchange.name,
                "base_url": self.bot.config.exchange.base_url,
                "dry_run": self.bot.config.dry_run,
                "live_trading_enabled": self.bot.config.live_trading_enabled,
                "pairs": self.bot.config.market_data.pairs,
                "universe": {
                    "requested": getattr(getattr(self.bot, "universe_status", None), "requested_pairs", self.bot.config.market_data.pairs),
                    "active": getattr(getattr(self.bot, "universe_status", None), "active_pairs", self.bot.config.market_data.pairs),
                    "skipped": getattr(getattr(self.bot, "universe_status", None), "skipped_pairs", {}),
                },
                "initial_capital": self.bot.config.portfolio.initial_capital,
                "min_cash_reserve_pct": self.bot.config.portfolio.min_cash_reserve_pct,
                "max_gross_exposure_pct": self.bot.config.portfolio.max_gross_exposure_pct,
                "max_risk_per_trade_pct": self.bot.config.portfolio.max_risk_per_trade_pct,
                "max_open_positions": self.bot.config.portfolio.max_open_positions,
                "circuit_breakers": {
                    "rolling_24h_limit": self.bot.config.risk_controls.rolling_24h_drawdown_limit,
                    "max_drawdown_limit": self.bot.config.risk_controls.max_drawdown_limit,
                    "freeze_hours": self.bot.config.risk_controls.freeze_duration_hours,
                },
                "strategies": {
                    "value_area": self.bot.config.strategies.value_area.enabled,
                    "liquidity_sweep": self.bot.config.strategies.liquidity_sweep.enabled,
                }
            }
            return JSONResponse(cfg_dict)

        @self.app.post("/command/{cmd}")
        async def handle_command(cmd: str, payload: Optional[Dict[str, Any]] = None):
            logger.info(f"Dashboard command received: {cmd} with payload: {payload}")
            try:
                if cmd == "pause":
                    new_state = self.bot.toggle_pause()
                    return {"status": "success", "message": f"Bot paused: {new_state}"}

                elif cmd == "kill":
                    result = self.bot.handle_kill_switch()
                    return {"status": "success", "message": "Emergency Kill Switch Activated", "details": result}

                elif cmd == "reconcile":
                    report = self.bot.handle_reconcile()
                    return {"status": "success", "message": "Reconciliation completed", "details": report}

                elif cmd == "derisk":
                    symbol = (payload or {}).get("symbol", "")
                    result = self.bot.handle_derisk(symbol)
                    return {"status": "success", "message": f"De-risk executed for {symbol or 'ALL'}", "details": result}

                elif cmd == "reset_risk":
                    result = self.bot.handle_reset_risk()
                    return {"status": "success", "message": "Risk freeze reset", "details": result}

                elif cmd == "manual_trade":
                    if (
                        getattr(getattr(self.bot, "config", None), "is_live", False)
                        or getattr(getattr(self.bot, "config", None), "live_trading_enabled", False)
                        or os.getenv("COMPETITION_LIVE", "").lower() in ("true", "1")
                    ):
                        raise HTTPException(
                            status_code=403,
                            detail="MANUAL_TRADE_FORBIDDEN: Manual trade execution is strictly disabled in competition live mode to enforce 100% autonomous execution compliance."
                        )
                    if not payload or "symbol" not in payload or "side" not in payload:
                        raise HTTPException(status_code=400, detail="Missing symbol or side in manual trade")
                    result = self.bot.handle_manual_trade(
                        symbol=payload["symbol"],
                        side=payload["side"],
                        notional_usd=float(payload.get("notional_usd", 0.0)),
                    )
                    return {"status": "success", "message": "Manual trade evaluated & executed", "details": result}

                elif cmd == "shutdown":
                    logger.info("Graceful shutdown command received from web endpoint.")
                    def _do_shutdown():
                        time.sleep(0.3)
                        self.bot.shutdown()
                    Thread(target=_do_shutdown, daemon=True, name="shutdown_worker").start()
                    return {"status": "success", "message": "Graceful shutdown initiated"}

                return {"status": "failed", "message": f"Unknown command: {cmd}"}

            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error handling dashboard command '{cmd}': {e}", exc_info=True)
                return {"status": "failed", "message": str(e)}

        @self.app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket):
            await websocket.accept()
            async with self._ws_lock:
                self.active_connections.append(websocket)
            try:
                loop = asyncio.get_running_loop()
                data = await asyncio.wait_for(
                    loop.run_in_executor(self._dash_executor, self._prepare_data),
                    timeout=3.0,
                )
                self._last_payload = data
            except Exception as e:
                logger.error(f"Error preparing initial WS frame: {e}")
                data = self._last_payload or {}

            try:
                await websocket.send_json(data)
                while True:
                    await websocket.receive_text()
            except WebSocketDisconnect:
                pass
            finally:
                async with self._ws_lock:
                    if websocket in self.active_connections:
                        self.active_connections.remove(websocket)

    def _pos_to_dict(self, pos) -> Dict[str, Any]:
        """Convert a portfolio Position into a rich AutoSL-compatible dictionary."""
        sym = getattr(pos, "symbol", "")
        base = getattr(pos, "base_coin", sym.split("/")[0] if "/" in sym else sym)
        qty = float(getattr(pos, "quantity", 0.0))
        entry_px = float(getattr(pos, "entry_price", 0.0))
        curr_px = float(getattr(pos, "current_price", entry_px))
        unrealized_pnl = float(getattr(pos, "unrealized_pnl", 0.0))
        realized_pnl = float(getattr(pos, "realized_pnl", 0.0))

        cost_basis = entry_px * qty
        pnl_pct = (unrealized_pnl / cost_basis * 100.0) if cost_basis > 0 else 0.0

        sl_val = float(getattr(pos, "stop_loss", 0.0))
        tp1_val = float(getattr(pos, "take_profit_1", 0.0))
        tp2_val = float(getattr(pos, "take_profit_2", 0.0))
        init_sl = float(getattr(pos, "initial_stop_loss", sl_val) or sl_val)

        mom_stat = getattr(pos, "momentum_status", "NORMAL")
        ext_lvl = int(getattr(pos, "extension_level", 0))
        val_surv = bool(getattr(pos, "validation_survived", False))
        locked_pr = float(getattr(pos, "locked_profit", 0.0))
        sl_pct_val = float(getattr(pos, "sl_percent", 2.0))
        candles_cnt = int(getattr(pos, "candles_since_entry", 0))

        if mom_stat.startswith("MOMENTUM_EXTENSION"):
            trail_status = f"Momentum Ext Lvl {ext_lvl} (50% Locked)"
        elif mom_stat == "FINAL_TRAILING":
            trail_status = f"Final Trailing (SL: ${sl_val:.2f})"
        elif val_surv:
            if locked_pr > 0:
                trail_status = f"Phase 2 Validated (+${locked_pr:.2f} Locked)"
            else:
                trail_status = f"Phase 2 Validated (Trailing SL {sl_pct_val:.1f}%)"
        else:
            trail_status = f"Phase 1 Validating (Candle {candles_cnt}/2)"

        is_exiting = bool(getattr(pos, "is_exit_initiated", False) or getattr(pos, "exit_lock", False))
        raw_state = getattr(pos, "exit_state", "OPEN")
        if is_exiting:
            pos_status = "EXITING"
        elif raw_state in ("TP1_PENDING", "EXIT_PENDING", "CLOSED"):
            pos_status = raw_state
        elif val_surv:
            pos_status = "VALIDATED"
        else:
            pos_status = "OPEN"

        notional = getattr(pos, "notional_value", qty * curr_px)

        return {
            "symbol": sym,
            "base_coin": base,
            "side": getattr(pos, "side", "BUY"),
            "quantity": qty,
            "entry_price": entry_px,
            "current_price": curr_px,
            "notional_value": notional,
            "unrealized_pnl": unrealized_pnl,
            "unrealized_pnl_pct": pnl_pct,
            "realized_pnl": realized_pnl,
            "strategy": getattr(pos, "strategy", "MULTI_ENGINE") or "MULTI_ENGINE",
            "stop_loss": sl_val,
            "initial_stop_loss": init_sl,
            "take_profit_1": tp1_val,
            "take_profit_1_hit": bool(getattr(pos, "take_profit_1_hit", False)),
            "take_profit_2": tp2_val,
            "take_profit_2_hit": bool(getattr(pos, "take_profit_2_hit", False)),
            "opened_timestamp": getattr(pos, "opened_timestamp", 0),
            "highest_price": float(getattr(pos, "highest_price", curr_px)),
            "peak_profit_points": float(getattr(pos, "peak_profit_points", 0.0)),
            "trailing_status": trail_status,
            "phase": getattr(pos, "phase", "PHASE_1_VALIDATION"),
            "validation_survived": val_surv,
            "candles_since_entry": candles_cnt,
            "entry_breakout_level": float(getattr(pos, "entry_breakout_level", entry_px)),
            "broker_sl_price": float(getattr(pos, "broker_sl_price", sl_val)),
            "sl_percent": sl_pct_val,
            "locked_profit": locked_pr,
            "momentum_status": mom_stat,
            "extension_level": ext_lvl,
            "volume_drop_detected": bool(getattr(pos, "volume_drop_detected", False)),
            "trailing_sl_active": bool(getattr(pos, "trailing_sl_active", False)),
            "is_exit_initiated": is_exiting,
            "status": pos_status,
        }

    def _prepare_data(self) -> Dict[str, Any]:
        """
        Gathers complete real-time quantitative telemetry snapshot.
        Executed inside self._dash_executor so it never blocks the event loop or bot.
        """
        ist = timezone(timedelta(hours=5, minutes=30))
        now_ist = datetime.now(ist)
        server_time_str = now_ist.strftime("%Y-%m-%d %H:%M:%S IST")

        # 1. Bot State & Configuration
        is_running = getattr(self.bot, "is_running", False)
        paused = getattr(self.bot, "paused", False)
        mode = "LIVE" if self.bot.config.is_live else "DRY_RUN"
        dry_run = self.bot.config.dry_run

        # 2. Portfolio Accounting
        port = self.bot.portfolio
        equity = float(port.total_equity)
        cash = float(port.cash)
        avail_cash = float(port.available_cash)
        min_reserve = float(port.initial_capital * port.min_cash_reserve_pct)
        gross_exp_pct = float(port.gross_exposure_pct * 100.0)
        realized_pnl = float(port.realized_pnl)

        unrealized_pnl = 0.0
        active_positions_list = []
        for sym, pos in port.positions.items():
            unrealized_pnl += pos.unrealized_pnl
            if pos.quantity > 0:
                active_positions_list.append(self._pos_to_dict(pos))

        total_pnl = realized_pnl + unrealized_pnl
        current_dd = float(port.get_current_drawdown() * 100.0)
        rolling_24h_dd = float(port.get_rolling_24h_drawdown() * 100.0)
        is_frozen = bool(getattr(self.bot.risk_manager, "is_frozen", False))
        permanent_kill = getattr(self.bot.risk_manager, "permanent_kill_switch", False)




        # 3. Performance & Official Composite Score
        completed_trades = []
        try:
            if hasattr(self.bot, "order_manager"):
                filled_orders = [o for o in self.bot.order_manager.get_history(limit=500) if getattr(o, "status", None) and o.status.value == "FILLED"]
                for o in filled_orders:
                    if o.side.upper() == "SELL" and o.filled_quantity > 0:
                        completed_trades.append({
                            "pnl": getattr(o, "realized_pnl", 0.0),
                            "fee": getattr(o, "commission", 0.0),
                        })
        except Exception:
            pass

        metrics = PerformanceEngine.calculate_metrics(
            equity_snapshots=port.equity_curve,
            initial_capital=self.bot.config.portfolio.initial_capital,
            completed_trades=completed_trades if completed_trades else None,
            peak_equity=port.peak_equity,
        )

        # 4. Strategy & Microstructure Tracking
        tracking_data = []
        try:
            if hasattr(self.bot, "get_strategy_tracking_data"):
                tracking_data = self.bot.get_strategy_tracking_data()
        except Exception as e:
            logger.error(f"Error getting strategy tracking data: {e}")

        # 5. Orders & Execution History
        orders_list = []
        try:
            orders = self.bot.order_manager.get_history(limit=30)
            orders_list = [o.to_dict() for o in orders]
        except Exception:
            pass

        # 6. Audit Trail Preview & Integrity
        audit_records = []
        audit_valid = True
        audit_msg = "OK"
        audit_count = 0
        try:
            audit_file = self.bot.config.audit.audit_file
            if os.path.exists(audit_file):
                with open(audit_file, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                    for line in reversed(lines[-15:]):
                        try:
                            audit_records.append(json.loads(line.strip()))
                        except Exception:
                            continue
            audit_valid, audit_msg, audit_count = self.bot.logger.verify_integrity()
        except Exception:
            pass

        # 7. Rate Limiter & Connectivity
        tokens_avail = getattr(getattr(self.bot.client, "rate_limiter", None), "tokens", 10.0)
        time_drift = getattr(self.bot.client, "time_drift_ms", 0.0)

        # 8. Equity Curve Data Points for Charting
        equity_points = []
        try:
            snaps = port.equity_curve[-60:]
            for s in snaps:
                equity_points.append({
                    "timestamp": s.timestamp_ms,
                    "equity": s.equity,
                    "cash": s.cash,
                    "gross_exposure": s.gross_exposure,
                })
        except Exception:
            pass

        # 9. Recent Market Ticks
        recent_ticks = []
        try:
            if hasattr(self.bot, "market_data") and hasattr(self.bot.market_data, "recent_ticks"):
                recent_ticks = self.bot.market_data.recent_ticks
        except Exception:
            pass

        return {
            "server_time": server_time_str,
            "status": {
                "is_running": is_running,
                "paused": paused,
                "mode": mode,
                "dry_run": dry_run,
                "live_trading_enabled": self.bot.config.live_trading_enabled,
                "git_commit": getattr(self.bot, "git_commit", "unknown"),
                "last_market_update": self.bot.last_market_update,
                "last_api_request": self.bot.last_api_request,
                "active_strategy": self.bot.active_strategy,
                "last_signal": self.bot.last_signal,
                "last_order": self.bot.last_order,
                "last_error": self.bot.last_error,
            },
            "universe": {
                "requested": getattr(getattr(self.bot, "universe_status", None), "requested_pairs", self.bot.config.market_data.pairs),
                "active": getattr(getattr(self.bot, "universe_status", None), "active_pairs", self.bot.config.market_data.pairs),
                "skipped": getattr(getattr(self.bot, "universe_status", None), "skipped_pairs", {}),
            },
            "portfolio": {
                "equity": equity,
                "cash": cash,
                "available_cash": avail_cash,
                "min_cash_reserve": min_reserve,
                "cash_reserve_pct": self.bot.config.portfolio.min_cash_reserve_pct * 100.0,
                "gross_exposure_pct": gross_exp_pct,
                "gross_exposure_limit": self.bot.config.portfolio.max_gross_exposure_pct * 100.0,
                "realized_pnl": realized_pnl,
                "unrealized_pnl": unrealized_pnl,
                "total_pnl": total_pnl,
                "peak_equity": port.peak_equity,
                "current_drawdown_pct": current_dd,
                "max_drawdown_limit": self.bot.config.risk_controls.max_drawdown_limit * 100.0,
                "rolling_24h_drawdown_pct": rolling_24h_dd,
                "rolling_24h_drawdown_limit": self.bot.config.risk_controls.rolling_24h_drawdown_limit * 100.0,
                "is_frozen": is_frozen,
                "permanent_kill": permanent_kill,
                "risk_state": (
                    self.bot.risk_manager.governor.drawdown_governor.evaluate_state(port.get_current_drawdown())[0].value
                    if hasattr(self.bot.risk_manager, "governor")
                    else "NORMAL"
                ),
                "recovery_mode": (
                    self.bot.risk_manager.governor.drawdown_governor.recovery_mode_active
                    if hasattr(self.bot.risk_manager, "governor")
                    else False
                ),
                "open_position_count": len(active_positions_list),
                "max_positions": self.bot.config.portfolio.max_open_positions,
            },
            "risk_governor": (
                self.bot.risk_manager.get_governor_telemetry()
                if hasattr(self.bot, "risk_manager") and hasattr(self.bot.risk_manager, "get_governor_telemetry")
                else {}
            ),
            "competition": {
                "composite_score": metrics.composite_score,
                "sortino_ratio": metrics.sortino_ratio,
                "sharpe_ratio": metrics.sharpe_ratio,
                "calmar_ratio": metrics.calmar_ratio,
                "total_return_pct": metrics.total_return_pct,
                "max_drawdown_pct": metrics.max_drawdown_pct,
            },
            "positions": active_positions_list,
            "tracking": tracking_data,
            "ticks": recent_ticks,
            "orders": orders_list,
            "audit": {
                "verified": audit_valid,
                "message": audit_msg,
                "total_records": audit_count,
                "records": audit_records,
            },
            "rate_limiter": {
                "tokens": tokens_avail,
                "drift_ms": time_drift,
            },
            "equity_curve": equity_points,
            "signal_history": getattr(self.bot, "signal_history", [])[-20:],
            "logs": self.bot.logger.get_recent_events(limit=50) if hasattr(self.bot, "logger") and hasattr(self.bot.logger, "get_recent_events") else [],
        }

    async def _broadcast_loop(self):
        """
        Background loop pushing telemetry frames over WebSocket.
        """
        loop = asyncio.get_running_loop()
        while not self.stop_event.is_set():
            async with self._ws_lock:
                connections = list(self.active_connections)

            if not connections:
                await asyncio.sleep(self.broadcast_interval)
                continue

            data = self._last_payload
            try:
                data = await asyncio.wait_for(
                    loop.run_in_executor(self._dash_executor, self._prepare_data),
                    timeout=2.0,
                )
                self._last_payload = data
            except Exception as e:
                logger.warning(f"Dashboard prepare_data warning: {e}")

            if data:
                dead_connections = []
                for ws in connections:
                    try:
                        await ws.send_json(data)
                    except Exception:
                        dead_connections.append(ws)

                if dead_connections:
                    async with self._ws_lock:
                        for ws in dead_connections:
                            if ws in self.active_connections:
                                self.active_connections.remove(ws)

            await asyncio.sleep(self.broadcast_interval)

    def start(self):
        """
        Start the web server in a dedicated background daemon thread.
        """
        if self._server_thread is not None and self._server_thread.is_alive():
            logger.warning("WebServer already running.")
            return

        def _run_server():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

            # Schedule the broadcast loop
            self._loop.create_task(self._broadcast_loop())

            config = uvicorn.Config(
                app=self.app,
                host=self.host,
                port=self.port,
                log_level="warning",
                loop="asyncio",
            )
            self._server = uvicorn.Server(config)
            self._loop.run_until_complete(self._server.serve())

        self._server_thread = Thread(target=_run_server, daemon=True, name="roostoo_web_server")
        self._server_thread.start()
        logger.info(f"Dashboard web server started on http://{self.host}:{self.port}")

    def stop(self):
        """
        Stop the web server and release resources.
        """
        self.stop_event.set()
        if self._server:
            self._server.should_exit = True
        self._dash_executor.shutdown(wait=False)
        logger.info("Dashboard web server stopped.")
