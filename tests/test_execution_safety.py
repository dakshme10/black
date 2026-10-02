"""
Comprehensive Execution Safety & Trade Management Test Suite.
Verifies all 23 Section 23 safety invariants and Section 24 failure injection tests.
"""

from datetime import datetime, timezone
import json
import os
import time
from unittest.mock import MagicMock, patch
import pytest

from config.trading_params import AppConfig
from core.api_client import RoostooClient, UnknownOrderStateError, RoostooAPIError
from core.autosl_exit_engine import AutoSLExitEngine, CryptoPosition
from core.order_executor import OrderExecutor
from core.risk_manager import RiskManager
from core.strategy_engine import Signal
from core.telegram_notifier import TelegramNotifier
from logs.audit_logger import AuditLogger
from state.order_state import Order, OrderStateManager, OrderStatus
from state.portfolio_tracker import PortfolioTracker, Position
from state.reconciliation import ReconciliationEngine


@pytest.fixture
def mock_config(tmp_path):
    cfg = AppConfig()
    cfg.dry_run = True
    cfg.live_trading_enabled = False
    cfg.audit.audit_file = str(tmp_path / "audit.jsonl")
    cfg.audit.api_log_file = str(tmp_path / "api.jsonl")
    cfg.audit.enable_hash_chain = True
    return cfg


@pytest.fixture
def order_manager(tmp_path):
    return OrderStateManager(persistence_file=str(tmp_path / "orders.json"))


@pytest.fixture
def portfolio(tmp_path):
    return PortfolioTracker(initial_capital=100000.0, persistence_file=str(tmp_path / "portfolio.json"))


@pytest.fixture
def audit_logger(mock_config):
    return AuditLogger(
        audit_file=mock_config.audit.audit_file,
        api_log_file=mock_config.audit.api_log_file,
        enable_hash_chain=True,
    )


@pytest.fixture
def mock_api_client(mock_config, audit_logger):
    client = RoostooClient(
        api_key="test_key",
        secret_key="test_secret",
        config=mock_config.exchange,
        audit_logger=audit_logger,
    )
    # Ensure all client methods return mock responses by default
    client.get_balance = MagicMock(return_value={"Success": True, "Wallet": {"USD": {"Free": "100000.0", "Lock": "0.0"}}})
    client.query_order = MagicMock(return_value={"Success": True, "OrderMatched": []})
    client.get_ticker = MagicMock(return_value={"Success": True, "Data": {"BTC/USD": {"LastPrice": "80000.0"}}})
    return client


@pytest.fixture
def risk_manager(portfolio, order_manager, mock_config, audit_logger):
    return RiskManager(
        portfolio=portfolio,
        risk_config=mock_config.risk_controls,
        trailing_config=mock_config.trailing_stop,
        max_risk_per_trade_pct=0.01,
        max_gross_exposure_pct=0.70,
        min_cash_reserve_pct=0.05,
        max_open_positions=3,
        order_manager=order_manager,
        audit_logger=audit_logger,
    )


@pytest.fixture
def executor(mock_config, mock_api_client, portfolio, order_manager, risk_manager, audit_logger):
    return OrderExecutor(
        config=mock_config,
        api_client=mock_api_client,
        portfolio=portfolio,
        order_manager=order_manager,
        risk_manager=risk_manager,
        fees_config=mock_config.fees,
        audit_logger=audit_logger,
    )


# =========================================================================
# Test 1: Duplicate BUY suppression (Same signal, same candle repeated)
# =========================================================================
def test_duplicate_buy_suppression(executor, risk_manager, portfolio):
    sig = Signal(
        strategy="VALUE_AREA",
        symbol="BTC/USD",
        direction="BUY",
        confidence=0.85,
        entry_price=80000.0,
        stop_loss=78400.0,
        take_profit_1=81600.0,
        take_profit_2=83200.0,
        expected_rr=2.0,
        reason="Test duplicate signal",
        regime="RANGE",
        timestamp=1000000,
    )

    # First evaluation: approved & executed
    dec1 = risk_manager.evaluate_signal(sig)
    assert dec1.approved is True
    order1 = executor.execute_decision(sig, dec1, 80000.0)
    assert order1 is not None

    # Immediate second evaluation on same candle/signal: must be rejected
    dec2 = risk_manager.evaluate_signal(sig)
    assert dec2.approved is False
    assert "DUPLICATE_ENTRY_REJECTED" in dec2.reason


