"""
Strategy A: Volume Profile / Auction Market Theory (AMT).
Executes VAL failed-auction reclaims (Long) and VAH rejection spot de-risking.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional
import pandas as pd

from config.trading_params import ValueAreaConfig
from core.feature_engine import FeatureEngine, VolumeProfile
from core.regime_detector import MarketRegime, RegimeClassification


class ValueAreaStrategy:
    """
    Auction Market Theory Strategy:
    - Long on failed auction breakdown and reclaim of Value Area Low (VAL).
    - De-risk spot positions when price sweeps above Value Area High (VAH) and rejects.
    """

    def __init__(self, config: Optional[ValueAreaConfig] = None):
        self.config = config or ValueAreaConfig()
        self.name = "VALUE_AREA"

    def evaluate(
        self,
        symbol: str,
        df: pd.DataFrame,
        regime_info: RegimeClassification,
        fees_pct: float = 0.0012, # Estimated round-trip taker fees + slippage
    ) -> Dict[str, Any]:
        """
        Evaluate candle data and generate a standardized signal.
        """
        if not self.config.enabled:
            return self._no_trade(symbol, "Strategy disabled in configuration")

        # Failsafe on insufficient candles
        if len(df) < self.config.lookback_candles:
            return self._no_trade(symbol, "Insufficient candle history for Volume Profile")

        # In trending market or low liquidity, avoid aggressive mean reversion
        if regime_info.regime in (MarketRegime.LOW_LIQUIDITY, MarketRegime.UNCERTAIN):
            return self._no_trade(symbol, f"Regime {regime_info.regime.value} not favorable for Value Area")

        # Calculate Volume Profile over lookback
        df_window = df.iloc[-self.config.lookback_candles :]
        vp = FeatureEngine.calculate_volume_profile(
            df_window,
            volume_fraction=self.config.volume_profile_fraction,
        )
        if vp is None or vp.val >= vp.vah:
            return self._no_trade(symbol, "Invalid volume profile structure")

        closes = df_window["close"].values
        highs = df_window["high"].values
        lows = df_window["low"].values
        timestamps = df_window["timestamp"].values if "timestamp" in df_window else [int(time.time() * 1000)] * len(closes)

        atr_series = FeatureEngine.calculate_atr(df_window, period=14)
        curr_atr = float(atr_series.iloc[-1]) if len(atr_series) > 0 else 0.0
        atr_buffer = curr_atr * 0.5 if curr_atr > 0 else closes[-1] * 0.002

        curr_close = float(closes[-1])
        curr_low = float(lows[-1])
        curr_high = float(highs[-1])
        prev_close = float(closes[-2])
        prev_low = float(lows[-2])
        prev_high = float(highs[-2])

        # ---------------------------------------------------------------------
        # 1. LONG SIGNAL: FAILED AUCTION / VALUE RECLAIM
        # ---------------------------------------------------------------------
        # Conditions:
        # 1. Recent bars probed below VAL (failed auction breakdown).
        # 2. Current bar reclaims and closes firmly above VAL.
        # 3. Minimum R:R is satisfied with TP1 at POC and TP2 at VAH.
        recent_probed_below_val = any(l < vp.val for l in lows[-5:])
        closed_above_val = curr_close > vp.val and prev_close <= vp.val * 1.002

        if recent_probed_below_val and closed_above_val and curr_close < vp.poc:
            sweep_low = min(lows[-5:])
            stop_loss = float(sweep_low - atr_buffer)
            risk = curr_close - stop_loss

            if risk > 0:
                tp1 = float(vp.poc)
                tp2 = float(vp.vah)
                # Weighted target: 50% at TP1, 50% at TP2
                avg_reward = (tp1 * self.config.tp1_allocation_pct + tp2 * self.config.tp2_allocation_pct) - curr_close
                expected_rr = avg_reward / risk

                # Ensure edge exceeds fees and minimum R:R
                net_edge_pct = (avg_reward / curr_close) - fees_pct
                if expected_rr >= self.config.min_risk_reward and net_edge_pct > 0.003:
                    # Higher confidence if regime is RANGE
                    conf = 0.85 if regime_info.regime == MarketRegime.RANGE else 0.70
                    return {
                        "strategy": self.name,
                        "symbol": symbol,
                        "direction": "BUY",
                        "confidence": conf,
                        "entry_price": curr_close,
                        "stop_loss": stop_loss,
                        "take_profit_1": tp1,
                        "take_profit_2": tp2,
                        "expected_rr": round(expected_rr, 2),
                        "reason": f"Failed auction below VAL ({vp.val:.2f}) reclaimed with close at {curr_close:.2f}",
                        "regime": regime_info.regime.value,
                        "timestamp": int(timestamps[-1]),
                        "metadata": {
                            "val": vp.val,
                            "poc": vp.poc,
                            "vah": vp.vah,
                            "vwap": vp.vwap,
                        },
                    }

        # ---------------------------------------------------------------------
        # 2. SHORT-SIDE DE-RISKING (SPOT ONLY)
        # ---------------------------------------------------------------------
        # Conditions: Price sweeps above VAH and rejects -> reduce spot exposure
        swept_vah = any(h > vp.vah for h in highs[-3:])
        rejected_vah = curr_close < vp.vah and curr_close < prev_close

        if swept_vah and rejected_vah:
            return {
                "strategy": self.name,
                "symbol": symbol,
                "direction": "DE_RISK",
                "confidence": 0.80,
                "entry_price": curr_close,
                "stop_loss": curr_close * 1.01,
                "take_profit_1": vp.poc,
                "take_profit_2": vp.val,
                "expected_rr": 1.5,
                "reason": f"VAH rejection ({vp.vah:.2f}) observed. De-risking spot exposure to cash.",
                "regime": regime_info.regime.value,
                "timestamp": int(timestamps[-1]),
                "metadata": {"vah": vp.vah, "poc": vp.poc},
            }

        return self._no_trade(symbol, "No Value Area auction condition met")

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
