"""
Forensic Performance Remediation — Historical Competition Log Replay Utility
Compares BEFORE (actual competition exchange trades) vs AFTER (risk-governed execution).

Evaluates on the actual observed exchange trades:
- Trade count
- Turnover ($)
- Gross P&L ($)
- Fees ($)
- Net P&L ($)
- Win rate (%)
- Average trade ($)
- Average holding time
- Maximum drawdown ($ and %)
- Rapid re-entries (<10m)
- Rejections & reasons
"""

from __future__ import annotations

import csv
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

# Ensure root directory is on PYTHONPATH
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from config.trading_params import load_config, AppConfig
from core.risk_governor import RiskGovernor
from core.strategy_engine import Signal
from state.portfolio_tracker import PortfolioTracker


def parse_timestamp(ts_str: str) -> datetime:
    """Parses timestamp like '2026-10-04 19:31:56 UTC' or '2026-10-05 01:01:56 IST'."""
    ts_clean = ts_str.replace(" UTC", "").replace(" IST", "").strip()
    return datetime.strptime(ts_clean, "%Y-%m-%d %H:%M:%S")


class PositionTracker:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.qty: float = 0.0
        self.entry_price: float = 0.0
        self.entry_time: Optional[datetime] = None
        self.total_cost: float = 0.0

    def buy(self, qty: float, price: float, dt: datetime):
        new_cost = qty * price
        if self.qty > 0:
            self.entry_price = (self.total_cost + new_cost) / (self.qty + qty)
            self.qty += qty
            self.total_cost += new_cost
        else:
            self.qty = qty
            self.entry_price = price
            self.total_cost = new_cost
            self.entry_time = dt

    def sell(self, qty: float, price: float) -> tuple[float, float]:
        """Returns (realized_gross_pnl, sold_qty)."""
        sold_qty = min(self.qty, qty)
        if sold_qty <= 0:
            return 0.0, 0.0
        pnl = sold_qty * (price - self.entry_price)
        self.qty -= sold_qty
        self.total_cost -= sold_qty * self.entry_price
        if self.qty < 1e-8:
            self.qty = 0.0
            self.total_cost = 0.0
        return pnl, sold_qty


def run_baseline_replay(trades: list[dict]) -> dict[str, Any]:
    """Replay baseline actual competition trades without new governor."""
    positions: dict[str, PositionTracker] = {}
    round_trips = []
    total_turnover = 0.0
    total_fees = 0.0
    rapid_reentries = 0
    last_exit_times: dict[str, datetime] = {}

    initial_equity = 100000.0
    current_equity = initial_equity
    peak_equity = initial_equity
    max_drawdown = 0.0

    for t in trades:
        symbol = t["symbol"]
        side = t["side"]
        qty = float(t["filled_qty"])
        price = float(t["filled_price"])
        notional = float(t["notional_usd"])
        commission = float(t["commission"])
        dt = parse_timestamp(t["timestamp_utc"])

        total_turnover += notional
        total_fees += commission

        if symbol not in positions:
            positions[symbol] = PositionTracker(symbol)
        pos = positions[symbol]

        if side == "BUY":
            # Check rapid re-entry (<10m)
            if symbol in last_exit_times:
                gap = (dt - last_exit_times[symbol]).total_seconds()
                if gap < 600:
                    rapid_reentries += 1
            pos.buy(qty, price, dt)
        elif side == "SELL":
            entry_time = pos.entry_time or dt
            entry_px = pos.entry_price
            gross_pnl, sold_qty = pos.sell(qty, price)
            hold_sec = (dt - entry_time).total_seconds()
            round_trips.append({
                "symbol": symbol,
                "gross_pnl": gross_pnl,
                "hold_sec": hold_sec,
                "entry_price": entry_px,
                "exit_price": price,
                "qty": sold_qty,
            })
            last_exit_times[symbol] = dt
            current_equity += gross_pnl - commission
            peak_equity = max(peak_equity, current_equity)
            dd = peak_equity - current_equity
            if dd > max_drawdown:
                max_drawdown = dd

    total_gross_pnl = sum(rt["gross_pnl"] for rt in round_trips)
    net_pnl = total_gross_pnl - total_fees
    wins = [rt for rt in round_trips if rt["gross_pnl"] > 0]
    win_rate = (len(wins) / len(round_trips) * 100) if round_trips else 0.0
    avg_trade = (net_pnl / len(round_trips)) if round_trips else 0.0
    avg_hold_sec = (sum(rt["hold_sec"] for rt in round_trips) / len(round_trips)) if round_trips else 0.0
    closed_pos_count = sum(1 for p in positions.values() if p.qty < 1e-8)
    open_pos_count = sum(1 for p in positions.values() if p.qty > 1e-8)
    buy_orders_count = sum(1 for t in trades if t["side"] == "BUY")
    sell_orders_count = sum(1 for t in trades if t["side"] == "SELL")

    return {
        "trade_count": len(trades),
        "buy_orders": buy_orders_count,
        "sell_orders": sell_orders_count,
        "closed_positions": closed_pos_count,
        "open_positions": open_pos_count,
        "exit_tranches": len(round_trips),
        "turnover": total_turnover,
        "gross_pnl": total_gross_pnl,
        "fees": total_fees,
        "net_pnl": net_pnl,
        "win_rate": win_rate,
        "avg_trade": avg_trade,
        "avg_hold_minutes": avg_hold_sec / 60.0,
        "max_drawdown": max_drawdown,
        "max_drawdown_pct": (max_drawdown / initial_equity) * 100,
        "rapid_reentries": rapid_reentries,
        "rejections": 0,
        "rejection_reasons": {},
    }