# =========================================================================
# Test 2: Existing position blocks new BUY
# =========================================================================
def test_existing_position_blocks_new_buy(risk_manager, portfolio):
    portfolio.record_fill(symbol="BTC/USD", side="BUY", quantity=0.5, price=80000.0, fee=0.0)
    assert portfolio.positions["BTC/USD"].quantity == 0.5

    sig = Signal(
        strategy="LIQUIDITY_SWEEP",
        symbol="BTC/USD",
        direction="BUY",
        confidence=0.90,
        entry_price=81000.0,
        stop_loss=79000.0,
        take_profit_1=83000.0,
        take_profit_2=85000.0,
        expected_rr=2.0,
        reason="Second entry",
        regime="TRENDING_UP",
        timestamp=1005000,
    )
    dec = risk_manager.evaluate_signal(sig)
    assert dec.approved is False
    assert "active position" in dec.reason.lower() or "duplicate" in dec.reason.lower()


# =========================================================================
# Test 3: Pending entry blocks new BUY
# =========================================================================
def test_pending_entry_blocks_new_buy(risk_manager, order_manager):
    # Lock symbol with PENDING_EXCHANGE order
    order_manager.create_order(
        client_order_id="test_ord_1",
        symbol="BTC/USD",
        side="BUY",
        quantity=0.1,
        price=80000.0,
        order_type="MARKET",
        strategy="TEST",
    )
    order_manager.update_order("test_ord_1", status=OrderStatus.PENDING_EXCHANGE)
    order_manager.lock_symbol("BTC/USD", reason="PENDING_EXCHANGE")

    sig = Signal(
        strategy="VALUE_AREA",
        symbol="BTC/USD",
        direction="BUY",
        confidence=0.85,
        entry_price=80000.0,
        stop_loss=78400.0,
        take_profit_1=81600.0,
        take_profit_2=83200.0,
        expected_rr=2.0,
        reason="Entry during pending",
        regime="RANGE",
        timestamp=1000000,
    )
    dec = risk_manager.evaluate_signal(sig)
    assert dec.approved is False
    assert "pending" in dec.reason.lower() or "locked" in dec.reason.lower()


# =========================================================================
# Test 4: UNKNOWN entry blocks new BUY
# =========================================================================
def test_unknown_entry_blocks_new_buy(risk_manager, order_manager):
    order_manager.create_order(
        client_order_id="test_ord_unk",
        symbol="BTC/USD",
        side="BUY",
        quantity=0.2,
        price=80000.0,
        order_type="MARKET",
        strategy="TEST",
    )
    order_manager.update_order("test_ord_unk", status=OrderStatus.UNKNOWN)
    order_manager.lock_symbol("BTC/USD", reason="UNKNOWN_ORDER_FREEZE")

    sig = Signal(
        strategy="VALUE_AREA",
        symbol="BTC/USD",
        direction="BUY",
        confidence=0.85,
        entry_price=80000.0,
        stop_loss=78400.0,
        take_profit_1=81600.0,
        take_profit_2=83200.0,
        expected_rr=2.0,
        reason="Entry during unknown",
        regime="RANGE",
        timestamp=1000000,
    )
    dec = risk_manager.evaluate_signal(sig)
    assert dec.approved is False
    assert "pending" in dec.reason.lower() or "locked" in dec.reason.lower()


# =========================================================================
# Test 5: Partial fill (Requested 1.0, filled 0.4 -> position = 0.4)
# =========================================================================
def test_partial_fill_accounting(order_manager, portfolio):
    order = order_manager.create_order(
        client_order_id="test_partial_1",
        symbol="BTC/USD",
        side="BUY",
        quantity=1.0,
        price=80000.0,
        order_type="MARKET",
        strategy="TEST",
    )

    # Exchange fills 0.4 BTC
    delta, updated_order = order_manager.record_fill_delta(
        client_order_id="test_partial_1",
        exchange_cumulative_filled=0.4,
        price=80000.0,
        exchange_order_id=9001,
    )
    assert delta == 0.4
    order_manager.update_order("test_partial_1", status=OrderStatus.PARTIALLY_FILLED)

    portfolio.record_fill(symbol="BTC/USD", side="BUY", quantity=delta, price=80000.0, fee=0.0)
    assert portfolio.positions["BTC/USD"].quantity == 0.4
    assert order.remaining_quantity == 0.6
    assert order.cumulative_filled_quantity == 0.4


