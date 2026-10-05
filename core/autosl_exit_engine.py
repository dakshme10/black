"""
AutoSL Exit Engine for Crypto Trading.
=====================================
Production implementation of the AutoSL two-phase position lifecycle:
1. Phase 1: Validation Window (Breakout Failed Exit / Early Invalidation)
   - 4-condition confluence check: Price dropped below entry + Failed expansion +
     Surrendered structural breakout level + Completed volume drop.
   - Avoids the 0-second trap via elapsed candle seconds tracking.
   - Cuts losses early (-0.2% to -0.5%) instead of a full (-2.0%) SL hit.
2. Phase 2: Validated Trend & Dual-Layer Trailing Stop Loss
   - Layer A: Step-based Hard Stop ratcheting on exchange orderbook (avoids rate limits).
   - Layer B: Internal Soft Dynamic SL tracking tick-by-tick with profit protection tiers.
   - IRON RULE: Stop loss only tightens, NEVER widens.
3. Fast Momentum Extension & Profit Ratchet
   - Extends TP targets on fast momentum breakouts (< 180s).
   - Ratchets stop loss to lock in 50% of original target distance per level.
   - Transitions to FINAL_TRAILING once max extensions are reached.

Thread-safe, tick-driven, long/short side aware, and exchange-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import math
import threading
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("AutoSLExitEngine")


@dataclass
class CryptoPosition:
    """
    Position state tracked by the AutoSL Exit Engine.
    Compatible with both Spot and Perpetual Futures (Long/Short).
    """
    position_id: str
    symbol: str
    side: str  # "BUY" (Long) or "SELL" (Short)
    quantity: float
    entry_price: float
    entry_time: datetime

    # Structural Breakout Context
    entry_breakout_level: float  # Resistance broken for BUY, Support for SELL
    entry_candle_volume: float = 0.0
    prev_candle_volume: float = 0.0

    # Stop Loss & Take Profit
    initial_stop_loss: float = 0.0
    stop_loss_price: float = 0.0
    take_profit_price: float = 0.0
    take_profit_1: float = 0.0
    take_profit_1_hit: bool = False
    take_profit_2: float = 0.0
    take_profit_2_hit: bool = False
    broker_sl_price: float = 0.0  # Software synthetic stop (Roostoo lacks native stops)
    sl_percent: float = 2.0       # Current dynamic SL %

    # State Tracking
    status: str = "OPEN"          # "OPEN", "EXITING", "CLOSED"
    exit_reason: str = ""
    current_price: float = 0.0
    peak_price: float = 0.0
    peak_profit_points: float = 0.0
    locked_profit: float = 0.0

    # Phase 1 vs Phase 2 Validation
    candles_since_entry: int = 0
    last_candle_time: Optional[datetime] = None
    validation_survived: bool = False
    volume_drop_detected: bool = False
    trailing_sl_active: bool = False
    is_exit_initiated: bool = False

    # Momentum Extension State
    momentum_status: str = "NORMAL"  # "NORMAL", "MOMENTUM_EXTENSION_1", ..., "FINAL_TRAILING"
    original_target_distance: float = 0.0
    extension_level: int = 0
    last_sl_recalc_time: float = 0.0

    # Additional Exchange Order Reference
    broker_sl_order_id: Optional[str] = None
    broker_tp_order_id: Optional[str] = None

    def __post_init__(self):
        if self.side:
            self.side = self.side.upper()
        if self.peak_price == 0.0:
            self.peak_price = self.entry_price
        if self.current_price == 0.0:
            self.current_price = self.entry_price
        if self.stop_loss_price == 0.0 and self.initial_stop_loss > 0:
            self.stop_loss_price = self.initial_stop_loss
        if self.broker_sl_price == 0.0 and self.initial_stop_loss > 0:
            self.broker_sl_price = self.initial_stop_loss
        if self.original_target_distance == 0.0 and self.take_profit_price > 0:
            self.original_target_distance = abs(self.take_profit_price - self.entry_price)
        if self.last_candle_time is None:
            self.last_candle_time = self.entry_time

    @property
    def is_long(self) -> bool:
        return self.side in ("BUY", "LONG")

    @property
    def phase(self) -> str:
        if self.momentum_status.startswith("MOMENTUM_EXTENSION") or self.momentum_status == "FINAL_TRAILING":
            return self.momentum_status
        if self.validation_survived:
            return "PHASE_2_VALIDATED"
        return "PHASE_1_VALIDATION"

    @property
    def unrealized_pnl(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        if self.is_long:
            return (self.current_price - self.entry_price) * self.quantity
        return (self.entry_price - self.current_price) * self.quantity

    @property
    def unrealized_pnl_pct(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        if self.is_long:
            return ((self.current_price / self.entry_price) - 1.0) * 100.0
        return ((self.entry_price / self.current_price) - 1.0) * 100.0

    @property
    def peak_expansion_pct(self) -> float:
        if self.entry_price <= 0 or self.peak_price <= 0:
            return 0.0
        if self.is_long:
            return ((self.peak_price / self.entry_price) - 1.0) * 100.0
        return ((self.entry_price / self.peak_price) - 1.0) * 100.0


class AutoSLExitEngine:
    """
    Production-grade, thread-safe AutoSL Exit Engine.
    Executes early invalidation, dual-layer dynamic trailing, and momentum extensions.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self._lock = threading.Lock()
        default_config: Dict[str, Any] = {
            # Validation Window (Phase 1)
            "validation_candles": 2,
            "candle_timeframe_seconds": 60,   # 1-minute default candle size
            "min_expansion_percent": 1.0,     # Must expand >= 1.0% to confirm breakout
            "phase2_profit_activation": 2.0,  # +2.0% profit triggers early Phase 2 graduation

            # Trailing SL Configuration
            "hard_sl_trailing_step": 10.0,    # Discrete hard stop ratcheting step
            "hard_sl_step_usd": {
                "BTC/USD": 50.0,
                "ETH/USD": 4.0,
                "SOL/USD": 0.5,
                "SUI/USD": 0.01,
                "ADA/USD": 0.002,
                "FET/USD": 0.005,
                "ENA/USD": 0.002,
                "STO/USD": 0.001,
                "S/USD": 0.002,
                "PUMP/USD": 0.00001,
                "BONK/USD": 0.0000001,
                "PEPE/USD": 0.00000005,
                "BTCUSDT": 50.0,
                "ETHUSDT": 4.0,
                "SOLUSDT": 0.5,
                "SUIUSDT": 0.01,
                "ADAUSDT": 0.002,
                "FETUSDT": 0.005,
                "ENAUSDT": 0.002,
                "STOUSDT": 0.001,
                "SUSDT": 0.002,
                "PUMPUSDT": 0.00001,
                "BONKUSDT": 0.0000001,
                "PEPEUSDT": 0.00000005,
            },
            "recalc_interval_seconds": 60.0,  # Mid-trade dynamic SL recalculation interval
            "min_tick_buffer": 0.5,           # Minimum price movement to adjust SL
            "tick_size": 0.1,                 # Default tick size for rounding

            # Multi-Tier Profit Protection Tiers (Crypto Calibrated)
            # Unrealized Profit % -> Max Allowed SL Distance %
            "profit_protection_tiers": [
                {"profit_pct": 10.0, "sl_percent": 0.4},
                {"profit_pct": 5.0,  "sl_percent": 0.6},
                {"profit_pct": 3.0,  "sl_percent": 0.9},
                {"profit_pct": 1.5,  "sl_percent": 1.2},
            ],

            # Fast Momentum Extension
            "momentum_extension_enabled": True,
            "fast_target_max_seconds": 180.0,   # 3 minutes for fast breakout test
            "max_extensions": 3,
            "profit_lock_ratio": 0.50,          # Lock 50% of target distance per level
            "extreme_range_threshold_pct": 0.2, # 0.2% tolerance to market extreme high/low
        }

        if config:
            # Deep merge config
            default_config.update(config)
        self.config = default_config

    def get_symbol_step(self, symbol: str, current_price: float = 0.0) -> float:
        """Fetch discrete hard stop step for symbol or fall back to dynamic percentage or default."""
        steps = self.config.get("hard_sl_step_usd", {})
        if symbol in steps:
            return float(steps[symbol])
        clean_sym = symbol.replace("/", "")
        if clean_sym in steps:
            return float(steps[clean_sym])
        if current_price > 0:
            return max(self.config.get("tick_size", 0.00000001), current_price * 0.0005)
        return float(self.config.get("hard_sl_trailing_step", 10.0))

    def round_to_tick(self, price: float, tick_size: Optional[float] = None) -> float:
        """Round price to the exchange symbol tick size."""
        tick = tick_size if tick_size is not None else self.config.get("tick_size", 0.1)
        if tick <= 0:
            return round(price, 4)
        return round(round(price / tick) * tick, 8)

    # ─────────────────────────────────────────────────────────────
    # Main Hook: Call on every market tick or price update
    # ─────────────────────────────────────────────────────────────
    def on_tick(
        self,
        pos: CryptoPosition,
        current_price: float,
        current_candle_vol: float = 0.0,
        prev_completed_vol: float = 0.0,
        prev_prev_completed_vol: float = 0.0,
        recent_market_high: float = 0.0,
        recent_market_low: float = 0.0,
        current_time: Optional[datetime] = None,
        tick_size: Optional[float] = None,
        is_stale_data: bool = False,
    ) -> Tuple[Optional[str], Optional[float]]:
        """
        Processes a new market tick for an open position.
        Returns:
            (exit_signal_reason, trigger_price) or (None, None) if position remains open.
        """
        with self._lock:
            if pos.status != "OPEN" or pos.is_exit_initiated:
                return None, None

            if current_price <= 0:
                return None, None

            current_time = current_time or datetime.now(timezone.utc)
            pos.current_price = current_price

            effective_tick = tick_size or self.config.get("tick_size", 0.1)

            # 1. Update High-Water Mark (Peak Price) & Profit (Skip updating peak if data is stale)
            if not is_stale_data:
                if pos.is_long:
                    pos.peak_price = max(pos.peak_price, current_price)
                else:
                    pos.peak_price = min(pos.peak_price, current_price) if pos.peak_price > 0 else current_price

            if pos.is_long:
                profit_points = pos.peak_price - pos.entry_price
                profit_pct = ((current_price / pos.entry_price) - 1.0) * 100.0 if pos.entry_price > 0 else 0.0
            else:
                profit_points = pos.entry_price - pos.peak_price
                profit_pct = ((pos.entry_price / current_price) - 1.0) * 100.0 if current_price > 0 else 0.0

            # 2. Check Absolute Stop Loss Hit First (Always checked even if data is stale for safety)
            if pos.stop_loss_price > 0:
                if pos.is_long and current_price <= pos.stop_loss_price:
                    return self._initiate_exit(pos, "SL_HIT", pos.stop_loss_price)
                if (not pos.is_long) and current_price >= pos.stop_loss_price:
                    return self._initiate_exit(pos, "SL_HIT", pos.stop_loss_price)

            # If market data is stale, maintain existing protection but DO NOT ratchet stops or evaluate fresh profit targets
            if is_stale_data:
                return None, None

            # 3. Check Take Profit 1 (Partial 50% Scaling) & Take Profit 2 / Momentum Extension
            if pos.take_profit_1 > 0 and not pos.take_profit_1_hit:
                tp1_hit = (pos.is_long and current_price >= pos.take_profit_1) or (not pos.is_long and current_price <= pos.take_profit_1)
                if tp1_hit:
                    pos.take_profit_1_hit = True
                    # Breakeven ratchet upon TP1 hit: move stop loss to entry price + fee buffer
                    be_stop = pos.entry_price * 1.001 if pos.is_long else pos.entry_price * 0.999
                    if pos.is_long and be_stop > pos.stop_loss_price:
                        pos.stop_loss_price = self.round_to_tick(be_stop, effective_tick)
                    elif (not pos.is_long) and be_stop < pos.stop_loss_price:
                        pos.stop_loss_price = self.round_to_tick(be_stop, effective_tick)
                    return self._initiate_exit(pos, "TP1_HIT", pos.take_profit_1)

            target_tp2 = pos.take_profit_2 if pos.take_profit_2 > 0 else pos.take_profit_price
            if target_tp2 > 0 and (pos.take_profit_1_hit or pos.take_profit_1 <= 0):
                tp2_hit = (pos.is_long and current_price >= target_tp2) or (not pos.is_long and current_price <= target_tp2)
                if tp2_hit:
                    market_high = recent_market_high if recent_market_high > 0 else pos.peak_price
                    market_low = recent_market_low if recent_market_low > 0 else pos.peak_price
                    if self._can_extend_momentum(pos, current_time, market_high, market_low):
                        self._apply_momentum_extension(pos, effective_tick)
                    else:
                        pos.take_profit_2_hit = True
                        reason = "TP2_HIT" if pos.take_profit_2 > 0 else "TP_HIT"
                        return self._initiate_exit(pos, reason, target_tp2)
            elif target_tp2 <= 0 and self._is_tp_reached(pos, current_price):
                market_high = recent_market_high if recent_market_high > 0 else pos.peak_price
                market_low = recent_market_low if recent_market_low > 0 else pos.peak_price
                if self._can_extend_momentum(pos, current_time, market_high, market_low):
                    self._apply_momentum_extension(pos, effective_tick)
                else:
                    return self._initiate_exit(pos, "TP_HIT", pos.take_profit_price)

            # 4. Update Candle Progression & Volume Drop Detection
            self._update_candle_tracking(pos, current_time, prev_completed_vol, prev_prev_completed_vol)

            # 5. Evaluate Phase 1 vs Phase 2
            validation_candles = self.config["validation_candles"]
            min_expansion = self.config["min_expansion_percent"]
            phase2_activation_pct = self.config["phase2_profit_activation"]

            # Phase 1: Breakout Validation Check
            if pos.candles_since_entry <= validation_candles and not pos.validation_survived:
                # Check Initial SL breach during Phase 1
                if pos.initial_stop_loss > 0:
                    if pos.is_long and current_price <= pos.initial_stop_loss:
                        return self._initiate_exit(pos, "FAILED_BREAKOUT_EXIT", current_price)
                    if (not pos.is_long) and current_price >= pos.initial_stop_loss:
                        return self._initiate_exit(pos, "FAILED_BREAKOUT_EXIT", current_price)

                # Check 4-Condition Confluence Invalidation (after first candle has closed, > 0)
                if pos.candles_since_entry > 0:
                    is_failed = self._check_breakout_failure(pos, current_price, min_expansion)
                    if is_failed:
                        full_sl_loss_pct = (
                            abs((pos.initial_stop_loss - pos.entry_price) / pos.entry_price * 100.0)
                            if pos.initial_stop_loss > 0 and pos.entry_price > 0
                            else 2.0
                        )
                        actual_loss_pct = abs(profit_pct)
                        saved_pct = max(0.0, full_sl_loss_pct - actual_loss_pct)
                        logger.warning(
                            f"[FAILED_BREAKOUT_EXIT] {pos.symbol} ({pos.side}): "
                            f"Entry={pos.entry_price}, Current={current_price}, BreakoutLvl={pos.entry_breakout_level}, "
                            f"PeakExp={pos.peak_expansion_pct:.2f}%. Loss={profit_pct:.2f}% (Saved ~{saved_pct:.2f}% vs full SL)."
                        )
                        return self._initiate_exit(pos, "FAILED_BREAKOUT_EXIT", current_price)

                # Even in Phase 1, trail SL if the trade is in profit (> 0.5%)
                if profit_pct > 0.5:
                    self._update_soft_trailing_sl(pos, effective_tick)

                # Graduate to Phase 2 early if price exploded into strong profit
                if profit_pct >= phase2_activation_pct:
                    pos.validation_survived = True
                    logger.info(
                        f"[PHASE2_GRADUATION] {pos.symbol} reached +{profit_pct:.2f}% profit >= {phase2_activation_pct}%. Validated!"
                    )

            # Phase 2: Post-Validation Active Trailing
            if pos.candles_since_entry > validation_candles or profit_pct >= phase2_activation_pct:
                pos.validation_survived = True

                # Recalculate dynamic SL % periodically (tighter only)
                self._recalculate_dynamic_sl(pos, current_time, profit_pct)

                # Execute Soft Trailing SL calculation
                self._update_soft_trailing_sl(pos, effective_tick)

                # Check Hard Broker Stop Trailing Step
                step = self.get_symbol_step(pos.symbol, current_price=pos.current_price)
                self._update_hard_broker_sl(pos, profit_points, step, effective_tick)

            return None, None

    # ─────────────────────────────────────────────────────────────
    # Internal Evaluation Helpers
    # ─────────────────────────────────────────────────────────────
    def _check_breakout_failure(self, pos: CryptoPosition, current_price: float, min_expansion: float) -> bool:
        """
        AutoSL 4-Condition Confluence Check for Failed Breakout:
        1. Price dropped below entry (or rose above entry for Short)
        2. Peak price never achieved minimum breakout expansion %
        3. Price surrendered the structural breakout level
        4. Follow-through completed volume dried up
        """
        if pos.is_long:
            price_dropped = current_price < pos.entry_price
            peak_expansion_pct = ((pos.peak_price / pos.entry_price) - 1.0) * 100.0 if pos.entry_price > 0 else 0.0
            failed_expansion = peak_expansion_pct < min_expansion
            lost_breakout = current_price <= pos.entry_breakout_level
            weak_volume = pos.volume_drop_detected
            return bool(price_dropped and failed_expansion and lost_breakout and weak_volume)
        else:
            price_dropped = current_price > pos.entry_price
            peak_expansion_pct = ((pos.entry_price / pos.peak_price) - 1.0) * 100.0 if pos.peak_price > 0 else 0.0
            failed_expansion = peak_expansion_pct < min_expansion
            lost_breakout = current_price >= pos.entry_breakout_level
            weak_volume = pos.volume_drop_detected
            return bool(price_dropped and failed_expansion and lost_breakout and weak_volume)

    def _update_candle_tracking(
        self,
        pos: CryptoPosition,
        current_time: datetime,
        prev_vol: float,
        prev_prev_vol: float,
    ) -> None:
        """
        Tracks candle progression and detects completed volume drops.
        Enforces elapsed seconds check to prevent the 0-second boundary trap.
        """
        tf_seconds = self.config.get("candle_timeframe_seconds", 60)
        if pos.last_candle_time is None:
            pos.last_candle_time = current_time
            return

        elapsed = (current_time - pos.last_candle_time).total_seconds()
        if elapsed >= tf_seconds:
            # Advance candle counter
            pos.candles_since_entry += 1
            pos.last_candle_time = current_time

            # Evaluate completed candle volume drop:
            # completed_vol = candle[T-1].volume, prev_completed_vol = candle[T-2].volume
            if prev_prev_vol > 0 and prev_vol < prev_prev_vol:
                pos.volume_drop_detected = True
            else:
                pos.volume_drop_detected = False

    def _update_soft_trailing_sl(self, pos: CryptoPosition, tick_size: float) -> bool:
        """
        Tightens pos.stop_loss_price based on peak price and current sl_percent.
        Enforces AutoSL core rule: Only ever tightens (never widens).
        Returns True if stop was tightened.
        """
        min_tick = self.config.get("min_tick_buffer", 0.5)

        if pos.is_long:
            potential_sl = pos.peak_price * (1.0 - (pos.sl_percent / 100.0))
            if potential_sl > pos.stop_loss_price and (potential_sl - pos.stop_loss_price) >= min_tick:
                new_sl = self.round_to_tick(potential_sl, tick_size)
                if new_sl > pos.stop_loss_price:
                    pos.stop_loss_price = new_sl
                    pos.trailing_sl_active = True
                    pos.locked_profit = (new_sl - pos.entry_price) * pos.quantity
                    logger.info(
                        f"[TSL_LONG] {pos.symbol}: Ratchet Stop={new_sl}, Peak={pos.peak_price}, "
                        f"LockedProfit=${pos.locked_profit:.2f}"
                    )
                    return True
        else:
            potential_sl = pos.peak_price * (1.0 + (pos.sl_percent / 100.0))
            if potential_sl < pos.stop_loss_price and (pos.stop_loss_price - potential_sl) >= min_tick:
                new_sl = self.round_to_tick(potential_sl, tick_size)
                if new_sl < pos.stop_loss_price:
                    pos.stop_loss_price = new_sl
                    pos.trailing_sl_active = True
                    pos.locked_profit = (pos.entry_price - new_sl) * pos.quantity
                    logger.info(
                        f"[TSL_SHORT] {pos.symbol}: Ratchet Stop={new_sl}, Peak={pos.peak_price}, "
                        f"LockedProfit=${pos.locked_profit:.2f}"
                    )
                    return True
        return False

    def _update_hard_broker_sl(
        self,
        pos: CryptoPosition,
        profit_points: float,
        step: float,
        tick_size: float,
    ) -> bool:
        """
        Step-based exchange orderbook hard stop ratcheting.
        Ratchets forward in discrete steps (e.g. $50 on BTC) to prevent API rate-limit exhaustion.
        Returns True if hard stop was moved.
        """
        if step <= 0 or pos.broker_sl_price <= 0:
            return False

        if profit_points >= pos.peak_profit_points + step:
            steps_moved = int((profit_points - pos.peak_profit_points) // step)
            total_points_to_trail = steps_moved * step

            if pos.is_long:
                new_broker_sl = self.round_to_tick(pos.broker_sl_price + total_points_to_trail, tick_size)
                if new_broker_sl > pos.broker_sl_price:
                    pos.peak_profit_points += total_points_to_trail
                    pos.broker_sl_price = new_broker_sl
                    logger.info(f"[HARD_SL_RATCHET_LONG] {pos.symbol}: Moved exchange stop to {new_broker_sl}")
                    return True
            else:
                new_broker_sl = self.round_to_tick(pos.broker_sl_price - total_points_to_trail, tick_size)
                if new_broker_sl < pos.broker_sl_price:
                    pos.peak_profit_points += total_points_to_trail
                    pos.broker_sl_price = new_broker_sl
                    logger.info(f"[HARD_SL_RATCHET_SHORT] {pos.symbol}: Moved exchange stop to {new_broker_sl}")
                    return True

        return False

    def _recalculate_dynamic_sl(self, pos: CryptoPosition, current_time: datetime, profit_pct: float) -> None:
        """
        Evaluates profit tiers to tighten sl_percent.
        Enforces AutoSL core rule: Never widen SL, only tighten!
        """
        now_ts = current_time.timestamp()
        recalc_interval = self.config.get("recalc_interval_seconds", 60.0)
        if (now_ts - pos.last_sl_recalc_time) < recalc_interval:
            return

        pos.last_sl_recalc_time = now_ts
        recommended_sl = pos.sl_percent

        tiers = self.config.get("profit_protection_tiers", [])
        # Tiers are evaluated from highest profit threshold downwards
        for tier in sorted(tiers, key=lambda t: t["profit_pct"], reverse=True):
            if profit_pct >= tier["profit_pct"]:
                recommended_sl = min(recommended_sl, float(tier["sl_percent"]))
                break

        # Iron rule: Never widen!
        if recommended_sl < pos.sl_percent:
            logger.info(
                f"[DYNAMIC_SL_TIGHTEN] {pos.symbol}: Tightening dynamic SL% from {pos.sl_percent}% -> {recommended_sl}%"
            )
            pos.sl_percent = recommended_sl

    def _is_tp_reached(self, pos: CryptoPosition, current_price: float) -> bool:
        if pos.momentum_status == "FINAL_TRAILING":
            return False  # Let trailing stop handle final exit

        if pos.take_profit_price <= 0:
            return False

        if pos.is_long and current_price >= pos.take_profit_price:
            return True
        if (not pos.is_long) and current_price <= pos.take_profit_price:
            return True
        return False

    def _can_extend_momentum(
        self,
        pos: CryptoPosition,
        current_time: datetime,
        recent_market_high: float,
        recent_market_low: float,
    ) -> bool:
        if not self.config.get("momentum_extension_enabled", True):
            return False

        max_ext = self.config.get("max_extensions", 3)
        if pos.extension_level >= max_ext:
            return False

        elapsed = (current_time - pos.entry_time).total_seconds()
        max_sec = self.config.get("fast_target_max_seconds", 180.0)
        if elapsed > max_sec:
            return False

        threshold_pct = self.config.get("extreme_range_threshold_pct", 0.2) / 100.0

        # Momentum check: price must be driving near the extreme high/low
        if pos.is_long:
            min_high = recent_market_high * (1.0 - threshold_pct)
            return pos.current_price >= min_high
        else:
            max_low = recent_market_low * (1.0 + threshold_pct)
            return pos.current_price <= max_low

    def _apply_momentum_extension(self, pos: CryptoPosition, tick_size: float) -> None:
        pos.extension_level += 1
        lock_pct = self.config.get("profit_lock_ratio", 0.50)

        if pos.original_target_distance <= 0:
            pos.original_target_distance = abs(pos.take_profit_price - pos.entry_price)

        dist = pos.original_target_distance

        if pos.is_long:
            pos.take_profit_price = self.round_to_tick(pos.take_profit_price + dist, tick_size)
            new_stop = pos.entry_price + (dist * pos.extension_level * lock_pct)
            pos.stop_loss_price = max(pos.stop_loss_price, self.round_to_tick(new_stop, tick_size))
        else:
            pos.take_profit_price = self.round_to_tick(pos.take_profit_price - dist, tick_size)
            new_stop = pos.entry_price - (dist * pos.extension_level * lock_pct)
            pos.stop_loss_price = min(pos.stop_loss_price, self.round_to_tick(new_stop, tick_size))

        max_ext = self.config.get("max_extensions", 3)
        if pos.extension_level >= max_ext:
            pos.momentum_status = "FINAL_TRAILING"
            pos.take_profit_price = float("inf") if pos.is_long else 0.0
            logger.info(
                f"[MOMENTUM_EXT] {pos.symbol}: Reached MAX extensions ({max_ext}). Switched to FINAL_TRAILING mode."
            )
        else:
            pos.momentum_status = f"MOMENTUM_EXTENSION_{pos.extension_level}"
            logger.info(
                f"[MOMENTUM_EXT] {pos.symbol}: Extended to Level {pos.extension_level}. "
                f"New TP={pos.take_profit_price}, Ratcheted Stop={pos.stop_loss_price}"
            )

    def _initiate_exit(self, pos: CryptoPosition, reason: str, trigger_price: float) -> Tuple[str, float]:
        pos.status = "EXITING"
        pos.is_exit_initiated = True
        pos.exit_reason = reason
        logger.info(f"[EXIT_TRIGGERED] {pos.symbol} ({pos.side}): Reason={reason}, Price={trigger_price}")
        return reason, trigger_price
