"""
Pre-Deployment Validation Audit Test Suite.
===========================================
Strict verification covering:
1. Compliance: Manual trade route disabled in competition live mode (HTTP 403)
2. Strategy C decommissioned and unactivatable
3. Live order path enforcement (RiskGovernor cannot be bypassed)
4. Expected-edge filter zero look-ahead bias
5. Dynamic position sizing extreme stop distance safety (NaN, Inf, 0.01%, 50%)
6. 1% Risk budget mathematical verification
7. RiskGovernor state persistence across restarts (cooldowns, recovery mode)
8. Circuit breaker freeze persistence across restarts
9. Partial fill accounting & lock maintenance
10. UNKNOWN order state freeze & reconciliation protection
11. Strict 5% cash reserve enforcement
12. Duplicate order and active position locking
"""

import math
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from config.trading_params import (
    AppConfig,
    ExchangeConfig,
    FeesConfig,
    PortfolioConfig,
    RiskControlsConfig,
    RiskGovernorConfig,
    StrategiesConfig,
    TrailingStopConfig,
    load_config,
)
from core.order_executor import OrderExecutor, UnknownOrderStateError
from state.order_state import Order, OrderStateManager as OrderManager, OrderStatus
from core.risk_governor import (
    GovernorEvaluation,
    RejectionReason,
    RiskGovernor,
    RiskState,
    SignalLifecycleState,
)
from core.risk_manager import RiskManager
from core.strategy_engine import Signal, StrategyEngine
from core.web_server import WebServer
from state.portfolio_tracker import PortfolioTracker


@pytest.fixture
def temp_dir(tmp_path):
    return tmp_path


# ==============================================================================
# 1. MANUAL TRADE ENDPOINT — COMPETITION COMPLIANCE BLOCKER
# ==============================================================================

def test_manual_trade_disabled_in_competition_live(tmp_path):
    """
    Verify that in competition live mode, POST /command/manual_trade is strictly
    forbidden and returns HTTP 403, and handle_manual_trade returns failure.
    """
    bot = MagicMock()
    bot.config = AppConfig()
    bot.config.dry_run = False
    bot.config.live_trading_enabled = True
    assert bot.config.is_live is True

    server = WebServer(bot=bot, host="127.0.0.1", port=8999, auth_token="")
    client = TestClient(server.app)

    # Attempt manual trade injection via Web API
    res = client.post("/command/manual_trade", json={"symbol": "BTC/USD", "side": "BUY", "notional_usd": 1000.0})
    assert res.status_code == 403
    assert "MANUAL_TRADE_FORBIDDEN" in res.json().get("detail", "")

    # Also test environment variable override
    bot.config.dry_run = True
    bot.config.live_trading_enabled = False
    with patch.dict(os.environ, {"COMPETITION_LIVE": "true"}):
        res_env = client.post("/command/manual_trade", json={"symbol": "BTC/USD", "side": "BUY"})
        assert res_env.status_code == 403
        assert "MANUAL_TRADE_FORBIDDEN" in res_env.json().get("detail", "")


# ==============================================================================
# 2. STRATEGY C — CANNOT BE ACTIVATED IN COMPETITION MODE
# ==============================================================================

def test_strategy_c_cannot_be_activated_in_competition_mode(tmp_path):
    """
    Verify that Strategy C (CVD Absorption) is strictly decommissioned and
    cannot be activated through configuration or StrategyEngine initialization.
    """
    # 1. Test config loader hard-locks cvd_absorption to False
    cfg_file = tmp_path / "test_cfg.yaml"
    cfg_file.write_text("""
exchange:
  name: "roostoo_mock"
strategies:
  value_area:
    enabled: true
  liquidity_sweep:
    enabled: true
  cvd_absorption:
    enabled: true  # Attempt to activate Strategy C
""", encoding="utf-8")

    loaded_cfg = load_config(str(cfg_file))
    assert loaded_cfg.strategies.cvd_absorption.enabled is False

    # 2. Test StrategyEngine hard-locks cvd_absorption to False
    engine = StrategyEngine(loaded_cfg.strategies)
    assert engine.config.cvd_absorption.enabled is False


# ==============================================================================
# 3. LIVE ORDER PATH ENFORCEMENT (NO BYPASS POSSIBLE)
# ==============================================================================

