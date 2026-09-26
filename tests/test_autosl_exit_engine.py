"""
Unit and Integration Tests for AutoSL Exit Engine.
Covers:
1. Two-phase position lifecycle (Phase 1 Validation Window vs Phase 2 Validated Trailing).
2. The 4-condition confluence check for FAILED_BREAKOUT_EXIT (Long & Short).
3. Confluence fail-safe (does not trigger if any of the 4 conditions is false).
4. Prevention of the 0-second boundary trap in candle progression.
5. Phase 1 early graduation (+2.0% profit) and normal graduation (> 2 candles).
6. Iron Rule of Dynamic Trailing SL: Stop loss only tightens, NEVER widens.
7. Multi-tier profit protection tightening (+1.5%, +3%, +5%, +10%).
8. Step-based hard exchange order ratchet (discrete points, rate-limit safe).
9. Fast Momentum Take-Profit Extension & 50% target distance profit lock ratchet.
10. Max extension transition to FINAL_TRAILING mode.
11. Clean exchange execution integration and opposing order cancellation.
"""

from datetime import datetime, timedelta, timezone
import pytest

from core.autosl_exit_engine import AutoSLExitEngine, CryptoPosition
from state.portfolio_tracker import Position, PortfolioTracker
from state.order_state import Order, OrderStateManager, OrderStatus
from core.order_executor import OrderExecutor
from core.risk_manager import RiskDecision, RiskManager
from core.strategy_engine import Signal
from config.trading_params import AppConfig, AutoSLConfig


# =============================================================================
# FIXTURES
# =============================================================================
@pytest.fixture
def exit_engine():
    config = {
        "validation_candles": 2,
        "candle_timeframe_seconds": 60,
        "min_expansion_percent": 1.0,
        "phase2_profit_activation": 2.0,
        "hard_sl_trailing_step": 10.0,
        "hard_sl_step_usd": {
            "BTC/USD": 50.0,
            "ETH/USD": 4.0,
        },
        "recalc_interval_seconds": 60.0,
        "min_tick_buffer": 0.5,
        "tick_size": 0.1,
        "profit_protection_tiers": [
            {"profit_pct": 10.0, "sl_percent": 0.4},
            {"profit_pct": 5.0,  "sl_percent": 0.6},
            {"profit_pct": 3.0,  "sl_percent": 0.9},
            {"profit_pct": 1.5,  "sl_percent": 1.2},
        ],
        "momentum_extension_enabled": True,
        "fast_target_max_seconds": 180.0,
        "max_extensions": 3,
        "profit_lock_ratio": 0.50,
        "extreme_range_threshold_pct": 0.2,
    }
    return AutoSLExitEngine(config=config)


@pytest.fixture
def long_position():
    t0 = datetime(2026, 9, 26, 10, 0, 0, tzinfo=timezone.utc)
    return CryptoPosition(
        position_id="test_long_1",
        symbol="BTC/USD",
        side="BUY",
        quantity=1.0,
        entry_price=84000.0,
        entry_time=t0,
        entry_breakout_level=83950.0,
        initial_stop_loss=82320.0,  # -2.0% full stop loss
        stop_loss_price=82320.0,
        take_profit_price=85680.0,  # +2.0% target
        broker_sl_price=82320.0,
        sl_percent=2.0,
        status="OPEN",
        last_candle_time=t0,
    )


@pytest.fixture
def short_position():
    t0 = datetime(2026, 9, 26, 10, 0, 0, tzinfo=timezone.utc)
    return CryptoPosition(
        position_id="test_short_1",
        symbol="BTC/USD",
        side="SELL",
        quantity=1.0,
        entry_price=84000.0,
        entry_time=t0,
        entry_breakout_level=84050.0,
        initial_stop_loss=85680.0,  # +2.0% full stop loss
        stop_loss_price=85680.0,
        take_profit_price=82320.0,  # -2.0% target
        broker_sl_price=85680.0,
        sl_percent=2.0,
        status="OPEN",
        last_candle_time=t0,
    )


