"""
Unit tests for the Risk Governor & Portfolio Protection Layer (Features 1-12).
==============================================================================
Validates:
1. Trade frequency (minimum hold, entry spacing, emergency exit bypass)
2. Loss cooldown (900s loss cooldown, 120s profit cooldown, expiry, symbol isolation)
3. Signal freshness (WAIT_FOR_RESET prevents repeat entries, reset unlocks)
4. Minimum expected edge (0.40% hurdle vs round-trip costs)
5. Dynamic position sizing (Majors vs Alts, stop distance, cash reserve)
6. Portfolio drawdown governor (NORMAL, CAUTION, REDUCED_RISK, DEFENSIVE, EMERGENCY)
7. Recovery mode (activation, de-risking, gradual confirmed recovery)
8. Correlated exposure & Altcoin limits (max 1 altcoin, 50% alt cap)
9. Symbol performance throttling (consecutive losses cut size, streak cooldown)
10. Strategy attribution (Strategy A & B active, Strategy C inactive)
"""

import pytest
import time
from unittest.mock import MagicMock

from config.trading_params import (
    AppConfig,
    FeesConfig,
    RiskControlsConfig,
    RiskGovernorConfig,
    StrategiesConfig,
    TrailingStopConfig,
)
from core.risk_governor import (
    GovernorEvaluation,
    RejectionReason,
    RiskGovernor,
    RiskState,
    SignalLifecycleState,
)
from core.risk_manager import RiskManager
from core.strategy_engine import Signal, StrategyEngine
from state.portfolio_tracker import PortfolioTracker


@pytest.fixture
def portfolio(tmp_path):
    p = tmp_path / "port_gov.json"
    return PortfolioTracker(initial_capital=100000.0, min_cash_reserve_pct=0.05, persistence_file=str(p))


@pytest.fixture
def governor_config():
    cfg = RiskGovernorConfig()
    cfg.trade_frequency.minimum_hold_seconds = 300.0
    cfg.trade_frequency.minimum_seconds_between_same_symbol_entries = 300.0
    cfg.trade_frequency.cooldown_after_loss_seconds = 900.0
    cfg.trade_frequency.cooldown_after_profit_seconds = 120.0
    cfg.expected_edge.min_edge_pct = 0.0040
    cfg.position_sizing.max_major_position_pct = 0.50
    cfg.position_sizing.max_alt_position_pct = 0.25
    return cfg


@pytest.fixture
def risk_manager(portfolio, governor_config):
    return RiskManager(
        portfolio=portfolio,
        risk_config=RiskControlsConfig(
            rolling_24h_drawdown_limit=0.035,
            freeze_duration_hours=6.0,
            max_drawdown_limit=0.06,
            enforce_circuit_breakers=True,
        ),
        trailing_config=TrailingStopConfig(breakeven_trigger_r=1.0, trail_activation_r=2.0),
        max_risk_per_trade_pct=0.01,
        max_gross_exposure_pct=1.00,
        min_cash_reserve_pct=0.05,
        max_open_positions=2,
        governor_config=governor_config,
        fees_config=FeesConfig(maker_fee_pct=0.0005, taker_fee_pct=0.0010, slippage_pct=0.0002),
    )


# ==============================================================================
# FEATURE 1: TRADE FREQUENCY GOVERNOR & EXIT SAFETY
# ==============================================================================