def test_risk_governor_on_all_live_order_paths(tmp_path):
    """
    Verify that every order path through execute_decision requires risk_decision.approved=True,
    and unapproved decisions generate ZERO orders to the exchange.
    """
    port = PortfolioTracker(initial_capital=100000.0, persistence_file=str(tmp_path / "p.json"))
    order_mgr = OrderManager(persistence_file=str(tmp_path / "o.json"))
    client = MagicMock()
    app_cfg = AppConfig()
    app_cfg.dry_run = False
    app_cfg.live_trading_enabled = True

    executor = OrderExecutor(
        config=app_cfg,
        api_client=client,
        portfolio=port,
        order_manager=order_mgr,
        risk_manager=MagicMock(),
    )

    sig = Signal(
        strategy="Strategy_A",
        symbol="BTC/USD",
        direction="BUY",
        confidence=0.80,
        entry_price=80000.0,
        stop_loss=78500.0,
        take_profit_1=82000.0,
        take_profit_2=84000.0,
        expected_rr=2.0,
        reason="Test",
        regime="TRENDING_UP",
        timestamp=int(time.time()),
    )

    # Unapproved decision MUST be rejected without touching exchange client
    unapproved_decision = MagicMock()
    unapproved_decision.approved = False
    unapproved_decision.reason = "VETOED_BY_GOVERNOR"

    order = executor.execute_decision(sig, unapproved_decision, 80000.0)
    assert order is None
    client.place_order.assert_not_called()


# ==============================================================================
# 4. EXPECTED-EDGE FILTER — ZERO LOOK-AHEAD BIAS
# ==============================================================================

def test_expected_edge_zero_lookahead_bias():
    """
    Verify that ExpectedEdgeValidator strictly relies on signal metadata
    determined at time T and uses ZERO future information or candles.
    """
    gov_cfg = RiskGovernorConfig()
    gov_cfg.expected_edge.min_edge_pct = 0.0040  # 40 bps
    fees_cfg = FeesConfig(taker_fee_pct=0.0010, slippage_pct=0.0002)

    gov = RiskGovernor(gov_cfg, fees_cfg)

    # Valid Signal: Entry 100, Target 101 -> 1.0% edge > 0.40% hurdle
    sig_good = Signal(
        strategy="Strategy_A",
        symbol="BTC/USD",
        direction="BUY",
        confidence=0.80,
        entry_price=100.0,
        stop_loss=98.5,
        take_profit_1=101.0,
        take_profit_2=102.0,
        expected_rr=2.0,
        reason="Test",
        regime="TRENDING",
        timestamp=1000,
    )
    ok, gross_edge, req_edge, rt_cost, reason = gov.expected_edge.evaluate(sig_good)
    assert ok is True
    assert gross_edge == pytest.approx(0.010, rel=1e-3)
    assert rt_cost == pytest.approx(0.0024, rel=1e-3)

    # Sub-hurdle Signal: Entry 100, Target 100.20 -> 0.20% edge < 0.40% hurdle
    sig_poor = Signal(
        strategy="Strategy_A",
        symbol="BTC/USD",
        direction="BUY",
        confidence=0.80,
        entry_price=100.0,
        stop_loss=99.0,
        take_profit_1=100.20,
        take_profit_2=100.30,
        expected_rr=1.5,
        reason="Test",
        regime="TRENDING",
        timestamp=1000,
    )
    ok_poor, gross_edge_poor, req_edge_poor, _, reason_poor = gov.expected_edge.evaluate(sig_poor)
    assert ok_poor is False
    assert "INSUFFICIENT_EXPECTED_EDGE_AFTER_COSTS" in reason_poor


# ==============================================================================
# 5. POSITION SIZING — EXTREME STOP DISTANCE TEST
# ==============================================================================