# =============================================================================
# TEST 1: Phase 1 Validation Window - 4-Condition Confluence Check (Long)
# =============================================================================
def test_long_failed_breakout_exit_confluence(exit_engine, long_position):
    """
    LONG Failed Breakout Exit triggers when ALL 4 conditions are met:
    1. current_price < entry_price (83900 < 84000)
    2. failed_expansion: peak (84200) < 1.0% expansion (+0.23% < 1.0%)
    3. lost_breakout: current_price <= entry_breakout_level (83900 <= 83950)
    4. volume_drop_detected == True
    """
    pos = long_position
    pos.candles_since_entry = 1
    pos.volume_drop_detected = True
    pos.peak_price = 84200.0  # Only expanded +0.238%

    reason, trigger_px = exit_engine.on_tick(
        pos=pos,
        current_price=83900.0,  # Dropped below entry & lost breakout level 83950
        current_candle_vol=50.0,
        prev_completed_vol=80.0,
        prev_prev_completed_vol=120.0,  # Volume dropped
        recent_market_high=84300.0,
        recent_market_low=83800.0,
    )

    assert reason == "FAILED_BREAKOUT_EXIT"
    assert trigger_px == 83900.0
    assert pos.status == "EXITING"
    assert pos.exit_reason == "FAILED_BREAKOUT_EXIT"


# =============================================================================
# TEST 2: Phase 1 Validation Window - 4-Condition Confluence Check (Short)
# =============================================================================
def test_short_failed_breakout_exit_confluence(exit_engine, short_position):
    """
    SHORT Failed Breakout Exit triggers when ALL 4 conditions are met:
    1. current_price > entry_price (84100 > 84000)
    2. failed_expansion: peak (83800) < 1.0% expansion (+0.23% < 1.0%)
    3. lost_breakout: current_price >= entry_breakout_level (84100 >= 84050)
    4. volume_drop_detected == True
    """
    pos = short_position
    pos.candles_since_entry = 1
    pos.volume_drop_detected = True
    pos.peak_price = 83800.0  # Lowest price reached only expanded +0.238%

    reason, trigger_px = exit_engine.on_tick(
        pos=pos,
        current_price=84100.0,  # Rose above entry and surrendered breakout 84050
        current_candle_vol=50.0,
        prev_completed_vol=60.0,
        prev_prev_completed_vol=100.0,
        recent_market_high=84200.0,
        recent_market_low=83750.0,
    )

    assert reason == "FAILED_BREAKOUT_EXIT"
    assert trigger_px == 84100.0
    assert pos.status == "EXITING"


# =============================================================================
# TEST 3: Confluence Fail-Safe - Any Single False Condition Aborts Exit
# =============================================================================
def test_confluence_failsafe_expansion_achieved(exit_engine, long_position):
    """If trade achieved >= 1.0% expansion, it proved itself; confluence fails."""
    pos = long_position
    pos.candles_since_entry = 1
    pos.volume_drop_detected = True
    pos.peak_price = 84900.0  # +1.07% expansion!

    reason, trigger_px = exit_engine.on_tick(
        pos=pos,
        current_price=83900.0,
        current_candle_vol=50.0,
        prev_completed_vol=50.0,
        prev_prev_completed_vol=100.0,
    )
    # Even though price dropped below entry, failed_expansion is FALSE
    assert reason is None
    assert pos.status == "OPEN"


def test_confluence_failsafe_volume_sustaining(exit_engine, long_position):
    """If volume is sustaining, confluence fails."""
    pos = long_position
    pos.candles_since_entry = 1
    pos.volume_drop_detected = False  # Volume did NOT drop
    pos.peak_price = 84100.0

    reason, _ = exit_engine.on_tick(
        pos=pos,
        current_price=83900.0,
        current_candle_vol=150.0,
        prev_completed_vol=120.0,
        prev_prev_completed_vol=100.0,  # Volume growing
    )
    assert reason is None
    assert pos.status == "OPEN"


