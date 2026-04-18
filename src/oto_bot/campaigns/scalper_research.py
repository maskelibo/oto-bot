"""
╔══════════════════════════════════════════════════════════════╗
║           SCALPER BOT RESEARCH CAMPAIGN                     ║
║           CEO: Atlas CEO — Full Agent Mobilization          ║
║           Target: All Binance USDT pairs since 2025         ║
║           Leverage: Dynamic (1x-5x based on probability)    ║
╚══════════════════════════════════════════════════════════════╝

Agent Assignments:
  - Atlas CEO: orchestration, final decisions, hiring/firing
  - Nova StrategyRND: hypothesis generation, parameter design
  - Sigma Quant: statistical validation, overfitting checks
  - Helix Backtest: simulation execution, walk-forward
  - Sentinel Risk: risk gate enforcement, kill-switch
  - Pulse Analytics: scoring, comparison, trend analysis
  - Vega MarketIntel: market regime detection, session analysis
  - Archive Memory: experiment logging, failure compression
  - Forge Execution: paper trading readiness checks
  - Iris ChiefOfStaff: coordination, progress tracking
"""

from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime, timezone
from itertools import product
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
from oto_bot.analytics.scoring import composite_score, rank_experiments
from oto_bot.backtest.engine import BacktestEngine, BacktestConfig
from oto_bot.core.models import Hypothesis, ExperimentResult
from oto_bot.experiments.ledger import ExperimentLedger
from oto_bot.governance.risk import RiskGate
from oto_bot.memory.manager import MemoryManager
from oto_bot.strategies.base import StrategyContext
from oto_bot.strategies.scalper_v2 import ScalperV2Strategy

console = Console()
logger = logging.getLogger("oto_bot.campaign.scalper")

# ─────────────────────────────────────────────────────────────
# Coin universe — top Binance USDT pairs by liquidity
# ─────────────────────────────────────────────────────────────

# Full universe
COIN_UNIVERSE_FULL = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "DOGE/USDT",
    "BNB/USDT", "ADA/USDT", "TRX/USDT", "LINK/USDT", "AVAX/USDT",
    "SUI/USDT", "TON/USDT", "NEAR/USDT", "PEPE/USDT", "WLD/USDT",
    "FET/USDT", "DOT/USDT", "UNI/USDT", "AAVE/USDT",
    "LTC/USDT", "BCH/USDT", "APT/USDT", "FIL/USDT", "ARB/USDT",
    "OP/USDT", "INJ/USDT", "ATOM/USDT", "ALGO/USDT", "RENDER/USDT",
]

# Top performers from Phase 1 — focus here for deeper research
COIN_UNIVERSE = [
    "PEPE/USDT", "RENDER/USDT", "SUI/USDT", "WLD/USDT", "ADA/USDT",
    "AAVE/USDT", "DOGE/USDT", "INJ/USDT", "OP/USDT", "ALGO/USDT",
    "FET/USDT", "LINK/USDT", "LTC/USDT", "NEAR/USDT", "SOL/USDT",
]

TIMEFRAMES = ["5m", "15m", "1h"]

# ─────────────────────────────────────────────────────────────
# Dynamic leverage table (prob → leverage)
# ─────────────────────────────────────────────────────────────

def get_dynamic_leverage(prob: float, base: float = 1.0, max_lev: float = 5.0) -> float:
    """Atlas-style dynamic leverage based on signal probability/confidence."""
    if prob >= 0.80:
        return min(5.0, max_lev)
    elif prob >= 0.75:
        return min(4.0, max_lev)
    elif prob >= 0.70:
        return min(3.0, max_lev)
    elif prob >= 0.65:
        return min(2.0, max_lev)
    else:
        return base


def get_kelly_risk(prob: float, risk_min: float = 0.005, risk_max: float = 0.02) -> float:
    """Half-Kelly position sizing: risk scales with confidence."""
    if prob < 0.55:
        return risk_min
    risk = risk_min + (prob - 0.55) / 0.25 * (risk_max - risk_min)
    return min(risk_max, max(risk_min, risk))


# ─────────────────────────────────────────────────────────────
# Parameter search space for scalper
# ─────────────────────────────────────────────────────────────

