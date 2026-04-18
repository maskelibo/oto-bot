"""
╔══════════════════════════════════════════════════════════════╗
║         OVERNIGHT AUTONOMOUS SCALPER EVOLUTION              ║
║         Target: 50%+ Annual ROI                             ║
║         Mode: Research & Backtest ONLY                      ║
║         NO LIVE TRADING — NO PURCHASES                      ║
╚══════════════════════════════════════════════════════════════╝

All agents work continuously:
  - Evolve parameters via mutation of best performers
  - Test across all top coins with 1-year data
  - Each generation: keep top 10, mutate, test offspring
  - Stop when 50%+ ROI achieved with acceptable risk
"""

from __future__ import annotations

import json
import logging
import random
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from oto_bot.agents.ceo import CEOAgent
from oto_bot.agents.debate import AgentDebater
from oto_bot.agents.registry import AgentRegistry
from oto_bot.analytics.scoring import composite_score
from oto_bot.backtest.engine import BacktestEngine, BacktestConfig
from oto_bot.core.models import Hypothesis, ExperimentResult
from oto_bot.experiments.ledger import ExperimentLedger
from oto_bot.governance.risk import RiskGate
from oto_bot.memory.manager import MemoryManager
from oto_bot.strategies.base import StrategyContext
from oto_bot.strategies.scalper_v2 import ScalperV2Strategy

console = Console()
logger = logging.getLogger("oto_bot.overnight")

# ─────────────────────────────────────────────────────────────
# Target
# ─────────────────────────────────────────────────────────────
TARGET_ROI = 0.50  # 50% annual

# Top coins from previous campaign
TOP_COINS = [
    "LTC/USDT", "SUI/USDT", "ADA/USDT", "LINK/USDT", "ALGO/USDT",
    "FET/USDT", "DOGE/USDT", "RENDER/USDT", "AAVE/USDT", "PEPE/USDT",
    "SOL/USDT", "OP/USDT", "INJ/USDT", "WLD/USDT", "NEAR/USDT",
]

TIMEFRAMES = ["15m", "1h", "4h"]

# ─────────────────────────────────────────────────────────────
# Parameter ranges for evolution
# ─────────────────────────────────────────────────────────────
PARAM_RANGES = {
    "bb_period": (10, 30),
    "bb_std": (1.2, 3.0),
    "zscore_period": (10, 30),
    "zscore_entry": (0.5, 2.0),
    "rsi_period": (5, 21),
    "rsi_oversold": (25, 45),
    "rsi_overbought": (55, 75),
    "vwap_period": (10, 40),
    "vwap_dev_threshold": (0.8, 2.5),
    "volume_surge_mult": (1.0, 2.5),
    "adx_max_for_reversion": (20, 40),
    "adx_min_for_reversion": (5, 15),
    "min_confluence_score": (0.30, 0.55),
    "atr_period": (7, 21),
    "atr_sl_multiplier": (0.8, 2.0),
    "atr_tp_multiplier": (1.0, 3.0),
    "wick_ratio_threshold": (0.4, 0.7),
    "htf_ema_period": (30, 100),
}

INT_PARAMS = {"bb_period", "zscore_period", "rsi_period", "rsi_oversold",
              "rsi_overbought", "vwap_period", "atr_period", "adx_min_for_reversion",
              "adx_max_for_reversion", "htf_ema_period"}


def random_params() -> dict:
    """Generate random parameters within ranges."""
    p = {}
    for key, (lo, hi) in PARAM_RANGES.items():
        val = random.uniform(lo, hi)
        if key in INT_PARAMS:
            val = int(round(val))
        else:
            val = round(val, 2)
        p[key] = val
    # Ensure rsi_oversold < rsi_overbought
    if p["rsi_oversold"] >= p["rsi_overbought"]:
        p["rsi_oversold"], p["rsi_overbought"] = 30, 70
    if p["adx_min_for_reversion"] >= p["adx_max_for_reversion"]:
        p["adx_min_for_reversion"] = 10
        p["adx_max_for_reversion"] = 30
    return p


