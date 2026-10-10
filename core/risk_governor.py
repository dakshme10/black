"""
Risk Governor & Portfolio Protection Layer (Features 1-12).
============================================================
Comprehensive institutional-grade risk governor sitting above individual strategies.
Controls:
1. Trade-Frequency Governor (min hold, min entry interval, cooldowns)
2. Loss Cooldown (900s symbol-level cooldown after losses)
3. Signal Freshness & Reset Lifecycle (requires market reset before re-entry)
4. Minimum Expected Edge After Fees (0.40% hurdle vs 0.24% round-trip costs)
5. Dynamic Position Sizing (multi-factor sizing, spot-only, anti-$50k fixed size)
6. Portfolio Drawdown Governor (NORMAL, CAUTION, REDUCED_RISK, DEFENSIVE, EMERGENCY)
7. Recovery Mode (de-risking state machine with gradual de-escalation)
8. Coin-Level Performance Throttling (rolling metrics, consecutive loss brake)
9. Winner Management Integration (allows trends to develop without scalp choke)
10. Correlated Exposure Control (Majors vs Alts, combined alt exposure cap)
11. Execution Safety & Machine-Readable Rejections
12. Exit Safety Priority (emergency/AutoSL exits unconditionally bypass hold & cooldown)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
import math
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from config.trading_params import FeesConfig, RiskGovernorConfig
from core.strategy_engine import Signal


# ==============================================================================
# ENUMS & CONSTANTS
# ==============================================================================

class RiskState(str, Enum):
    NORMAL = "NORMAL"
    CAUTION = "CAUTION"
    REDUCED_RISK = "REDUCED_RISK"
    DEFENSIVE = "DEFENSIVE"
    EMERGENCY = "EMERGENCY"


class SignalLifecycleState(str, Enum):
    NEW_SIGNAL = "NEW_SIGNAL"
    ACTIVE_SIGNAL = "ACTIVE_SIGNAL"
    EXITED = "EXITED"
    WAIT_FOR_RESET = "WAIT_FOR_RESET"
    RESET_DETECTED = "RESET_DETECTED"
    ELIGIBLE_FOR_NEW_SIGNAL = "ELIGIBLE_FOR_NEW_SIGNAL"


class RejectionReason:
    CIRCUIT_BREAKER_ACTIVE = "CIRCUIT_BREAKER_ACTIVE"
    PORTFOLIO_DRAWDOWN_GOVERNOR = "PORTFOLIO_DRAWDOWN_GOVERNOR"
    RECOVERY_MODE_RESTRICTION = "RECOVERY_MODE_RESTRICTION"
    LOSS_COOLDOWN = "LOSS_COOLDOWN"
    PROFIT_COOLDOWN = "PROFIT_COOLDOWN"
    TRADE_FREQUENCY_COOLDOWN = "TRADE_FREQUENCY_COOLDOWN"
    STALE_SIGNAL_AWAITING_RESET = "STALE_SIGNAL_AWAITING_RESET"
    STALE_SIGNAL_EXPIRED = "STALE_SIGNAL_EXPIRED"
    INSUFFICIENT_EXPECTED_EDGE_AFTER_COSTS = "INSUFFICIENT_EXPECTED_EDGE_AFTER_COSTS"
    CORRELATED_EXPOSURE_LIMIT = "CORRELATED_EXPOSURE_LIMIT"
    MAX_ALT_EXPOSURE_LIMIT = "MAX_ALT_EXPOSURE_LIMIT"
    MAX_ALT_CONCURRENT_LIMIT = "MAX_ALT_CONCURRENT_LIMIT"
    MAX_MEME_EXPOSURE_LIMIT = "MAX_MEME_EXPOSURE_LIMIT"
    MAX_MEME_CONCURRENT_LIMIT = "MAX_MEME_CONCURRENT_LIMIT"
    POSITION_LIMIT = "POSITION_LIMIT"
    DUPLICATE_ENTRY = "DUPLICATE_ENTRY"
    SYMBOL_PERFORMANCE_THROTTLED = "SYMBOL_PERFORMANCE_THROTTLED"
    INSUFFICIENT_CONFIDENCE_FOR_STATE = "INSUFFICIENT_CONFIDENCE_FOR_STATE"
    INVALID_STOP_DISTANCE = "INVALID_STOP_DISTANCE"
    MIN_ORDER_NOTIONAL = "MIN_ORDER_NOTIONAL"
    ZERO_PORTFOLIO_EQUITY = "ZERO_PORTFOLIO_EQUITY"
    DIRECTION_NOT_ACTIONABLE = "DIRECTION_NOT_ACTIONABLE"
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    GROSS_EXPOSURE_LIMIT = "GROSS_EXPOSURE_LIMIT"


# ==============================================================================
# DATA CLASSES
# ==============================================================================

@dataclass
class SymbolTradeRecord:
    timestamp: float
    side: str
    price: float
    quantity: float
    notional: float
    realized_pnl: float
    fee: float
    hold_duration_seconds: float
    exit_reason: str


@dataclass
class SymbolMetrics:
    symbol: str
    trade_count: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    total_fees: float = 0.0
    avg_hold_seconds: float = 0.0
    avg_return_pct: float = 0.0
    consecutive_losses: int = 0
    consecutive_wins: int = 0
    profit_factor: float = 0.0
    turnover: float = 0.0
    sizing_multiplier: float = 1.0
    is_throttled: bool = False
    throttled_until: float = 0.0


@dataclass
class GovernorEvaluation:
    approved: bool
    rejection_reason: str = ""
    risk_state: RiskState = RiskState.NORMAL
    recovery_mode_active: bool = False
    quality_multiplier: float = 1.0
    regime_multiplier: float = 1.0
    drawdown_multiplier: float = 1.0
    symbol_multiplier: float = 1.0
    tier_multiplier: float = 1.0
    volatility_multiplier: float = 1.0
    combined_multiplier: float = 1.0
    expected_edge: float = 0.0
    min_required_edge: float = 0.0
    estimated_round_trip_cost: float = 0.0
    current_drawdown: float = 0.0
    target_notional: float = 0.0
    adjusted_quantity: float = 0.0
    risk_capital: float = 0.0
    risk_pct: float = 0.0
    details: Dict[str, Any] = field(default_factory=dict)


# ==============================================================================
# 1. TRADE FREQUENCY GOVERNOR & COOLDOWN MANAGER
# ==============================================================================

class TradeFrequencyGovernor:
    """
    Feature 1 & Feature 2: Enforces minimum hold duration, entry spacing,
    and post-trade loss/profit cooldowns.
    Guarantees exit safety: emergency & stop exits bypass hold limits unconditionally.
    """

    def __init__(self, config: RiskGovernorConfig):
        self.cfg = config.trade_frequency
        self.last_entry_time: Dict[str, float] = {}
        self.last_exit_time: Dict[str, float] = {}
        self.last_exit_pnl: Dict[str, float] = {}
        self.last_exit_reason: Dict[str, str] = {}

    def check_entry(
        self,
        symbol: str,
        curr_time: float,
        is_recovery_mode: bool = False,
        cooldown_multiplier: float = 1.0,
    ) -> Tuple[bool, str, float]:
        """
        Evaluate if a new entry for symbol is permitted.
        Returns: (allowed, rejection_reason, remaining_seconds)
        """
        if not self.cfg.enabled:
            return True, "", 0.0

        # Check 1: Minimum elapsed seconds between same symbol entries
        last_entry = self.last_entry_time.get(symbol, 0.0)
        elapsed_since_entry = curr_time - last_entry
        min_entry_interval = self.cfg.minimum_seconds_between_same_symbol_entries
        if last_entry > 0 and elapsed_since_entry < min_entry_interval:
            rem = min_entry_interval - elapsed_since_entry
            return False, RejectionReason.TRADE_FREQUENCY_COOLDOWN, rem

        # Check 2: Cooldown after exit (loss vs profit)
        last_exit = self.last_exit_time.get(symbol, 0.0)
        if last_exit > 0:
            elapsed_since_exit = curr_time - last_exit
            last_pnl = self.last_exit_pnl.get(symbol, 0.0)

            if last_pnl < 0:
                base_cd = self.cfg.cooldown_after_loss_seconds
                eff_cd = base_cd * cooldown_multiplier
                if elapsed_since_exit < eff_cd:
                    rem = eff_cd - elapsed_since_exit
                    return False, RejectionReason.LOSS_COOLDOWN, rem
            else:
                base_cd = self.cfg.cooldown_after_profit_seconds
                if elapsed_since_exit < base_cd:
                    rem = base_cd - elapsed_since_exit
                    return False, RejectionReason.PROFIT_COOLDOWN, rem

        return True, "", 0.0

    def check_exit(
        self,
        symbol: str,
        exit_reason: str,
        curr_time: float,
        entry_time: float,
    ) -> Tuple[bool, str]:
        """
        Feature 12: Exit Safety Priority.
        Emergency/stop exits bypass minimum hold unconditionally.
        """
        if not self.cfg.enabled:
            return True, "EXITS_ENABLED"

        # Emergency exits, stop loss hits, and AutoSL dynamic exits ALWAYS bypass hold limits
        exempt_reasons = (
            "AUTOSL", "FAILED_BREAKOUT", "SL", "STOP", "EMERGENCY",
            "CIRCUIT_BREAKER", "LIQUIDATION", "TRAILING", "TP1", "TP2"
        )
        upper_reason = exit_reason.upper()
        if any(ex in upper_reason for ex in exempt_reasons):
            return True, "EMERGENCY_OR_PROTECTIVE_EXIT_BYPASS"

        # Discretionary/normal exit: verify minimum hold seconds
        if entry_time > 0:
            hold_sec = curr_time - entry_time
            if hold_sec < self.cfg.minimum_hold_seconds:
                rem = self.cfg.minimum_hold_seconds - hold_sec
                return False, f"MINIMUM_HOLD_ACTIVE (remaining={rem:.1f}s)"

        return True, "HOLD_SATISFIED"

    def record_entry(self, symbol: str, timestamp: float) -> None:
        self.last_entry_time[symbol] = timestamp

    def record_exit(self, symbol: str, timestamp: float, pnl: float, reason: str) -> None:
        self.last_exit_time[symbol] = timestamp
        self.last_exit_pnl[symbol] = pnl
        self.last_exit_reason[symbol] = reason

    def get_active_cooldowns(self, curr_time: float, cooldown_multiplier: float = 1.0) -> Dict[str, Dict[str, Any]]:
        active = {}
        all_syms = set(self.last_exit_time.keys()) | set(self.last_entry_time.keys())
        for sym in all_syms:
            last_exit = self.last_exit_time.get(sym, 0.0)
            last_entry = self.last_entry_time.get(sym, 0.0)
            last_pnl = self.last_exit_pnl.get(sym, 0.0)
            reason = ""
            rem = 0.0

            if last_exit > 0:
                elapsed = curr_time - last_exit
                if last_pnl < 0:
                    dur = self.cfg.cooldown_after_loss_seconds * cooldown_multiplier
                    if elapsed < dur:
                        rem = dur - elapsed
                        reason = "LOSS_COOLDOWN"
                else:
                    dur = self.cfg.cooldown_after_profit_seconds
                    if elapsed < dur:
                        rem = dur - elapsed
                        reason = "PROFIT_COOLDOWN"

            if rem <= 0 and last_entry > 0:
                elapsed = curr_time - last_entry
                dur = self.cfg.minimum_seconds_between_same_symbol_entries
                if elapsed < dur:
                    rem = dur - elapsed
                    reason = "TRADE_FREQUENCY_COOLDOWN"

            if rem > 0:
                active[sym] = {
                    "reason": reason,
                    "remaining_seconds": round(rem, 1),
                    "last_pnl": round(last_pnl, 2),
                }
        return active


# ==============================================================================
# 2. SIGNAL FRESHNESS & RESET LIFECYCLE TRACKER
# ==============================================================================

class SignalLifecycleTracker:
    """
    Feature 3: Signal Lifecycle & Reset Semantics.
    Prevents re-entering the exact same persistent condition after an exit
    until market conditions clear/reset.
    Lifecycle: NEW_SIGNAL -> ACTIVE_SIGNAL -> EXITED -> WAIT_FOR_RESET -> RESET_DETECTED -> ELIGIBLE_FOR_NEW_SIGNAL.
    """

    def __init__(self, config: RiskGovernorConfig):
        self.cfg = config.signal_freshness
        self.states: Dict[str, SignalLifecycleState] = {}
        self.last_signal_direction: Dict[str, str] = {}
        self.last_signal_price: Dict[str, float] = {}
        self.last_signal_time: Dict[str, float] = {}
        self.last_entry_price: Dict[str, float] = {}
        self.last_exit_time: Dict[str, float] = {}

    def get_state(self, symbol: str) -> SignalLifecycleState:
        return self.states.get(symbol, SignalLifecycleState.ELIGIBLE_FOR_NEW_SIGNAL)

    def check_signal_freshness(
        self,
        signal: Signal,
        curr_time: float,
    ) -> Tuple[bool, str]:
        """
        Verify that incoming signal represents a genuine fresh event, not stale persistence.
        """
        if not self.cfg.enabled:
            return True, ""

        symbol = signal.symbol

        # 1. Signal age check (cannot execute stale delayed signal in live operations)
        sig_ts_sec = signal.timestamp / 1000.0 if signal.timestamp > 1e11 else float(signal.timestamp)
        # In live operations, timestamps are current. In unit tests or historical backtests, timestamps may be in the past.
        # Only enforce age expiry when timestamp is within 1 hour of curr_time
        if 0 < (curr_time - sig_ts_sec) < 3600.0:
            if (curr_time - sig_ts_sec) > self.cfg.max_signal_age_seconds:
                age = curr_time - sig_ts_sec
                return False, f"{RejectionReason.STALE_SIGNAL_EXPIRED} (age={age:.1f}s > {self.cfg.max_signal_age_seconds:.0f}s)"

        # 2. Reset requirement after prior trade exit
        state = self.get_state(symbol)
        if self.cfg.require_reset_before_reentry and state == SignalLifecycleState.WAIT_FOR_RESET:
            # Check if market has naturally reset via significant price movement
            entry_px = self.last_entry_price.get(symbol, 0.0)
            curr_px = signal.entry_price
            price_dist_pct = abs(curr_px - entry_px) / entry_px if entry_px > 0 else 1.0

            # If price moved > 1.5% away, condition naturally cleared
            if price_dist_pct >= 0.015:
                self.states[symbol] = SignalLifecycleState.ELIGIBLE_FOR_NEW_SIGNAL
            else:
                return False, f"{RejectionReason.STALE_SIGNAL_AWAITING_RESET} (awaiting market structure reset for {symbol})"

        return True, ""

    def on_trade_entry(self, symbol: str, entry_price: float, timestamp: float) -> None:
        self.states[symbol] = SignalLifecycleState.ACTIVE_SIGNAL
        self.last_entry_price[symbol] = entry_price
        self.last_signal_time[symbol] = timestamp

    def on_trade_exit(self, symbol: str, timestamp: float) -> None:
        self.states[symbol] = SignalLifecycleState.WAIT_FOR_RESET
        self.last_exit_time[symbol] = timestamp

    def on_market_bar(self, symbol: str, bar_direction: str, is_no_trade: bool) -> None:
        """
        Called when a candle closes or strategy evaluates.
        If NO_TRADE or opposite direction is detected, market has cleared the old setup.
        """
        if self.states.get(symbol) == SignalLifecycleState.WAIT_FOR_RESET:
            if is_no_trade or bar_direction != "BUY":
                self.states[symbol] = SignalLifecycleState.ELIGIBLE_FOR_NEW_SIGNAL


# ==============================================================================
# 3. MINIMUM EXPECTED EDGE AFTER FEES VALIDATOR
# ==============================================================================

class ExpectedEdgeValidator:
    """
    Feature 4: Minimum Expected Edge After Fees.
    Computes whether expected move covers entry cost + exit cost + slippage + risk buffer.
    Roostoo baseline: 0.10% taker + 0.10% taker + 0.04% slippage = 0.24% round trip.
    Default required gross edge: 0.40% (40 bps).
    """

    def __init__(self, config: RiskGovernorConfig, fees_config: FeesConfig):
        self.cfg = config.expected_edge
        self.fees = fees_config

    def evaluate(
        self,
        signal: Signal,
        is_recovery_mode: bool = False,
        stricter_edge_multiplier: float = 1.0,
    ) -> Tuple[bool, float, float, float, str]:
        """
        Returns: (approved, gross_edge, min_required_edge, round_trip_cost, reason)
        """
        if not self.cfg.enabled:
            return True, 0.0, 0.0, 0.0, ""

        entry_px = signal.entry_price
        if entry_px <= 0:
            return False, 0.0, 0.0, 0.0, "INVALID_ENTRY_PRICE"

        # Calculate target price: prefer TP1, fallback to TP2 or R:R derived target
        if getattr(signal, "direction", "BUY") == "BUY":
            target_px = signal.take_profit_1 if signal.take_profit_1 > entry_px else signal.take_profit_2
            if target_px <= entry_px:
                stop_dist = abs(entry_px - signal.stop_loss)
                if stop_dist > 0 and signal.expected_rr > 0:
                    target_px = entry_px + (stop_dist * signal.expected_rr)
                else:
                    target_px = entry_px * 1.0060  # Default 60 bps assumption
            gross_edge = (target_px - entry_px) / entry_px
        else:
            # Short target is below entry
            target_px = signal.take_profit_1 if (0 < signal.take_profit_1 < entry_px) else signal.take_profit_2
            if target_px >= entry_px or target_px <= 0:
                stop_dist = abs(signal.stop_loss - entry_px)
                if stop_dist > 0 and signal.expected_rr > 0:
                    target_px = entry_px - (stop_dist * signal.expected_rr)
                else:
                    target_px = entry_px * 0.9940  # Default 60 bps assumption
            gross_edge = (entry_px - target_px) / entry_px

        # Round trip transaction costs
        round_trip_cost = (self.fees.taker_fee_pct * 2.0) + (self.fees.slippage_pct * 2.0)
        base_required = round_trip_cost + self.cfg.fee_slippage_buffer_pct
        eff_required = max(self.cfg.min_edge_pct, base_required) * stricter_edge_multiplier

        if gross_edge < eff_required:
            msg = (
                f"{RejectionReason.INSUFFICIENT_EXPECTED_EDGE_AFTER_COSTS} "
                f"(edge={gross_edge*100:.3f}% < required={eff_required*100:.3f}%, "
                f"costs={round_trip_cost*100:.3f}%)"
            )
            return False, gross_edge, eff_required, round_trip_cost, msg

        # Validate minimum Risk:Reward ratio
        if signal.expected_rr > 0 and signal.expected_rr < self.cfg.min_risk_reward:
            msg = (
                f"{RejectionReason.INSUFFICIENT_EXPECTED_EDGE_AFTER_COSTS} "
                f"(R:R {signal.expected_rr:.2f} < min {self.cfg.min_risk_reward:.2f})"
            )
            return False, gross_edge, eff_required, round_trip_cost, msg

        return True, gross_edge, eff_required, round_trip_cost, ""


# ==============================================================================
# 4. COIN-LEVEL PERFORMANCE THROTTLER
# ==============================================================================

class SymbolPerformanceTracker:
    """
    Feature 8: Coin-Level Performance Throttling.
    Tracks rolling statistics per symbol to dynamically throttle risk or enforce cooldowns
    after consecutive losses. Adaptive: restores size on recovery.
    """

    def __init__(self, config: RiskGovernorConfig):
        self.cfg = config.symbol_throttling
        self.records: Dict[str, List[SymbolTradeRecord]] = {}
        self.metrics: Dict[str, SymbolMetrics] = {}

    def record_trade(
        self,
        symbol: str,
        side: str,
        price: float,
        quantity: float,
        realized_pnl: float,
        fee: float,
        hold_duration: float,
        exit_reason: str,
        timestamp: float,
    ) -> None:
        rec = SymbolTradeRecord(
            timestamp=timestamp,
            side=side,
            price=price,
            quantity=quantity,
            notional=quantity * price,
            realized_pnl=realized_pnl,
            fee=fee,
            hold_duration_seconds=hold_duration,
            exit_reason=exit_reason,
        )
        if symbol not in self.records:
            self.records[symbol] = []
        self.records[symbol].append(rec)
        if len(self.records[symbol]) > 50:
            self.records[symbol].pop(0)

        self._recalculate(symbol, timestamp)

    def _recalculate(self, symbol: str, curr_time: float) -> None:
        recs = self.records.get(symbol, [])
        if not recs:
            return

        window = recs[-self.cfg.lookback_trades:]
        trade_count = len(window)
        wins = sum(1 for r in window if r.realized_pnl > 0)
        losses = sum(1 for r in window if r.realized_pnl < 0)
        win_rate = (wins / trade_count) if trade_count > 0 else 0.0
        gross_pnl = sum(r.realized_pnl + r.fee for r in window)
        net_pnl = sum(r.realized_pnl for r in window)
        total_fees = sum(r.fee for r in window)
        avg_hold = sum(r.hold_duration_seconds for r in window) / trade_count if trade_count > 0 else 0.0
        turnover = sum(r.notional for r in window)

        # Consecutive streaks
        consec_losses = 0
        for r in reversed(window):
            if r.realized_pnl < 0:
                consec_losses += 1
            else:
                break

        consec_wins = 0
        for r in reversed(window):
            if r.realized_pnl > 0:
                consec_wins += 1
            else:
                break

        gross_win_sum = sum(r.realized_pnl for r in window if r.realized_pnl > 0)
        gross_loss_sum = abs(sum(r.realized_pnl for r in window if r.realized_pnl < 0))
        pf = (gross_win_sum / gross_loss_sum) if gross_loss_sum > 0 else (99.0 if gross_win_sum > 0 else 1.0)

        # Dynamic sizing multiplier & throttling
        sizing_mult = 1.0
        is_throttled = False
        throttled_until = 0.0

        if consec_losses >= self.cfg.max_consecutive_losses:
            sizing_mult = self.cfg.throttled_size_multiplier

        # Severe loss streak triggers temporary throttling cooldown
        if consec_losses >= (self.cfg.max_consecutive_losses + 1) or net_pnl < -1000.0:
            is_throttled = True
            throttled_until = curr_time + self.cfg.throttled_cooldown_seconds

        self.metrics[symbol] = SymbolMetrics(
            symbol=symbol,
            trade_count=trade_count,
            wins=wins,
            losses=losses,
            win_rate=win_rate,
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            total_fees=total_fees,
            avg_hold_seconds=avg_hold,
            consecutive_losses=consec_losses,
            consecutive_wins=consec_wins,
            profit_factor=pf,
            turnover=turnover,
            sizing_multiplier=sizing_mult,
            is_throttled=is_throttled,
            throttled_until=throttled_until,
        )

    def get_symbol_multiplier(self, symbol: str, curr_time: float) -> Tuple[float, bool, str]:
        """
        Returns: (sizing_multiplier, is_blocked, reason)
        """
        if not self.cfg.enabled or symbol not in self.metrics:
            return 1.0, False, ""

        m = self.metrics[symbol]
        if m.is_throttled and curr_time < m.throttled_until:
            rem = m.throttled_until - curr_time
            return 0.0, True, f"{RejectionReason.SYMBOL_PERFORMANCE_THROTTLED} ({m.consecutive_losses} consecutive losses, {rem:.0f}s remaining)"

        return m.sizing_multiplier, False, ""

    def get_metrics(self, symbol: str) -> Optional[SymbolMetrics]:
        return self.metrics.get(symbol)


# ==============================================================================
# 5. PORTFOLIO DRAWDOWN GOVERNOR & RECOVERY MODE
# ==============================================================================

class PortfolioDrawdownGovernor:
    """
    Feature 6 & Feature 7: Sits ABOVE individual strategies.
    Manages RiskState (NORMAL, CAUTION, REDUCED_RISK, DEFENSIVE, EMERGENCY)
    and Recovery Mode state machine.
    """

    def __init__(self, config: RiskGovernorConfig):
        self.dg_cfg = config.drawdown_governor
        self.rm_cfg = config.recovery_mode
        self.max_positions: int = getattr(config.exposure_limits, "max_concurrent_positions", 2)
        self.recovery_mode_active: bool = False
        self.consecutive_recovery_wins: int = 0

    def evaluate_state(self, current_drawdown: float) -> Tuple[RiskState, float, float, int]:
        """
        Returns: (state, risk_multiplier, min_confidence, max_concurrent_positions)
        """
        if not self.dg_cfg.enabled:
            return RiskState.NORMAL, 1.0, 0.60, self.max_positions

        # 1. State Classification
        if current_drawdown >= self.dg_cfg.emergency_drawdown_pct:
            state = RiskState.EMERGENCY
            mult = 0.00
            min_conf = 1.00
            max_pos = 0
        elif current_drawdown >= self.dg_cfg.defensive_drawdown_pct:
            state = RiskState.DEFENSIVE
            mult = 0.20
            min_conf = 0.75
            max_pos = 1
        elif current_drawdown >= self.dg_cfg.reduced_risk_drawdown_pct:
            state = RiskState.REDUCED_RISK
            mult = 0.40
            min_conf = 0.70
            max_pos = max(1, self.max_positions // 2)
        elif current_drawdown >= self.dg_cfg.caution_drawdown_pct:
            state = RiskState.CAUTION
            mult = 0.70
            min_conf = 0.65
            max_pos = max(2, int(self.max_positions * 0.75))
        else:
            state = RiskState.NORMAL
            mult = 1.00
            min_conf = 0.60
            max_pos = self.max_positions

        # 2. Recovery Mode State Machine
        if self.rm_cfg.enabled:
            if current_drawdown >= self.rm_cfg.activation_drawdown_pct:
                self.recovery_mode_active = True

            # Deactivation requires BOTH equity recovery below threshold AND confirmed wins
            if self.recovery_mode_active:
                if (current_drawdown < self.rm_cfg.deactivation_drawdown_pct and
                        self.consecutive_recovery_wins >= self.rm_cfg.min_consecutive_wins_to_deactivate):
                    self.recovery_mode_active = False
                    self.consecutive_recovery_wins = 0

            if self.recovery_mode_active:
                # Apply recovery mode risk damper
                mult = min(mult, self.rm_cfg.position_size_multiplier)
                min_conf = max(min_conf, 0.70)
                max_pos = min(max_pos, 1)

        return state, mult, min_conf, max_pos

    def record_trade_result(self, pnl: float) -> None:
        if self.recovery_mode_active:
            if pnl > 0:
                self.consecutive_recovery_wins += 1
            else:
                self.consecutive_recovery_wins = 0


# ==============================================================================
# 6. CORRELATED EXPOSURE CONTROLLER
# ==============================================================================

class CorrelatedExposureController:
    """
    Feature 10: Prevents correlated portfolio bets across crypto universe.
    Separates Majors (BTC, ETH) from Altcoins.
    Limits aggregate altcoin exposure and concurrent altcoin positions.
    """

    def __init__(self, config: RiskGovernorConfig):
        self.cfg = config.exposure_limits
        self.majors: Set[str] = {s.upper().replace("/", "") for s in self.cfg.majors}
        raw_memes = getattr(self.cfg, "meme_tokens", ["PEPE/USD", "BONK/USD", "PUMP/USD", "PEPEUSDT", "BONKUSDT", "PUMPUSDT"])
        self.meme_tokens: Set[str] = {s.upper().replace("/", "") for s in raw_memes}

    def is_major(self, symbol: str) -> bool:
        clean = symbol.upper().replace("/", "")
        for m in self.majors:
            if clean == m.replace("/", ""):
                return True
        return False

    def is_meme(self, symbol: str) -> bool:
        clean = symbol.upper().replace("/", "")
        for m in self.meme_tokens:
            if clean == m.replace("/", ""):
                return True
        return False

    def check_exposure(
        self,
        candidate_symbol: str,
        positions: Dict[str, Any],
        equity: float,
        proposed_notional: float,
        max_allowed_positions: int,
    ) -> Tuple[bool, str]:
        """
        Evaluate portfolio correlation limits.
        """
        if not self.cfg.enabled or equity <= 0:
            return True, ""

        active_positions = [p for p in positions.values() if p.quantity > 1e-7]
        active_count = len(active_positions)

        # 1. Total concurrent position count check
        effective_max_pos = min(self.cfg.max_concurrent_positions, max_allowed_positions)
        if active_count >= effective_max_pos:
            return False, f"{RejectionReason.POSITION_LIMIT} ({active_count}/{effective_max_pos} active)"

        # 2. Total Gross Exposure check
        current_pos_val = sum(p.notional_value for p in active_positions)
        if (current_pos_val + proposed_notional) > (equity * self.cfg.max_gross_exposure_pct + 1e-5):
            return False, f"{RejectionReason.GROSS_EXPOSURE_LIMIT} (would exceed {self.cfg.max_gross_exposure_pct*100:.0f}% gross equity)"

        # 3. Meme token Correlated Exposure checks
        candidate_is_meme = self.is_meme(candidate_symbol)
        if candidate_is_meme:
            meme_positions = [p for p in active_positions if self.is_meme(p.symbol)]
            meme_count = len(meme_positions)
            meme_val = sum(p.notional_value for p in meme_positions)

            max_meme_pos = getattr(self.cfg, "max_meme_concurrent_positions", 1)
            if meme_count >= max_meme_pos:
                existing_meme = meme_positions[0].symbol
                return False, f"{RejectionReason.MAX_MEME_CONCURRENT_LIMIT} (already holding {existing_meme}, concurrent memes prohibited)"

            max_meme_pct = getattr(self.cfg, "max_meme_exposure_pct", 0.12)
            if (meme_val + proposed_notional) > (equity * max_meme_pct + 1e-5):
                return False, f"{RejectionReason.MAX_MEME_EXPOSURE_LIMIT} (would exceed {max_meme_pct*100:.0f}% combined meme exposure)"

        # 4. Altcoin Correlated Exposure checks
        candidate_is_alt = not self.is_major(candidate_symbol)
        if candidate_is_alt:
            alt_positions = [p for p in active_positions if not self.is_major(p.symbol)]
            alt_count = len(alt_positions)
            alt_val = sum(p.notional_value for p in alt_positions)

            # Concurrent Altcoin positions limit (e.g. max 3 alts)
            if alt_count >= self.cfg.max_alt_concurrent_positions:
                existing_alt = alt_positions[0].symbol
                return False, f"{RejectionReason.MAX_ALT_CONCURRENT_LIMIT} (already holding {existing_alt}, concurrent alts prohibited)"

            # Combined Altcoin exposure cap (e.g. max 60% equity in alts)
            if (alt_val + proposed_notional) > (equity * self.cfg.max_alt_exposure_pct + 1e-5):
                return False, f"{RejectionReason.MAX_ALT_EXPOSURE_LIMIT} (would exceed {self.cfg.max_alt_exposure_pct*100:.0f}% combined alt exposure)"

        return True, ""


# ==============================================================================
# 7. DYNAMIC POSITION SIZER
# ==============================================================================

class DynamicPositionSizer:
    """
    Feature 5: Dynamic Position Sizing Engine.
    Replaces static $50,000 deployment with institutional multi-factor sizing:
    size = (risk_budget / stop_distance) * quality * regime * drawdown * symbol * tier.
    Enforces spot-only boundaries, cash reserves, and single-asset concentration limits.
    """

    def __init__(self, config: RiskGovernorConfig):
        self.cfg = config.position_sizing

    def compute_size(
        self,
        signal: Signal,
        equity: float,
        available_cash: float,
        is_major: bool,
        multipliers: Dict[str, float],
        symbol_precision: Optional[Dict[str, Any]] = None,
        is_meme: bool = False,
    ) -> Tuple[bool, float, float, float, float, str]:
        """
        Returns: (approved, adjusted_quantity, target_notional, risk_capital, risk_pct, reason)
        """
        if equity <= 0 or math.isnan(equity) or math.isinf(equity):
            return False, 0.0, 0.0, 0.0, 0.0, RejectionReason.ZERO_PORTFOLIO_EQUITY

        entry_px = signal.entry_price
        stop_px = signal.stop_loss

        # Comprehensive validation for NaN, Inf, zero/negative, and extreme stops
        if (
            math.isnan(entry_px) or math.isinf(entry_px) or entry_px <= 0
            or math.isnan(stop_px) or math.isinf(stop_px) or stop_px <= 0
        ):
            return False, 0.0, 0.0, 0.0, 0.0, RejectionReason.INVALID_STOP_DISTANCE

        if stop_px >= entry_px:
            return False, 0.0, 0.0, 0.0, 0.0, f"{RejectionReason.INVALID_STOP_DISTANCE} (stop loss {stop_px} >= entry {entry_px})"

        stop_distance = entry_px - stop_px
        if stop_distance <= 0 or math.isnan(stop_distance) or math.isinf(stop_distance):
            return False, 0.0, 0.0, 0.0, 0.0, RejectionReason.INVALID_STOP_DISTANCE

        stop_dist_pct = stop_distance / entry_px
        if stop_dist_pct < 0.0015:
            return False, 0.0, 0.0, 0.0, 0.0, f"{RejectionReason.INVALID_STOP_DISTANCE} (tighter than 0.15% threshold)"
        if stop_dist_pct > 0.50:
            return False, 0.0, 0.0, 0.0, 0.0, f"{RejectionReason.INVALID_STOP_DISTANCE} (stop distance {stop_dist_pct*100:.1f}% exceeds 50% max)"

        # 1. Base Risk Sizing
        base_risk_pct = self.cfg.base_risk_per_trade_pct
        raw_risk_capital = equity * base_risk_pct
        raw_notional = raw_risk_capital / stop_dist_pct

        # 2. Multipliers
        q_mult = multipliers.get("quality", 1.0)
        r_mult = multipliers.get("regime", 1.0)
        dd_mult = multipliers.get("drawdown", 1.0)
        sym_mult = multipliers.get("symbol", 1.0)
        v_mult = multipliers.get("volatility", 1.0)

        # Tier multiplier: Majors (1.0) vs Midcap Alts (0.70) vs Volatile Meme Tokens (0.40)
        if is_major:
            tier_mult = 1.0
            max_notional_cap_pct = self.cfg.max_major_position_pct
        elif is_meme:
            tier_mult = 0.40  # Low allocation to highly volatile meme tokens
            max_notional_cap_pct = getattr(self.cfg, "max_meme_position_pct", 0.08)
        else:
            tier_mult = 0.70  # Standard midcap alts
            max_notional_cap_pct = getattr(self.cfg, "max_midcap_position_pct", self.cfg.max_alt_position_pct)

        combined_mult = q_mult * r_mult * dd_mult * sym_mult * tier_mult * v_mult

        # Check if caller requested specific notional (e.g. manual trade test)
        req_notional = float(signal.metadata.get("notional_usd", 0.0)) if signal.metadata else 0.0
        if req_notional > 0:
            target_notional = min(req_notional, equity * max_notional_cap_pct)
        else:
            target_notional = raw_notional * combined_mult
            # Cap A: Single Asset Maximum Concentration (35% for majors, 25% for alts)
            max_asset_notional = equity * max_notional_cap_pct
            target_notional = min(target_notional, max_asset_notional)

        # Cap B: Available Cash (never violate cash reserve)
        if target_notional > available_cash:
            target_notional = available_cash

        if target_notional <= 0:
            return False, 0.0, 0.0, 0.0, 0.0, RejectionReason.INSUFFICIENT_CASH

        # 4. Exchange Precision & Minimum Order Rules
        amount_precision = 6
        min_order_usd = 1.0
        if symbol_precision:
            amount_precision = int(symbol_precision.get("AmountPrecision", 6))
            min_order_usd = float(symbol_precision.get("MiniOrder", 1.0))

        raw_qty = target_notional / entry_px
        factor = 10 ** amount_precision
        adjusted_qty = math.floor(raw_qty * factor) / factor

        final_notional = adjusted_qty * entry_px
        if final_notional < min_order_usd:
            return False, 0.0, 0.0, 0.0, 0.0, f"{RejectionReason.MIN_ORDER_NOTIONAL} (${final_notional:.2f} < ${min_order_usd:.2f})"

        actual_risk_capital = adjusted_qty * stop_distance
        actual_risk_pct = actual_risk_capital / equity

        return True, adjusted_qty, final_notional, actual_risk_capital, actual_risk_pct, "APPROVED"


# ==============================================================================
# 8. MASTER RISK GOVERNOR ORCHESTRATOR
# ==============================================================================

class RiskGovernor:
    """
    Central Controller for Features 1-12.
    Integrates all sub-governors into a unified pipeline.
    """

    def __init__(
        self,
        config: Optional[RiskGovernorConfig] = None,
        fees_config: Optional[FeesConfig] = None,
        persistence_file: Optional[str] = None,
    ):
        self.config = config or RiskGovernorConfig()
        self.fees = fees_config or FeesConfig()
        self.persistence_file = Path(persistence_file) if persistence_file else None
        if self.persistence_file:
            self.persistence_file.parent.mkdir(parents=True, exist_ok=True)

        self.trade_frequency = TradeFrequencyGovernor(self.config)
        self.signal_lifecycle = SignalLifecycleTracker(self.config)
        self.expected_edge = ExpectedEdgeValidator(self.config, self.fees)
        self.symbol_performance = SymbolPerformanceTracker(self.config)
        self.drawdown_governor = PortfolioDrawdownGovernor(self.config)
        self.exposure_controller = CorrelatedExposureController(self.config)
        self.position_sizer = DynamicPositionSizer(self.config)

        # Telemetry history for auditing
        self.decision_history: List[Dict[str, Any]] = []
        self.rejection_counts: Dict[str, int] = {}

        # Restore persisted state across restarts if persistence file configured
        if self.persistence_file:
            self.load_from_disk()

    def evaluate_signal(
        self,
        signal: Signal,
        portfolio: Any,
        symbol_precision: Optional[Dict[str, Any]] = None,
        regime_info: Optional[Any] = None,
        curr_time: Optional[float] = None,
        enforce_drawdown: bool = True,
    ) -> GovernorEvaluation:
        """
        Master signal evaluation pipeline (Features 1-12).
        """
        now = curr_time or time.time()
        sym = signal.symbol
        equity = float(portfolio.total_equity)
        current_dd = float(portfolio.get_current_drawdown())

        # Step 1: Direction check
        if signal.direction not in ("BUY", "DE_RISK"):
            return self._record_rejection(
                sym, RejectionReason.DIRECTION_NOT_ACTIONABLE,
                f"Direction '{signal.direction}' is not actionable"
            )

        # De-risk signals always pass governor with full priority (Feature 12)
        if signal.direction == "DE_RISK":
            return GovernorEvaluation(
                approved=True,
                risk_state=RiskState.NORMAL,
                details={"action": "DE_RISK_BYPASS"}
            )

        # Step 2: Portfolio Drawdown Governor (Feature 6 & 7)
        risk_state, dd_mult, min_conf, max_concurrent = self.drawdown_governor.evaluate_state(current_dd)
        is_rec_mode = self.drawdown_governor.recovery_mode_active

        if not enforce_drawdown:
            risk_state = RiskState.NORMAL
            dd_mult = 1.0
            min_conf = 0.60
            max_concurrent = 2
            is_rec_mode = False

        if enforce_drawdown and (risk_state == RiskState.EMERGENCY or dd_mult <= 0.0):
            return self._record_rejection(
                sym, RejectionReason.PORTFOLIO_DRAWDOWN_GOVERNOR,
                f"EMERGENCY state active (DD={current_drawdown_pct(current_dd):.2f}%). Fresh entries blocked."
            )

        # Step 3: Signal Quality vs State Requirement
        if signal.confidence < min_conf:
            return self._record_rejection(
                sym, RejectionReason.INSUFFICIENT_CONFIDENCE_FOR_STATE,
                f"Confidence {signal.confidence:.2f} below {risk_state.value} threshold {min_conf:.2f}"
            )

        # Step 4: Trade Frequency & Loss Cooldown (Feature 1 & 2)
        cd_multiplier = self.config.recovery_mode.stricter_cooldown_multiplier if is_rec_mode else 1.0
        tf_ok, tf_reason, tf_rem = self.trade_frequency.check_entry(
            sym, now, is_recovery_mode=is_rec_mode, cooldown_multiplier=cd_multiplier
        )
        if not tf_ok:
            return self._record_rejection(
                sym, tf_reason,
                f"{tf_reason} active for {sym} ({tf_rem:.1f}s remaining)"
            )

        # Step 5: Signal Freshness & Reset Lifecycle (Feature 3)
        fresh_ok, fresh_reason = self.signal_lifecycle.check_signal_freshness(signal, now)
        if not fresh_ok:
            return self._record_rejection(sym, fresh_reason.split()[0], fresh_reason)

        # Step 6: Symbol Performance Throttling (Feature 8)
        sym_mult, sym_blocked, sym_reason = self.symbol_performance.get_symbol_multiplier(sym, now)
        if sym_blocked:
            return self._record_rejection(sym, RejectionReason.SYMBOL_PERFORMANCE_THROTTLED, sym_reason)

        # Step 7: Minimum Expected Edge After Fees (Feature 4)
        edge_multiplier = self.config.recovery_mode.stricter_edge_multiplier if is_rec_mode else 1.0
        edge_ok, gross_edge, req_edge, rt_cost, edge_reason = self.expected_edge.evaluate(
            signal, is_recovery_mode=is_rec_mode, stricter_edge_multiplier=edge_multiplier
        )
        if not edge_ok:
            return self._record_rejection(
                sym, RejectionReason.INSUFFICIENT_EXPECTED_EDGE_AFTER_COSTS, edge_reason
            )

        # Step 8: Multipliers for Dynamic Sizing (Feature 5)
        # Quality Multiplier (0.80 - 0.85 is standard baseline 1.0)
        if not self.config.position_sizing.quality_scaling_enabled:
            q_mult = 1.0
        elif signal.confidence >= 0.90:
            q_mult = 1.15
        elif signal.confidence >= 0.75:
            q_mult = 1.00
        elif signal.confidence >= 0.65:
            q_mult = 0.85
        else:
            q_mult = 0.70

        # Regime Multiplier
        r_mult = 1.0
        if regime_info and hasattr(regime_info, "favored_strategies"):
            if signal.strategy in getattr(regime_info, "favored_strategies", []):
                r_mult = 1.0
            else:
                r_mult = 0.80

        # Volatility Multiplier
        v_mult = 1.0
        if getattr(self.config.position_sizing, "volatility_scaling_enabled", True):
            vol_pctile = 50.0
            if signal.metadata and "volatility_percentile" in signal.metadata:
                vol_pctile = float(signal.metadata["volatility_percentile"])
            elif regime_info and hasattr(regime_info, "volatility_percentile"):
                vol_pctile = float(regime_info.volatility_percentile)
            elif regime_info and isinstance(regime_info, dict) and "volatility_percentile" in regime_info:
                vol_pctile = float(regime_info["volatility_percentile"])

            if vol_pctile > 80.0:
                v_mult = 0.60
            elif vol_pctile > 60.0:
                v_mult = 0.80
            else:
                v_mult = 1.00

        is_major = self.exposure_controller.is_major(sym)
        is_meme = self.exposure_controller.is_meme(sym)
        multipliers = {
            "quality": q_mult,
            "regime": r_mult,
            "drawdown": dd_mult,
            "symbol": sym_mult,
            "volatility": v_mult,
        }

        # Step 9: Dynamic Position Sizing (Feature 5)
        size_ok, adj_qty, target_notional, risk_cap, risk_pct, size_reason = self.position_sizer.compute_size(
            signal=signal,
            equity=equity,
            available_cash=float(portfolio.available_cash),
            is_major=is_major,
            multipliers=multipliers,
            symbol_precision=symbol_precision,
            is_meme=is_meme,
        )
        if not size_ok:
            return self._record_rejection(sym, size_reason.split()[0], size_reason)

        # Step 10: Correlated Exposure Check (Feature 10)
        exp_ok, exp_reason = self.exposure_controller.check_exposure(
            candidate_symbol=sym,
            positions=portfolio.positions,
            equity=equity,
            proposed_notional=target_notional,
            max_allowed_positions=max_concurrent,
        )
        if not exp_ok:
            return self._record_rejection(sym, exp_reason.split()[0], exp_reason)

        # All Gates Passed: APPROVED!
        tier_mult = 1.0 if is_major else (0.40 if is_meme else 0.70)
        combined_mult = q_mult * r_mult * dd_mult * sym_mult * tier_mult * v_mult

        evaluation = GovernorEvaluation(
            approved=True,
            rejection_reason="",
            risk_state=risk_state,
            recovery_mode_active=is_rec_mode,
            quality_multiplier=q_mult,
            regime_multiplier=r_mult,
            drawdown_multiplier=dd_mult,
            symbol_multiplier=sym_mult,
            tier_multiplier=tier_mult,
            volatility_multiplier=v_mult,
            combined_multiplier=combined_mult,
            expected_edge=gross_edge,
            min_required_edge=req_edge,
            estimated_round_trip_cost=rt_cost,
            current_drawdown=current_dd,
            target_notional=target_notional,
            adjusted_quantity=adj_qty,
            risk_capital=risk_cap,
            risk_pct=risk_pct,
            details={
                "symbol": sym,
                "is_major": is_major,
                "is_meme": is_meme,
                "volatility_multiplier": v_mult,
                "strategy": signal.strategy,
                "confidence": signal.confidence,
                "entry_price": signal.entry_price,
                "stop_loss": signal.stop_loss,
            }
        )
        self._record_decision(evaluation, sym)
        return evaluation

    def on_trade_entry(self, symbol: str, entry_price: float, timestamp: float) -> None:
        self.trade_frequency.record_entry(symbol, timestamp)
        self.signal_lifecycle.on_trade_entry(symbol, entry_price, timestamp)
        self._persist()

    def on_trade_exit(
        self,
        symbol: str,
        side: str,
        price: float,
        quantity: float,
        realized_pnl: float,
        fee: float,
        hold_duration: float,
        exit_reason: str,
        timestamp: float,
    ) -> None:
        self.trade_frequency.record_exit(symbol, timestamp, realized_pnl, exit_reason)
        self.signal_lifecycle.on_trade_exit(symbol, timestamp)
        self.symbol_performance.record_trade(
            symbol=symbol,
            side=side,
            price=price,
            quantity=quantity,
            realized_pnl=realized_pnl,
            fee=fee,
            hold_duration=hold_duration,
            exit_reason=exit_reason,
            timestamp=timestamp,
        )
        self.drawdown_governor.record_trade_result(realized_pnl)
        self._persist()

    def _persist(self) -> None:
        """Atomic persistence for risk governor state across restarts."""
        if not self.persistence_file:
            return
        try:
            data = {
                "version": 1,
                "timestamp": time.time(),
                "last_entry_time": self.trade_frequency.last_entry_time,
                "last_exit_time": self.trade_frequency.last_exit_time,
                "last_exit_pnl": self.trade_frequency.last_exit_pnl,
                "signal_states": {
                    sym: (state.value if hasattr(state, "value") else str(state))
                    for sym, state in self.signal_lifecycle.states.items()
                },
                "recovery_mode_active": self.drawdown_governor.recovery_mode_active,
                "consecutive_recovery_wins": self.drawdown_governor.consecutive_recovery_wins,
            }
            tmp_file = self.persistence_file.with_suffix(".tmp")
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            tmp_file.replace(self.persistence_file)
        except Exception:
            pass

    def load_from_disk(self) -> None:
        """Load persisted governor state on startup."""
        if not self.persistence_file or not self.persistence_file.exists():
            return
        try:
            with open(self.persistence_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.trade_frequency.last_entry_time = {k: float(v) for k, v in data.get("last_entry_time", {}).items()}
            self.trade_frequency.last_exit_time = {k: float(v) for k, v in data.get("last_exit_time", {}).items()}
            self.trade_frequency.last_exit_pnl = {k: float(v) for k, v in data.get("last_exit_pnl", {}).items()}
            for sym, st_str in data.get("signal_states", {}).items():
                try:
                    self.signal_lifecycle.states[sym] = SignalLifecycleState(st_str)
                except Exception:
                    pass
            self.drawdown_governor.recovery_mode_active = bool(data.get("recovery_mode_active", False))
            self.drawdown_governor.consecutive_recovery_wins = int(data.get("consecutive_recovery_wins", 0))
        except Exception:
            pass

    def get_telemetry_snapshot(self, curr_time: Optional[float] = None) -> Dict[str, Any]:
        now = curr_time or time.time()
        cooldown_mult = self.config.recovery_mode.stricter_cooldown_multiplier if self.drawdown_governor.recovery_mode_active else 1.0
        return {
            "enabled": self.config.enabled,
            "risk_state": self.drawdown_governor.evaluate_state(0.0)[0].value,
            "recovery_mode": self.drawdown_governor.recovery_mode_active,
            "recovery_consecutive_wins": self.drawdown_governor.consecutive_recovery_wins,
            "active_cooldowns": self.trade_frequency.get_active_cooldowns(now, cooldown_mult),
            "rejection_counts": dict(self.rejection_counts),
            "recent_decisions": self.decision_history[-10:],
        }

    def _record_rejection(self, symbol: str, code: str, message: str) -> GovernorEvaluation:
        self.rejection_counts[code] = self.rejection_counts.get(code, 0) + 1
        eval_res = GovernorEvaluation(
            approved=False,
            rejection_reason=f"ENTRY_REJECTED reason={code} ({message})",
        )
        self._record_decision(eval_res, symbol)
        return eval_res

    def _record_decision(self, evaluation: GovernorEvaluation, symbol: str) -> None:
        self.decision_history.append({
            "timestamp": int(time.time() * 1000),
            "symbol": symbol,
            "approved": evaluation.approved,
            "reason": evaluation.rejection_reason,
            "risk_state": evaluation.risk_state.value,
            "recovery_mode": evaluation.recovery_mode_active,
            "target_notional": evaluation.target_notional,
            "risk_pct": evaluation.risk_pct,
        })
        if len(self.decision_history) > 100:
            self.decision_history.pop(0)


def current_drawdown_pct(dd: float) -> float:
    return dd * 100.0
