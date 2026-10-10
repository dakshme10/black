"""
Unit and integration tests for Roostoo v6 Short Selling Endpoints,
reconciliation, and short-aware risk governor.
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from config.trading_params import FeesConfig
from core.api_client import RoostooClient
from core.risk_governor import ExpectedEdgeValidator, RiskGovernorConfig
from core.strategy_engine import Signal
from state.order_state import OrderStateManager
from state.portfolio_tracker import PortfolioTracker, Position
from state.reconciliation import ReconciliationEngine


class MockResponse:
    def __init__(self, json_data, status_code=200):
        self._json_data = json_data
        self.status_code = status_code

    def json(self):
        return self._json_data

    def raise_for_status(self):
        pass

    @property
    def text(self):
        import json
        return json.dumps(self._json_data)


def test_short_open_payload_formatting():
    client = RoostooClient(api_key="TEST_KEY", secret_key="TEST_SECRET")
    client._sync_server_time = MagicMock()

    with patch.object(client.session, "post") as mock_post:
        mock_post.return_value = MockResponse({
            "Success": True,
            "ID": 412,
            "Pair": "BTC/USD",
            "OrderType": "MARKET",
            "EntryPrice": 50000.0,
            "ShortQty": 0.2,
            "Collateral": 10000.0,
            "OpenFee": 10.0,
            "Status": "OPEN",
            "CreateTimestamp": 1757980800000,
        })

        resp = client.short_open(pair="BTC/USD", collateral=10000.0)
        assert resp["Success"] is True
        assert resp["ShortQty"] == 0.2

        # Verify endpoint called
        call_url = mock_post.call_args[0][0]
        assert "/v6/short_open" in call_url


def test_short_open_validation():
    client = RoostooClient(api_key="TEST_KEY", secret_key="TEST_SECRET")
    client._sync_server_time = MagicMock()

    # Minimum collateral constraint
    with pytest.raises(ValueError, match="Collateral must be at least 1.0"):
        client.short_open(pair="BTC/USD", collateral=0.5)

    # Invalid order type
    with pytest.raises(ValueError, match="Invalid order type"):
        client.short_open(pair="BTC/USD", collateral=100.0, order_type="STOP")

    # Limit order requires price
    with pytest.raises(ValueError, match="LIMIT short_open requires 'price'"):
        client.short_open(pair="BTC/USD", collateral=100.0, order_type="LIMIT", price=None)


def test_short_close_payload_formatting():
    client = RoostooClient(api_key="TEST_KEY", secret_key="TEST_SECRET")
    client._sync_server_time = MagicMock()

    with patch.object(client.session, "post") as mock_post:
        mock_post.return_value = MockResponse({
            "Success": True,
            "ClosePrice": 48000.0,
            "RealizedPNL": 400.0,
            "CloseFee": 9.6,
            "ReturnAmount": 10390.4,
            "ClosedQty": 0.2,
            "FullyClosed": True,
        })

        resp = client.short_close(pair="BTC/USD", close_qty=0.2)
        assert resp["Success"] is True
        assert resp["FullyClosed"] is True
        assert resp["RealizedPNL"] == 400.0

        call_url = mock_post.call_args[0][0]
        assert "/v6/short_close" in call_url


def test_get_short_positions():
    client = RoostooClient(api_key="TEST_KEY", secret_key="TEST_SECRET")
    client._sync_server_time = MagicMock()

    with patch.object(client.session, "get") as mock_get:
        mock_get.return_value = MockResponse({
            "Success": True,
            "Positions": [
                {
                    "ID": 412,
                    "Pair": "BTC/USD",
                    "EntryPrice": 50000.0,
                    "ShortQty": 0.2,
                    "Collateral": 10000.0,
                    "CurrentPrice": 48000.0,
                    "UnrealizedPNL": 400.0,
                    "UnrealizedPNLPct": 0.04,
                    "PositionValue": 10400.0,
                    "CreateTimestamp": 1757980800000,
                    "PositionStatus": "OPEN",
                }
            ],
        })

        resp = client.get_short_positions()
        assert resp["Success"] is True
        assert len(resp["Positions"]) == 1
        assert resp["Positions"][0]["Pair"] == "BTC/USD"


def test_short_position_reconciliation(tmp_path):
    portfolio = PortfolioTracker(initial_capital=100000.0)
    order_mgr = OrderStateManager()
    mock_client = MagicMock()

    # Exchange spot wallet has USD only
    mock_client.get_balance.return_value = {
        "Success": True,
        "SpotWallet": {"USD": {"Free": 90000.0, "Lock": 0.0}},
    }
    # Exchange has 1 open short in BTC/USD
    mock_client.get_short_positions.return_value = {
        "Success": True,
        "Positions": [
            {
                "ID": 101,
                "Pair": "BTC/USD",
                "EntryPrice": 60000.0,
                "ShortQty": 0.5,
                "Collateral": 30000.0,
                "CurrentPrice": 59000.0,
                "CreateTimestamp": 1757980800000,
            }
        ],
    }
    mock_client.query_order.return_value = {"Success": True, "OrderMatched": []}

    engine = ReconciliationEngine(
        api_client=mock_client,
        portfolio_tracker=portfolio,
        order_manager=order_mgr,
    )

    report = engine.reconcile()
    assert report.is_synchronized is True
    # Position was discovered and reconciled as short (side="SELL")
    assert "BTC/USD" in portfolio.positions
    pos = portfolio.positions["BTC/USD"]
    assert pos.side == "SELL"
    assert pos.quantity == 0.5
    assert pos.entry_price == 60000.0


def test_short_expected_edge_calculation():
    config = RiskGovernorConfig()
    governor = ExpectedEdgeValidator(config, FeesConfig())

    # Short signal: entry 60000, TP1 at 58500 (2.5% drop), SL at 60600 (1.0% risk)
    short_signal = Signal(
        strategy="VALUE_AREA",
        symbol="BTC/USD",
        direction="SELL",
        confidence=0.85,
        entry_price=60000.0,
        stop_loss=60600.0,
        take_profit_1=58500.0,
        take_profit_2=57000.0,
        expected_rr=2.5,
        reason="VAH rejection short candidate",
        regime="RANGE",
        timestamp=1757980800000,
    )

    approved, gross_edge, min_req, costs, msg = governor.evaluate(short_signal)
    assert approved is True
    # Edge is (60000 - 58500) / 60000 = 0.025 (2.5%)
    assert pytest.approx(gross_edge, 0.001) == 0.025
    assert gross_edge > min_req
