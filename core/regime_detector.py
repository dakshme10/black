"""
Market Regime Classification Engine.
Classifies real-time market microstructure into TREND, RANGE, HIGH_VOLATILITY_REVERSAL,
LOW_LIQUIDITY, or UNCERTAIN based on quantitative indicators and statistical thresholds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional
import numpy as np
import pandas as pd

from config.trading_params import RegimeConfig
from core.feature_engine import FeatureEngine, VolumeProfile


class MarketRegime(str, Enum):
    TREND = "TREND"
    RANGE = "RANGE"
    HIGH_VOLATILITY_REVERSAL = "HIGH_VOLATILITY_REVERSAL"
    LOW_LIQUIDITY = "LOW_LIQUIDITY"
    UNCERTAIN = "UNCERTAIN"


@dataclass
class RegimeClassification:
    regime: MarketRegime
    confidence: float
    trend_direction: str   # "BULLISH", "BEARISH", "SIDEWAYS"
    metrics: Dict[str, float] = field(default_factory=dict)
    favored_strategies: List[str] = field(default_factory=list)

    @property
    def adx(self) -> float:
        return float(self.metrics.get("adx", 0.0))

    @property
    def atr(self) -> float:
        return float(self.metrics.get("atr", 0.0))

    @property
    def volatility_percentile(self) -> float:
        return float(self.metrics.get("volatility_percentile", self.metrics.get("realized_vol", 0.0) * 100.0))


class RegimeDetector:
    """
    Evaluates rolling price action to categorize market regimes and align strategy execution.
    """

    def __init__(self, config: Optional[RegimeConfig] = None):
        self.config = config or RegimeConfig()

    def classify(
        self,
        df: pd.DataFrame,
        vp: Optional[VolumeProfile] = None,
    ) -> RegimeClassification:
        """
        Classify market regime using ADX, ATR, realized volatility, volume ratios, and VWAP distance.
        """
        if len(df) < self.config.lookback_periods:
            return RegimeClassification(
                regime=MarketRegime.UNCERTAIN,
                confidence=0.50,
                trend_direction="SIDEWAYS",
                metrics={"reason": "Insufficient candle lookback"},
                favored_strategies=[],
            )

        closes = df["close"].values
        volumes = df["volume"].values
        last_close = closes[-1]

        # 1. ADX and Directional Indicators
        adx_series, plus_di_series, minus_di_series = FeatureEngine.calculate_adx(df, period=self.config.atr_period)
        adx = float(adx_series.iloc[-1])
        plus_di = float(plus_di_series.iloc[-1])
        minus_di = float(minus_di_series.iloc[-1])

        # 2. Realized Volatility
        realized_vol = FeatureEngine.calculate_realized_volatility(df, window=20)

        # 3. ATR and ATR percent
        atr_series = FeatureEngine.calculate_atr(df, period=self.config.atr_period)
        curr_atr = float(atr_series.iloc[-1])
        atr_pct = (curr_atr / last_close) if last_close > 0 else 0.0

        # 4. Volume expansion / compression
        recent_vol = float(np.mean(volumes[-5:]))
        rolling_vol_med = float(np.median(volumes[-self.config.lookback_periods:]))
        vol_ratio = (recent_vol / rolling_vol_med) if rolling_vol_med > 0 else 1.0

        # 5. VWAP distance and extension
        vwap_dist_pct = 0.0
        vwap_zscore = 0.0
        if vp and vp.vwap > 0:
            vwap_dist_pct = (last_close - vp.vwap) / vp.vwap
            if vp.vwap_std > 0:
                vwap_zscore = (last_close - vp.vwap) / vp.vwap_std

        metrics = {
            "adx": round(adx, 2),
            "plus_di": round(plus_di, 2),
            "minus_di": round(minus_di, 2),
            "realized_vol": round(realized_vol, 4),
            "atr": round(curr_atr, 2),
            "atr_pct": round(atr_pct, 4),
            "vol_ratio": round(vol_ratio, 2),
            "vwap_dist_pct": round(vwap_dist_pct, 4),
            "vwap_zscore": round(vwap_zscore, 2),
        }

        # Trend direction determination
        if plus_di > minus_di + 5.0 and closes[-1] > closes[-10]:
            trend_direction = "BULLISH"
        elif minus_di > plus_di + 5.0 and closes[-1] < closes[-10]:
            trend_direction = "BEARISH"
        else:
            trend_direction = "SIDEWAYS"

        # Classification Hierarchy:
        # Rule 1: Low Liquidity check
        if vol_ratio < 0.25 and adx < 15.0:
            return RegimeClassification(
                regime=MarketRegime.LOW_LIQUIDITY,
                confidence=0.85,
                trend_direction=trend_direction,
                metrics=metrics,
                favored_strategies=[],
            )

        # Rule 2: High Volatility Reversal check (sharp volatility spike + VWAP extreme stretch)
        if (realized_vol > 0.60 or abs(vwap_zscore) > 2.2) and vol_ratio > 1.4:
            return RegimeClassification(
                regime=MarketRegime.HIGH_VOLATILITY_REVERSAL,
                confidence=min(0.95, 0.70 + abs(vwap_zscore) * 0.1),
                trend_direction=trend_direction,
                metrics=metrics,
                favored_strategies=["LIQUIDITY_SWEEP", "CVD_ABSORPTION"],
            )

        # Rule 3: Trending market (high ADX and clear directional imbalance)
        if adx >= self.config.adx_trend_threshold:
            conf = min(0.95, 0.60 + (adx - self.config.adx_trend_threshold) * 0.015)
            return RegimeClassification(
                regime=MarketRegime.TREND,
                confidence=conf,
                trend_direction=trend_direction,
                metrics=metrics,
                favored_strategies=["LIQUIDITY_SWEEP"],
            )

        # Rule 4: Range-bound consolidation (low ADX, prices oscillating around VWAP/POC)
        if adx < 22.0 and abs(vwap_zscore) < 1.8:
            conf = min(0.95, 0.65 + (22.0 - adx) * 0.015)
            return RegimeClassification(
                regime=MarketRegime.RANGE,
                confidence=conf,
                trend_direction="SIDEWAYS",
                metrics=metrics,
                favored_strategies=["VALUE_AREA"],
            )

        # Default: Uncertain
        return RegimeClassification(
            regime=MarketRegime.UNCERTAIN,
            confidence=0.50,
            trend_direction=trend_direction,
            metrics=metrics,
            favored_strategies=[],
        )
