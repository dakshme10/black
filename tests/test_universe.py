"""
Unit tests for the multi-coin trading universe management, exchange metadata handling,
symbol isolation, order routing safety, and risk limits preservation.
"""

from __future__ import annotations

import copy
import pytest
from unittest.mock import MagicMock

from config.trading_params import AppConfig, MarketDataConfig, load_config
from core.market_data import Candle, MarketDataManager, TickerSnapshot
from core.order_executor import OrderExecutor
from core.risk_manager import RiskDecision, RiskManager
from core.strategy_engine import Signal
from core.universe_manager import (
    TARGET_UNIVERSE,
    UniverseManager,
    canonicalize_universe,
    normalize_symbol,
)
from state.order_state import Order, OrderStateManager, OrderStatus
from state.portfolio_tracker import PortfolioTracker


class TestUniverseConfiguration:
    """Tests for UniverseManager and configuration normalization."""

    def test_canonical_universe_contains_all_12_assets(self):
        expected_assets = [
            "BTC", "ETH", "PEPE", "BONK", "STO", "FET",
            "PUMP", "ENA", "S", "ADA", "SOL", "SUI"
        ]
        assert len(TARGET_UNIVERSE) == 12
        for asset in expected_assets:
            assert f"{asset}/USD" in TARGET_UNIVERSE

    def test_symbol_normalization(self):
        assert normalize_symbol("BTC") == "BTC/USD"
        assert normalize_symbol("btc/usd") == "BTC/USD"
        assert normalize_symbol("ETHUSDT") == "ETH/USD"
        assert normalize_symbol("PEPE") == "PEPE/USD"
        assert normalize_symbol("BONKUSD") == "BONK/USD"
        assert normalize_symbol("") == ""

    def test_duplicate_symbol_deduplication(self):
        input_symbols = ["BTC", "BTC/USD", "ETH", "ETHUSDT", "SOL", "sol/usd", "PEPE"]
        result = canonicalize_universe(input_symbols)
        assert result == ["BTC/USD", "ETH/USD", "SOL/USD", "PEPE/USD"]

    def test_empty_universe_handled_safely(self):
        mgr = UniverseManager()
        status = mgr.resolve_universe(requested_pairs=[])
        # Gracefully falls back to default TARGET_UNIVERSE
        assert len(status.active_pairs) == 12
        assert "BTC/USD" in status.active_pairs

    def test_btc_eth_only_backward_compatibility(self):
        mgr = UniverseManager()
        status = mgr.resolve_universe(requested_pairs=["BTC/USD", "ETH/USD"])
        assert status.active_pairs == ["BTC/USD", "ETH/USD"]
        assert len(status.active_pairs) == 2
        assert status.skipped_pairs == {}


