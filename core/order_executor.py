"""
Order Execution Engine.
Enforces execution safety protocols (Section 8, 32, 33):
State refresh -> available cash verification -> position verification -> risk validation ->
idempotent order submission (or simulated fill in DRY_RUN) -> UNKNOWN state protection -> reconciliation.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

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

    def execute_decision(
        self,
        signal: Signal,
        risk_decision: RiskDecision,
        current_market_price: float,
    ) -> Optional[Order]:
        """
        Execute an approved trading decision under DRY_RUN or LIVE mode.
        """
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

        # Determine side and quantity
        if signal.direction == "DE_RISK":
            side = "SELL"
            qty = risk_decision.adjusted_quantity
            if qty <= 1e-7:
                return None
        elif signal.direction == "BUY":
            side = "BUY"
            qty = risk_decision.adjusted_quantity
        else:
            return None

        order_type = "MARKET"  # Standard competition taker entry for deterministic execution
        price = current_market_price
        client_order_id = self.order_manager.generate_client_order_id(signal.strategy, signal.symbol)

        order = Order(
            client_order_id=client_order_id,
            symbol=signal.symbol,
            side=side,
            order_type=order_type,
            quantity=qty,
            price=price,
            strategy=signal.strategy,
            stop_loss=risk_decision.stop_loss,
            take_profit_1=risk_decision.take_profit_1,
            take_profit_2=risk_decision.take_profit_2,
            status=OrderStatus.PENDING_SUBMIT,
        )
        self.order_manager.register_order(order)

        mode_str = "DRY_RUN" if self.config.dry_run or not self.config.live_trading_enabled else "LIVE"

        # ---------------------------------------------------------------------
        # 1. DRY_RUN / PAPER EXECUTION MODE (Section 32)
        # ---------------------------------------------------------------------
        if mode_str == "DRY_RUN":
            # Model slippage and taker fee
            slippage_mult = (1.0 + self.fees.slippage_pct) if side == "BUY" else (1.0 - self.fees.slippage_pct)
            fill_price = round(price * slippage_mult, 4)
            notional = qty * fill_price
            fee = notional * self.fees.taker_fee_pct

            order.status = OrderStatus.FILLED
            order.filled_quantity = qty
            order.filled_avg_price = fill_price
            order.role = "TAKER"
            order.commission = fee
            order.finish_timestamp = int(time.time() * 1000)
            self.order_manager.update_order(
                client_order_id=client_order_id,
                status=OrderStatus.FILLED,
                filled_qty=qty,
                filled_price=fill_price,
                role="TAKER",
                commission=fee,
            )

            # Record in portfolio ledger
            self.portfolio.record_fill(
                symbol=signal.symbol,
                side=side,
                quantity=qty,
                price=fill_price,
                fee=fee,
                strategy=signal.strategy,
                stop_loss=risk_decision.stop_loss,
                take_profit_1=risk_decision.take_profit_1,
                take_profit_2=risk_decision.take_profit_2,
            )

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

        # ---------------------------------------------------------------------
        # 2. LIVE TRADING EXECUTION MODE (Section 33)
        # ---------------------------------------------------------------------
        if not self.config.live_trading_enabled:
            return None

        try:
            # Place order on exchange
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

            order.exchange_order_id = ex_order_id
            order.status = OrderStatus.FILLED if ex_status == "FILLED" else OrderStatus.PENDING_EXCHANGE
            order.filled_quantity = filled_qty
            order.filled_avg_price = filled_price
            order.role = role
            order.commission = fee
            order.finish_timestamp = int(time.time() * 1000)

            self.order_manager.update_order(
                client_order_id=client_order_id,
                status=order.status,
                exchange_order_id=ex_order_id,
                filled_qty=filled_qty,
                filled_price=filled_price,
                role=role,
                commission=fee,
            )

            # Record in portfolio ledger
            if order.status == OrderStatus.FILLED:
                self.portfolio.record_fill(
                    symbol=signal.symbol,
                    side=side,
                    quantity=filled_qty,
                    price=filled_price,
                    fee=fee,
                    strategy=signal.strategy,
                    stop_loss=risk_decision.stop_loss,
                    take_profit_1=risk_decision.take_profit_1,
                    take_profit_2=risk_decision.take_profit_2,
                )

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
                    status=order.status.value,
                )
                self.audit_logger.log_order_event(
                    event="LIVE_ORDER_FILLED" if order.status == OrderStatus.FILLED else "LIVE_ORDER_PENDING",
                    symbol=signal.symbol,
                    side=side,
                    order_type=order_type,
                    quantity=qty,
                    price=filled_price,
                    client_order_id=client_order_id,
                    exchange_order_id=ex_order_id,
                    role=role,
                    status=order.status.value,
                    filled_qty=filled_qty,
                    filled_price=filled_price,
                    commission=fee,
                )
            return order

        except UnknownOrderStateError as unk_err:
            # Network timeout on order submission (Section 8 & 25)
            order.status = OrderStatus.UNKNOWN
            order.error_message = str(unk_err)
            self.order_manager.update_order(
                client_order_id=client_order_id,
                status=OrderStatus.UNKNOWN,
                error_msg=str(unk_err),
            )
            if self.audit_logger:
                self.audit_logger.log_system_event(
                    "UNKNOWN_ORDER_STATE",
                    f"Order {client_order_id} reached UNKNOWN state: {unk_err}. Must be reconciled.",
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
            return order
