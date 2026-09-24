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
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
import numpy as np
import pandas as pd

from backtest.engine import BacktestEngine
from backtest.walk_forward import WalkForwardValidator
from config.trading_params import AppConfig, load_config
from core.api_client import RoostooClient
from core.feature_engine import FeatureEngine
from core.market_data import MarketDataManager
from core.order_executor import OrderExecutor
from core.performance import PerformanceEngine
from core.regime_detector import MarketRegime, RegimeDetector
from core.risk_manager import RiskManager
from core.strategy_engine import Signal, StrategyEngine
from logs.audit_logger import AuditLogger
from state.order_state import OrderStateManager
from state.portfolio_tracker import PortfolioTracker
from state.reconciliation import ReconciliationEngine


class RoostooAutonomousBot:
    """
    Production Autonomous Trading Bot for Roostoo Mock Exchange.
    """

    def __init__(self, config: AppConfig):
        self.config = config
        self.is_running = False

        # 1. Audit Logger with SHA-256 hash chaining
        self.logger = AuditLogger(
            audit_file=config.audit.audit_file,
            api_log_file=config.audit.api_log_file,
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

        # 6. Reconciliation Engine
        self.reconciliation = ReconciliationEngine(
            api_client=self.client,
            portfolio_tracker=self.portfolio,
            order_manager=self.order_manager,
            audit_logger=self.logger,
        )

        # Observability state variables
        self.last_market_update: str = "N/A"
        self.last_api_request: str = "N/A"
        self.active_strategy: str = "N/A"
        self.last_signal: str = "None"
        self.last_order: str = "None"
        self.last_error: str = "None"

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
        self.logger.log_system_event(
            "STARTUP",
            f"Roostoo Autonomous Quant Bot starting up in MODE={mode_str}",
            {"mode": mode_str, "dry_run": self.config.dry_run, "live_enabled": self.config.live_trading_enabled},
        )

        print("\n" + "=" * 70)
        print(f"   ROOSTOO AUTONOMOUS QUANT BOT - STARTUP [MODE = {mode_str}]")
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
        self.is_running = True
        return True

    def run_cycle(self) -> None:
        """
        Execute one complete autonomous decision cycle.
        """
        # 1. Update Market Data
        tickers = self.market_data.update_ticker()
        self.last_market_update = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        self.last_api_request = f"/v3/ticker @ {self.last_market_update}"

        # 2. Update mark prices in portfolio ledger
        mark_prices = {p: snap.last_price for p, snap in tickers.items()}
        self.portfolio.update_mark_prices(mark_prices)

        # 3. Check Circuit Breakers (Section 17)
        is_tripped, breaker_msg = self.risk_manager.check_circuit_breakers()
        if is_tripped:
            self.last_error = f"CIRCUIT_BREAKER: {breaker_msg}"
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

        # 4. Manage Open Positions (Trailing Stops & Hard Stops)
        for sym, pos in list(self.portfolio.positions.items()):
            if pos.quantity <= 0:
                continue

            curr_px = mark_prices.get(sym, 0.0)
            if curr_px <= 0:
                continue

            # Update trailing stop ratchet
            df_5m = self.market_data.get_candle_df(sym, timeframe="5m", limit=30)
            atr_val = 0.0
            if len(df_5m) > 14:
                atr_s = FeatureEngine.calculate_atr(df_5m, period=14)
                atr_val = float(atr_s.iloc[-1])

            self.risk_manager.update_trailing_stop(sym, curr_px, atr=atr_val)

            # Check Hard Stop Trigger
            if curr_px <= pos.stop_loss and pos.stop_loss > 0:
                self.logger.log_system_event("STOP_TRIGGERED", f"{sym} breached stop loss at {pos.stop_loss:.2f}")
                stop_sig = Signal(
                    strategy="RISK_MANAGER",
                    symbol=sym,
                    direction="DE_RISK",
                    confidence=1.0,
                    entry_price=curr_px,
                    stop_loss=0.0,
                    take_profit_1=0.0,
                    take_profit_2=0.0,
                    expected_rr=0.0,
                    reason=f"Stop loss triggered at {curr_px:.2f} <= {pos.stop_loss:.2f}",
                    regime="",
                    timestamp=int(time.time() * 1000),
                )
                risk_dec = self.risk_manager.evaluate_signal(stop_sig)
                self.executor.execute_decision(stop_sig, risk_dec, curr_px)
                self.last_order = f"STOP_EXIT {sym} @ {curr_px:.2f}"
                continue

            # Check Take Profit 2 Trigger
            if curr_px >= pos.take_profit_2 and pos.take_profit_2 > 0:
                tp_sig = Signal(
                    strategy="STRATEGY_ENGINE",
                    symbol=sym,
                    direction="DE_RISK",
                    confidence=1.0,
                    entry_price=curr_px,
                    stop_loss=0.0,
                    take_profit_1=0.0,
                    take_profit_2=0.0,
                    expected_rr=0.0,
                    reason=f"Take Profit 2 target reached at {curr_px:.2f}",
                    regime="",
                    timestamp=int(time.time() * 1000),
                )
                risk_dec = self.risk_manager.evaluate_signal(tp_sig)
                self.executor.execute_decision(tp_sig, risk_dec, curr_px)
                self.last_order = f"TP2_EXIT {sym} @ {curr_px:.2f}"
                continue

        # 5. Evaluate Target Pairs for New Trading Opportunities
        for symbol in self.config.market_data.pairs:
            # Check stale data
            if self.market_data.is_stale(symbol):
                continue

            df_5m = self.market_data.get_candle_df(symbol, timeframe="5m", limit=100)
            if len(df_5m) < 10:
                # If running live with empty buffer, synthesize pseudo candles from ticker
                snap = tickers.get(symbol)
                if snap:
                    self.market_data._ingest_tick(symbol, snap.last_price, snap.coin_volume_24h, snap.server_time)
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
                self.last_signal = f"{signal.direction} {symbol} via {signal.strategy} (conf={signal.confidence:.2f})"
                self.active_strategy = signal.strategy

                # Risk Validation and Sizing
                sym_prec = self.executor.get_symbol_precision(symbol)
                risk_decision = self.risk_manager.evaluate_signal(signal, symbol_precision=sym_prec)

                curr_px = mark_prices.get(symbol, signal.entry_price)
                order = self.executor.execute_decision(signal, risk_decision, curr_px)
                if order:
                    self.last_order = f"{order.side} {symbol} qty={order.quantity} px={order.price:.2f} ({order.status.value})"

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
        self.is_running = False
        print("\n[+] Initiating graceful shutdown...")
        self.portfolio._persist()
        self.order_manager._persist()

        # Audit trail integrity check
        valid, msg, count = self.logger.verify_integrity()
        print(f"[+] Audit Trail Integrity: {msg} (Records: {count})")

        self.logger.log_system_event("SHUTDOWN", f"Graceful shutdown complete. Audit integrity verified: {valid}")
        print("[+] Bot stopped cleanly. State persisted to data/.")


def main():
    parser = argparse.ArgumentParser(description="Roostoo Production Autonomous Quant Trading Bot")
    parser.add_argument("--dry-run", action="store_true", help="Force DRY_RUN mode (default: True)")
    parser.add_argument("--live", action="store_true", help="Enable LIVE trading on Roostoo mock exchange")
    parser.add_argument("--backtest", action="store_true", help="Run historical backtest and walk-forward validation")
    parser.add_argument("--status", action="store_true", help="Run one-time status check and exit")
    parser.add_argument("--config", type=str, default="config/config.yaml", help="Path to custom config.yaml")

    args = parser.parse_args()
    config = load_config(args.config)

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
            bot.run_cycle()
            dashboard_counter += 1

            # Render live dashboard every 2 cycles (approx every 10 seconds)
            if dashboard_counter % 2 == 0:
                bot.render_observability_dashboard()

            time.sleep(poll_interval)
    except KeyboardInterrupt:
        bot.shutdown()


if __name__ == "__main__":
    main()
