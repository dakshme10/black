"""
Unit tests for Risk Management Engine.
Covers position sizing, 1.0% risk cap, 5% cash reserve, 1.0x gross exposure,
rolling 3.5% drawdown freeze, 6.0% max drawdown breaker, and trailing stops.
"""

import pytest
import time
from core.risk_manager import RiskManager
from core.strategy_engine import Signal
from state.portfolio_tracker import PortfolioTracker
from config.trading_params import RiskControlsConfig, TrailingStopConfig


@pytest.fixture
def portfolio(tmp_path):
    f = tmp_path / "port.json"
    return PortfolioTracker(initial_capital=100000.0, min_cash_reserve_pct=0.05, persistence_file=str(f))


@pytest.fixture
def risk_manager(portfolio):
    return RiskManager(
        portfolio=portfolio,
        risk_config=RiskControlsConfig(
            rolling_24h_drawdown_limit=0.035,
            freeze_duration_hours=6.0,
            max_drawdown_limit=0.06,
            enforce_circuit_breakers=True,
        ),
        trailing_config=TrailingStopConfig(
            breakeven_trigger_r=1.0,
            trail_activation_r=2.0,
            atr_multiplier=1.5,
        ),
        max_risk_per_trade_pct=0.01,
        max_gross_exposure_pct=1.00,
        min_cash_reserve_pct=0.05,
    )


def test_position_sizing_standard(risk_manager):
    """
    Equity = $100,000. Risk % = 1.0% -> Risk Capital = $1,000.
    Entry = $50,000, Stop = $49,000 -> Stop Distance = $1,000.
    Sized Quantity = $1,000 / $1,000 = 1.0 BTC.
    """
    sig = Signal(
        strategy="VALUE_AREA",
        symbol="BTC/USD",
        direction="BUY",
        confidence=0.85,
        entry_price=50000.0,
        stop_loss=49000.0,
        take_profit_1=51500.0,
        take_profit_2=53000.0,
        expected_rr=2.0,
        reason="Test",
        regime="RANGE",
        timestamp=int(time.time() * 1000),
    )
    decision = risk_manager.evaluate_signal(sig)
    assert decision.approved is True
    assert pytest.approx(decision.adjusted_quantity, rel=1e-3) == 1.0
    assert pytest.approx(decision.risk_capital, rel=1e-3) == 1000.0
    assert pytest.approx(decision.risk_pct, rel=1e-3) == 0.01


def test_invalid_stop_loss_rejection(risk_manager):
    """Stop loss above or equal to entry must be rejected."""
    sig = Signal(
        strategy="VALUE_AREA",
        symbol="BTC/USD",
        direction="BUY",
        confidence=0.85,
        entry_price=50000.0,
        stop_loss=50500.0,  # Invalid: above entry
        take_profit_1=52000.0,
        take_profit_2=54000.0,
        expected_rr=2.0,
        reason="Test",
        regime="RANGE",
        timestamp=int(time.time() * 1000),
    )
    decision = risk_manager.evaluate_signal(sig)
    assert decision.approved is False
    assert "Invalid stop loss" in decision.reason


def test_cash_reserve_and_exposure_capping(portfolio, risk_manager):
    """
    If sized position notional exceeds available cash (after 5% reserve),
    quantity must be truncated to available cash.
    """
    risk_manager.risk_config.enforce_circuit_breakers = False
    # Set cash to $10,000 and total equity $100,000
    portfolio.cash = 10000.0
    # Mandatory reserve is 5% of $100k = $5,000. Available cash = $5,000.
    sig = Signal(
        strategy="VALUE_AREA",
        symbol="BTC/USD",
        direction="BUY",
        confidence=0.85,
        entry_price=50000.0,
        stop_loss=49900.0,  # Very tight stop -> would otherwise size huge position
        take_profit_1=51000.0,
        take_profit_2=52000.0,
        expected_rr=2.0,
        reason="Test",
        regime="RANGE",
        timestamp=int(time.time() * 1000),
    )
    decision = risk_manager.evaluate_signal(sig)
    assert decision.approved is True
    notional = decision.adjusted_quantity * 50000.0
    assert notional <= 5000.01  # Cannot exceed available cash


