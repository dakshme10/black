"""
Unit tests for the Web Telemetry Dashboard and REST/WebSocket API endpoints.
"""

from __future__ import annotations

import json
import pytest
from fastapi.testclient import TestClient

from config.trading_params import AppConfig
from core.web_server import WebServer
from main import RoostooAutonomousBot


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

    bot = RoostooAutonomousBot(config, data_dir=str(tmp_path))
    bot.order_manager.persistence_file = str(tmp_path / "orders.json")
    bot.portfolio.persistence_file = str(tmp_path / "portfolio.json")
    bot.portfolio.positions.clear()
    bot.portfolio.cash = config.portfolio.initial_capital
    bot.portfolio.realized_pnl = 0.0
    bot.portfolio.equity_curve.clear()
    bot.order_manager._orders_by_client_id.clear()
    bot.order_manager._orders_by_exchange_id.clear()
    bot.order_manager._symbol_locks.clear()

    # Seed mock ticker data so tracking works
    bot.market_data._latest_tickers = {}
    return bot


def test_index_page(mock_bot):
    server = WebServer(mock_bot, port=8080)
    client = TestClient(server.app)

    response = client.get("/")
    assert response.status_code == 200
    assert "Roostoo Autonomous Quant Bot" in response.text
    assert "Live Market & Strategies" in response.text


def test_ping_and_health_endpoints(mock_bot):
    server = WebServer(mock_bot, port=8080)
    client = TestClient(server.app)

    # Ping
    res_ping = client.get("/api/ping")
    assert res_ping.status_code == 200
    data_ping = res_ping.json()
    assert data_ping["ok"] is True
    assert "time" in data_ping

    # Health
    res_health = client.get("/api/health")
    assert res_health.status_code == 200
    data_health = res_health.json()
    assert "bot_running" in data_health
    assert "mode" in data_health
    assert data_health["mode"] == "DRY_RUN"


def test_status_endpoint(mock_bot):
    server = WebServer(mock_bot, port=8080)
    client = TestClient(server.app)

    response = client.get("/api/status")
    assert response.status_code == 200
    data = response.json()

    assert "portfolio" in data
    assert "competition" in data
    assert "tracking" in data
    assert "positions" in data
    assert "orders" in data
    assert "audit" in data
    assert data["portfolio"]["equity"] == 100000.0
    assert data["portfolio"]["cash_reserve_pct"] == 5.0
    assert data["status"]["mode"] == "DRY_RUN"


def test_positions_and_orders_endpoints(mock_bot):
    server = WebServer(mock_bot, port=8080)
    client = TestClient(server.app)

    # Seed a virtual position
    mock_bot.portfolio.record_fill(
        symbol="BTC/USD",
        side="BUY",
        quantity=0.5,

        price=80000.0,
        fee=40.0,
        strategy="VALUE_AREA",
        stop_loss=79000.0,
        take_profit_1=82000.0,
        take_profit_2=84000.0,
    )

    res_pos = client.get("/api/positions")
    assert res_pos.status_code == 200
    positions = res_pos.json()
    assert len(positions) == 1
    assert positions[0]["symbol"] == "BTC/USD"
    assert positions[0]["quantity"] == 0.5
    assert positions[0]["stop_loss"] == 79000.0

    res_orders = client.get("/api/orders")
    assert res_orders.status_code == 200
    assert isinstance(res_orders.json(), list)


def test_command_pause_and_resume(mock_bot):
    server = WebServer(mock_bot, port=8080)
    client = TestClient(server.app)

    assert mock_bot.paused is False

    # Pause
    res = client.post("/command/pause")
    assert res.status_code == 200
    assert mock_bot.paused is True

    # Resume
    res2 = client.post("/command/pause")
    assert res2.status_code == 200
    assert mock_bot.paused is False


def test_command_kill_switch(mock_bot):
    server = WebServer(mock_bot, port=8080)
    client = TestClient(server.app)

    res = client.post("/command/kill")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert mock_bot.risk_manager.permanent_kill_switch is True


def test_command_derisk_and_reset_risk(mock_bot):
    server = WebServer(mock_bot, port=8080)
    client = TestClient(server.app)

    # De-risk
    res = client.post("/command/derisk", json={"symbol": "BTC/USD"})
    assert res.status_code == 200
    assert res.json()["status"] == "success"

    # Reset risk
    mock_bot.risk_manager.freeze_until_timestamp = 99999999999.0
    res_reset = client.post("/command/reset_risk")
    assert res_reset.status_code == 200
    assert mock_bot.risk_manager.freeze_until_timestamp == 0.0



def test_command_manual_trade_risk_check(mock_bot):
    server = WebServer(mock_bot, port=8080)
    client = TestClient(server.app)

    # Without price in market data, fails gracefully
    res = client.post("/command/manual_trade", json={"symbol": "BTC/USD", "side": "BUY"})
    assert res.status_code == 200
    assert res.json()["status"] == "failed" or "error" in res.json().get("details", {})