def test_trade_frequency_minimum_hold_and_emergency_bypass(risk_manager):
    """
    Voluntary exits before 300s are blocked, but emergency SL/AutoSL exits pass unconditionally.
    """
    now = time.time()
    tf_gov = risk_manager.governor.trade_frequency

    # Discretionary normal exit held for only 60 seconds -> Blocked
    allowed, reason = tf_gov.check_exit(
        symbol="BTC/USD",
        exit_reason="DISCRETIONARY_EXIT",
        curr_time=now,
        entry_time=now - 60.0,
    )
    assert allowed is False
    assert "MINIMUM_HOLD_ACTIVE" in reason

    # AutoSL exit held for only 30 seconds -> Unconditionally bypassed!
    allowed, reason = tf_gov.check_exit(
        symbol="BTC/USD",
        exit_reason="AUTOSL_FAILED_BREAKOUT",
        curr_time=now,
        entry_time=now - 30.0,
    )
    assert allowed is True
    assert "BYPASS" in reason

    # Hard Stop Loss exit held for 10 seconds -> Unconditionally bypassed!
    allowed, reason = tf_gov.check_exit(
        symbol="BTC/USD",
        exit_reason="STOP_LOSS_HIT",
        curr_time=now,
        entry_time=now - 10.0,
    )
    assert allowed is True
    assert "BYPASS" in reason


def test_trade_frequency_same_symbol_entry_spacing(risk_manager):
    """
    Cannot enter the same symbol within 300s of prior entry.
    """
    now = time.time()
    sig = Signal(
        strategy="VALUE_AREA",
        symbol="BTC/USD",
        direction="BUY",
        confidence=0.85,
        entry_price=50000.0,
        stop_loss=49000.0,
        take_profit_1=52000.0,
        take_profit_2=53000.0,
        expected_rr=2.0,
        reason="Test",
        regime="RANGE",
        timestamp=int(now * 1000),
    )

    # 1. First entry: approved
    dec1 = risk_manager.evaluate_signal(sig)
    assert dec1.approved is True

    # Record entry
    risk_manager.record_trade_fill("BTC/USD", "BUY", 1.0, 50000.0, timestamp=now)

    # 2. Immediate re-entry attempt 30 seconds later -> Blocked
    dec2 = risk_manager.evaluate_signal(sig)
    assert dec2.approved is False
    assert RejectionReason.TRADE_FREQUENCY_COOLDOWN in dec2.reason or "DUPLICATE" in dec2.reason


# ==============================================================================
# FEATURE 2: LOSS COOLDOWN & SYMBOL ISOLATION
# ==============================================================================

def test_loss_cooldown_activation_and_expiry(risk_manager):
    """
    Losing trade activates 900s cooldown; expires correctly.
    """
    now = time.time()
    # Simulate trade exit with loss (-$500)
    risk_manager.record_trade_fill(
        symbol="ENA/USD",
        side="SELL",
        quantity=10000.0,
        price=0.50,
        fee=5.0,
        realized_pnl=-500.0,
        hold_duration=120.0,
        exit_reason="AUTOSL_SL_HIT",
        timestamp=now,
    )

    sig_ena = Signal(
        strategy="LIQUIDITY_SWEEP",
        symbol="ENA/USD",
        direction="BUY",
        confidence=0.85,
        entry_price=0.50,
        stop_loss=0.48,
        take_profit_1=0.54,
        take_profit_2=0.56,
        expected_rr=2.0,
        reason="Test",
        regime="RANGE",
        timestamp=int(now * 1000),
    )

    # Attempt entry 60 seconds later -> Rejected due to LOSS_COOLDOWN
    dec = risk_manager.evaluate_signal(sig_ena)
    assert dec.approved is False
    assert RejectionReason.LOSS_COOLDOWN in dec.reason

    # Verify telemetry shows active cooldown
    telem = risk_manager.get_governor_telemetry()
    assert "ENA/USD" in telem["active_cooldowns"]
    assert telem["active_cooldowns"]["ENA/USD"]["reason"] == "LOSS_COOLDOWN"

    # Fast forward past 900s cooldown (t = now + 950s)
    tf_ok, tf_reason, _ = risk_manager.governor.trade_frequency.check_entry("ENA/USD", now + 950.0)
    assert tf_ok is True