def run_governed_replay(trades: list[dict], app_config: AppConfig) -> dict[str, Any]:
    """Replay trades governed by RiskGovernor rules."""
    governor = RiskGovernor(app_config.risk_governor, app_config.fees)

    # Temporary persistence for portfolio ledger
    tmp_persist = Path(tempfile.gettempdir()) / "replay_portfolio_state.json"
    if tmp_persist.exists():
        tmp_persist.unlink()

    portfolio = PortfolioTracker(
        initial_capital=100000.0,
        min_cash_reserve_pct=app_config.portfolio.min_cash_reserve_pct,
        persistence_file=str(tmp_persist),
    )

    round_trips = []
    total_turnover = 0.0
    total_fees = 0.0
    rejections = 0
    rejection_reasons: dict[str, int] = {}
    rapid_reentries = 0
    executed_trades_count = 0
    last_exit_times: dict[str, float] = {}
    entry_timestamps: dict[str, float] = {}

    initial_equity = portfolio.total_equity
    peak_equity = initial_equity
    max_drawdown = 0.0

    for t in trades:
        symbol = t["symbol"]
        side = t["side"]
        raw_qty = float(t["filled_qty"])
        price = float(t["filled_price"])
        notional = float(t["notional_usd"])
        dt = parse_timestamp(t["timestamp_utc"])
        now_ts = dt.timestamp()

        # Update mark prices for open positions
        portfolio.update_mark_prices({symbol: price})

        if side == "BUY":
            # Synthesize signal from observed entry
            stop_dist = price * 0.015  # 1.5% stop loss standard
            signal = Signal(
                strategy="Strategy_A",
                symbol=symbol,
                direction="BUY",
                confidence=0.80,
                entry_price=price,
                stop_loss=price - stop_dist,
                take_profit_1=price + (stop_dist * 1.5),
                take_profit_2=price + (stop_dist * 2.5),
                expected_rr=2.0,
                reason="Competition trade replay",
                regime="TRENDING_UP",
                timestamp=int(now_ts),
            )

            # Evaluate with Governor
            eval_res = governor.evaluate_signal(
                signal=signal,
                portfolio=portfolio,
                curr_time=now_ts,
                enforce_drawdown=True,
            )

            if not eval_res.approved:
                rejections += 1
                reason = eval_res.rejection_reason or "UNKNOWN"
                rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
                continue

            # Accepted: check rapid re-entry
            if symbol in last_exit_times:
                gap = now_ts - last_exit_times[symbol]
                if gap < 600:
                    rapid_reentries += 1

            # Dynamic sizing: cap at governed maximum notional
            target_notional = min(notional, eval_res.target_notional)
            final_qty = target_notional / price
            commission = target_notional * 0.0010  # 0.10% fee

            portfolio.record_fill(
                symbol=symbol,
                side="BUY",
                quantity=final_qty,
                price=price,
                fee=commission,
                stop_loss=price - stop_dist,
                take_profit_1=signal.take_profit_1,
                take_profit_2=signal.take_profit_2,
            )

            entry_timestamps[symbol] = now_ts
            governor.on_trade_entry(symbol, price, now_ts)
            total_turnover += target_notional
            total_fees += commission
            executed_trades_count += 1

        elif side == "SELL":
            pos = portfolio.positions.get(symbol)
            if not pos or pos.quantity <= 1e-7:
                # Corresponding entry was rejected by governor
                continue

            exit_qty = pos.quantity  # Full exit
            exit_notional = exit_qty * price
            commission = exit_notional * 0.0010

            entry_px = pos.entry_price
            gross_pnl = exit_qty * (price - entry_px)
            net_pnl = gross_pnl - commission
            entry_ts = entry_timestamps.get(symbol, now_ts)
            hold_sec = max(0.0, now_ts - entry_ts)

            # Record exit in portfolio and governor
            portfolio.record_fill(
                symbol=symbol,
                side="SELL",
                quantity=exit_qty,
                price=price,
                fee=commission,
            )

            governor.on_trade_exit(
                symbol=symbol,
                side="SELL",
                price=price,
                quantity=exit_qty,
                realized_pnl=net_pnl,
                fee=commission,
                hold_duration=hold_sec,
                exit_reason="STRATEGY_EXIT",
                timestamp=now_ts,
            )

            last_exit_times[symbol] = now_ts

            round_trips.append({
                "symbol": symbol,
                "gross_pnl": gross_pnl,
                "net_pnl": net_pnl,
                "hold_sec": hold_sec,
                "entry_price": entry_px,
                "exit_price": price,
                "qty": exit_qty,
            })

            total_turnover += exit_notional
            total_fees += commission
            executed_trades_count += 1

            eq = portfolio.total_equity
            peak_equity = max(peak_equity, eq)
            dd = peak_equity - eq
            if dd > max_drawdown:
                max_drawdown = dd

    total_gross_pnl = sum(rt["gross_pnl"] for rt in round_trips)
    net_pnl = total_gross_pnl - total_fees
    wins = [rt for rt in round_trips if rt["gross_pnl"] > 0]
    win_rate = (len(wins) / len(round_trips) * 100) if round_trips else 0.0
    avg_trade = (net_pnl / len(round_trips)) if round_trips else 0.0
    avg_hold_sec = (sum(rt["hold_sec"] for rt in round_trips) / len(round_trips)) if round_trips else 0.0
    closed_pos_count = len(round_trips)
    open_pos_count = max(0, (executed_trades_count - len(round_trips)) - len(round_trips))

    return {
        "trade_count": executed_trades_count,
        "buy_orders": executed_trades_count - len(round_trips),
        "sell_orders": len(round_trips),
        "closed_positions": closed_pos_count,
        "open_positions": open_pos_count,
        "exit_tranches": len(round_trips),
        "turnover": total_turnover,
        "gross_pnl": total_gross_pnl,
        "fees": total_fees,
        "net_pnl": net_pnl,
        "win_rate": win_rate,
        "avg_trade": avg_trade,
        "avg_hold_minutes": avg_hold_sec / 60.0,
        "max_drawdown": max_drawdown,
        "max_drawdown_pct": (max_drawdown / initial_equity) * 100,
        "rapid_reentries": rapid_reentries,
        "rejections": rejections,
        "rejection_reasons": rejection_reasons,
    }