def test_audit_endpoints(mock_bot):
    server = WebServer(mock_bot, port=8080)
    client = TestClient(server.app)

    # Log an audit event
    mock_bot.logger.log_system_event("TEST_EVENT", "Testing dashboard audit trail endpoint")

    res = client.get("/api/audit-trail")
    assert res.status_code == 200
    data = res.json()
    assert data["integrity_valid"] is True
    assert len(data["records"]) >= 1

    res_verify = client.get("/api/verify-audit")
    assert res_verify.status_code == 200
    assert res_verify.json()["valid"] is True


def test_auth_gate_protection(mock_bot):
    # Enable token authentication
    server = WebServer(mock_bot, port=8080, auth_token="secret_token_123")
    client = TestClient(server.app)

    # Unauthorized requests to /api/status should return 401
    res_unauth = client.get("/api/status")
    assert res_unauth.status_code == 401

    # Unauthorized request to / returns login gate HTML
    res_index_unauth = client.get("/")
    assert res_index_unauth.status_code == 401
    assert "ROOSTOO QUANT DASHBOARD" in res_index_unauth.text

    # Authorized with query param ?token=
    res_token_query = client.get("/api/status?token=secret_token_123")
    assert res_token_query.status_code == 200

    # Authorized with Bearer header
    res_token_header = client.get("/api/status", headers={"Authorization": "Bearer secret_token_123"})
    assert res_token_header.status_code == 200


def test_websocket_telemetry_stream(mock_bot):
    server = WebServer(mock_bot, port=8080)
    client = TestClient(server.app)

    with client.websocket_connect("/ws") as websocket:
        data = websocket.receive_json()
        assert "portfolio" in data
        assert "competition" in data
        assert "server_time" in data


def test_command_graceful_shutdown(mock_bot):
    server = WebServer(mock_bot, port=8080)
    client = TestClient(server.app)

    res = client.post("/command/shutdown")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert "shutdown" in data["message"].lower()


def test_trade_log_csv_endpoint(mock_bot):
    server = WebServer(mock_bot, port=8080)
    client = TestClient(server.app)

    res = client.get("/api/trade-log.csv")
    assert res.status_code == 200
    assert "timestamp_ist" in res.text
    assert "symbol" in res.text


def test_logs_endpoint_and_telemetry_stream(mock_bot):
    server = WebServer(mock_bot, port=8080)
    client = TestClient(server.app)

    # Log a system event
    mock_bot.logger.log_system_event("UNIT_TEST_EVENT", "Testing log streaming endpoint")

    res = client.get("/api/logs")
    assert res.status_code == 200
    logs = res.json()
    assert isinstance(logs, list)
    assert len(logs) > 0
    assert any("UNIT_TEST_EVENT" in l.get("event", "") for l in logs)

    # Check status endpoint includes logs
    res_status = client.get("/api/status")
    assert res_status.status_code == 200
    data = res_status.json()
    assert "logs" in data
    assert len(data["logs"]) > 0


def test_positions_endpoint_enriched_fields(mock_bot):
    server = WebServer(mock_bot, port=8080)
    client = TestClient(server.app)

    # Seed a virtual position with AutoSL attributes
    mock_bot.portfolio.record_fill(
        symbol="BTC/USD",
        side="BUY",
        quantity=0.5,
        price=80000.0,
        fee=40.0,
        strategy="VALUE_AREA",
        stop_loss=79000.0,
        take_profit_1=82000.0,
        take_profit_2=84000.0,
        entry_breakout_level=79800.0,
    )
    pos = mock_bot.portfolio.positions["BTC/USD"]
    pos.locked_profit = 500.0
    pos.validation_survived = True
    pos.current_price = 81000.0
    pos.unrealized_pnl = 0.5 * (81000.0 - 80000.0)

    res = client.get("/api/positions")
    assert res.status_code == 200
    positions = res.json()
    assert len(positions) == 1
    p = positions[0]

    assert p["symbol"] == "BTC/USD"
    assert p["side"] == "BUY"
    assert p["quantity"] == 0.5
    assert p["entry_price"] == 80000.0
    assert p["current_price"] == 81000.0
    assert p["notional_value"] == 0.5 * 81000.0
    assert p["unrealized_pnl"] == 500.0
    assert p["unrealized_pnl_pct"] == pytest.approx(1.25, 0.01)
    assert p["initial_stop_loss"] == 79000.0
    assert p["stop_loss"] == 79000.0
    assert p["locked_profit"] == 500.0
    assert p["take_profit_1"] == 82000.0
    assert p["take_profit_2"] == 84000.0
    assert p["phase"] == "PHASE_2_VALIDATED"
    assert p["status"] == "VALIDATED"
    assert "Phase 2 Validated" in p["trailing_status"]


