"""
Unit tests for Performance Metrics Engine and Official Composite Score.
Composite Score = 0.40 * Sortino + 0.30 * Sharpe + 0.30 * Calmar.
"""

import pytest
import numpy as np

from core.performance import PerformanceEngine
from state.portfolio_tracker import EquitySnapshot


def test_composite_score_formula_adherence():
    """
    Verify exact mathematical adherence to the official competition score formula:
    Composite Score = 0.40 * Sortino + 0.30 * Sharpe + 0.30 * Calmar
    """
    # Create equity series with steady growth (higher Sortino than Sharpe due to low downside)
    t0 = 1700000000000
    dt = 300000
    equities = [100000.0, 100500.0, 100800.0, 101200.0, 101100.0, 102000.0, 102500.0]

    snapshots = [
        EquitySnapshot(
            timestamp_ms=t0 + i * dt,
            equity=eq,
            cash=eq,
            gross_exposure=0.0,
            unrealized_pnl=0.0,
            realized_pnl=eq - 100000.0,
            cumulative_fees=10.0 * i,
        )
        for i, eq in enumerate(equities)
    ]

    metrics = PerformanceEngine.calculate_metrics(snapshots, initial_capital=100000.0)

    expected_composite = 0.40 * metrics.sortino_ratio + 0.30 * metrics.sharpe_ratio + 0.30 * metrics.calmar_ratio
    assert pytest.approx(metrics.composite_score, rel=1e-4) == expected_composite
    assert metrics.total_return_pct == pytest.approx(2.5, rel=1e-3)
    assert metrics.max_drawdown_pct >= 0.0


def test_downside_deviation_vs_volatility():
    """
    Sortino downside deviation must only penalize returns below risk-free rate,
    meaning large upside jumps increase Sharpe's denominator (volatility) but NOT Sortino's.
    """
    t0 = 1700000000000
    # Series with zero downside returns (only flat and positive steps)
    equities = [100000.0, 101000.0, 101000.0, 103000.0, 105000.0]
    snapshots = [
        EquitySnapshot(
            timestamp_ms=t0 + i * 300000,
            equity=eq,
            cash=eq,
            gross_exposure=0.0,
            unrealized_pnl=0.0,
            realized_pnl=eq - 100000.0,
            cumulative_fees=0.0,
        )
        for i, eq in enumerate(equities)
    ]

    metrics = PerformanceEngine.calculate_metrics(snapshots, initial_capital=100000.0)
    # Downside deviation must be zero since no returns were negative
    assert metrics.downside_deviation == 0.0
    # Annualized volatility must be positive
    assert metrics.annualized_volatility > 0.0


def test_trade_expectancy_and_profit_factor():
    """Verify win rate, profit factor, and expectancy calculations."""
    trades = [
        {"pnl": 500.0},
        {"pnl": -200.0},
        {"pnl": 300.0},
        {"pnl": -100.0},
    ]
    snapshots = [
        EquitySnapshot(timestamp_ms=1000, equity=100000.0, cash=100000.0, gross_exposure=0.0, unrealized_pnl=0.0, realized_pnl=0.0, cumulative_fees=0.0),
        EquitySnapshot(timestamp_ms=2000, equity=100500.0, cash=100500.0, gross_exposure=0.0, unrealized_pnl=0.0, realized_pnl=500.0, cumulative_fees=10.0),
    ]

    metrics = PerformanceEngine.calculate_metrics(snapshots, completed_trades=trades)
    # Wins: 500, 300 (2 wins). Losses: -200, -100 (2 losses). Total: 4 trades.
    assert metrics.win_rate_pct == 50.0
    # Gross Profit = 800. Gross Loss = 300. Profit Factor = 800 / 300 = 2.67
    assert pytest.approx(metrics.profit_factor, rel=1e-2) == 2.67
    # Expectancy = (500 - 200 + 300 - 100) / 4 = 125.0
    assert pytest.approx(metrics.expectancy, rel=1e-2) == 125.0


def test_metrics_inception_anchor_and_peak_equity():
    """Verify that flat equity snapshots anchor to initial_capital and reflect peak_equity drawdown."""
    t0 = 1790990000000
    # Simulate snapshots starting at 49,000 when initial capital was 50,000
    snapshots = [
        EquitySnapshot(
            timestamp_ms=t0 + i * 2000,
            equity=49000.0,
            cash=49000.0,
            gross_exposure=0.0,
            unrealized_pnl=0.0,
            realized_pnl=-1000.0,
            cumulative_fees=50.0,
        )
        for i in range(20)
    ]

    # Without peak_equity override: max_drawdown is (50000 - 49000) / 50000 = 2.0%
    metrics = PerformanceEngine.calculate_metrics(snapshots, initial_capital=50000.0)
    assert pytest.approx(metrics.total_return_pct, rel=1e-3) == -2.0
    assert pytest.approx(metrics.max_drawdown_pct, rel=1e-3) == 2.0
    assert metrics.composite_score < 0.0  # Must be negative, not 0.0000!

    # With historical peak_equity = 51,000: drawdown from peak = (51000 - 49000) / 51000 = 3.92%
    metrics_peak = PerformanceEngine.calculate_metrics(snapshots, initial_capital=50000.0, peak_equity=51000.0)
    assert pytest.approx(metrics_peak.max_drawdown_pct, rel=1e-2) == 3.92

