"""
Order Execution Engine.
Enforces execution safety protocols (Section 8, 32, 33):
State refresh -> available cash verification -> position verification -> risk validation ->
idempotent order submission (or simulated fill in DRY_RUN) -> UNKNOWN state protection -> reconciliation.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from config.trading_params import AppConfig, FeesConfig
from core.api_client import RoostooAPIError, RoostooClient, UnknownOrderStateError
from core.risk_manager import RiskDecision, RiskManager
from core.strategy_engine import Signal
from logs.audit_logger import AuditLogger
from state.order_state import Order, OrderStateManager, OrderStatus
from state.portfolio_tracker import PortfolioTracker


class OrderExecutor:
    """
    State-aware order routing, simulation in DRY_RUN, and exchange interaction in LIVE mode.
    """

    def __init__(
        self,
        config: AppConfig,
        api_client: RoostooClient,
        portfolio: PortfolioTracker,
        order_manager: OrderStateManager,
        risk_manager: RiskManager,
        fees_config: Optional[FeesConfig] = None,
        audit_logger: Optional[AuditLogger] = None,
    ):
        self.config = config
        self.client = api_client
        self.portfolio = portfolio
        self.order_manager = order_manager
        self.risk_manager = risk_manager
        self.fees = fees_config or FeesConfig()
        self.audit_logger = audit_logger
        self._lock = threading.RLock()

        # Exchange rules cache: {symbol: {PricePrecision, AmountPrecision, MiniOrder}}
        self._exchange_info: Dict[str, Any] = {}
        self.refresh_exchange_info()

    def refresh_exchange_info(self) -> None:
        """Fetch symbol precision and minimum order limits from exchange."""
        try:
            info = self.client.get_exchange_info()
            self._exchange_info = info.get("TradePairs", {})
        except Exception:
            pass

    def get_symbol_precision(self, symbol: str) -> Dict[str, Any]:
        return self._exchange_info.get(symbol, {
            "PricePrecision": 2,
            "AmountPrecision": 6,
            "MiniOrder": 1.0,
        })

    def cancel_opposing_orders(self, symbol: str) -> List[str]:
        """
        Cancel any resting or pending orders for the specified symbol.
        Used upon position exit (e.g. FAILED_BREAKOUT_EXIT, SL_HIT, TP_HIT)
        to prevent orphaned trigger orders from filling after exit.
        """
        cancelled_ids = []
        active_orders = self.order_manager.get_active_orders(symbol=symbol)
        for ord in active_orders:
            try:
                if not self.config.dry_run and self.config.live_trading_enabled and ord.exchange_order_id:
                    self.client.cancel_order(order_id=ord.exchange_order_id)
                self.order_manager.update_order(
                    client_order_id=ord.client_order_id,
                    status=OrderStatus.CANCELED,
                    error_msg="Cancelled opposing order upon position exit",
                )
                cancelled_ids.append(ord.client_order_id)
                if self.audit_logger:
                    self.audit_logger.log_order_event(
                        event="OPPOSING_ORDER_CANCELLED",
                        symbol=symbol,
                        side=ord.side,
                        order_type=ord.order_type,
                        quantity=ord.quantity,
                        client_order_id=ord.client_order_id,
                        status="CANCELED",
                        details={"reason": "Position exit cancellation"},
                    )
            except Exception as e:
                if self.audit_logger:
                    self.audit_logger.log_system_event(
                        "CANCEL_ORDER_ERROR",
                        f"Failed to cancel order {ord.client_order_id} for {symbol}: {e}",
                    )
        return cancelled_ids

    def update_exchange_stop_order(self, symbol: str, new_stop_price: float) -> bool:
        """
        Ratchet update for client-side software synthetic stop orders (Layer A/B).
        NOTE: Roostoo Mock Exchange v3 does not support native broker stop orders;
        this updates the authoritative in-memory software stop threshold.
        """
        with self._lock:
            pos = self.portfolio.positions.get(symbol)
            if not pos:
                return False
            pos.broker_sl_price = new_stop_price
            if self.audit_logger:
                self.audit_logger.log_system_event(
                    "SOFTWARE_SYNTHETIC_STOP_RATCHETED",
                    f"{symbol} software stop ratcheted to ${new_stop_price:.8g} (Roostoo lacks native stop orders)",
                    {"symbol": symbol, "broker_sl_price": new_stop_price}
                )
            return True

    def execute_decision(
        self,
        signal: Signal,
        risk_decision: RiskDecision,
        current_market_price: float,
    ) -> Optional[Order]:
        """
        Execute an approved trading decision under DRY_RUN or LIVE mode.
        Serialized through centralized execution coordinator (self._lock).
        Enforces:
        - Atomic entry reservation & duplicate BUY prevention
        - Atomic exit reservation & duplicate exit prevention
        - Incremental partial fill accounting without double-counting
        - Symbol entry freeze on UNKNOWN state
        """
        with self._lock:
            if not risk_decision.approved:
                if self.audit_logger:
                    self.audit_logger.log_decision(
                        strategy=signal.strategy,
                        symbol=signal.symbol,
                        action="REJECTED_BY_RISK",
                        signal_id="",
                        price=signal.entry_price,
                        size=0.0,
                        notional=0.0,
                        stop_loss=signal.stop_loss,
                        take_profit_1=signal.take_profit_1,
                        take_profit_2=signal.take_profit_2,
                        confidence=signal.confidence,
                        expected_rr=signal.expected_rr,
                        risk_percent=risk_decision.risk_pct,
                        reason=risk_decision.reason,
                        regime=signal.regime,
                        portfolio_equity=self.portfolio.total_equity,
                        cash_before=self.portfolio.cash,
                        status="REJECTED",
                    )
                return None

            # -----------------------------------------------------------------
            # 1. Atomic Reservation & Duplicate Protection
            # -----------------------------------------------------------------
            current_pos = self.portfolio.positions.get(signal.symbol)

            if signal.direction == "BUY":
                # Check A: Existing position blocks new BUY
                if current_pos and current_pos.quantity > 1e-7:
                    if self.audit_logger:
                        self.audit_logger.log_system_event(
                            "DUPLICATE_ENTRY_REJECTED",
                            f"Blocked BUY for {signal.symbol}: Active position ({current_pos.quantity} {current_pos.base_coin}) already exists",
                        )
                    return None

                # Check B: Pending / UNKNOWN order blocks new BUY
                is_locked, lock_reason = self.order_manager.is_symbol_entry_locked(signal.symbol)
                if is_locked:
                    if self.audit_logger:
                        self.audit_logger.log_system_event(
                            "ENTRY_LOCKED_REJECTED",
                            f"Blocked BUY for {signal.symbol}: {lock_reason}",
                        )
                    return None

                # Atomically reserve entry
                self.order_manager.lock_symbol(
                    signal.symbol,
                    reason=f"Order submission pending for {signal.symbol}",
                    ttl_seconds=60.0,
                )
                side = "BUY"
                qty = risk_decision.adjusted_quantity

            elif signal.direction == "DE_RISK":
                if not current_pos or current_pos.quantity <= 1e-7:
                    if self.audit_logger:
                        self.audit_logger.log_system_event(
                            "DE_RISK_REJECTED_NO_POSITION",
                            f"Blocked DE_RISK for {signal.symbol}: No open position to close",
                        )
                    return None

                # Prevent duplicate / concurrent exit orders
                if current_pos.exit_lock:
                    if self.audit_logger:
                        self.audit_logger.log_system_event(
                            "ALREADY_EXITING",
                            f"Blocked duplicate exit for {signal.symbol}: Exit already in progress",
                        )
                    return None

                # Lock position for exit
                current_pos.exit_lock = True
                current_pos.is_exit_initiated = True

                # Cancel opposing trigger/resting orders for this symbol first
                self.cancel_opposing_orders(signal.symbol)

                # Spot only: close by selling
                side = "SELL"
                qty = min(risk_decision.adjusted_quantity, current_pos.quantity)
                if qty <= 1e-7:
                    current_pos.exit_lock = False
                    current_pos.is_exit_initiated = False
                    return None

            elif signal.direction == "SELL":
                side = "SELL"
                qty = risk_decision.adjusted_quantity
                if current_pos and current_pos.quantity > 0:
                    qty = min(qty, current_pos.quantity)
                else:
                    return None
            else:
                return None

            # Enforce exchange amount precision and price rounding
            sym_prec = self.get_symbol_precision(signal.symbol)
            amount_precision = int(sym_prec.get("AmountPrecision", 6))
            price_precision = int(sym_prec.get("PricePrecision", 2))
            factor = 10 ** amount_precision
            qty = math.floor(qty * factor) / factor

            if qty <= 1e-7:
                if side == "SELL" and current_pos:
                    current_pos.exit_lock = False
                    current_pos.is_exit_initiated = False
                    if current_pos.quantity <= 1e-4 or (current_market_price > 0 and current_pos.quantity * current_market_price < 5.0):
                        if signal.symbol in self.portfolio.positions:
                            del self.portfolio.positions[signal.symbol]
                            self.portfolio._persist()
                if self.audit_logger:
                    self.audit_logger.log_system_event(
                        "ORDER_REJECTED_ZERO_QTY",
                        f"Order rejected: quantity {qty} is below symbol precision for {signal.symbol}",
                    )
                return None

            order_type = "MARKET"
            price = round(current_market_price, price_precision)
            client_order_id = self.order_manager.generate_client_order_id(signal.strategy, signal.symbol)

            order = Order(
                client_order_id=client_order_id,
                symbol=signal.symbol,
                side=side,
                order_type=order_type,
                quantity=qty,
                remaining_quantity=qty,
                cumulative_filled_quantity=0.0,
                price=price,
                strategy=signal.strategy,
                stop_loss=risk_decision.stop_loss,
                take_profit_1=risk_decision.take_profit_1,
                take_profit_2=risk_decision.take_profit_2,
                status=OrderStatus.PENDING_SUBMIT,
            )
            self.order_manager.register_order(order)

            mode_str = "DRY_RUN" if self.config.dry_run or not self.config.live_trading_enabled else "LIVE"

            # -----------------------------------------------------------------
            # 2. DRY_RUN / PAPER EXECUTION MODE
            # -----------------------------------------------------------------
            if mode_str == "DRY_RUN":
                slippage_mult = (1.0 + self.fees.slippage_pct) if side == "BUY" else (1.0 - self.fees.slippage_pct)
                fill_price = round(price * slippage_mult, price_precision)
                notional = qty * fill_price
                fee = notional * self.fees.taker_fee_pct

                fill_delta, _ = self.order_manager.record_fill_delta(
                    client_order_id=client_order_id,
                    exchange_cumulative_filled=qty,
                    filled_price=fill_price,
                    commission=fee,
                    role="TAKER",
                    ex_status="FILLED",
                )

                if fill_delta > 1e-7:
                    breakout_lvl = float(signal.metadata.get("breakout_level", fill_price))
                    candle_vol = float(signal.metadata.get("candle_volume", 0.0))
                    prev_vol = float(signal.metadata.get("prev_candle_volume", 0.0))

                    self.portfolio.record_fill(
                        symbol=signal.symbol,
                        side=side,
                        quantity=fill_delta,
                        price=fill_price,
                        fee=fee,
                        strategy=signal.strategy,
                        stop_loss=risk_decision.stop_loss,
                        take_profit_1=risk_decision.take_profit_1,
                        take_profit_2=risk_decision.take_profit_2,
                        entry_breakout_level=breakout_lvl,
                        entry_candle_volume=candle_vol,
                        prev_candle_volume=prev_vol,
                    )

                    if hasattr(self, "risk_manager") and hasattr(self.risk_manager, "record_trade_fill"):
                        hold_dur = (time.time() - (current_pos.opened_timestamp / 1000.0)) if (current_pos and current_pos.opened_timestamp > 0) else 0.0
                        realized = (fill_delta * (fill_price - current_pos.entry_price) - fee) if (side == "SELL" and current_pos) else 0.0
                        self.risk_manager.record_trade_fill(
                            symbol=signal.symbol,
                            side=side,
                            quantity=fill_delta,
                            price=fill_price,
                            fee=fee,
                            realized_pnl=realized,
                            hold_duration=hold_dur,
                            exit_reason=str(signal.metadata.get("exit_reason", "")),
                        )

                self.order_manager.unlock_symbol(signal.symbol)
                if side == "SELL" and current_pos:
                    current_pos.exit_lock = False
                    current_pos.is_exit_initiated = False
                    if signal.metadata.get("exit_reason") == "TP1_HIT":
                        current_pos.take_profit_1_hit = True
                        current_pos.take_profit_1_filled = True
                    elif signal.metadata.get("exit_reason") == "TP2_HIT":
                        current_pos.take_profit_2_hit = True
                        current_pos.take_profit_2_filled = True

                if self.audit_logger:
                    self.audit_logger.log_decision(
                        strategy=signal.strategy,
                        symbol=signal.symbol,
                        action=f"SIMULATED_{side}",
                        signal_id=client_order_id,
                        price=fill_price,
                        size=qty,
                        notional=notional,
                        stop_loss=risk_decision.stop_loss,
                        take_profit_1=risk_decision.take_profit_1,
                        take_profit_2=risk_decision.take_profit_2,
                        confidence=signal.confidence,
                        expected_rr=signal.expected_rr,
                        risk_percent=risk_decision.risk_pct,
                        reason=f"[{mode_str}] {signal.reason}",
                        regime=signal.regime,
                        portfolio_equity=self.portfolio.total_equity,
                        cash_before=self.portfolio.cash + notional,
                        api_request_id=client_order_id,
                        status="SIMULATED_FILLED",
                    )
                    self.audit_logger.log_order_event(
                        event=f"SIMULATED_FILL_{mode_str}",
                        symbol=signal.symbol,
                        side=side,
                        order_type=order_type,
                        quantity=qty,
                        price=fill_price,
                        client_order_id=client_order_id,
                        role="TAKER",
                        status="FILLED",
                        filled_qty=qty,
                        filled_price=fill_price,
                        commission=fee,
                    )
                return order

            # -----------------------------------------------------------------
            # 3. LIVE TRADING EXECUTION MODE
            # -----------------------------------------------------------------
            if not self.config.live_trading_enabled:
                self.order_manager.unlock_symbol(signal.symbol)
                if current_pos:
                    current_pos.exit_lock = False
                return None

            try:
                resp = self.client.place_order(
                    pair=signal.symbol,
                    side=side,
                    quantity=qty,
                    price=None,  # MARKET order
                    order_type=order_type,
                    client_order_id=client_order_id,
                )

                if not resp.get("Success", False):
                    err = resp.get("ErrMsg", "Unknown exchange error")
                    order.status = OrderStatus.REJECTED
                    order.error_message = err
                    self.order_manager.update_order(client_order_id=client_order_id, status=OrderStatus.REJECTED, error_msg=err)
                    self.order_manager.unlock_symbol(signal.symbol)
                    if current_pos:
                        current_pos.exit_lock = False
                        current_pos.is_exit_initiated = False
                    if self.audit_logger:
                        self.audit_logger.log_order_event(
                            event="ORDER_REJECTED",
                            symbol=signal.symbol,
                            side=side,
                            order_type=order_type,
                            quantity=qty,
                            client_order_id=client_order_id,
                            status="REJECTED",
                            details={"error": err},
                        )
                    return order

                detail = resp.get("OrderDetail", {})
                ex_order_id = detail.get("OrderID")
                ex_status = detail.get("Status", "FILLED")
                filled_qty = float(detail.get("FilledQuantity", qty))
                filled_price = float(detail.get("FilledAverPrice", price))
                fee = float(detail.get("CommissionChargeValue", 0.0))
                role = detail.get("Role", "TAKER")

                # Incremental fill accounting: compute exact fill delta
                fill_delta, updated_order = self.order_manager.record_fill_delta(
                    client_order_id=client_order_id,
                    exchange_cumulative_filled=filled_qty,
                    filled_price=filled_price,
                    commission=fee,
                    role=role,
                    ex_status=ex_status,
                    exchange_order_id=ex_order_id,
                )

                # Record non-zero fill delta immediately in portfolio ledger
                if fill_delta > 1e-7:
                    breakout_lvl = float(signal.metadata.get("breakout_level", filled_price))
                    candle_vol = float(signal.metadata.get("candle_volume", 0.0))
                    prev_vol = float(signal.metadata.get("prev_candle_volume", 0.0))

                    self.portfolio.record_fill(
                        symbol=signal.symbol,
                        side=side,
                        quantity=fill_delta,
                        price=filled_price,
                        fee=fee,
                        strategy=signal.strategy,
                        stop_loss=risk_decision.stop_loss,
                        take_profit_1=risk_decision.take_profit_1,
                        take_profit_2=risk_decision.take_profit_2,
                        entry_breakout_level=breakout_lvl,
                        entry_candle_volume=candle_vol,
                        prev_candle_volume=prev_vol,
                    )

                    if hasattr(self, "risk_manager") and hasattr(self.risk_manager, "record_trade_fill"):
                        hold_dur = (time.time() - (current_pos.opened_timestamp / 1000.0)) if (current_pos and current_pos.opened_timestamp > 0) else 0.0
                        realized = (fill_delta * (filled_price - current_pos.entry_price) - fee) if (side == "SELL" and current_pos) else 0.0
                        self.risk_manager.record_trade_fill(
                            symbol=signal.symbol,
                            side=side,
                            quantity=fill_delta,
                            price=filled_price,
                            fee=fee,
                            realized_pnl=realized,
                            hold_duration=hold_dur,
                            exit_reason=str(signal.metadata.get("exit_reason", "")),
                        )

                # Handle lock release and lifecycle transitions
                if updated_order and updated_order.status == OrderStatus.FILLED:
                    self.order_manager.unlock_symbol(signal.symbol)
                elif updated_order and updated_order.status == OrderStatus.PARTIALLY_FILLED:
                    # Keep entry locked while partial fill has remaining open volume
                    self.order_manager.lock_symbol(
                        signal.symbol,
                        reason=f"Partial fill open on exchange ({updated_order.remaining_quantity} remaining)",
                        ttl_seconds=300.0,
                    )

                if side == "SELL" and current_pos:
                    current_pos.exit_lock = False
                    current_pos.is_exit_initiated = False
                    if signal.metadata.get("exit_reason") == "TP1_HIT":
                        current_pos.take_profit_1_hit = True
                        current_pos.take_profit_1_filled = True
                    elif signal.metadata.get("exit_reason") == "TP2_HIT":
                        current_pos.take_profit_2_hit = True
                        current_pos.take_profit_2_filled = True

                if self.audit_logger:
                    self.audit_logger.log_decision(
                        strategy=signal.strategy,
                        symbol=signal.symbol,
                        action=f"LIVE_{side}",
                        signal_id=client_order_id,
                        price=filled_price,
                        size=filled_qty,
                        notional=filled_qty * filled_price,
                        stop_loss=risk_decision.stop_loss,
                        take_profit_1=risk_decision.take_profit_1,
                        take_profit_2=risk_decision.take_profit_2,
                        confidence=signal.confidence,
                        expected_rr=signal.expected_rr,
                        risk_percent=risk_decision.risk_pct,
                        reason=f"[LIVE] {signal.reason}",
                        regime=signal.regime,
                        portfolio_equity=self.portfolio.total_equity,
                        cash_before=self.portfolio.cash + (filled_qty * filled_price),
                        api_request_id=client_order_id,
                        exchange_order_id=str(ex_order_id),
                        status=updated_order.status.value if updated_order else "UNKNOWN",
                    )
                    self.audit_logger.log_order_event(
                        event="LIVE_ORDER_FILLED" if (updated_order and updated_order.status == OrderStatus.FILLED) else "LIVE_ORDER_PARTIAL",
                        symbol=signal.symbol,
                        side=side,
                        order_type=order_type,
                        quantity=qty,
                        price=filled_price,
                        client_order_id=client_order_id,
                        exchange_order_id=ex_order_id,
                        role=role,
                        status=updated_order.status.value if updated_order else "UNKNOWN",
                        filled_qty=filled_qty,
                        filled_price=filled_price,
                        commission=fee,
                    )
                return updated_order or order

            except UnknownOrderStateError as unk_err:
                # Network timeout on order submission (Section 5)
                order.status = OrderStatus.UNKNOWN
                order.error_message = str(unk_err)
                self.order_manager.update_order(
                    client_order_id=client_order_id,
                    status=OrderStatus.UNKNOWN,
                    error_msg=str(unk_err),
                )
                # Freeze new entries on this symbol until reconciled
                self.order_manager.lock_symbol(
                    signal.symbol,
                    reason=f"Order {client_order_id} in UNKNOWN state. Freezing entries until reconciled.",
                    ttl_seconds=300.0,
                )
                if current_pos:
                    current_pos.exit_lock = False
                    current_pos.is_exit_initiated = False

                if self.audit_logger:
                    self.audit_logger.log_system_event(
                        "UNKNOWN_ORDER_STATE",
                        f"Order {client_order_id} reached UNKNOWN state: {unk_err}. Symbol locked for reconciliation.",
                    )
                return order

            except RoostooAPIError as api_err:
                order.status = OrderStatus.REJECTED
                order.error_message = str(api_err)
                self.order_manager.update_order(
                    client_order_id=client_order_id,
                    status=OrderStatus.REJECTED,
                    error_msg=str(api_err),
                )
                self.order_manager.unlock_symbol(signal.symbol)
                if current_pos:
                    current_pos.exit_lock = False
                    current_pos.is_exit_initiated = False
                return order