def test_confluence_failsafe_first_candle_skipped(exit_engine, long_position):
    """On entry candle (candles_since_entry == 0), breakout failure check is skipped."""
    pos = long_position
    pos.candles_since_entry = 0
    pos.volume_drop_detected = True

    reason, _ = exit_engine.on_tick(
        pos=pos,
        current_price=83900.0,
        current_candle_vol=50.0,
        prev_completed_vol=50.0,
        prev_prev_completed_vol=100.0,
    )
    assert reason is None
    assert pos.status == "OPEN"


# =============================================================================
# TEST 4: Avoid the 0-Second Trap
# =============================================================================
def test_avoid_zero_second_trap(exit_engine, long_position):
    """Ensure candles_since_entry only increments when candle_timeframe_seconds elapsed."""
    pos = long_position
    t0 = pos.entry_time

    # 10 seconds later: candle counter must NOT increment
    t_10s = t0 + timedelta(seconds=10)
    exit_engine.on_tick(pos, 84050.0, current_time=t_10s)
    assert pos.candles_since_entry == 0

    # 65 seconds later: candle counter MUST increment
    t_65s = t0 + timedelta(seconds=65)
    exit_engine.on_tick(pos, 84050.0, current_time=t_65s, prev_completed_vol=50.0, prev_prev_completed_vol=80.0)
    assert pos.candles_since_entry == 1
    assert pos.volume_drop_detected is True


# =============================================================================
# TEST 5: Early Graduation to Phase 2 (+2.0% profit)
# =============================================================================
def test_early_graduation_to_phase_2(exit_engine, long_position):
    """When price explodes into +2.0% profit, position graduates to Phase 2 immediately."""
    pos = long_position
    pos.take_profit_price = 88000.0  # Higher TP so it doesn't trigger momentum extension
    pos.candles_since_entry = 1
    assert pos.validation_survived is False

    # Price moves to +2.05% profit (84000 * 1.0205 = 85722)
    reason, _ = exit_engine.on_tick(pos, 85722.0)
    assert pos.validation_survived is True
    assert pos.phase == "PHASE_2_VALIDATED"


# =============================================================================
# TEST 6: Normal Graduation to Phase 2 (> validation_candles)
# =============================================================================
def test_normal_graduation_after_validation_window(exit_engine, long_position):
    """After validation_candles (2) have closed, position transitions to Phase 2."""
    pos = long_position
    pos.candles_since_entry = 3  # > 2
    exit_engine.on_tick(pos, 84050.0)
    assert pos.validation_survived is True
    assert pos.phase == "PHASE_2_VALIDATED"


# =============================================================================
# TEST 7: Iron Rule of Dynamic Trailing SL (Never Widens, Only Tightens)
# =============================================================================
def test_iron_rule_never_widen(exit_engine, long_position):
    """Dynamic SL percent must only tighten as profit climbs, NEVER widen."""
    pos = long_position
    pos.validation_survived = True
    pos.sl_percent = 1.2
    t0 = datetime.now(timezone.utc)

    # If profit is low (< 1.5%), recommended SL would be 2.0%, but pos.sl_percent is 1.2%
    # Iron Rule: KEEP 1.2%!
    exit_engine._recalculate_dynamic_sl(pos, t0, profit_pct=0.5)
    assert pos.sl_percent == 1.2

    # If profit climbs to +6.0%, tier triggers 0.6% -> tightens!
    t1 = t0 + timedelta(seconds=70)
    exit_engine._recalculate_dynamic_sl(pos, t1, profit_pct=6.0)
    assert pos.sl_percent == 0.6

    # If profit pulls back to +2.0%, SL% must STAY at 0.6%, NEVER loosen!
    t2 = t1 + timedelta(seconds=70)
    exit_engine._recalculate_dynamic_sl(pos, t2, profit_pct=2.0)
    assert pos.sl_percent == 0.6


