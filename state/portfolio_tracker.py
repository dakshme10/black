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


from datetime import datetime, timezone
import math


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

    # AutoSL Tracking Fields (Two-Phase Lifecycle + Dynamic Trailing + Momentum Extension)
    side: str = "BUY"
    entry_breakout_level: float = 0.0
    entry_candle_volume: float = 0.0
    prev_candle_volume: float = 0.0
    initial_stop_loss: float = 0.0
    broker_sl_price: float = 0.0
    sl_percent: float = 2.0
    peak_profit_points: float = 0.0
    locked_profit: float = 0.0
    candles_since_entry: int = 0
    last_candle_time: Optional[float] = None  # epoch seconds
    validation_survived: bool = False
    volume_drop_detected: bool = False
    trailing_sl_active: bool = False
    is_exit_initiated: bool = False
    momentum_status: str = "NORMAL"
    original_target_distance: float = 0.0
    extension_level: int = 0
    last_sl_recalc_time: float = 0.0
    broker_sl_order_id: Optional[str] = None
    broker_tp_order_id: Optional[str] = None

    # Multi-Target Partial Exit & Recovery Management (Section 9 & 10)
    take_profit_1_quantity: float = 0.0
    take_profit_1_hit: bool = False
    take_profit_1_filled: bool = False
    take_profit_2_quantity: float = 0.0
    take_profit_2_hit: bool = False
    take_profit_2_filled: bool = False
    is_recovered: bool = False
    recovery_status: str = ""       # "RECOVERED_ACTIVE" or "RECOVERY_UNRESOLVED"
    exit_lock: bool = False
    exit_state: str = "OPEN"        # "OPEN", "TP1_PENDING", "EXIT_PENDING", "CLOSED"

    @property
    def notional_value(self) -> float:
        return self.quantity * self.current_price

    @property
    def phase(self) -> str:
        if self.momentum_status.startswith("MOMENTUM_EXTENSION") or self.momentum_status == "FINAL_TRAILING":
            return self.momentum_status
        if self.validation_survived:
            return "PHASE_2_VALIDATED"
        return "PHASE_1_VALIDATION"

    def to_crypto_position(self) -> Any:
        """Convert to AutoSL CryptoPosition object for tick evaluation."""
        from core.autosl_exit_engine import CryptoPosition
        entry_dt = (
            datetime.fromtimestamp(self.opened_timestamp / 1000.0, tz=timezone.utc)
            if self.opened_timestamp > 0
            else datetime.now(timezone.utc)
        )
        last_c_dt = (
            datetime.fromtimestamp(self.last_candle_time, tz=timezone.utc)
            if self.last_candle_time is not None
            else entry_dt
        )
        return CryptoPosition(
            position_id=f"{self.symbol}_{self.opened_timestamp}",
            symbol=self.symbol,
            side=self.side,
            quantity=self.quantity,
            entry_price=self.entry_price,
            entry_time=entry_dt,
            entry_breakout_level=self.entry_breakout_level if self.entry_breakout_level > 0 else self.entry_price,
            entry_candle_volume=self.entry_candle_volume,
            prev_candle_volume=self.prev_candle_volume,
            initial_stop_loss=self.initial_stop_loss if self.initial_stop_loss > 0 else self.stop_loss,
            stop_loss_price=self.stop_loss,
            take_profit_price=self.take_profit_2 if self.take_profit_2 > 0 else self.take_profit_1,
            take_profit_1=self.take_profit_1,
            take_profit_1_hit=self.take_profit_1_hit,
            take_profit_2=self.take_profit_2,
            take_profit_2_hit=self.take_profit_2_hit,
            broker_sl_price=self.broker_sl_price if self.broker_sl_price > 0 else self.stop_loss,
            sl_percent=self.sl_percent,
            status="OPEN",
            current_price=self.current_price,
            peak_price=self.highest_price if self.highest_price > 0 else self.entry_price,
            peak_profit_points=self.peak_profit_points,
            locked_profit=self.locked_profit,
            candles_since_entry=self.candles_since_entry,
            last_candle_time=last_c_dt,
            validation_survived=self.validation_survived,
            volume_drop_detected=self.volume_drop_detected,
            trailing_sl_active=self.trailing_sl_active,
            is_exit_initiated=self.is_exit_initiated or self.exit_lock,
            momentum_status=self.momentum_status,
            original_target_distance=self.original_target_distance,
            extension_level=self.extension_level,
            last_sl_recalc_time=self.last_sl_recalc_time,
            broker_sl_order_id=self.broker_sl_order_id,
            broker_tp_order_id=self.broker_tp_order_id,
        )

    def update_from_crypto_position(self, cp: Any) -> None:
        """Sync updated state from CryptoPosition back into this ledger Position."""
        self.stop_loss = cp.stop_loss_price
        self.highest_price = cp.peak_price
        self.broker_sl_price = cp.broker_sl_price
        self.sl_percent = cp.sl_percent
        self.peak_profit_points = cp.peak_profit_points
        self.locked_profit = cp.locked_profit
        self.candles_since_entry = cp.candles_since_entry
        self.last_candle_time = cp.last_candle_time.timestamp() if cp.last_candle_time else None
        self.validation_survived = cp.validation_survived
        self.volume_drop_detected = cp.volume_drop_detected
        self.trailing_sl_active = cp.trailing_sl_active
        self.is_exit_initiated = cp.is_exit_initiated
        self.momentum_status = cp.momentum_status
        self.take_profit_1_hit = getattr(cp, "take_profit_1_hit", self.take_profit_1_hit)
        self.take_profit_2_hit = getattr(cp, "take_profit_2_hit", self.take_profit_2_hit)
        if cp.take_profit_price > 0 and not math.isinf(cp.take_profit_price):
            self.take_profit_2 = cp.take_profit_price
        self.original_target_distance = cp.original_target_distance
        self.extension_level = cp.extension_level
        self.last_sl_recalc_time = cp.last_sl_recalc_time
        self.broker_sl_order_id = cp.broker_sl_order_id
        self.broker_tp_order_id = cp.broker_tp_order_id


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
        self._lock = threading.RLock()

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
            changed = False
            for sym, pos in self.positions.items():
                if sym in mark_prices and mark_prices[sym] > 0:
                    px = mark_prices[sym]
                    if abs(pos.current_price - px) > 1e-4:
                        changed = True
                    pos.current_price = px
                    pos.highest_price = max(pos.highest_price, px)
                    pos.unrealized_pnl = pos.quantity * (px - pos.entry_price)

            now_ms = int(time.time() * 1000)
            last_snap_ts = self.equity_curve[-1].timestamp_ms if self.equity_curve else 0
            time_elapsed_ms = now_ms - last_snap_ts

            # Throttle snapshots when idle: record if equity changed or at least 60s elapsed
            if changed or not self.equity_curve or time_elapsed_ms >= 60000:
                self._record_snapshot()
                self._persist()

    def record_fill(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        fee: float = 0.0,
        fee_coin: str = "USD",
        strategy: str = "",
        stop_loss: float = 0.0,
        take_profit_1: float = 0.0,
        take_profit_2: float = 0.0,
        entry_breakout_level: float = 0.0,
        entry_candle_volume: float = 0.0,
        prev_candle_volume: float = 0.0,
    ) -> None:
        """
        Process order fill in the portfolio ledger.
        """
        with self._lock:
            notional = quantity * price
            self.cumulative_turnover += notional
            self.cumulative_fees += fee

            base_coin = symbol.split("/")[0]
            calculated_sl_pct = (abs(price - stop_loss) / price * 100.0) if (stop_loss > 0 and price > 0) else 2.0
            breakout_lvl = entry_breakout_level if entry_breakout_level > 0 else price
            target_dist = abs(take_profit_2 - price) if take_profit_2 > 0 else abs(take_profit_1 - price) if take_profit_1 > 0 else 0.0

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
                    existing.take_profit_1_quantity = total_qty * 0.50
                    existing.take_profit_2_quantity = total_qty - existing.take_profit_1_quantity
                else:
                    tp1_qty = quantity * 0.50
                    tp2_qty = quantity - tp1_qty
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
                        take_profit_1_quantity=tp1_qty,
                        take_profit_2=take_profit_2,
                        take_profit_2_quantity=tp2_qty,
                        opened_timestamp=int(time.time() * 1000),
                        highest_price=price,
                        side="BUY",
                        entry_breakout_level=breakout_lvl,
                        entry_candle_volume=entry_candle_volume,
                        prev_candle_volume=prev_candle_volume,
                        initial_stop_loss=stop_loss,
                        broker_sl_price=stop_loss,
                        sl_percent=calculated_sl_pct,
                        peak_profit_points=0.0,
                        locked_profit=0.0,
                        candles_since_entry=0,
                        last_candle_time=time.time(),
                        validation_survived=False,
                        volume_drop_detected=False,
                        trailing_sl_active=False,
                        is_exit_initiated=False,
                        momentum_status="NORMAL",
                        original_target_distance=target_dist,
                        extension_level=0,
                        last_sl_recalc_time=time.time(),
                    )

            elif side.upper() == "SELL":
                pos = self.positions.get(symbol)
                # Invariant: Never manufacture cash without corresponding valid inventory!
                if pos is not None and pos.quantity > 0:
                    sold_qty = min(quantity, pos.quantity)
                    actual_notional = sold_qty * price
                    self.cash += (actual_notional - fee)
                    trade_pnl = sold_qty * (price - pos.entry_price) - fee
                    pos.realized_pnl += trade_pnl
                    self.realized_pnl += trade_pnl
                    pos.quantity -= sold_qty

                    if pos.quantity <= 1e-5 or (getattr(pos, "take_profit_2_filled", False) and pos.quantity <= 1e-4):
                        # Fully closed (clean up sub-minimum dust)
                        del self.positions[symbol]
                    else:
                        pos.unrealized_pnl = pos.quantity * (price - pos.entry_price)
                        pos.exit_lock = False
                        pos.is_exit_initiated = False

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
        # Limit in-memory snapshots to 10,000 points, preserving inception snapshot at index 0
        if len(self.equity_curve) > 10000:
            self.equity_curve.pop(1)

    def _persist(self) -> None:
        """Save state atomically."""
        try:
            # Preserve inception snapshot and latest 2,000 snapshots
            persisted_snaps = []
            if self.equity_curve:
                if len(self.equity_curve) <= 2000:
                    persisted_snaps = [asdict(s) for s in self.equity_curve]
                else:
                    persisted_snaps = [asdict(self.equity_curve[0])] + [asdict(s) for s in self.equity_curve[-2000:]]

            data = {
                "state_version": 2,
                "initial_capital": self.initial_capital,
                "cash": self.cash,
                "locked_cash": self.locked_cash,
                "positions": {sym: asdict(pos) for sym, pos in self.positions.items()},
                "realized_pnl": self.realized_pnl,
                "cumulative_fees": self.cumulative_fees,
                "cumulative_turnover": self.cumulative_turnover,
                "peak_equity": self.peak_equity,
                "equity_curve": persisted_snaps,
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
            persisted_initial = float(data.get("initial_capital", self.initial_capital))
            self.cash = float(data.get("cash", self.initial_capital))
            self.locked_cash = float(data.get("locked_cash", 0.0))
            self.realized_pnl = float(data.get("realized_pnl", 0.0))
            self.cumulative_fees = float(data.get("cumulative_fees", 0.0))
            self.cumulative_turnover = float(data.get("cumulative_turnover", 0.0))
            self.peak_equity = float(data.get("peak_equity", self.initial_capital))

            pos_dict = data.get("positions", {})
            valid_fields = set(Position.__dataclass_fields__.keys())
            loaded_positions = {}
            for sym, p in pos_dict.items():
                pos_obj = Position(**{k: v for k, v in p.items() if k in valid_fields})
                if pos_obj.quantity > 1e-5 and not (getattr(pos_obj, "take_profit_2_filled", False) and pos_obj.quantity <= 1e-4):
                    loaded_positions[sym] = pos_obj
            self.positions = loaded_positions

            curve_data = data.get("equity_curve", [])
            self.equity_curve = [EquitySnapshot(**s) for s in curve_data]

            curr_equity = self.total_equity
            # Guard against stale baseline / paper run peak equity poisoning live mock run
            if (
                abs(persisted_initial - self.initial_capital) > 1.0
                or (not self.positions and self.peak_equity > curr_equity * 1.05)
            ):
                self.peak_equity = curr_equity
                self.equity_curve = [s for s in self.equity_curve if s.equity <= curr_equity * 1.05]
                if not self.equity_curve:
                    self._record_snapshot()
                self._persist()
        except Exception:
            pass