class TestExchangeAvailabilityAndMetadata:
    """Tests for exchange filtering (CanTrade, precision, miniOrder)."""

    @pytest.fixture
    def mock_exchange_info(self):
        return {
            "BTC/USD": {
                "CanTrade": True,
                "PricePrecision": 2,
                "AmountPrecision": 5,
                "MiniOrder": 1.0,
                "Coin": "BTC",
                "Unit": "USD",
            },
            "ETH/USD": {
                "CanTrade": True,
                "PricePrecision": 2,
                "AmountPrecision": 4,
                "MiniOrder": 1.0,
                "Coin": "ETH",
                "Unit": "USD",
            },
            "PEPE/USD": {
                "CanTrade": True,
                "PricePrecision": 8,
                "AmountPrecision": 0,
                "MiniOrder": 1.0,
                "Coin": "PEPE",
                "Unit": "USD",
            },
            "SOL/USD": {
                "CanTrade": False,  # Deliberately mark non-tradable
                "PricePrecision": 2,
                "AmountPrecision": 3,
                "MiniOrder": 1.0,
                "Coin": "SOL",
                "Unit": "USD",
            },
        }

    def test_cantrade_filtering_and_skipping(self, mock_exchange_info):
        mgr = UniverseManager()
        requested = ["BTC/USD", "ETH/USD", "PEPE/USD", "SOL/USD", "UNKNOWN/USD"]
        status = mgr.resolve_universe(requested, exchange_info=mock_exchange_info)

        # Active tradable pairs
        assert "BTC/USD" in status.active_pairs
        assert "ETH/USD" in status.active_pairs
        assert "PEPE/USD" in status.active_pairs

        # Non-tradable and unlisted pairs must be skipped gracefully
        assert "SOL/USD" not in status.active_pairs
        assert "SOL/USD" in status.skipped_pairs
        assert "Exchange reports CanTrade=False" in status.skipped_pairs["SOL/USD"]

        assert "UNKNOWN/USD" not in status.active_pairs
        assert "UNKNOWN/USD" in status.skipped_pairs
        assert "Not listed" in status.skipped_pairs["UNKNOWN/USD"]

    def test_symbol_constraints_extraction(self, mock_exchange_info):
        mgr = UniverseManager()
        status = mgr.resolve_universe(["BTC/USD", "PEPE/USD"], exchange_info=mock_exchange_info)

        btc_meta = status.symbol_metadata["BTC/USD"]
        assert btc_meta.price_precision == 2
        assert btc_meta.amount_precision == 5
        assert btc_meta.min_order_usd == 1.0

        pepe_meta = status.symbol_metadata["PEPE/USD"]
        assert pepe_meta.price_precision == 8
        assert pepe_meta.amount_precision == 0  # Whole integer tokens only
        assert pepe_meta.min_order_usd == 1.0

    def test_summary_banner_generation(self, mock_exchange_info):
        mgr = UniverseManager()
        status = mgr.resolve_universe(["BTC/USD", "SOL/USD"], exchange_info=mock_exchange_info)
        banner = status.summary_banner()
        assert "TRADING UNIVERSE INITIALIZATION" in banner
        assert "Requested universe (2):" in banner
        assert "Active tradable universe (1):" in banner
        assert "Skipped/unavailable (1):" in banner
        assert "SOL/USD: Exchange reports CanTrade=False" in banner


class TestMarketDataIsolation:
    """Verify symbol state isolation: BTC != ETH != PEPE."""

    def test_market_data_candles_and_indicators_isolation(self):
        client = MagicMock()
        cfg = MarketDataConfig(pairs=["BTC/USD", "ETH/USD", "PEPE/USD"])
        md = MarketDataManager(api_client=client, config=cfg)

        # Ingest distinctive ticks
        md._ingest_tick("BTC/USD", 95000.0, 1000.0, 1700000000000)
        md._ingest_tick("ETH/USD", 3500.0, 500.0, 1700000000000)
        md._ingest_tick("PEPE/USD", 0.0000085, 20000.0, 1700000000000)

        btc_candles = md.get_candles("BTC/USD", timeframe="5m")
        eth_candles = md.get_candles("ETH/USD", timeframe="5m")
        pepe_candles = md.get_candles("PEPE/USD", timeframe="5m")

        assert len(btc_candles) > 0
        assert len(eth_candles) > 0
        assert len(pepe_candles) > 0

        assert btc_candles[-1].close == 95000.0
        assert eth_candles[-1].close == 3500.0
        assert pepe_candles[-1].close == 0.0000085

        # Verify cross-symbol independence: modifying or querying one does not contaminate another
        assert btc_candles[-1].close != eth_candles[-1].close
        assert eth_candles[-1].close != pepe_candles[-1].close
        assert pepe_candles[-1].close != btc_candles[-1].close


