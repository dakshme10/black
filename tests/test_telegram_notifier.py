"""
Unit tests for TelegramNotifier module.
"""

from unittest.mock import MagicMock, patch
import pytest

from core.telegram_notifier import TelegramNotifier
from config.trading_params import AppConfig, TelegramConfig


def test_telegram_disabled_by_default():
    notifier = TelegramNotifier()
    assert not notifier.enabled
    assert not notifier.send_message("Test message")


def test_telegram_enabled():
    notifier = TelegramNotifier(
        bot_token="test_token",
        chat_id="test_chat",
        enabled=True,
    )
    assert notifier.enabled
    assert notifier.chat_id == "test_chat"


@patch("urllib.request.urlopen")
def test_telegram_send_message_blocking(mock_urlopen):
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_urlopen.return_value.__enter__.return_value = mock_resp

    notifier = TelegramNotifier(
        bot_token="test_token",
        chat_id="12345",
        enabled=True,
    )
    result = notifier.send_message("<b>Hello World</b>", blocking=True)
    assert result is True
    assert mock_urlopen.called


@patch("urllib.request.urlopen")
def test_telegram_notify_startup(mock_urlopen):
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_urlopen.return_value.__enter__.return_value = mock_resp

    notifier = TelegramNotifier(
        bot_token="test_token",
        chat_id="12345",
        enabled=True,
    )
    notifier.notify_startup(
        mode="DRY_RUN",
        git_commit="6800d7cd",
        equity=100000.0,
        pairs=["BTC/USD", "ETH/USD"],
        tickers={"BTC/USD": 85000.0, "ETH/USD": 2700.0},
    )
    # Give thread a small moment to execute
    import time
    time.sleep(0.1)
    assert mock_urlopen.called


@patch("urllib.request.urlopen")
def test_telegram_notify_trade_entry(mock_urlopen):
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_urlopen.return_value.__enter__.return_value = mock_resp

    notifier = TelegramNotifier(
        bot_token="test_token",
        chat_id="12345",
        enabled=True,
    )
    notifier.notify_trade_entry(
        symbol="BTC/USD",
        side="BUY",
        strategy="REGIME_BREAKOUT",
        price=85000.0,
        quantity=0.05,
        notional_usd=4250.0,
        stop_loss=83300.0,
        take_profit=86700.0,
        confidence=0.85,
    )
    import time
    time.sleep(0.1)
    assert mock_urlopen.called


@patch("urllib.request.urlopen")
def test_telegram_notify_trade_exit(mock_urlopen):
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_urlopen.return_value.__enter__.return_value = mock_resp

    notifier = TelegramNotifier(
        bot_token="test_token",
        chat_id="12345",
        enabled=True,
    )
    notifier.notify_trade_exit(
        symbol="BTC/USD",
        side="BUY",
        exit_reason="FAILED_BREAKOUT_EXIT",
        exit_price=84800.0,
        entry_price=85000.0,
        quantity=0.05,
        pnl_usd=-10.0,
        pnl_pct=-0.24,
        saved_loss_pct=1.76,
    )
    import time
    time.sleep(0.1)
    assert mock_urlopen.called


@patch("urllib.request.urlopen")
def test_telegram_notify_shutdown(mock_urlopen):
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_urlopen.return_value.__enter__.return_value = mock_resp

    notifier = TelegramNotifier(
        bot_token="test_token",
        chat_id="12345",
        enabled=True,
    )
    notifier.notify_shutdown(
        reason="Manual Stop",
        equity=100500.0,
        realized_pnl=500.0,
        open_positions=0,
        wait_seconds=0.1,
    )
    assert mock_urlopen.called