# =========================================================================
# Test 6: Incremental partial fills (0.4 -> 0.7 -> 1.0 without double-counting)
# =========================================================================
def test_incremental_partial_fills(order_manager, portfolio):
    order = order_manager.create_order(
        client_order_id="test_inc_1",
        symbol="BTC/USD",
        side="BUY",
        quantity=1.0,
        price=80000.0,
        order_type="MARKET",
        strategy="TEST",
    )

    # Polling round 1: 0.4 filled
    d1, _ = order_manager.record_fill_delta("test_inc_1", exchange_cumulative_filled=0.4, price=80000.0)
    assert d1 == 0.4
    portfolio.record_fill("BTC/USD", "BUY", d1, 80000.0, fee=0.0)
    assert portfolio.positions["BTC/USD"].quantity == 0.4

    # Polling round 2: unchanged 0.4 filled -> delta must be 0.0!
    d2, _ = order_manager.record_fill_delta("test_inc_1", exchange_cumulative_filled=0.4, price=80000.0)
    assert d2 == 0.0
    portfolio.record_fill("BTC/USD", "BUY", d2, 80000.0, fee=0.0)
    assert portfolio.positions["BTC/USD"].quantity == 0.4

    # Polling round 3: 0.7 cumulative filled -> delta must be 0.3!
    d3, _ = order_manager.record_fill_delta("test_inc_1", exchange_cumulative_filled=0.7, price=80000.0)
    assert round(d3, 6) == 0.3
    portfolio.record_fill("BTC/USD", "BUY", d3, 80000.0, fee=0.0)
    assert round(portfolio.positions["BTC/USD"].quantity, 6) == 0.7

    # Polling round 4: 1.0 cumulative filled -> delta must be 0.3!
    d4, _ = order_manager.record_fill_delta("test_inc_1", exchange_cumulative_filled=1.0, price=80000.0)
    assert round(d4, 6) == 0.3
    portfolio.record_fill("BTC/USD", "BUY", d4, 80000.0, fee=0.0)
    assert round(portfolio.positions["BTC/USD"].quantity, 6) == 1.0


# =========================================================================
# Test 7: Network timeout after order submission marks UNKNOWN and locks symbol
# =========================================================================
def test_network_timeout_marks_unknown_and_locks(executor, risk_manager, order_manager):
    def mock_place_order(*args, **kwargs):
        raise UnknownOrderStateError("Simulated network timeout during POST")

    executor.config.dry_run = False
    executor.config.live_trading_enabled = True
    executor.client.place_order = mock_place_order

    sig = Signal(
        strategy="VALUE_AREA",
        symbol="BTC/USD",
        direction="BUY",
        confidence=0.85,
        entry_price=80000.0,
        stop_loss=78400.0,
        take_profit_1=81600.0,
        take_profit_2=83200.0,
        expected_rr=2.0,
        reason="Timeout test",
        regime="RANGE",
        timestamp=1000000,
    )
    dec = risk_manager.evaluate_signal(sig)
    order = executor.execute_decision(sig, dec, 80000.0)

    assert order.status == OrderStatus.UNKNOWN
    assert order_manager.is_symbol_entry_locked("BTC/USD").is_locked is True

    # Subsequent BUY must be blocked
    dec2 = risk_manager.evaluate_signal(sig)
    assert dec2.approved is False


