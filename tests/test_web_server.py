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
