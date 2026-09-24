"""
Event-Driven Quantitative Backtesting Engine.
Executes bar-by-bar simulation with strict avoidance of lookahead bias,
realistic maker/taker fees, slippage, trailing stops, and drawdown circuit breakers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import numpy as np
import pandas as pd

from backtest.execution_model import BacktestTrade, SimulatedExecutionModel
from config.trading_params import AppConfig
from core.performance import PerformanceEngine, PerformanceMetrics
from core.risk_manager import RiskManager
from core.strategy_engine import StrategyEngine
from state.portfolio_tracker import EquitySnapshot, PortfolioTracker


@dataclass
class BacktestResult:
    metrics: PerformanceMetrics
    trades: List[BacktestTrade]
    equity_curve: List[EquitySnapshot]
    initial_capital: float
    final_equity: float

    def summary(self) -> str:
        m = self.metrics
        return (
            f"====================================================================\n"
            f"               BACKTEST PERFORMANCE SUMMARY                        \n"
            f"====================================================================\n"
            f"Initial Capital:         ${self.initial_capital:,.2f}\n"
            f"Final Equity:            ${self.final_equity:,.2f}\n"
            f"Total Return:            {m.total_return_pct:+.2f}%\n"
            f"Annualized Return (CAGR):{m.cagr_pct:+.2f}%\n"
            f"Max Drawdown:            {m.max_drawdown_pct:.2f}%\n"
            f"Annualized Volatility:   {m.annualized_volatility:.4f}\n"
            f"Downside Deviation:      {m.downside_deviation:.4f}\n"
            f"--------------------------------------------------------------------\n"
            f"Sharpe Ratio:            {m.sharpe_ratio:.4f}\n"
            f"Sortino Ratio:           {m.sortino_ratio:.4f}\n"
            f"Calmar Ratio:            {m.calmar_ratio:.4f}\n"
            f"OFFICIAL COMPOSITE SCORE: {m.composite_score:.4f}\n"
            f"  (0.40 * Sortino + 0.30 * Sharpe + 0.30 * Calmar)\n"
            f"--------------------------------------------------------------------\n"
            f"Total Trades:            {m.total_trades}\n"
            f"Win Rate:                {m.win_rate_pct:.1f}%\n"
            f"Profit Factor:           {m.profit_factor:.2f}\n"
            f"Trade Expectancy:        ${m.expectancy:.2f}\n"
            f"Total Turnover:          ${m.total_turnover:,.2f}\n"
            f"Total Fees Paid:         ${m.total_fees:,.2f}\n"
            f"===================================================================="
        )


class BacktestEngine:
    """
    Event-driven backtesting engine for quantitative crypto trading strategies.
    """

    def __init__(self, config: Optional[AppConfig] = None):
        self.config = config or AppConfig()
        self.exec_model = SimulatedExecutionModel(self.config.fees)

    def run(
        self,
        df: pd.DataFrame,
        symbol: str = "BTC/USD",
        warmup_bars: int = 60,
    ) -> BacktestResult:
        """
        Run sequential backtest on OHLCV dataframe.
        """
        portfolio = PortfolioTracker(
            initial_capital=self.config.portfolio.initial_capital,
            min_cash_reserve_pct=self.config.portfolio.min_cash_reserve_pct,
            persistence_file="data/backtest_port.json",
        )
        risk_manager = RiskManager(
            portfolio=portfolio,
            risk_config=self.config.risk_controls,
            trailing_config=self.config.trailing_stop,
            max_risk_per_trade_pct=self.config.portfolio.max_risk_per_trade_pct,
            max_gross_exposure_pct=self.config.portfolio.max_gross_exposure_pct,
            min_cash_reserve_pct=self.config.portfolio.min_cash_reserve_pct,
        )
        strategy_engine = StrategyEngine(config=self.config.strategies)

        trades: List[BacktestTrade] = []
        n = len(df)

        closes = df["close"].values
        highs = df["high"].values
        lows = df["low"].values
        opens = df["open"].values
        timestamps = df["timestamp"].values if "timestamp" in df.columns else np.arange(n) * 300000

        # Active trade tracking helper
        active_trade: Optional[Dict[str, Any]] = None

        for i in range(warmup_bars, n):
            curr_bar_open = float(opens[i])
            curr_bar_high = float(highs[i])
            curr_bar_low = float(lows[i])
            curr_bar_close = float(closes[i])
            curr_ts = int(timestamps[i])

            # Update mark price
            portfolio.update_mark_prices({symbol: curr_bar_close})

            # Check Circuit Breakers
            is_tripped, breaker_reason = risk_manager.check_circuit_breakers()
            if is_tripped and active_trade:
                # Liquidate active position due to circuit breaker
                pos = portfolio.positions.get(symbol)
                if pos and pos.quantity > 0:
                    exit_px, exit_fee = self.exec_model.simulate_exit("SELL", curr_bar_open, pos.quantity)
                    trade_pnl = pos.quantity * (exit_px - pos.entry_price) - exit_fee
                    trades.append(
                        BacktestTrade(
                            symbol=symbol,
                            strategy=active_trade["strategy"],
                            entry_time=active_trade["entry_time"],
                            exit_time=curr_ts,
                            side="BUY",
                            quantity=pos.quantity,
                            entry_price=pos.entry_price,
                            exit_price=exit_px,
                            stop_loss=pos.stop_loss,
                            take_profit_1=pos.take_profit_1,
                            take_profit_2=pos.take_profit_2,
                            pnl=trade_pnl + exit_fee,
                            net_pnl=trade_pnl,
                            fees=active_trade["entry_fee"] + exit_fee,
                            exit_reason=f"CIRCUIT_BREAKER: {breaker_reason}",
                        )
                    )
                    portfolio.record_fill(symbol, "SELL", pos.quantity, exit_px, exit_fee)
                    active_trade = None

            # -----------------------------------------------------------------
            # 1. Manage Active Position (Stops, Targets, Trailing Stop)
            # -----------------------------------------------------------------
            if active_trade and symbol in portfolio.positions:
                pos = portfolio.positions[symbol]
                qty = pos.quantity

                # Evaluate trailing stop ratchet
                risk_manager.update_trailing_stop(symbol, curr_bar_high)

                # Check Stop Loss Trigger (intrabar low penetrates stop)
                if curr_bar_low <= pos.stop_loss:
                    exit_px, exit_fee = self.exec_model.simulate_exit("SELL", pos.stop_loss, qty)
                    trade_pnl = qty * (exit_px - pos.entry_price) - exit_fee
                    trades.append(
                        BacktestTrade(
                            symbol=symbol,
                            strategy=active_trade["strategy"],
                            entry_time=active_trade["entry_time"],
                            exit_time=curr_ts,
                            side="BUY",
                            quantity=qty,
                            entry_price=pos.entry_price,
                            exit_price=exit_px,
                            stop_loss=pos.stop_loss,
                            take_profit_1=pos.take_profit_1,
                            take_profit_2=pos.take_profit_2,
                            pnl=trade_pnl + exit_fee,
                            net_pnl=trade_pnl,
                            fees=active_trade["entry_fee"] + exit_fee,
                            exit_reason="STOP_LOSS",
                        )
                    )
                    portfolio.record_fill(symbol, "SELL", qty, exit_px, exit_fee)
                    active_trade = None

                # Check Take Profit 2 (Full Exit)
                elif curr_bar_high >= pos.take_profit_2 and pos.take_profit_2 > 0:
                    exit_px, exit_fee = self.exec_model.simulate_exit("SELL", pos.take_profit_2, qty)
                    trade_pnl = qty * (exit_px - pos.entry_price) - exit_fee
                    trades.append(
                        BacktestTrade(
                            symbol=symbol,
                            strategy=active_trade["strategy"],
                            entry_time=active_trade["entry_time"],
                            exit_time=curr_ts,
                            side="BUY",
                            quantity=qty,
                            entry_price=pos.entry_price,
                            exit_price=exit_px,
                            stop_loss=pos.stop_loss,
                            take_profit_1=pos.take_profit_1,
                            take_profit_2=pos.take_profit_2,
                            pnl=trade_pnl + exit_fee,
                            net_pnl=trade_pnl,
                            fees=active_trade["entry_fee"] + exit_fee,
                            exit_reason="TAKE_PROFIT_2",
                        )
                    )
                    portfolio.record_fill(symbol, "SELL", qty, exit_px, exit_fee)
                    active_trade = None

                # Check Take Profit 1 (Partial scale out 50% if not yet scaled)
                elif curr_bar_high >= pos.take_profit_1 and not active_trade.get("tp1_hit", False) and pos.take_profit_1 > 0:
                    scale_qty = qty * 0.50
                    exit_px, exit_fee = self.exec_model.simulate_exit("SELL", pos.take_profit_1, scale_qty)
                    portfolio.record_fill(symbol, "SELL", scale_qty, exit_px, exit_fee)
                    active_trade["tp1_hit"] = True
                    # Move stop to breakeven after TP1 hit
                    pos.stop_loss = max(pos.stop_loss, pos.entry_price * 1.001)

            # -----------------------------------------------------------------
            # 2. Evaluate Strategy Signals (Strictly past bars [0:i] - No lookahead)
            # -----------------------------------------------------------------
            history_df = df.iloc[: i + 1]
            signal = strategy_engine.evaluate_symbol(
                symbol=symbol,
                df=history_df,
                cvd_available=("delta" in df.columns),
                oi_available=False,
                taker_fee_pct=self.config.fees.taker_fee_pct,
                slippage_pct=self.config.fees.slippage_pct,
            )

            # Handle Spot De-risking signal
            if signal.direction == "DE_RISK" and active_trade and symbol in portfolio.positions:
                pos = portfolio.positions[symbol]
                qty = pos.quantity
                exit_px, exit_fee = self.exec_model.simulate_exit("SELL", curr_bar_close, qty)
                trade_pnl = qty * (exit_px - pos.entry_price) - exit_fee
                trades.append(
                    BacktestTrade(
                        symbol=symbol,
                        strategy=active_trade["strategy"],
                        entry_time=active_trade["entry_time"],
                        exit_time=curr_ts,
                        side="BUY",
                        quantity=qty,
                        entry_price=pos.entry_price,
                        exit_price=exit_px,
                        stop_loss=pos.stop_loss,
                        take_profit_1=pos.take_profit_1,
                        take_profit_2=pos.take_profit_2,
                        pnl=trade_pnl + exit_fee,
                        net_pnl=trade_pnl,
                        fees=active_trade["entry_fee"] + exit_fee,
                        exit_reason="STRATEGY_DE_RISK",
                    )
                )
                portfolio.record_fill(symbol, "SELL", qty, exit_px, exit_fee)
                active_trade = None

            # Handle BUY Signal if no active position
            elif signal.direction == "BUY" and not active_trade:
                risk_dec = risk_manager.evaluate_signal(signal)
                if risk_dec.approved and risk_dec.adjusted_quantity > 0:
                    qty = risk_dec.adjusted_quantity
                    fill_px, entry_fee = self.exec_model.simulate_entry("BUY", curr_bar_close, qty)

                    portfolio.record_fill(
                        symbol=symbol,
                        side="BUY",
                        quantity=qty,
                        price=fill_px,
                        fee=entry_fee,
                        strategy=signal.strategy,
                        stop_loss=risk_dec.stop_loss,
                        take_profit_1=risk_dec.take_profit_1,
                        take_profit_2=risk_dec.take_profit_2,
                    )
                    active_trade = {
                        "strategy": signal.strategy,
                        "entry_time": curr_ts,
                        "entry_price": fill_px,
                        "entry_fee": entry_fee,
                        "tp1_hit": False,
                    }

        # Calculate final performance
        completed_trade_dicts = [{"pnl": t.net_pnl} for t in trades]
        metrics = PerformanceEngine.calculate_metrics(
            equity_snapshots=portfolio.equity_curve,
            initial_capital=self.config.portfolio.initial_capital,
            completed_trades=completed_trade_dicts,
        )

        return BacktestResult(
            metrics=metrics,
            trades=trades,
            equity_curve=portfolio.equity_curve,
            initial_capital=self.config.portfolio.initial_capital,
            final_equity=portfolio.total_equity,
        )
