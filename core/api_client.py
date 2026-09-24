"""
Production Roostoo Mock Exchange API Client.
Implements token bucket rate limiting, HMAC-SHA256 signature generation,
exponential backoff with jitter, idempotent order execution, and UNKNOWN state handling.
"""

from __future__ import annotations

import hashlib
import hmac
import random
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple, Union
import requests

from config.trading_params import ExchangeConfig
from logs.audit_logger import AuditLogger


class RateLimiter:
    """
    Thread-safe token bucket rate limiter.
    """

    def __init__(self, rate: float = 5.0, capacity: int = 10):
        self.rate = float(rate)  # tokens per second
        self.capacity = float(capacity)
        self.tokens = float(capacity)
        self.last_update = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """
        Wait for a token if necessary. Returns wait duration in seconds.
        """
        with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_update
            self.last_update = now

            # Replenish tokens
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)

            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return 0.0

            needed = 1.0 - self.tokens
            wait_time = needed / self.rate
            self.tokens = 0.0

        if wait_time > 0:
            time.sleep(wait_time)
        return wait_time


class RoostooAPIError(Exception):
    """Base exception for Roostoo API communication errors."""

    def __init__(self, message: str, status_code: Optional[int] = None, error_code: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


class UnknownOrderStateError(RoostooAPIError):
    """Raised when an order placement outcome is ambiguous (e.g. network timeout)."""
    pass


class RoostooClient:
    """
    Production-grade client for the Roostoo Mock REST API.
    Adheres strictly to the official Roostoo Public API specification.
    """

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        config: Optional[ExchangeConfig] = None,
        audit_logger: Optional[AuditLogger] = None,
    ):
        self.api_key = api_key
        self.secret_key = secret_key
        self.config = config or ExchangeConfig()
        self.audit_logger = audit_logger

        self.base_url = self.config.base_url.rstrip("/")
        self.session = requests.Session()
        self.rate_limiter = RateLimiter(
            rate=self.config.requests_per_second,
            capacity=self.config.burst_capacity,
        )

        # Server clock synchronization offset (server_time - local_time in ms)
        self._server_time_offset_ms: int = 0
        self._last_time_sync: float = 0.0

        # Sync server time at startup if credentials or network allowed
        self._sync_server_time()

    def _sync_server_time(self) -> None:
        """Synchronize local timestamp with Roostoo server time to prevent clock skew rejection."""
        try:
            url = f"{self.base_url}/v3/serverTime"
            local_before = int(time.time() * 1000)
            res = self.session.get(url, timeout=self.config.timeout_seconds)
            local_after = int(time.time() * 1000)
            if res.status_code == 200:
                data = res.json()
                server_time = int(data.get("ServerTime", 0))
                if server_time > 0:
                    local_mid = (local_before + local_after) // 2
                    self._server_time_offset_ms = server_time - local_mid
                    self._last_time_sync = time.monotonic()
        except Exception:
            # Fall back to 0 offset if server is unreachable
            pass

    def get_synced_timestamp(self) -> str:
        """Return 13-digit millisecond timestamp adjusted for server time skew."""
        # Resync every 10 minutes
        if time.monotonic() - self._last_time_sync > 600:
            self._sync_server_time()
        curr_ms = int(time.time() * 1000) + self._server_time_offset_ms
        return str(curr_ms)

    def generate_signature(self, params: Dict[str, Any]) -> Tuple[str, str]:
        """
        Generate HMAC-SHA256 signature according to Roostoo specification.
        1. Sort parameters alphabetically by key.
        2. Format into query string: key1=val1&key2=val2.
        3. Compute HMAC-SHA256 hex digest using secretKey.
        Returns: (signature, total_params_query_string)
        """
        sorted_keys = sorted(params.keys())
        total_params = "&".join(f"{k}={params[k]}" for k in sorted_keys)

        sig = hmac.new(
            self.secret_key.encode("utf-8"),
            total_params.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        return sig, total_params

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        signed: bool = False,
        is_idempotent: bool = True,
        client_order_id: str = "",
    ) -> Dict[str, Any]:
        """
        Execute HTTP request with rate limiting, signing, exponential backoff, and logging.
        """
        request_id = client_order_id or str(uuid.uuid4())
        url = f"{self.base_url}{path}"
        headers: Dict[str, str] = {
            "User-Agent": "RoostooQuantBot/1.0",
        }

        # Apply Rate Limiter
        wait_s = self.rate_limiter.acquire()
        wait_ms = wait_s * 1000.0

        req_params = dict(params or {})
        req_data = dict(data or {})

        # Handle signatures
        body_string = None
        if signed:
            headers["RST-API-KEY"] = self.api_key
            if method.upper() == "GET":
                if "timestamp" not in req_params:
                    req_params["timestamp"] = self.get_synced_timestamp()
                sig, _ = self.generate_signature(req_params)
                headers["MSG-SIGNATURE"] = sig
            elif method.upper() == "POST":
                if "timestamp" not in req_data:
                    req_data["timestamp"] = self.get_synced_timestamp()
                sig, body_string = self.generate_signature(req_data)
                headers["MSG-SIGNATURE"] = sig
                headers["Content-Type"] = "application/x-www-form-urlencoded"

        attempts = 0
        max_retries = self.config.max_retries if is_idempotent else 0

        while True:
            attempts += 1
            t0 = time.monotonic()
            status_code = 0
            err_msg = ""

            try:
                if method.upper() == "GET":
                    resp = self.session.get(
                        url,
                        params=req_params,
                        headers=headers,
                        timeout=self.config.timeout_seconds,
                    )
                else:
                    # Roostoo expects raw urlencoded string body for signed POST
                    payload_to_send = body_string if body_string is not None else req_data
                    resp = self.session.post(
                        url,
                        data=payload_to_send,
                        headers=headers,
                        timeout=self.config.timeout_seconds,
                    )

                status_code = resp.status_code
                elapsed_ms = (time.monotonic() - t0) * 1000.0

                # Check HTTP retryable status codes (429, 500, 502, 503, 504)
                if status_code in (429, 500, 502, 503, 504):
                    err_msg = f"HTTP {status_code}: {resp.text}"
                    if attempts <= max_retries:
                        backoff = min(
                            self.config.retry_max_delay,
                            self.config.retry_base_delay * (2 ** (attempts - 1)),
                        ) + random.uniform(0.05, 0.25)
                        if self.audit_logger:
                            self.audit_logger.log_api_request(
                                method=method,
                                endpoint=path,
                                status_code=status_code,
                                elapsed_ms=elapsed_ms,
                                request_id=request_id,
                                retries=attempts,
                                rate_limit_delay_ms=backoff * 1000.0,
                                error=f"Retryable error: {err_msg}",
                            )
                        time.sleep(backoff)
                        continue
                    else:
                        if self.audit_logger:
                            self.audit_logger.log_api_request(
                                method=method,
                                endpoint=path,
                                status_code=status_code,
                                elapsed_ms=elapsed_ms,
                                request_id=request_id,
                                retries=attempts,
                                error=f"Retries exhausted: {err_msg}",
                            )
                        raise RoostooAPIError(f"HTTP {status_code} on {path}: {resp.text}", status_code=status_code)

                # Successful HTTP status
                resp.raise_for_status()
                data_json = resp.json()

                if self.audit_logger:
                    self.audit_logger.log_api_request(
                        method=method,
                        endpoint=path,
                        status_code=status_code,
                        elapsed_ms=elapsed_ms,
                        request_id=request_id,
                        retries=attempts - 1,
                        rate_limit_delay_ms=wait_ms,
                    )

                return data_json

            except (requests.Timeout, requests.ConnectionError) as net_err:
                elapsed_ms = (time.monotonic() - t0) * 1000.0
                err_msg = str(net_err)

                # If this was a non-idempotent order submission, we MUST NOT blindly retry!
                if not is_idempotent and path.endswith("/place_order"):
                    if self.audit_logger:
                        self.audit_logger.log_api_request(
                            method=method,
                            endpoint=path,
                            status_code=0,
                            elapsed_ms=elapsed_ms,
                            request_id=request_id,
                            retries=attempts,
                            error=f"NETWORK_TIMEOUT on place_order -> UNKNOWN: {err_msg}",
                        )
                    raise UnknownOrderStateError(
                        f"Order submission outcome UNKNOWN due to network error: {net_err}. Must reconcile.",
                        status_code=0,
                    )

                if attempts <= max_retries:
                    backoff = min(
                        self.config.retry_max_delay,
                        self.config.retry_base_delay * (2 ** (attempts - 1)),
                    ) + random.uniform(0.05, 0.25)
                    time.sleep(backoff)
                    continue

                if self.audit_logger:
                    self.audit_logger.log_api_request(
                        method=method,
                        endpoint=path,
                        status_code=0,
                        elapsed_ms=elapsed_ms,
                        request_id=request_id,
                        retries=attempts,
                        error=f"Network error retries exhausted: {err_msg}",
                    )
                raise RoostooAPIError(f"Network error communicating with {path}: {net_err}")

    # =========================================================================
    # Public Endpoints
    # =========================================================================

    def get_server_time(self) -> int:
        """
        GET /v3/serverTime
        Returns current exchange timestamp in milliseconds.
        """
        res = self._request("GET", "/v3/serverTime", signed=False, is_idempotent=True)
        return int(res.get("ServerTime", 0))

    def get_exchange_info(self) -> Dict[str, Any]:
        """
        GET /v3/exchangeInfo
        Returns trading pairs, decimal precision, and minimum order rules.
        """
        return self._request("GET", "/v3/exchangeInfo", signed=False, is_idempotent=True)

    def get_ticker(self, pair: Optional[str] = None) -> Dict[str, Any]:
        """
        GET /v3/ticker
        Returns ticker data for a specific pair or all pairs.
        """
        params = {"timestamp": self.get_synced_timestamp()}
        if pair:
            params["pair"] = pair
        return self._request("GET", "/v3/ticker", params=params, signed=False, is_idempotent=True)

    # =========================================================================
    # Signed Endpoints (RCL_TopLevelCheck)
    # =========================================================================

    def get_balance(self) -> Dict[str, Any]:
        """
        GET /v3/balance
        Returns wallet balance (Free and Lock for each currency).
        """
        params = {"timestamp": self.get_synced_timestamp()}
        return self._request("GET", "/v3/balance", params=params, signed=True, is_idempotent=True)

    def get_pending_count(self) -> Dict[str, Any]:
        """
        GET /v3/pending_count
        Returns count of outstanding pending orders.
        """
        params = {"timestamp": self.get_synced_timestamp()}
        return self._request("GET", "/v3/pending_count", params=params, signed=True, is_idempotent=True)

    def place_order(
        self,
        pair: str,
        side: str,
        quantity: float,
        price: Optional[float] = None,
        order_type: str = "MARKET",
        client_order_id: str = "",
    ) -> Dict[str, Any]:
        """
        POST /v3/place_order
        Place a new order on Roostoo mock exchange.
        Non-idempotent operation: handles UNKNOWN state safely on timeout.
        """
        side_upper = side.upper()
        type_upper = order_type.upper()
        if side_upper not in ("BUY", "SELL"):
            raise ValueError(f"Invalid side: {side}. Must be 'BUY' or 'SELL'.")
        if type_upper not in ("LIMIT", "MARKET"):
            raise ValueError(f"Invalid order type: {order_type}. Must be 'LIMIT' or 'MARKET'.")
        if type_upper == "LIMIT" and price is None:
            raise ValueError("LIMIT order requires 'price'.")

        payload: Dict[str, Any] = {
            "pair": pair,
            "side": side_upper,
            "type": type_upper,
            "quantity": str(quantity),
            "timestamp": self.get_synced_timestamp(),
        }
        if type_upper == "LIMIT" and price is not None:
            payload["price"] = str(price)

        # Place order is marked is_idempotent=False to prevent duplicate submissions on timeouts
        return self._request(
            method="POST",
            path="/v3/place_order",
            data=payload,
            signed=True,
            is_idempotent=False,
            client_order_id=client_order_id,
        )

    def query_order(
        self,
        order_id: Optional[Union[int, str]] = None,
        pair: Optional[str] = None,
        pending_only: Optional[bool] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """
        POST /v3/query_order
        Query matched orders or active pending orders.
        """
        payload: Dict[str, Any] = {
            "timestamp": self.get_synced_timestamp(),
        }
        if order_id is not None:
            payload["order_id"] = str(order_id)
        else:
            if pair:
                payload["pair"] = pair
            if pending_only is not None:
                payload["pending_only"] = "TRUE" if pending_only else "FALSE"
            if limit:
                payload["limit"] = str(limit)
            if offset:
                payload["offset"] = str(offset)

        return self._request(
            method="POST",
            path="/v3/query_order",
            data=payload,
            signed=True,
            is_idempotent=True,
        )

    def cancel_order(
        self,
        order_id: Optional[Union[int, str]] = None,
        pair: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        POST /v3/cancel_order
        Cancel pending order(s).
        """
        payload: Dict[str, Any] = {
            "timestamp": self.get_synced_timestamp(),
        }
        if order_id is not None:
            payload["order_id"] = str(order_id)
        elif pair is not None:
            payload["pair"] = pair

        return self._request(
            method="POST",
            path="/v3/cancel_order",
            data=payload,
            signed=True,
            is_idempotent=True,
        )
