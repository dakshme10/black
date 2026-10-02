"""
Order State Management and Persistence.
Defines explicit order lifecycle states (including UNKNOWN), manages client order IDs,
and provides disk-backed persistence for crash recovery and restart resilience.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Dict, List, NamedTuple, Optional, Tuple, Union


@dataclass
class SymbolLockStatus:
    is_locked: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.is_locked

    def __iter__(self):
        yield self.is_locked
        yield self.reason

    def __getitem__(self, index):
        return (self.is_locked, self.reason)[index]

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, bool):
            return self.is_locked == other
        if isinstance(other, tuple) and len(other) == 2:
            return (self.is_locked, self.reason) == other
        if isinstance(other, SymbolLockStatus):
            return self.is_locked == other.is_locked and self.reason == other.reason
        return False


class OrderStatus(str, Enum):
    PENDING_SUBMIT = "PENDING_SUBMIT"
    PENDING_EXCHANGE = "PENDING"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"      # Ambiguous network timeout state
    RESOLVING = "RESOLVING"  # Currently undergoing exchange reconciliation query


@dataclass
class Order:
    client_order_id: str
    symbol: str
    side: str                            # "BUY", "SELL"
    order_type: str                      # "LIMIT", "MARKET"
    quantity: float
    price: Optional[float] = None
    exchange_order_id: Optional[int] = None
    filled_quantity: float = 0.0
    cumulative_filled_quantity: float = 0.0
    remaining_quantity: float = 0.0
    last_fill_quantity: float = 0.0
    last_fill_timestamp: Optional[int] = None
    filled_avg_price: float = 0.0
    status: OrderStatus = OrderStatus.PENDING_SUBMIT
    role: str = ""                       # "MAKER", "TAKER"
    commission: float = 0.0
    commission_coin: str = "USD"
    strategy: str = ""
    stop_loss: float = 0.0
    take_profit_1: float = 0.0
    take_profit_2: float = 0.0
    create_timestamp: int = field(default_factory=lambda: int(time.time() * 1000))
    finish_timestamp: Optional[int] = None
    error_message: str = ""

    def __post_init__(self) -> None:
        if self.remaining_quantity <= 0.0 and self.quantity > 0.0 and self.cumulative_filled_quantity == 0.0:
            self.remaining_quantity = self.quantity
        if self.cumulative_filled_quantity == 0.0 and self.filled_quantity > 0.0:
            self.cumulative_filled_quantity = self.filled_quantity

    @property
    def is_terminal(self) -> bool:
        return self.status in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED)

    @property
    def is_active(self) -> bool:
        return not self.is_terminal

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Order:
        data_copy = dict(data)
        data_copy["status"] = OrderStatus(data_copy.get("status", "UNKNOWN"))
        return cls(**data_copy)


class OrderStateManager:
    """
    Thread-safe ledger of all active, historical, and UNKNOWN orders.
    Persists to disk to allow graceful crash recovery.
    """

    def __init__(self, persistence_file: str = "data/order_state.json"):
        self.persistence_file = Path(persistence_file)
        self.persistence_file.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

        # In-memory indices
        self._orders_by_client_id: Dict[str, Order] = {}
        self._orders_by_exchange_id: Dict[int, Order] = {}
        self._symbol_locks: Dict[str, Tuple[float, str]] = {}  # symbol -> (expire_ts, reason)

        self.load_from_disk()

    def lock_symbol(self, symbol: str, reason: str, ttl_seconds: float = 300.0) -> None:
        """Lock symbol from fresh entry orders."""
        with self._lock:
            expire_ts = time.time() + ttl_seconds
            self._symbol_locks[symbol] = (expire_ts, reason)

    def unlock_symbol(self, symbol: str) -> None:
        """Clear symbol entry lock."""
        with self._lock:
            self._symbol_locks.pop(symbol, None)

    def is_symbol_entry_locked(self, symbol: str) -> SymbolLockStatus:
        """
        Determine if symbol is locked from placing new entry orders.
        A symbol is locked if:
        1. An explicit lock is active (e.g. UNKNOWN order undergoing reconciliation).
        2. Any active BUY order is PENDING_SUBMIT, PENDING_EXCHANGE, PARTIALLY_FILLED, UNKNOWN, or RESOLVING.
        """
        with self._lock:
            now = time.time()
            if symbol in self._symbol_locks:
                exp_ts, reason = self._symbol_locks[symbol]
                if now < exp_ts:
                    return SymbolLockStatus(True, f"Symbol entry locked: {reason}")
                else:
                    del self._symbol_locks[symbol]

            for ord in self._orders_by_client_id.values():
                if ord.symbol == symbol and ord.side.upper() == "BUY":
                    if ord.status in (
                        OrderStatus.PENDING_SUBMIT,
                        OrderStatus.PENDING_EXCHANGE,
                        OrderStatus.PARTIALLY_FILLED,
                        OrderStatus.UNKNOWN,
                        OrderStatus.RESOLVING,
                    ):
                        return SymbolLockStatus(True, f"Active entry order {ord.client_order_id} in state {ord.status.value}")

            return SymbolLockStatus(False, "")

    def generate_client_order_id(self, strategy: str, symbol: str) -> str:
        """
        Generate deterministic and unique client order ID.
        Format: RST_{STRATEGY}_{CLEAN_SYM}_{TIMESTAMP_MS}_{SEQ}
        """
        clean_sym = symbol.replace("/", "").replace("-", "")
        ts = int(time.time() * 1000)
        short_uuid = os.urandom(3).hex()
        return f"RST_{strategy[:4]}_{clean_sym}_{ts}_{short_uuid}"

    def register_order(self, order: Order) -> None:
        """Add new order to tracking."""
        with self._lock:
            self._orders_by_client_id[order.client_order_id] = order
            if order.exchange_order_id is not None:
                self._orders_by_exchange_id[order.exchange_order_id] = order
            self._persist()

    def create_order(
        self,
        client_order_id: str,
        symbol: str,
        side: str,
        quantity: float,
        price: float = 0.0,
        order_type: str = "MARKET",
        strategy: str = "",
        status: OrderStatus = OrderStatus.PENDING_SUBMIT,
    ) -> Order:
        """Create, register, and persist a new order."""
        order = Order(
            client_order_id=client_order_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            strategy=strategy,
            status=status,
            create_timestamp=int(time.time() * 1000),
        )
        self.register_order(order)
        return order

    def record_fill_delta(
        self,
        client_order_id: str,
        exchange_cumulative_filled: float,
        filled_price: float = 0.0,
        commission: float = 0.0,
        role: str = "",
        ex_status: str = "",
        exchange_order_id: Optional[int] = None,
        price: Optional[float] = None,
    ) -> Tuple[float, Optional[Order]]:
        """
        Incremental fill accounting:
        Calculates new_fill = exchange_cumulative_filled - locally_recorded_cumulative_filled.
        Guarantees that repeated polls of the same fill never double-count.
        """
        actual_price = price if price is not None else filled_price
        with self._lock:
            order = self._orders_by_client_id.get(client_order_id)
            if not order:
                return 0.0, None

            # Calculate non-negative incremental fill
            fill_delta = max(0.0, float(exchange_cumulative_filled) - float(order.cumulative_filled_quantity))
            order.cumulative_filled_quantity = float(exchange_cumulative_filled)
            order.filled_quantity = float(exchange_cumulative_filled)
            order.remaining_quantity = max(0.0, float(order.quantity) - float(order.cumulative_filled_quantity))
            order.last_fill_quantity = fill_delta
            order.last_fill_timestamp = int(time.time() * 1000)

            if actual_price > 0:
                order.filled_avg_price = actual_price
            if commission > 0:
                order.commission = commission
            if role:
                order.role = role
            if exchange_order_id is not None:
                order.exchange_order_id = exchange_order_id
                self._orders_by_exchange_id[exchange_order_id] = order

            ex_status_upper = ex_status.upper() if ex_status else ""
            if ex_status_upper == "FILLED" or order.remaining_quantity <= 1e-7:
                order.status = OrderStatus.FILLED
                order.finish_timestamp = int(time.time() * 1000)
            elif ex_status_upper in ("PARTIALLY_FILLED", "PENDING") or order.cumulative_filled_quantity > 0:
                order.status = OrderStatus.PARTIALLY_FILLED
            elif ex_status_upper == "CANCELED":
                order.status = OrderStatus.CANCELED
            elif ex_status_upper == "REJECTED":
                order.status = OrderStatus.REJECTED

            self._persist()
            return fill_delta, order

    def update_order(
        self,
        client_order_id: str,
        status: Optional[OrderStatus] = None,
        exchange_order_id: Optional[int] = None,
        filled_qty: Optional[float] = None,
        filled_price: Optional[float] = None,
        role: Optional[str] = None,
        commission: Optional[float] = None,
        finish_timestamp: Optional[int] = None,
        error_msg: Optional[str] = None,
    ) -> Optional[Order]:
        """Update existing order fields atomically."""
        with self._lock:
            order = self._orders_by_client_id.get(client_order_id)
            if not order:
                return None

            if status is not None:
                order.status = status
            if exchange_order_id is not None:
                order.exchange_order_id = exchange_order_id
                self._orders_by_exchange_id[exchange_order_id] = order
            if filled_qty is not None:
                order.filled_quantity = filled_qty
                if order.cumulative_filled_quantity < filled_qty:
                    order.cumulative_filled_quantity = filled_qty
                order.remaining_quantity = max(0.0, order.quantity - order.cumulative_filled_quantity)
            if filled_price is not None:
                order.filled_avg_price = filled_price
            if role is not None:
                order.role = role
            if commission is not None:
                order.commission = commission
            if finish_timestamp is not None:
                order.finish_timestamp = finish_timestamp
            if error_msg is not None:
                order.error_message = error_msg

            self._persist()
            return order

    def get_order_by_client_id(self, client_order_id: str) -> Optional[Order]:
        with self._lock:
            return self._orders_by_client_id.get(client_order_id)

    def get_order_by_exchange_id(self, exchange_order_id: int) -> Optional[Order]:
        with self._lock:
            return self._orders_by_exchange_id.get(exchange_order_id)

    def get_active_orders(self, symbol: Optional[str] = None) -> List[Order]:
        """Return orders that are currently pending submission or resting on exchange."""
        with self._lock:
            active = []
            for o in self._orders_by_client_id.values():
                if o.status in (
                    OrderStatus.PENDING_SUBMIT,
                    OrderStatus.PENDING_EXCHANGE,
                    OrderStatus.PARTIALLY_FILLED,
                    OrderStatus.UNKNOWN,
                    OrderStatus.RESOLVING,
                ):
                    if symbol is None or o.symbol == symbol:
                        active.append(o)
            return active

    def get_unknown_orders(self) -> List[Order]:
        """Return orders that require exchange reconciliation due to timeout or network fault."""
        with self._lock:
            return [o for o in self._orders_by_client_id.values() if o.status == OrderStatus.UNKNOWN]

    def get_history(self, limit: int = 100) -> List[Order]:
        """Return orders in reverse chronological order."""
        with self._lock:
            all_orders = sorted(
                self._orders_by_client_id.values(),
                key=lambda o: o.create_timestamp,
                reverse=True,
            )
            return all_orders[:limit]


    def _persist(self) -> None:
        """Save order state atomically to file."""
        try:
            records = [o.to_dict() for o in self._orders_by_client_id.values()]
            tmp_file = self.persistence_file.with_suffix(".tmp")
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(records, f, indent=2)
            tmp_file.replace(self.persistence_file)
        except Exception:
            pass

    def load_from_disk(self) -> None:
        """Load order records from disk upon system startup."""
        if not self.persistence_file.exists():
            return

        try:
            with open(self.persistence_file, "r", encoding="utf-8") as f:
                records = json.load(f)
            with self._lock:
                for r in records:
                    order = Order.from_dict(r)
                    self._orders_by_client_id[order.client_order_id] = order
                    if order.exchange_order_id is not None:
                        self._orders_by_exchange_id[order.exchange_order_id] = order
        except Exception:
            pass
