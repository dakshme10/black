"""
Unit tests for Exchange Reconciliation Engine.
Covers startup reconciliation, balance alignment to exchange truth, and UNKNOWN state recovery.
"""

import pytest
import time
from unittest.mock import MagicMock

from core.api_client import RoostooClient
from state.order_state import Order, OrderStateManager, OrderStatus
from state.portfolio_tracker import PortfolioTracker
from state.reconciliation import ReconciliationEngine


def test_reconciliation_perfect_match():
    """When local cash and wallet match exchange, reconciliation succeeds with no discrepancy."""
    portfolio = PortfolioTracker(initial_capital=100000.0, persistence_file="data/test_rec_port1.json")
    order_mgr = OrderStateManager(persistence_file="data/test_rec_ord1.json")
    client = RoostooClient("k", "s")

    client.get_balance = MagicMock(return_value={
        "Success": True,
        "ErrMsg": "",
        "Wallet": {
            "USD": {"Free": 100000.0, "Lock": 0.0},
            "BTC": {"Free": 0.0, "Lock": 0.0},
        }
    })
    client.query_order = MagicMock(return_value={"Success": True, "OrderMatched": []})

    engine = ReconciliationEngine(client, portfolio, order_mgr)
    report = engine.reconcile()

    assert report.is_synchronized is True
    assert report.cash_discrepancy <= 0.10
    assert engine.is_reconciled is True


def test_reconciliation_cash_discrepancy_alignment():
    """
    Principle 2: When local cash ($95,000) differs from exchange ($98,500),
    local portfolio must align to exchange truth.
    """
    portfolio = PortfolioTracker(initial_capital=95000.0, persistence_file="data/test_rec_port2.json")
    order_mgr = OrderStateManager(persistence_file="data/test_rec_ord2.json")
    client = RoostooClient("k", "s")

    client.get_balance = MagicMock(return_value={
        "Success": True,
        "ErrMsg": "",
        "Wallet": {
            "USD": {"Free": 98500.0, "Lock": 0.0},
        }
    })
    client.query_order = MagicMock(return_value={"Success": True, "OrderMatched": []})

    engine = ReconciliationEngine(client, portfolio, order_mgr)
    report = engine.reconcile()

    assert report.cash_discrepancy == 3500.0
    # Portfolio cash should now be updated to exchange truth $98,500
    assert portfolio.cash == 98500.0


def test_reconciliation_resolves_unknown_order():
    """
    An UNKNOWN order must be queried against exchange order history
    and resolved to FILLED if matched on exchange.
    """
    portfolio = PortfolioTracker(initial_capital=100000.0, persistence_file="data/test_rec_port3.json")
    order_mgr = OrderStateManager(persistence_file="data/test_rec_ord3.json")
    client = RoostooClient("k", "s")

    ts = int(time.time() * 1000)
    unk_order = Order(
        client_order_id="RST_VAL_BTCUSD_1001",
        symbol="BTC/USD",
        side="BUY",
        order_type="MARKET",
        quantity=0.25,
        create_timestamp=ts,
        status=OrderStatus.UNKNOWN,
    )
    order_mgr.register_order(unk_order)
    assert len(order_mgr.get_unknown_orders()) == 1

    client.get_balance = MagicMock(return_value={
        "Success": True,
        "Wallet": {"USD": {"Free": 87500.0, "Lock": 0.0}, "BTC": {"Free": 0.25, "Lock": 0.0}}
    })
    # Exchange returns matched order showing it was filled
    client.query_order = MagicMock(return_value={
        "Success": True,
        "OrderMatched": [
            {
                "OrderID": 999,
                "Pair": "BTC/USD",
                "Side": "BUY",
                "Quantity": 0.25,
                "FilledQuantity": 0.25,
                "FilledAverPrice": 50000.0,
                "Status": "FILLED",
                "CreateTimestamp": ts,
            }
        ]
    })

    engine = ReconciliationEngine(client, portfolio, order_mgr)
    report = engine.reconcile()

    assert report.is_synchronized is True
    # The UNKNOWN order should now be resolved to FILLED
    resolved = order_mgr.get_order_by_client_id("RST_VAL_BTCUSD_1001")
    assert resolved.status == OrderStatus.FILLED
    assert resolved.exchange_order_id == 999
    assert len(order_mgr.get_unknown_orders()) == 0
