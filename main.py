"""
Roostoo Hackathon Autonomous Quant Trading Bot - Main Orchestrator.
Coordinates market data ingestion, quantitative regime detection, multi-strategy signal evaluation,
risk-manager veto & sizing, idempotent execution, portfolio tracking, and real-time observability.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, Tuple
import numpy as np
import pandas as pd

from backtest.engine import BacktestEngine
from backtest.walk_forward import WalkForwardValidator
from config.trading_params import AppConfig, load_config
from core.api_client import RoostooClient
from core.autosl_exit_engine import AutoSLExitEngine, CryptoPosition
from core.feature_engine import FeatureEngine
from core.market_data import MarketDataManager
from core.order_executor import OrderExecutor
from core.performance import PerformanceEngine
from core.regime_detector import MarketRegime, RegimeDetector
from core.risk_manager import RiskManager
from core.strategy_engine import Signal, StrategyEngine
from core.version import get_deployment_metadata, get_git_commit_sha
from logs.audit_logger import AuditLogger
from state.order_state import OrderStateManager
from state.portfolio_tracker import PortfolioTracker
from state.reconciliation import ReconciliationEngine
from core.web_server import WebServer
from core.telegram_notifier import TelegramNotifier


class RoostooAutonomousBot:
    """
    Production Autonomous Trading Bot for Roostoo Mock Exchange.
    """

    def __init__(self, config: AppConfig):
        self.config = config
        self.is_running = False
        self.git_commit = get_git_commit_sha()


        # 1. Audit Logger with SHA-256 hash chaining
        self.logger = AuditLogger(
            audit_file=config.audit.audit_file,
            api_log_file=config.audit.api_log_file,
            trade_log_file=config.audit.trade_log_file,
            enable_hash_chain=config.audit.enable_hash_chain,
        )

        # 2. Exchange API Client
        self.client = RoostooClient(
            api_key=config.api_key,
            secret_key=config.secret_key,
            config=config.exchange,
            audit_logger=self.logger,
        )

        # 3. State Management & Accounting Ledgers
        self.order_manager = OrderStateManager(persistence_file="data/order_state.json")
        self.portfolio = PortfolioTracker(
            initial_capital=config.portfolio.initial_capital,
            min_cash_reserve_pct=config.portfolio.min_cash_reserve_pct,
            persistence_file="data/portfolio_state.json",
        )

        # 4. Market Data Engine
        self.market_data = MarketDataManager(
            api_client=self.client,
            config=config.market_data,
            audit_logger=self.logger,
        )

        # 5. Core Quantitative Engines
        self.regime_detector = RegimeDetector(config=config.regime)
        self.strategy_engine = StrategyEngine(
            config=config.strategies,
            regime_detector=self.regime_detector,
        )
        self.risk_manager = RiskManager(
            portfolio=self.portfolio,
            risk_config=config.risk_controls,
            trailing_config=config.trailing_stop,
            max_risk_per_trade_pct=config.portfolio.max_risk_per_trade_pct,
            max_gross_exposure_pct=config.portfolio.max_gross_exposure_pct,
            min_cash_reserve_pct=config.portfolio.min_cash_reserve_pct,
            max_open_positions=config.portfolio.max_open_positions,
            order_manager=self.order_manager,
            audit_logger=self.logger,
        )
        self.executor = OrderExecutor(
            config=config,
            api_client=self.client,
            portfolio=self.portfolio,
            order_manager=self.order_manager,
            risk_manager=self.risk_manager,
            fees_config=config.fees,
            audit_logger=self.logger,
        )

        # 5b. AutoSL Dynamic Exit Engine
        self.autosl_engine = AutoSLExitEngine(config=config.autosl.to_dict())

        # 6. Reconciliation Engine
        self.reconciliation = ReconciliationEngine(
            api_client=self.client,
            portfolio_tracker=self.portfolio,
            order_manager=self.order_manager,
            audit_logger=self.logger,
            emergency_recovery_sl_pct=getattr(config.risk_controls, "emergency_recovery_sl_pct", 0.02),
        )

        # Signal Deduplication Cache & Periodic Timers (Section 3C, 3D, 8)
        self._processed_signals: set = set()
        self._last_signal_cleanup: float = time.time()
        self._last_periodic_recon: float = time.time()

        # Observability state variables
        self.last_market_update: str = "N/A"
        self.last_api_request: str = "N/A"
        self.active_strategy: str = "N/A"
        self.last_signal: str = "None"
        self.last_order: str = "None"
        self.last_error: str = "None"

        # Web Dashboard & Interactive Controls
        self.paused: bool = False
        self.signal_history: List[Dict[str, Any]] = []
        self.web_server: Optional[WebServer] = None
        if getattr(config, "web", None) and config.web.enabled:
            self.web_server = WebServer(
                bot=self,
                host=config.web.host,
                port=config.web.port,
                auth_token=config.web.auth_token,
            )

        # Telegram Notification Channel
        self.notifier = TelegramNotifier(
            bot_token=getattr(config.telegram, "bot_token", ""),
            chat_id=getattr(config.telegram, "chat_id", ""),
            enabled=getattr(config.telegram, "enabled", False),
        )

    def verify_live_safety_gate(self) -> Tuple[bool, str]:
        """
        Screen 1 & Section 33: Refuses live order placement unless all strict preconditions pass.
        """
        if self.config.dry_run:
            return True, "DRY_RUN mode active - Live safety gate bypassed for paper trading."

        if not self.config.live_trading_enabled:
            return False, "LIVE_TRADING_ENABLED flag is false. Real orders prohibited."

        if not self.config.api_key or not self.config.secret_key:
            return False, "Missing API credentials (ROOSTOO_API_KEY / ROOSTOO_SECRET_KEY)."

        # Test Exchange Connectivity
        try:
            st = self.client.get_server_time()
            if st <= 0:
                return False, "Cannot verify exchange server time."
        except Exception as e:
            return False, f"Exchange connectivity check failed: {e}"

        # Check for unresolved UNKNOWN orders
        unknowns = self.order_manager.get_unknown_orders()
        if unknowns:
            return False, f"Unresolved UNKNOWN orders in ledger ({len(unknowns)}). Must reconcile."

        # Check circuit breakers
        tripped, reason = self.risk_manager.check_circuit_breakers()
        if tripped:
            return False, f"Risk circuit breaker tripped: {reason}"

        return True, "All live safety gates verified and passing."

    def startup(self) -> bool:
        """
        Execute startup sequence, reconciliation, and safety checks.
        """
        mode_str = "LIVE" if self.config.is_live else "DRY_RUN"
        meta = get_deployment_metadata(is_live=self.config.is_live)
        self.git_commit = meta["git_commit"]
        self.logger.log_system_event(
            "STARTUP",
            f"AutoSL Autonomous Quant Bot starting up in MODE={mode_str} (Git SHA: {meta['git_commit']})",
            {
                "mode": mode_str,
                "version": meta["version"],
                "git_commit": meta["git_commit"],
                "environment": meta["environment"],
                "started_at": meta["started_at"],
                "dry_run": self.config.dry_run,
                "live_enabled": self.config.live_trading_enabled,
            },
        )

        print("\n" + "=" * 70)
        print(f"   AUTOSL QUANT TRADING BOT - STARTUP [MODE = {mode_str}]")
        print(f"   Version/Commit : {meta['git_commit']}")
        print(f"   Environment    : {meta['environment']}")
        print(f"   Started        : {meta['started_at']}")
        print("=" * 70)


        # Validate Live Safety Gate
        gate_ok, gate_msg = self.verify_live_safety_gate()
        print(f"Safety Gate Status: {gate_msg}")
        if not gate_ok and not self.config.dry_run:
            self.logger.log_system_event("STARTUP_FAILURE", f"Safety gate rejected live trading: {gate_msg}")
            return False

        # Startup Reconciliation (Section 24)
        if not self.config.dry_run and self.config.api_key:
            print("\n[+] Executing Startup Reconciliation against Roostoo exchange truth...")
            recon_report = self.reconciliation.reconcile()
            print(f"    Reconciliation Success: {recon_report.is_synchronized}")
            for act in recon_report.actions_taken:
                print(f"    - {act}")
            if not recon_report.is_synchronized:
                print("[!] CRITICAL: Reconciliation failed. Halting startup.")
                return False

        # Initial Market Data Fetch
        print("\n[+] Polling initial market tickers...")
        tickers = self.market_data.update_ticker()
        for p, snap in tickers.items():
            print(f"    {p}: Last=${snap.last_price:,.2f} | Bid=${snap.max_bid:,.2f} | Ask=${snap.min_ask:,.2f} | 24h Change={snap.change_24h*100:+.2f}%")

        self.last_market_update = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

        # Bootstrap synthetic historical candles for immediate strategy warmup
        # (Prevents 2.5 hour wait for 30 candle bars to build from scratch)
        for p, snap in tickers.items():
            self.market_data.bootstrap_from_ticker(p, snap)
            candle_count = len(self.market_data.get_candles(p, timeframe="5m"))
            print(f"    [{p}] Bootstrapped candle buffer: {candle_count} bars ready")

        self.is_running = True

        # Write PID file and clear any stale shutdown sentinel
        try:
            os.makedirs("data", exist_ok=True)
            with open("data/bot.pid", "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
            if os.path.exists("data/shutdown.trigger"):
                os.remove("data/shutdown.trigger")
        except Exception as e:
            self.logger.log_system_event("PID_FILE_WARNING", f"Could not record PID: {e}")

        # Launch Web Dashboard Server if enabled
        if self.web_server:
            print(f"\n[+] Starting Web Telemetry Dashboard on http://{self.config.web.host}:{self.config.web.port} ...")
            self.web_server.start()

        # Dispatch Telegram Startup Alert
        if getattr(self, "notifier", None):
            self.notifier.notify_startup(
                mode=mode_str,
                git_commit=self.git_commit,
                equity=self.portfolio.total_equity,
                pairs=self.config.market_data.pairs,
                tickers={p: snap.last_price for p, snap in tickers.items()},
            )

        return True

    def run_cycle(self) -> None:
        """
        Execute one complete autonomous decision cycle.
        """
        # 0. Periodic Reconciliation against exchange truth (Section 8)
        now_ts = time.time()
        recon_interval = getattr(self.config.risk_controls, "reconciliation_interval_seconds", 60.0)
        if not self.config.dry_run and (now_ts - self._last_periodic_recon >= recon_interval):
            self._last_periodic_recon = now_ts
            try:
                recon_report = self.reconciliation.reconcile()
                if not recon_report.is_synchronized:
                    self.logger.log_system_event("PERIODIC_RECON_DESYNC", f"Reconciliation detected desync: {recon_report.actions_taken}")
            except Exception as recon_err:
                self.logger.log_system_event("PERIODIC_RECON_ERROR", f"Periodic reconciliation failed: {recon_err}")

        # 1. Update Market Data
        tickers = self.market_data.update_ticker()
        ist = timezone(timedelta(hours=5, minutes=30))
        self.last_market_update = datetime.now(ist).strftime("%H:%M:%S IST")
        self.last_api_request = f"/v3/ticker @ {self.last_market_update}"

        # 2. Update mark prices in portfolio ledger
        mark_prices = {p: snap.last_price for p, snap in tickers.items()}
        self.portfolio.update_mark_prices(mark_prices)

        # Check if bot is paused
        if self.paused:
            self.last_signal = "BOT PAUSED (Manual Pause Active)"
            return

        # 3. Check Circuit Breakers (Section 17)
        is_tripped, breaker_msg = self.risk_manager.check_circuit_breakers()
        if is_tripped:
            self.last_error = f"CIRCUIT_BREAKER: {breaker_msg}"
            if getattr(self, "notifier", None):
                self.notifier.notify_circuit_breaker(
                    reason=breaker_msg,
                    current_drawdown_pct=self.portfolio.get_current_drawdown(),
                    max_drawdown_pct=self.config.risk_controls.max_drawdown_limit,
                )
            # Liquidate open positions if max drawdown hit
            if self.risk_manager.permanent_kill_switch:
                for sym in list(self.portfolio.positions.keys()):
                    px = mark_prices.get(sym, 0.0)
                    sig_liquidate = Signal(
                        strategy="RISK_MANAGER",
                        symbol=sym,
                        direction="DE_RISK",
                        confidence=1.0,
                        entry_price=px,
                        stop_loss=0.0,
                        take_profit_1=0.0,
                        take_profit_2=0.0,
                        expected_rr=0.0,
                        reason="PERMANENT CIRCUIT BREAKER LIQUIDATION",
                        regime="",
                        timestamp=int(time.time() * 1000),
                    )
                    risk_dec = self.risk_manager.evaluate_signal(sig_liquidate)
                    self.executor.execute_decision(sig_liquidate, risk_dec, px)
            return

        # 4. Manage Open Positions via AutoSL Exit Engine
        # (Phase 1 Validation Window + Phase 2 Dynamic Trailing + Fast Momentum Extension)
        for sym, pos in list(self.portfolio.positions.items()):
            curr_px = mark_prices.get(sym, 0.0)
            if pos.quantity <= 1e-5 or (curr_px > 0 and pos.quantity * curr_px < 2.0) or (getattr(pos, "take_profit_2_filled", False) and pos.quantity <= 1e-4):
                if sym in self.portfolio.positions:
                    del self.portfolio.positions[sym]
                    self.portfolio._persist()
                continue

            if curr_px <= 0:
                continue

            # Fetch candle series for volume and extreme range tracking
            df_5m = self.market_data.get_candle_df(sym, timeframe="5m", limit=30)
            curr_vol = float(df_5m["volume"].iloc[-1]) if len(df_5m) > 0 and "volume" in df_5m.columns else 0.0
            prev_vol = float(df_5m["volume"].iloc[-2]) if len(df_5m) > 1 and "volume" in df_5m.columns else 0.0
            prev_prev_vol = float(df_5m["volume"].iloc[-3]) if len(df_5m) > 2 and "volume" in df_5m.columns else 0.0
            recent_high = float(df_5m["high"].tail(10).max()) if len(df_5m) > 0 and "high" in df_5m.columns else curr_px
            recent_low = float(df_5m["low"].tail(10).min()) if len(df_5m) > 0 and "low" in df_5m.columns else curr_px

            sym_prec = self.executor.get_symbol_precision(sym)
            px_prec = int(sym_prec.get("PricePrecision", 2))
            tick_sz = 10 ** (-px_prec)

            # Convert to AutoSL CryptoPosition
            cp = pos.to_crypto_position()
            prev_broker_sl = cp.broker_sl_price

            # Evaluate tick through AutoSL Exit Engine (with stale data guard)
            is_stale = self.market_data.is_stale(sym)
            exit_reason, trigger_px = self.autosl_engine.on_tick(
                pos=cp,
                current_price=curr_px,
                current_candle_vol=curr_vol,
                prev_completed_vol=prev_vol,
                prev_prev_completed_vol=prev_prev_vol,
                recent_market_high=recent_high,
                recent_market_low=recent_low,
                current_time=datetime.now(timezone.utc),
                tick_size=tick_sz,
                is_stale_data=is_stale,
            )

            # Sync updated state back into portfolio tracker position
            pos.update_from_crypto_position(cp)

            # If broker hard stop was ratcheted, update exchange stop order
            if cp.broker_sl_price != prev_broker_sl:
                self.executor.update_exchange_stop_order(sym, cp.broker_sl_price)

            # Process exit signal if triggered
            if exit_reason:
                trigger_price_val = trigger_px or curr_px
                exit_ratio = 0.5 if exit_reason == "TP1_HIT" else 1.0
                extra_details: Dict[str, Any] = {
                    "exit_reason": exit_reason,
                    "price": trigger_price_val,
                    "exit_ratio": exit_ratio,
                }
                if exit_reason == "FAILED_BREAKOUT_EXIT":
                    full_sl = pos.initial_stop_loss or (pos.entry_price * 0.98 if pos.side == "BUY" else pos.entry_price * 1.02)
                    full_sl_loss = abs((pos.entry_price - full_sl) / pos.entry_price * 100.0) if pos.entry_price > 0 else 2.0
                    actual_loss = abs((pos.entry_price - trigger_price_val) / pos.entry_price * 100.0) if pos.entry_price > 0 else 0.5
                    saved_loss = max(0.0, full_sl_loss - actual_loss)
                    extra_details["saved_loss_pct"] = saved_loss
                    self.logger.log_system_event(
                        "FAILED_BREAKOUT_EXIT",
                        f"[FAILED_BREAKOUT_EXIT] {sym} ({pos.side}): Early invalidation executed at ${trigger_price_val:,.2f}. "
                        f"Loss={actual_loss:.2f}% (Saved ~{saved_loss:.2f}% vs full SL hit).",
                        extra_details,
                    )
                else:
                    self.logger.log_system_event(
                        f"AUTOSL_{exit_reason}",
                        f"[AUTOSL] {sym} ({pos.side}): {exit_reason} triggered at ${trigger_price_val:,.2f}.",
                        extra_details,
                    )

                stop_sig = Signal(
                    strategy="AUTOSL_ENGINE",
                    symbol=sym,
                    direction="DE_RISK",
                    confidence=1.0,
                    entry_price=trigger_price_val,
                    stop_loss=0.0,
                    take_profit_1=0.0,
                    take_profit_2=0.0,
                    expected_rr=0.0,
                    reason=f"[{exit_reason}] {sym} ({pos.side}) triggered at {trigger_price_val:.2f}",
                    regime="",
                    timestamp=int(time.time() * 1000),
                    metadata=extra_details,
                )
                risk_dec = self.risk_manager.evaluate_signal(stop_sig)
                order = self.executor.execute_decision(stop_sig, risk_dec, trigger_price_val)
                self.last_order = f"{exit_reason} {sym} @ {trigger_price_val:.2f}"
                if order and getattr(order, "filled_quantity", 0.0) > 1e-6 and getattr(self, "notifier", None):
                    pnl_usd = (trigger_price_val - pos.entry_price) * pos.quantity if pos.side == "BUY" else (pos.entry_price - trigger_price_val) * pos.quantity
                    pnl_pct = ((trigger_price_val / pos.entry_price) - 1.0) * 100.0 if pos.entry_price > 0 else 0.0
                    if pos.side != "BUY" and trigger_price_val > 0:
                        pnl_pct = ((pos.entry_price / trigger_price_val) - 1.0) * 100.0
                    self.notifier.notify_trade_exit(
                        symbol=sym,
                        side=pos.side,
                        exit_reason=exit_reason,
                        exit_price=trigger_price_val,
                        entry_price=pos.entry_price,
                        quantity=pos.quantity,
                        pnl_usd=pnl_usd,
                        pnl_pct=pnl_pct,
                        saved_loss_pct=extra_details.get("saved_loss_pct"),
                    )
                continue

        # 5. Evaluate Target Pairs for New Trading Opportunities
        for symbol in self.config.market_data.pairs:
            # Check stale data
            if self.market_data.is_stale(symbol):
                continue

            # Check existing position protection (Section 3A)
            if symbol in self.portfolio.positions and self.portfolio.positions[symbol].quantity > 1e-7:
                continue

            # Check pending order / UNKNOWN entry lock (Section 3B)
            if self.order_manager.is_symbol_entry_locked(symbol):
                continue

            # Historical candle warm-up requirement (Section 16)
            # Reduced from 30 to 15 since bootstrapped candles provide initial data
            warmup_req = min(getattr(self.config.risk_controls, 'candle_warmup_candles', 30), 15)
            df_5m = self.market_data.get_candle_df(symbol, timeframe="5m", limit=100)
            if len(df_5m) < warmup_req:
                # Bootstrap if not yet done, or ingest tick if buffer is nearly ready
                snap = tickers.get(symbol)
                if snap:
                    self.market_data.bootstrap_from_ticker(symbol, snap)
                    self.market_data._ingest_tick(symbol, snap.last_price, snap.coin_volume_24h, snap.server_time)
                    # Re-fetch after bootstrap
                    df_5m = self.market_data.get_candle_df(symbol, timeframe="5m", limit=100)
                    if len(df_5m) < warmup_req:
                        continue
                else:
                    continue

            # Strategy Signal Evaluation
            signal = self.strategy_engine.evaluate_symbol(
                symbol=symbol,
                df=df_5m,
                cvd_available=self.market_data.cvd_available,
                oi_available=self.market_data.oi_available,
                taker_fee_pct=self.config.fees.taker_fee_pct,
                slippage_pct=self.config.fees.slippage_pct,
            )

            if signal.direction != "NO_TRADE":
                # Candle-level signal deduplication (Section 3C)
                candle_ts = int(df_5m["timestamp"].iloc[-1]) if "timestamp" in df_5m.columns else int(time.time() / 300) * 300
                sig_key = f"{symbol}_{candle_ts}_{signal.direction}"
                if sig_key in self._processed_signals:
                    continue
                self._processed_signals.add(sig_key)
                if time.time() - self._last_signal_cleanup > 3600:
                    self._processed_signals.clear()
                    self._last_signal_cleanup = time.time()
                self.last_signal = f"{signal.direction} {symbol} via {signal.strategy} (conf={signal.confidence:.2f})"
                self.active_strategy = signal.strategy
                self.signal_history.append({
                    "timestamp": int(time.time() * 1000),
                    "symbol": symbol,
                    "direction": signal.direction,
                    "strategy": signal.strategy,
                    "confidence": signal.confidence,
                    "entry_price": signal.entry_price,
                    "stop_loss": signal.stop_loss,
                    "take_profit_1": signal.take_profit_1,
                    "take_profit_2": signal.take_profit_2,
                    "expected_rr": signal.expected_rr,
                    "reason": signal.reason,
                    "regime": signal.regime,
                })
                if len(self.signal_history) > 100:
                    self.signal_history.pop(0)

                # Risk Validation and Sizing
                sym_prec = self.executor.get_symbol_precision(symbol)
                risk_decision = self.risk_manager.evaluate_signal(signal, symbol_precision=sym_prec)

                curr_px = mark_prices.get(symbol, signal.entry_price)
                order = self.executor.execute_decision(signal, risk_decision, curr_px)
                if order:
                    self.last_order = f"{order.side} {symbol} qty={order.quantity} px={order.price:.2f} ({order.status.value})"
                    if getattr(self, "notifier", None):
                        self.notifier.notify_trade_entry(
                            symbol=symbol,
                            side=order.side,
                            strategy=signal.strategy,
                            price=order.price,
                            quantity=order.quantity,
                            notional_usd=order.quantity * order.price,
                            stop_loss=signal.stop_loss,
                            take_profit=signal.take_profit_1,
                            confidence=signal.confidence,
                        )

    def render_observability_dashboard(self) -> None:
        """
        Section 35: Concise live terminal status display for continuous autonomous operation.
        """
        mode_str = "LIVE" if self.config.is_live else "DRY_RUN"
        eq = self.portfolio.total_equity
        cash = self.portfolio.cash
        pos_count = len([p for p in self.portfolio.positions.values() if p.quantity > 0])
        exp_pct = self.portfolio.gross_exposure_pct * 100.0
        curr_dd = self.portfolio.get_current_drawdown() * 100.0
        roll_dd = self.portfolio.get_rolling_24h_drawdown() * 100.0
        daily_pnl = self.portfolio.realized_pnl + sum(p.unrealized_pnl for p in self.portfolio.positions.values())

        # Calculate live metrics
        metrics = PerformanceEngine.calculate_metrics(
            equity_snapshots=self.portfolio.equity_curve,
            initial_capital=self.config.portfolio.initial_capital,
        )

        dashboard = (
            f"\n"
            f"+----------------------------------------------------------------------+\n"
            f"|              ROOSTOO AUTONOMOUS QUANT BOT STATUS                     |\n"
            f"+----------------------------------------------------------------------+\n"
            f"| MODE:                {mode_str:<47} |\n"
            f"| BOT STATUS:          {'RUNNING AUTONOMOUSLY':<47} |\n"
            f"| LAST MARKET UPDATE:  {self.last_market_update:<47} |\n"
            f"| LAST API REQUEST:    {self.last_api_request:<47} |\n"
            f"| PORTFOLIO EQUITY:    ${eq:>12,.2f} USD {'':<31} |\n"
            f"| AVAILABLE CASH:      ${self.portfolio.available_cash:>12,.2f} USD {'':<31} |\n"
            f"| OPEN POSITIONS:      {pos_count:<47} |\n"
            f"| GROSS EXPOSURE:      {exp_pct:>6.2f}% (Limit: 100%) {'':<22} |\n"
            f"| TOTAL PNL:           ${daily_pnl:>+12,.2f} USD ({metrics.total_return_pct:>+6.2f}%) {'':<19} |\n"
            f"| CURRENT DRAWDOWN:    {curr_dd:>6.2f}% (Limit: 6.0%) {'':<23} |\n"
            f"| ROLLING 24H DD:      {roll_dd:>6.2f}% (Freeze: 3.5%) {'':<22} |\n"
            f"| COMPOSITE SCORE:     {metrics.composite_score:>8.4f} (Sharpe: {metrics.sharpe_ratio:.2f}, Sortino: {metrics.sortino_ratio:.2f}) |\n"
            f"| ACTIVE STRATEGY:     {self.active_strategy:<47} |\n"
            f"| LAST SIGNAL:         {self.last_signal[:47]:<47} |\n"
            f"| LAST ORDER:          {self.last_order[:47]:<47} |\n"
            f"| LAST ERROR:          {self.last_error[:47]:<47} |\n"
            f"+----------------------------------------------------------------------+\n"
        )
        print(dashboard)

    def shutdown(self) -> None:
        """
        Graceful shutdown: preserves state, verifies audit integrity, and logs clean exit.
        """
        if getattr(self, "_is_shutting_down", False):
            return
        self._is_shutting_down = True
        self.is_running = False
        print("\n[+] Initiating graceful shutdown...")

        if self.web_server:
            self.web_server.stop()

        # Send Telegram Shutdown Alert (flushed synchronously before process exit)
        if getattr(self, "notifier", None):
            self.notifier.notify_shutdown(
                reason="Service Stop / Process Terminated",
                equity=self.portfolio.total_equity,
                realized_pnl=self.portfolio.realized_pnl,
                open_positions=len([p for p in self.portfolio.positions.values() if p.quantity > 0]),
                wait_seconds=2.0,
            )

        self.portfolio._persist()
        self.order_manager._persist()

        # Clean up PID and shutdown sentinel
        for fpath in ("data/bot.pid", "data/shutdown.trigger"):
            try:
                if os.path.exists(fpath):
                    os.remove(fpath)
            except Exception:
                pass

        # Audit trail integrity check
        valid, msg, count = self.logger.verify_integrity()
        print(f"[+] Audit Trail Integrity: {msg} (Records: {count})")

        self.logger.log_system_event("SHUTDOWN", f"Graceful shutdown complete. Audit integrity verified: {valid}")
        print("[+] Bot stopped cleanly. State persisted to data/.")

    def toggle_pause(self) -> bool:
        """Toggle pause state for scanning and opening new positions."""
        self.paused = not self.paused
        state_str = "PAUSED" if self.paused else "RESUMED"
        self.logger.log_system_event("OPERATOR_PAUSE_TOGGLE", f"Bot execution toggled to {state_str}", {"paused": self.paused})
        if getattr(self, "notifier", None):
            self.notifier.notify_pause_state(self.paused)
        return self.paused

    def handle_kill_switch(self) -> Dict[str, Any]:
        """Emergency kill switch: activates permanent breaker and de-risks all positions."""
        self.risk_manager.permanent_kill_switch = True
        liquidated = []
        tickers = self.market_data.latest_tickers
        for sym, pos in list(self.portfolio.positions.items()):
            if pos.quantity > 0:
                snap = tickers.get(sym)
                px = snap.last_price if snap else pos.current_price
                sig = Signal(
                    strategy="RISK_MANAGER",
                    symbol=sym,
                    direction="DE_RISK",
                    confidence=1.0,
                    entry_price=px,
                    stop_loss=0.0,
                    take_profit_1=0.0,
                    take_profit_2=0.0,
                    expected_rr=0.0,
                    reason="EMERGENCY WEB DASHBOARD KILL SWITCH",
                    regime="",
                    timestamp=int(time.time() * 1000),
                )
                risk_dec = self.risk_manager.evaluate_signal(sig)
                order = self.executor.execute_decision(sig, risk_dec, px)
                liquidated.append({"symbol": sym, "order": order.to_dict() if order else None})

        self.portfolio._persist()
        self.order_manager._persist()
        self.logger.log_system_event("KILL_SWITCH_ACTIVATED", "Emergency kill switch activated from web dashboard", {"liquidated": liquidated})
        return {"killed": True, "liquidated": liquidated}

    def handle_reconcile(self) -> Dict[str, Any]:
        """Trigger on-demand exchange reconciliation and balance sync."""
        report = self.reconciliation.reconcile()
        return {
            "is_synchronized": report.is_synchronized,
            "actions_taken": report.actions_taken,
            "discrepancies": report.discrepancies,
            "timestamp": report.timestamp,
        }

    def handle_derisk(self, symbol: str = "") -> Dict[str, Any]:
        """Close specific position or all open positions cleanly to cash."""
        liquidated = []
        tickers = self.market_data.latest_tickers
        targets = [symbol] if symbol else list(self.portfolio.positions.keys())
        for sym in targets:
            pos = self.portfolio.positions.get(sym)
            if pos and pos.quantity > 0:
                snap = tickers.get(sym)
                px = snap.last_price if snap else pos.current_price
                sig = Signal(
                    strategy="OPERATOR_OVERRIDE",
                    symbol=sym,
                    direction="DE_RISK",
                    confidence=1.0,
                    entry_price=px,
                    stop_loss=0.0,
                    take_profit_1=0.0,
                    take_profit_2=0.0,
                    expected_rr=0.0,
                    reason="MANUAL DASHBOARD DE-RISK",
                    regime="",
                    timestamp=int(time.time() * 1000),
                )
                risk_dec = self.risk_manager.evaluate_signal(sig)
                order = self.executor.execute_decision(sig, risk_dec, px)
                liquidated.append({"symbol": sym, "order": order.to_dict() if order else None})

        return {"success": True, "liquidated": liquidated}

    def handle_reset_risk(self) -> Dict[str, Any]:
        """Reset rolling 24h freeze timer and cleared breaker flags."""
        self.risk_manager.freeze_until_timestamp = 0.0
        self.risk_manager.permanent_kill_switch = False
        self.logger.log_system_event("RISK_RESET", "Operator manually reset risk circuit breaker freeze via dashboard")
        return {"reset": True, "is_frozen": False}


    def handle_manual_trade(self, symbol: str, side: str, notional_usd: float = 0.0) -> Dict[str, Any]:
        """Evaluate and execute manual trade with strict risk sizing."""
        snap = self.market_data.latest_tickers.get(symbol)
        curr_px = snap.last_price if snap else 0.0
        if curr_px <= 0:
            return {"success": False, "error": f"No ticker price available for {symbol}"}

        direction = "BUY" if side.upper() == "BUY" else "DE_RISK"
        sig = Signal(
            strategy="MANUAL_TRADE",
            symbol=symbol,
            direction=direction,
            confidence=0.85,
            entry_price=curr_px,
            stop_loss=curr_px * 0.98 if direction == "BUY" else 0.0,
            take_profit_1=curr_px * 1.02 if direction == "BUY" else 0.0,
            take_profit_2=curr_px * 1.04 if direction == "BUY" else 0.0,
            expected_rr=2.0,
            reason="OPERATOR MANUAL TRADE VIA DASHBOARD",
            regime="",
            timestamp=int(time.time() * 1000),
        )
        sym_prec = self.executor.get_symbol_precision(symbol)
        risk_decision = self.risk_manager.evaluate_signal(sig, symbol_precision=sym_prec)
        if not risk_decision.approved:
            return {"success": False, "error": f"Risk Manager Veto: {risk_decision.reason}"}

        order = self.executor.execute_decision(sig, risk_decision, curr_px)
        return {"success": True, "order": order.to_dict() if order else None}

    def get_strategy_tracking_data(self) -> List[Dict[str, Any]]:
        """
        Extract real-time strategy tracking data for each trading pair,
        including Volume Profile (VAH/VAL/POC), Liquidity Sweeps, CVD Absorption, and Market Regime.
        """
        tracking_list = []
        tickers = self.market_data.latest_tickers

        for symbol in self.config.market_data.pairs:
            snap = tickers.get(symbol)
            last_px = snap.last_price if snap else 0.0
            bid = snap.max_bid if snap else 0.0
            ask = snap.min_ask if snap else 0.0
            spread = snap.spread if snap else 0.0
            spread_bps = snap.spread_bps if snap else 0.0
            vol_24h = snap.coin_volume_24h if snap else 0.0
            chg_24h = snap.change_24h if snap else 0.0

            df_5m = self.market_data.get_candle_df(symbol, timeframe="5m", limit=100)

            if len(df_5m) >= 15:
                vp = FeatureEngine.calculate_volume_profile(df_5m, volume_fraction=self.config.strategies.value_area.volume_profile_fraction)
                regime_info = self.regime_detector.classify(df_5m, vp=vp)
                vah = float(vp.vah)
                val = float(vp.val)
                poc = float(vp.poc)

                # Session VWAP
                typical_price = (df_5m["high"] + df_5m["low"] + df_5m["close"]) / 3.0
                vol_sum = df_5m["volume"].sum()
                session_vwap = float((typical_price * df_5m["volume"]).sum() / vol_sum) if vol_sum > 0 else last_px

                # Market Structure (Swings, Displacement, FVG)
                ms = FeatureEngine.detect_market_structure(df_5m)
                latest_sh = ms.last_swing_high.price if ms.last_swing_high else (last_px * 1.01)
                latest_sl = ms.last_swing_low.price if ms.last_swing_low else (last_px * 0.99)
                is_displacement = bool(ms.sweep_detected or ms.bullish_mss_confirmed or ms.bearish_mss_confirmed)
                has_fvg = len(ms.active_fvgs) > 0
                latest_delta = 0.0
                latest_cvd = 0.0

                # Strategy evaluations
                fees_pct = self.config.fees.taker_fee_pct + self.config.fees.slippage_pct
                sig_va = self.strategy_engine.strategy_va.evaluate(symbol, df_5m, regime_info, fees_pct=fees_pct)
                sig_ls = self.strategy_engine.strategy_ls.evaluate(symbol, df_5m, regime_info, fees_pct=fees_pct)
                if self.config.strategies.cvd_absorption.enabled:
                    sig_cvd = self.strategy_engine.strategy_cvd.evaluate(
                        symbol, df_5m, regime_info,
                        cvd_available=self.market_data.cvd_available,
                        oi_available=self.market_data.oi_available,
                        fees_pct=fees_pct
                    )
                else:
                    sig_cvd = {"direction": "NO_TRADE", "confidence": 0.0, "reason": "Strategy disabled"}
                chosen_sig = self.strategy_engine.evaluate_symbol(
                    symbol, df_5m,
                    cvd_available=self.market_data.cvd_available,
                    oi_available=self.market_data.oi_available,
                    taker_fee_pct=self.config.fees.taker_fee_pct,
                    slippage_pct=self.config.fees.slippage_pct
                )
                regime_name = regime_info.regime.value
                adx_val = regime_info.adx
                atr_val = regime_info.atr if regime_info.atr > 0 else (float(regime_info.metrics.get("atr_pct", 0.005)) * last_px)
                trend_dir = regime_info.trend_direction
                vol_pctile = regime_info.volatility_percentile
            else:
                vah = last_px * 1.012 if last_px > 0 else 85000.0
                val = last_px * 0.988 if last_px > 0 else 83000.0
                poc = last_px * 1.001 if last_px > 0 else 84100.0
                session_vwap = last_px
                latest_sh = last_px * 1.015 if last_px > 0 else 85500.0
                latest_sl = last_px * 0.985 if last_px > 0 else 82500.0
                is_displacement = False
                has_fvg = False
                latest_delta = 0.0
                latest_cvd = 0.0
                regime_name = "RANGE"
                adx_val = 18.5
                atr_val = last_px * 0.005 if last_px > 0 else 400.0
                trend_dir = "NEUTRAL"
                vol_pctile = 45.0
                sig_va = {"direction": "NO_TRADE", "confidence": 0.0, "reason": "Awaiting candle buffers"}
                sig_ls = {"direction": "NO_TRADE", "confidence": 0.0, "reason": "Awaiting candle buffers"}
                sig_cvd = {"direction": "NO_TRADE", "confidence": 0.0, "reason": "Awaiting candle buffers"}
                chosen_sig = Signal("MULTI_ENGINE", symbol, "NO_TRADE", 0.0, last_px, 0.0, 0.0, 0.0, 0.0, "Buffering data", regime_name, int(time.time()*1000))

            if last_px > vah:
                va_pos = "ABOVE_VAH"
                va_pos_desc = "Above VAH (Premium Zone)"
            elif last_px < val:
                va_pos = "BELOW_VAL"
                va_pos_desc = "Below VAL (Discount Zone)"
            else:
                va_pos = "INSIDE_VA"
                va_pos_desc = "Inside Value Area (Fair Value)"

            tracking_list.append({
                "symbol": symbol,
                "ticker": {
                    "last_price": last_px,
                    "bid": bid,
                    "ask": ask,
                    "spread": spread,
                    "spread_bps": spread_bps,
                    "change_24h_pct": chg_24h * 100.0,
                    "volume_24h": vol_24h,
                },
                "regime": {
                    "name": regime_name,
                    "adx": adx_val,
                    "atr": atr_val,
                    "trend_direction": trend_dir,
                    "volatility_percentile": vol_pctile,
                },
                "value_area": {
                    "vah": vah,
                    "val": val,
                    "poc": poc,
                    "session_vwap": session_vwap,
                    "position": va_pos,
                    "position_desc": va_pos_desc,
                    "width_pct": ((vah - val) / poc * 100.0) if poc > 0 else 0.0,
                    "signal": sig_va.get("direction", "NO_TRADE"),
                    "confidence": sig_va.get("confidence", 0.0),
                    "reason": sig_va.get("reason", ""),
                },
                "liquidity_sweep": {
                    "swing_high": latest_sh,
                    "swing_low": latest_sl,
                    "displacement": is_displacement,
                    "fvg_present": has_fvg,
                    "signal": sig_ls.get("direction", "NO_TRADE"),
                    "confidence": sig_ls.get("confidence", 0.0),
                    "reason": sig_ls.get("reason", ""),
                },
                "cvd_absorption": {
                    "delta_volume": latest_delta,
                    "cvd_value": latest_cvd,
                    "cvd_available": self.market_data.cvd_available,
                    "oi_available": self.market_data.oi_available,
                    "degraded_mode": not self.market_data.cvd_available,
                    "signal": sig_cvd.get("direction", "NO_TRADE"),
                    "confidence": sig_cvd.get("confidence", 0.0),
                    "reason": sig_cvd.get("reason", ""),
                },
                "chosen_signal": {
                    "direction": chosen_sig.direction,
                    "strategy": chosen_sig.strategy,
                    "confidence": chosen_sig.confidence,
                    "entry_price": chosen_sig.entry_price,
                    "stop_loss": chosen_sig.stop_loss,
                    "take_profit_1": chosen_sig.take_profit_1,
                    "take_profit_2": chosen_sig.take_profit_2,
                    "expected_rr": chosen_sig.expected_rr,
                    "reason": chosen_sig.reason,
                },
            })

        return tracking_list


def main():
    parser = argparse.ArgumentParser(description="Roostoo Production Autonomous Quant Trading Bot")
    parser.add_argument("--dry-run", action="store_true", help="Force DRY_RUN mode (default: True)")
    parser.add_argument("--live", action="store_true", help="Enable LIVE trading on Roostoo mock exchange")
    parser.add_argument("--backtest", action="store_true", help="Run historical backtest and walk-forward validation")
    parser.add_argument("--status", action="store_true", help="Run one-time status check and exit")
    parser.add_argument("--config", type=str, default="config/config.yaml", help="Path to custom config.yaml")
    parser.add_argument("--dashboard", action="store_true", default=None, help="Enable web dashboard (default: True)")
    parser.add_argument("--no-dashboard", action="store_true", help="Disable web dashboard")
    parser.add_argument("--port", type=int, default=None, help="Override web dashboard port (e.g. 8080)")

    args = parser.parse_args()
    config = load_config(args.config)

    if args.no_dashboard:
        config.web.enabled = False
    elif args.dashboard:
        config.web.enabled = True
    if args.port:
        config.web.port = args.port

    # Command line flag overrides
    if args.dry_run:
        config.dry_run = True
        config.live_trading_enabled = False
    elif args.live:
        config.dry_run = False
        config.live_trading_enabled = True

    # -------------------------------------------------------------------------
    # Option 1: Backtesting & Walk-Forward Validation
    # -------------------------------------------------------------------------
    if args.backtest:
        print("[+] Running Reproducible Event-Driven Backtesting & Walk-Forward Analysis...")
        # Generate realistic crypto market data or load from historical cache
        n = 1000
        np.random.seed(42)
        t0 = int(time.time() * 1000) - (n * 300000)
        prices = [84000.0]
        for _ in range(n - 1):
            prices.append(prices[-1] + np.random.randn() * 35.0 + 1.5)

        highs = [p + abs(np.random.randn() * 25.0) for p in prices]
        lows = [p - abs(np.random.randn() * 25.0) for p in prices]
        opens = [p - np.random.randn() * 12.0 for p in prices]
        volumes = np.random.exponential(15.0, n)

        df = pd.DataFrame({
            "timestamp": [t0 + i * 300000 for i in range(n)],
            "open": opens,
            "high": highs,
            "low": lows,
            "close": prices,
            "volume": volumes,
        })

        validator = WalkForwardValidator(config)
        report = validator.run_walk_forward(df, symbol="BTC/USD")
        print("\n" + report.summary())
        return

    # -------------------------------------------------------------------------
    # Option 2: Live / Dry-Run Continuous Autonomous Execution
    # -------------------------------------------------------------------------
    bot = RoostooAutonomousBot(config)

    # Signal handlers for graceful exit
    def handle_exit_signal(sig, frame):
        bot.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_exit_signal)
    signal.signal(signal.SIGTERM, handle_exit_signal)

    if not bot.startup():
        print("[!] Startup checks failed. Exiting.")
        sys.exit(1)

    if args.status:
        bot.render_observability_dashboard()
        bot.shutdown()
        return

    poll_interval = config.market_data.poll_interval_seconds
    dashboard_counter = 0

    try:
        while bot.is_running:
            if os.path.exists("data/shutdown.trigger"):
                print("\n[!] Graceful shutdown trigger detected (sentinel file). Exiting...")
                bot.shutdown()
                break

            bot.run_cycle()
            dashboard_counter += 1

            # Render live dashboard every 2 cycles (approx every 10 seconds)
            if dashboard_counter % 2 == 0:
                bot.render_observability_dashboard()

            # Responsive sleep loop checking for shutdown trigger
            sleep_slices = max(1, int(poll_interval / 0.5))
            slice_dur = poll_interval / sleep_slices
            for _ in range(sleep_slices):
                if not bot.is_running or os.path.exists("data/shutdown.trigger"):
                    break
                time.sleep(slice_dur)

    except KeyboardInterrupt:
        bot.shutdown()


if __name__ == "__main__":
    main()