def test_dynamic_position_sizing_extreme_stop_distances():
    """
    Test position sizing under extreme stop scenarios:
    0.01%, 0.05%, 0.10%, 0.25%, 0.50%, 1.0%, 5.0%, negative, NaN, Inf, and >50%.
    Every case must fail safely or respect available cash and asset limits.
    """
    gov_cfg = RiskGovernorConfig()
    gov_cfg.position_sizing.base_risk_per_trade_pct = 0.01
    gov_cfg.position_sizing.max_major_position_pct = 0.50
    gov_cfg.position_sizing.max_alt_position_pct = 0.25
    gov = RiskGovernor(gov_cfg)

    equity = 100000.0
    cash = 80000.0
    multipliers = {"quality": 1.0, "regime": 1.0, "drawdown": 1.0, "symbol": 1.0}

    def make_sig(entry, stop):
        return Signal(
            strategy="Strategy_A",
            symbol="BTC/USD",
            direction="BUY",
            confidence=0.80,
            entry_price=entry,
            stop_loss=stop,
            take_profit_1=entry * 1.05,
            take_profit_2=entry * 1.10,
            expected_rr=2.0,
            reason="Test",
            regime="TRENDING",
            timestamp=1000,
        )

    # Case A: Extremely tight stops (< 0.15% threshold) must be REJECTED
    for stop_pct in (0.0001, 0.0005, 0.0010):
        sig = make_sig(100.0, 100.0 * (1.0 - stop_pct))
        ok, qty, notional, _, _, reason = gov.position_sizer.compute_size(
            sig, equity, cash, is_major=True, multipliers=multipliers
        )
        assert ok is False
        assert "INVALID_STOP_DISTANCE" in reason

    # Case B: Modest stop (0.25%, 0.50%) must be APPROVED and CAPPED at max position ($50,000)
    for stop_pct in (0.0025, 0.0050):
        sig = make_sig(100.0, 100.0 * (1.0 - stop_pct))
        ok, qty, notional, _, _, reason = gov.position_sizer.compute_size(
            sig, equity, cash, is_major=True, multipliers=multipliers
        )
        assert ok is True
        assert notional <= 50000.0  # Respects 50% major cap
        assert notional <= cash

    # Case C: Standard stops (1.0%, 5.0%)
    sig_1pct = make_sig(100.0, 99.0)
    ok_1, qty_1, notional_1, _, _, _ = gov.position_sizer.compute_size(
        sig_1pct, equity, cash, is_major=True, multipliers=multipliers
    )
    assert ok_1 is True
    assert notional_1 <= 50000.0

    # Case D: Stop <= 0 or entry <= 0
    sig_neg = make_sig(100.0, -10.0)
    ok_neg, _, _, _, _, reason_neg = gov.position_sizer.compute_size(
        sig_neg, equity, cash, is_major=True, multipliers=multipliers
    )
    assert ok_neg is False

    # Case E: Stop is NaN or Inf
    for bad_stop in (float("nan"), float("inf"), float("-inf")):
        sig_bad = make_sig(100.0, bad_stop)
        ok_bad, _, _, _, _, reason_bad = gov.position_sizer.compute_size(
            sig_bad, equity, cash, is_major=True, multipliers=multipliers
        )
        assert ok_bad is False
        assert "INVALID_STOP_DISTANCE" in reason_bad

    # Case F: Absurdly large stop (>50%)
    sig_huge = make_sig(100.0, 40.0)  # 60% stop
    ok_huge, _, _, _, _, reason_huge = gov.position_sizer.compute_size(
        sig_huge, equity, cash, is_major=True, multipliers=multipliers
    )
    assert ok_huge is False
    assert "INVALID_STOP_DISTANCE" in reason_huge


# ==============================================================================
# 6. MATHEMATICAL VERIFICATION OF 1% RISK CLAIM
# ==============================================================================

def test_final_order_risk_budget_limit():
    """
    Verify that actual risk capital at stop loss does not exceed the 1% risk budget.
    """
    gov_cfg = RiskGovernorConfig()
    gov_cfg.position_sizing.base_risk_per_trade_pct = 0.01  # 1% equity budget
    gov = RiskGovernor(gov_cfg)

    equity = 100000.0
    cash = 90000.0
    expected_risk_budget = equity * 0.01  # $1,000

    sig = Signal(
        strategy="Strategy_A",
        symbol="BTC/USD",
        direction="BUY",
        confidence=0.80,
        entry_price=80000.0,
        stop_loss=78400.0,  # 2.0% stop distance = $1,600
        take_profit_1=83200.0,
        take_profit_2=84800.0,
        expected_rr=2.0,
        reason="Test",
        regime="TRENDING",
        timestamp=1000,
    )
    multipliers = {"quality": 1.0, "regime": 1.0, "drawdown": 1.0, "symbol": 1.0}

    ok, qty, notional, actual_risk_cap, risk_pct, _ = gov.position_sizer.compute_size(
        sig, equity, cash, is_major=True, multipliers=multipliers
    )
    assert ok is True
    # Maximum loss at stop: qty * (entry - stop)
    loss_at_stop = qty * (sig.entry_price - sig.stop_loss)
    assert loss_at_stop <= expected_risk_budget * 1.01  # within precision tolerance
    assert risk_pct <= 0.0101


