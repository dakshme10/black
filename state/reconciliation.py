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
        emergency_recovery_sl_pct: float = 0.02, # 2.0% emergency stop loss for recovered positions
        risk_manager: Optional[Any] = None,
    ):
        self.client = api_client
        self.portfolio = portfolio_tracker
        self.order_manager = order_manager
        self.audit_logger = audit_logger
        self.cash_tolerance = cash_tolerance
        self.emergency_recovery_sl_pct = emergency_recovery_sl_pct
        self.risk_manager = risk_manager

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
            with self.portfolio._lock:
                # 1. Fetch live balance from exchange
                bal_resp = self.client.get_balance()
                if not bal_resp.get("Success", False):
                    raise RuntimeError(f"Failed to fetch exchange balance: {bal_resp.get('ErrMsg')}")

                wallet = bal_resp.get("SpotWallet") or bal_resp.get("Wallet") or {}
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

                # Re-align peak equity and clean stale paper-run curve whenever peak exceeds exchange truth with no open positions
                curr_eq = self.portfolio.total_equity
                if not self.portfolio.positions and self.portfolio.peak_equity > curr_eq * 1.05:
                    actions.append(
                        f"Re-aligned peak equity from ${self.portfolio.peak_equity:,.2f} to ${curr_eq:,.2f} to prevent stale capital drawdown"
                    )
                    self.portfolio.peak_equity = curr_eq
                    self.portfolio.equity_curve = [s for s in self.portfolio.equity_curve if s.equity <= curr_eq * 1.05]
                    if not self.portfolio.equity_curve:
                        self.portfolio._record_snapshot()
                    self.portfolio._persist()
                    if self.risk_manager:
                        self.risk_manager.reset_circuit_breaker()

                # 4. Compare Crypto Asset Holdings
                exchange_pairs = set()
                for coin, coin_bal in wallet.items():
                    if coin == "USD":
                        continue
                    free_qty = float(coin_bal.get("Free", 0.0))
                    pair = f"{coin}/USD"
                    exchange_pairs.add(pair)
                    local_pos = self.portfolio.positions.get(pair, None)
                    local_amount = local_pos.quantity if local_pos else 0.0
                    diff = abs(local_amount - free_qty)

                    if diff > 1e-6:
                        asset_diffs[coin] = diff
                        actions.append(
                            f"Reconciled {coin} holding: local {local_amount:.6f} -> exchange {free_qty:.6f}"
                        )
                        if free_qty > 1e-6:
                            if pair in self.portfolio.positions:
                                # Quantity mismatch: update local quantity to exchange truth
                                self.portfolio.positions[pair].quantity = free_qty
                                self.portfolio.positions[pair].take_profit_1_quantity = free_qty * 0.5
                                self.portfolio.positions[pair].take_profit_2_quantity = free_qty * 0.5
                            else:
                                # Discovered holding from prior session / restart
                                # Step 1: Query historical BUY orders
                                recovered_entry_price = 0.0
                                recovered_ts = int(time.time() * 1000)
                                try:
                                    q_res = self.client.query_order(pair=pair, limit=20)
                                    if q_res.get("Success", False):
                                        matches = q_res.get("OrderMatched", [])
                                        # Find latest filled BUY
                                        for m in sorted(matches, key=lambda x: x.get("CreateTimestamp", 0), reverse=True):
                                            if m.get("Side", "").upper() == "BUY" and m.get("Status") == "FILLED":
                                                avg_p = float(m.get("FilledAverPrice", 0.0))
                                                if avg_p > 0:
                                                    recovered_entry_price = avg_p
                                                    recovered_ts = int(m.get("CreateTimestamp", recovered_ts))
                                                    break
                                except Exception:
                                    pass

                                # Step 2: Fallback to ticker price if no order match found
                                if recovered_entry_price <= 0:
                                    try:
                                        t_res = self.client.get_ticker(pair=pair)
                                        ticker_data = t_res.get("Data", {})
                                        if pair in ticker_data:
                                            recovered_entry_price = float(ticker_data[pair].get("LastPrice", 0.0))
                                    except Exception:
                                        pass

                                # Step 3 & 4: Conservative protection - NEVER leave 0 entry or 0 SL
                                if recovered_entry_price > 0:
                                    rec_sl = recovered_entry_price * (1.0 - self.emergency_recovery_sl_pct)
                                    rec_tp1 = recovered_entry_price * 1.015
                                    rec_tp2 = recovered_entry_price * 1.03
                                    rec_status = "RECOVERED_ACTIVE"
                                else:
                                    rec_sl = 0.0
                                    rec_tp1 = 0.0
                                    rec_tp2 = 0.0
                                    rec_status = "RECOVERY_UNRESOLVED"

                                self.portfolio.positions[pair] = Position(
                                    symbol=pair,
                                    base_coin=coin,
                                    quantity=free_qty,
                                    entry_price=recovered_entry_price,
                                    current_price=recovered_entry_price,
                                    stop_loss=rec_sl,
                                    initial_stop_loss=rec_sl,
                                    take_profit_1=rec_tp1,
                                    take_profit_1_quantity=free_qty * 0.5,
                                    take_profit_2=rec_tp2,
                                    take_profit_2_quantity=free_qty * 0.5,
                                    opened_timestamp=recovered_ts,
                                    is_recovered=True,
                                    recovery_status=rec_status,
                                )
                                actions.append(
                                    f"Created RECOVERED_POSITION for {pair}: qty={free_qty:.6f}, entry=${recovered_entry_price:.2f}, sl=${rec_sl:.2f}, status={rec_status}"
                                )
                        else:
                            # Exchange has 0 holding, local had > 0 -> close local position without phantom SELL
                            if pair in self.portfolio.positions:
                                del self.portfolio.positions[pair]
                                actions.append(f"Exchange holding for {coin} is 0; closed local position without sending phantom SELL.")

                # 4b. Reconcile Open Short Positions (v6 Exchange Truth)
                ex_short_pairs = set()
                try:
                    if hasattr(self.client, "get_short_positions"):
                        short_resp = self.client.get_short_positions()
                        if short_resp.get("Success", False):
                            ex_shorts = short_resp.get("Positions", [])
                            for sp in ex_shorts:
                                s_pair = sp.get("Pair", "")
                                if not s_pair:
                                    continue
                                ex_short_pairs.add(s_pair)
                                s_qty = float(sp.get("ShortQty", 0.0))
                                s_entry = float(sp.get("EntryPrice", 0.0))
                                s_curr_px = float(sp.get("CurrentPrice", s_entry))

                                if s_pair in self.portfolio.positions:
                                    self.portfolio.positions[s_pair].quantity = s_qty
                                    self.portfolio.positions[s_pair].side = "SELL"
                                    self.portfolio.positions[s_pair].current_price = s_curr_px
                                else:
                                    base_c = s_pair.split("/")[0]
                                    s_sl = s_entry * (1.0 + self.emergency_recovery_sl_pct)
                                    self.portfolio.positions[s_pair] = Position(
                                        symbol=s_pair,
                                        base_coin=base_c,
                                        quantity=s_qty,
                                        entry_price=s_entry,
                                        current_price=s_curr_px,
                                        side="SELL",
                                        stop_loss=s_sl,
                                        initial_stop_loss=s_sl,
                                        take_profit_1=s_entry * 0.985,
                                        take_profit_2=s_entry * 0.97,
                                        opened_timestamp=int(sp.get("CreateTimestamp", time.time() * 1000)),
                                        is_recovered=True,
                                        recovery_status="RECOVERED_ACTIVE",
                                    )
                                    actions.append(f"Reconciled exchange short position for {s_pair}: qty={s_qty}, entry=${s_entry:,.2f}")
                except Exception as e:
                    actions.append(f"Warning: Failed to fetch short positions: {e}")

                # Check if local portfolio has positions for coins NOT in exchange spot or short holdings
                for lp in list(self.portfolio.positions.keys()):
                    if lp not in exchange_pairs and lp not in ex_short_pairs:
                        del self.portfolio.positions[lp]
                        actions.append(f"Local position {lp} not present in exchange wallet; closed locally without order.")

                self.portfolio._persist()

                # 5. Check Pending & Matched Exchange Orders (AUTHORITATIVE EXCHANGE TRUTH)
                try:
                    pending_resp = self.client.query_order(pending_only=True)
                    if pending_resp.get("Success", False):
                        ex_pending_list = pending_resp.get("OrderMatched", [])
                        for po in ex_pending_list:
                            ex_id = po.get("OrderID")
                            if not ex_id:
                                continue
                            local_match = self.order_manager.get_order_by_exchange_id(ex_id)
                            if not local_match:
                                unmatched_ex_orders.append(ex_id)
                                p_qty = float(po.get("Quantity", 0.0))
                                p_filled = float(po.get("FilledQuantity", 0.0))
                                p_order = Order(
                                    client_order_id=f"EX_PEND_{ex_id}",
                                    symbol=po.get("Pair", ""),
                                    side=po.get("Side", "").upper(),
                                    order_type=po.get("Type", "MARKET").upper(),
                                    quantity=p_qty,
                                    remaining_quantity=max(0.0, p_qty - p_filled),
                                    filled_quantity=p_filled,
                                    cumulative_filled_quantity=p_filled,
                                    price=float(po.get("Price", 0.0) or 0.0),
                                    filled_avg_price=float(po.get("FilledAverPrice", 0.0) or 0.0),
                                    exchange_order_id=ex_id,
                                    status=OrderStatus.PARTIALLY_FILLED if p_filled > 0 else OrderStatus.PENDING_EXCHANGE,
                                    role=po.get("Role", "MAKER"),
                                    strategy="EXCHANGE_PENDING_SYNC",
                                    create_timestamp=int(po.get("CreateTimestamp", time.time() * 1000)),
                                )
                                self.order_manager.register_order(p_order)
                                actions.append(f"Found exchange pending order {ex_id} [{p_order.side} {p_order.symbol}]; registered into local ledger.")
                except Exception as e:
                    actions.append(f"Warning: Failed to fetch pending exchange orders: {e}")

                # 5b. Query recent matched & filled orders from Roostoo Mock Exchange to backfill execution history
                try:
                    matched_resp = self.client.query_order(pending_only=False, limit=50)
                    if matched_resp.get("Success", False):
                        matches = matched_resp.get("OrderMatched", [])
                        for mo in matches:
                            ex_id = mo.get("OrderID")
                            if not ex_id:
                                continue
                            local_match = self.order_manager.get_order_by_exchange_id(ex_id)
                            ex_status = mo.get("Status", "FILLED")
                            filled_qty = float(mo.get("FilledQuantity", 0.0))
                            filled_price = float(mo.get("FilledAverPrice", 0.0)) or float(mo.get("Price", 0.0) or 0.0)
                            comm = float(mo.get("CommissionChargeValue", 0.0))

                            if local_match:
                                # Update locally tracked pending order if exchange reports terminal fill
                                if not local_match.is_terminal and ex_status == "FILLED":
                                    fill_delta, _ = self.order_manager.record_fill_delta(
                                        client_order_id=local_match.client_order_id,
                                        exchange_cumulative_filled=filled_qty,
                                        filled_price=filled_price,
                                        commission=comm,
                                        role=mo.get("Role", "TAKER"),
                                        ex_status=ex_status,
                                        exchange_order_id=ex_id,
                                    )
                                    if fill_delta > 0:
                                        self.portfolio.record_fill(
                                            symbol=local_match.symbol,
                                            side=local_match.side,
                                            quantity=fill_delta,
                                            price=filled_price,
                                            fee=comm,
                                        )
                                    self.order_manager.unlock_symbol(local_match.symbol)
                                    actions.append(f"Reconciled order fill for {local_match.client_order_id} (ex={ex_id}): FILLED {filled_qty} @ ${filled_price:,.2f}")
                                    if self.audit_logger:
                                        self.audit_logger.log_order_event(
                                            event="ORDER_FILL_RECONCILED",
                                            symbol=local_match.symbol,
                                            side=local_match.side,
                                            order_type=local_match.order_type,
                                            quantity=local_match.quantity,
                                            price=filled_price,
                                            client_order_id=local_match.client_order_id,
                                            exchange_order_id=ex_id,
                                            role=mo.get("Role", "TAKER"),
                                            status="FILLED",
                                            filled_qty=filled_qty,
                                            filled_price=filled_price,
                                            commission=comm,
                                        )
                            else:
                                # Order existed on exchange but not in local state (e.g. prior session or external trade)
                                qty = float(mo.get("Quantity", 0.0)) or filled_qty
                                if ex_status == "FILLED":
                                    ord_status = OrderStatus.FILLED
                                elif ex_status == "PENDING" and filled_qty > 0:
                                    ord_status = OrderStatus.PARTIALLY_FILLED
                                elif ex_status == "PENDING":
                                    ord_status = OrderStatus.PENDING_EXCHANGE
                                elif ex_status == "CANCELED":
                                    ord_status = OrderStatus.CANCELED
                                else:
                                    ord_status = OrderStatus.UNKNOWN

                                new_order = Order(
                                    client_order_id=f"EX_{ex_id}",
                                    symbol=mo.get("Pair", ""),
                                    side=mo.get("Side", "").upper(),
                                    order_type=mo.get("Type", "MARKET").upper(),
                                    quantity=qty,
                                    remaining_quantity=max(0.0, qty - filled_qty),
                                    filled_quantity=filled_qty,
                                    cumulative_filled_quantity=filled_qty,
                                    price=float(mo.get("Price", 0.0) or filled_price),
                                    filled_avg_price=filled_price,
                                    exchange_order_id=ex_id,
                                    status=ord_status,
                                    role=mo.get("Role", "TAKER"),
                                    commission=comm,
                                    strategy="EXCHANGE_MOCK_SYNC",
                                    create_timestamp=int(mo.get("CreateTimestamp", time.time() * 1000)),
                                    finish_timestamp=int(mo.get("FinishTimestamp", 0)) or None,
                                )
                                self.order_manager.register_order(new_order)
                                actions.append(f"Synchronized exchange order {ex_id} [{new_order.side} {new_order.symbol}] into order ledger")

                                if self.audit_logger and (filled_qty > 0 or ord_status == OrderStatus.FILLED):
                                    self.audit_logger.log_order_event(
                                        event="EXCHANGE_MOCK_ORDER_SYNCED",
                                        symbol=new_order.symbol,
                                        side=new_order.side,
                                        order_type=new_order.order_type,
                                        quantity=qty,
                                        price=new_order.price,
                                        client_order_id=new_order.client_order_id,
                                        exchange_order_id=ex_id,
                                        role=new_order.role,
                                        status=new_order.status.value,
                                        filled_qty=filled_qty,
                                        filled_price=filled_price,
                                        commission=new_order.commission,
                                        details={"source": "roostoo_reconciliation_sync"},
                                    )
                except Exception as e:
                    actions.append(f"Warning: Failed to sync matched exchange orders: {e}")

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
                # Match by timestamp within 10 seconds and matching quantity and side
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
                    filled_qty = float(ex_ord.get("FilledQuantity", 0.0))
                    filled_price = float(ex_ord.get("FilledAverPrice", 0.0)) or order.price or 0.0
                    commission = float(ex_ord.get("CommissionChargeValue", 0.0))

                    if ex_status == "FILLED":
                        new_status = OrderStatus.FILLED
                    elif ex_status == "PENDING" and filled_qty > 0:
                        new_status = OrderStatus.PARTIALLY_FILLED
                    elif ex_status == "PENDING":
                        new_status = OrderStatus.PENDING_EXCHANGE
                    else:
                        new_status = OrderStatus.CANCELED

                    # Record fill delta to prevent double-counting
                    fill_delta, _ = self.order_manager.record_fill_delta(
                        order.client_order_id,
                        exchange_cumulative_filled=filled_qty,
                        price=filled_price,
                        exchange_order_id=ex_id,
                    )

                    if fill_delta > 0:
                        self.portfolio.record_fill(
                            symbol=order.symbol,
                            side=order.side,
                            quantity=fill_delta,
                            price=filled_price,
                            fee=commission,
                        )

                    self.order_manager.update_order(
                        client_order_id=order.client_order_id,
                        status=new_status,
                        exchange_order_id=ex_id,
                        filled_qty=filled_qty,
                        filled_price=filled_price,
                        role=ex_ord.get("Role", ""),
                        commission=commission,
                    )

                    if self.audit_logger and (filled_qty > 0 or new_status == OrderStatus.FILLED):
                        self.audit_logger.log_order_event(
                            event="UNKNOWN_ORDER_RESOLVED_FILL" if new_status == OrderStatus.FILLED else "UNKNOWN_ORDER_RESOLVED",
                            symbol=order.symbol,
                            side=order.side,
                            order_type=order.order_type,
                            quantity=order.quantity,
                            price=filled_price,
                            client_order_id=order.client_order_id,
                            exchange_order_id=ex_id,
                            role=ex_ord.get("Role", "TAKER"),
                            status=new_status.value,
                            filled_qty=filled_qty,
                            filled_price=filled_price,
                            commission=commission,
                            details={"previous_status": "UNKNOWN"},
                        )

                    if new_status in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED):
                        self.order_manager.unlock_symbol(order.symbol)

                    return True

            # If order was created over 60 seconds ago and not found on exchange, it was never accepted
            if (time.time() * 1000) - order.create_timestamp > 60000:
                self.order_manager.update_order(
                    client_order_id=order.client_order_id,
                    status=OrderStatus.REJECTED,
                    error_msg="Order not found on exchange after timeout. Marked REJECTED.",
                )
                self.order_manager.unlock_symbol(order.symbol)
                if self.audit_logger:
                    self.audit_logger.log_order_event(
                        event="UNKNOWN_ORDER_REJECTED",
                        symbol=order.symbol,
                        side=order.side,
                        order_type=order.order_type,
                        quantity=order.quantity,
                        price=order.price,
                        client_order_id=order.client_order_id,
                        status="REJECTED",
                        details={"reason": "Order not found on exchange after timeout"},
                    )
                return True

            return False
        except Exception:
            return False
