"""
Production Audit Logger for Roostoo Autonomous Trading Bot.
Provides append-oriented structured JSON logging with cryptographic SHA-256 hash chaining
to guarantee audit trail integrity, verification, and tamper detection.
"""

from __future__ import annotations

from collections import deque
import csv
import hashlib
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class AuditLogger:
    """
    Append-only structured audit logger with sequence numbering and SHA-256 hash chaining.
    Also maintains a dedicated CSV trade execution log (trade_log.csv) and in-memory event stream.
    """

    def __init__(
        self,
        audit_file: str = "logs/audit_trail.jsonl",
        api_log_file: str = "logs/api_requests.jsonl",
        trade_log_file: str = "logs/trade_log.csv",
        enable_hash_chain: bool = True,
        console_log_level: int = logging.INFO,
    ):
        self.audit_file = Path(audit_file)
        self.api_log_file = Path(api_log_file)
        self.trade_log_file = Path(trade_log_file)
        self.enable_hash_chain = enable_hash_chain
        self._lock = threading.Lock()
        self._recent_events: deque = deque(maxlen=200)

        # Ensure directories exist
        self.audit_file.parent.mkdir(parents=True, exist_ok=True)
        self.api_log_file.parent.mkdir(parents=True, exist_ok=True)
        self.trade_log_file.parent.mkdir(parents=True, exist_ok=True)
        self._init_trade_csv()

        # Setup standard Python logger for console output
        self._console_logger = logging.getLogger("RoostooAudit")
        self._console_logger.setLevel(console_log_level)
        if not self._console_logger.handlers:
            ch = logging.StreamHandler(sys.stdout)
            ch.setLevel(console_log_level)
            formatter = logging.Formatter(
                fmt="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
            ch.setFormatter(formatter)
            self._console_logger.addHandler(ch)

        # State tracking for hash chain
        self._seq = 0
        self._last_hash = "0" * 64
        self._init_hash_chain()

    def _init_hash_chain(self) -> None:
        """Scan existing audit file to resume sequence number and previous hash."""
        if not self.audit_file.exists():
            return

        try:
            with open(self.audit_file, "r", encoding="utf-8") as f:
                last_line = ""
                count = 0
                for line in f:
                    line = line.strip()
                    if line:
                        last_line = line
                        count += 1
                if last_line:
                    data = json.loads(last_line)
                    self._seq = data.get("seq", count)
                    self._last_hash = data.get("curr_hash", "0" * 64)
        except Exception as e:
            self._console_logger.warning(f"Could not read existing audit trail for hash chain resume: {e}")
            self._seq = 0
            self._last_hash = "0" * 64

    def _init_trade_csv(self) -> None:
        """Initialize trade_log.csv with standardized header and backfill historical fills from audit trail."""
        needs_header = not self.trade_log_file.exists() or self.trade_log_file.stat().st_size == 0
        if needs_header:
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
            try:
                with open(self.trade_log_file, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(header)

                # Backfill historical fills from audit trail if available
                if self.audit_file.exists():
                    ist = timezone(timedelta(hours=5, minutes=30))
                    with open(self.audit_file, "r", encoding="utf-8") as af, open(self.trade_log_file, "a", newline="", encoding="utf-8") as tf:
                        writer = csv.writer(tf)
                        for line in af:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                rec = json.loads(line)
                                if rec.get("record_type") == "ORDER_EVENT":
                                    filled_qty = float(rec.get("filled_qty", 0.0) or 0.0)
                                    status = str(rec.get("status", ""))
                                    event = str(rec.get("event", ""))
                                    if filled_qty > 0 or status in ("FILLED", "PARTIALLY_FILLED") or "FILL" in event:
                                        ts_iso = rec.get("timestamp_iso", "")
                                        dt = datetime.fromisoformat(ts_iso) if ts_iso else datetime.now(timezone.utc)
                                        ts_ist = dt.astimezone(ist).strftime("%Y-%m-%d %H:%M:%S IST")
                                        ts_utc = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
                                        px = float(rec.get("filled_price", 0.0) or rec.get("price", 0.0) or 0.0)
                                        qty = filled_qty if filled_qty > 0 else float(rec.get("quantity", 0.0) or 0.0)
                                        notional = round(px * qty, 4)
                                        writer.writerow([
                                            ts_ist,
                                            ts_utc,
                                            event,
                                            rec.get("symbol", ""),
                                            rec.get("side", ""),
                                            rec.get("order_type", ""),
                                            rec.get("quantity", ""),
                                            rec.get("price", ""),
                                            filled_qty,
                                            px,
                                            notional,
                                            rec.get("commission", 0.0),
                                            rec.get("client_order_id", ""),
                                            rec.get("exchange_order_id", ""),
                                            status,
                                        ])
                            except Exception:
                                continue
            except Exception as e:
                self._console_logger.error(f"Error initializing trade_log.csv: {e}")

    def _append_trade_csv(
        self,
        event: str,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: Optional[float],
        filled_qty: float,
        filled_price: float,
        commission: float,
        client_order_id: str,
        exchange_order_id: Optional[int],
        status: str,
    ) -> None:
        """Append executed trade or fill event to trade_log.csv with IST timestamp."""
        try:
            ist = timezone(timedelta(hours=5, minutes=30))
            now = datetime.now(timezone.utc)
            now_ist_str = now.astimezone(ist).strftime("%Y-%m-%d %H:%M:%S IST")
            now_utc_str = now.strftime("%Y-%m-%d %H:%M:%S UTC")
            px = filled_price if filled_price > 0 else (price or 0.0)
            qty = filled_qty if filled_qty > 0 else quantity
            notional = round(px * qty, 4)

            row = [
                now_ist_str,
                now_utc_str,
                event,
                symbol,
                side,
                order_type,
                str(quantity),
                str(price or 0.0),
                str(filled_qty),
                str(filled_price),
                str(notional),
                str(commission),
                client_order_id,
                str(exchange_order_id or ""),
                status,
            ]
            with open(self.trade_log_file, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(row)
        except Exception as e:
            self._console_logger.error(f"Failed to append to trade_log.csv: {e}")

    def _compute_hash(self, record: Dict[str, Any]) -> str:
        """Compute SHA-256 hash of a canonicalized JSON string without curr_hash."""
        record_copy = dict(record)
        record_copy.pop("curr_hash", None)
        canonical = json.dumps(record_copy, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _sanitize(self, data: Any) -> Any:
        """Recursively redact secrets, keys, and tokens from logging."""
        sensitive_keys = {"secret", "secretkey", "api_key", "rst-api-key", "msg-signature", "password", "token"}
        if isinstance(data, dict):
            clean = {}
            for k, v in data.items():
                if str(k).lower().replace("_", "").replace("-", "") in sensitive_keys:
                    clean[k] = "[REDACTED]"
                else:
                    clean[k] = self._sanitize(v)
            return clean
        elif isinstance(data, list):
            return [self._sanitize(item) for item in data]
        return data

    def _write_audit_record(self, record_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Atomically append a structured record to the audit trail with hash chaining."""
        with self._lock:
            self._seq += 1
            now_iso = datetime.now(timezone.utc).isoformat()
            now_ms = int(time.time() * 1000)

            sanitized_payload = self._sanitize(payload)

            record: Dict[str, Any] = {
                "seq": self._seq,
                "timestamp_iso": now_iso,
                "timestamp_ms": now_ms,
                "record_type": record_type,
                "prev_hash": self._last_hash,
                **sanitized_payload,
            }

            if self.enable_hash_chain:
                curr_hash = self._compute_hash(record)
                record["curr_hash"] = curr_hash
                self._last_hash = curr_hash

            with open(self.audit_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")

            # Store recent event in ring buffer for real-time console streaming
            ist = timezone(timedelta(hours=5, minutes=30))
            now_ist = datetime.now(timezone.utc).astimezone(ist)
            entry = {
                "seq": self._seq,
                "timestamp_iso": now_iso,
                "timestamp_ist": now_ist.strftime("%Y-%m-%d %H:%M:%S IST"),
                "record_type": record_type,
                "event": sanitized_payload.get("event") or sanitized_payload.get("event_type") or record_type,
                "message": self._format_summary_message(record_type, sanitized_payload),
                "data": sanitized_payload,
            }
            self._recent_events.append(entry)

            return record

    def _format_summary_message(self, record_type: str, data: Dict[str, Any]) -> str:
        """Create a compact, human-readable summary for console stream."""
        if record_type == "ORDER_EVENT":
            ev = data.get("event", "")
            side = data.get("side", "")
            sym = data.get("symbol", "")
            qty = data.get("quantity", 0)
            px = data.get("price", 0)
            f_qty = data.get("filled_qty", 0)
            f_px = data.get("filled_price", 0)
            stat = data.get("status", "")
            oid = data.get("exchange_order_id") or data.get("client_order_id", "")
            if f_qty > 0 or stat == "FILLED":
                return f"{ev} [{side} {sym}]: filled {f_qty} @ ${f_px:,.2f} | status={stat} (id={oid})"
            return f"{ev} [{side} {sym}]: qty={qty} px=${px:,.2f} | status={stat} (id={oid})"
        elif record_type == "DECISION":
            return f"[{data.get('action', '')} {data.get('symbol', '')}] Strategy={data.get('strategy', '')} size={data.get('size', 0)} px=${data.get('price', 0):,.2f} | {data.get('reason', '')}"
        elif record_type == "CIRCUIT_BREAKER":
            return f"[{data.get('breaker_type', '')}] {data.get('reason', '')} -> Action={data.get('action', '')}"
        elif record_type == "RECONCILIATION":
            return f"Exchange Reconciled: Synced={data.get('is_synchronized', False)} CashDiff=${data.get('cash_discrepancy', 0):.2f} Actions={len(data.get('actions_taken', []))}"
        elif record_type == "SYSTEM_EVENT":
            return f"[{data.get('event_type', '')}] {data.get('message', '')}"
        return str(data)

    def get_recent_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Retrieve recent events from in-memory ring buffer (newest first)."""
        with self._lock:
            items = list(self._recent_events)
            return items[-limit:]

    def log_decision(
        self,
        strategy: str,
        symbol: str,
        action: str,
        signal_id: str,
        price: float,
        size: float,
        notional: float,
        stop_loss: float,
        take_profit_1: float,
        take_profit_2: float,
        confidence: float,
        expected_rr: float,
        risk_percent: float,
        reason: str,
        regime: str,
        portfolio_equity: float,
        cash_before: float,
        api_request_id: str = "",
        exchange_order_id: str = "",
        status: str = "GENERATED",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Record a strategy / risk trading decision adhering to Section 26.
        """
        payload = {
            "strategy": strategy,
            "symbol": symbol,
            "action": action,
            "signal_id": signal_id,
            "price": round(price, 6),
            "size": round(size, 8),
            "notional": round(notional, 2),
            "stop_loss": round(stop_loss, 6),
            "take_profit_1": round(take_profit_1, 6),
            "take_profit_2": round(take_profit_2, 6),
            "confidence": round(confidence, 4),
            "expected_rr": round(expected_rr, 2),
            "risk_percent": round(risk_percent, 4),
            "reason": reason,
            "regime": regime,
            "portfolio_equity": round(portfolio_equity, 2),
            "cash_before": round(cash_before, 2),
            "api_request_id": api_request_id,
            "exchange_order_id": exchange_order_id,
            "status": status,
        }
        if metadata:
            payload["metadata"] = metadata

        rec = self._write_audit_record("STRATEGY_DECISION", payload)
        self._console_logger.info(
            f"DECISION [{action}] {symbol} via {strategy} (conf={confidence:.2f}, status={status}) - {reason}"
        )
        return rec

    def log_order_event(
        self,
        event: str,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: Optional[float] = None,
        client_order_id: str = "",
        exchange_order_id: Optional[int] = None,
        role: str = "",
        status: str = "",
        filled_qty: float = 0.0,
        filled_price: float = 0.0,
        commission: float = 0.0,
        commission_coin: str = "USD",
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Log order placement, fill, rejection, or cancellation."""
        payload = {
            "event": event,
            "symbol": symbol,
            "side": side,
            "order_type": order_type,
            "quantity": quantity,
            "price": price,
            "client_order_id": client_order_id,
            "exchange_order_id": exchange_order_id,
            "role": role,
            "status": status,
            "filled_qty": filled_qty,
            "filled_price": filled_price,
            "commission": commission,
            "commission_coin": commission_coin,
        }
        if details:
            payload["details"] = details

        rec = self._write_audit_record("ORDER_EVENT", payload)
        self._console_logger.info(
            f"ORDER {event} [{side} {symbol}] qty={quantity} px={price} "
            f"status={status} id={exchange_order_id or client_order_id}"
        )

        # Record fill / execution to trade_log.csv
        if filled_qty > 0 or status in ("FILLED", "PARTIALLY_FILLED") or "FILL" in event:
            self._append_trade_csv(
                event=event,
                symbol=symbol,
                side=side,
                order_type=order_type,
                quantity=quantity,
                price=price,
                filled_qty=filled_qty,
                filled_price=filled_price,
                commission=commission,
                client_order_id=client_order_id,
                exchange_order_id=exchange_order_id,
                status=status,
            )

        return rec

    def log_api_request(
        self,
        method: str,
        endpoint: str,
        status_code: int,
        elapsed_ms: float,
        request_id: str,
        retries: int = 0,
        rate_limit_delay_ms: float = 0.0,
        error: str = "",
    ) -> None:
        """Log HTTP API interactions in api_requests.jsonl."""
        record = {
            "timestamp_iso": datetime.now(timezone.utc).isoformat(),
            "timestamp_ms": int(time.time() * 1000),
            "method": method,
            "endpoint": endpoint,
            "status_code": status_code,
            "elapsed_ms": round(elapsed_ms, 2),
            "request_id": request_id,
            "retries": retries,
            "rate_limit_delay_ms": round(rate_limit_delay_ms, 2),
            "error": error,
        }
        with self._lock:
            with open(self.api_log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")

    def log_circuit_breaker(
        self,
        breaker_type: str,
        current_drawdown: float,
        threshold: float,
        action: str,
        freeze_until: Optional[str] = None,
        reason: str = "",
    ) -> Dict[str, Any]:
        """Log circuit breaker trigger event."""
        payload = {
            "breaker_type": breaker_type,
            "current_drawdown_pct": round(current_drawdown * 100, 2),
            "threshold_pct": round(threshold * 100, 2),
            "action": action,
            "freeze_until": freeze_until,
            "reason": reason,
        }
        rec = self._write_audit_record("CIRCUIT_BREAKER", payload)
        self._console_logger.critical(
            f"CIRCUIT BREAKER TRIGGERED [{breaker_type}] Drawdown={current_drawdown*100:.2f}% "
            f"(threshold={threshold*100:.2f}%) Action: {action} - {reason}"
        )
        return rec

    def log_reconciliation(
        self,
        status: str,
        local_cash: float,
        exchange_cash: float,
        discrepancies: Dict[str, Any],
        action_taken: str = "",
    ) -> Dict[str, Any]:
        """Log portfolio state reconciliation against exchange truth."""
        payload = {
            "reconciliation_status": status,
            "local_cash": round(local_cash, 2),
            "exchange_cash": round(exchange_cash, 2),
            "discrepancies": discrepancies,
            "action_taken": action_taken,
        }
        rec = self._write_audit_record("PORTFOLIO_RECONCILIATION", payload)
        self._console_logger.info(
            f"RECONCILIATION [{status}] Local Cash=${local_cash:,.2f} Ex Cash=${exchange_cash:,.2f} "
            f"Action={action_taken}"
        )
        return rec

    def log_system_event(self, event_type: str, message: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Log lifecycle, startup, shutdown, or health state events."""
        payload = {
            "event_type": event_type,
            "message": message,
        }
        if metadata:
            payload["metadata"] = metadata
        rec = self._write_audit_record("SYSTEM_EVENT", payload)
        self._console_logger.info(f"SYSTEM [{event_type}] {message}")
        return rec

    def verify_integrity(self) -> Tuple[bool, str, int]:
        """
        Scan the entire audit log file and verify cryptographic SHA-256 hash chaining.
        Returns (is_valid, message, count).
        """
        if not self.audit_file.exists():
            return True, "Audit log file does not exist yet (clean state)", 0

        with open(self.audit_file, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]

        if not lines:
            return True, "Audit log is empty", 0

        prev_hash = "0" * 64
        for i, line in enumerate(lines):
            try:
                record = json.loads(line)
            except Exception as e:
                return False, f"JSON parse error at line {i + 1}: {e}", i

            recorded_seq = record.get("seq")
            recorded_prev = record.get("prev_hash")
            recorded_curr = record.get("curr_hash")

            if recorded_seq != i + 1:
                return False, f"Sequence discontinuity at line {i + 1}: expected {i + 1}, found {recorded_seq}", i

            if i > 0 and recorded_prev != prev_hash:
                return False, f"Hash chain broken at line {i + 1}: prev_hash mismatch", i

            if self.enable_hash_chain and recorded_curr:
                expected_curr = self._compute_hash(record)
                if expected_curr != recorded_curr:
                    return False, f"Tampered record at line {i + 1}: hash mismatch", i

            prev_hash = recorded_curr or prev_hash

        return True, f"All {len(lines)} audit records successfully verified. Chain is intact.", len(lines)
