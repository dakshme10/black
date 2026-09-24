"""
Market Data Engine for Roostoo Autonomous Trading Bot.
Manages ticker polling, candle aggregation (1m, 5m, 15m), stale-data detection,
and CVD/OI feature availability tracking without data fabrication.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
from typing import Any, Dict, List, Optional
import pandas as pd

from config.trading_params import MarketDataConfig
from core.api_client import RoostooClient
from logs.audit_logger import AuditLogger


@dataclass
class Candle:
    timestamp: int        # Millisecond timestamp of bar open
    open: float
    high: float
    low: float
    close: float
    volume: float         # Base asset volume
    quote_volume: float = 0.0
    is_closed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "quote_volume": self.quote_volume,
            "is_closed": self.is_closed,
        }


@dataclass
class TickerSnapshot:
    pair: str
    last_price: float
    max_bid: float
    min_ask: float
    change_24h: float
    coin_volume_24h: float
    unit_volume_24h: float
    server_time: int
    local_time: float = field(default_factory=time.time)

    @property
    def spread(self) -> float:
        return max(0.0, self.min_ask - self.max_bid)

    @property
    def spread_pct(self) -> float:
        mid = (self.min_ask + self.max_bid) / 2.0 if (self.min_ask + self.max_bid) > 0 else self.last_price
        return (self.spread / mid) if mid > 0 else 0.0

    @property
    def spread_bps(self) -> float:
        return self.spread_pct * 10000.0



class MarketDataManager:
    """
    Polls Roostoo ticker, synthesizes OHLCV bars for multiple timeframes,
    detects stale data, and inspects CVD / OI feature availability.
    """

    def __init__(
        self,
        api_client: RoostooClient,
        config: Optional[MarketDataConfig] = None,
        audit_logger: Optional[AuditLogger] = None,
    ):
        self.client = api_client
        self.config = config or MarketDataConfig()
        self.audit_logger = audit_logger

        self._lock = threading.Lock()
        self._latest_tickers: Dict[str, TickerSnapshot] = {}
        self._last_poll_time: float = 0.0

        # Raw candle buffers by pair and timeframe: {pair: {"1m": [...], "5m": [...], "15m": [...]}}
        self._candles: Dict[str, Dict[str, List[Candle]]] = {
            p: {"1m": [], "5m": [], "15m": []} for p in self.config.pairs
        }

        # CVD / OI availability inspection (Principle 3: No hallucinated data)
        self.cvd_available: bool = False
        self.oi_available: bool = False
        self._check_feature_availability()

    def _check_feature_availability(self) -> None:
        """
        Check if Roostoo provides native CVD or Open Interest.
        Official Roostoo /v3/ticker only provides LastPrice, MaxBid, MinAsk, Change, Volume.
        Explicitly mark unavailable so Strategy C degrades gracefully.
        """
        self.cvd_available = False
        self.oi_available = False
        if self.audit_logger:
            self.audit_logger.log_system_event(
                "DATA_AVAILABILITY_CHECK",
                "Exchange feature verification: Roostoo mock provides spot ticker/volume. "
                "Perpetual OI and tick-level CVD are unavailable. Graceful degradation active.",
                {"cvd_available": False, "oi_available": False},
            )

    @property
    def latest_tickers(self) -> Dict[str, TickerSnapshot]:
        """Thread-safe snapshot of latest tickers by pair."""
        with self._lock:
            return dict(self._latest_tickers)

    def is_stale(self, pair: str) -> bool:
        """
        Return True if market data for the pair has not updated within stale_threshold_seconds.
        """

        with self._lock:
            snap = self._latest_tickers.get(pair)
            if snap is None:
                return True
            elapsed = time.time() - snap.local_time
            return elapsed > self.config.stale_data_threshold_seconds

    def update_ticker(self, pair: Optional[str] = None) -> Dict[str, TickerSnapshot]:
        """
        Poll latest ticker from Roostoo API and update internal candle bars.
        """
        try:
            data = self.client.get_ticker(pair=pair)
            server_time = int(data.get("ServerTime", int(time.time() * 1000)))
            ticker_data = data.get("Data", {})

            updated = {}
            with self._lock:
                for p, info in ticker_data.items():
                    if self.config.pairs and p not in self.config.pairs:
                        continue

                    snap = TickerSnapshot(
                        pair=p,
                        last_price=float(info.get("LastPrice", 0.0)),
                        max_bid=float(info.get("MaxBid", 0.0)),
                        min_ask=float(info.get("MinAsk", 0.0)),
                        change_24h=float(info.get("Change", 0.0)),
                        coin_volume_24h=float(info.get("CoinTradeValue", 0.0)),
                        unit_volume_24h=float(info.get("UnitTradeValue", 0.0)),
                        server_time=server_time,
                        local_time=time.time(),
                    )
                    self._latest_tickers[p] = snap
                    updated[p] = snap

                    # Ingest price tick into candle timeframes
                    self._ingest_tick(p, snap.last_price, snap.coin_volume_24h, server_time)

                self._last_poll_time = time.time()
            return updated

        except Exception as e:
            if self.audit_logger:
                self.audit_logger.log_system_event("MARKET_DATA_ERROR", f"Error updating ticker: {e}")
            return {}

    def _ingest_tick(self, pair: str, price: float, cumulative_vol: float, timestamp_ms: int) -> None:
        """
        Aggregate ticks into 1m, 5m, and 15m candle bars.
        """
        timeframe_ms = {
            "1m": 60 * 1000,
            "5m": 5 * 60 * 1000,
            "15m": 15 * 60 * 1000,
        }

        if pair not in self._candles:
            self._candles[pair] = {"1m": [], "5m": [], "15m": []}

        for tf, ms in timeframe_ms.items():
            bar_start = (timestamp_ms // ms) * ms
            candles = self._candles[pair][tf]

            if not candles or candles[-1].timestamp < bar_start:
                # Close previous bar if open
                if candles and not candles[-1].is_closed:
                    candles[-1].is_closed = True

                # Start new candle
                new_bar = Candle(
                    timestamp=bar_start,
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    volume=0.0,
                    is_closed=False,
                )
                candles.append(new_bar)
                # Keep max 500 candles in memory
                if len(candles) > 500:
                    candles.pop(0)
            else:
                # Update current active candle
                curr = candles[-1]
                curr.high = max(curr.high, price)
                curr.low = min(curr.low, price)
                curr.close = price

    def get_latest_ticker(self, pair: str) -> Optional[TickerSnapshot]:
        with self._lock:
            return self._latest_tickers.get(pair)

    def get_candles(self, pair: str, timeframe: str = "5m", limit: int = 100) -> List[Candle]:
        """
        Return a copy of the latest candles for the given pair and timeframe.
        """
        with self._lock:
            bars = self._candles.get(pair, {}).get(timeframe, [])
            return [Candle(**b.to_dict()) for b in bars[-limit:]]

    def get_candle_df(self, pair: str, timeframe: str = "5m", limit: int = 100) -> pd.DataFrame:
        """
        Return candles as a pandas DataFrame formatted for quantitative feature calculation.
        """
        candles = self.get_candles(pair, timeframe=timeframe, limit=limit)
        if not candles:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

        df = pd.DataFrame([c.to_dict() for c in candles])
        df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("datetime", inplace=True)
        return df

    def bootstrap_candles(self, pair: str, timeframe: str, candles: List[Candle]) -> None:
        """
        Pre-load historical candles (e.g. from historical data cache or warm-up).
        """
        with self._lock:
            if pair not in self._candles:
                self._candles[pair] = {"1m": [], "5m": [], "15m": []}
            self._candles[pair][timeframe] = list(candles)
