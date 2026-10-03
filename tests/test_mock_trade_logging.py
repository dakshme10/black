"""
Unit and integration tests verifying trade logging in both DRY_RUN (paper)
and Roostoo Mock Exchange (reconciled live) modes.
"""

from __future__ import annotations

import os
import time
import pytest
from unittest.mock import MagicMock

from config.trading_params import AppConfig
from core.api_client import RoostooClient
from core.web_server import WebServer
from main import RoostooAutonomousBot
from state.order_state import Order, OrderStatus
from state.reconciliation import ReconciliationEngine


@pytest.fixture
def mock_bot(tmp_path):
    config = AppConfig()
    config.dry_run = True
    config.live_trading_enabled = False
    config.audit.audit_file = str(tmp_path / "audit.jsonl")
    config.audit.api_log_file = str(tmp_path / "api_log.jsonl")
    config.audit.trade_log_file = str(tmp_path / "trade_log.csv")
    config.web.enabled = True
    config.web.auth_token = ""

    bot = RoostooAutonomousBot(config)
    bot.order_manager.persistence_file = str(tmp_path / "orders.json")
    bot.portfolio.persistence_file = str(tmp_path / "portfolio.json")
    bot.portfolio.positions.clear()
    bot.portfolio.cash = config.portfolio.initial_capital
    bot.portfolio.realized_pnl = 0.0
    bot.portfolio.equity_curve.clear()
    bot.order_manager._orders_by_client_id.clear()
    bot.order_manager._orders_by_exchange_id.clear()
    bot.order_manager._symbol_locks.clear()

    # Mock tickers
    mock_ticker = MagicMock()
    mock_ticker.last_price = 80000.0
    mock_ticker.max_bid = 79990.0
    mock_ticker.min_ask = 80010.0
    mock_ticker.spread = 20.0
    mock_ticker.spread_bps = 2.5
    mock_ticker.coin_volume_24h = 1000.0
    mock_ticker.change_24h = 2.0
    bot.market_data._latest_tickers = {"BTC/USD": mock_ticker}

    return bot


def test_manual_trade_logging_in_mock_mode(mock_bot):
    """
    Test that manual test trades (e.g. $500 BTC) in mock/dry-run mode:
    1. Obey the requested notional_usd.
    2. Register in order_manager.
    3. Update last_order.
    4. Are logged to trade_log.csv.
    5. Are buffered in recent logs for console streaming.
    """
    res = mock_bot.handle_manual_trade("BTC/USD", "BUY", notional_usd=500.0)
    assert res["success"] is True, f"Trade failed: {res.get('error')}"

    order_dict = res["order"]
    assert order_dict is not None
    assert order_dict["symbol"] == "BTC/USD"
    assert order_dict["status"] == "FILLED"

    # Verify notional sizing ~ $500 (0.00625 BTC @ 80000)
    expected_qty = 500.0 / 80000.0
    assert abs(order_dict["quantity"] - expected_qty) < 1e-4

    # Verify last_order is updated on bot
    assert mock_bot.last_order is not None
    assert mock_bot.last_order["client_order_id"] == order_dict["client_order_id"]

    # Verify trade_log.csv has the trade recorded
    csv_file = mock_bot.logger.trade_log_file
    assert csv_file.exists()
    with open(csv_file, "r", encoding="utf-8") as f:
        content = f.read()
    assert "BTC/USD" in content
    assert "BUY" in content
    assert "SIMULATED_FILL_DRY_RUN" in content

    # Verify recent events buffer contains the execution
    recent = mock_bot.logger.get_recent_events(limit=20)
    assert any("MANUAL_TRADE_EXECUTED" in e.get("event", "") for e in recent)
    assert any("SIMULATED_FILL_DRY_RUN" in e.get("event", "") for e in recent)


def test_reconciliation_syncs_and_logs_roostoo_mock_trades(tmp_path):
    """
    Test that ReconciliationEngine fetches matched/filled orders from Roostoo Mock Exchange,
    registers them in order_manager, and records them in trade_log.csv.
    """
    cfg = AppConfig()
    cfg.dry_run = False
    cfg.live_trading_enabled = True
    cfg.audit.audit_file = str(tmp_path / "audit.jsonl")
    cfg.audit.api_log_file = str(tmp_path / "api.jsonl")
    cfg.audit.trade_log_file = str(tmp_path / "trade_log.csv")

    bot = RoostooAutonomousBot(cfg)
    bot.order_manager.persistence_file = str(tmp_path / "orders.json")
    bot.portfolio.persistence_file = str(tmp_path / "portfolio.json")

    # Mock API client balance & query_order responses
    bot.client.get_balance = MagicMock(return_value={
        "Success": True,
        "SpotWallet": {
            "USD": {"Free": 90000.0, "Lock": 0.0},
            "BTC": {"Free": 0.125, "Lock": 0.0},
        },
    })
    bot.client.get_ticker = MagicMock(return_value={
        "Success": True,
        "Data": {
            "BTC/USD": {"LastPrice": 80000.0},
        },
    })

    # Return a matched filled order from exchange
    bot.client.query_order = MagicMock(side_effect=lambda **kwargs: {
        "Success": True,
        "OrderMatched": [
            {
                "OrderID": 999123,
                "Pair": "BTC/USD",
                "Side": "BUY",
                "Type": "MARKET",
                "Status": "FILLED",
                "Quantity": 0.125,
                "FilledQuantity": 0.125,
                "FilledAverPrice": 80000.0,
                "Price": 80000.0,
                "CommissionChargeValue": 10.0,
                "Role": "TAKER",
                "CreateTimestamp": 1690000000000,
                "FinishTimestamp": 1690000000050,
            }
        ],
    })

    recon = ReconciliationEngine(
        api_client=bot.client,
        portfolio_tracker=bot.portfolio,
        order_manager=bot.order_manager,
        audit_logger=bot.logger,
    )

    report = recon.reconcile()
    assert report.is_synchronized is True

    # Order should be in order_manager
    synced_order = bot.order_manager.get_order_by_exchange_id(999123)
    assert synced_order is not None
    assert synced_order.status == OrderStatus.FILLED
    assert synced_order.quantity == 0.125
    assert synced_order.filled_avg_price == 80000.0

    # Trade should be logged in trade_log.csv
    csv_file = bot.logger.trade_log_file
    assert csv_file.exists()
    with open(csv_file, "r", encoding="utf-8") as f:
        csv_content = f.read()
    assert "999123" in csv_content
    assert ("EXCHANGE_MOCK_ORDER_SYNCED" in csv_content) or ("ORDER_FILL_RECONCILED" in csv_content)
    assert "BTC/USD" in csv_content