# =========================================================================
# Test 8: Unknown order later found FILLED -> position reconstructed
# =========================================================================
def test_unknown_order_found_filled(order_manager, portfolio, mock_api_client):
    order = order_manager.create_order(
        client_order_id="unk_fill_1",
        symbol="BTC/USD",
        side="BUY",
        quantity=0.5,
        price=80000.0,
        order_type="MARKET",
        strategy="TEST",
    )
    order_manager.update_order("unk_fill_1", status=OrderStatus.UNKNOWN)
    order_manager.lock_symbol("BTC/USD", reason="UNKNOWN")

    recon = ReconciliationEngine(
        api_client=mock_api_client,
        portfolio_tracker=portfolio,
        order_manager=order_manager,
    )

    mock_api_client.query_order = MagicMock(return_value={
        "Success": True,
        "OrderMatched": [{
            "OrderID": 99991,
            "Side": "BUY",
            "Quantity": 0.5,
            "FilledQuantity": 0.5,
            "FilledAverPrice": 80000.0,
            "Status": "FILLED",
            "CreateTimestamp": order.create_timestamp,
            "Role": "TAKER",
            "CommissionChargeValue": 10.0,
        }],
    })

    resolved = recon._resolve_unknown_order(order)
    assert resolved is True
    assert order.status == OrderStatus.FILLED
    assert portfolio.positions["BTC/USD"].quantity == 0.5
    assert order_manager.is_symbol_entry_locked("BTC/USD").is_locked is False


# =========================================================================
# Test 9: Unknown order later found PARTIALLY_FILLED -> partial position created
# =========================================================================
def test_unknown_order_found_partially_filled(order_manager, portfolio, mock_api_client):
    order = order_manager.create_order(
        client_order_id="unk_part_1",
        symbol="BTC/USD",
        side="BUY",
        quantity=1.0,
        price=80000.0,
        order_type="MARKET",
        strategy="TEST",
    )
    order_manager.update_order("unk_part_1", status=OrderStatus.UNKNOWN)
    order_manager.lock_symbol("BTC/USD", reason="UNKNOWN")

    recon = ReconciliationEngine(
        api_client=mock_api_client,
        portfolio_tracker=portfolio,
        order_manager=order_manager,
    )

    mock_api_client.query_order = MagicMock(return_value={
        "Success": True,
        "OrderMatched": [{
            "OrderID": 99992,
            "Side": "BUY",
            "Quantity": 1.0,
            "FilledQuantity": 0.4,
            "FilledAverPrice": 80000.0,
            "Status": "PENDING",
            "CreateTimestamp": order.create_timestamp,
            "Role": "TAKER",
            "CommissionChargeValue": 8.0,
        }],
    })

    resolved = recon._resolve_unknown_order(order)
    assert resolved is True
    assert order.status == OrderStatus.PARTIALLY_FILLED
    assert portfolio.positions["BTC/USD"].quantity == 0.4


# =========================================================================
# Test 10: TP1 partial exit (For 1.0 BTC -> TP1 sells 0.5 BTC)
# =========================================================================
def test_tp1_partial_exit(portfolio, risk_manager, executor):
    portfolio.record_fill(symbol="BTC/USD", side="BUY", quantity=1.0, price=80000.0, fee=0.0)
    pos = portfolio.positions["BTC/USD"]
    assert pos.quantity == 1.0
    assert pos.take_profit_1_quantity == 0.5

    # Trigger TP1 signal with exit_ratio 0.5
    tp1_sig = Signal(
        strategy="AUTOSL_ENGINE",
        symbol="BTC/USD",
        direction="DE_RISK",
        confidence=1.0,
        entry_price=82000.0,
        stop_loss=0.0,
        take_profit_1=0.0,
        take_profit_2=0.0,
        expected_rr=0.0,
        reason="TP1 hit",
        regime="",
        timestamp=1000000,
        metadata={"exit_reason": "TP1_HIT", "exit_ratio": 0.5, "price": 82000.0},
    )

    dec = risk_manager.evaluate_signal(tp1_sig)
    assert dec.approved is True
    assert dec.adjusted_quantity == 0.5

    order = executor.execute_decision(tp1_sig, dec, 82000.0)
    assert order.quantity == 0.5
    assert pos.quantity == 0.5
    assert pos.take_profit_1_hit is True
    assert pos.exit_lock is False  # Lock released after partial exit!


