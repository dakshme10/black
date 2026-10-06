"""
Strategy Engine & Signal Aggregation.
Orchestrates Strategy A (Value Area), Strategy B (Liquidity Sweep), and Strategy C (CVD Absorption),
evaluates market regime, normalizes confidence, prevents conflicting exposure, and returns
standardized Signal objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any, Dict, List, Optional
import pandas as pd

from config.trading_params import StrategiesConfig
from core.feature_engine import FeatureEngine
from core.regime_detector import MarketRegime, RegimeClassification, RegimeDetector
from strategies.cvd_absorption import CvdAbsorptionStrategy
from strategies.liquidity_sweep import LiquiditySweepStrategy
from strategies.value_area import ValueAreaStrategy


@dataclass
class Signal:
    strategy: str
    symbol: str
    direction: str       # "BUY", "SELL", "DE_RISK", "NO_TRADE"
    confidence: float    # 0.0 to 1.0
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    expected_rr: float
    reason: str
    regime: str
    timestamp: int
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_actionable(self) -> bool:
        return self.direction in ("BUY", "DE_RISK") and self.confidence >= 0.60


class StrategyEngine:
    """
    Coordinates multi-strategy signal evaluation, regime filtering, and signal selection.
    """

    def __init__(
        self,
        config: Optional[StrategiesConfig] = None,
        regime_detector: Optional[RegimeDetector] = None,
    ):
        self.config = config or StrategiesConfig()
        self.regime_detector = regime_detector or RegimeDetector()

        # Initialize active strategies (Strategy A & B)
        self.strategy_va = ValueAreaStrategy(self.config.value_area)
        self.strategy_ls = LiquiditySweepStrategy(self.config.liquidity_sweep)
        # Strategy C is strictly decommissioned
        if hasattr(self.config, "cvd_absorption") and self.config.cvd_absorption:
            self.config.cvd_absorption.enabled = False
        self.strategy_cvd = CvdAbsorptionStrategy(self.config.cvd_absorption)

    def evaluate_symbol(
        self,
        symbol: str,
        df: pd.DataFrame,
        cvd_available: bool = False,
        oi_available: bool = False,
        taker_fee_pct: float = 0.0010,
        slippage_pct: float = 0.0002,
    ) -> Signal:
        """
        Evaluate candle data across all three strategies and return the best actionable signal.
        """
        if len(df) < 30:
            return self._create_no_trade_signal(symbol, "Insufficient candle data for strategy evaluation")

        # 1. Volume profile for regime detection
        vp = FeatureEngine.calculate_volume_profile(df, volume_fraction=0.70)

        # 2. Classify market regime
        regime_info = self.regime_detector.classify(df, vp=vp)

        # In UNCERTAIN or LOW_LIQUIDITY regimes, do not open new trades (Section 14)
        if regime_info.regime in (MarketRegime.UNCERTAIN, MarketRegime.LOW_LIQUIDITY):
            return self._create_no_trade_signal(
                symbol,
                f"Regime is {regime_info.regime.value}. Market conditions unfavorable for fresh entries.",
                regime=regime_info.regime.value,
            )

        fees_pct = taker_fee_pct + slippage_pct

        # 3. Collect signals from all strategies
        raw_signals: List[Dict[str, Any]] = []

        # Strategy A: Value Area
        sig_va = self.strategy_va.evaluate(symbol, df, regime_info, fees_pct=fees_pct)
        if sig_va["direction"] != "NO_TRADE":
            raw_signals.append(sig_va)

        # Strategy B: Liquidity Sweep + MSS
        sig_ls = self.strategy_ls.evaluate(symbol, df, regime_info, fees_pct=fees_pct)
        if sig_ls["direction"] != "NO_TRADE":
            raw_signals.append(sig_ls)

        # Strategy C: CVD Absorption (gracefully handles missing CVD/OI)
        if getattr(self.config, "cvd_absorption", None) and self.config.cvd_absorption.enabled:
            sig_cvd = self.strategy_cvd.evaluate(
                symbol,
                df,
                regime_info,
                cvd_available=cvd_available,
                oi_available=oi_available,
                fees_pct=fees_pct,
            )
            if sig_cvd["direction"] != "NO_TRADE":
                raw_signals.append(sig_cvd)

        curr_vol = float(df["volume"].iloc[-1]) if ("volume" in df.columns and len(df) > 0) else 0.0
        prev_vol = float(df["volume"].iloc[-2]) if ("volume" in df.columns and len(df) > 1) else 0.0
        for s in raw_signals:
            meta = s.setdefault("metadata", {})
            meta.setdefault("candle_volume", curr_vol)
            meta.setdefault("prev_candle_volume", prev_vol)
            if "breakout_level" not in meta:
                if "sweep_low" in meta and meta["sweep_low"]:
                    meta["breakout_level"] = float(meta["sweep_low"])
                elif "sweep_high" in meta and meta["sweep_high"]:
                    meta["breakout_level"] = float(meta["sweep_high"])
                elif "val" in meta and meta["val"]:
                    meta["breakout_level"] = float(meta["val"])
                elif "vah" in meta and meta["vah"]:
                    meta["breakout_level"] = float(meta["vah"])
                else:
                    meta["breakout_level"] = float(s.get("entry_price", 0.0))

        if not raw_signals:
            return self._create_no_trade_signal(
                symbol,
                "No strategy generated an actionable signal under current regime conditions.",
                regime=regime_info.regime.value,
            )

        # 4. Filter and Prioritize Signals (Section 15)
        # Priority 1: DE_RISK signals take immediate priority to protect spot capital
        de_risk_signals = [s for s in raw_signals if s["direction"] == "DE_RISK"]
        if de_risk_signals:
            best_derisk = max(de_risk_signals, key=lambda s: s["confidence"])
            return self._dict_to_signal(best_derisk)

        # Priority 2: BUY signals aligned with regime
        buy_signals = [s for s in raw_signals if s["direction"] == "BUY"]
        if not buy_signals:
            return self._create_no_trade_signal(symbol, "No actionable BUY signal found", regime=regime_info.regime.value)

        # If regime is TREND, penalize counter-trend mean reversion (Value Area)
        if regime_info.regime == MarketRegime.TREND:
            for s in buy_signals:
                if s["strategy"] == "VALUE_AREA" and regime_info.trend_direction == "BEARISH":
                    s["confidence"] *= 0.60  # heavily downweight counter-trend buy in strong downtrend

        # Check edge > fees + slippage + safety buffer (0.3% net edge minimum)
        valid_buys = []
        for s in buy_signals:
            entry = s["entry_price"]
            reward = ((s["take_profit_1"] + s["take_profit_2"]) / 2.0) - entry
            risk = entry - s["stop_loss"]
            if risk <= 0:
                continue

            net_edge = (reward / entry) - fees_pct
            if net_edge >= 0.003 and s["expected_rr"] >= 1.5:
                # Add regime alignment bonus
                if s["strategy"] in regime_info.favored_strategies:
                    s["confidence"] = min(0.98, s["confidence"] + 0.05)
                valid_buys.append(s)

        if not valid_buys:
            return self._create_no_trade_signal(
                symbol,
                "Candidate signals failed net edge or R:R threshold after fees and slippage",
                regime=regime_info.regime.value,
            )

        # Pick top signal by confidence
        best_signal = max(valid_buys, key=lambda s: s["confidence"])
        return self._dict_to_signal(best_signal)

    def _dict_to_signal(self, d: Dict[str, Any]) -> Signal:
        return Signal(
            strategy=d.get("strategy", "UNKNOWN"),
            symbol=d.get("symbol", ""),
            direction=d.get("direction", "NO_TRADE"),
            confidence=float(d.get("confidence", 0.0)),
            entry_price=float(d.get("entry_price", 0.0)),
            stop_loss=float(d.get("stop_loss", 0.0)),
            take_profit_1=float(d.get("take_profit_1", 0.0)),
            take_profit_2=float(d.get("take_profit_2", 0.0)),
            expected_rr=float(d.get("expected_rr", 0.0)),
            reason=d.get("reason", ""),
            regime=d.get("regime", ""),
            timestamp=int(d.get("timestamp", int(time.time() * 1000))),
            metadata=d.get("metadata", {}),
        )

    def _create_no_trade_signal(self, symbol: str, reason: str, regime: str = "") -> Signal:
        return Signal(
            strategy="STRATEGY_ENGINE",
            symbol=symbol,
            direction="NO_TRADE",
            confidence=0.0,
            entry_price=0.0,
            stop_loss=0.0,
            take_profit_1=0.0,
            take_profit_2=0.0,
            expected_rr=0.0,
            reason=reason,
            regime=regime,
            timestamp=int(time.time() * 1000),
            metadata={},
        )