class TestOrderExecutionSafety:
    """Verify order execution symbol preservation and precision handling."""

    def test_order_symbol_cannot_be_mutated(self, tmp_path):
        app_cfg = AppConfig()
        app_cfg.dry_run = True
        app_cfg.live_trading_enabled = False

        client = MagicMock()
        portfolio = PortfolioTracker(initial_capital=50000.0, persistence_file=str(tmp_path / "port1.json"))
        order_mgr = OrderStateManager(persistence_file=str(tmp_path / "orders1.json"))
        risk_mgr = MagicMock()

        executor = OrderExecutor(
            config=app_cfg,
            api_client=client,
            portfolio=portfolio,
            order_manager=order_mgr,
            risk_manager=risk_mgr,
        )

        executor._exchange_info = {
            "SOL/USD": {"PricePrecision": 2, "AmountPrecision": 3, "MiniOrder": 1.0},
            "PEPE/USD": {"PricePrecision": 8, "AmountPrecision": 0, "MiniOrder": 1.0},
        }

        # Issue SOL signal
        sol_signal = Signal(
            strategy="VALUE_AREA",
            symbol="SOL/USD",
            direction="BUY",
            confidence=0.85,
            entry_price=180.0,
            stop_loss=175.0,
            take_profit_1=190.0,
            take_profit_2=200.0,
            expected_rr=2.0,
            reason="Test SOL signal",
            regime="RANGE",
            timestamp=1700000000000,
        )
        risk_decision = RiskDecision(
            approved=True,
            adjusted_quantity=5.0,
            adjusted_price=180.0,
            stop_loss=175.0,
            take_profit_1=190.0,
            take_profit_2=200.0,
        )

        order = executor.execute_decision(sol_signal, risk_decision, 180.0)
        assert order is not None
        assert order.symbol == "SOL/USD"
        assert order.symbol != "BTC/USD"
        assert "SOL/USD" in portfolio.positions
        assert "BTC/USD" not in portfolio.positions

    def test_amount_precision_zero_for_meme_coins(self, tmp_path):
        app_cfg = AppConfig()
        app_cfg.dry_run = True

        client = MagicMock()
        portfolio = PortfolioTracker(initial_capital=50000.0, persistence_file=str(tmp_path / "port2.json"))
        order_mgr = OrderStateManager(persistence_file=str(tmp_path / "orders2.json"))

        executor = OrderExecutor(
            config=app_cfg,
            api_client=client,
            portfolio=portfolio,
            order_manager=order_mgr,
            risk_manager=MagicMock(),
        )

        executor._exchange_info = {
            "PEPE/USD": {"PricePrecision": 8, "AmountPrecision": 0, "MiniOrder": 1.0},
        }

        pepe_signal = Signal(
            strategy="VALUE_AREA",
            symbol="PEPE/USD",
            direction="BUY",
            confidence=0.8,
            entry_price=0.0000085,
            stop_loss=0.0000080,
            take_profit_1=0.0000095,
            take_profit_2=0.0000100,
            expected_rr=2.0,
            reason="PEPE test",
            regime="RANGE",
            timestamp=1700000000000,
        )
        risk_decision = RiskDecision(
            approved=True,
            adjusted_quantity=11764705.88,  # Fractional quantity
            adjusted_price=0.0000085,
            stop_loss=0.0000080,
            take_profit_1=0.0000095,
            take_profit_2=0.0000100,
        )

        order = executor.execute_decision(pepe_signal, risk_decision, 0.0000085)
        assert order is not None
        # Must be floored to an exact integer (AmountPrecision = 0)
        assert order.quantity == 11764705.0
        assert isinstance(order.quantity, float)
        assert order.quantity.is_integer()


