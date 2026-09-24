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
from typing import Any, Dict, List, Optional, Union


class OrderStatus(str, Enum):
    PENDING_SUBMIT = "PENDING_SUBMIT"
    PENDING_EXCHANGE = "PENDING"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"  # Ambiguous network timeout state


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
        self._lock = threading.Lock()

        # In-memory indices
        self._orders_by_client_id: Dict[str, Order] = {}
        self._orders_by_exchange_id: Dict[int, Order] = {}

        self.load_from_disk()

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
                if o.status in (OrderStatus.PENDING_SUBMIT, OrderStatus.PENDING_EXCHANGE, OrderStatus.UNKNOWN):
                    if symbol is None or o.symbol == symbol:
                        active.append(o)
            return active

    def get_unknown_orders(self) -> List[Order]:
        """Return orders that require exchange reconciliation due to timeout or network fault."""
        with self._lock:
            return [o for o in self._orders_by_client_id.values() if o.status == OrderStatus.UNKNOWN]

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
