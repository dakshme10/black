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


@patch("urllib.request.urlopen")
def test_telegram_notify_circuit_breaker_throttles_repeated_alerts(mock_urlopen):
    """
    Ensure that repeated calls to notify_circuit_breaker within the cooldown period
    are suppressed and do not flood Telegram, even when reason countdown text changes.
    """
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_urlopen.return_value.__enter__.return_value = mock_resp

    notifier = TelegramNotifier(
        bot_token="test_token",
        chat_id="12345",
        enabled=True,
    )

    # 1. First alert should be sent
    res1 = notifier.notify_circuit_breaker(
        reason="FROZEN: Rolling 24h drawdown freeze active (360.0 min remaining)",
        current_drawdown_pct=0.038,
        max_drawdown_pct=0.06,
    )
    assert res1 is True
    import time
    time.sleep(0.05)
    initial_count = mock_urlopen.call_count
    assert initial_count >= 1

    # 2. Second alert with countdown decremented (e.g. 5 seconds later) should be dropped
    res2 = notifier.notify_circuit_breaker(
        reason="FROZEN: Rolling 24h drawdown freeze active (359.9 min remaining)",
        current_drawdown_pct=0.038,
        max_drawdown_pct=0.06,
    )
    assert res2 is False
    time.sleep(0.05)
    assert mock_urlopen.call_count == initial_count

    # 3. Third alert with another countdown string should also be dropped
    res3 = notifier.notify_circuit_breaker(
        reason="FROZEN: Rolling 24h drawdown freeze active (359.8 min remaining)",
        current_drawdown_pct=0.039,
        max_drawdown_pct=0.06,
    )
    assert res3 is False
    time.sleep(0.05)
    assert mock_urlopen.call_count == initial_count


@patch("urllib.request.urlopen")
def test_telegram_notify_circuit_breaker_escalation(mock_urlopen):
    """
    Ensure that severity escalation (e.g. from rolling freeze to permanent kill switch)
    alerts immediately despite the cooldown.
    """
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_urlopen.return_value.__enter__.return_value = mock_resp

    notifier = TelegramNotifier(
        bot_token="test_token",
        chat_id="12345",
        enabled=True,
    )

    # 1. Rolling freeze alert
    res1 = notifier.notify_circuit_breaker(
        reason="ROLLING_DRAWDOWN_BREAKER_TRIGGERED",
        current_drawdown_pct=0.036,
        max_drawdown_pct=0.06,
    )
    assert res1 is True
    import time
    time.sleep(0.05)
    count_after_first = mock_urlopen.call_count
    assert count_after_first >= 1

    # 2. Escalation to permanent kill switch
    res2 = notifier.notify_circuit_breaker(
        reason="PERMANENT_HALT: Maximum drawdown limit reached",
        current_drawdown_pct=0.065,
        max_drawdown_pct=0.06,
    )
    assert res2 is True
    time.sleep(0.05)
    assert mock_urlopen.call_count > count_after_first


@patch("urllib.request.urlopen")
def test_telegram_notify_circuit_breaker_cleared_resets_latch(mock_urlopen):
    """
    Ensure notify_circuit_breaker_cleared sends resolution alert and resets the latch.
    """
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_urlopen.return_value.__enter__.return_value = mock_resp

    notifier = TelegramNotifier(
        bot_token="test_token",
        chat_id="12345",
        enabled=True,
    )

    # 1. Trigger breaker
    notifier.notify_circuit_breaker(
        reason="FROZEN: Rolling 24h drawdown freeze active (360.0 min remaining)",
        current_drawdown_pct=0.038,
        max_drawdown_pct=0.06,
    )
    import time
    time.sleep(0.05)
    count_1 = mock_urlopen.call_count

    # 2. Clear breaker
    res_clear = notifier.notify_circuit_breaker_cleared(
        reason="Operator reset freeze via dashboard",
        current_drawdown_pct=0.01,
    )
    assert res_clear is True
    time.sleep(0.05)
    count_2 = mock_urlopen.call_count
    assert count_2 > count_1

    # 3. New trip after clearance should be allowed to send immediately
    res_retrip = notifier.notify_circuit_breaker(
        reason="ROLLING_DRAWDOWN_BREAKER_TRIGGERED",
        current_drawdown_pct=0.04,
        max_drawdown_pct=0.06,
    )
    assert res_retrip is True
    time.sleep(0.05)
    assert mock_urlopen.call_count > count_2