def test_profit_cooldown_shorter_than_loss(risk_manager):
    """
    Profitable trade uses shorter 120s cooldown rather than 900s.
    """
    now = time.time()
    # Exit with profit (+$300)
    risk_manager.record_trade_fill(
        symbol="ADA/USD",
        side="SELL",
        quantity=5000.0,
        price=0.80,
        fee=4.0,
        realized_pnl=300.0,
        hold_duration=600.0,
        exit_reason="TP1_HIT",
        timestamp=now,
    )

    # Check cooldown duration
    cds = risk_manager.governor.trade_frequency.get_active_cooldowns(now + 10.0)
    assert "ADA/USD" in cds
    assert cds["ADA/USD"]["reason"] == "PROFIT_COOLDOWN"
    assert cds["ADA/USD"]["remaining_seconds"] <= 120.0

    # At t = now + 150s, profit cooldown has expired!
    tf_ok, _, _ = risk_manager.governor.trade_frequency.check_entry("ADA/USD", now + 150.0)
    assert tf_ok is True


def test_symbol_isolation_cooldowns(risk_manager):
    """
    ENA's loss cooldown does NOT block an entry on BTC/USD or SOL/USD.
    """
    now = time.time()
    # ENA enters loss cooldown
    risk_manager.record_trade_fill(
        symbol="ENA/USD", side="SELL", quantity=10000.0, price=0.50, realized_pnl=-600.0, timestamp=now
    )

    # Candidate signal on BTC/USD
    sig_btc = Signal(
        strategy="VALUE_AREA",
        symbol="BTC/USD",
        direction="BUY",
        confidence=0.85,
        entry_price=50000.0,
        stop_loss=49000.0,
        take_profit_1=52000.0,
        take_profit_2=53000.0,
        expected_rr=2.0,
        reason="Test",
        regime="RANGE",
        timestamp=int(now * 1000),
    )

    dec_btc = risk_manager.evaluate_signal(sig_btc)
    assert dec_btc.approved is True, f"BTC incorrectly blocked: {dec_btc.reason}"


# ==============================================================================
# FEATURE 3: SIGNAL FRESHNESS & RESET LIFECYCLE
# ==============================================================================

def test_signal_freshness_lifecycle_wait_for_reset(risk_manager):
    """
    A persistent condition after an exit must WAIT_FOR_RESET before a new entry is accepted.
    """
    now = time.time()
    sig = Signal(
        strategy="VALUE_AREA",
        symbol="BTC/USD",
        direction="BUY",
        confidence=0.85,
        entry_price=50000.0,
        stop_loss=49000.0,
        take_profit_1=52000.0,
        take_profit_2=53000.0,
        expected_rr=2.0,
        reason="Test",
        regime="RANGE",
        timestamp=int(now * 1000),
    )

    # 1. Entry
    risk_manager.record_trade_fill("BTC/USD", "BUY", 1.0, 50000.0, timestamp=now)
    assert risk_manager.governor.signal_lifecycle.get_state("BTC/USD") == SignalLifecycleState.ACTIVE_SIGNAL

    # 2. Exit
    risk_manager.record_trade_fill("BTC/USD", "SELL", 1.0, 50050.0, realized_pnl=50.0, timestamp=now + 60.0)
    assert risk_manager.governor.signal_lifecycle.get_state("BTC/USD") == SignalLifecycleState.WAIT_FOR_RESET

    # 3. Next tick has same signal at same price -> Blocked awaiting reset
    fresh_ok, reason = risk_manager.governor.signal_lifecycle.check_signal_freshness(sig, now + 150.0)
    assert fresh_ok is False
    assert RejectionReason.STALE_SIGNAL_AWAITING_RESET in reason

    # 4. Market bar produces NO_TRADE -> Reset detected!
    risk_manager.governor.signal_lifecycle.on_market_bar("BTC/USD", "NO_TRADE", is_no_trade=True)
    assert risk_manager.governor.signal_lifecycle.get_state("BTC/USD") == SignalLifecycleState.ELIGIBLE_FOR_NEW_SIGNAL

    # 5. Subsequent fresh signal is now accepted!
    fresh_ok2, _ = risk_manager.governor.signal_lifecycle.check_signal_freshness(sig, now + 200.0)
    assert fresh_ok2 is True