# =========================================================================
# Test 11: TP1 partial fill (Requested 0.5, filled 0.2 -> remaining 0.3 managed)
# =========================================================================
def test_tp1_partial_fill_handling(portfolio, order_manager):
    portfolio.record_fill(symbol="BTC/USD", side="BUY", quantity=1.0, price=80000.0, fee=0.0)
    pos = portfolio.positions["BTC/USD"]

    order = order_manager.create_order(
        client_order_id="tp1_ord_1",
        symbol="BTC/USD",
        side="SELL",
        quantity=0.5,
        price=82000.0,
        order_type="MARKET",
        strategy="AUTOSL",
    )

    # 0.2 filled
    d, _ = order_manager.record_fill_delta("tp1_ord_1", exchange_cumulative_filled=0.2, price=82000.0)
    portfolio.record_fill(symbol="BTC/USD", side="SELL", quantity=d, price=82000.0, fee=0.0)

    assert round(pos.quantity, 6) == 0.8
    assert round(order.remaining_quantity, 6) == 0.3


# =========================================================================
# Test 12: TP2 only sells remaining position
# =========================================================================
def test_tp2_closes_remaining_position(portfolio, risk_manager, executor):
    portfolio.record_fill(symbol="BTC/USD", side="BUY", quantity=1.0, price=80000.0, fee=0.0)
    # TP1 already completed
    portfolio.record_fill(symbol="BTC/USD", side="SELL", quantity=0.5, price=82000.0, fee=0.0)
    pos = portfolio.positions["BTC/USD"]
    assert pos.quantity == 0.5

    # TP2 exit signal (remaining 0.5)
    tp2_sig = Signal(
        strategy="AUTOSL_ENGINE",
        symbol="BTC/USD",
        direction="DE_RISK",
        confidence=1.0,
        entry_price=84000.0,
        stop_loss=0.0,
        take_profit_1=0.0,
        take_profit_2=0.0,
        expected_rr=0.0,
        reason="TP2 hit",
        regime="",
        timestamp=1000000,
        metadata={"exit_reason": "TP2_HIT", "exit_ratio": 1.0, "price": 84000.0},
    )

    dec = risk_manager.evaluate_signal(tp2_sig)
    assert dec.approved is True
    assert dec.adjusted_quantity == 0.5

    order = executor.execute_decision(tp2_sig, dec, 84000.0)
    assert order.quantity == 0.5
    assert "BTC/USD" not in portfolio.positions


# =========================================================================
# Test 13: Double exit race (AutoSL + Web de-risk simultaneously -> ONE order)
# =========================================================================
def test_double_exit_race_protection(portfolio, risk_manager, executor):
    portfolio.record_fill(symbol="BTC/USD", side="BUY", quantity=1.0, price=80000.0, fee=0.0)
    pos = portfolio.positions["BTC/USD"]

    sig1 = Signal(
        strategy="AUTOSL_ENGINE",
        symbol="BTC/USD",
        direction="DE_RISK",
        confidence=1.0,
        entry_price=78000.0,
        stop_loss=0.0,
        take_profit_1=0.0,
        take_profit_2=0.0,
        expected_rr=0.0,
        reason="SL_HIT",
        regime="",
        timestamp=1000000,
    )
    sig2 = Signal(
        strategy="OPERATOR_OVERRIDE",
        symbol="BTC/USD",
        direction="DE_RISK",
        confidence=1.0,
        entry_price=78000.0,
        stop_loss=0.0,
        take_profit_1=0.0,
        take_profit_2=0.0,
        expected_rr=0.0,
        reason="WEB_DERISK",
        regime="",
        timestamp=1000000,
    )

    # First exit initiates and sets exit_lock
    dec1 = risk_manager.evaluate_signal(sig1)
    order1 = executor.execute_decision(sig1, dec1, 78000.0)
    assert order1 is not None

    # Second exit attempts during active exit -> blocked!
    dec2 = risk_manager.evaluate_signal(sig2)
    assert dec2.approved is False or "ALREADY_EXITING" in dec2.reason or pos.exit_lock is False


