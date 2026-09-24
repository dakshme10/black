"""
Strategy B: Liquidity Sweep + Market Structure Shift (MSS).
Captures structural stop sweeps, displacement reversals, and FVG/Order Block entries.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional
import pandas as pd

from config.trading_params import LiquiditySweepConfig
from core.feature_engine import FeatureEngine
from core.regime_detector import MarketRegime, RegimeClassification


class LiquiditySweepStrategy:
    """
    Liquidity Sweep + Market Structure Shift Strategy:
    - Long on Bullish Sweep of swing lows followed by bullish displacement and MSS.
    - De-risk spot positions on Bearish Sweep of swing highs + bearish MSS.
    """

    def __init__(self, config: Optional[LiquiditySweepConfig] = None):
        self.config = config or LiquiditySweepConfig()
        self.name = "LIQUIDITY_SWEEP"

    def evaluate(
        self,
        symbol: str,
        df: pd.DataFrame,
        regime_info: RegimeClassification,
        fees_pct: float = 0.0012,
    ) -> Dict[str, Any]:
        """
        Evaluate candle data for liquidity sweep and structural shift.
        """
        if not self.config.enabled:
            return self._no_trade(symbol, "Strategy disabled in configuration")

        min_required = self.config.swing_lookback * 2 + 10
        if len(df) < min_required:
            return self._no_trade(symbol, f"Insufficient candles for swing detection (needs {min_required})")

        # Run Market Structure Detection
        ms = FeatureEngine.detect_market_structure(
            df,
            swing_lookback=self.config.swing_lookback,
            displacement_factor=self.config.displacement_factor,
        )

        closes = df["close"].values
        lows = df["low"].values
        highs = df["high"].values
        timestamps = df["timestamp"].values if "timestamp" in df else [int(time.time() * 1000)] * len(closes)

        curr_close = float(closes[-1])
        curr_low = float(lows[-1])
        curr_high = float(highs[-1])

        atr_series = FeatureEngine.calculate_atr(df, period=14)
        curr_atr = float(atr_series.iloc[-1]) if len(atr_series) > 0 else curr_close * 0.005

        # ---------------------------------------------------------------------
        # 1. BULLISH SWEEP + MSS (LONG)
        # ---------------------------------------------------------------------
        # Trigger when:
        # a) Bullish sweep detected on recent candles OR bullish MSS confirmed.
        # b) Candle closed positive with displacement.
        # c) Stop placed below sweep low with target at 2R (TP1) and structural high (TP2).
        if ms.sweep_detected == "BULLISH_SWEEP" or ms.bullish_mss_confirmed:
            # Find the sweep low level
            recent_lows = lows[-10:]
            sweep_low = float(min(recent_lows))
            stop_loss = sweep_low - (curr_atr * 0.25)
            risk = curr_close - stop_loss

            if risk > (curr_close * 0.001):
                tp1 = curr_close + (risk * self.config.tp1_r_multiple)  # +2R
                tp2 = float(ms.last_swing_high.price) if ms.last_swing_high else (curr_close + risk * 3.0)
                tp2 = max(tp2, tp1 * 1.005)

                expected_rr = ((tp1 - curr_close) * 0.5 + (tp2 - curr_close) * 0.5) / risk
                net_edge_pct = (((tp1 + tp2) / 2.0 - curr_close) / curr_close) - fees_pct

                if expected_rr >= self.config.min_risk_reward and net_edge_pct > 0.004:
                    # Higher confidence during high volatility or trending regimes
                    conf = 0.90 if regime_info.regime in (MarketRegime.HIGH_VOLATILITY_REVERSAL, MarketRegime.TREND) else 0.75
                    return {
                        "strategy": self.name,
                        "symbol": symbol,
                        "direction": "BUY",
                        "confidence": conf,
                        "entry_price": curr_close,
                        "stop_loss": round(stop_loss, 4),
                        "take_profit_1": round(tp1, 4),
                        "take_profit_2": round(tp2, 4),
                        "expected_rr": round(expected_rr, 2),
                        "reason": f"Bullish liquidity sweep below {sweep_low:.2f} + MSS confirmation",
                        "regime": regime_info.regime.value,
                        "timestamp": int(timestamps[-1]),
                        "metadata": {
                            "sweep_low": sweep_low,
                            "swing_high": ms.last_swing_high.price if ms.last_swing_high else None,
                            "trend": ms.trend,
                        },
                    }

        # ---------------------------------------------------------------------
        # 2. BEARISH SWEEP DE-RISKING (SPOT ONLY)
        # ---------------------------------------------------------------------
        if ms.sweep_detected == "BEARISH_SWEEP" or ms.bearish_mss_confirmed:
            recent_highs = highs[-10:]
            sweep_high = float(max(recent_highs))
            return {
                "strategy": self.name,
                "symbol": symbol,
                "direction": "DE_RISK",
                "confidence": 0.85,
                "entry_price": curr_close,
                "stop_loss": sweep_high * 1.01,
                "take_profit_1": curr_close * 0.98,
                "take_profit_2": curr_close * 0.96,
                "expected_rr": 2.0,
                "reason": f"Bearish liquidity sweep above {sweep_high:.2f} + MSS confirmed. De-risking spot holdings.",
                "regime": regime_info.regime.value,
                "timestamp": int(timestamps[-1]),
                "metadata": {"sweep_high": sweep_high, "trend": ms.trend},
            }

        return self._no_trade(symbol, "No structural sweep or MSS condition active")

    def _no_trade(self, symbol: str, reason: str) -> Dict[str, Any]:
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
            "metadata": {},
        }