def test_rolling_24h_drawdown_breaker(portfolio, risk_manager):
    """
    A 3.5% rolling 24h drawdown must trip the circuit breaker and freeze entries for 6h.
    """
    now_ms = int(time.time() * 1000)
    # Simulate peak equity $100k followed by drop to $96.0k (4.0% drawdown)
    portfolio.equity_curve = [
        portfolio.equity_curve[0],
        type(portfolio.equity_curve[0])(
            timestamp_ms=now_ms,
            equity=96000.0,
            cash=96000.0,
            gross_exposure=0.0,
            unrealized_pnl=0.0,
            realized_pnl=-4000.0,
            cumulative_fees=50.0,
        ),
    ]

    tripped, msg = risk_manager.check_circuit_breakers()
    assert tripped is True
    assert "ROLLING_DRAWDOWN_BREAKER" in msg or "FROZEN" in msg

    # Signal evaluation should be vetoed by circuit breaker
    sig = Signal(
        strategy="VALUE_AREA",
        symbol="BTC/USD",
        direction="BUY",
        confidence=0.85,
        entry_price=50000.0,
        stop_loss=49000.0,
        take_profit_1=51500.0,
        take_profit_2=53000.0,
        expected_rr=2.0,
        reason="Test",
        regime="RANGE",
        timestamp=now_ms,
    )
    decision = risk_manager.evaluate_signal(sig)
    assert decision.approved is False
    assert decision.circuit_breaker_active is True


def test_max_drawdown_permanent_breaker(portfolio, risk_manager):
    """
    A 6.0% maximum drawdown must trigger permanent liquidation kill switch.
    """
    portfolio.peak_equity = 100000.0
    portfolio.cash = 93500.0  # 6.5% drawdown
    portfolio.positions.clear()

    tripped, msg = risk_manager.check_circuit_breakers()
    assert tripped is True
    assert risk_manager.permanent_kill_switch is True
    assert "MAX_DRAWDOWN" in msg or "PERMANENT" in msg


def test_trailing_stop_engine(portfolio, risk_manager):
    """
    Trailing stop ratchets:
    - At +1.0R: move stop to breakeven
    - At +2.0R: activate trailing stop
    - Stop never moves backward
    """
    portfolio.record_fill(
        symbol="BTC/USD",
        side="BUY",
        quantity=1.0,
        price=50000.0,
        fee=25.0,
        stop_loss=49000.0,  # 1R = $1,000
    )

    # 1. Price at 50,500 (+0.5R): Stop should remain at 49,000
    new_stop = risk_manager.update_trailing_stop("BTC/USD", 50500.0, atr=500.0)
    assert new_stop is None
    assert portfolio.positions["BTC/USD"].stop_loss == 49000.0

    # 2. Price reaches 51,100 (+1.1R): Breakeven triggers
    new_stop = risk_manager.update_trailing_stop("BTC/USD", 51100.0, atr=500.0)
    assert new_stop is not None
    assert new_stop >= 50000.0  # Protected breakeven

    # 3. Price reaches 53,000 (+3.0R): Trailing stop activates
    # highest_price = 53,000. atr = 500 * 1.5 = 750. Trail = 52,250
    portfolio.positions["BTC/USD"].highest_price = 53000.0
    new_stop2 = risk_manager.update_trailing_stop("BTC/USD", 53000.0, atr=500.0)
    assert new_stop2 is not None
    assert new_stop2 >= 52250.0

    # 4. Price pulls back to 52,500: Stop must NOT move down (ratchet only)
    new_stop3 = risk_manager.update_trailing_stop("BTC/USD", 52500.0, atr=500.0)
    assert new_stop3 is None
    assert portfolio.positions["BTC/USD"].stop_loss == new_stop2