# =============================================================================
# TEST 8: Profit Protection Tiers & Soft Trailing Ratchet
# =============================================================================
def test_profit_protection_tiers_tightening(exit_engine, long_position):
    pos = long_position
    pos.validation_survived = True
    pos.peak_price = 84000.0
    pos.stop_loss_price = 82320.0
    now = datetime.now(timezone.utc)

    # 1. At +1.6% profit -> SL% tightens to 1.2%
    exit_engine._recalculate_dynamic_sl(pos, now, profit_pct=1.6)
    assert pos.sl_percent == 1.2

    # Peak price climbs to 85500. Soft trailing stop ratchets: 85500 * (1 - 0.012) = 84474
    pos.peak_price = 85500.0
    tightened = exit_engine._update_soft_trailing_sl(pos, tick_size=0.1)
    assert tightened is True
    assert pos.stop_loss_price == 84474.0
    assert pos.trailing_sl_active is True
    assert pos.locked_profit > 0

    # 2. At +5.2% profit -> SL% tightens to 0.6%
    now2 = now + timedelta(seconds=70)
    exit_engine._recalculate_dynamic_sl(pos, now2, profit_pct=5.2)
    assert pos.sl_percent == 0.6

    # Peak price climbs to 88500. Soft stop: 88500 * (1 - 0.006) = 87969
    pos.peak_price = 88500.0
    exit_engine._update_soft_trailing_sl(pos, tick_size=0.1)
    assert pos.stop_loss_price == 87969.0


# =============================================================================
# TEST 9: Step-Based Hard Exchange Order Ratchet (Layer A)
# =============================================================================
def test_hard_broker_sl_step_ratchet(exit_engine, long_position):
    """
    On BTC, hard stop ratchets in discrete $50 steps.
    Prevents API rate limit exhaustion while guaranteeing safety.
    """
    pos = long_position
    pos.broker_sl_price = 82320.0
    pos.peak_profit_points = 0.0

    # Move profit points by $45 (< $50 step): should not ratchet
    moved = exit_engine._update_hard_broker_sl(pos, profit_points=45.0, step=50.0, tick_size=0.1)
    assert moved is False
    assert pos.broker_sl_price == 82320.0

    # Move profit points by $110 (2 full $50 steps = $100): ratchets forward by $100
    moved = exit_engine._update_hard_broker_sl(pos, profit_points=110.0, step=50.0, tick_size=0.1)
    assert moved is True
    assert pos.broker_sl_price == 82420.0
    assert pos.peak_profit_points == 100.0


# =============================================================================
# TEST 10: Fast Momentum Extension & 50% Profit Ratchet
# =============================================================================
def test_fast_momentum_tp_extension(exit_engine, long_position):
    """
    When TP is reached in < 180s and momentum confirms (price >= recent_high * 0.998):
    1. Extends TP by original_target_distance.
    2. Ratchets stop to lock 50% of target distance per level.
    """
    pos = long_position
    pos.take_profit_price = 85680.0  # +$1,680 target
    pos.original_target_distance = 1680.0
    pos.stop_loss_price = 84000.0
    now = pos.entry_time + timedelta(seconds=120)  # Reached within 120s (< 180s)

    # Check momentum extension when current_price == TP (85680)
    reason, trigger_px = exit_engine.on_tick(
        pos=pos,
        current_price=85680.0,
        recent_market_high=85700.0,  # Momentum driving extreme high
        current_time=now,
    )

    # Should NOT exit! Should extend TP!
    assert reason is None
    assert pos.extension_level == 1
    assert pos.momentum_status == "MOMENTUM_EXTENSION_1"
    # New TP = 85680 + 1680 = 87360
    assert pos.take_profit_price == 87360.0
    # Ratcheted stop locks 50% of target = 84000 + (1680 * 1 * 0.50) = 84840
    assert pos.stop_loss_price == 84840.0


