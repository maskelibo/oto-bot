#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║     AUTONOMOUS MULTI-STRATEGY OPTIMIZER                        ║
║     Target: 50%+ Realistic ROI (2025 data)                     ║
║     Mode: RESEARCH & BACKTEST ONLY — NO LIVE TRADING           ║
║     All strategies: ScalperV2, DayTrader, SwingTrader           ║
║     Leverage handled by BacktestEngine (realistic margin)       ║
╚══════════════════════════════════════════════════════════════════╝

Agents work autonomously:
  - Fetch full 2025 data (paginated) for top coins
  - Evolve parameters via genetic algorithm across all strategies
  - Leverage is set inside BacktestConfig (not multiplied after)
  - Stop when 50%+ ROI achieved with acceptable risk
  - If 50% hit and room to improve, keep going up to stretch target
"""

from __future__ import annotations

import json
import logging
import os
import random
import sys
import time
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from oto_bot.analytics.scoring import composite_score
from oto_bot.backtest.engine import BacktestEngine, BacktestConfig
from oto_bot.core.models import ExperimentResult
from oto_bot.strategies.base import StrategyContext
from oto_bot.strategies.scalper_v2 import ScalperV2Strategy
from oto_bot.strategies.day_trader import DayTraderStrategy
from oto_bot.strategies.swing_trader import SwingTraderStrategy

# ─────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────
Path("logs").mkdir(exist_ok=True)
Path("artifacts").mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("logs/autonomous_optimizer.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("autonomous_optimizer")

# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────
TARGET_ROI = 0.50           # Minimum 50%
STRETCH_ROI = 1.00          # Stretch: 100%+
MAX_GENERATIONS = 200
POPULATION_SIZE = 40
ELITE_COUNT = 10
INITIAL_CAPITAL = 10_000.0

TOP_COINS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT",
    "LTC/USDT", "SUI/USDT", "ADA/USDT", "LINK/USDT",
    "ALGO/USDT", "FET/USDT", "DOGE/USDT", "RENDER/USDT",
    "AAVE/USDT", "PEPE/USDT", "OP/USDT", "INJ/USDT",
    "WLD/USDT", "NEAR/USDT", "AVAX/USDT", "XRP/USDT",
]

TIMEFRAMES = ["15m", "1h", "4h"]

STRATEGIES = {
    "scalp_v2": {
        "class": ScalperV2Strategy,
        "family": "scalp",
        "timeframes": ["15m", "1h", "4h"],
    },
    "day": {
        "class": DayTraderStrategy,
        "family": "day",
        "timeframes": ["15m", "1h", "4h"],
    },
    "swing": {
        "class": SwingTraderStrategy,
        "family": "swing",
        "timeframes": ["1h", "4h"],
    },
}

# ─────────────────────────────────────────────────────────────
# Parameter ranges per strategy
# ─────────────────────────────────────────────────────────────

SCALPER_V2_RANGES = {
    "bb_period": (10, 30),
    "bb_std": (1.2, 3.0),
    "zscore_period": (10, 30),
    "zscore_entry": (0.5, 2.5),
    "rsi_period": (5, 21),
    "rsi_oversold": (20, 45),
    "rsi_overbought": (55, 80),
    "vwap_period": (10, 40),
    "vwap_dev_threshold": (0.5, 3.0),
    "volume_surge_mult": (1.0, 3.0),
    "adx_max_for_reversion": (20, 45),
    "adx_min_for_reversion": (5, 20),
    "min_confluence_score": (0.25, 0.60),
    "atr_period": (7, 21),
    "atr_sl_multiplier": (0.5, 2.5),
    "atr_tp_multiplier": (0.8, 4.0),
    "wick_ratio_threshold": (0.3, 0.8),
    "htf_ema_period": (20, 120),
}

DAY_RANGES = {
    "ema_fast": (5, 20),
    "ema_slow": (15, 50),
    "rsi_period": (7, 21),
    "rsi_overbought": (60, 85),
    "rsi_oversold": (15, 40),
    "volume_ma_period": (10, 30),
    "volume_multiplier": (1.0, 3.0),
    "atr_period": (7, 21),
    "atr_sl_multiplier": (0.8, 3.0),
    "atr_tp_multiplier": (1.0, 5.0),
}

SWING_RANGES = {
    "ma_fast": (20, 80),
    "ma_slow": (80, 300),
    "macd_fast": (8, 20),
    "macd_slow": (18, 40),
    "macd_signal": (5, 15),
    "adx_period": (10, 21),
    "adx_threshold": (15, 35),
    "atr_period": (7, 21),
    "atr_sl_multiplier": (1.0, 4.0),
    "atr_tp_multiplier": (1.5, 6.0),
}

PARAM_RANGES_MAP = {
    "scalp_v2": SCALPER_V2_RANGES,
    "day": DAY_RANGES,
    "swing": SWING_RANGES,
}

INT_PARAMS_ALL = {
    "bb_period", "zscore_period", "rsi_period", "rsi_oversold", "rsi_overbought",
    "vwap_period", "atr_period", "adx_min_for_reversion", "adx_max_for_reversion",
    "htf_ema_period", "ema_fast", "ema_slow", "volume_ma_period",
    "ma_fast", "ma_slow", "macd_fast", "macd_slow", "macd_signal",
    "adx_period", "adx_threshold",
}

# ─────────────────────────────────────────────────────────────
# Paginated data fetcher (full 2025)
# ─────────────────────────────────────────────────────────────
_data_cache: dict[str, pd.DataFrame] = {}


def _tf_to_ms(tf: str) -> int:
    """Convert timeframe string to milliseconds per candle."""
    units = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
    num = int(tf[:-1])
    return num * units[tf[-1]]


def fetch_full_2025(symbol: str, timeframe: str) -> pd.DataFrame | None:
    """Fetch data from 2025-01-01 to now, paginated (max 1000 per request)."""
    cache_key = f"{symbol}_{timeframe}"
    if cache_key in _data_cache:
        return _data_cache[cache_key]

    try:
        import ccxt
        exchange = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "spot"}})
        exchange.load_markets()

        if symbol not in exchange.markets:
            logger.warning(f"{symbol} not found on Binance")
            return None

        start = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        now = int(datetime.now(timezone.utc).timestamp() * 1000)
        tf_ms = _tf_to_ms(timeframe)

        all_candles = []
        cursor = start
        while cursor < now:
            batch = exchange.fetch_ohlcv(symbol, timeframe, since=cursor, limit=1000)
            if not batch:
                break
            all_candles.extend(batch)
            last_ts = batch[-1][0]
            cursor = last_ts + tf_ms
            if len(batch) < 1000:
                break
            time.sleep(exchange.rateLimit / 1000)

        if len(all_candles) < 50:
            logger.warning(f"{symbol} {timeframe}: only {len(all_candles)} candles, skipping")
            return None

        df = pd.DataFrame(all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.drop_duplicates(subset=["timestamp"], inplace=True)
        df.set_index("timestamp", inplace=True)
        df.sort_index(inplace=True)
        df.index.name = None

        _data_cache[cache_key] = df
        logger.info(f"Fetched {symbol} {timeframe}: {len(df)} candles ({df.index[0]} → {df.index[-1]})")
        return df

    except Exception as e:
        logger.error(f"Failed to fetch {symbol} {timeframe}: {e}")
        return None


# ─────────────────────────────────────────────────────────────
# Parameter helpers
# ─────────────────────────────────────────────────────────────

def random_params(strategy_key: str) -> dict:
    ranges = PARAM_RANGES_MAP[strategy_key]
    p = {}
    for key, (lo, hi) in ranges.items():
        val = random.uniform(lo, hi)
        if key in INT_PARAMS_ALL:
            val = int(round(val))
        else:
            val = round(val, 3)
        p[key] = val
    _fix_constraints(p, strategy_key)
    return p


def mutate_params(base: dict, strategy_key: str, strength: float = 0.15) -> dict:
    ranges = PARAM_RANGES_MAP[strategy_key]
    child = {}
    for key, (lo, hi) in ranges.items():
        base_val = base.get(key, (lo + hi) / 2)
        range_size = hi - lo
        noise = random.gauss(0, strength * range_size)
        new_val = base_val + noise
        new_val = max(lo, min(hi, new_val))
        if key in INT_PARAMS_ALL:
            new_val = int(round(new_val))
        else:
            new_val = round(new_val, 3)
        child[key] = new_val
    _fix_constraints(child, strategy_key)
    return child


def crossover(p1: dict, p2: dict, strategy_key: str) -> dict:
    ranges = PARAM_RANGES_MAP[strategy_key]
    child = {}
    for key in ranges:
        child[key] = p1.get(key) if random.random() < 0.5 else p2.get(key)
        if child[key] is None:
            child[key] = random_params(strategy_key)[key]
    _fix_constraints(child, strategy_key)
    return child


def _fix_constraints(p: dict, strategy_key: str):
    if "rsi_oversold" in p and "rsi_overbought" in p:
        if p["rsi_oversold"] >= p["rsi_overbought"]:
            p["rsi_oversold"] = p["rsi_overbought"] - 15
    if "adx_min_for_reversion" in p and "adx_max_for_reversion" in p:
        if p["adx_min_for_reversion"] >= p["adx_max_for_reversion"]:
            p["adx_min_for_reversion"] = p["adx_max_for_reversion"] - 10
    if "ema_fast" in p and "ema_slow" in p:
        if p["ema_fast"] >= p["ema_slow"]:
            p["ema_fast"] = max(5, p["ema_slow"] - 10)
    if "ma_fast" in p and "ma_slow" in p:
        if p["ma_fast"] >= p["ma_slow"]:
            p["ma_fast"] = max(20, p["ma_slow"] - 30)
    if "macd_fast" in p and "macd_slow" in p:
        if p["macd_fast"] >= p["macd_slow"]:
            p["macd_fast"] = max(8, p["macd_slow"] - 8)


# ─────────────────────────────────────────────────────────────
# Single experiment
# ─────────────────────────────────────────────────────────────

def run_experiment(
    symbol: str,
    timeframe: str,
    strategy_key: str,
    params: dict,
    capital: float = INITIAL_CAPITAL,
    leverage: float = 1.0,
) -> dict | None:
    """Run one backtest. Leverage is set in BacktestConfig (realistic)."""
    data = fetch_full_2025(symbol, timeframe)
    if data is None:
        return None

    strat_info = STRATEGIES[strategy_key]
    strategy = strat_info["class"]()

    # Merge params
    full_params = {**params, "capital": capital, "risk_per_trade": 0.01}
    if strategy_key == "scalp_v2":
        full_params["base_leverage"] = leverage
        full_params["max_leverage"] = min(leverage * 2, 5.0)

    context = StrategyContext(
        market="crypto",
        strategy_family=strat_info["family"],
        symbol=symbol,
        timeframe=timeframe,
        params=full_params,
    )

    # Leverage is handled BY the engine (margin, sizing, slippage)
    config = BacktestConfig(
        fee_bps=10.0,
        slippage_bps=5.0,
        initial_capital=capital,
        risk_per_trade=0.01,
        use_position_sizing=True,
        use_volume_slippage=True,
        leverage=leverage,
        apply_funding=True,
        funding_rate=0.0001,
        funding_interval_h=8.0,
        strategy_family=strat_info["family"],
        daily_loss_limit=0.05,
    )

    try:
        engine = BacktestEngine(config)
        result = engine.run(strategy, data, context)

        if result.total_trades < 5:
            return None

        score = composite_score(result)

        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "strategy": strategy_key,
            "params": params,
            "leverage": leverage,
            "result": result,
            "score": score,
            "roi": result.roi,
            "sharpe": result.sharpe,
            "sortino": result.sortino,
            "max_drawdown": result.max_drawdown,
            "win_rate": result.win_rate,
            "profit_factor": result.profit_factor,
            "total_trades": result.total_trades,
            "calmar": result.calmar,
            "cagr": result.cagr,
        }
    except Exception as e:
        logger.error(f"Experiment failed {strategy_key} {symbol} {timeframe}: {e}")
        return None


# ─────────────────────────────────────────────────────────────
# Target check
# ─────────────────────────────────────────────────────────────

def is_qualified(r: dict, target_roi: float = TARGET_ROI) -> bool:
    """Check if a result qualifies as a realistic 50%+ strategy."""
    return (
        r["roi"] >= target_roi
        and r["sharpe"] >= 0.8
        and r["max_drawdown"] >= -0.35      # Max 35% drawdown
        and r["total_trades"] >= 15
        and r["win_rate"] >= 0.35
        and r["profit_factor"] >= 1.2
    )


# ─────────────────────────────────────────────────────────────
# Evolutionary optimizer
# ─────────────────────────────────────────────────────────────

class AutonomousOptimizer:
    def __init__(self):
        self.generation = 0
        self.total_experiments = 0
        self.all_results: list[dict] = []
        self.best_ever: dict | None = None
        self.generation_bests: list[dict] = []
        self.qualified_results: list[dict] = []
        self.target_hit = False
        self.start_time = 0.0

    def _create_initial_population(self) -> list[dict]:
        """Generate diverse initial population across all strategies."""
        population = []
        for _ in range(POPULATION_SIZE):
            strategy_key = random.choice(list(STRATEGIES.keys()))
            strat_info = STRATEGIES[strategy_key]
            coin = random.choice(TOP_COINS)
            tf = random.choice(strat_info["timeframes"])
            params = random_params(strategy_key)
            leverage = random.choice([1.0, 1.5, 2.0, 2.5, 3.0])
            population.append({
                "symbol": coin, "timeframe": tf,
                "strategy": strategy_key,
                "params": params, "leverage": leverage,
            })
        return population

    def _evaluate_population(self, population: list[dict]) -> list[dict]:
        results = []
        for member in population:
            exp = run_experiment(
                member["symbol"], member["timeframe"],
                member["strategy"], member["params"],
                INITIAL_CAPITAL, member.get("leverage", 1.0),
            )
            if exp is not None:
                results.append(exp)
                self.all_results.append(exp)
                self.total_experiments += 1

        return sorted(results, key=lambda x: x["score"], reverse=True)

    def _evolve_population(self, elites: list[dict]) -> list[dict]:
        next_gen = []

        # Keep elites
        for e in elites:
            next_gen.append({
                "symbol": e["symbol"], "timeframe": e["timeframe"],
                "strategy": e["strategy"],
                "params": e["params"], "leverage": e.get("leverage", 1.0),
            })

        # Mutations (50%)
        while len(next_gen) < POPULATION_SIZE * 0.55:
            parent = random.choice(elites)
            sk = parent["strategy"]
            child_params = mutate_params(parent["params"], sk, strength=0.15)
            # Sometimes try different coin/tf
            coin = random.choice(TOP_COINS) if random.random() < 0.3 else parent["symbol"]
            tf_opts = STRATEGIES[sk]["timeframes"]
            tf = random.choice(tf_opts) if random.random() < 0.2 else parent["timeframe"]
            # Vary leverage slightly
            lev = parent.get("leverage", 1.0)
            if random.random() < 0.2:
                lev = max(1.0, min(3.0, lev + random.choice([-0.5, 0, 0.5, 1.0])))
            next_gen.append({
                "symbol": coin, "timeframe": tf,
                "strategy": sk, "params": child_params, "leverage": lev,
            })

        # Crossovers (25%)
        while len(next_gen) < POPULATION_SIZE * 0.80:
            if len(elites) >= 2:
                p1, p2 = random.sample(elites, 2)
                if p1["strategy"] == p2["strategy"]:
                    sk = p1["strategy"]
                    child_params = crossover(p1["params"], p2["params"], sk)
                    coin = random.choice([p1["symbol"], p2["symbol"]])
                    tf = random.choice([p1["timeframe"], p2["timeframe"]])
                    lev = random.choice([p1.get("leverage", 1.0), p2.get("leverage", 1.0)])
                    next_gen.append({
                        "symbol": coin, "timeframe": tf,
                        "strategy": sk, "params": child_params, "leverage": lev,
                    })
                    continue
            # Fallback: mutation
            parent = random.choice(elites)
            sk = parent["strategy"]
            next_gen.append({
                "symbol": parent["symbol"], "timeframe": parent["timeframe"],
                "strategy": sk,
                "params": mutate_params(parent["params"], sk, strength=0.2),
                "leverage": parent.get("leverage", 1.0),
            })

        # Fresh exploration (20%)
        while len(next_gen) < POPULATION_SIZE:
            sk = random.choice(list(STRATEGIES.keys()))
            strat_info = STRATEGIES[sk]
            coin = random.choice(TOP_COINS)
            tf = random.choice(strat_info["timeframes"])
            next_gen.append({
                "symbol": coin, "timeframe": tf,
                "strategy": sk,
                "params": random_params(sk),
                "leverage": random.choice([1.0, 1.5, 2.0, 2.5, 3.0]),
            })

        return next_gen

    def _deep_optimize_winners(self, winners: list[dict]) -> list[dict]:
        """Take qualified winners and do focused optimization around them."""
        logger.info(f"=== DEEP OPTIMIZATION on {len(winners)} winners ===")
        improved = []

        for w in winners[:5]:
            best = w
            for attempt in range(30):
                child_params = mutate_params(best["params"], best["strategy"], strength=0.08)
                # Also try slight leverage variations
                lev = best.get("leverage", 1.0)
                if random.random() < 0.3:
                    lev = max(1.0, min(3.0, lev + random.uniform(-0.3, 0.3)))

                exp = run_experiment(
                    best["symbol"], best["timeframe"],
                    best["strategy"], child_params,
                    INITIAL_CAPITAL, lev,
                )
                self.total_experiments += 1

                if exp and exp["score"] > best["score"] and is_qualified(exp):
                    best = exp
                    logger.info(
                        f"  Deep opt improved: {best['symbol']} {best['strategy']} "
                        f"ROI:{best['roi']:.1%} Sharpe:{best['sharpe']:.2f} DD:{best['max_drawdown']:.1%}"
                    )

            improved.append(best)

        return improved

    def _multi_coin_validation(self, winner: dict) -> list[dict]:
        """Test a winning config across multiple coins for robustness."""
        logger.info(f"=== MULTI-COIN VALIDATION: {winner['strategy']} ===")
        results = []
        test_coins = random.sample(TOP_COINS, min(8, len(TOP_COINS)))

        for coin in test_coins:
            exp = run_experiment(
                coin, winner["timeframe"],
                winner["strategy"], winner["params"],
                INITIAL_CAPITAL, winner.get("leverage", 1.0),
            )
            self.total_experiments += 1
            if exp:
                results.append(exp)
                status = "OK" if exp["roi"] > 0 else "LOSS"
                logger.info(f"  {coin}: ROI:{exp['roi']:.1%} DD:{exp['max_drawdown']:.1%} [{status}]")

        profitable = [r for r in results if r["roi"] > 0]
        logger.info(f"  Profitable on {len(profitable)}/{len(results)} coins")
        return results

    def _save_report(self):
        all_sorted = sorted(self.all_results, key=lambda x: x["score"], reverse=True)
        qualified_sorted = sorted(self.qualified_results, key=lambda x: x["roi"], reverse=True)

        report = {
            "campaign": "autonomous_optimizer_v2",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "generations": self.generation,
            "total_experiments": self.total_experiments,
            "target_roi": TARGET_ROI,
            "target_hit": self.target_hit,
            "elapsed_minutes": (time.time() - self.start_time) / 60,
            "qualified_count": len(self.qualified_results),
            "best_ever": self._serialize_result(self.best_ever) if self.best_ever else None,
            "qualified_strategies": [
                self._serialize_result(r) for r in qualified_sorted[:20]
            ],
            "top_30_overall": [
                self._serialize_result(r) for r in all_sorted[:30]
            ],
            "generation_progress": [
                {
                    "gen": g.get("gen", i),
                    "best_roi": g.get("roi", 0),
                    "best_score": g.get("score", 0),
                    "strategy": g.get("strategy", ""),
                    "symbol": g.get("symbol", ""),
                }
                for i, g in enumerate(self.generation_bests)
            ],
        }

        report_path = Path("artifacts/autonomous_optimizer_report.json")
        report_path.write_text(json.dumps(report, indent=2, default=str))
        logger.info(f"Report saved: {report_path}")

    @staticmethod
    def _serialize_result(r: dict) -> dict:
        return {
            "symbol": r["symbol"],
            "timeframe": r["timeframe"],
            "strategy": r["strategy"],
            "leverage": r.get("leverage", 1.0),
            "params": r["params"],
            "roi": r["roi"],
            "sharpe": r["sharpe"],
            "sortino": r.get("sortino", 0),
            "max_drawdown": r["max_drawdown"],
            "win_rate": r["win_rate"],
            "profit_factor": r["profit_factor"],
            "total_trades": r["total_trades"],
            "calmar": r.get("calmar", 0),
            "cagr": r.get("cagr", 0),
            "score": r["score"],
        }

    # ─── Main loop ───────────────────────────────────────────

    def run(self):
        self.start_time = time.time()

        logger.info("=" * 60)
        logger.info("  AUTONOMOUS MULTI-STRATEGY OPTIMIZER")
        logger.info(f"  Target: {TARGET_ROI:.0%} ROI | Stretch: {STRETCH_ROI:.0%}")
        logger.info(f"  Capital: ${INITIAL_CAPITAL:,.0f}")
        logger.info(f"  Population: {POPULATION_SIZE} | Max generations: {MAX_GENERATIONS}")
        logger.info(f"  Strategies: {list(STRATEGIES.keys())}")
        logger.info(f"  Coins: {len(TOP_COINS)} | Timeframes: {TIMEFRAMES}")
        logger.info("  Mode: RESEARCH ONLY — NO LIVE TRADING")
        logger.info("=" * 60)

        # Pre-fetch data for top coins to speed up iterations
        logger.info("\n--- PRE-FETCHING 2025 DATA ---")
        for coin in TOP_COINS:
            for tf in TIMEFRAMES:
                fetch_full_2025(coin, tf)

        logger.info(f"\nData cache: {len(_data_cache)} datasets loaded\n")

        # Gen 0
        population = self._create_initial_population()
        keep_going_after_target = True

        try:
            for gen in range(MAX_GENERATIONS):
                self.generation = gen

                results = self._evaluate_population(population)
                if not results:
                    logger.warning(f"Gen {gen}: All experiments failed, regenerating")
                    population = self._create_initial_population()
                    continue

                # Track best
                gen_best = results[0]
                self.generation_bests.append({**gen_best, "gen": gen})
                if self.best_ever is None or gen_best["score"] > self.best_ever["score"]:
                    self.best_ever = gen_best
                    logger.info(
                        f"  ★ NEW BEST: {gen_best['strategy']} {gen_best['symbol']} "
                        f"{gen_best['timeframe']} {gen_best.get('leverage',1):.1f}x → "
                        f"ROI:{gen_best['roi']:.1%} Sharpe:{gen_best['sharpe']:.2f} "
                        f"DD:{gen_best['max_drawdown']:.1%}"
                    )

                # Check for qualified results
                new_qualified = [r for r in results if is_qualified(r)]
                if new_qualified:
                    self.qualified_results.extend(new_qualified)
                    for q in new_qualified:
                        logger.info(
                            f"  ✓ QUALIFIED: {q['strategy']} {q['symbol']} {q['timeframe']} "
                            f"{q.get('leverage',1):.1f}x → ROI:{q['roi']:.1%} "
                            f"Sharpe:{q['sharpe']:.2f} DD:{q['max_drawdown']:.1%} "
                            f"WR:{q['win_rate']:.1%} PF:{q['profit_factor']:.2f}"
                        )

                # Summary
                avg_roi = sum(r["roi"] for r in results) / len(results)
                pos_roi = sum(1 for r in results if r["roi"] > 0)
                q50 = sum(1 for r in results if r["roi"] >= TARGET_ROI)
                logger.info(
                    f"Gen {gen} | Pop:{len(results)} | "
                    f"Best ROI:{gen_best['roi']:.1%} ({gen_best['strategy']}) | "
                    f"Avg ROI:{avg_roi:.1%} | Profitable:{pos_roi}/{len(results)} | "
                    f"50%+:{q50} | Total qualified:{len(self.qualified_results)} | "
                    f"Experiments:{self.total_experiments}"
                )

                # Save checkpoint every 10 generations
                if gen % 10 == 9:
                    self._save_report()

                # Target check
                if not self.target_hit and len(self.qualified_results) > 0:
                    self.target_hit = True
                    logger.info("\n" + "=" * 50)
                    logger.info(f"  TARGET HIT! {len(self.qualified_results)} strategies at {TARGET_ROI:.0%}+ ROI")
                    logger.info("=" * 50)

                    # Deep optimize winners
                    improved = self._deep_optimize_winners(self.qualified_results[-5:])
                    self.qualified_results.extend([r for r in improved if is_qualified(r)])

                    # Multi-coin validation on best
                    if improved:
                        best_winner = max(improved, key=lambda x: x["score"])
                        self._multi_coin_validation(best_winner)

                    self._save_report()

                    # Check if agents think they can do better
                    best_roi = max(r["roi"] for r in self.qualified_results)
                    if best_roi < STRETCH_ROI:
                        logger.info(f"\nBest ROI {best_roi:.1%} < stretch target {STRETCH_ROI:.0%}. Continuing...")
                        keep_going_after_target = True
                    else:
                        logger.info(f"\nBest ROI {best_roi:.1%} >= stretch target {STRETCH_ROI:.0%}. Doing final optimization...")
                        # Final deep optimization
                        top_qualified = sorted(self.qualified_results, key=lambda x: x["roi"], reverse=True)[:3]
                        final_improved = self._deep_optimize_winners(top_qualified)
                        self.qualified_results.extend([r for r in final_improved if is_qualified(r)])
                        self._save_report()

                        # If still improving, keep going
                        new_best = max(r["roi"] for r in self.qualified_results)
                        if new_best > best_roi * 1.05:
                            logger.info(f"Still improving ({new_best:.1%} > {best_roi:.1%}). Continuing...")
                            keep_going_after_target = True
                        else:
                            logger.info("Optimization converged. Final results saved.")
                            keep_going_after_target = False

                # If we hit target and can't improve more, stop after a few more gens
                if self.target_hit and not keep_going_after_target:
                    logger.info("Stopping — target achieved and optimization converged.")
                    break

                # Evolve
                elites = results[:ELITE_COUNT]
                population = self._evolve_population(elites)

        except KeyboardInterrupt:
            logger.info("\nInterrupted by user")

        # Final report
        elapsed = (time.time() - self.start_time) / 60
        logger.info(f"\n{'=' * 60}")
        logger.info(f"FINAL RESULTS")
        logger.info(f"{'=' * 60}")
        logger.info(f"Generations: {self.generation + 1}")
        logger.info(f"Total experiments: {self.total_experiments}")
        logger.info(f"Time: {elapsed:.1f} minutes")
        logger.info(f"Qualified strategies (50%+ ROI): {len(self.qualified_results)}")

        if self.best_ever:
            b = self.best_ever
            logger.info(
                f"\nBest overall: {b['strategy']} {b['symbol']} {b['timeframe']} "
                f"{b.get('leverage',1):.1f}x\n"
                f"  ROI: {b['roi']:.1%}\n"
                f"  Sharpe: {b['sharpe']:.2f}\n"
                f"  Sortino: {b.get('sortino',0):.2f}\n"
                f"  Max DD: {b['max_drawdown']:.1%}\n"
                f"  Win Rate: {b['win_rate']:.1%}\n"
                f"  Profit Factor: {b['profit_factor']:.2f}\n"
                f"  Total Trades: {b['total_trades']}\n"
                f"  Score: {b['score']:.2f}"
            )

        if self.qualified_results:
            logger.info("\nQualified strategies:")
            for i, r in enumerate(sorted(self.qualified_results, key=lambda x: x["roi"], reverse=True)[:10], 1):
                logger.info(
                    f"  #{i} {r['strategy']} {r['symbol']} {r['timeframe']} "
                    f"{r.get('leverage',1):.1f}x | ROI:{r['roi']:.1%} "
                    f"Sharpe:{r['sharpe']:.2f} DD:{r['max_drawdown']:.1%} "
                    f"WR:{r['win_rate']:.1%} PF:{r['profit_factor']:.2f}"
                )

        self._save_report()
        logger.info(f"\nReport: artifacts/autonomous_optimizer_report.json")


if __name__ == "__main__":
    optimizer = AutonomousOptimizer()
    optimizer.run()