# ==============================================================================
# FEATURE 4: MINIMUM EXPECTED EDGE AFTER FEES
# ==============================================================================

def test_expected_edge_vs_fees_and_slippage(risk_manager):
    """
    Requires gross expected edge >= 0.40% (covers 0.24% round trip + 0.16% buffer).
    """
    now = time.time()

    # Good edge: Entry 50,000, TP 50,500 (+1.0% gain) -> Approved
    sig_good = Signal(
        strategy="VALUE_AREA",
        symbol="BTC/USD",
        direction="BUY",
        confidence=0.85,
        entry_price=50000.0,
        stop_loss=49500.0,
        take_profit_1=50500.0,
        take_profit_2=51000.0,
        expected_rr=2.0,
        reason="Good edge",
        regime="RANGE",
        timestamp=int(now * 1000),
    )
    dec_good = risk_manager.evaluate_signal(sig_good)
    assert dec_good.approved is True

    # Bad edge: Entry 50,000, TP 50,100 (+0.20% gain < 0.40% hurdle) -> Rejected
    sig_bad = Signal(
        strategy="VALUE_AREA",
        symbol="BTC/USD",
        direction="BUY",
        confidence=0.85,
        entry_price=50000.0,
        stop_loss=49900.0,
        take_profit_1=50100.0,
        take_profit_2=50150.0,
        expected_rr=1.5,
        reason="Tiny edge",
        regime="RANGE",
        timestamp=int(now * 1000),
    )
    dec_bad = risk_manager.evaluate_signal(sig_bad)
    assert dec_bad.approved is False
    assert RejectionReason.INSUFFICIENT_EXPECTED_EDGE_AFTER_COSTS in dec_bad.reason


# ==============================================================================
# FEATURE 5 & 10: DYNAMIC POSITION SIZING & CORRELATED ALTCOIN LIMITS
# ==============================================================================

def test_dynamic_position_sizing_majors_vs_alts(risk_manager):
    """
    Majors (BTC) allow up to 50% equity ($50,000).
    Alts (PEPE) are capped at 25% equity ($25,000), avoiding huge alt concentration.
    """
    now = time.time()

    # BTC Major: Sized under 1.0% risk / 2% stop = $50,000 notional (1.0 BTC @ 50k)
    sig_btc = Signal(
        strategy="VALUE_AREA",
        symbol="BTC/USD",
        direction="BUY",
        confidence=0.85,
        entry_price=50000.0,
        stop_loss=49000.0,
        take_profit_1=52000.0,
        take_profit_2=53000.0,
        expected_rr=2.0,
        reason="Test",
        regime="RANGE",
        timestamp=int(now * 1000),
    )
    dec_btc = risk_manager.evaluate_signal(sig_btc)
    assert dec_btc.approved is True
    assert pytest.approx(dec_btc.adjusted_quantity * 50000.0, rel=1e-2) == 50000.0

    # PEPE Altcoin: Tight stop would want $100k+, but capped at 25% ($25,000 max)
    sig_pepe = Signal(
        strategy="VALUE_AREA",
        symbol="PEPE/USD",
        direction="BUY",
        confidence=0.85,
        entry_price=0.00001,
        stop_loss=0.0000099,
        take_profit_1=0.000012,
        take_profit_2=0.000014,
        expected_rr=2.0,
        reason="Test",
        regime="RANGE",
        timestamp=int(now * 1000),
    )
    dec_pepe = risk_manager.evaluate_signal(sig_pepe)
    assert dec_pepe.approved is True
    pepe_notional = dec_pepe.adjusted_quantity * 0.00001
    assert pepe_notional <= 25000.01  # Strictly respects 25% altcoin cap