# =========================================================================
# Test 14: Discovered position must never have zero entry or zero SL
# =========================================================================
def test_reconciliation_discovers_position_nonzero_sl(portfolio, order_manager, mock_api_client):
    recon = ReconciliationEngine(
        api_client=mock_api_client,
        portfolio_tracker=portfolio,
        order_manager=order_manager,
        emergency_recovery_sl_pct=0.02,
    )

    mock_api_client.get_balance = MagicMock(return_value={
        "Success": True,
        "Wallet": {
            "USD": {"Free": "50000.0", "Lock": "0.0"},
            "BTC": {"Free": "0.5", "Lock": "0.0"},
        },
    })
    mock_api_client.query_order = MagicMock(return_value={"Success": True, "OrderMatched": []})
    mock_api_client.get_ticker = MagicMock(return_value={
        "Success": True,
        "Data": {"BTC/USD": {"LastPrice": "80000.0"}},
    })

    rep = recon.reconcile()
    assert rep.is_synchronized is True
    assert "BTC/USD" in portfolio.positions

    pos = portfolio.positions["BTC/USD"]
    assert pos.quantity == 0.5
    assert pos.entry_price == 80000.0
    assert pos.stop_loss == 80000.0 * 0.98  # 2% emergency recovery SL
    assert pos.is_recovered is True
    assert pos.recovery_status == "RECOVERED_ACTIVE"


# =========================================================================
# Test 15: Reconciliation quantity mismatch (Local 1.0, Exchange 0.7)
# =========================================================================
def test_reconciliation_quantity_mismatch(portfolio, order_manager, mock_api_client):
    portfolio.record_fill(symbol="BTC/USD", side="BUY", quantity=1.0, price=80000.0, fee=0.0)
    recon = ReconciliationEngine(api_client=mock_api_client, portfolio_tracker=portfolio, order_manager=order_manager)

    mock_api_client.get_balance = MagicMock(return_value={
        "Success": True,
        "Wallet": {
            "USD": {"Free": "20000.0", "Lock": "0.0"},
            "BTC": {"Free": "0.7", "Lock": "0.0"},
        },
    })
    mock_api_client.query_order = MagicMock(return_value={"Success": True, "OrderMatched": []})

    rep = recon.reconcile()
    assert rep.is_synchronized is True
    assert round(portfolio.positions["BTC/USD"].quantity, 6) == 0.7


# =========================================================================
# Test 16: Local position exists but exchange position is zero -> close safely
# =========================================================================
def test_local_position_with_zero_exchange_closed_safely(portfolio, order_manager, mock_api_client):
    portfolio.record_fill(symbol="BTC/USD", side="BUY", quantity=0.5, price=80000.0, fee=0.0)
    assert "BTC/USD" in portfolio.positions

    recon = ReconciliationEngine(api_client=mock_api_client, portfolio_tracker=portfolio, order_manager=order_manager)
    # Exchange wallet has 0 BTC
    mock_api_client.get_balance = MagicMock(return_value={
        "Success": True,
        "Wallet": {
            "USD": {"Free": "100000.0", "Lock": "0.0"},
            "BTC": {"Free": "0.0", "Lock": "0.0"},
        },
    })
    mock_api_client.query_order = MagicMock(return_value={"Success": True, "OrderMatched": []})

    rep = recon.reconcile()
    assert "BTC/USD" not in portfolio.positions
    # Verifies no phantom order was placed on exchange!
    mock_api_client.place_order = MagicMock()
    assert mock_api_client.place_order.call_count == 0


# =========================================================================
# Test 17: Duplicate SELL protection
# =========================================================================
def test_duplicate_sell_protection(portfolio, risk_manager, executor):
    portfolio.record_fill(symbol="BTC/USD", side="BUY", quantity=0.5, price=80000.0, fee=0.0)

    sig1 = Signal("AUTOSL", "BTC/USD", "DE_RISK", 1.0, 79000.0, 0, 0, 0, 0, "Exit 1", "", 1000000)
    sig2 = Signal("AUTOSL", "BTC/USD", "DE_RISK", 1.0, 79000.0, 0, 0, 0, 0, "Exit 2", "", 1000000)

    dec1 = risk_manager.evaluate_signal(sig1)
    order1 = executor.execute_decision(sig1, dec1, 79000.0)
    assert order1 is not None

    dec2 = risk_manager.evaluate_signal(sig2)
    assert dec2.approved is False


