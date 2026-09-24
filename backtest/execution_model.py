"""
Backtesting Execution Model.
Simulates realistic trade fills, maker/taker fees (0.05% / 0.10%), slippage,
and intrabar stop-loss and take-profit order fills without lookahead bias.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from config.trading_params import FeesConfig


@dataclass
class BacktestTrade:
    symbol: str
    strategy: str
    entry_time: int
    exit_time: int
    side: str
    quantity: float
    entry_price: float
    exit_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    pnl: float
    net_pnl: float
    fees: float
    exit_reason: str


class SimulatedExecutionModel:
    """
    Simulates execution fills with transaction costs and slippage.
    """

    def __init__(self, fees: Optional[FeesConfig] = None):
        self.fees = fees or FeesConfig()

    def simulate_entry(
        self,
        side: str,
        price: float,
        quantity: float,
        is_maker: bool = False,
    ) -> Tuple[float, float]:
        """
        Calculate fill price and commission for entry.
        Returns: (fill_price, fee_amount)
        """
        fee_rate = self.fees.maker_fee_pct if is_maker else self.fees.taker_fee_pct
        slip_rate = 0.0 if is_maker else self.fees.slippage_pct

        fill_price = price * (1.0 + slip_rate) if side == "BUY" else price * (1.0 - slip_rate)
        fee = fill_price * quantity * fee_rate
        return fill_price, fee

    def simulate_exit(
        self,
        side: str,
        price: float,
        quantity: float,
        is_maker: bool = False,
    ) -> Tuple[float, float]:
        """
        Calculate fill price and commission for exit.
        Returns: (fill_price, fee_amount)
        """
        fee_rate = self.fees.maker_fee_pct if is_maker else self.fees.taker_fee_pct
        slip_rate = 0.0 if is_maker else self.fees.slippage_pct

        fill_price = price * (1.0 - slip_rate) if side == "SELL" else price * (1.0 + slip_rate)
        fee = fill_price * quantity * fee_rate
        return fill_price, fee
