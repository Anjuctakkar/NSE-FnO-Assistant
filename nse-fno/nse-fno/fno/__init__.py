"""NSE F&O framework: technical + Greeks-based trade suggestions, backtesting and forward evaluation.

    from fno import suggest, run_backtest, SyntheticProvider
"""
from .backtest import edge_vs_random, run_backtest, simulate_position
from .config import CostModel, MarketConfig, StrategyConfig
from .data import BhavcopyProvider, StaticProvider, SyntheticProvider, chain_from_records
from .evaluate import evaluate_log, log_suggestions, set_taken
from .selector import suggest

__all__ = ["suggest", "run_backtest", "simulate_position", "edge_vs_random", "evaluate_log", "log_suggestions", "set_taken",
           "SyntheticProvider", "BhavcopyProvider", "StaticProvider", "chain_from_records", "MarketConfig", "CostModel", "StrategyConfig"]
