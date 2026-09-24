"""
Unit tests for Order Execution, API Client, Rate Limiting, and UNKNOWN state handling.
"""

import pytest
import time
from unittest.mock import MagicMock, patch

from config.trading_params import AppConfig, ExchangeConfig, FeesConfig
from core.api_client import RateLimiter, RoostooClient, UnknownOrderStateError
from core.order_executor import OrderExecutor
from core.risk_manager import RiskDecision, RiskManager
from core.strategy_engine import Signal
from state.order_state import OrderStateManager, OrderStatus
from state.portfolio_tracker import PortfolioTracker


def test_rate_limiter():
    """Verify token bucket rate limiter rate and burst capacity."""
    limiter = RateLimiter(rate=10.0, capacity=2)
    # First 2 should be immediate (consuming burst capacity)
    w1 = limiter.acquire()
    w2 = limiter.acquire()
    assert w1 == 0.0
    assert w2 == 0.0
    # Next token should require waiting ~0.1s
    t0 = time.monotonic()
    limiter.acquire()
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.05


def test_hmac_signature_generation():
    """
    Verify HMAC-SHA256 generation complies with Roostoo documentation:
    Sorted parameters, connected by '=' and '&'.
    """
    client = RoostooClient(api_key="TEST_API_KEY", secret_key="TEST_SECRET_KEY")
    params = {
        "timestamp": "1580774512000",
        "pair": "BTC/USD",
        "side": "BUY",
        "quantity": "1.0",
        "type": "MARKET",
    }
    sig, total_params = client.generate_signature(params)
    assert total_params == "pair=BTC/USD&quantity=1.0&side=BUY&timestamp=1580774512000&type=MARKET"
    assert len(sig) == 64  # SHA-256 hex string


def test_dry_run_simulation_execution(tmp_path):
    """
    In DRY_RUN mode, order must be simulated with taker fee (0.10%) and slippage,
    and recorded in the portfolio without touching the live API.
    """
    cfg = AppConfig()
    cfg.dry_run = True
    cfg.live_trading_enabled = False
    cfg.fees = FeesConfig(maker_fee_pct=0.0005, taker_fee_pct=0.0010, slippage_pct=0.0002)

    portfolio = PortfolioTracker(initial_capital=100000.0, persistence_file=str(tmp_path / "dry_port.json"))
    order_mgr = OrderStateManager(persistence_file=str(tmp_path / "dry_orders.json"))
    risk_mgr = RiskManager(portfolio=portfolio)
    client = RoostooClient("dummy", "dummy")

    executor = OrderExecutor(
        config=cfg,
        api_client=client,
        portfolio=portfolio,
        order_manager=order_mgr,
        risk_manager=risk_mgr,
        fees_config=cfg.fees,
    )

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
        reason="Test buy",
        regime="RANGE",
        timestamp=int(time.time() * 1000),
    )
    risk_dec = RiskDecision(
        approved=True,
        adjusted_quantity=0.5,
        adjusted_price=50000.0,
        stop_loss=49000.0,
        take_profit_1=51500.0,
        take_profit_2=53000.0,
        risk_capital=500.0,
        risk_pct=0.005,
        reason="Approved",
    )

    order = executor.execute_decision(sig, risk_dec, current_market_price=50000.0)
    assert order is not None
    assert order.status == OrderStatus.FILLED
    assert order.role == "TAKER"
    # Slipped price: 50,000 * 1.0002 = 50,010
    assert pytest.approx(order.filled_avg_price, rel=1e-3) == 50010.0
    # Fee: 0.5 * 50,010 * 0.0010 = 25.005
    assert pytest.approx(order.commission, rel=1e-3) == 25.005
    assert "BTC/USD" in portfolio.positions
    assert portfolio.positions["BTC/USD"].quantity == 0.5


def test_unknown_order_state_on_timeout(tmp_path):
    """
    Section 8 & 25: On network timeout during place_order, order must transition to UNKNOWN,
    not blindly retried, and flagged for reconciliation.
    """
    cfg = AppConfig()
    cfg.dry_run = False
    cfg.live_trading_enabled = True
    cfg.api_key = "VALID_KEY"
    cfg.secret_key = "VALID_SECRET"

    portfolio = PortfolioTracker(initial_capital=100000.0, persistence_file=str(tmp_path / "unk_port.json"))
    order_mgr = OrderStateManager(persistence_file=str(tmp_path / "unk_orders.json"))
    risk_mgr = RiskManager(portfolio=portfolio)
    client = RoostooClient("VALID_KEY", "VALID_SECRET")

    # Mock client.place_order to raise UnknownOrderStateError
    client.place_order = MagicMock(side_effect=UnknownOrderStateError("Connection timeout during submission"))

    executor = OrderExecutor(
        config=cfg,
        api_client=client,
        portfolio=portfolio,
        order_manager=order_mgr,
        risk_manager=risk_mgr,
    )

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
        reason="Test buy",
        regime="RANGE",
        timestamp=int(time.time() * 1000),
    )
    risk_dec = RiskDecision(
        approved=True,
        adjusted_quantity=0.1,
        adjusted_price=50000.0,
        stop_loss=49000.0,
        take_profit_1=51500.0,
        take_profit_2=53000.0,
        risk_capital=100.0,
        risk_pct=0.001,
        reason="Approved",
    )

    order = executor.execute_decision(sig, risk_dec, current_market_price=50000.0)
    assert order is not None
    assert order.status == OrderStatus.UNKNOWN
    assert len(order_mgr.get_unknown_orders()) == 1