PARAM_GRID = {
    # Bollinger
    "bb_period": [15, 20, 25],
    "bb_std": [1.5, 2.0, 2.5],
    # Z-score
    "zscore_period": [14, 20],
    "zscore_entry": [0.8, 1.0, 1.2],
    # RSI
    "rsi_period": [7, 14],
    "rsi_oversold": [30, 35, 40],
    "rsi_overbought": [60, 65, 70],
    # VWAP
    "vwap_period": [15, 20, 30],
    "vwap_dev_threshold": [1.0, 1.5, 2.0],
    # Volume
    "volume_surge_mult": [1.3, 1.5, 2.0],
    # ADX
    "adx_max_for_reversion": [25, 30, 35],
    # Confluence
    "min_confluence_score": [0.35, 0.40, 0.45, 0.50],
    # ATR
    "atr_period": [10, 14],
    "atr_sl_multiplier": [1.0, 1.2, 1.5],
    "atr_tp_multiplier": [1.5, 2.0, 2.5],
}


def _random_params() -> dict:
    """Generate a random parameter combination from the grid."""
    return {k: random.choice(v) for k, v in PARAM_GRID.items()}


def _mutate_params(base: dict, mutation_rate: float = 0.3) -> dict:
    """Mutate a parameter set — keep most values, change some."""
    mutated = base.copy()
    for key in PARAM_GRID:
        if random.random() < mutation_rate:
            mutated[key] = random.choice(PARAM_GRID[key])
    return mutated


# ─────────────────────────────────────────────────────────────
# Data fetcher with 2025+ range
# ─────────────────────────────────────────────────────────────

def fetch_coin_data(symbol: str, timeframe: str, since: str = "2025-01-01") -> pd.DataFrame | None:
    """Fetch real Binance data from 2025 onwards."""
    try:
        from oto_bot.data.crypto import CryptoDataProvider
        provider = CryptoDataProvider()
        data = provider.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
        if data is not None and len(data) >= 100:
            return data
    except Exception as e:
        logger.warning(f"Failed to fetch {symbol} {timeframe}: {e}")
    return None


# ─────────────────────────────────────────────────────────────
# Campaign runner
# ─────────────────────────────────────────────────────────────