class TestRiskControlsPreservedAcrossExpandedUniverse:
    """Verify portfolio risk constraints remain strictly enforced with 12 coins."""

    def test_max_open_positions_cap_enforced(self, tmp_path):
        portfolio = PortfolioTracker(initial_capital=50000.0, persistence_file=str(tmp_path / "port3.json"))
        order_mgr = OrderStateManager(persistence_file=str(tmp_path / "orders3.json"))
        risk_mgr = RiskManager(
            portfolio=portfolio,
            max_open_positions=2,  # Strictly max 2 concurrent positions
            order_manager=order_mgr,
        )

        # Open position 1: BTC
        portfolio.record_fill(
            symbol="BTC/USD",
            side="BUY",
            quantity=0.1,
            price=60000.0,
            fee=3.0,
            strategy="VALUE_AREA",
            stop_loss=58000.0,
        )

        # Open position 2: ETH
        portfolio.record_fill(
            symbol="ETH/USD",
            side="BUY",
            quantity=2.0,
            price=3000.0,
            fee=3.0,
            strategy="VALUE_AREA",
            stop_loss=2900.0,
        )

        assert len([p for p in portfolio.positions.values() if p.quantity > 0]) == 2

        # Attempt to open position 3: SOL (must be rejected by risk manager)
        sol_signal = Signal(
            strategy="VALUE_AREA",
            symbol="SOL/USD",
            direction="BUY",
            confidence=0.9,
            entry_price=150.0,
            stop_loss=140.0,
            take_profit_1=170.0,
            take_profit_2=180.0,
            expected_rr=2.0,
            reason="Attempt 3rd position in expanded universe",
            regime="TREND",
            timestamp=1700000000000,
        )

        decision = risk_mgr.evaluate_signal(sol_signal)
        assert not decision.approved
        assert "Max open positions limit (2) reached" in decision.reason

    def test_cash_reserve_constraint_enforced(self, tmp_path):
        portfolio = PortfolioTracker(
            initial_capital=10000.0,
            min_cash_reserve_pct=0.05,
            persistence_file=str(tmp_path / "port4.json"),
        )
        # Cash is $10,000, reserve is $500.
        # Buy $9,200 of BTC: cash becomes ~$795.4, equity remains ~$10,000.
        portfolio.record_fill(
            symbol="BTC/USD",
            side="BUY",
            quantity=0.1,
            price=92000.0,
            fee=4.6,
            strategy="VALUE_AREA",
            stop_loss=90000.0,
        )
        # 5% reserve of $10,000 is $500 -> available cash is ~$295.4
        risk_mgr = RiskManager(portfolio=portfolio, min_cash_reserve_pct=0.05, max_open_positions=3)
        assert portfolio.available_cash < 300.0

        pepe_signal = Signal(
            strategy="VALUE_AREA",
            symbol="PEPE/USD",
            direction="BUY",
            confidence=0.8,
            entry_price=0.00001,
            stop_loss=0.0000099,  # Tight stop -> calculated size would want $10,000 notional
            take_profit_1=0.000012,
            take_profit_2=0.000014,
            expected_rr=2.0,
            reason="Cash reserve test",
            regime="RANGE",
            timestamp=1700000000000,
        )
        decision = risk_mgr.evaluate_signal(pepe_signal)
        # Notional must be strictly capped at available cash, leaving the 5% reserve intact
        assert decision.approved
        notional = decision.adjusted_quantity * pepe_signal.entry_price
        assert notional <= portfolio.available_cash + 0.01

    def test_gross_exposure_limit_enforced(self, tmp_path):
        portfolio = PortfolioTracker(
            initial_capital=10000.0,
            min_cash_reserve_pct=0.05,
            persistence_file=str(tmp_path / "port5.json"),
        )
        # Existing position with 9500 notional (already at 95% of equity)
        portfolio.record_fill(
            symbol="BTC/USD",
            side="BUY",
            quantity=0.1,
            price=95000.0,
            fee=5.0,
            strategy="VALUE_AREA",
            stop_loss=90000.0,
        )
        risk_mgr = RiskManager(
            portfolio=portfolio,
            max_gross_exposure_pct=1.00,  # 100% max exposure
            max_open_positions=3,
        )

        sui_signal = Signal(
            strategy="VALUE_AREA",
            symbol="SUI/USD",
            direction="BUY",
            confidence=0.8,
            entry_price=2.0,
            stop_loss=1.8,
            take_profit_1=2.3,
            take_profit_2=2.5,
            expected_rr=2.0,
            reason="Exposure limit test",
            regime="TREND",
            timestamp=1700000000000,
        )
        decision = risk_mgr.evaluate_signal(sui_signal)
        # Remaining exposure is at most $500 ($10000 - $9500)
        assert decision.adjusted_quantity * 2.0 <= 500.01

    def test_kill_switch_blocks_all_signals(self, tmp_path):
        portfolio = PortfolioTracker(initial_capital=10000.0, persistence_file=str(tmp_path / "port6.json"))
        risk_mgr = RiskManager(portfolio=portfolio)
        risk_mgr.permanent_kill_switch = True

        for sym in ["BTC/USD", "PEPE/USD", "SOL/USD"]:
            sig = Signal(
                strategy="VALUE_AREA",
                symbol=sym,
                direction="BUY",
                confidence=0.9,
                entry_price=10.0,
                stop_loss=9.0,
                take_profit_1=12.0,
                take_profit_2=14.0,
                expected_rr=2.0,
                reason="Kill switch test",
                regime="RANGE",
                timestamp=1700000000000,
            )
            decision = risk_mgr.evaluate_signal(sig)
            assert not decision.approved
            assert decision.circuit_breaker_active