def test_positions_empty_and_multi_positions(mock_bot):
    server = WebServer(mock_bot, port=8080)
    client = TestClient(server.app)

    # Empty
    res_empty = client.get("/api/positions")
    assert res_empty.status_code == 200
    assert res_empty.json() == []

    # Two positions: BTC/USD and ETH/USD
    mock_bot.portfolio.record_fill(
        symbol="BTC/USD",
        side="BUY",
        quantity=0.25,
        price=80000.0,
        fee=20.0,
        strategy="VALUE_AREA",
        stop_loss=78400.0,
        take_profit_1=82000.0,
        take_profit_2=84000.0,
    )
    mock_bot.portfolio.record_fill(
        symbol="ETH/USD",
        side="BUY",
        quantity=5.0,
        price=3000.0,
        fee=15.0,
        strategy="LIQUIDITY_SWEEP",
        stop_loss=2940.0,
        take_profit_1=3100.0,
        take_profit_2=3200.0,
    )

    res = client.get("/api/positions")
    assert res.status_code == 200
    positions = res.json()
    assert len(positions) == 2
    symbols = {p["symbol"] for p in positions}
    assert symbols == {"BTC/USD", "ETH/USD"}


def test_dashboard_html_contains_positions_elements(mock_bot):
    server = WebServer(mock_bot, port=8080)
    client = TestClient(server.app)

    res = client.get("/")
    assert res.status_code == 200
    html = res.text
    # Verify AutoSL Positions tab components
    assert "pos-summary-grid" in html
    assert "pos-toolbar" in html
    assert "pos-search-input" in html
    assert "pos-filter-btn" in html
    assert "pos-table-card" in html
    assert "pos-empty-state" in html
    assert "pos-stale-banner" in html
    assert "renderPositionsTable" in html
    assert "togglePositionExpand" in html


def test_positions_short_negative_and_flat_pnl(mock_bot):
    server = WebServer(mock_bot, port=8080)
    client = TestClient(server.app)

    # Position 1: BTC/USD with negative PnL
    mock_bot.portfolio.record_fill(
        symbol="BTC/USD",
        side="BUY",
        quantity=0.1,
        price=80000.0,
        fee=10.0,
        strategy="MOMENTUM",
        stop_loss=78000.0,
        take_profit_1=83000.0,
    )
    btc_pos = mock_bot.portfolio.positions["BTC/USD"]
    btc_pos.current_price = 79000.0
    btc_pos.unrealized_pnl = 0.1 * (79000.0 - 80000.0)  # -100.0

    # Position 2: ETH/USD SHORT with positive PnL
    from state.portfolio_tracker import Position
    mock_bot.portfolio.positions["ETH/USD"] = Position(
        symbol="ETH/USD",
        base_coin="ETH",
        side="SELL",
        quantity=2.0,
        entry_price=3200.0,
        current_price=3100.0,
        unrealized_pnl=200.0,
        strategy="REVERSION",
        stop_loss=3300.0,
        take_profit_1=3000.0,
    )

    res = client.get("/api/positions")
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 2

    btc_dict = next(p for p in data if p["symbol"] == "BTC/USD")
    eth_dict = next(p for p in data if p["symbol"] == "ETH/USD")

    assert btc_dict["unrealized_pnl"] == -100.0
    assert btc_dict["unrealized_pnl_pct"] < 0
    assert eth_dict["side"] == "SELL"
    assert eth_dict["unrealized_pnl"] == 200.0
    assert eth_dict["unrealized_pnl_pct"] > 0


def test_positions_telemetry_consistency_and_missing_attributes(mock_bot):
    server = WebServer(mock_bot, port=8080)
    client = TestClient(server.app)

    # Bare position without extra AutoSL attributes
    class BarePosition:
        symbol = "SOL/USD"
        quantity = 10.0
        entry_price = 150.0
        current_price = 155.0
        unrealized_pnl = 50.0
        realized_pnl = 0.0
        strategy = "TEST"
        side = "BUY"

        @property
        def notional_value(self):
            return self.quantity * self.current_price

    mock_bot.portfolio.positions["SOL/USD"] = BarePosition()

    res = client.get("/api/positions")
    assert res.status_code == 200
    pos_data = res.json()
    assert len(pos_data) == 1
    p = pos_data[0]
    assert p["symbol"] == "SOL/USD"
    assert p["stop_loss"] == 0.0
    assert p["phase"] == "PHASE_1_VALIDATION"
    assert "Phase 1 Validating" in p["trailing_status"]

    # Verify status/telemetry consistency
    telemetry = server._prepare_data()
    assert "positions" in telemetry
    assert len(telemetry["positions"]) == 1
    assert telemetry["positions"][0]["symbol"] == "SOL/USD"
    assert telemetry["positions"][0]["notional_value"] == 1550.0