def test_correlated_altcoin_concurrent_limit(risk_manager, portfolio):
    """
    Cannot hold 2 altcoins simultaneously (e.g. ENA + PEPE).
    Max 1 altcoin at a time to prevent correlated beta dump.
    """
    now = time.time()
    # 1. Fill ENA (Altcoin)
    portfolio.record_fill(
        symbol="ENA/USD", side="BUY", quantity=20000.0, price=0.50, fee=10.0, stop_loss=0.48
    )

    # 2. Try to open PEPE (Second Altcoin) -> Blocked!
    sig_pepe = Signal(
        strategy="VALUE_AREA",
        symbol="PEPE/USD",
        direction="BUY",
        confidence=0.85,
        entry_price=0.00001,
        stop_loss=0.0000095,
        take_profit_1=0.000012,
        take_profit_2=0.000014,
        expected_rr=2.0,
        reason="Test",
        regime="RANGE",
        timestamp=int(now * 1000),
    )
    dec_pepe = risk_manager.evaluate_signal(sig_pepe)
    assert dec_pepe.approved is False
    assert RejectionReason.MAX_ALT_CONCURRENT_LIMIT in dec_pepe.reason

    # 3. But BTC (Major) CAN be opened as position #2!
    sig_btc = Signal(
        strategy="VALUE_AREA",
        symbol="BTC/USD",
        direction="BUY",
        confidence=0.85,
        entry_price=50000.0,
        stop_loss=49000.0,
        take_profit_1=52000.0,
        take_profit_2=53000.0,
        expected_rr=2.0,
        reason="Test",
        regime="RANGE",
        timestamp=int(now * 1000),
    )
    dec_btc = risk_manager.evaluate_signal(sig_btc)
    assert dec_btc.approved is True


# ==============================================================================
# FEATURE 6 & 7: PORTFOLIO DRAWDOWN GOVERNOR & RECOVERY MODE
# ==============================================================================

def test_portfolio_drawdown_governor_states(risk_manager):
    """
    Validates state transitions:
    NORMAL (<1.5%) -> CAUTION (1.5-2.5%) -> REDUCED_RISK (2.5-3.5%) -> DEFENSIVE (3.5-5.0%) -> EMERGENCY (>=5.0%).
    """
    gov = risk_manager.governor.drawdown_governor

    # 0.5% DD -> NORMAL
    state, mult, conf, max_pos = gov.evaluate_state(0.005)
    assert state == RiskState.NORMAL
    assert mult == 1.0
    assert conf == 0.60
    assert max_pos == 2

    # 2.0% DD -> CAUTION
    state, mult, conf, max_pos = gov.evaluate_state(0.020)
    assert state == RiskState.CAUTION
    assert mult == 0.70
    assert conf == 0.65

    # 3.0% DD -> REDUCED_RISK & Recovery Mode ON
    state, mult, conf, max_pos = gov.evaluate_state(0.030)
    assert state == RiskState.REDUCED_RISK
    assert mult <= 0.50
    assert gov.recovery_mode_active is True

    # 4.0% DD -> DEFENSIVE
    state, mult, conf, max_pos = gov.evaluate_state(0.040)
    assert state == RiskState.DEFENSIVE
    assert mult <= 0.20
    assert conf >= 0.75
    assert max_pos == 1

    # 5.5% DD -> EMERGENCY
    state, mult, conf, max_pos = gov.evaluate_state(0.055)
    assert state == RiskState.EMERGENCY
    assert mult == 0.00
    assert max_pos == 0


def test_recovery_mode_gradual_exit(risk_manager):
    """
    Recovery mode does not instantly deactivate after 1 win; requires equity recovery AND 2 confirmed wins.
    """
    gov = risk_manager.governor.drawdown_governor

    # Trigger recovery mode at 3.0% DD
    gov.evaluate_state(0.030)
    assert gov.recovery_mode_active is True

    # 1 win occurs, but DD is still 1.5% -> Still in recovery mode
    gov.record_trade_result(100.0)
    gov.evaluate_state(0.015)
    assert gov.recovery_mode_active is True

    # 2nd win occurs, but DD is still 1.2% (> 1.0% threshold) -> Still in recovery mode
    gov.record_trade_result(150.0)
    gov.evaluate_state(0.012)
    assert gov.recovery_mode_active is True

    # DD recovers below 1.0% (0.8%) with 2 wins confirmed -> Recovery Mode DEACTIVATES!
    gov.evaluate_state(0.008)
    assert gov.recovery_mode_active is False


