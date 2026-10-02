"""
Telegram Notification Engine for AutoSL Autonomous Trading Bot.
Provides non-blocking, reliable real-time notifications for:
- Bot Lifecycle: Startup, Shutdown, Pause, Resume, Kill-Switch
- Trade Execution: Trade Entry (BUY/SELL), Exit (AutoSL Trailing, Failed Breakout, TP, SL)
- Risk Management: Circuit Breaker warnings, Drawdown alerts
"""

from __future__ import annotations

import html
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

logger = logging.getLogger("AutoSLTelegram")


class TelegramNotifier:
    """
    Non-blocking Telegram notifier that dispatches formatted messages in background threads.
    Ensures network latency or Telegram API throttling never blocks the bot's quant execution cycle.
    """

    def __init__(self, bot_token: str = "", chat_id: str = "", enabled: bool = True):
        self.bot_token = bot_token.strip()
        self.chat_id = str(chat_id).strip()
        self.enabled = bool(enabled and self.bot_token and self.chat_id)
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="telegram_notify")
        self._api_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

        if self.enabled:
            logger.info("Telegram notifier initialized (chat_id: %s)", self.chat_id)
        else:
            logger.info("Telegram notifier disabled (missing credentials or disabled)")

    def send_message(
        self,
        text: str,
        parse_mode: str = "HTML",
        blocking: bool = False,
        timeout: float = 6.0,
    ) -> bool:
        """
        Sends an alert message to the configured Telegram chat.
        Dispatched asynchronously by default to protect execution loop timing.
        """
        if not self.enabled:
            return False

        if blocking:
            return self._send_http(text, parse_mode, timeout)

        try:
            self._executor.submit(self._send_http, text, parse_mode, timeout)
            return True
        except Exception as e:
            logger.error("Failed to enqueue Telegram message: %s", e)
            return False

    def _send_http(self, text: str, parse_mode: str, timeout: float) -> bool:
        """
        Direct HTTP post to Telegram Bot API. Catches all errors safely.
        """
        try:
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                self._api_url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    return True
                logger.warning("Telegram API returned non-200 status: %s", resp.status)
                return False
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            logger.warning("Telegram HTTPError %s: %s", e.code, body)
            return False
        except Exception as e:
            logger.warning("Telegram network delivery failed: %s", e)
            return False

    # -------------------------------------------------------------------------
    # Lifecycle Notifications
    # -------------------------------------------------------------------------

    def notify_startup(
        self,
        mode: str,
        git_commit: str,
        equity: float,
        pairs: List[str],
        tickers: Optional[Dict[str, float]] = None,
    ) -> None:
        """
        Alerts when the bot boots up and begins autonomous trading.
        """
        pairs_str = ", ".join(pairs)
        ticker_lines = []
        if tickers:
            for pair, px in tickers.items():
                ticker_lines.append(f"• <b>{html.escape(pair)}:</b> ${px:,.2f}")
        tickers_block = "\n".join(ticker_lines) if ticker_lines else "• Live data stream active"

        msg = (
            "🚀 <b>AutoSL Quant Bot Started</b>\n\n"
            f"• <b>Mode:</b> <code>{html.escape(mode)}</code>\n"
            f"• <b>Commit SHA:</b> <code>{html.escape(git_commit[:8])}</code>\n"
            f"• <b>Portfolio Equity:</b> ${equity:,.2f} USD\n"
            f"• <b>Target Pairs:</b> {html.escape(pairs_str)}\n\n"
            "<b>Market Snapshots:</b>\n"
            f"{tickers_block}\n\n"
            "<i>Status: Continuous 24/7 Autonomous Market Monitoring</i>"
        )
        self.send_message(msg)

    def notify_shutdown(
        self,
        reason: str = "Service Stop",
        equity: float = 0.0,
        realized_pnl: float = 0.0,
        open_positions: int = 0,
        wait_seconds: float = 2.0,
    ) -> None:
        """
        Alerts when the bot service stops or shuts down gracefully.
        Uses blocking mode to guarantee delivery before process exits.
        """
        pnl_sign = "+" if realized_pnl >= 0 else ""
        msg = (
            "🛑 <b>AutoSL Quant Bot Stopped</b>\n\n"
            f"• <b>Reason:</b> {html.escape(reason)}\n"
            f"• <b>Final Equity:</b> ${equity:,.2f} USD\n"
            f"• <b>Realized PnL:</b> {pnl_sign}${realized_pnl:,.2f} USD\n"
            f"• <b>Open Positions:</b> {open_positions}\n\n"
            "<i>State safely preserved to disk. Audit hash chain finalized.</i>"
        )
        self.send_message(msg, blocking=True, timeout=wait_seconds)

    def notify_pause_state(self, paused: bool) -> None:
        """
        Alerts when trading is paused or resumed.
        """
        if paused:
            msg = (
                "⏸️ <b>AutoSL Bot Paused</b>\n\n"
                "• Autonomous signal evaluation is suspended.\n"
                "• Existing position trailing stop-losses remain active."
            )
        else:
            msg = (
                "▶️ <b>AutoSL Bot Resumed</b>\n\n"
                "• Autonomous signal evaluation and order placement active."
            )
        self.send_message(msg)

    # -------------------------------------------------------------------------
    # Trade Execution Notifications
    # -------------------------------------------------------------------------

    def notify_trade_entry(
        self,
        symbol: str,
        side: str,
        strategy: str,
        price: float,
        quantity: float,
        notional_usd: float,
        stop_loss: float = 0.0,
        take_profit: float = 0.0,
        confidence: float = 0.0,
    ) -> None:
        """
        Alerts when a new trade position is entered.
        """
        icon = "🟢" if side.upper() == "BUY" else "🔴"
        sl_pct_str = ""
        if stop_loss > 0 and price > 0:
            diff_pct = abs((stop_loss - price) / price * 100.0)
            sl_pct_str = f" (${stop_loss:,.2f} | -{diff_pct:.2f}%)"

        tp_pct_str = ""
        if take_profit > 0 and price > 0:
            diff_pct = abs((take_profit - price) / price * 100.0)
            tp_pct_str = f" (${take_profit:,.2f} | +{diff_pct:.2f}%)"

        msg = (
            f"{icon} <b>TRADE ENTRY ({html.escape(side.upper())})</b>\n\n"
            f"• <b>Symbol:</b> <code>{html.escape(symbol)}</code>\n"
            f"• <b>Strategy:</b> <code>{html.escape(strategy)}</code>\n"
            f"• <b>Fill Price:</b> ${price:,.2f}\n"
            f"• <b>Quantity:</b> {quantity:.4f} (${notional_usd:,.2f} USD)\n"
            f"• <b>Stop Loss:</b> {sl_pct_str or 'AutoSL Dynamic'}\n"
            f"• <b>Target TP:</b> {tp_pct_str or 'Momentum Extension'}\n"
            f"• <b>Model Confidence:</b> {confidence * 100.0:.1f}%\n\n"
            "<i>AutoSL Dynamic Invalidation Engine Attached</i>"
        )
        self.send_message(msg)

    def notify_trade_exit(
        self,
        symbol: str,
        side: str,
        exit_reason: str,
        exit_price: float,
        entry_price: float,
        quantity: float,
        pnl_usd: float,
        pnl_pct: float,
        saved_loss_pct: Optional[float] = None,
    ) -> None:
        """
        Alerts when an open trade position is closed.
        """
        is_profit = pnl_usd >= 0
        icon = "🎯" if is_profit else "🛡️"
        outcome_label = "PROFIT TARGET HIT" if is_profit else "STOPPED OUT / INVALIDATED"
        pnl_sign = "+" if is_profit else ""

        saved_block = ""
        if saved_loss_pct is not None and saved_loss_pct > 0:
            saved_block = f"\n• <b>AutoSL Capital Saved:</b> +{saved_loss_pct:.2f}% vs full SL hit"

        msg = (
            f"{icon} <b>TRADE EXIT: {outcome_label}</b>\n\n"
            f"• <b>Symbol:</b> <code>{html.escape(symbol)}</code> (Closed {html.escape(side)})\n"
            f"• <b>Trigger Reason:</b> <code>{html.escape(exit_reason)}</code>\n"
            f"• <b>Exit Price:</b> ${exit_price:,.2f}\n"
            f"• <b>Entry Price:</b> ${entry_price:,.2f}\n"
            f"• <b>Net PnL:</b> <b>{pnl_sign}${pnl_usd:,.2f} USD ({pnl_sign}{pnl_pct:.2f}%)</b>"
            f"{saved_block}\n\n"
            "<i>Position ledger and cash balances updated</i>"
        )
        self.send_message(msg)

    # -------------------------------------------------------------------------
    # Risk & Observability Notifications
    # -------------------------------------------------------------------------

    def notify_circuit_breaker(
        self,
        reason: str,
        current_drawdown_pct: float,
        max_drawdown_pct: float,
    ) -> None:
        """
        High-priority risk alert when drawdown limit or circuit breaker is tripped.
        """
        msg = (
            "🚨 <b>CIRCUIT BREAKER TRIGGERED</b>\n\n"
            f"• <b>Reason:</b> {html.escape(reason)}\n"
            f"• <b>Current Drawdown:</b> {current_drawdown_pct * 100.0:.2f}%\n"
            f"• <b>Allowed Limit:</b> {max_drawdown_pct * 100.0:.2f}%\n"
            "• <b>Action:</b> Trading paused, open risk frozen or de-risked."
        )
        self.send_message(msg)
