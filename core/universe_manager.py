"""
Authoritative Trading Universe Manager.
Manages symbol normalization, exchange metadata discovery, CanTrade validation,
and graceful skipping of unsupported or unavailable assets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Canonical 12-Asset Target Universe
TARGET_UNIVERSE: List[str] = [
    "BTC/USD",
    "ETH/USD",
    "PEPE/USD",
    "BONK/USD",
    "STO/USD",
    "FET/USD",
    "PUMP/USD",
    "ENA/USD",
    "S/USD",
    "ADA/USD",
    "SOL/USD",
    "SUI/USD",
]


def normalize_symbol(symbol: str) -> str:
    """
    Normalize raw ticker or asset name to canonical 'ASSET/USD' format.
    Examples:
        'BTC' -> 'BTC/USD'
        'btc/usd' -> 'BTC/USD'
        'ETHUSDT' -> 'ETH/USD'
        'PEPE' -> 'PEPE/USD'
    """
    if not symbol:
        return ""
    s = symbol.strip().upper()
    if "/" in s:
        parts = s.split("/")
        return f"{parts[0].strip()}/{parts[1].strip()}"
    if s.endswith("USDT") and len(s) > 4:
        return f"{s[:-4]}/USD"
    if s.endswith("USD") and len(s) > 3:
        return f"{s[:-3]}/USD"
    return f"{s}/USD"


def canonicalize_universe(symbols: List[str]) -> List[str]:
    """
    Deduplicate and normalize a list of symbol strings while preserving order.
    """
    seen: Set[str] = set()
    result: List[str] = []
    for sym in symbols:
        norm = normalize_symbol(sym)
        if norm and norm not in seen:
            seen.add(norm)
            result.append(norm)
    return result


@dataclass
class SymbolConstraints:
    pair: str
    base_coin: str
    unit: str
    can_trade: bool = True
    price_precision: int = 2
    amount_precision: int = 6
    min_order_usd: float = 1.0
    asset_type: str = "crypto"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pair": self.pair,
            "base_coin": self.base_coin,
            "unit": self.unit,
            "can_trade": self.can_trade,
            "price_precision": self.price_precision,
            "amount_precision": self.amount_precision,
            "min_order_usd": self.min_order_usd,
            "asset_type": self.asset_type,
        }


@dataclass
class UniverseStatus:
    requested_pairs: List[str]
    active_pairs: List[str]
    skipped_pairs: Dict[str, str] = field(default_factory=dict)
    symbol_metadata: Dict[str, SymbolConstraints] = field(default_factory=dict)

    def summary_banner(self) -> str:
        req_str = ", ".join(self.requested_pairs) if self.requested_pairs else "None"
        act_str = ", ".join(self.active_pairs) if self.active_pairs else "None"
        if self.skipped_pairs:
            skip_lines = "\n".join(f"  - {p}: {reason}" for p, reason in self.skipped_pairs.items())
        else:
            skip_lines = "  None"

        return (
            f"\n+======================================================================+\n"
            f"|                   TRADING UNIVERSE INITIALIZATION                    |\n"
            f"+======================================================================+\n"
            f"Requested universe ({len(self.requested_pairs)}):\n"
            f"  {req_str}\n\n"
            f"Active tradable universe ({len(self.active_pairs)}):\n"
            f"  {act_str}\n\n"
            f"Skipped/unavailable ({len(self.skipped_pairs)}):\n"
            f"{skip_lines}\n"
            f"+======================================================================+"
        )


class UniverseManager:
    """
    Authoritative manager for the bot's trading universe.
    """

    def __init__(self, default_pairs: Optional[List[str]] = None):
        self.default_pairs = canonicalize_universe(default_pairs or TARGET_UNIVERSE)

    def resolve_universe(
        self,
        requested_pairs: Optional[List[str]],
        exchange_info: Optional[Dict[str, Any]] = None,
    ) -> UniverseStatus:
        """
        Validate requested universe against exchange metadata (TradePairs).
        - Respects 'CanTrade' flag.
        - Captures price precision, amount precision, and minimum order rules.
        - Skips unsupported or non-tradable pairs gracefully without raising exceptions.
        - In offline/mock mode (exchange_info is empty or None), falls back safely to requested pairs.
        """
        if requested_pairs is None:
            raw_requested = list(self.default_pairs)
        else:
            raw_requested = list(requested_pairs)

        canonical_requested = canonicalize_universe(raw_requested)
        if not canonical_requested:
            canonical_requested = list(self.default_pairs)

        # If no exchange info available (e.g. offline dry run or mock tests), assume tradable with defaults
        if not exchange_info:
            metadata: Dict[str, SymbolConstraints] = {}
            for p in canonical_requested:
                base = p.split("/")[0]
                metadata[p] = SymbolConstraints(
                    pair=p,
                    base_coin=base,
                    unit="USD",
                    can_trade=True,
                    price_precision=2 if base in ("BTC", "ETH", "SOL") else 6,
                    amount_precision=6,
                    min_order_usd=1.0,
                )
            return UniverseStatus(
                requested_pairs=canonical_requested,
                active_pairs=canonical_requested,
                skipped_pairs={},
                symbol_metadata=metadata,
            )

        active: List[str] = []
        skipped: Dict[str, str] = {}
        metadata = {}

        for pair in canonical_requested:
            info = exchange_info.get(pair)
            if not info:
                skipped[pair] = "Not listed in exchange trading pairs"
                continue

            can_trade = bool(info.get("CanTrade", True))
            if not can_trade:
                skipped[pair] = "Exchange reports CanTrade=False"
                continue

            base = info.get("Coin", pair.split("/")[0])
            unit = info.get("Unit", "USD")
            price_prec = int(info.get("PricePrecision", 2))
            amt_prec = int(info.get("AmountPrecision", 6))
            mini_order = float(info.get("MiniOrder", 1.0))
            asset_type = str(info.get("AssetType", "crypto"))

            constraints = SymbolConstraints(
                pair=pair,
                base_coin=base,
                unit=unit,
                can_trade=can_trade,
                price_precision=price_prec,
                amount_precision=amt_prec,
                min_order_usd=mini_order,
                asset_type=asset_type,
            )
            metadata[pair] = constraints
            active.append(pair)

        return UniverseStatus(
            requested_pairs=canonical_requested,
            active_pairs=active,
            skipped_pairs=skipped,
            symbol_metadata=metadata,
        )
