"""
Risk Management Engine.
Enforces Principle 5: The Risk Manager has absolute veto authority over all strategy signals.
Controls position sizing (1.0% equity risk), cash reserve (5%), gross exposure (1.0x),
trailing stops (+1R breakeven, +2R trailing), rolling 24h drawdown breaker (3.5% -> 6h freeze),
and maximum drawdown breaker (6.0% -> permanent halt and liquidation).
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Any, Dict, Optional, Tuple

from config.trading_params import RiskControlsConfig, TrailingStopConfig
from core.strategy_engine import Signal
from logs.audit_logger import AuditLogger
from state.portfolio_tracker import PortfolioTracker


@dataclass
class RiskDecision:
    approved: bool
    adjusted_quantity: float
    adjusted_price: float
    stop_loss: float = 0.0
    take_profit_1: float = 0.0
    take_profit_2: float = 0.0
    risk_capital: float = 0.0
    risk_pct: float = 0.0
    reason: str = ""
    circuit_breaker_active: bool = False


class RiskManager:
    """
    Central risk gate and position sizing controller.
    """

    def __init__(
        self,
        portfolio: PortfolioTracker,
        risk_config: Optional[RiskControlsConfig] = None,
        trailing_config: Optional[TrailingStopConfig] = None,
        max_risk_per_trade_pct: float = 0.01,
        max_gross_exposure_pct: float = 1.00,
        min_cash_reserve_pct: float = 0.05,
        max_open_positions: int = 2,
        audit_logger: Optional[AuditLogger] = None,
        order_manager: Optional[Any] = None,
    ):
        self.portfolio = portfolio
        self.risk_config = risk_config or RiskControlsConfig()
        self.trailing_config = trailing_config or TrailingStopConfig()
        self.max_risk_per_trade_pct = max_risk_per_trade_pct
        self.max_gross_exposure_pct = max_gross_exposure_pct
        self.min_cash_reserve_pct = min_cash_reserve_pct
        self.max_open_positions = max_open_positions
        self.audit_logger = audit_logger
        self.order_manager = order_manager

        # Circuit breaker states
        self.permanent_kill_switch: bool = False
        self.freeze_until_timestamp: float = 0.0

    @property
    def is_frozen(self) -> bool:
        return time.time() < self.freeze_until_timestamp

    def reset_circuit_breaker(self) -> None:
        """Reset circuit breaker flags when reconciliation aligns capital or operator clears freeze."""
        self.permanent_kill_switch = False
        self.freeze_until_timestamp = 0.0


    def check_circuit_breakers(self) -> Tuple[bool, str]:
        """
        Evaluate portfolio drawdown thresholds.
        Returns: (is_tripped, action_required)
        """
        if not self.risk_config.enforce_circuit_breakers:
            return False, "Circuit breakers disabled"

        # Check permanent kill switch
        if self.permanent_kill_switch:
            return True, "PERMANENT_HALT: Maximum drawdown limit reached"

        curr_time = time.time()
        # Check temporary freeze
        if curr_time < self.freeze_until_timestamp:
            remaining_min = (self.freeze_until_timestamp - curr_time) / 60.0
            return True, f"FROZEN: Rolling 24h drawdown freeze active ({remaining_min:.1f} min remaining)"

        # 1. Check Maximum Peak-to-Trough Drawdown (Section 17.2: 6.0%)
        max_dd = self.portfolio.get_current_drawdown()
        if max_dd >= self.risk_config.max_drawdown_limit:
            self.permanent_kill_switch = True
            if self.audit_logger:
                self.audit_logger.log_circuit_breaker(
                    breaker_type="MAX_DRAWDOWN",
                    current_drawdown=max_dd,
                    threshold=self.risk_config.max_drawdown_limit,
                    action="LIQUIDATE_ALL_PERMANENT_HALT",
                    reason="Portfolio peak-to-trough drawdown reached 6.0% maximum circuit breaker",
                )
            return True, "MAX_DRAWDOWN_BREAKER_TRIGGERED"

        # 2. Check Rolling 24-hour Drawdown (Section 17.1: 3.5%)
        rolling_dd = self.portfolio.get_rolling_24h_drawdown()
        if rolling_dd >= self.risk_config.rolling_24h_drawdown_limit:
            self.freeze_until_timestamp = curr_time + (self.risk_config.freeze_duration_hours * 3600.0)
            freeze_str = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(self.freeze_until_timestamp))
            if self.audit_logger:
                self.audit_logger.log_circuit_breaker(
                    breaker_type="ROLLING_24H_DRAWDOWN",
                    current_drawdown=rolling_dd,
                    threshold=self.risk_config.rolling_24h_drawdown_limit,
                    action="CLOSE_ALL_FREEZE_6H",
                    freeze_until=freeze_str,
                    reason="Rolling 24h drawdown reached 3.5%. Entries frozen for 6 hours.",
                )
            return True, "ROLLING_DRAWDOWN_BREAKER_TRIGGERED"

        return False, "Normal Risk State"

    def evaluate_signal(
        self,
        signal: Signal,
        symbol_precision: Optional[Dict[str, Any]] = None,
    ) -> RiskDecision:
        """
        Validate and size incoming trade signals.
        Enforces position sizing, stop requirements, cash reserves, and exposure limits.
        """
        # Circuit breaker verification
        is_tripped, breaker_msg = self.check_circuit_breakers()
        if is_tripped:
            return RiskDecision(
                approved=False,
                adjusted_quantity=0.0,
                adjusted_price=0.0,
                stop_loss=0.0,
                take_profit_1=0.0,
                take_profit_2=0.0,
                risk_capital=0.0,
                risk_pct=0.0,
                reason=f"Rejected: Circuit breaker active ({breaker_msg})",
                circuit_breaker_active=True,
            )

        # De-risk signals are approved to facilitate capital preservation and partial scaling (e.g. TP1)
        if signal.direction == "DE_RISK":
            current_pos = self.portfolio.positions.get(signal.symbol)
            if not current_pos or current_pos.quantity <= 1e-7:
                return RiskDecision(
                    approved=False,
                    adjusted_quantity=0.0,
                    adjusted_price=signal.entry_price,
                    reason=f"Rejected: No active position to de-risk for {signal.symbol}",
                )

            # Check if this is a partial exit (e.g. TP1 at 50%)
            exit_ratio = float(signal.metadata.get("exit_ratio", 1.0))
            if 0.0 < exit_ratio < 1.0:
                qty = current_pos.quantity * exit_ratio
            else:
                qty = current_pos.quantity

            return RiskDecision(
                approved=True,
                adjusted_quantity=qty,
                adjusted_price=signal.entry_price,
                stop_loss=0.0,
                take_profit_1=0.0,
                take_profit_2=0.0,
                risk_capital=0.0,
                risk_pct=0.0,
                reason=f"De-risk signal approved ({exit_ratio * 100:.0f}% exit)",
            )

        # Signal must be a BUY
        if signal.direction != "BUY":
            return RiskDecision(
                approved=False,
                adjusted_quantity=0.0,
                adjusted_price=0.0,
                stop_loss=0.0,
                take_profit_1=0.0,
                take_profit_2=0.0,
                risk_capital=0.0,
                risk_pct=0.0,
                reason=f"Rejected: Direction '{signal.direction}' not actionable",
            )

        # Multi-layer duplicate entry protection (Section 3)
        # Check A: Reject if symbol already has an open position
        existing_pos = self.portfolio.positions.get(signal.symbol)
        if existing_pos and existing_pos.quantity > 1e-7:
            return RiskDecision(
                approved=False,
                adjusted_quantity=0.0,
                adjusted_price=0.0,
                reason=f"DUPLICATE_ENTRY_REJECTED: Existing position active ({existing_pos.quantity} {existing_pos.base_coin}) for {signal.symbol}",
            )

        # Check B: Reject if symbol has an active pending, submitting, or UNKNOWN order
        if getattr(self, "order_manager", None):
            is_locked, lock_reason = self.order_manager.is_symbol_entry_locked(signal.symbol)
            if is_locked:
                return RiskDecision(
                    approved=False,
                    adjusted_quantity=0.0,
                    adjusted_price=0.0,
                    reason=f"Rejected: {lock_reason}",
                )

        # Max open positions check across portfolio
        active_pos_count = len([p for p in self.portfolio.positions.values() if p.quantity > 1e-7])
        if active_pos_count >= self.max_open_positions:
            return RiskDecision(
                approved=False,
                adjusted_quantity=0.0,
                adjusted_price=0.0,
                stop_loss=0.0,
                take_profit_1=0.0,
                take_profit_2=0.0,
                risk_capital=0.0,
                risk_pct=0.0,
                reason=f"Rejected: Max open positions limit ({self.max_open_positions}) reached",
            )

        entry_px = signal.entry_price
        stop_px = signal.stop_loss
        stop_distance = entry_px - stop_px

        # Stop loss validation: Must be below entry and non-trivial
        if stop_distance <= 0:
            return RiskDecision(
                approved=False,
                adjusted_quantity=0.0,
                adjusted_price=0.0,
                stop_loss=0.0,
                take_profit_1=0.0,
                take_profit_2=0.0,
                risk_capital=0.0,
                risk_pct=0.0,
                reason=f"Rejected: Invalid stop loss {stop_px} >= entry {entry_px}",
            )

        if (stop_distance / entry_px) < 0.0015:
            return RiskDecision(
                approved=False,
                adjusted_quantity=0.0,
                adjusted_price=0.0,
                stop_loss=0.0,
                take_profit_1=0.0,
                take_profit_2=0.0,
                risk_capital=0.0,
                risk_pct=0.0,
                reason="Rejected: Stop distance tighter than minimum 0.15% threshold",
            )

        equity = self.portfolio.total_equity
        if equity <= 0:
            return RiskDecision(
                approved=False,
                adjusted_quantity=0.0,
                adjusted_price=0.0,
                stop_loss=0.0,
                take_profit_1=0.0,
                take_profit_2=0.0,
                risk_capital=0.0,
                risk_pct=0.0,
                reason="Rejected: Portfolio equity is zero or negative",
            )

        # ---------------------------------------------------------------------
        # Position Sizing (Section 16)
        # Risk Capital = Portfolio Equity * Risk % (Default 1.0%)
        # Position Quantity = Risk Capital / abs(Entry Price - Stop Price)
        # ---------------------------------------------------------------------
        risk_pct = min(self.max_risk_per_trade_pct, 0.015)  # Strict cap at 1.5%
        risk_capital = equity * risk_pct
        desired_quantity = risk_capital / stop_distance
        desired_notional = desired_quantity * entry_px

        # Cap 1: Available Cash after mandatory 5% cash reserve
        available_cash = self.portfolio.available_cash
        if desired_notional > available_cash:
            desired_quantity = available_cash / entry_px
            desired_notional = desired_quantity * entry_px

        # Cap 2: Gross exposure limit (100% equity max, no leverage)
        current_pos_val = sum(p.notional_value for p in self.portfolio.positions.values())
        max_allowed_pos_val = equity * self.max_gross_exposure_pct
        remaining_exposure = max(0.0, max_allowed_pos_val - current_pos_val)
        if desired_notional > remaining_exposure:
            desired_quantity = remaining_exposure / entry_px
            desired_notional = desired_quantity * entry_px

        # Cap 3: Single asset concentration cap (max 50% equity)
        max_single_notional = equity * 0.50
        if desired_notional > max_single_notional:
            desired_quantity = max_single_notional / entry_px
            desired_notional = desired_quantity * entry_px

        # Align with exchange precision rules if provided
        amount_precision = 6
        min_order_usd = 1.0
        if symbol_precision:
            amount_precision = int(symbol_precision.get("AmountPrecision", 6))
            min_order_usd = float(symbol_precision.get("MiniOrder", 1.0))

        # Truncate quantity down to precision
        factor = 10 ** amount_precision
        adjusted_qty = math.floor(desired_quantity * factor) / factor

        final_notional = adjusted_qty * entry_px
        if final_notional < min_order_usd:
            return RiskDecision(
                approved=False,
                adjusted_quantity=0.0,
                adjusted_price=entry_px,
                stop_loss=stop_px,
                take_profit_1=signal.take_profit_1,
                take_profit_2=signal.take_profit_2,
                risk_capital=risk_capital,
                risk_pct=risk_pct,
                reason=f"Rejected: Sized notional (${final_notional:.2f}) below exchange minimum (${min_order_usd:.2f})",
            )

        actual_risk_capital = adjusted_qty * stop_distance
        actual_risk_pct = actual_risk_capital / equity

        return RiskDecision(
            approved=True,
            adjusted_quantity=adjusted_qty,
            adjusted_price=entry_px,
            stop_loss=stop_px,
            take_profit_1=signal.take_profit_1,
            take_profit_2=signal.take_profit_2,
            risk_capital=actual_risk_capital,
            risk_pct=actual_risk_pct,
            reason="Approved: Position sized under 1.0% risk, 5% cash reserve, and 100% exposure limits.",
        )

    def update_trailing_stop(self, symbol: str, current_price: float, atr: float = 0.0) -> Optional[float]:
        """
        Evaluate and update trailing stop for an active position.
        - Initial: hard stop.
        - At +1.0R: move stop to breakeven (entry_price + fee buffer).
        - At +2.0R: activate trailing stop (highest_price - atr_multiplier * ATR).
        - Stop cannot move further away from entry (ratchet only).
        """
        pos = self.portfolio.positions.get(symbol)
        if not pos or pos.quantity <= 0:
            return None

        entry = pos.entry_price
        curr_stop = pos.stop_loss
        r_dist = entry - curr_stop if entry > curr_stop else entry * 0.01

        profit_r = (current_price - entry) / r_dist if r_dist > 0 else 0.0

        new_stop = curr_stop

        # +1.0R rule: Breakeven (Section 18)
        if profit_r >= self.trailing_config.breakeven_trigger_r:
            be_level = entry * 1.0015  # Covers entry and exit fees
            new_stop = max(new_stop, be_level)

        # +2.0R rule: Trailing Stop
        if profit_r >= self.trailing_config.trail_activation_r:
            trail_dist = (atr * self.trailing_config.atr_multiplier) if atr > 0 else (r_dist * 1.5)
            trail_level = pos.highest_price - trail_dist
            new_stop = max(new_stop, trail_level)

        # Guarantee stop only moves upward for long position
        if new_stop > curr_stop:
            pos.stop_loss = round(new_stop, 4)
            if self.audit_logger:
                self.audit_logger.log_system_event(
                    "TRAILING_STOP_UPDATE",
                    f"{symbol} stop ratcheted from {curr_stop:.2f} to {pos.stop_loss:.2f} (Profit: +{profit_r:.2f}R)",
                )
            return pos.stop_loss

        return None