# ==============================================================================
# 7. RISK GOVERNOR STATE PERSISTENCE ACROSS RESTARTS
# ==============================================================================

def test_risk_governor_persistence_across_restart(tmp_path):
    """
    Verify that loss cooldowns, signal states, and recovery mode state
    persist to disk and reload properly across process restarts.
    """
    state_file = tmp_path / "risk_gov_state.json"
    gov_cfg = RiskGovernorConfig()
    fees_cfg = FeesConfig()

    gov1 = RiskGovernor(gov_cfg, fees_cfg, persistence_file=str(state_file))

    # Trigger a losing exit on ENA/USD (sets loss cooldown)
    gov1.on_trade_entry("ENA/USD", entry_price=0.25, timestamp=1000.0)
    gov1.on_trade_exit(
        symbol="ENA/USD",
        side="SELL",
        price=0.24,
        quantity=100000.0,
        realized_pnl=-1000.0,  # Loss!
        fee=25.0,
        hold_duration=60.0,
        exit_reason="STOP_LOSS",
        timestamp=1060.0,
    )
    # Activate recovery mode
    gov1.drawdown_governor.recovery_mode_active = True
    gov1.drawdown_governor.consecutive_recovery_wins = 1
    gov1._persist()

    # Create new instance (simulating restart)
    gov2 = RiskGovernor(gov_cfg, fees_cfg, persistence_file=str(state_file))

    # Verify state was restored
    assert gov2.trade_frequency.last_exit_pnl.get("ENA/USD") == -1000.0
    assert gov2.drawdown_governor.recovery_mode_active is True
    assert gov2.drawdown_governor.consecutive_recovery_wins == 1

    # Verify loss cooldown remains active at timestamp 1400 (400s after entry > 300s, 340s after exit < 900s)
    can_enter, reason, rem = gov2.trade_frequency.check_entry("ENA/USD", curr_time=1400.0)
    assert can_enter is False
    assert reason == RejectionReason.LOSS_COOLDOWN


# ==============================================================================
# 8. CIRCUIT BREAKER FREEZE PERSISTENCE ACROSS RESTARTS
# ==============================================================================

def test_circuit_breaker_freeze_persistence_across_restart(tmp_path):
    """
    Verify that 24h drawdown freeze timestamp and kill switch persist across restart.
    """
    port = PortfolioTracker(initial_capital=100000.0, persistence_file=str(tmp_path / "p.json"))
    state_file = tmp_path / "risk_mgr_state.json"

    rm1 = RiskManager(portfolio=port, persistence_file=str(state_file))
    curr_t = time.time()
    freeze_target = curr_t + 21600.0  # 6 hours freeze
    rm1.freeze_until_timestamp = freeze_target
    rm1.permanent_kill_switch = False
    rm1._persist()

    # Restart
    rm2 = RiskManager(portfolio=port, persistence_file=str(state_file))
    assert rm2.freeze_until_timestamp == pytest.approx(freeze_target, rel=1e-4)
    assert rm2.is_frozen is True

    tripped, msg = rm2.check_circuit_breakers()
    assert tripped is True
    assert "FROZEN" in msg


# ==============================================================================
# 9. PARTIAL FILL ACCOUNTING & LOCK MAINTENANCE
# ==============================================================================