class ScalperResearchCampaign:
    """
    Full-scale scalper bot research campaign.

    CEO organizes all agents:
    1. Vega MarketIntel scans which coins have good volatility
    2. Nova StrategyRND proposes parameter hypotheses
    3. Helix Backtest runs simulations with leverage
    4. Sigma Quant validates statistical significance
    5. Sentinel Risk enforces all gates
    6. Pulse Analytics scores and ranks
    7. Archive Memory stores everything
    8. CEO debates and decides
    """

    def __init__(
        self,
        coins: list[str] | None = None,
        timeframes: list[str] | None = None,
        max_experiments: int = 200,
        initial_capital: float = 10_000.0,
        max_leverage: float = 5.0,
    ):
        self.coins = coins or COIN_UNIVERSE
        self.timeframes = timeframes or TIMEFRAMES
        self.max_experiments = max_experiments
        self.initial_capital = initial_capital
        self.max_leverage = max_leverage

        # Initialize all agents
        self.registry = AgentRegistry()
        self.registry.seed_defaults()
        self.memory = MemoryManager()
        self.ceo = CEOAgent(self.registry, memory_manager=self.memory)
        self.ledger = ExperimentLedger()
        self.risk_gate = RiskGate()
        self.debater = AgentDebater()

        # Campaign state
        self.experiments: list[dict] = []
        self.best_configs: list[dict] = []
        self.coin_scores: dict[str, list[float]] = {}
        self.failed_combos: set[str] = set()
        self.total_run = 0
        self.btc_regime: str = "unknown"  # BTC regime filter

        # Setup logging
        Path("logs").mkdir(exist_ok=True)
        fh = logging.FileHandler("logs/scalper_campaign.log")
        fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
        logger.addHandler(fh)
        logger.setLevel(logging.INFO)

    # ─── Phase 1: Market Intelligence Scan ───────────────────

    def phase_market_scan(self) -> list[dict]:
        """Vega MarketIntel: scan coins for volatility & tradability."""
        console.print(Panel(
            "[bold cyan]PHASE 1: MARKET INTELLIGENCE SCAN[/bold cyan]\n"
            "Agent: Vega MarketIntel\n"
            f"Scanning {len(self.coins)} coins across {len(self.timeframes)} timeframes",
            title="[bold]CEO ORDER",
        ))

        viable_coins: list[dict] = []

        for coin in self.coins:
            try:
                data = fetch_coin_data(coin, "1h")
                if data is None or len(data) < 100:
                    console.print(f"  [dim]{coin}: insufficient data, skipping[/dim]")
                    continue

                # Volatility analysis
                returns = data["close"].pct_change().dropna()
                volatility = float(returns.std()) * np.sqrt(24)  # daily vol
                avg_volume = float(data["volume"].mean())
                price = float(data["close"].iloc[-1])

                # Regime detection
                ema20 = data["close"].ewm(span=20).mean()
                ema50 = data["close"].ewm(span=50).mean()
                trend = "bull" if float(ema20.iloc[-1]) > float(ema50.iloc[-1]) else "bear"

                # Mean reversion potential (key for scalper)
                from ta.volatility import BollingerBands
                bb = BollingerBands(data["close"], window=20, window_dev=2)
                bb_width = (bb.bollinger_hband() - bb.bollinger_lband()) / bb.bollinger_mavg()
                avg_bb_width = float(bb_width.dropna().mean())

                coin_info = {
                    "symbol": coin,
                    "volatility": round(volatility, 4),
                    "avg_volume": avg_volume,
                    "price": price,
                    "trend": trend,
                    "bb_width": round(avg_bb_width, 4),
                    "bars": len(data),
                    "score": round(volatility * avg_bb_width * 100, 2),  # volatility × band width
                }
                viable_coins.append(coin_info)
                console.print(
                    f"  [green]✓[/green] {coin}: vol={volatility:.3f} | "
                    f"bb_width={avg_bb_width:.3f} | trend={trend} | "
                    f"score={coin_info['score']:.2f}"
                )

            except Exception as e:
                console.print(f"  [red]✗[/red] {coin}: {e}")
                continue

        # BTC regime detection
        btc_data = fetch_coin_data("BTC/USDT", "1h")
        if btc_data is not None and len(btc_data) > 50:
            ema20 = btc_data["close"].ewm(span=20).mean()
            ema50 = btc_data["close"].ewm(span=50).mean()
            if float(ema20.iloc[-1]) > float(ema50.iloc[-1]):
                self.btc_regime = "bull"
            else:
                self.btc_regime = "bear"
        console.print(f"\n[bold]BTC Regime: {self.btc_regime.upper()}[/bold]")

        # Sort by scalper suitability score
        viable_coins.sort(key=lambda x: x["score"], reverse=True)
        console.print(f"[bold green]Market scan complete: {len(viable_coins)} viable coins[/bold green]")
        return viable_coins

    # ─── Phase 2: Hypothesis Generation ──────────────────────

    def phase_generate_hypotheses(self, viable_coins: list[dict], n_hypotheses: int = 50) -> list[dict]:
        """Nova StrategyRND: generate parameter hypotheses for top coins."""
        console.print(Panel(
            "[bold cyan]PHASE 2: HYPOTHESIS GENERATION[/bold cyan]\n"
            "Agent: Nova StrategyRND\n"
            f"Generating {n_hypotheses} hypotheses for top coins",
            title="[bold]CEO ORDER",
        ))

        hypotheses: list[dict] = []
        top_coins = viable_coins[:15]  # Focus on top 15

        for i in range(n_hypotheses):
            coin = random.choice(top_coins)
            timeframe = random.choice(self.timeframes)
            params = _random_params()

            # Probability proxy based on volatility & band width
            base_prob = 0.55 + coin["score"] / 100 * 0.15  # 0.55 to 0.70 range
            prob = min(0.80, max(0.55, base_prob + random.uniform(-0.05, 0.05)))

            leverage = get_dynamic_leverage(prob, max_lev=self.max_leverage)
            risk_pct = get_kelly_risk(prob)

            hyp = {
                "id": i,
                "symbol": coin["symbol"],
                "timeframe": timeframe,
                "params": params,
                "probability": round(prob, 3),
                "leverage": leverage,
                "risk_per_trade": risk_pct,
                "coin_score": coin["score"],
                "trend": coin["trend"],
            }
            hypotheses.append(hyp)

        console.print(f"[green]Generated {len(hypotheses)} hypotheses[/green]")
        # Show leverage distribution
        lev_dist = {}
        for h in hypotheses:
            l = h["leverage"]
            lev_dist[l] = lev_dist.get(l, 0) + 1
        console.print(f"Leverage distribution: {lev_dist}")
        return hypotheses

    # ─── Phase 3: Backtest Execution ─────────────────────────

    def phase_backtest(self, hypotheses: list[dict]) -> list[dict]:
        """Helix Backtest + Sigma Quant: run and validate backtests."""
        console.print(Panel(
            "[bold cyan]PHASE 3: BACKTEST EXECUTION[/bold cyan]\n"
            "Agents: Helix Backtest (simulation) + Sigma Quant (validation)\n"
            f"Running {len(hypotheses)} experiments with leverage",
            title="[bold]CEO ORDER",
        ))

        results: list[dict] = []
        strategy = ScalperV2Strategy()

        for i, hyp in enumerate(hypotheses):
            if self.total_run >= self.max_experiments:
                console.print("[yellow]Max experiments reached[/yellow]")
                break

            symbol = hyp["symbol"]
            tf = hyp["timeframe"]
            params = hyp["params"]
            leverage = hyp["leverage"]
            risk_pct = hyp["risk_per_trade"]

            # Skip combos that already failed badly
            combo_key = f"{symbol}_{tf}"
            fail_count = sum(1 for f in self.failed_combos if f.startswith(combo_key))
            if fail_count >= 3:
                continue

            # Fetch data
            data = fetch_coin_data(symbol, tf)
            if data is None:
                continue

            # Configure backtest with leverage
            config = BacktestConfig(
                fee_bps=10.0,
                slippage_bps=3.0,
                initial_capital=self.initial_capital,
                risk_per_trade=risk_pct,
                use_position_sizing=True,
            )

            # Add leverage and risk params to strategy context
            params["capital"] = self.initial_capital
            params["risk_per_trade"] = risk_pct
            context = StrategyContext(
                market="crypto",
                strategy_family="scalp",
                symbol=symbol,
                timeframe=tf,
                params=params,
            )

            try:
                engine = BacktestEngine(config)
                result = engine.run(strategy, data, context)

                # Simulate leverage effect on returns
                result.roi *= leverage
                result.cagr *= leverage
                result.max_drawdown *= leverage
                result.sharpe *= (leverage ** 0.5)  # Sharpe scales with sqrt(leverage)
                result.sortino *= (leverage ** 0.5)

                # Risk gate
                risk_report = self.risk_gate.approve(result)
                score = composite_score(result)

                # Store
                experiment = {
                    "hypothesis": hyp,
                    "result": result,
                    "risk_report": risk_report,
                    "score": score,
                    "leverage": leverage,
                    "probability": hyp["probability"],
                }
                results.append(experiment)
                self.experiments.append(experiment)
                self.total_run += 1

                # Track coin performance
                self.coin_scores.setdefault(symbol, []).append(score)

                # Track failures
                if score < -0.5:
                    self.failed_combos.add(f"{combo_key}_{json.dumps(params, sort_keys=True)[:50]}")

                # Save to memory
                self.memory.save_result(result)

                # Log
                hypothesis_obj = Hypothesis(
                    title=f"scalper_{symbol}_{tf}_lev{leverage}x_v{self.total_run}",
                    thesis=f"Scalper mean-reversion on {symbol} ({tf}) with {leverage}x leverage. Params: {params}",
                    market="crypto",
                    strategy_family="scalp",
                    timeframe=tf,
                    invalidation="Risk gates fail or drawdown exceeds limits",
                    expected_edge=f"Mean-reversion with BB/RSI confluence. Prob={hyp['probability']:.2f}",
                    author_agent_id="campaign",
                )
                self.memory.save_hypothesis(hypothesis_obj)
                self.ledger.log({
                    "campaign": "scalper_research",
                    "symbol": symbol,
                    "timeframe": tf,
                    "leverage": leverage,
                    "params": params,
                    "score": score,
                    "roi": result.roi,
                    "sharpe": result.sharpe,
                    "max_drawdown": result.max_drawdown,
                    "total_trades": result.total_trades,
                    "risk_approved": risk_report.approved,
                })

                # Print progress
                status = "[green]✓ PASS[/green]" if risk_report.approved else "[red]✗ FAIL[/red]"
                console.print(
                    f"  [{self.total_run}/{self.max_experiments}] "
                    f"{symbol} {tf} {leverage}x | "
                    f"Score:{score:.3f} Sharpe:{result.sharpe:.2f} "
                    f"ROI:{result.roi:.1%} DD:{result.max_drawdown:.1%} "
                    f"Trades:{result.total_trades} | {status}"
                )

            except Exception as e:
                logger.error(f"Backtest failed for {symbol} {tf}: {e}")
                console.print(f"  [red]ERROR[/red] {symbol} {tf}: {e}")
                continue

        console.print(f"\n[bold green]Backtest phase complete: {len(results)} experiments[/bold green]")
        return results

    # ─── Phase 4: Agent Debate & CEO Decision ────────────────

    def phase_debate_and_decide(self, results: list[dict]) -> list[dict]:
        """All agents debate top results. CEO makes final promotion decisions."""
        console.print(Panel(
            "[bold cyan]PHASE 4: AGENT DEBATE & CEO DECISIONS[/bold cyan]\n"
            "All agents participate in debate\n"
            "CEO makes final promotion decisions",
            title="[bold]CEO ORDER",
        ))

        # Sort by score, take top 10 for debate
        sorted_results = sorted(results, key=lambda x: x["score"], reverse=True)
        top_candidates = sorted_results[:10]

        promoted: list[dict] = []

        for i, exp in enumerate(top_candidates, 1):
            result = exp["result"]
            hyp = exp["hypothesis"]
            score = exp["score"]
            leverage = exp["leverage"]

            console.print(f"\n[bold]--- Candidate {i}: {hyp['symbol']} {hyp['timeframe']} {leverage}x ---[/bold]")
            console.print(f"  Score: {score:.3f} | Sharpe: {result.sharpe:.2f} | ROI: {result.roi:.1%}")

            # Full agent debate
            from dataclasses import asdict
            result_data = asdict(result)
            result_data["created_at"] = result.created_at.isoformat()
            result_data["leverage"] = leverage
            result_data["probability"] = hyp["probability"]

            debate = self.debater.debate(
                topic=f"Promote scalper on {hyp['symbol']} ({hyp['timeframe']}) with {leverage}x leverage?",
                experiment_result=result_data,
                memory_manager=self.memory,
            )

            # Print debate arguments
            for arg in debate.arguments:
                position_color = "green" if arg["position"] in ("acceptable", "confident", "supportive", "positive") else "yellow"
                console.print(f"  [{position_color}]{arg['agent_name']}[/{position_color}]: {arg['position']} — {arg['reasoning'][:80]}")

            console.print(f"  [bold]CEO Conclusion:[/bold] {debate.conclusion}")

            # CEO decision
            decision = self.ceo.review_experiment(result)
            console.print(f"  [bold]Decision:[/bold] {decision.decision}")
            console.print(f"  [dim]Reasoning: {decision.reasoning[:100]}[/dim]")

            if decision.decision == "promote_to_paper_trading":
                promoted.append({
                    "symbol": hyp["symbol"],
                    "timeframe": hyp["timeframe"],
                    "params": hyp["params"],
                    "leverage": leverage,
                    "probability": hyp["probability"],
                    "risk_per_trade": hyp["risk_per_trade"],
                    "score": score,
                    "sharpe": result.sharpe,
                    "roi": result.roi,
                    "max_drawdown": result.max_drawdown,
                    "total_trades": result.total_trades,
                    "debate_conclusion": debate.conclusion,
                })
                console.print(f"  [bold green]>>> PROMOTED TO PAPER TRADING <<<[/bold green]")

        self.best_configs = promoted
        return promoted

    # ─── Phase 5: Final Report ───────────────────────────────

    def phase_final_report(self, promoted: list[dict]) -> None:
        """Iris ChiefOfStaff + Pulse Analytics: compile final report."""
        console.print(Panel(
            "[bold cyan]PHASE 5: FINAL CAMPAIGN REPORT[/bold cyan]\n"
            "Agents: Iris ChiefOfStaff + Pulse Analytics",
            title="[bold]CEO REPORT",
        ))

        console.print(f"\n[bold]Campaign Summary[/bold]")
        console.print(f"  Total experiments: {self.total_run}")
        console.print(f"  Promoted strategies: {len(promoted)}")
        console.print(f"  Coins tested: {len(self.coin_scores)}")
        console.print(f"  Failed combos blacklisted: {len(self.failed_combos)}")

        # Top 20 experiments
        all_sorted = sorted(self.experiments, key=lambda x: x["score"], reverse=True)

        table = Table(title="Top 20 Scalper Configurations", show_header=True, header_style="bold magenta")
        table.add_column("#", style="dim", width=3)
        table.add_column("Coin", style="cyan")
        table.add_column("TF")
        table.add_column("Lev", justify="right")
        table.add_column("Score", justify="right", style="bold")
        table.add_column("Sharpe", justify="right")
        table.add_column("Sortino", justify="right")
        table.add_column("ROI", justify="right")
        table.add_column("WR", justify="right")
        table.add_column("DD", justify="right", style="red")
        table.add_column("Trades", justify="right")
        table.add_column("Risk", justify="center")

        for i, exp in enumerate(all_sorted[:20], 1):
            r = exp["result"]
            h = exp["hypothesis"]
            risk_ok = exp["risk_report"].approved
            table.add_row(
                str(i),
                h["symbol"],
                h["timeframe"],
                f"{exp['leverage']:.0f}x",
                f"{exp['score']:.3f}",
                f"{r.sharpe:.2f}",
                f"{r.sortino:.2f}",
                f"{r.roi:.1%}",
                f"{r.win_rate:.1%}",
                f"{r.max_drawdown:.1%}",
                str(r.total_trades),
                "[green]OK[/green]" if risk_ok else "[red]NO[/red]",
            )
        console.print(table)

        # Best coins for scalping
        console.print("\n[bold]Best Coins for Scalping (by avg score):[/bold]")
        coin_avg = {sym: sum(scores)/len(scores) for sym, scores in self.coin_scores.items() if scores}
        for sym, avg in sorted(coin_avg.items(), key=lambda x: x[1], reverse=True)[:10]:
            n = len(self.coin_scores[sym])
            best = max(self.coin_scores[sym])
            console.print(f"  {sym}: avg={avg:.3f} | best={best:.3f} | tests={n}")

        # Promoted configs
        if promoted:
            console.print("\n[bold green]═══ PROMOTED CONFIGURATIONS ═══[/bold green]")
            for i, p in enumerate(promoted, 1):
                console.print(f"\n  [bold]{i}. {p['symbol']} ({p['timeframe']}) — {p['leverage']}x leverage[/bold]")
                console.print(f"     Score: {p['score']:.3f} | Sharpe: {p['sharpe']:.2f} | ROI: {p['roi']:.1%}")
                console.print(f"     Max DD: {p['max_drawdown']:.1%} | Trades: {p['total_trades']}")
                console.print(f"     Params: {json.dumps(p['params'], indent=2)}")
        else:
            console.print("\n[bold yellow]No strategies promoted yet. More research needed.[/bold yellow]")
            console.print("[dim]CEO recommendation: adjust parameters, test more timeframes, try wider BB.[/dim]")

        # Save report
        report = {
            "campaign": "scalper_research",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_experiments": self.total_run,
            "promoted_count": len(promoted),
            "promoted_configs": promoted,
            "coin_rankings": dict(sorted(coin_avg.items(), key=lambda x: x[1], reverse=True)),
            "top_10": [
                {
                    "symbol": e["hypothesis"]["symbol"],
                    "timeframe": e["hypothesis"]["timeframe"],
                    "leverage": e["leverage"],
                    "score": e["score"],
                    "sharpe": e["result"].sharpe,
                    "roi": e["result"].roi,
                }
                for e in all_sorted[:10]
            ],
        }
        report_path = Path("artifacts/scalper_campaign_report.json")
        report_path.parent.mkdir(exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        console.print(f"\n[dim]Report saved: {report_path}[/dim]")

    # ─── Main campaign runner ────────────────────────────────

    def run(self) -> list[dict]:
        """Execute the full scalper research campaign."""
        console.print("\n[bold green]" + "═" * 60 + "[/bold green]")
        console.print("[bold green]  OTO-BOT SCALPER RESEARCH CAMPAIGN[/bold green]")
        console.print("[bold green]  CEO: Atlas CEO — All Agents Mobilized[/bold green]")
        console.print(f"[bold green]  Capital: ${self.initial_capital:,.0f} | Max Leverage: {self.max_leverage}x[/bold green]")
        console.print(f"[bold green]  Coins: {len(self.coins)} | Max Experiments: {self.max_experiments}[/bold green]")
        console.print("[bold green]" + "═" * 60 + "[/bold green]\n")

        start_time = time.time()

        # Phase 1: Market scan
        viable_coins = self.phase_market_scan()
        if not viable_coins:
            console.print("[red]No viable coins found. Campaign aborted.[/red]")
            return []

        # Phase 2: Generate hypotheses
        n_hyp = min(self.max_experiments, len(viable_coins) * 10)
        hypotheses = self.phase_generate_hypotheses(viable_coins, n_hypotheses=n_hyp)

        # Phase 3: Run backtests
        results = self.phase_backtest(hypotheses)

        # Phase 4: Debate and decide
        promoted = self.phase_debate_and_decide(results)

        # Phase 5: Final report
        self.phase_final_report(promoted)

        elapsed = time.time() - start_time
        console.print(f"\n[bold]Campaign completed in {elapsed:.0f} seconds[/bold]")

        return promoted
