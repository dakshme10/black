# QUANTITATIVE STRATEGY SPECIFICATION

This document details the quantitative rationale, entry rules, exit targets, stop placement, risk-reward criteria, and spot-only de-risking mechanics for the three strategy modules implemented in the bot.

---

## 1. Strategy A — Volume Profile / Auction Market Theory (AMT)

### 1.1 Quantitative Rationale
Auction Market Theory posits that financial markets exist to facilitate trade between buyers and sellers. When price trades within an accepted range, approximately 70% of traded volume accumulates within the **Value Area** bounded by:
- **VAH (Value Area High)**: The upper price boundary of the 70% volume distribution.
- **VAL (Value Area Low)**: The lower price boundary of the 70% volume distribution.
- **POC (Point of Control)**: The price level with the single highest traded volume.

When price breaks below VAL and fails to attract new selling volume, it creates a **failed auction**. Sellers are exhausted, passive buyers absorb liquidity, and price reclaims VAL to re-auction back through the volume node toward POC and VAH.

### 1.2 Long Entry Rules (Failed Auction / Value Reclaim)
1. **Regime Alignment**: Favored in `RANGE` and mild consolidation. Suppressed in aggressive downtrending `TREND` regimes.
2. **Breakdown Probe**: Within the last 5 bars, low traded below VAL:
   $$\min(Low_{t-5:t}) < VAL$$
3. **Reclaim Confirmation**: Current candle closes firmly above VAL:
   $$Close_t > VAL \quad \text{and} \quad Close_{t-1} \le VAL \times 1.002$$
4. **Current Price Location**: Price must be below POC to ensure favorable reward-to-risk:
   $$Close_t < POC$$
5. **Stop Loss**: Placed below the lowest sweep wick minus an ATR buffer:
   $$StopLoss = \min(Low_{t-5:t}) - (0.50 \times ATR_{14})$$
6. **Take Profit Targets**:
   - **TP1 (50% scale out)**: Set at Point of Control ($POC$).
   - **TP2 (50% scale out)**: Set at Value Area High ($VAH$).
7. **Risk-Reward Threshold**:
   $$\text{Expected } R:R = \frac{0.50 \times (POC - Entry) + 0.50 \times (VAH - Entry)}{Entry - StopLoss} \ge 1.5$$
   $$\text{Net Expected Edge} = \frac{ExpectedReward}{Entry} - (TakerFee + Slippage) > 0.30\%$$

### 1.3 Short-Side De-risking (Spot Only)
Under spot-market competition rules, naked shorting is strictly prohibited. When price probes above VAH and rejects:
$$\max(High_{t-3:t}) > VAH \quad \text{and} \quad Close_t < VAH$$
The strategy issues a `DE_RISK` signal to liquidate or reduce existing spot holdings back to cash.

---

## 2. Strategy B — Liquidity Sweep + Market Structure Shift (MSS)

### 2.1 Quantitative Rationale
Institutions frequently trigger retail stop-loss clusters resting above swing highs and below swing lows before engineering large structural reversals. By identifying stop runs followed by aggressive displacement candles, the bot enters structural market structure shifts (MSS) with asymmetric risk-reward.

### 2.2 Long Entry Rules (Bullish Sweep + MSS)
1. **Swing Low Identification**: Identify key pivot lows where:
   $$Low_i = \min(Low_{i-k : i+k}) \quad (k = \text{swing lookback})$$
2. **Liquidity Sweep**: Current bar probes below a prominent prior swing low but closes back above it:
   $$Low_t < SwingLow \quad \text{and} \quad Close_t > SwingLow$$
3. **Displacement Confirmation**: Candle body demonstrates expansion:
   $$|Close_t - Open_t| > 1.20 \times ATR_{14}$$
4. **Market Structure Shift (MSS)**: Price closes above the most recent lower-high swing point:
   $$Close_t > LastLowerHigh$$
5. **Stop Loss**: Placed below the absolute sweep low minus a quarter ATR buffer:
   $$StopLoss = SweepLow - (0.25 \times ATR_{14})$$
6. **Take Profit Targets**:
   - **TP1**: Fixed at $+2.0R$ ($Entry + 2.0 \times (Entry - StopLoss)$). At TP1, 50% is closed and stop ratchets to breakeven.
   - **TP2**: Opposing major structural liquidity pool ($LastSwingHigh$).
7. **Risk-Reward Threshold**: Expected $R:R \ge 2.0$ with net edge $> 0.40\%$.

### 2.3 Bearish Sweep De-risking (Spot Only)
When price wicks above a major swing high and confirms bearish MSS:
$$High_t > SwingHigh \quad \text{and} \quad Close_t < LastHigherLow$$
A `DE_RISK` signal is generated, exiting spot exposure to cash.

---

## 3. Strategy C — CVD / OI Absorption

### 3.1 Quantitative Rationale & Principle 3 Compliance
Cumulative Volume Delta (CVD) measures the cumulative net difference between market buy volume and market sell volume. When aggressive market sellers dump large volume (sharp drop in CVD) but price forms a higher low or refuses to break down, it indicates **passive limit bid absorption**.

**Compliance Rule (Principle 3 — No Hallucinated Data)**:
Roostoo's public mock exchange provides spot market data (`LastPrice`, `MaxBid`, `MinAsk`, `CoinTradeValue`, `UnitTradeValue`) but **does not** provide perpetual open interest (OI) or tick-level delta.
- The strategy explicitly checks feature availability (`cvd_available` and `oi_available`).
- When unavailable in live trading, the strategy **safely degrades** and outputs `NO_TRADE` with diagnostic metadata:
  ```json
  {"missing_features": ["CVD", "OI"], "reason": "CVD feature unavailable from exchange market data. Safe degradation active."}
  ```
- No mock or simulated delta values are ever fabricated during live trading.

### 3.2 Long Entry Rules (When Delta Feed Available)
1. **Selling Absorption Divergence**: Price forms a double bottom or higher low while CVD makes a new swing low over a 5-bar window:
   $$Price_t \ge \min(Price_{t-5:t}) \quad \text{and} \quad CVD_t < \min(CVD_{t-5:t})$$
2. **Delta Turn**: The current candle prints positive net delta:
   $$\Delta_t > 0$$
3. **EMA Reclaim**: Price closes above the 20-period Exponential Moving Average:
   $$Close_t > EMA_{20}$$
4. **Stop Loss**: Placed below the absorption base low:
   $$StopLoss = AbsorptionBaseLow - (0.30 \times ATR_{14})$$
5. **Take Profit Targets**:
   - **TP1**: Nearest resistance / VWAP level ($+2.0R$).
   - **TP2**: Trailing stop ratchet ($+3.5R$).

---

## 4. Signal Normalization & Aggregation

When candidate signals are produced across strategies:
1. **Regime Weighting**: Signals aligned with the active regime receive confidence boosts ($+0.05$). Counter-trend signals in strong trends are heavily penalised (confidence $\times 0.60$).
2. **Correlation & Conflicts**: If a `BUY` and `DE_RISK` signal occur simultaneously, `DE_RISK` takes absolute priority to preserve capital.
3. **Net Edge Filter**: Any signal with net expected reward after transaction costs ($TakerFee + Slippage$) less than $0.30\%$ is rejected.
4. **Single Signal Output**: Only the highest confidence actionable signal is forwarded to the Risk Manager.