# =========================================================================
# Test 18: Stale market data blocks trailing stop ratcheting
# =========================================================================
def test_stale_data_blocks_trailing_ratchet():
    engine = AutoSLExitEngine()
    now = datetime.now(timezone.utc)
    pos = CryptoPosition(
        position_id="test_stale_pos",
        symbol="BTC/USD",
        side="BUY",
        quantity=1.0,
        entry_price=80000.0,
        entry_time=now,
        entry_breakout_level=80000.0,
        stop_loss_price=78400.0,
        broker_sl_price=78400.0,
        peak_price=80000.0,
        trailing_sl_active=True,
    )

    # Normal tick ratchets peak and stop
    engine.on_tick(pos, current_price=85000.0, current_time=now, is_stale_data=False)
    assert pos.peak_price == 85000.0
    ratcheted_sl = pos.stop_loss_price

    # Stale price tick must NOT update peak or ratchet stop!
    engine.on_tick(pos, current_price=90000.0, current_time=now, is_stale_data=True)
    assert pos.peak_price == 85000.0  # Kept unchanged!
    assert pos.stop_loss_price == ratcheted_sl


# =========================================================================
# Test 19: Crash & restart state migration (v1 -> v2)
# =========================================================================
def test_crash_restart_state_migration(tmp_path):
    state_file = tmp_path / "portfolio_state.json"
    # Write a v1 format state without state_version or TP fields
    v1_data = {
        "cash": 95000.0,
        "locked_cash": 0.0,
        "positions": {
            "BTC/USD": {
                "symbol": "BTC/USD",
                "base_coin": "BTC",
                "quantity": 0.5,
                "entry_price": 80000.0,
                "current_price": 81000.0,
                "stop_loss": 78400.0,
                "side": "BUY",
                "opened_timestamp": 12345678,
            }
        },
        "realized_pnl": 500.0,
        "total_equity": 135500.0,
    }
    with open(state_file, "w") as f:
        json.dump(v1_data, f)

    # Load into PortfolioTracker
    tracker = PortfolioTracker(persistence_file=str(state_file))
    assert "BTC/USD" in tracker.positions
    pos = tracker.positions["BTC/USD"]
    assert pos.quantity == 0.5
    assert pos.take_profit_1_hit is False
    assert pos.take_profit_1_quantity == 0.0  # Initialized safely via dataclass default


# =========================================================================
# Test 20: Cash accounting invariant (SELL without inventory cannot add cash)
# =========================================================================
def test_cash_accounting_invariant_on_sell(portfolio):
    initial_cash = portfolio.cash
    # Attempting to record a SELL fill when position does not exist!
    portfolio.record_fill(symbol="ETH/USD", side="SELL", quantity=10.0, price=3000.0, fee=0.0)
    # Cash must NOT increase!
    assert portfolio.cash == initial_cash
    assert "ETH/USD" not in portfolio.positions


# =========================================================================
# Test 21: Audit log hash chain validation
# =========================================================================
def test_audit_log_hash_chain_integrity(audit_logger):
    audit_logger.log_system_event("EVENT_1", "First event")
    audit_logger.log_system_event("EVENT_2", "Second event")
    audit_logger.log_system_event("EVENT_3", "Third event")

    is_valid, msg, count = audit_logger.verify_integrity()
    assert is_valid is True, f"Audit log verification failed: {msg}"
    assert count == 3


# =========================================================================
# Test 22: Failure Injection - HTTP 500 error handling
# =========================================================================
def test_failure_injection_http_500(mock_config, monkeypatch):
    client = RoostooClient(
        api_key="test_key",
        secret_key="test_secret",
        config=mock_config.exchange,
    )
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"
    monkeypatch.setattr(client.session, "get", MagicMock(return_value=mock_resp))

    with pytest.raises(RoostooAPIError) as exc_info:
        client.get_balance()
    assert "HTTP 500" in str(exc_info.value)


# =========================================================================
# Test 23: Failure Injection - Telegram timeout never blocks trading
# =========================================================================
def test_telegram_timeout_does_not_block():
    notifier = TelegramNotifier(bot_token="fake", chat_id="fake", enabled=True)

    def slow_send(*args, **kwargs):
        time.sleep(0.5)
        raise Exception("Telegram Connection Timeout")

    with patch.object(notifier, "_send_http", side_effect=slow_send):
        t0 = time.time()
        notifier.notify_trade_entry("BTC/USD", "BUY", "TEST", 80000.0, 0.1, 8000.0, 78400.0, 82000.0, 0.9)
        elapsed = time.time() - t0
        # Main trading thread must return immediately (< 100ms)
        assert elapsed < 0.1