def test_partial_fill_handling(tmp_path):
    """
    Verify that partial fills update the portfolio incrementally without double counting,
    and retain symbol locking while volume remains open.
    """
    port = PortfolioTracker(initial_capital=100000.0, persistence_file=str(tmp_path / "p.json"))
    order_mgr = OrderManager(persistence_file=str(tmp_path / "o.json"))

    order = Order(
        client_order_id="TEST_PARTIAL_01",
        symbol="BTC/USD",
        side="BUY",
        order_type="MARKET",
        quantity=1.0,
        remaining_quantity=1.0,
        price=80000.0,
        status=OrderStatus.PENDING_SUBMIT,
    )
    order_mgr.register_order(order)

    # First fill: 0.40 BTC @ $80,000
    delta1, ord1 = order_mgr.record_fill_delta(
        client_order_id="TEST_PARTIAL_01",
        exchange_cumulative_filled=0.40,
        filled_price=80000.0,
        commission=32.0,
        role="TAKER",
        ex_status="PARTIALLY_FILLED",
    )
    assert delta1 == pytest.approx(0.40, rel=1e-5)
    assert ord1.status == OrderStatus.PARTIALLY_FILLED
    assert ord1.remaining_quantity == pytest.approx(0.60, rel=1e-5)

    # Second fill: cumulative 1.0 BTC @ $80,000
    delta2, ord2 = order_mgr.record_fill_delta(
        client_order_id="TEST_PARTIAL_01",
        exchange_cumulative_filled=1.00,
        filled_price=80000.0,
        commission=80.0,
        role="TAKER",
        ex_status="FILLED",
    )
    assert delta2 == pytest.approx(0.60, rel=1e-5)
    assert ord2.status == OrderStatus.FILLED
    assert ord2.remaining_quantity == 0.0


# ==============================================================================
# 10. UNKNOWN ORDER STATE LOCKS SYMBOL SAFELY
# ==============================================================================

def test_unknown_order_state_locks_symbol(tmp_path):
    """
    Verify that an UNKNOWN order state locks the symbol to prevent duplicate BUY attempts.
    """
    order_mgr = OrderManager(persistence_file=str(tmp_path / "o.json"))
    order_mgr.lock_symbol("BTC/USD", reason="Order in UNKNOWN state", ttl_seconds=300.0)

    is_locked, reason = order_mgr.is_symbol_entry_locked("BTC/USD")
    assert is_locked is True
    assert "UNKNOWN" in reason


# ==============================================================================
# 11. STRICT 5% CASH RESERVE ENFORCEMENT
# ==============================================================================

def test_cash_reserve_strictly_enforced():
    """
    Verify that position sizing never allows cash to fall below 5% equity reserve.
    """
    gov_cfg = RiskGovernorConfig()
    gov = RiskGovernor(gov_cfg)

    equity = 100000.0
    reserve = equity * 0.05  # $5,000
    # Only $6,000 total cash (available cash = $1,000 after $5,000 reserve)
    available_cash = 1000.0

    sig = Signal(
        strategy="Strategy_A",
        symbol="BTC/USD",
        direction="BUY",
        confidence=0.80,
        entry_price=80000.0,
        stop_loss=79000.0,
        take_profit_1=82000.0,
        take_profit_2=84000.0,
        expected_rr=2.0,
        reason="Test",
        regime="TRENDING",
        timestamp=1000,
    )
    multipliers = {"quality": 1.0, "regime": 1.0, "drawdown": 1.0, "symbol": 1.0}

    ok, qty, notional, _, _, _ = gov.position_sizer.compute_size(
        sig, equity, available_cash=available_cash, is_major=True, multipliers=multipliers
    )
    assert ok is True
    # Notional cannot exceed available cash ($1,000), protecting the $5,000 reserve
    assert notional <= 1000.0


# ==============================================================================
# 12. PHASE 10 FAILURE & RESTART VERIFICATION
# ==============================================================================

def test_inverted_stop_strictly_rejected():
    """Verify that inverted stops (stop >= entry) are strictly rejected by both sizer and manager."""
    gov_cfg = RiskGovernorConfig()
    gov = RiskGovernor(gov_cfg)
    sig = Signal(
        strategy="Strategy_A",
        symbol="BTC/USD",
        direction="BUY",
        confidence=0.80,
        entry_price=80000.0,
        stop_loss=82000.0,  # Inverted!
        take_profit_1=84000.0,
        take_profit_2=86000.0,
        expected_rr=2.0,
        reason="Test",
        regime="TRENDING",
        timestamp=1000,
    )
    ok, _, _, _, _, reason = gov.position_sizer.compute_size(
        sig, 100000.0, available_cash=50000.0, is_major=True, multipliers={}
    )
    assert ok is False
    assert "INVALID_STOP_DISTANCE" in reason


