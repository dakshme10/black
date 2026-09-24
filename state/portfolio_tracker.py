"""
Portfolio Tracker and Position Ledger.
Maintains continuous state for cash, crypto balances, gross exposure, realized/unrealized PnL,
cumulative turnover, fees, and the high-resolution equity curve series.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import threading
import time
from typing import Any, Dict, List, Optional


@dataclass
class Position:
    symbol: str
    base_coin: str
    quantity: float = 0.0
    entry_price: float = 0.0
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    strategy: str = ""
    stop_loss: float = 0.0
    take_profit_1: float = 0.0
    take_profit_2: float = 0.0
    opened_timestamp: int = 0
    highest_price: float = 0.0  # Used for trailing stops

    @property
    def notional_value(self) -> float:
        return self.quantity * self.current_price


@dataclass
class EquitySnapshot:
    timestamp_ms: int
    equity: float
    cash: float
    gross_exposure: float
    unrealized_pnl: float
    realized_pnl: float
    cumulative_fees: float


class PortfolioTracker:
    """
    Thread-safe portfolio accounting ledger with persistence and equity curve tracking.
    """

    def __init__(
        self,
        initial_capital: float = 100000.0,
        min_cash_reserve_pct: float = 0.05,
        persistence_file: str = "data/portfolio_state.json",
    ):
        self.initial_capital = initial_capital
        self.min_cash_reserve_pct = min_cash_reserve_pct
        self.persistence_file = Path(persistence_file)
        self.persistence_file.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

        # Balances
        self.cash: float = initial_capital
        self.locked_cash: float = 0.0
        self.positions: Dict[str, Position] = {}

        # Cumulative performance metrics
        self.realized_pnl: float = 0.0
        self.cumulative_fees: float = 0.0
        self.cumulative_turnover: float = 0.0
        self.peak_equity: float = initial_capital

        # Equity time series
        self.equity_curve: List[EquitySnapshot] = []

        # Load persisted state if exists
        self.load_from_disk()

        # Record initial snapshot if empty
        if not self.equity_curve:
            self._record_snapshot()

    @property
    def total_equity(self) -> float:
        """
        Total Portfolio Equity = Free Cash + Locked Cash + Position Notional Values.
        """
        with self._lock:
            pos_value = sum(p.notional_value for p in self.positions.values())
            return self.cash + self.locked_cash + pos_value

    @property
    def gross_exposure_pct(self) -> float:
        """
        Gross Exposure = Total Position Value / Total Equity. Must be <= 1.0 (no leverage).
        """
        eq = self.total_equity
        if eq <= 0:
            return 0.0
        with self._lock:
            pos_value = sum(p.notional_value for p in self.positions.values())
            return pos_value / eq

    @property
    def available_cash(self) -> float:
        """
        Cash available for new entries after respecting the mandatory cash reserve.
        """
        eq = self.total_equity
        reserve = eq * self.min_cash_reserve_pct
        with self._lock:
            return max(0.0, self.cash - reserve)

    def update_mark_prices(self, mark_prices: Dict[str, float]) -> None:
        """Update current market prices and recompute unrealized PnL."""
        with self._lock:
            for sym, pos in self.positions.items():
                if sym in mark_prices and mark_prices[sym] > 0:
                    px = mark_prices[sym]
                    pos.current_price = px
                    pos.highest_price = max(pos.highest_price, px)
                    pos.unrealized_pnl = pos.quantity * (px - pos.entry_price)
            self._record_snapshot()
            self._persist()

    def record_fill(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        fee: float,
        fee_coin: str = "USD",
        strategy: str = "",
        stop_loss: float = 0.0,
        take_profit_1: float = 0.0,
        take_profit_2: float = 0.0,
    ) -> None:
        """
        Process order fill in the portfolio ledger.
        """
        with self._lock:
            notional = quantity * price
            self.cumulative_turnover += notional
            self.cumulative_fees += fee

            base_coin = symbol.split("/")[0]

            if side.upper() == "BUY":
                self.cash -= (notional + fee)

                pos = self.positions.get(symbol)
                if pos is not None and pos.quantity > 0:
                    # Merge with existing position using weighted average
                    existing = pos
                    total_qty = existing.quantity + quantity
                    avg_px = (existing.quantity * existing.entry_price + quantity * price) / total_qty
                    existing.quantity = total_qty
                    existing.entry_price = avg_px
                    existing.current_price = price
                    existing.highest_price = max(existing.highest_price, price)
                    existing.stop_loss = stop_loss or existing.stop_loss
                    existing.take_profit_1 = take_profit_1 or existing.take_profit_1
                    existing.take_profit_2 = take_profit_2 or existing.take_profit_2
                else:
                    self.positions[symbol] = Position(
                        symbol=symbol,
                        base_coin=base_coin,
                        quantity=quantity,
                        entry_price=price,
                        current_price=price,
                        unrealized_pnl=0.0,
                        realized_pnl=0.0,
                        strategy=strategy,
                        stop_loss=stop_loss,
                        take_profit_1=take_profit_1,
                        take_profit_2=take_profit_2,
                        opened_timestamp=int(time.time() * 1000),
                        highest_price=price,
                    )

            elif side.upper() == "SELL":
                self.cash += (notional - fee)

                pos = self.positions.get(symbol)
                if pos is not None and pos.quantity > 0:
                    sold_qty = min(quantity, pos.quantity)
                    trade_pnl = sold_qty * (price - pos.entry_price) - fee
                    pos.realized_pnl += trade_pnl
                    self.realized_pnl += trade_pnl
                    pos.quantity -= sold_qty


                    if pos.quantity <= 1e-7:
                        # Fully closed
                        del self.positions[symbol]
                    else:
                        # Partial close
                        pos.unrealized_pnl = pos.quantity * (price - pos.entry_price)

            self._record_snapshot()
            self._persist()

    def get_current_drawdown(self) -> float:
        """
        Peak-to-trough portfolio drawdown percentage.
        """
        eq = self.total_equity
        with self._lock:
            self.peak_equity = max(self.peak_equity, eq)
            if self.peak_equity <= 0:
                return 0.0
            return max(0.0, (self.peak_equity - eq) / self.peak_equity)

    def get_rolling_24h_drawdown(self) -> float:
        """
        Calculate rolling 24-hour peak-to-trough drawdown from equity snapshots.
        """
        now_ms = int(time.time() * 1000)
        window_ms = 24 * 3600 * 1000
        cutoff_ms = now_ms - window_ms

        with self._lock:
            recent_snaps = [s for s in self.equity_curve if s.timestamp_ms >= cutoff_ms]
            if not recent_snaps:
                return 0.0
            peak_24h = max(s.equity for s in recent_snaps)
            curr_eq = recent_snaps[-1].equity
            if peak_24h <= 0:
                return 0.0
            return max(0.0, (peak_24h - curr_eq) / peak_24h)

    def _record_snapshot(self) -> None:
        """Append an equity snapshot."""
        now_ms = int(time.time() * 1000)
        pos_value = sum(p.notional_value for p in self.positions.values())
        unrealized = sum(p.unrealized_pnl for p in self.positions.values())
        eq = self.cash + self.locked_cash + pos_value
        exp = (pos_value / eq) if eq > 0 else 0.0

        self.peak_equity = max(self.peak_equity, eq)

        snap = EquitySnapshot(
            timestamp_ms=now_ms,
            equity=eq,
            cash=self.cash,
            gross_exposure=exp,
            unrealized_pnl=unrealized,
            realized_pnl=self.realized_pnl,
            cumulative_fees=self.cumulative_fees,
        )
        self.equity_curve.append(snap)
        # Limit in-memory snapshots to 10,000 points
        if len(self.equity_curve) > 10000:
            self.equity_curve.pop(0)

    def _persist(self) -> None:
        """Save state atomically."""
        try:
            data = {
                "initial_capital": self.initial_capital,
                "cash": self.cash,
                "locked_cash": self.locked_cash,
                "positions": {sym: asdict(pos) for sym, pos in self.positions.items()},
                "realized_pnl": self.realized_pnl,
                "cumulative_fees": self.cumulative_fees,
                "cumulative_turnover": self.cumulative_turnover,
                "peak_equity": self.peak_equity,
                "equity_curve": [asdict(s) for s in self.equity_curve[-500:]],
            }
            tmp_file = self.persistence_file.with_suffix(".tmp")
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            tmp_file.replace(self.persistence_file)
        except Exception:
            pass

    def load_from_disk(self) -> None:
        """Load state on startup."""
        if not self.persistence_file.exists():
            return
        try:
            with open(self.persistence_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.cash = float(data.get("cash", self.initial_capital))
            self.locked_cash = float(data.get("locked_cash", 0.0))
            self.realized_pnl = float(data.get("realized_pnl", 0.0))
            self.cumulative_fees = float(data.get("cumulative_fees", 0.0))
            self.cumulative_turnover = float(data.get("cumulative_turnover", 0.0))
            self.peak_equity = float(data.get("peak_equity", self.initial_capital))

            pos_dict = data.get("positions", {})
            self.positions = {sym: Position(**p) for sym, p in pos_dict.items()}

            curve_data = data.get("equity_curve", [])
            self.equity_curve = [EquitySnapshot(**s) for s in curve_data]
        except Exception:
            pass
