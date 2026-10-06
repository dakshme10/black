#!/usr/bin/env python3
"""
Synchronizes official matched orders from Roostoo Mock Exchange into local logs/trade_log.csv.
Ensures the local repository copy reflects all live competition executions from Oct 4-6.
"""

from __future__ import annotations

import csv
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv

# Ensure root directory is on sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from core.api_client import RoostooClient


def sync_trade_log():
    load_dotenv(ROOT_DIR / ".env")
    api_key = os.getenv("ROOSTOO_API_KEY", "")
    secret_key = os.getenv("ROOSTOO_SECRET_KEY", "")

    if not api_key or not secret_key:
        print("[-] Error: ROOSTOO_API_KEY or ROOSTOO_SECRET_KEY missing from .env")
        return

    client = RoostooClient(api_key=api_key, secret_key=secret_key)
    print("[+] Connecting to Roostoo Mock Exchange...")

    # Fetch all matched orders from exchange
    resp = client.query_order(pending_only=False, limit=200)
    if not resp.get("Success", False):
        print(f"[-] Failed to fetch orders from exchange: {resp.get('ErrMsg', 'Unknown error')}")
        return

    exchange_orders = resp.get("OrderMatched", [])
    print(f"[+] Retrieved {len(exchange_orders)} authoritative executed orders from exchange.")

    trade_log_path = ROOT_DIR / "logs" / "trade_log.csv"
    trade_log_path.parent.mkdir(parents=True, exist_ok=True)

    header = [
        "timestamp_ist",
        "timestamp_utc",
        "event",
        "symbol",
        "side",
        "order_type",
        "quantity",
        "price",
        "filled_qty",
        "filled_price",
        "notional_usd",
        "commission",
        "client_order_id",
        "exchange_order_id",
        "status",
    ]

    existing_exchange_ids = set()
    existing_rows = []

    if trade_log_path.exists() and trade_log_path.stat().st_size > 0:
        with open(trade_log_path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            first_row = True
            for row in reader:
                if first_row:
                    first_row = False
                    continue
                if not row:
                    continue
                existing_rows.append(row)
                if len(row) > 13 and row[13]:
                    existing_exchange_ids.add(str(row[13]).strip())

    ist = timezone(timedelta(hours=5, minutes=30))
    new_rows = []

    # Sort chronological by CreateTimestamp ascending
    sorted_orders = sorted(exchange_orders, key=lambda x: x.get("CreateTimestamp", 0))

    for o in sorted_orders:
        ex_id = str(o.get("OrderID", "")).strip()
        if not ex_id or ex_id in existing_exchange_ids:
            continue

        ts_ms = o.get("CreateTimestamp", 0)
        dt_utc = datetime.fromtimestamp(ts_ms / 1000.0, timezone.utc) if ts_ms > 0 else datetime.now(timezone.utc)
        dt_ist = dt_utc.astimezone(ist)

        ts_ist_str = dt_ist.strftime("%Y-%m-%d %H:%M:%S IST")
        ts_utc_str = dt_utc.strftime("%Y-%m-%d %H:%M:%S UTC")

        qty = float(o.get("Quantity", 0.0) or 0.0)
        filled_qty = float(o.get("FilledQuantity", 0.0) or qty)
        filled_px = float(o.get("FilledAverPrice", 0.0) or o.get("Price", 0.0) or 0.0)
        req_px = float(o.get("Price", 0.0) or filled_px)
        notional = round(filled_px * filled_qty, 4)
        comm = float(o.get("CommissionChargeValue", 0.0) or 0.0)

        row = [
            ts_ist_str,
            ts_utc_str,
            "EXCHANGE_TRADE_SYNCED",
            o.get("Pair", ""),
            o.get("Side", "").upper(),
            o.get("Type", "MARKET").upper(),
            str(qty),
            str(req_px),
            str(filled_qty),
            str(filled_px),
            str(notional),
            str(comm),
            f"EX_{ex_id}",
            ex_id,
            o.get("Status", "FILLED").upper(),
        ]
        new_rows.append(row)
        existing_exchange_ids.add(ex_id)

    # Write back standardized CSV with all historical and newly synced rows
    with open(trade_log_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for r in existing_rows:
            writer.writerow(r)
        for r in new_rows:
            writer.writerow(r)

    print(f"[SUCCESS] Appended {len(new_rows)} new exchange trades to {trade_log_path}.")
    print(f"[+] Total trades in local trade_log.csv: {len(existing_rows) + len(new_rows)}")

    # Show preview of latest trades
    print("\n--- Latest 10 Competition Trades Synced ---")
    for r in (existing_rows + new_rows)[-10:]:
        print(f"[{r[0]}] {r[4]} {r[3]} | Qty: {r[8]} @ ${r[9]} (Notional: ${r[10]}) | ID: {r[13]} | {r[14]}")


if __name__ == "__main__":
    sync_trade_log()
