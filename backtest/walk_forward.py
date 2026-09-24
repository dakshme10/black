"""
Walk-Forward Validation and Parameter Sensitivity Analysis Engine.
Implements chronological train-validation-test partitioning without data leakage (Section 21)
and parameter stability evaluation (Section 22).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
import pandas as pd

from backtest.engine import BacktestEngine, BacktestResult
from config.trading_params import AppConfig


@dataclass
class WalkForwardReport:
    in_sample_result: BacktestResult
    validation_result: BacktestResult
    out_of_sample_result: BacktestResult
    sensitivity_analysis: List[Dict[str, Any]]

    def summary(self) -> str:
        ins = self.in_sample_result.metrics
        val = self.validation_result.metrics
        oos = self.out_of_sample_result.metrics

        return (
            f"====================================================================\n"
            f"             CHRONOLOGICAL WALK-FORWARD VALIDATION REPORT           \n"
            f"====================================================================\n"
            f"{'Metric':<25} | {'In-Sample (60%)':<16} | {'Validation (20%)':<16} | {'Out-of-Sample (20%)':<16}\n"
            f"--------------------------------------------------------------------\n"
            f"{'Total Return':<25} | {ins.total_return_pct:>15.2f}% | {val.total_return_pct:>15.2f}% | {oos.total_return_pct:>15.2f}%\n"
            f"{'Sharpe Ratio':<25} | {ins.sharpe_ratio:>16.4f} | {val.sharpe_ratio:>16.4f} | {oos.sharpe_ratio:>16.4f}\n"
            f"{'Sortino Ratio':<25} | {ins.sortino_ratio:>16.4f} | {val.sortino_ratio:>16.4f} | {oos.sortino_ratio:>16.4f}\n"
            f"{'Calmar Ratio':<25} | {ins.calmar_ratio:>16.4f} | {val.calmar_ratio:>16.4f} | {oos.calmar_ratio:>16.4f}\n"
            f"{'COMPOSITE SCORE':<25} | {ins.composite_score:>16.4f} | {val.composite_score:>16.4f} | {oos.composite_score:>16.4f}\n"
            f"{'Max Drawdown':<25} | {ins.max_drawdown_pct:>15.2f}% | {val.max_drawdown_pct:>15.2f}% | {oos.max_drawdown_pct:>15.2f}%\n"
            f"{'Win Rate':<25} | {ins.win_rate_pct:>15.1f}% | {val.win_rate_pct:>15.1f}% | {oos.win_rate_pct:>15.1f}%\n"
            f"{'Profit Factor':<25} | {ins.profit_factor:>16.2f} | {val.profit_factor:>16.2f} | {oos.profit_factor:>16.2f}\n"
            f"{'Trade Count':<25} | {ins.total_trades:>16} | {val.total_trades:>16} | {oos.total_trades:>16}\n"
            f"{'Total Fees':<25} | ${ins.total_fees:>15.2f} | ${val.total_fees:>15.2f} | ${oos.total_fees:>15.2f}\n"
            f"===================================================================="
        )


class WalkForwardValidator:
    """
    Performs chronological walk-forward validation and parameter sensitivity sweeps.
    """

    def __init__(self, base_config: Optional[AppConfig] = None):
        self.base_config = base_config or AppConfig()

    def run_walk_forward(
        self,
        df: pd.DataFrame,
        symbol: str = "BTC/USD",
        train_pct: float = 0.60,
        val_pct: float = 0.20,
    ) -> WalkForwardReport:
        """
        Split dataset chronologically into Train (60%), Validation (20%), and Out-of-Sample (20%).
        """
        n = len(df)
        train_end = int(n * train_pct)
        val_end = int(n * (train_pct + val_pct))

        df_train = df.iloc[:train_end].copy().reset_index(drop=True)
        df_val = df.iloc[train_end:val_end].copy().reset_index(drop=True)
        df_oos = df.iloc[val_end:].copy().reset_index(drop=True)

        engine = BacktestEngine(self.base_config)

        res_train = engine.run(df_train, symbol=symbol, warmup_bars=40)
        res_val = engine.run(df_val, symbol=symbol, warmup_bars=40)
        res_oos = engine.run(df_oos, symbol=symbol, warmup_bars=40)

        # Sensitivity analysis on risk percent and ATR multiplier
        sensitivity = self.run_sensitivity_analysis(df_train, symbol=symbol)

        return WalkForwardReport(
            in_sample_result=res_train,
            validation_result=res_val,
            out_of_sample_result=res_oos,
            sensitivity_analysis=sensitivity,
        )

    def run_sensitivity_analysis(
        self,
        df: pd.DataFrame,
        symbol: str = "BTC/USD",
    ) -> List[Dict[str, Any]]:
        """
        Test strategy robustness across parameter ranges (Section 22).
        """
        results = []
        risk_pcts = [0.0075, 0.010, 0.0125]
        atr_mults = [1.2, 1.5, 2.0]

        for r_pct in risk_pcts:
            for atr_m in atr_mults:
                cfg = AppConfig()
                cfg.portfolio.max_risk_per_trade_pct = r_pct
                cfg.trailing_stop.atr_multiplier = atr_m

                eng = BacktestEngine(cfg)
                res = eng.run(df, symbol=symbol, warmup_bars=40)
                results.append({
                    "risk_per_trade_pct": r_pct * 100.0,
                    "atr_multiplier": atr_m,
                    "total_return_pct": res.metrics.total_return_pct,
                    "max_drawdown_pct": res.metrics.max_drawdown_pct,
                    "sharpe": res.metrics.sharpe_ratio,
                    "composite_score": res.metrics.composite_score,
                    "trades": res.metrics.total_trades,
                })
        return results
