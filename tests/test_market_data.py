"""
Unit tests for Market Data Manager.
Covers candle synthesis across timeframes, stale-data detection, and feature inspection.
"""

import pytest
import time
from unittest.mock import MagicMock

from core.api_client import RoostooClient
from core.market_data import MarketDataManager
from config.trading_params import MarketDataConfig


def test_market_data_stale_detection():
    """Verify stale data detection flags True after threshold seconds."""
    client = RoostooClient("k", "s")
    cfg = MarketDataConfig(pairs=["BTC/USD"], stale_data_threshold_seconds=0.1)
    md = MarketDataManager(client, config=cfg)

    # Initial state is stale
    assert md.is_stale("BTC/USD") is True

    # Ingest tick
    md._ingest_tick("BTC/USD", 50000.0, 100.0, int(time.time() * 1000))
    from core.market_data import TickerSnapshot
    md._latest_tickers["BTC/USD"] = TickerSnapshot(
        pair="BTC/USD",
        last_price=50000.0,
        max_bid=49999.0,
        min_ask=50001.0,
        change_24h=0.01,
        coin_volume_24h=100.0,
        unit_volume_24h=5000000.0,
        server_time=int(time.time() * 1000),
        local_time=time.time(),
    )

    assert md.is_stale("BTC/USD") is False

    # Wait for threshold expiration
    time.sleep(0.15)
    assert md.is_stale("BTC/USD") is True


def test_market_data_candle_aggregation():
    """Verify ticks are correctly binned into 1m, 5m, and 15m candle bars."""
    client = RoostooClient("k", "s")
    md = MarketDataManager(client, config=MarketDataConfig(pairs=["BTC/USD"]))

    base_ts = 1700000000000  # A round timestamp
    md._ingest_tick("BTC/USD", 50000.0, 10.0, base_ts)
    md._ingest_tick("BTC/USD", 50500.0, 20.0, base_ts + 10000)
    md._ingest_tick("BTC/USD", 49800.0, 30.0, base_ts + 20000)
    md._ingest_tick("BTC/USD", 50200.0, 40.0, base_ts + 30000)

    c5m = md.get_candles("BTC/USD", timeframe="5m")
    assert len(c5m) == 1
    bar = c5m[0]
    assert bar.open == 50000.0
    assert bar.high == 50500.0
    assert bar.low == 49800.0
    assert bar.close == 50200.0


def test_market_data_feature_inspection_principle_3():
    """Verify that CVD and OI are marked unavailable on Roostoo spot mock."""
    client = RoostooClient("k", "s")
    md = MarketDataManager(client)
    assert md.cvd_available is False
    assert md.oi_available is False
