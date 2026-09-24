"""
Unit tests for Backtesting Engine, Execution Model, and Walk-Forward Validator.
"""

import pytest
import numpy as np
import pandas as pd

from backtest.engine import BacktestEngine
from backtest.execution_model import SimulatedExecutionModel
from backtest.walk_forward import WalkForwardValidator
from config.trading_params import AppConfig, FeesConfig


def test_simulated_execution_model_fees():
    """Verify maker (0.05%) and taker (0.10%) fees and slippage calculations."""
    model = SimulatedExecutionModel(FeesConfig(maker_fee_pct=0.0005, taker_fee_pct=0.0010, slippage_pct=0.0002))

    # Taker BUY: price slipped up by 0.02%, fee is 0.10% of slipped notional
    fill_px, fee = model.simulate_entry("BUY", 50000.0, 1.0, is_maker=False)
    assert pytest.approx(fill_px, rel=1e-4) == 50010.0
    assert pytest.approx(fee, rel=1e-4) == 50.01

    # Maker BUY: no slippage, fee is 0.05%
    fill_px, fee = model.simulate_entry("BUY", 50000.0, 1.0, is_maker=True)
    assert fill_px == 50000.0
    assert pytest.approx(fee, rel=1e-4) == 25.0


def test_backtest_engine_execution_flow():
    """Run BacktestEngine on synthetic price series."""
    n = 200
    np.random.seed(42)
    prices = [50000.0 + i * 10.0 for i in range(n)]

    df = pd.DataFrame({
        "timestamp": [1700000000000 + i * 300000 for i in range(n)],
        "open": [p - 5.0 for p in prices],
        "high": [p + 20.0 for p in prices],
        "low": [p - 20.0 for p in prices],
        "close": prices,
        "volume": [50.0] * n,
    })

    engine = BacktestEngine()
    result = engine.run(df, symbol="BTC/USD", warmup_bars=30)
    assert result.initial_capital == 100000.0
    assert len(result.equity_curve) > 0
    assert result.metrics.composite_score is not None
    summary_str = result.summary()
    assert "BACKTEST PERFORMANCE SUMMARY" in summary_str


def test_walk_forward_partitioning():
    """Verify train, validation, and test chronological splitting."""
    n = 300
    df = pd.DataFrame({
        "timestamp": [1700000000000 + i * 300000 for i in range(n)],
        "open": [50000.0] * n,
        "high": [50100.0] * n,
        "low": [49900.0] * n,
        "close": [50000.0] * n,
        "volume": [10.0] * n,
    })

    validator = WalkForwardValidator()
    report = validator.run_walk_forward(df, symbol="BTC/USD", train_pct=0.60, val_pct=0.20)
    assert report.in_sample_result is not None
    assert report.validation_result is not None
    assert report.out_of_sample_result is not None
    assert len(report.sensitivity_analysis) == 9
    assert "WALK-FORWARD VALIDATION REPORT" in report.summary()