# ==============================================================================
# FEATURE 8: SYMBOL PERFORMANCE THROTTLING
# ==============================================================================

def test_symbol_performance_throttling_consecutive_losses(risk_manager):
    """
    2 consecutive losses on a symbol cuts sizing by 50%.
    3 consecutive losses puts symbol in 30-min throttling cooldown.
    """
    now = time.time()
    perf = risk_manager.governor.symbol_performance

    # 1 loss
    perf.record_trade("ENA/USD", "SELL", 0.50, 1000.0, -50.0, 1.0, 60.0, "SL", now)
    mult, blocked, _ = perf.get_symbol_multiplier("ENA/USD", now)
    assert mult == 1.0
    assert blocked is False

    # 2 consecutive losses -> size cut by 50%
    perf.record_trade("ENA/USD", "SELL", 0.50, 1000.0, -80.0, 1.0, 60.0, "SL", now + 10.0)
    mult, blocked, _ = perf.get_symbol_multiplier("ENA/USD", now + 10.0)
    assert mult == 0.50
    assert blocked is False

    # 3 consecutive losses -> symbol throttled/blocked
    perf.record_trade("ENA/USD", "SELL", 0.50, 1000.0, -120.0, 1.0, 60.0, "SL", now + 20.0)
    mult, blocked, reason = perf.get_symbol_multiplier("ENA/USD", now + 20.0)
    assert blocked is True
    assert RejectionReason.SYMBOL_PERFORMANCE_THROTTLED in reason


# ==============================================================================
# FEATURE 19: STRATEGY ATTRIBUTION (A & B ACTIVE, C INACTIVE)
# ==============================================================================

def test_strategy_attribution_a_and_b_active_c_inactive():
    """
    Explicitly confirms:
    Strategy A (Value Area) = ACTIVE
    Strategy B (Liquidity Sweep) = ACTIVE
    Strategy C (CVD Absorption) = INACTIVE / DISABLED
    """
    cfg = StrategiesConfig()
    assert cfg.value_area.enabled is True
    assert cfg.liquidity_sweep.enabled is True
    assert cfg.cvd_absorption.enabled is False  # Strategy C strictly disabled

    engine = StrategyEngine(config=cfg)
    assert engine.strategy_va is not None
    assert engine.strategy_ls is not None
    assert engine.config.cvd_absorption.enabled is False


# ==============================================================================
# FEATURE 20: 3-TIER VOLATILITY-ADJUSTED ALLOCATION & CONCURRENT CAPACITY
# ==============================================================================

