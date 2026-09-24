"""
Quantitative Feature Engine.
Computes Volume Profile (VAH, VAL, POC, VWAP), Market Structure Shifts (MSS),
Liquidity Sweeps, Fair Value Gaps (FVG), Order Blocks (OB), and Volatility features.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
import numpy as np
import pandas as pd


@dataclass
class VolumeProfile:
    vah: float          # Value Area High (~70% volume bound)
    val: float          # Value Area Low (~70% volume bound)
    poc: float          # Point of Control (highest volume price)
    vwap: float         # Volume-Weighted Average Price
    vwap_std: float     # Standard deviation of VWAP
    total_volume: float
    profile_bins: Dict[float, float] = field(default_factory=dict)


@dataclass
class SwingPoint:
    index: int
    timestamp: int
    price: float
    is_high: bool       # True for Swing High, False for Swing Low
    swept: bool = False


@dataclass
class FairValueGap:
    top: float
    bottom: float
    is_bullish: bool
    timestamp: int
    mitigated: bool = False


@dataclass
class MarketStructureState:
    trend: str                           # "BULLISH", "BEARISH", "NEUTRAL"
    last_swing_high: Optional[SwingPoint] = None
    last_swing_low: Optional[SwingPoint] = None
    recent_swings: List[SwingPoint] = field(default_factory=list)
    active_fvgs: List[FairValueGap] = field(default_factory=list)
    bullish_mss_confirmed: bool = False
    bearish_mss_confirmed: bool = False
    sweep_detected: Optional[str] = None # "BULLISH_SWEEP", "BEARISH_SWEEP", None


class FeatureEngine:
    """
    Computes mathematical and quantitative microstructure indicators on OHLCV data.
    """

    @staticmethod
    def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
        """Calculate Average True Range (ATR)."""
        if len(df) < 2:
            return pd.Series(0.0, index=df.index)

        high = df["high"]
        low = df["low"]
        close = df["close"]
        prev_close = close.shift(1)

        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return tr.rolling(window=period, min_periods=1).mean()

    @staticmethod
    def calculate_adx(df: pd.DataFrame, period: int = 14) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """
        Calculate Average Directional Index (ADX), +DI, -DI.
        """
        if len(df) < period + 1:
            zeros = pd.Series(0.0, index=df.index)
            return zeros, zeros, zeros

        high = df["high"]
        low = df["low"]
        close = df["close"]

        up_move = high - high.shift(1)
        down_move = low.shift(1) - low

        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

        tr = FeatureEngine.calculate_atr(df, period=1)
        atr_smooth = tr.rolling(window=period, min_periods=1).mean()

        plus_di = 100 * (pd.Series(plus_dm, index=df.index).rolling(period, min_periods=1).mean() / atr_smooth)
        minus_di = 100 * (pd.Series(minus_dm, index=df.index).rolling(period, min_periods=1).mean() / atr_smooth)

        dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di + 1e-9))
        adx = dx.rolling(window=period, min_periods=1).mean()
        return adx.fillna(0.0), plus_di.fillna(0.0), minus_di.fillna(0.0)

    @staticmethod
    def calculate_emas(df: pd.DataFrame, periods: Tuple[int, ...] = (9, 20, 50, 200)) -> Dict[int, pd.Series]:
        """Calculate Exponential Moving Averages."""
        emas = {}
        for p in periods:
            emas[p] = df["close"].ewm(span=p, adjust=False).mean()
        return emas

    @staticmethod
    def calculate_realized_volatility(df: pd.DataFrame, window: int = 20, annualization_factor: float = np.sqrt(365 * 288)) -> float:
        """
        Calculate annualized realized volatility based on 5-minute log returns.
        """
        if len(df) < window:
            return 0.0
        log_rets = np.log(df["close"] / df["close"].shift(1)).dropna()
        if len(log_rets) < 2:
            return 0.0
        vol = log_rets.iloc[-window:].std() * annualization_factor
        return float(vol) if not np.isnan(vol) else 0.0

    @staticmethod
    def calculate_volume_profile(
        df: pd.DataFrame,
        volume_fraction: float = 0.70,
        num_bins: int = 50,
    ) -> Optional[VolumeProfile]:
        """
        Calculate Auction Market Theory Volume Profile:
        POC (Point of Control), VAH (Value Area High), VAL (Value Area Low), and VWAP.
        Uses 70% of traded volume according to Section 11.
        """
        if len(df) < 5:
            return None

        closes = df["close"].values
        highs = df["high"].values
        lows = df["low"].values
        volumes = df["volume"].values

        total_vol = float(np.sum(volumes))
        if total_vol <= 0:
            # Fall back to equal-weighted ticks if volume is zero
            volumes = np.ones_like(closes)
            total_vol = float(np.sum(volumes))

        # VWAP calculation
        typical_price = (highs + lows + closes) / 3.0
        vwap = float(np.sum(typical_price * volumes) / total_vol)
        variance = float(np.sum(volumes * ((typical_price - vwap) ** 2)) / total_vol)
        vwap_std = float(np.sqrt(variance)) if variance > 0 else 0.0

        min_price = float(np.min(lows))
        max_price = float(np.max(highs))
        if min_price >= max_price:
            return VolumeProfile(
                vah=closes[-1],
                val=closes[-1],
                poc=closes[-1],
                vwap=vwap,
                vwap_std=vwap_std,
                total_volume=total_vol,
            )

        bin_edges = np.linspace(min_price, max_price, num_bins + 1)
        bin_vol = np.zeros(num_bins)

        # Distribute volume across bins
        for h, l, v in zip(highs, lows, volumes):
            if h <= l:
                idx = min(num_bins - 1, max(0, int((l - min_price) / (max_price - min_price) * num_bins)))
                bin_vol[idx] += v
            else:
                # Disperse candle volume evenly across the touched price range
                overlap_bins = np.where((bin_edges[1:] >= l) & (bin_edges[:-1] <= h))[0]
                if len(overlap_bins) > 0:
                    share = v / len(overlap_bins)
                    bin_vol[overlap_bins] += share

        poc_idx = int(np.argmax(bin_vol))
        poc = float((bin_edges[poc_idx] + bin_edges[poc_idx + 1]) / 2.0)

        # Value area expansion: expand outward from POC until target volume fraction is reached
        target_vol = total_vol * volume_fraction
        accumulated_vol = bin_vol[poc_idx]
        upper_idx = poc_idx
        lower_idx = poc_idx

        while accumulated_vol < target_vol and (upper_idx < num_bins - 1 or lower_idx > 0):
            next_upper_vol = bin_vol[upper_idx + 1] if upper_idx < num_bins - 1 else 0.0
            next_lower_vol = bin_vol[lower_idx - 1] if lower_idx > 0 else 0.0

            if next_upper_vol >= next_lower_vol and upper_idx < num_bins - 1:
                upper_idx += 1
                accumulated_vol += next_upper_vol
            elif lower_idx > 0:
                lower_idx -= 1
                accumulated_vol += next_lower_vol
            elif upper_idx < num_bins - 1:
                upper_idx += 1
                accumulated_vol += next_upper_vol
            else:
                break

        val = float((bin_edges[lower_idx] + bin_edges[lower_idx + 1]) / 2.0)
        vah = float((bin_edges[upper_idx] + bin_edges[upper_idx + 1]) / 2.0)

        bin_dict = {
            float((bin_edges[i] + bin_edges[i + 1]) / 2.0): float(bin_vol[i])
            for i in range(num_bins)
        }

        return VolumeProfile(
            vah=vah,
            val=val,
            poc=poc,
            vwap=vwap,
            vwap_std=vwap_std,
            total_volume=total_vol,
            profile_bins=bin_dict,
        )

    @staticmethod
    def detect_market_structure(
        df: pd.DataFrame,
        swing_lookback: int = 10,
        displacement_factor: float = 1.2,
    ) -> MarketStructureState:
        """
        Detect Swing Highs/Lows, Liquidity Sweeps, Displacement, MSS, and FVGs.
        """
        if len(df) < swing_lookback * 2 + 3:
            return MarketStructureState(trend="NEUTRAL")

        highs = df["high"].values
        lows = df["low"].values
        closes = df["close"].values
        opens = df["open"].values
        timestamps = df["timestamp"].values if "timestamp" in df else np.arange(len(df))

        atr = FeatureEngine.calculate_atr(df, period=14).values

        swings: List[SwingPoint] = []
        n = len(df)

        # Detect swing highs and lows
        for i in range(swing_lookback, n - 1):
            window_high = highs[i - swing_lookback : i + swing_lookback + 1]
            window_low = lows[i - swing_lookback : i + swing_lookback + 1]

            if highs[i] == np.max(window_high):
                swings.append(SwingPoint(index=i, timestamp=int(timestamps[i]), price=float(highs[i]), is_high=True))
            elif lows[i] == np.min(window_low):
                swings.append(SwingPoint(index=i, timestamp=int(timestamps[i]), price=float(lows[i]), is_high=False))

        # Recent swings
        swing_highs = [s for s in swings if s.is_high]
        swing_lows = [s for s in swings if not s.is_high]

        last_sh = swing_highs[-1] if swing_highs else None
        last_sl = swing_lows[-1] if swing_lows else None

        # Detect displacement on the most recent candles
        curr_body = abs(closes[-1] - opens[-1])
        curr_atr = atr[-1] if atr[-1] > 0 else 1.0
        is_displacement = curr_body > (displacement_factor * curr_atr)

        # Detect liquidity sweep on current/recent bar
        sweep_detected = None
        if last_sl and lows[-1] < last_sl.price and closes[-1] > last_sl.price:
            sweep_detected = "BULLISH_SWEEP"  # Low probed below prior swing low, but candle closed back above
        elif last_sh and highs[-1] > last_sh.price and closes[-1] < last_sh.price:
            sweep_detected = "BEARISH_SWEEP"  # High probed above prior swing high, but candle closed back below

        # Detect Market Structure Shift (MSS)
        bullish_mss = False
        bearish_mss = False
        if last_sh and closes[-1] > last_sh.price and is_displacement and closes[-1] > opens[-1]:
            bullish_mss = True
        if last_sl and closes[-1] < last_sl.price and is_displacement and closes[-1] < opens[-1]:
            bearish_mss = True

        # Detect Fair Value Gaps (FVG)
        fvgs: List[FairValueGap] = []
        for i in range(max(2, n - 30), n):
            # Bullish FVG: Low of candle i > High of candle i-2
            if lows[i] > highs[i - 2]:
                fvgs.append(
                    FairValueGap(
                        top=float(lows[i]),
                        bottom=float(highs[i - 2]),
                        is_bullish=True,
                        timestamp=int(timestamps[i]),
                    )
                )
            # Bearish FVG: High of candle i < Low of candle i-2
            elif highs[i] < lows[i - 2]:
                fvgs.append(
                    FairValueGap(
                        top=float(lows[i - 2]),
                        bottom=float(highs[i]),
                        is_bullish=False,
                        timestamp=int(timestamps[i]),
                    )
                )

        # Determine structural trend
        trend = "NEUTRAL"
        if len(swing_highs) >= 2 and len(swing_lows) >= 2:
            if swing_highs[-1].price > swing_highs[-2].price and swing_lows[-1].price > swing_lows[-2].price:
                trend = "BULLISH"
            elif swing_highs[-1].price < swing_highs[-2].price and swing_lows[-1].price < swing_lows[-2].price:
                trend = "BEARISH"

        return MarketStructureState(
            trend=trend,
            last_swing_high=last_sh,
            last_swing_low=last_sl,
            recent_swings=swings[-10:],
            active_fvgs=fvgs[-5:],
            bullish_mss_confirmed=bullish_mss,
            bearish_mss_confirmed=bearish_mss,
            sweep_detected=sweep_detected,
        )