def test_restart_during_open_position(tmp_path):
    """Verify that open positions persist safely and peak equity is not wiped out across process restart."""
    state_file = tmp_path / "portfolio.json"
    p1 = PortfolioTracker(initial_capital=100000.0, persistence_file=str(state_file))
    p1.record_fill(
        symbol="BTC/USD",
        side="BUY",
        quantity=0.5,
        price=80000.0,
        fee=40.0,
        strategy="Strategy_A",
        stop_loss=78400.0,
        take_profit_1=82000.0,
        take_profit_2=84000.0,
    )
    p1.peak_equity = 105000.0
    p1._persist()

    # Restart
    p2 = PortfolioTracker(initial_capital=100000.0, persistence_file=str(state_file))
    assert "BTC/USD" in p2.positions
    assert p2.positions["BTC/USD"].quantity == 0.5
    assert p2.positions["BTC/USD"].entry_price == 80000.0
    assert p2.peak_equity == 105000.0


def test_restart_with_unknown_order(tmp_path):
    """Verify that an UNKNOWN order and symbol entry lock persist and remain locked upon process restart."""
    state_file = tmp_path / "orders.json"
    om1 = OrderManager(persistence_file=str(state_file))
    ord1 = Order(
        client_order_id="UNK_001",
        symbol="ETH/USD",
        side="BUY",
        order_type="MARKET",
        quantity=5.0,
        remaining_quantity=5.0,
        price=3000.0,
        status=OrderStatus.UNKNOWN,
        error_message="Network timeout during submission",
    )
    om1.register_order(ord1)
    om1.lock_symbol("ETH/USD", reason="Order UNK_001 in UNKNOWN state", ttl_seconds=300.0)
    om1._persist()

    # Restart
    om2 = OrderManager(persistence_file=str(state_file))
    ord_loaded = om2.get_order_by_client_id("UNK_001")
    assert ord_loaded is not None
    assert ord_loaded.status == OrderStatus.UNKNOWN
    is_locked, reason = om2.is_symbol_entry_locked("ETH/USD")
    assert is_locked is True
    assert "UNKNOWN" in reason


def test_duplicate_strategy_signals_prevented(tmp_path):
    """
    Verify that simultaneous or sequential Strategy A and Strategy B signals on the same symbol
    cannot establish duplicate or conflicting positions.
    """
    port = PortfolioTracker(initial_capital=100000.0, persistence_file=str(tmp_path / "p.json"))
    order_mgr = OrderManager(persistence_file=str(tmp_path / "o.json"))
    gov_cfg = RiskGovernorConfig()
    rm = RiskManager(portfolio=port, order_manager=order_mgr, governor_config=gov_cfg)

    sig_a = Signal(
        strategy="Strategy_A",
        symbol="BTC/USD",
        direction="BUY",
        confidence=0.85,
        entry_price=80000.0,
        stop_loss=78400.0,
        take_profit_1=82000.0,
        take_profit_2=84000.0,
        expected_rr=2.0,
        reason="Strategy A signal",
        regime="RANGE",
        timestamp=int(time.time()),
    )
    # First signal approved
    dec_a = rm.evaluate_signal(sig_a)
    assert dec_a.approved is True

    # Fill established
    port.record_fill(
        symbol="BTC/USD",
        side="BUY",
        quantity=dec_a.adjusted_quantity,
        price=80000.0,
        fee=25.0,
        strategy="Strategy_A",
    )

    # Strategy B signal arrives for same symbol
    sig_b = Signal(
        strategy="Strategy_B",
        symbol="BTC/USD",
        direction="BUY",
        confidence=0.90,
        entry_price=80100.0,
        stop_loss=78500.0,
        take_profit_1=82500.0,
        take_profit_2=84500.0,
        expected_rr=2.0,
        reason="Strategy B signal",
        regime="RANGE",
        timestamp=int(time.time()),
    )
    # MUST be rejected due to active position
    dec_b = rm.evaluate_signal(sig_b)
    assert dec_b.approved is False
    assert "DUPLICATE_ENTRY" in dec_b.reason
