"""
Exchange-to-Local State Reconciliation Engine.
Enforces Principle 2: The exchange is the authoritative source of truth.
Performs startup and periodic synchronization of balances, active orders, and resolves UNKNOWN states.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any, Dict, List, Optional, Tuple

from core.api_client import RoostooClient
from logs.audit_logger import AuditLogger
from state.order_state import Order, OrderStateManager, OrderStatus
from state.portfolio_tracker import PortfolioTracker, Position


@dataclass
class DiscrepancyReport:
    is_synchronized: bool
    cash_discrepancy: float
    asset_discrepancies: Dict[str, float] = field(default_factory=dict)
    unmatched_exchange_orders: List[int] = field(default_factory=list)
    unresolved_local_orders: List[str] = field(default_factory=list)
    actions_taken: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)


class ReconciliationEngine:
    """
    Reconciles local portfolio and order states against live Roostoo exchange state.
    """

    def __init__(
        self,
        api_client: RoostooClient,
        portfolio_tracker: PortfolioTracker,
        order_manager: OrderStateManager,
        audit_logger: Optional[AuditLogger] = None,
        cash_tolerance: float = 0.10, # $0.10 tolerance for float rounding
    ):
        self.client = api_client
        self.portfolio = portfolio_tracker
        self.order_manager = order_manager
        self.audit_logger = audit_logger
        self.cash_tolerance = cash_tolerance

        self.last_reconciliation_time: float = 0.0
        self.is_reconciled: bool = False

    def reconcile(self) -> DiscrepancyReport:
        """
        Execute full reconciliation cycle.
        1. Fetch exchange wallet balances.
        2. Fetch exchange pending orders.
        3. Resolve any UNKNOWN order states.
        4. Reconcile cash and asset balances.
        5. Update local state to match exchange truth.
        """
        actions = []
        asset_diffs = {}
        unmatched_ex_orders = []
        unresolved_local = []

        try:
            # 1. Fetch live balance from exchange
            bal_resp = self.client.get_balance()
            if not bal_resp.get("Success", False):
                raise RuntimeError(f"Failed to fetch exchange balance: {bal_resp.get('ErrMsg')}")

            wallet = bal_resp.get("Wallet", {})
            ex_usd_free = float(wallet.get("USD", {}).get("Free", 0.0))
            ex_usd_lock = float(wallet.get("USD", {}).get("Lock", 0.0))

            # 2. Reconcile UNKNOWN orders first
            unknown_orders = self.order_manager.get_unknown_orders()
            for uo in unknown_orders:
                resolved = self._resolve_unknown_order(uo)
                if resolved:
                    actions.append(f"Resolved UNKNOWN order {uo.client_order_id} -> {uo.status.value}")
                else:
                    unresolved_local.append(uo.client_order_id)

            # 3. Compare Cash Balances
            local_cash = self.portfolio.cash
            cash_diff = abs(local_cash - ex_usd_free)

            if cash_diff > self.cash_tolerance:
                actions.append(
                    f"Adjusted local cash from ${local_cash:,.2f} to exchange truth ${ex_usd_free:,.2f} (diff=${cash_diff:,.2f})"
                )
                self.portfolio.cash = ex_usd_free
                self.portfolio.locked_cash = ex_usd_lock

            # 4. Compare Crypto Asset Holdings
            for coin, coin_bal in wallet.items():
                if coin == "USD":
                    continue
                free_qty = float(coin_bal.get("Free", 0.0))
                pair = f"{coin}/USD"
                local_qty = self.portfolio.positions.get(pair, None)
                local_amount = local_qty.quantity if local_qty else 0.0
                diff = abs(local_amount - free_qty)

                if diff > 1e-6:
                    asset_diffs[coin] = diff
                    actions.append(
                        f"Reconciled {coin} holding: local {local_amount:.6f} -> exchange {free_qty:.6f}"
                    )
                    if free_qty > 1e-6:
                        if pair in self.portfolio.positions:
                            self.portfolio.positions[pair].quantity = free_qty
                        else:
                            # Discovered holding from prior session / restart
                            self.portfolio.positions[pair] = Position(
                                symbol=pair,
                                base_coin=coin,
                                quantity=free_qty,
                                entry_price=0.0,
                                current_price=0.0,
                                opened_timestamp=int(time.time() * 1000),
                            )
                    else:
                        if pair in self.portfolio.positions:
                            del self.portfolio.positions[pair]

            # 5. Check Pending Orders
            pending_resp = self.client.query_order(pending_only=True)
            if pending_resp.get("Success", False):
                ex_pending_list = pending_resp.get("OrderMatched", [])
                for po in ex_pending_list:
                    ex_id = po.get("OrderID")
                    local_match = self.order_manager.get_order_by_exchange_id(ex_id)
                    if not local_match:
                        unmatched_ex_orders.append(ex_id)
                        actions.append(f"Found exchange pending order {ex_id} not in local memory; registered.")

            is_synced = (len(unresolved_local) == 0)

            report = DiscrepancyReport(
                is_synchronized=is_synced,
                cash_discrepancy=cash_diff,
                asset_discrepancies=asset_diffs,
                unmatched_exchange_orders=unmatched_ex_orders,
                unresolved_local_orders=unresolved_local,
                actions_taken=actions,
                details={"wallet": wallet},
            )

            self.is_reconciled = is_synced
            self.last_reconciliation_time = time.time()

            if self.audit_logger:
                self.audit_logger.log_reconciliation(
                    status="SUCCESS" if is_synced else "DISCREPANCY",
                    local_cash=local_cash,
                    exchange_cash=ex_usd_free,
                    discrepancies={
                        "cash_diff": cash_diff,
                        "asset_diffs": asset_diffs,
                        "unresolved_orders": unresolved_local,
                    },
                    action_taken="; ".join(actions) if actions else "State perfectly synchronized",
                )

            return report

        except Exception as e:
            self.is_reconciled = False
            err_msg = f"Reconciliation exception: {e}"
            if self.audit_logger:
                self.audit_logger.log_system_event("RECONCILIATION_ERROR", err_msg)
            return DiscrepancyReport(
                is_synchronized=False,
                cash_discrepancy=-1.0,
                actions_taken=[err_msg],
            )

    def _resolve_unknown_order(self, order: Order) -> bool:
        """
        Query Roostoo exchange to determine status of an UNKNOWN order.
        """
        try:
            # Query recent orders for the pair
            resp = self.client.query_order(pair=order.symbol, limit=20)
            if not resp.get("Success", False):
                return False

            matched = resp.get("OrderMatched", [])
            for ex_ord in matched:
                # Match by timestamp within 5 seconds and matching quantity and side
                ex_ts = ex_ord.get("CreateTimestamp", 0)
                ex_side = ex_ord.get("Side", "")
                ex_qty = float(ex_ord.get("Quantity", 0.0))
                ex_status = ex_ord.get("Status", "")
                ex_id = ex_ord.get("OrderID")

                if (
                    ex_side.upper() == order.side.upper()
                    and abs(ex_qty - order.quantity) < 1e-6
                    and abs(ex_ts - order.create_timestamp) < 10000
                ):
                    # Found match on exchange!
                    new_status = OrderStatus.FILLED if ex_status == "FILLED" else (
                        OrderStatus.PENDING_EXCHANGE if ex_status == "PENDING" else OrderStatus.CANCELED
                    )
                    self.order_manager.update_order(
                        client_order_id=order.client_order_id,
                        status=new_status,
                        exchange_order_id=ex_id,
                        filled_qty=float(ex_ord.get("FilledQuantity", 0.0)),
                        filled_price=float(ex_ord.get("FilledAverPrice", 0.0)),
                        role=ex_ord.get("Role", ""),
                        commission=float(ex_ord.get("CommissionChargeValue", 0.0)),
                    )
                    return True

            # If order was created over 60 seconds ago and not found on exchange, it was never accepted
            if (time.time() * 1000) - order.create_timestamp > 60000:
                self.order_manager.update_order(
                    client_order_id=order.client_order_id,
                    status=OrderStatus.REJECTED,
                    error_msg="Order not found on exchange after timeout. Marked REJECTED.",
                )
                return True

            return False
        except Exception:
            return False
