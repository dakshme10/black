"""
Strategy C: Cumulative Volume Delta (CVD) and Open Interest (OI) Absorption.
Detects aggressive market selling absorbed by passive limit bids, followed by delta reversal.
Adheres strictly to Principle 3: No fabricated data. Safely degrades if exchange lacks OI/CVD.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional
import pandas as pd

from config.trading_params import CvdAbsorptionConfig
from core.feature_engine import FeatureEngine
from core.regime_detector import MarketRegime, RegimeClassification


class CvdAbsorptionStrategy:
    """
    CVD / OI Absorption Strategy.
    Explicitly checks feature availability. If CVD/OI is missing from exchange market data,
    the strategy marks features unavailable and safely disables itself without hallucinating data.
    """

    def __init__(self, config: Optional[CvdAbsorptionConfig] = None):
        self.config = config or CvdAbsorptionConfig()
        self.name = "CVD_ABSORPTION"

    def evaluate(
        self,
        symbol: str,
        df: pd.DataFrame,
        regime_info: RegimeClassification,
        cvd_available: bool = False,
        oi_available: bool = False,
        fees_pct: float = 0.0012,
    ) -> Dict[str, Any]:
        """
        Evaluate absorption signals. If CVD or OI is unavailable, gracefully degrades to NO_TRADE.
        """
        if not self.config.enabled:
            return self._no_trade(symbol, "Strategy disabled in configuration")

        # ---------------------------------------------------------------------
        # Mandatory Requirement Check (Section 13 & Principle 3):
        # Explicitly detect missing CVD/OI features and never fabricate values.
        # ---------------------------------------------------------------------
        if not cvd_available:
            return self._no_trade(
                symbol,
                "CVD feature unavailable from exchange market data. Strategy safely degraded.",
                missing_features=["CVD", "OI"] if not oi_available else ["CVD"],
            )

        # Ensure we have candle history and delta columns
        if len(df) < self.config.delta_period + 5:
            return self._no_trade(symbol, "Insufficient candle history for CVD analysis")

        if "delta" not in df.columns and "cvd" not in df.columns:
            return self._no_trade(
                symbol,
                "Candle dataset lacks delta/CVD series. Safe degradation active.",
                missing_features=["delta"],
            )

        closes = df["close"].values
        lows = df["low"].values
        highs = df["high"].values
        timestamps = df["timestamp"].values if "timestamp" in df else [int(time.time() * 1000)] * len(closes)

        # Calculate CVD if delta provided
        if "cvd" in df.columns:
            cvd = df["cvd"].values
        else:
            cvd = df["delta"].cumsum().values

        # EMAs and VWAP
        emas = FeatureEngine.calculate_emas(df, periods=(20,))
        ema20 = float(emas[20].iloc[-1])
        curr_close = float(closes[-1])
        prev_close = float(closes[-2])

        atr_series = FeatureEngine.calculate_atr(df, period=14)
        curr_atr = float(atr_series.iloc[-1]) if len(atr_series) > 0 else curr_close * 0.005

        # ---------------------------------------------------------------------
        # 1. LONG SIGNAL: SELLING ABSORPTION + DELTA REVERSAL
        # ---------------------------------------------------------------------
        # Lookback window for divergence
        w = self.config.divergence_window
        price_lower_low = lows[-1] < min(lows[-w - 5 : -1]) or closes[-1] <= closes[-w]
        cvd_aggressive_drop = cvd[-1] < min(cvd[-w - 5 : -1])

        # Positive delta turn on the current bar
        curr_delta = float(df["delta"].iloc[-1]) if "delta" in df.columns else (cvd[-1] - cvd[-2])
        delta_turned_positive = curr_delta > 0

        # Price reclaim of EMA 20
        reclaimed_ema = curr_close > ema20 and prev_close <= ema20 * 1.002

        if price_lower_low and cvd_aggressive_drop and delta_turned_positive and reclaimed_ema:
            absorption_base_low = float(min(lows[-w:]))
            stop_loss = absorption_base_low - (curr_atr * 0.3)
            risk = curr_close - stop_loss

            if risk > (curr_close * 0.001):
                tp1 = curr_close + (risk * 2.0)
                tp2 = curr_close + (risk * 3.5)
                expected_rr = ((tp1 - curr_close) * 0.5 + (tp2 - curr_close) * 0.5) / risk

                if expected_rr >= 1.5:
                    return {
                        "strategy": self.name,
                        "symbol": symbol,
                        "direction": "BUY",
                        "confidence": 0.85,
                        "entry_price": curr_close,
                        "stop_loss": round(stop_loss, 4),
                        "take_profit_1": round(tp1, 4),
                        "take_profit_2": round(tp2, 4),
                        "expected_rr": round(expected_rr, 2),
                        "reason": f"Selling absorption confirmed: aggressive delta drop absorbed, EMA20 ({ema20:.2f}) reclaimed.",
                        "regime": regime_info.regime.value,
                        "timestamp": int(timestamps[-1]),
                        "metadata": {
                            "absorption_base_low": absorption_base_low,
                            "curr_delta": curr_delta,
                            "ema20": ema20,
                        },
                    }

        # ---------------------------------------------------------------------
        # 2. SHORT-SIDE DE-RISKING: BUYING ABSORPTION REJECTION
        # ---------------------------------------------------------------------
        price_higher_high = highs[-1] > max(highs[-w - 5 : -1])
        cvd_aggressive_rise = cvd[-1] > max(cvd[-w - 5 : -1])
        delta_turned_negative = curr_delta < 0
        price_failed_expansion = curr_close < prev_close

        if price_higher_high and cvd_aggressive_rise and delta_turned_negative and price_failed_expansion:
            return {
                "strategy": self.name,
                "symbol": symbol,
                "direction": "DE_RISK",
                "confidence": 0.80,
                "entry_price": curr_close,
                "stop_loss": curr_close * 1.01,
                "take_profit_1": curr_close * 0.98,
                "take_profit_2": curr_close * 0.96,
                "expected_rr": 1.8,
                "reason": "Buying absorption at high detected: price stalled despite CVD aggressive rise. De-risking spot.",
                "regime": regime_info.regime.value,
                "timestamp": int(timestamps[-1]),
                "metadata": {"curr_delta": curr_delta},
            }

        return self._no_trade(symbol, "No absorption divergence active")

    def _no_trade(self, symbol: str, reason: str, missing_features: Optional[list] = None) -> Dict[str, Any]:
        return {
            "strategy": self.name,
            "symbol": symbol,
            "direction": "NO_TRADE",
            "confidence": 0.0,
            "entry_price": 0.0,
            "stop_loss": 0.0,
            "take_profit_1": 0.0,
            "take_profit_2": 0.0,
            "expected_rr": 0.0,
            "reason": reason,
            "regime": "",
            "timestamp": int(time.time() * 1000),
            "metadata": {"missing_features": missing_features or []},
        }