def test_three_tier_position_sizing_allocations():
    """
    Validates:
    - Tier 1 (Majors): up to 35% cap with 1.0x multiplier
    - Tier 2 (Mid-caps): up to 20% cap with 0.70x multiplier
    - Tier 3 (Meme tokens): up to 8% cap with 0.40x multiplier
    """
    gov_cfg = RiskGovernorConfig()
    gov_cfg.position_sizing.max_major_position_pct = 0.35
    gov_cfg.position_sizing.max_midcap_position_pct = 0.20
    gov_cfg.position_sizing.max_meme_position_pct = 0.08
    gov_cfg.position_sizing.base_risk_per_trade_pct = 0.0075
    gov = RiskGovernor(gov_cfg)

    equity = 100000.0
    cash = 100000.0
    multipliers = {"quality": 1.0, "regime": 1.0, "drawdown": 1.0, "symbol": 1.0, "volatility": 1.0}

    # 1. Major (BTC) - Tight stop with large theoretical size capped at 35% ($35,000)
    sig_btc = Signal("VA", "BTC/USD", "BUY", 0.85, 50000.0, 49500.0, 52000.0, 53000.0, 2.0, "Test", "RANGE", 1000)
    ok_btc, _, notional_btc, _, _, _ = gov.position_sizer.compute_size(
        sig_btc, equity, cash, is_major=True, multipliers=multipliers, is_meme=False
    )
    assert ok_btc is True
    assert notional_btc <= 35000.01

    # 2. Mid-cap (SOL) - Capped at 20% ($20,000)
    sig_sol = Signal("VA", "SOL/USD", "BUY", 0.85, 100.0, 99.0, 104.0, 106.0, 2.0, "Test", "RANGE", 1000)
    ok_sol, _, notional_sol, _, _, _ = gov.position_sizer.compute_size(
        sig_sol, equity, cash, is_major=False, multipliers=multipliers, is_meme=False
    )
    assert ok_sol is True
    assert notional_sol <= 20000.01

    # 3. Meme (PEPE) - Capped at 8% ($8,000) with 0.40x tier multiplier
    sig_pepe = Signal("VA", "PEPE/USD", "BUY", 0.85, 0.00001, 0.0000099, 0.000012, 0.000014, 2.0, "Test", "RANGE", 1000)
    ok_pepe, _, notional_pepe, _, _, _ = gov.position_sizer.compute_size(
        sig_pepe, equity, cash, is_major=False, multipliers=multipliers, is_meme=True
    )
    assert ok_pepe is True
    assert notional_pepe <= 8000.01


def test_dynamic_realized_volatility_multiplier(portfolio):
    """
    High volatility (>80th percentile) cuts size by 40% (0.60x).
    Moderate volatility (60-80th percentile) trims size by 20% (0.80x).
    """
    gov_cfg = RiskGovernorConfig()
    gov_cfg.position_sizing.volatility_scaling_enabled = True
    gov = RiskGovernor(gov_cfg)
    now = time.time()

    sig = Signal(
        strategy="VALUE_AREA",
        symbol="ETH/USD",
        direction="BUY",
        confidence=0.85,
        entry_price=3000.0,
        stop_loss=2900.0,
        take_profit_1=3200.0,
        take_profit_2=3300.0,
        expected_rr=2.0,
        reason="Test",
        regime="RANGE",
        timestamp=int(now * 1000),
        metadata={"volatility_percentile": 85.0},  # High volatility spike
    )
    eval_res = gov.evaluate_signal(sig, portfolio)
    assert eval_res.approved is True
    assert eval_res.details.get("volatility_multiplier") == 0.60


def test_meme_token_concurrent_and_aggregate_exposure_limits(portfolio):
    """
    At most 1 meme token at a time; total meme exposure capped at 12%.
    """
    gov_cfg = RiskGovernorConfig()
    gov_cfg.exposure_limits.max_concurrent_positions = 4
    gov_cfg.exposure_limits.max_alt_concurrent_positions = 3
    gov_cfg.exposure_limits.max_meme_concurrent_positions = 1
    gov_cfg.exposure_limits.max_meme_exposure_pct = 0.12
    gov = RiskGovernor(gov_cfg)

    # 1. Fill PEPE (Meme Token #1)
    portfolio.record_fill(
        symbol="PEPE/USD", side="BUY", quantity=500000000.0, price=0.00001, fee=2.0, stop_loss=0.0000095
    )

    # 2. Try to open BONK (Meme Token #2) -> Rejected due to MAX_MEME_CONCURRENT_LIMIT
    ok, reason = gov.exposure_controller.check_exposure(
        candidate_symbol="BONK/USD",
        positions=portfolio.positions,
        equity=portfolio.total_equity,
        proposed_notional=5000.0,
        max_allowed_positions=4,
    )
    assert ok is False
    assert RejectionReason.MAX_MEME_CONCURRENT_LIMIT in reason

    # 3. But non-meme altcoin SOL CAN be opened!
    ok_sol, _ = gov.exposure_controller.check_exposure(
        candidate_symbol="SOL/USD",
        positions=portfolio.positions,
        equity=portfolio.total_equity,
        proposed_notional=15000.0,
        max_allowed_positions=4,
    )
    assert ok_sol is True
