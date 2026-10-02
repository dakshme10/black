"""
Trading parameters and configuration management.
Parses config.yaml and environment variables with strict validation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml
from dotenv import load_dotenv


@dataclass
class ExchangeConfig:
    name: str = "roostoo_mock"
    base_url: str = "https://mock-api.roostoo.com"
    requests_per_second: float = 5.0
    burst_capacity: int = 10
    timeout_seconds: float = 5.0
    max_retries: int = 4
    retry_base_delay: float = 0.5
    retry_max_delay: float = 8.0


@dataclass
class PortfolioConfig:
    initial_capital: float = 100000.0
    min_cash_reserve_pct: float = 0.05
    max_gross_exposure_pct: float = 1.00
    max_risk_per_trade_pct: float = 0.01
    max_open_positions: int = 2
    max_position_notional_pct: float = 0.50


@dataclass
class RiskControlsConfig:
    rolling_24h_drawdown_limit: float = 0.035
    freeze_duration_hours: float = 6.0
    max_drawdown_limit: float = 0.06
    enforce_circuit_breakers: bool = True
    reconciliation_interval_seconds: float = 60.0
    emergency_recovery_sl_pct: float = 0.02
    entry_cooldown_seconds: float = 300.0
    candle_warmup_candles: int = 30


@dataclass
class FeesConfig:
    maker_fee_pct: float = 0.0005  # 0.05%
    taker_fee_pct: float = 0.0010  # 0.10%
    slippage_pct: float = 0.0002   # 0.02%


@dataclass
class TrailingStopConfig:
    breakeven_trigger_r: float = 1.0
    trail_activation_r: float = 2.0
    atr_multiplier: float = 1.5
    atr_period: int = 14


@dataclass
class AutoSLConfig:
    """
    Configuration for AutoSL Stop Loss Trailing & Breakout Failed Exit Engine.
    """
    # Phase 1 Validation Window
    validation_candles: int = 2
    candle_timeframe_seconds: int = 60
    min_expansion_percent: float = 1.0
    phase2_profit_activation: float = 2.0

    # Layer A & B Trailing Stop
    hard_sl_trailing_step: float = 10.0
    hard_sl_step_usd: Dict[str, float] = field(default_factory=lambda: {
        "BTC/USD": 50.0,
        "ETH/USD": 4.0,
        "SOL/USD": 0.5,
        "BTCUSDT": 50.0,
        "ETHUSDT": 4.0,
        "SOLUSDT": 0.5,
    })
    recalc_interval_seconds: float = 60.0
    min_tick_buffer: float = 0.5
    tick_size: float = 0.1
    profit_protection_tiers: List[Dict[str, float]] = field(default_factory=lambda: [
        {"profit_pct": 10.0, "sl_percent": 0.4},
        {"profit_pct": 5.0, "sl_percent": 0.6},
        {"profit_pct": 3.0, "sl_percent": 0.9},
        {"profit_pct": 1.5, "sl_percent": 1.2},
    ])

    # Fast Momentum Extension
    momentum_extension_enabled: bool = True
    fast_target_max_seconds: float = 180.0
    max_extensions: int = 3
    profit_lock_ratio: float = 0.50
    extreme_range_threshold_pct: float = 0.2

    def to_dict(self) -> Dict[str, Any]:
        return {
            "validation_candles": self.validation_candles,
            "candle_timeframe_seconds": self.candle_timeframe_seconds,
            "min_expansion_percent": self.min_expansion_percent,
            "phase2_profit_activation": self.phase2_profit_activation,
            "hard_sl_trailing_step": self.hard_sl_trailing_step,
            "hard_sl_step_usd": self.hard_sl_step_usd,
            "recalc_interval_seconds": self.recalc_interval_seconds,
            "min_tick_buffer": self.min_tick_buffer,
            "tick_size": self.tick_size,
            "profit_protection_tiers": self.profit_protection_tiers,
            "momentum_extension_enabled": self.momentum_extension_enabled,
            "fast_target_max_seconds": self.fast_target_max_seconds,
            "max_extensions": self.max_extensions,
            "profit_lock_ratio": self.profit_lock_ratio,
            "extreme_range_threshold_pct": self.extreme_range_threshold_pct,
        }


@dataclass
class MarketDataConfig:
    pairs: List[str] = field(default_factory=lambda: ["BTC/USD", "ETH/USD"])
    poll_interval_seconds: float = 5.0
    stale_data_threshold_seconds: float = 30.0
    primary_timeframe: str = "5m"
    context_timeframe: str = "15m"


@dataclass
class RegimeConfig:
    lookback_periods: int = 50
    adx_trend_threshold: float = 25.0
    atr_period: int = 14
    volatility_high_percentile: float = 80.0
    volatility_low_percentile: float = 20.0


@dataclass
class ValueAreaConfig:
    enabled: bool = True
    volume_profile_fraction: float = 0.70
    lookback_candles: int = 72
    min_risk_reward: float = 1.5
    tp1_allocation_pct: float = 0.50
    tp2_allocation_pct: float = 0.50


@dataclass
class LiquiditySweepConfig:
    enabled: bool = True
    swing_lookback: int = 20
    displacement_factor: float = 1.2
    min_risk_reward: float = 2.0
    tp1_r_multiple: float = 2.0


@dataclass
class CvdAbsorptionConfig:
    enabled: bool = True
    delta_period: int = 14
    divergence_window: int = 5
    require_vwap_reclaim: bool = True


@dataclass
class StrategiesConfig:
    value_area: ValueAreaConfig = field(default_factory=ValueAreaConfig)
    liquidity_sweep: LiquiditySweepConfig = field(default_factory=LiquiditySweepConfig)
    cvd_absorption: CvdAbsorptionConfig = field(default_factory=CvdAbsorptionConfig)


@dataclass
class AuditConfig:
    enable_hash_chain: bool = True
    audit_file: str = "logs/audit_trail.jsonl"
    api_log_file: str = "logs/api_requests.jsonl"
    trade_log_file: str = "logs/trade_log.csv"
    max_log_bytes: int = 104857600


@dataclass
class WebConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8080
    auth_token: str = ""
    broadcast_interval_seconds: float = 1.0


@dataclass
class TelegramConfig:
    enabled: bool = True
    bot_token: str = ""
    chat_id: str = ""


@dataclass
class AppConfig:
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)
    risk_controls: RiskControlsConfig = field(default_factory=RiskControlsConfig)
    fees: FeesConfig = field(default_factory=FeesConfig)
    trailing_stop: TrailingStopConfig = field(default_factory=TrailingStopConfig)
    autosl: AutoSLConfig = field(default_factory=AutoSLConfig)
    market_data: MarketDataConfig = field(default_factory=MarketDataConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    strategies: StrategiesConfig = field(default_factory=StrategiesConfig)
    audit: AuditConfig = field(default_factory=AuditConfig)
    web: WebConfig = field(default_factory=WebConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)

    # Environment-based security flags
    api_key: str = ""
    secret_key: str = ""
    dry_run: bool = True
    live_trading_enabled: bool = False

    @property
    def is_live(self) -> bool:
        return not self.dry_run and self.live_trading_enabled


def load_config(config_path: Optional[str] = None) -> AppConfig:
    """
    Load configuration from yaml file and environment variables.
    Environment variables take precedence over config files for security settings.
    """
    load_dotenv()

    if config_path is None:
        # Default search path
        default_yaml = Path(__file__).resolve().parent / "config.yaml"
        if default_yaml.exists():
            config_path = str(default_yaml)
        else:
            config_path = "config/config.yaml"

    raw_cfg: Dict[str, Any] = {}
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            raw_cfg = yaml.safe_load(f) or {}

    app_cfg = AppConfig()

    # Populate exchange
    if "exchange" in raw_cfg:
        e = raw_cfg["exchange"]
        app_cfg.exchange = ExchangeConfig(
            name=e.get("name", app_cfg.exchange.name),
            base_url=e.get("base_url", app_cfg.exchange.base_url),
            requests_per_second=float(e.get("requests_per_second", app_cfg.exchange.requests_per_second)),
            burst_capacity=int(e.get("burst_capacity", app_cfg.exchange.burst_capacity)),
            timeout_seconds=float(e.get("timeout_seconds", app_cfg.exchange.timeout_seconds)),
            max_retries=int(e.get("max_retries", app_cfg.exchange.max_retries)),
            retry_base_delay=float(e.get("retry_base_delay", app_cfg.exchange.retry_base_delay)),
            retry_max_delay=float(e.get("retry_max_delay", app_cfg.exchange.retry_max_delay)),
        )

    # Populate portfolio
    if "portfolio" in raw_cfg:
        p = raw_cfg["portfolio"]
        app_cfg.portfolio = PortfolioConfig(
            initial_capital=float(p.get("initial_capital", app_cfg.portfolio.initial_capital)),
            min_cash_reserve_pct=float(p.get("min_cash_reserve_pct", app_cfg.portfolio.min_cash_reserve_pct)),
            max_gross_exposure_pct=float(p.get("max_gross_exposure_pct", app_cfg.portfolio.max_gross_exposure_pct)),
            max_risk_per_trade_pct=float(p.get("max_risk_per_trade_pct", app_cfg.portfolio.max_risk_per_trade_pct)),
            max_open_positions=int(p.get("max_open_positions", app_cfg.portfolio.max_open_positions)),
            max_position_notional_pct=float(p.get("max_position_notional_pct", app_cfg.portfolio.max_position_notional_pct)),
        )

    # Populate risk controls
    if "risk_controls" in raw_cfg:
        rc = raw_cfg["risk_controls"]
        app_cfg.risk_controls = RiskControlsConfig(
            rolling_24h_drawdown_limit=float(rc.get("rolling_24h_drawdown_limit", app_cfg.risk_controls.rolling_24h_drawdown_limit)),
            freeze_duration_hours=float(rc.get("freeze_duration_hours", app_cfg.risk_controls.freeze_duration_hours)),
            max_drawdown_limit=float(rc.get("max_drawdown_limit", app_cfg.risk_controls.max_drawdown_limit)),
            enforce_circuit_breakers=bool(rc.get("enforce_circuit_breakers", app_cfg.risk_controls.enforce_circuit_breakers)),
            reconciliation_interval_seconds=float(rc.get("reconciliation_interval_seconds", app_cfg.risk_controls.reconciliation_interval_seconds)),
            emergency_recovery_sl_pct=float(rc.get("emergency_recovery_sl_pct", app_cfg.risk_controls.emergency_recovery_sl_pct)),
            entry_cooldown_seconds=float(rc.get("entry_cooldown_seconds", app_cfg.risk_controls.entry_cooldown_seconds)),
            candle_warmup_candles=int(rc.get("candle_warmup_candles", app_cfg.risk_controls.candle_warmup_candles)),
        )

    # Populate fees
    if "fees" in raw_cfg:
        f = raw_cfg["fees"]
        app_cfg.fees = FeesConfig(
            maker_fee_pct=float(f.get("maker_fee_pct", app_cfg.fees.maker_fee_pct)),
            taker_fee_pct=float(f.get("taker_fee_pct", app_cfg.fees.taker_fee_pct)),
            slippage_pct=float(f.get("slippage_pct", app_cfg.fees.slippage_pct)),
        )

    # Populate trailing stop
    if "trailing_stop" in raw_cfg:
        ts = raw_cfg["trailing_stop"]
        app_cfg.trailing_stop = TrailingStopConfig(
            breakeven_trigger_r=float(ts.get("breakeven_trigger_r", app_cfg.trailing_stop.breakeven_trigger_r)),
            trail_activation_r=float(ts.get("trail_activation_r", app_cfg.trailing_stop.trail_activation_r)),
            atr_multiplier=float(ts.get("atr_multiplier", app_cfg.trailing_stop.atr_multiplier)),
            atr_period=int(ts.get("atr_period", app_cfg.trailing_stop.atr_period)),
        )

    # Populate AutoSL (Stop Loss Trailing & Breakout Failed Exit)
    if "autosl" in raw_cfg or "exit_engine" in raw_cfg:
        asl = raw_cfg.get("autosl") or raw_cfg.get("exit_engine", {})
        vw = asl.get("validation_window", {})
        tsl = asl.get("trailing_stop_loss", {})
        me = asl.get("momentum_extension", {})

        app_cfg.autosl = AutoSLConfig(
            validation_candles=int(vw.get("validation_candles", asl.get("validation_candles", app_cfg.autosl.validation_candles))),
            candle_timeframe_seconds=int(vw.get("candle_timeframe_seconds", asl.get("candle_timeframe_seconds", app_cfg.autosl.candle_timeframe_seconds))),
            min_expansion_percent=float(vw.get("min_expansion_percent", asl.get("min_expansion_percent", app_cfg.autosl.min_expansion_percent))),
            phase2_profit_activation=float(vw.get("phase2_profit_activation", asl.get("phase2_profit_activation", app_cfg.autosl.phase2_profit_activation))),
            hard_sl_trailing_step=float(tsl.get("hard_sl_trailing_step", asl.get("hard_sl_trailing_step", app_cfg.autosl.hard_sl_trailing_step))),
            hard_sl_step_usd=tsl.get("hard_sl_step_usd", asl.get("hard_sl_step_usd", app_cfg.autosl.hard_sl_step_usd)),
            recalc_interval_seconds=float(tsl.get("recalc_interval_seconds", asl.get("recalc_interval_seconds", app_cfg.autosl.recalc_interval_seconds))),
            min_tick_buffer=float(tsl.get("min_tick_buffer", asl.get("min_tick_buffer", app_cfg.autosl.min_tick_buffer))),
            tick_size=float(tsl.get("tick_size", asl.get("tick_size", app_cfg.autosl.tick_size))),
            profit_protection_tiers=tsl.get("profit_protection_tiers", asl.get("profit_protection_tiers", app_cfg.autosl.profit_protection_tiers)),
            momentum_extension_enabled=bool(me.get("enabled", asl.get("momentum_extension_enabled", app_cfg.autosl.momentum_extension_enabled))),
            fast_target_max_seconds=float(me.get("fast_target_max_seconds", asl.get("fast_target_max_seconds", app_cfg.autosl.fast_target_max_seconds))),
            max_extensions=int(me.get("max_extensions", asl.get("max_extensions", app_cfg.autosl.max_extensions))),
            profit_lock_ratio=float(me.get("profit_lock_ratio", asl.get("profit_lock_ratio", app_cfg.autosl.profit_lock_ratio))),
            extreme_range_threshold_pct=float(me.get("extreme_range_threshold_pct", asl.get("extreme_range_threshold_pct", app_cfg.autosl.extreme_range_threshold_pct))),
        )

    # Populate market data
    if "market_data" in raw_cfg:
        md = raw_cfg["market_data"]
        app_cfg.market_data = MarketDataConfig(
            pairs=md.get("pairs", app_cfg.market_data.pairs),
            poll_interval_seconds=float(md.get("poll_interval_seconds", app_cfg.market_data.poll_interval_seconds)),
            stale_data_threshold_seconds=float(md.get("stale_data_threshold_seconds", app_cfg.market_data.stale_data_threshold_seconds)),
            primary_timeframe=str(md.get("primary_timeframe", app_cfg.market_data.primary_timeframe)),
            context_timeframe=str(md.get("context_timeframe", app_cfg.market_data.context_timeframe)),
        )

    # Populate regime detection
    if "regime_detection" in raw_cfg:
        rd = raw_cfg["regime_detection"]
        app_cfg.regime = RegimeConfig(
            lookback_periods=int(rd.get("lookback_periods", app_cfg.regime.lookback_periods)),
            adx_trend_threshold=float(rd.get("adx_trend_threshold", app_cfg.regime.adx_trend_threshold)),
            atr_period=int(rd.get("atr_period", app_cfg.regime.atr_period)),
            volatility_high_percentile=float(rd.get("volatility_high_percentile", app_cfg.regime.volatility_high_percentile)),
            volatility_low_percentile=float(rd.get("volatility_low_percentile", app_cfg.regime.volatility_low_percentile)),
        )

    # Populate strategies
    if "strategies" in raw_cfg:
        st = raw_cfg["strategies"]
        va_raw = st.get("value_area", {})
        ls_raw = st.get("liquidity_sweep", {})
        cvd_raw = st.get("cvd_absorption", {})

        app_cfg.strategies = StrategiesConfig(
            value_area=ValueAreaConfig(
                enabled=bool(va_raw.get("enabled", True)),
                volume_profile_fraction=float(va_raw.get("volume_profile_fraction", 0.70)),
                lookback_candles=int(va_raw.get("lookback_candles", 72)),
                min_risk_reward=float(va_raw.get("min_risk_reward", 1.5)),
                tp1_allocation_pct=float(va_raw.get("tp1_allocation_pct", 0.50)),
                tp2_allocation_pct=float(va_raw.get("tp2_allocation_pct", 0.50)),
            ),
            liquidity_sweep=LiquiditySweepConfig(
                enabled=bool(ls_raw.get("enabled", True)),
                swing_lookback=int(ls_raw.get("swing_lookback", 20)),
                displacement_factor=float(ls_raw.get("displacement_factor", 1.2)),
                min_risk_reward=float(ls_raw.get("min_risk_reward", 2.0)),
                tp1_r_multiple=float(ls_raw.get("tp1_r_multiple", 2.0)),
            ),
            cvd_absorption=CvdAbsorptionConfig(
                enabled=bool(cvd_raw.get("enabled", True)),
                delta_period=int(cvd_raw.get("delta_period", 14)),
                divergence_window=int(cvd_raw.get("divergence_window", 5)),
                require_vwap_reclaim=bool(cvd_raw.get("require_vwap_reclaim", True)),
            ),
        )

    # Populate audit
    if "audit" in raw_cfg:
        au = raw_cfg["audit"]
        app_cfg.audit = AuditConfig(
            enable_hash_chain=bool(au.get("enable_hash_chain", True)),
            audit_file=str(au.get("audit_file", "logs/audit_trail.jsonl")),
            api_log_file=str(au.get("api_log_file", "logs/api_requests.jsonl")),
            trade_log_file=str(au.get("trade_log_file", "logs/trade_log.csv")),
            max_log_bytes=int(au.get("max_log_bytes", 104857600)),
        )

    # Populate web dashboard
    if "web" in raw_cfg:
        wb = raw_cfg["web"]
        app_cfg.web = WebConfig(
            enabled=bool(wb.get("enabled", True)),
            host=str(wb.get("host", "0.0.0.0")),
            port=int(wb.get("port", 8080)),
            auth_token=str(wb.get("auth_token", "")),
            broadcast_interval_seconds=float(wb.get("broadcast_interval_seconds", 1.0)),
        )

    # Populate telegram
    if "telegram" in raw_cfg:
        tg = raw_cfg["telegram"]
        app_cfg.telegram = TelegramConfig(
            enabled=bool(tg.get("enabled", True)),
            bot_token=str(tg.get("bot_token", "")),
            chat_id=str(tg.get("chat_id", "")),
        )

    # Environment variables override credentials and execution safety
    app_cfg.api_key = os.getenv("ROOSTOO_API_KEY", "")
    app_cfg.secret_key = os.getenv("ROOSTOO_SECRET_KEY", "")
    base_url_env = os.getenv("ROOSTOO_BASE_URL")
    if base_url_env:
        app_cfg.exchange.base_url = base_url_env

    # Telegram env overrides
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if tg_token:
        app_cfg.telegram.bot_token = tg_token.strip()
    tg_chat = os.getenv("TELEGRAM_CHAT_ID")
    if tg_chat:
        app_cfg.telegram.chat_id = tg_chat.strip()
    tg_enabled = os.getenv("TELEGRAM_ENABLED")
    if tg_enabled is not None:
        app_cfg.telegram.enabled = tg_enabled.lower() in ("true", "1", "yes")
    elif not app_cfg.telegram.bot_token or not app_cfg.telegram.chat_id:
        app_cfg.telegram.enabled = False

    # Web Dashboard env overrides
    dash_port = os.getenv("ROOSTOO_DASHBOARD_PORT")
    if dash_port and dash_port.isdigit():
        app_cfg.web.port = int(dash_port)
    dash_token = os.getenv("ROOSTOO_DASHBOARD_TOKEN")
    if dash_token:
        app_cfg.web.auth_token = dash_token

    # Safety flags
    dry_run_env = os.getenv("DRY_RUN", "true").lower()
    app_cfg.dry_run = dry_run_env in ("true", "1", "yes")

    live_enabled_env = os.getenv("LIVE_TRADING_ENABLED", "false").lower()
    app_cfg.live_trading_enabled = live_enabled_env in ("true", "1", "yes")

    return app_cfg