def test_momentum_transition_to_final_trailing(exit_engine, long_position):
    """After max extensions (3), switches to FINAL_TRAILING mode."""
    pos = long_position
    pos.original_target_distance = 1680.0
    pos.extension_level = 2
    pos.take_profit_price = 89040.0
    now = pos.entry_time + timedelta(seconds=150)

    exit_engine.on_tick(
        pos=pos,
        current_price=89040.0,
        recent_market_high=89050.0,
        current_time=now,
    )

    assert pos.extension_level == 3
    assert pos.momentum_status == "FINAL_TRAILING"
    assert pos.take_profit_price == float("inf")


def test_regular_tp_hit_without_momentum(exit_engine, long_position):
    """If TP reached after 180s or without momentum confirmation, triggers TP_HIT."""
    pos = long_position
    pos.take_profit_price = 85680.0
    # Reached after 300s (> 180s max)
    late_time = pos.entry_time + timedelta(seconds=300)

    reason, trigger_px = exit_engine.on_tick(
        pos=pos,
        current_price=85680.0,
        recent_market_high=86500.0,
        current_time=late_time,
    )

    assert reason == "TP_HIT"
    assert trigger_px == 85680.0
    assert pos.status == "EXITING"


# =============================================================================
# TEST 11: Order Executor Opposing Order Cancellation & Execution Integration
# =============================================================================
def test_order_executor_cancellation_and_exit(tmp_path):
    config = AppConfig()
    config.dry_run = True
    config.live_trading_enabled = False

    order_mgr = OrderStateManager(persistence_file=str(tmp_path / "orders.json"))
    portfolio = PortfolioTracker(initial_capital=100000.0, persistence_file=str(tmp_path / "portfolio.json"))
    risk_mgr = RiskManager(portfolio=portfolio)
    executor = OrderExecutor(
        config=config,
        api_client=None,
        portfolio=portfolio,
        order_manager=order_mgr,
        risk_manager=risk_mgr,
    )

    # 1. Open a position via DRY_RUN fill
    portfolio.record_fill(
        symbol="BTC/USD",
        side="BUY",
        quantity=0.5,
        price=84000.0,
        fee=21.0,
        stop_loss=82320.0,
        take_profit_1=85680.0,
        take_profit_2=87360.0,
        entry_breakout_level=83950.0,
    )

    # 2. Register an active resting limit order for BTC/USD
    pending_ord = Order(
        client_order_id="RST_TEST_BTC_001",
        symbol="BTC/USD",
        side="BUY",
        order_type="LIMIT",
        quantity=0.2,
        price=83000.0,
        status=OrderStatus.PENDING_EXCHANGE,
    )
    order_mgr.register_order(pending_ord)
    assert len(order_mgr.get_active_orders("BTC/USD")) == 1

    # 3. Position triggers FAILED_BREAKOUT_EXIT -> Execute de-risk
    exit_sig = Signal(
        strategy="AUTOSL_ENGINE",
        symbol="BTC/USD",
        direction="DE_RISK",
        confidence=1.0,
        entry_price=83800.0,
        stop_loss=0.0,
        take_profit_1=0.0,
        take_profit_2=0.0,
        expected_rr=0.0,
        reason="[FAILED_BREAKOUT_EXIT] Early invalidation",
        regime="RANGE",
        timestamp=int(datetime.now(timezone.utc).timestamp() * 1000),
    )
    risk_dec = risk_mgr.evaluate_signal(exit_sig)
    assert risk_dec.approved is True

    # Execute decision: should cancel resting orders and close position
    exit_order = executor.execute_decision(exit_sig, risk_dec, current_market_price=83800.0)

    assert exit_order is not None
    assert exit_order.side == "SELL"
    assert exit_order.status == OrderStatus.FILLED

    # Verify opposing resting order was cancelled
    assert pending_ord.status == OrderStatus.CANCELED
    assert len(order_mgr.get_active_orders("BTC/USD")) == 0

    # Verify position in portfolio is closed
    assert "BTC/USD" not in portfolio.positions