def mutate_params(base: dict, strength: float = 0.2) -> dict:
    """Mutate params — small perturbations of a working config."""
    child = {}
    for key, (lo, hi) in PARAM_RANGES.items():
        base_val = base.get(key, (lo + hi) / 2)
        range_size = hi - lo
        noise = random.gauss(0, strength * range_size)
        new_val = base_val + noise
        new_val = max(lo, min(hi, new_val))
        if key in INT_PARAMS:
            new_val = int(round(new_val))
        else:
            new_val = round(new_val, 2)
        child[key] = new_val
    # Fix constraints
    if child["rsi_oversold"] >= child["rsi_overbought"]:
        child["rsi_oversold"] = child["rsi_overbought"] - 10
    if child["adx_min_for_reversion"] >= child["adx_max_for_reversion"]:
        child["adx_min_for_reversion"] = child["adx_max_for_reversion"] - 10
    return child


def crossover(parent1: dict, parent2: dict) -> dict:
    """Uniform crossover between two parent configs."""
    child = {}
    for key in PARAM_RANGES:
        if random.random() < 0.5:
            child[key] = parent1.get(key, random_params()[key])
        else:
            child[key] = parent2.get(key, random_params()[key])
    return child


# ─────────────────────────────────────────────────────────────
# Data cache (avoid re-fetching same coin)
# ─────────────────────────────────────────────────────────────
_data_cache: dict[str, pd.DataFrame] = {}


def fetch_data_cached(symbol: str, timeframe: str) -> pd.DataFrame | None:
    """Fetch 1-year data with caching."""
    cache_key = f"{symbol}_{timeframe}"
    if cache_key in _data_cache:
        return _data_cache[cache_key]

    try:
        from oto_bot.data.crypto import CryptoDataProvider
        provider = CryptoDataProvider()
        # Fetch from early 2025 — as much as we can get
        data = provider.fetch_ohlcv(symbol, timeframe, since="2025-01-01", limit=1000)
        if data is not None and len(data) >= 50:
            _data_cache[cache_key] = data
            return data
    except Exception as e:
        logger.warning(f"Failed to fetch {symbol} {timeframe}: {e}")
    return None


# ─────────────────────────────────────────────────────────────
# Single experiment runner
# ─────────────────────────────────────────────────────────────

def run_experiment(
    symbol: str,
    timeframe: str,
    params: dict,
    capital: float = 10_000.0,
    leverage: float = 1.0,
) -> dict | None:
    """Run one backtest experiment. Returns result dict or None."""
    data = fetch_data_cached(symbol, timeframe)
    if data is None:
        return None

    params_full = {**params, "capital": capital, "risk_per_trade": 0.01,
                   "base_leverage": leverage, "max_leverage": 5.0}
    context = StrategyContext(
        market="crypto", strategy_family="scalp",
        symbol=symbol, timeframe=timeframe, params=params_full,
    )
    config = BacktestConfig(
        fee_bps=10.0, slippage_bps=3.0,
        initial_capital=capital, risk_per_trade=0.01,
        use_position_sizing=True,
    )

    try:
        engine = BacktestEngine(config)
        strategy = ScalperV2Strategy()
        result = engine.run(strategy, data, context)

        # Apply leverage effect
        result.roi *= leverage
        result.cagr *= leverage
        result.max_drawdown *= leverage
        result.sharpe *= (leverage ** 0.5)
        result.sortino *= (leverage ** 0.5)

        score = composite_score(result)

        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "params": params,
            "leverage": leverage,
            "result": result,
            "score": score,
            "roi": result.roi,
            "sharpe": result.sharpe,
            "max_drawdown": result.max_drawdown,
            "win_rate": result.win_rate,
            "profit_factor": result.profit_factor,
            "total_trades": result.total_trades,
        }
    except Exception as e:
        logger.error(f"Experiment failed {symbol} {timeframe}: {e}")
        return None


# ─────────────────────────────────────────────────────────────
# Evolutionary optimizer
# ─────────────────────────────────────────────────────────────