def main():
    log_file = ROOT_DIR / "logs" / "trade_log.csv"
    if not log_file.exists():
        print(f"Error: Trade log not found at {log_file}")
        return

    # Load exchange trades
    exchange_trades = []
    with open(log_file, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("event") == "EXCHANGE_TRADE_SYNCED":
                exchange_trades.append(row)

    print("=" * 80)
    print("BTCETH COMPETITION BOT — FORENSIC PERFORMANCE REPLAY")
    print(f"Loaded {len(exchange_trades)} exchange trade records from logs/trade_log.csv")
    print("=" * 80)

    # 1. Baseline Replay
    before = run_baseline_replay(exchange_trades)

    # 2. Governed Replay
    app_config = load_config(str(ROOT_DIR / "config" / "config.yaml"))
    after = run_governed_replay(exchange_trades, app_config)

    # Output Comparison Table
    print("\n" + "=" * 80)
    print(f"{'METRIC':<32} | {'BEFORE (UNGOVERNED)':<20} | {'AFTER (GOVERNED)':<20}")
    print("-" * 80)
    print(f"{'Total Orders Executed':<32} | {before['trade_count']:<20} | {after['trade_count']:<20}")
    print(f"{'  • BUY Entry Orders':<32} | {before['buy_orders']:<20} | {after['buy_orders']:<20}")
    print(f"{'  • SELL Exit Orders':<32} | {before['sell_orders']:<20} | {after['sell_orders']:<20}")
    print(f"{'Full Position Cycles Closed':<32} | {before['closed_positions']:<20} | {after['closed_positions']:<20}")
    print(f"{'Exit Tranches Completed':<32} | {before['exit_tranches']:<20} | {after['exit_tranches']:<20}")
    print(f"{'Open Positions at End':<32} | {before['open_positions']:<20} | {after['open_positions']:<20}")
    print(f"{'Total Turnover ($)':<32} | ${before['turnover']:<19,.2f} | ${after['turnover']:<19,.2f}")
    print(f"{'Gross P&L ($)':<32} | ${before['gross_pnl']:<19,.2f} | ${after['gross_pnl']:<19,.2f}")
    print(f"{'Fees Paid ($)':<32} | ${before['fees']:<19,.2f} | ${after['fees']:<19,.2f}")
    print(f"{'Net P&L ($)':<32} | ${before['net_pnl']:<19,.2f} | ${after['net_pnl']:<19,.2f}")
    print(f"{'Win Rate (%)':<32} | {before['win_rate']:<19.1f}% | {after['win_rate']:<19.1f}%")
    print(f"{'Avg Trade Net P&L ($)':<32} | ${before['avg_trade']:<19,.2f} | ${after['avg_trade']:<19,.2f}")
    print(f"{'Avg Holding Time (min)':<32} | {before['avg_hold_minutes']:<19.1f}m | {after['avg_hold_minutes']:<19.1f}m")
    print(f"{'Max Equity Drawdown ($)':<32} | ${before['max_drawdown']:<19,.2f} | ${after['max_drawdown']:<19,.2f}")
    print(f"{'Max Drawdown (%)':<32} | {before['max_drawdown_pct']:<19.2f}% | {after['max_drawdown_pct']:<19.2f}%")
    print(f"{'Rapid Re-entries (<10m)':<32} | {before['rapid_reentries']:<20} | {after['rapid_reentries']:<20}")
    print(f"{'Entries Rejected':<32} | {before['rejections']:<20} | {after['rejections']:<20}")
    print("=" * 80)

    print("\nREJECTION REASONS IN AFTER (GOVERNED):")
    for reason, count in sorted(after["rejection_reasons"].items(), key=lambda x: -x[1]):
        print(f"  • {reason:<32}: {count} orders blocked")
    print("=" * 80)


if __name__ == "__main__":
    main()
