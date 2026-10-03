"""
Performance Analytics Engine.
Calculates official competition metrics:
Composite Score = 0.40 * Sortino + 0.30 * Sharpe + 0.30 * Calmar
Also tracks total return, downside deviation, max drawdown, win rate, profit factor, and fee drag.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import numpy as np
import pandas as pd

from state.portfolio_tracker import EquitySnapshot, PortfolioTracker


@dataclass
class PerformanceMetrics:
    total_return_pct: float
    cagr_pct: float
    annualized_volatility: float
    downside_deviation: float
    max_drawdown_pct: float
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float
    composite_score: float
    win_rate_pct: float
    profit_factor: float
    expectancy: float
    total_trades: int
    total_turnover: float
    total_fees: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_return_pct": round(self.total_return_pct, 2),
            "cagr_pct": round(self.cagr_pct, 2),
            "annualized_volatility": round(self.annualized_volatility, 4),
            "downside_deviation": round(self.downside_deviation, 4),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "sharpe_ratio": round(self.sharpe_ratio, 4),
            "sortino_ratio": round(self.sortino_ratio, 4),
            "calmar_ratio": round(self.calmar_ratio, 4),
            "composite_score": round(self.composite_score, 4),
            "win_rate_pct": round(self.win_rate_pct, 2),
            "profit_factor": round(self.profit_factor, 2),
            "expectancy": round(self.expectancy, 2),
            "total_trades": self.total_trades,
            "total_turnover": round(self.total_turnover, 2),
            "total_fees": round(self.total_fees, 2),
        }


class PerformanceEngine:
    """
    Computes mathematical risk-adjusted performance from portfolio equity curves and trade ledgers.
    """

    @staticmethod
    def calculate_metrics(
        equity_snapshots: List[EquitySnapshot],
        initial_capital: float = 100000.0,
        completed_trades: Optional[List[Dict[str, Any]]] = None,
        risk_free_rate: float = 0.0,
        periods_per_year: float = 365.0 * 24.0 * 12.0,  # 5-minute periods per year (105,120)
        peak_equity: Optional[float] = None,
    ) -> PerformanceMetrics:
        """
        Compute full statistical and competition metrics from the equity curve.
        """
        if not equity_snapshots:
            return PerformanceMetrics(
                total_return_pct=0.0,
                cagr_pct=0.0,
                annualized_volatility=0.0,
                downside_deviation=0.0,
                max_drawdown_pct=0.0,
                sharpe_ratio=0.0,
                sortino_ratio=0.0,
                calmar_ratio=0.0,
                composite_score=0.0,
                win_rate_pct=0.0,
                profit_factor=0.0,
                expectancy=0.0,
                total_trades=0,
                total_turnover=0.0,
                total_fees=0.0,
            )

        eq_list = [s.equity for s in equity_snapshots]
        ts_list = [s.timestamp_ms for s in equity_snapshots]

        # Anchor inception capital at baseline if not present
        if abs(eq_list[0] - initial_capital) > 1e-4:
            t0 = max(0, ts_list[0] - 300000)
            eq_list.insert(0, initial_capital)
            ts_list.insert(0, t0)

        equities = np.array(eq_list, dtype=float)
        timestamps = np.array(ts_list, dtype=float)

        curr_equity = equities[-1]
        total_return = (curr_equity - initial_capital) / initial_capital

        # Duration in years
        duration_ms = max(1.0, timestamps[-1] - timestamps[0]) if len(timestamps) > 1 else 1000.0
        duration_years = max(duration_ms / (1000.0 * 3600.0 * 24.0 * 365.25), 1.0 / 365.25)

        # CAGR
        if curr_equity > 0 and initial_capital > 0:
            cagr = ((curr_equity / initial_capital) ** (1.0 / duration_years)) - 1.0
        else:
            cagr = -1.0

        # Periodic returns
        returns = np.diff(equities) / np.maximum(equities[:-1], 1e-6) if len(equities) > 1 else np.array([0.0])

        # Annualized Volatility
        ret_std = float(np.std(returns)) if len(returns) > 1 else 0.0
        ann_vol = ret_std * np.sqrt(periods_per_year)

        # Downside Deviation (semi-variance of returns below risk-free rate)
        negative_returns = np.minimum(0.0, returns - (risk_free_rate / periods_per_year))
        downside_variance = float(np.mean(negative_returns ** 2)) if len(returns) > 0 else 0.0
        downside_dev = np.sqrt(downside_variance) * np.sqrt(periods_per_year)

        # Peak-to-trough Maximum Drawdown
        running_max = np.maximum.accumulate(equities)
        drawdowns = (running_max - equities) / np.maximum(running_max, 1e-6)
        max_drawdown = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0.0
        if peak_equity is not None and peak_equity > 0:
            peak_dd = max(0.0, (peak_equity - curr_equity) / peak_equity)
            max_drawdown = max(max_drawdown, peak_dd)

        # Risk-adjusted ratios
        excess_return = cagr - risk_free_rate
        if ann_vol > 1e-6:
            sharpe = excess_return / ann_vol
        elif excess_return > 0:
            sharpe = excess_return / 0.001
        elif excess_return < 0:
            effective_vol = max(abs(total_return), 0.001)
            sharpe = excess_return / effective_vol
        else:
            sharpe = 0.0

        if downside_dev > 1e-6:
            sortino = excess_return / downside_dev
        elif excess_return > 0:
            sortino = excess_return / 0.001
        elif excess_return < 0:
            effective_dev = max(ann_vol, abs(total_return), 0.001)
            sortino = excess_return / effective_dev
        else:
            sortino = 0.0

        if max_drawdown > 1e-6:
            calmar = cagr / max_drawdown
        elif cagr > 0:
            calmar = cagr / 0.001
        elif cagr < 0:
            effective_dd = max(abs(total_return), 0.001)
            calmar = cagr / effective_dd
        else:
            calmar = 0.0

        # Clip extreme outliers for stability
        sharpe = float(np.clip(sharpe, -10.0, 20.0))
        sortino = float(np.clip(sortino, -10.0, 30.0))
        calmar = float(np.clip(calmar, -10.0, 30.0))

        # Official Roostoo Composite Score: 0.40 * Sortino + 0.30 * Sharpe + 0.30 * Calmar
        composite = 0.40 * sortino + 0.30 * sharpe + 0.30 * calmar

        # Trade analytics
        trades = completed_trades or []
        trade_pnls = [t.get("pnl", 0.0) for t in trades]
        total_trades = len(trades)
        wins = [p for p in trade_pnls if p > 0]
        losses = [p for p in trade_pnls if p < 0]

        win_rate = (len(wins) / total_trades * 100.0) if total_trades > 0 else 0.0
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0.0)
        expectancy = (sum(trade_pnls) / total_trades) if total_trades > 0 else 0.0

        total_turnover = equity_snapshots[-1].realized_pnl + equity_snapshots[-1].cumulative_fees
        total_fees = equity_snapshots[-1].cumulative_fees

        return PerformanceMetrics(
            total_return_pct=total_return * 100.0,
            cagr_pct=cagr * 100.0,
            annualized_volatility=ann_vol,
            downside_deviation=downside_dev,
            max_drawdown_pct=max_drawdown * 100.0,
            sharpe_ratio=sharpe,
            sortino_ratio=sortino,
            calmar_ratio=calmar,
            composite_score=composite,
            win_rate_pct=win_rate,
            profit_factor=profit_factor,
            expectancy=expectancy,
            total_trades=total_trades,
            total_turnover=total_turnover,
            total_fees=total_fees,
        )