class OvernightEvolver:
    """Genetic algorithm-style parameter evolution for scalper."""

    def __init__(
        self,
        capital: float = 10_000.0,
        population_size: int = 30,
        elite_count: int = 8,
        max_generations: int = 100,
        target_roi: float = TARGET_ROI,
    ):
        self.capital = capital
        self.pop_size = population_size
        self.elite_count = elite_count
        self.max_generations = max_generations
        self.target_roi = target_roi

        # Components
        self.registry = AgentRegistry()
        self.registry.seed_defaults()
        self.memory = MemoryManager()
        self.ceo = CEOAgent(self.registry, memory_manager=self.memory)
        self.ledger = ExperimentLedger()
        self.risk_gate = RiskGate()
        self.debater = AgentDebater()

        # State
        self.generation = 0
        self.total_experiments = 0
        self.all_results: list[dict] = []
        self.best_ever: dict | None = None
        self.generation_bests: list[dict] = []
        self.target_hit = False

        # Logging
        Path("logs").mkdir(exist_ok=True)
        fh = logging.FileHandler("logs/overnight_evolution.log")
        fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
        logger.addHandler(fh)
        logger.setLevel(logging.INFO)

    def _evaluate_population(self, population: list[dict]) -> list[dict]:
        """Run backtests for all members of the population."""
        results: list[dict] = []

        for member in population:
            params = member["params"]
            symbol = member["symbol"]
            timeframe = member["timeframe"]
            leverage = member.get("leverage", 1.0)

            exp = run_experiment(symbol, timeframe, params, self.capital, leverage)
            if exp is not None:
                results.append(exp)
                self.all_results.append(exp)
                self.total_experiments += 1

                # Save to memory
                self.memory.save_result(exp["result"])
                self.ledger.log({
                    "campaign": "overnight_evolution",
                    "generation": self.generation,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "leverage": leverage,
                    "score": exp["score"],
                    "roi": exp["roi"],
                    "sharpe": exp["sharpe"],
                    "max_drawdown": exp["max_drawdown"],
                    "total_trades": exp["total_trades"],
                    "params": params,
                })

        return sorted(results, key=lambda x: x["score"], reverse=True)

    def _create_initial_population(self) -> list[dict]:
        """Gen 0: random params across top coins."""
        population = []
        for _ in range(self.pop_size):
            coin = random.choice(TOP_COINS)
            tf = random.choice(TIMEFRAMES)
            params = random_params()
            leverage = random.choice([1.0, 2.0, 3.0])
            population.append({
                "symbol": coin, "timeframe": tf,
                "params": params, "leverage": leverage,
            })
        return population

    def _evolve_population(self, elites: list[dict]) -> list[dict]:
        """Create next generation from elites."""
        next_gen: list[dict] = []

        # Keep elites as-is (but test on different coins too)
        for elite in elites:
            next_gen.append({
                "symbol": elite["symbol"],
                "timeframe": elite["timeframe"],
                "params": elite["params"],
                "leverage": elite.get("leverage", 1.0),
            })

        # Mutations of elites
        while len(next_gen) < self.pop_size * 0.6:
            parent = random.choice(elites)
            child_params = mutate_params(parent["params"], strength=0.15)
            # Sometimes try on different coin/tf
            if random.random() < 0.3:
                coin = random.choice(TOP_COINS)
                tf = random.choice(TIMEFRAMES)
            else:
                coin = parent["symbol"]
                tf = parent["timeframe"]
            # Vary leverage
            leverage = parent.get("leverage", 1.0)
            if random.random() < 0.2:
                leverage = random.choice([1.0, 2.0, 3.0, 4.0, 5.0])
            next_gen.append({
                "symbol": coin, "timeframe": tf,
                "params": child_params, "leverage": leverage,
            })

        # Crossovers
        while len(next_gen) < self.pop_size * 0.85:
            p1, p2 = random.sample(elites, min(2, len(elites)))
            child_params = crossover(p1["params"], p2["params"])
            coin = random.choice([p1["symbol"], p2["symbol"]])
            tf = random.choice([p1["timeframe"], p2["timeframe"]])
            leverage = random.choice([p1.get("leverage", 1.0), p2.get("leverage", 1.0)])
            next_gen.append({
                "symbol": coin, "timeframe": tf,
                "params": child_params, "leverage": leverage,
            })

        # Fresh randoms (exploration)
        while len(next_gen) < self.pop_size:
            coin = random.choice(TOP_COINS)
            tf = random.choice(TIMEFRAMES)
            params = random_params()
            leverage = random.choice([1.0, 2.0, 3.0])
            next_gen.append({
                "symbol": coin, "timeframe": tf,
                "params": params, "leverage": leverage,
            })

        return next_gen

    def _check_target(self, results: list[dict]) -> bool:
        """Check if any result hits 50%+ ROI with acceptable risk."""
        for r in results:
            if (r["roi"] >= self.target_roi
                    and r["sharpe"] >= 1.0
                    and r["max_drawdown"] >= -0.25
                    and r["total_trades"] >= 10
                    and r["win_rate"] >= 0.40):
                return True
        return False

    def _print_generation_summary(self, gen: int, results: list[dict]) -> None:
        """Print summary of current generation."""
        if not results:
            console.print(f"[red]Gen {gen}: No results[/red]")
            return

        best = results[0]
        avg_score = sum(r["score"] for r in results) / len(results)
        avg_roi = sum(r["roi"] for r in results) / len(results)
        positive_roi = sum(1 for r in results if r["roi"] > 0)
        hit_50 = sum(1 for r in results if r["roi"] >= self.target_roi)

        console.print(
            f"[bold]Gen {gen}[/bold] | "
            f"Pop: {len(results)} | "
            f"Best: {best['symbol']} {best['timeframe']} {best.get('leverage', 1)}x → "
            f"Score:{best['score']:.2f} ROI:{best['roi']:.1%} "
            f"Sharpe:{best['sharpe']:.2f} DD:{best['max_drawdown']:.1%} "
            f"WR:{best['win_rate']:.1%} Trades:{best['total_trades']} | "
            f"Avg ROI:{avg_roi:.1%} | "
            f"Profitable:{positive_roi}/{len(results)} | "
            f"50%+:{hit_50}"
        )

    def _print_top_table(self, results: list[dict], title: str = "Top 10") -> None:
        """Print top results table."""
        table = Table(title=title, show_header=True, header_style="bold magenta")
        table.add_column("#", width=3)
        table.add_column("Coin", style="cyan")
        table.add_column("TF")
        table.add_column("Lev", justify="right")
        table.add_column("Score", justify="right", style="bold")
        table.add_column("ROI", justify="right")
        table.add_column("Sharpe", justify="right")
        table.add_column("WR", justify="right")
        table.add_column("DD", justify="right", style="red")
        table.add_column("PF", justify="right")
        table.add_column("Trades", justify="right")

        for i, r in enumerate(results[:10], 1):
            roi_style = "green" if r["roi"] >= self.target_roi else "white"
            table.add_row(
                str(i), r["symbol"], r["timeframe"],
                f"{r.get('leverage', 1):.0f}x",
                f"{r['score']:.2f}",
                f"[{roi_style}]{r['roi']:.1%}[/{roi_style}]",
                f"{r['sharpe']:.2f}",
                f"{r['win_rate']:.1%}",
                f"{r['max_drawdown']:.1%}",
                f"{r['profit_factor']:.2f}",
                str(r["total_trades"]),
            )
        console.print(table)

    def _debate_and_save_winners(self, winners: list[dict]) -> None:
        """CEO debates top winners and saves to report."""
        console.print("\n[bold cyan]═══ CEO DEBATE ON WINNERS ═══[/bold cyan]")

        from dataclasses import asdict
        for i, w in enumerate(winners[:5], 1):
            result = w["result"]
            result_data = asdict(result)
            result_data["created_at"] = result.created_at.isoformat()
            result_data["leverage"] = w.get("leverage", 1.0)

            debate = self.debater.debate(
                topic=f"Winner #{i}: {w['symbol']} {w['timeframe']} {w.get('leverage',1)}x — ROI {w['roi']:.1%}",
                experiment_result=result_data,
                memory_manager=self.memory,
            )

            decision = self.ceo.review_experiment(result)

            console.print(
                f"\n  [bold]#{i} {w['symbol']} {w['timeframe']} {w.get('leverage',1)}x[/bold]"
                f" | ROI:{w['roi']:.1%} Sharpe:{w['sharpe']:.2f}"
            )
            console.print(f"  Debate: {debate.conclusion}")
            console.print(f"  CEO: {decision.decision} — {decision.reasoning[:80]}")

    def _save_report(self) -> None:
        """Save final report to artifacts."""
        all_sorted = sorted(self.all_results, key=lambda x: x["score"], reverse=True)

        report = {
            "campaign": "overnight_evolution",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "generations": self.generation,
            "total_experiments": self.total_experiments,
            "target_roi": self.target_roi,
            "target_hit": self.target_hit,
            "best_ever": {
                "symbol": self.best_ever["symbol"],
                "timeframe": self.best_ever["timeframe"],
                "leverage": self.best_ever.get("leverage", 1.0),
                "params": self.best_ever["params"],
                "roi": self.best_ever["roi"],
                "sharpe": self.best_ever["sharpe"],
                "max_drawdown": self.best_ever["max_drawdown"],
                "win_rate": self.best_ever["win_rate"],
                "profit_factor": self.best_ever["profit_factor"],
                "total_trades": self.best_ever["total_trades"],
                "score": self.best_ever["score"],
            } if self.best_ever else None,
            "generation_progress": [
                {
                    "gen": g.get("gen", i),
                    "best_roi": g.get("roi", 0),
                    "best_score": g.get("score", 0),
                }
                for i, g in enumerate(self.generation_bests)
            ],
            "top_20": [
                {
                    "symbol": r["symbol"],
                    "timeframe": r["timeframe"],
                    "leverage": r.get("leverage", 1.0),
                    "params": r["params"],
                    "roi": r["roi"],
                    "sharpe": r["sharpe"],
                    "max_drawdown": r["max_drawdown"],
                    "win_rate": r["win_rate"],
                    "profit_factor": r["profit_factor"],
                    "total_trades": r["total_trades"],
                    "score": r["score"],
                }
                for r in all_sorted[:20]
            ],
        }

        report_path = Path("artifacts/overnight_evolution_report.json")
        report_path.parent.mkdir(exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, default=str))
        console.print(f"\n[dim]Report saved: {report_path}[/dim]")

    # ─── Main loop ───────────────────────────────────────────

    def run(self) -> None:
        """Run the overnight evolutionary optimization."""
        console.print("\n[bold green]" + "═" * 60 + "[/bold green]")
        console.print("[bold green]  OVERNIGHT SCALPER EVOLUTION[/bold green]")
        console.print(f"[bold green]  Target: {self.target_roi:.0%} Annual ROI[/bold green]")
        console.print(f"[bold green]  Capital: ${self.capital:,.0f}[/bold green]")
        console.print(f"[bold green]  Population: {self.pop_size} | Generations: {self.max_generations}[/bold green]")
        console.print(f"[bold green]  Mode: RESEARCH ONLY — NO LIVE TRADING[/bold green]")
        console.print("[bold green]" + "═" * 60 + "[/bold green]\n")

        start_time = time.time()

        # Gen 0: random population
        console.print("[bold]Generating initial population...[/bold]")
        population = self._create_initial_population()

        try:
            for gen in range(self.max_generations):
                self.generation = gen

                # Evaluate
                results = self._evaluate_population(population)
                if not results:
                    console.print(f"[red]Gen {gen}: All experiments failed. Retrying...[/red]")
                    population = self._create_initial_population()
                    continue

                # Track best
                gen_best = results[0]
                self.generation_bests.append({**gen_best, "gen": gen})
                if self.best_ever is None or gen_best["score"] > self.best_ever["score"]:
                    self.best_ever = gen_best
                    console.print(f"  [bold yellow]★ NEW BEST: {gen_best['symbol']} {gen_best['timeframe']} — ROI:{gen_best['roi']:.1%} Sharpe:{gen_best['sharpe']:.2f}[/bold yellow]")

                # Summary
                self._print_generation_summary(gen, results)

                # Check target
                if self._check_target(results):
                    self.target_hit = True
                    winners = [r for r in results
                               if r["roi"] >= self.target_roi
                               and r["sharpe"] >= 1.0
                               and r["max_drawdown"] >= -0.25]
                    console.print(f"\n[bold green]{'═' * 50}[/bold green]")
                    console.print(f"[bold green]  TARGET HIT! {len(winners)} strategies at 50%+ ROI[/bold green]")
                    console.print(f"[bold green]{'═' * 50}[/bold green]")
                    self._print_top_table(winners, "50%+ ROI Winners")
                    self._debate_and_save_winners(winners)
                    self._save_report()

                    elapsed = time.time() - start_time
                    console.print(f"\n[bold]Done in {elapsed/60:.1f} minutes, {self.total_experiments} experiments, {gen+1} generations[/bold]")
                    return

                # Every 5 generations: show top table
                if gen % 5 == 4:
                    all_sorted = sorted(self.all_results, key=lambda x: x["score"], reverse=True)
                    self._print_top_table(all_sorted, f"All-Time Top 10 (Gen {gen})")

                # Evolve
                elites = results[:self.elite_count]
                population = self._evolve_population(elites)

        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted by user[/yellow]")

        # Final report even if target not hit
        console.print(f"\n[bold]Completed {self.generation + 1} generations, {self.total_experiments} experiments[/bold]")
        if self.best_ever:
            console.print(f"Best ever: {self.best_ever['symbol']} {self.best_ever['timeframe']} — ROI:{self.best_ever['roi']:.1%}")

        all_sorted = sorted(self.all_results, key=lambda x: x["score"], reverse=True)
        self._print_top_table(all_sorted, "Final Top 10")
        self._debate_and_save_winners(all_sorted[:5])
        self._save_report()

        elapsed = time.time() - start_time
        console.print(f"\nTotal time: {elapsed/60:.1f} minutes")
