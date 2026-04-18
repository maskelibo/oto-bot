"""Autonomous orchestration engine — runs 24/7 research cycles."""

from __future__ import annotations

import json
import time
import random
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from rich.console import Console
from rich.table import Table

from oto_bot.agents.ceo import CEOAgent
from oto_bot.agents.registry import AgentRegistry
from oto_bot.analytics.scoring import composite_score
from oto_bot.backtest.engine import BacktestEngine, BacktestConfig
from oto_bot.core.models import Hypothesis, ExperimentResult
from oto_bot.experiments.ledger import ExperimentLedger
from oto_bot.governance.risk import RiskGate
from oto_bot.memory.manager import MemoryManager
from oto_bot.strategies.base import StrategyContext, Strategy
from oto_bot.strategies.day_trader import DayTraderStrategy
from oto_bot.strategies.swing_trader import SwingTraderStrategy
from oto_bot.strategies.scalper import ScalperStrategy
from oto_bot.utils.data import make_synthetic_ohlc

console = Console()
logger = logging.getLogger("oto_bot.orchestrator")

# ---------------------------------------------------------------------------
# Market & strategy universe
# ---------------------------------------------------------------------------

MARKET_SYMBOLS: dict[str, list[str]] = {
    "crypto": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"],
    "forex": ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF"],
    "us_equities": ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "TSLA", "META"],
    "bist": ["THYAO", "GARAN", "ASELS", "SISE", "EREGL", "KCHOL"],
}

TIMEFRAMES: dict[str, list[str]] = {
    "day": ["1h", "4h"],
    "swing": ["4h", "1d"],
    "scalp": ["5m", "15m"],
}

STRATEGY_FACTORIES: dict[str, type[Strategy]] = {
    "day": DayTraderStrategy,
    "swing": SwingTraderStrategy,
    "scalp": ScalperStrategy,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Portfolio state tracker
# ---------------------------------------------------------------------------

@dataclass
class PortfolioState:
    """Tracks simulated portfolio state across orchestrator cycles."""

    initial_capital: float = 100_000.0
    total_capital: float = 100_000.0
    unrealized_pnl: float = 0.0
    promoted_strategies: list[dict[str, Any]] = field(default_factory=list)
    daily_pnl: float = 0.0
    weekly_pnl: float = 0.0
    monthly_pnl: float = 0.0
    last_daily_reset: str = ""
    last_weekly_reset: str = ""
    last_monthly_reset: str = ""

    @property
    def risk_utilization_pct(self) -> float:
        """Fraction of capital currently at risk via promoted strategies."""
        if not self.promoted_strategies or self.total_capital <= 0:
            return 0.0
        total_risk = sum(s.get("risk_pct", 0.0) for s in self.promoted_strategies)
        return min(total_risk, 1.0)

    @property
    def effective_capital(self) -> float:
        return self.total_capital + self.unrealized_pnl

    def update_pnl_tracking(self, cycle_pnl: float) -> None:
        """Accumulate P&L and reset on period boundaries."""
        now = _now()
        today = now.strftime("%Y-%m-%d")
        week = now.strftime("%Y-W%W")
        month = now.strftime("%Y-%m")

        if self.last_daily_reset != today:
            self.daily_pnl = 0.0
            self.last_daily_reset = today
        if self.last_weekly_reset != week:
            self.weekly_pnl = 0.0
            self.last_weekly_reset = week
        if self.last_monthly_reset != month:
            self.monthly_pnl = 0.0
            self.last_monthly_reset = month

        self.daily_pnl += cycle_pnl
        self.weekly_pnl += cycle_pnl
        self.monthly_pnl += cycle_pnl

    def add_promoted_strategy(self, name: str, risk_pct: float, roi: float) -> None:
        self.promoted_strategies.append({
            "name": name,
            "risk_pct": risk_pct,
            "roi": roi,
            "promoted_at": _now().isoformat(),
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_capital": self.total_capital,
            "unrealized_pnl": self.unrealized_pnl,
            "effective_capital": self.effective_capital,
            "risk_utilization_pct": f"{self.risk_utilization_pct:.1%}",
            "promoted_count": len(self.promoted_strategies),
            "daily_pnl": self.daily_pnl,
            "weekly_pnl": self.weekly_pnl,
            "monthly_pnl": self.monthly_pnl,
        }


class CycleResult:
    """Holds everything produced by a single research cycle."""

    def __init__(
        self,
        hypothesis: Hypothesis,
        result: ExperimentResult,
        risk_approved: bool,
        risk_reason: str,
        decision: str,
        score: float,
        debate_summary: str | None = None,
    ):
        self.hypothesis = hypothesis
        self.result = result
        self.risk_approved = risk_approved
        self.risk_reason = risk_reason
        self.decision = decision
        self.score = score
        self.debate_summary = debate_summary


class Orchestrator:
    """Autonomous loop that runs research cycles across all markets & strategies."""

    def __init__(
        self,
        markets: list[str] | None = None,
        strategies: list[str] | None = None,
        cycle_pause_seconds: float = 2.0,
        max_cycles: int | None = None,
        initial_capital: float = 100_000.0,
    ):
        self.markets = markets or list(MARKET_SYMBOLS.keys())
        self.strategies = strategies or list(STRATEGY_FACTORIES.keys())
        self.cycle_pause = cycle_pause_seconds
        self.max_cycles = max_cycles

        # Core components
        self.registry = AgentRegistry()
        self.registry.seed_defaults()
        self.ceo = CEOAgent(self.registry)
        self.memory = MemoryManager()
        self.ledger = ExperimentLedger()
        self.risk_gate = RiskGate()

        # Portfolio state
        self.portfolio = PortfolioState(
            initial_capital=initial_capital,
            total_capital=initial_capital,
        )

        # Stats
        self.total_cycles = 0
        self.best_score = -999.0
        self.best_result: CycleResult | None = None
        self.cycle_history: list[CycleResult] = []

        # Experiment tracking for smart selection
        self._tested_combos: set[str] = set()
        self._combo_scores: dict[str, list[float]] = defaultdict(list)
        self._failed_param_spaces: dict[str, int] = defaultdict(int)
        self._consecutive_failures: int = 0

        # Adaptive speed
        self._base_pause: float = cycle_pause_seconds
        self._min_pause: float = max(0.5, cycle_pause_seconds * 0.25)
        self._max_pause: float = cycle_pause_seconds * 3.0

        # Logging
        self._setup_logging()

    def _setup_logging(self) -> None:
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        fh = logging.FileHandler(log_dir / "orchestrator.log")
        fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
        logger.addHandler(fh)
        logger.setLevel(logging.INFO)

    # ------------------------------------------------------------------
    # Session context (Atlas Trading)
    # ------------------------------------------------------------------

    def _session_context(self) -> dict[str, Any]:
        """Determine current trading session and adjust parameters accordingly."""
        hour = _now().hour
        if hour < 8:
            return {
                "session": "asia",
                "size_mult": 0.7,
                "preferred_markets": ["crypto"],
            }
        elif 8 <= hour < 14:
            return {
                "session": "europe",
                "size_mult": 1.0,
                "preferred_markets": ["forex", "bist"],
            }
        elif 14 <= hour < 22:
            return {
                "session": "us",
                "size_mult": 1.2,
                "preferred_markets": ["us_equities", "crypto"],
            }
        else:
            return {
                "session": "late_us",
                "size_mult": 0.8,
                "preferred_markets": ["crypto"],
            }

    # ------------------------------------------------------------------
    # Smart experiment selection
    # ------------------------------------------------------------------

    def _combo_key(self, market: str, strat: str, symbol: str, timeframe: str) -> str:
        return f"{market}|{strat}|{symbol}|{timeframe}"

    def _param_space_key(self, strat: str, params: dict[str, Any]) -> str:
        """Coarse key for a parameter region (rounds numerics to reduce space)."""
        simplified = {}
        for k, v in sorted(params.items()):
            if isinstance(v, float):
                simplified[k] = round(v, 1)
            else:
                simplified[k] = v
        return f"{strat}|{json.dumps(simplified, sort_keys=True)}"

    def _pick_next_experiment(self) -> dict[str, Any]:
        """Smart experiment picker — session-aware, avoids repeating failures,
        favours untested combos and variations of top performers."""

        session = self._session_context()
        preferred_markets = session["preferred_markets"]

        # Build candidate universe
        all_combos: list[dict[str, Any]] = []
        for market in self.markets:
            for strat in self.strategies:
                for symbol in MARKET_SYMBOLS.get(market, []):
                    for tf in TIMEFRAMES.get(strat, ["1h"]):
                        all_combos.append({
                            "market": market,
                            "strategy": strat,
                            "symbol": symbol,
                            "timeframe": tf,
                        })

        # 1. Separate untested from tested
        untested = []
        tested = []
        for combo in all_combos:
            key = self._combo_key(combo["market"], combo["strategy"],
                                  combo["symbol"], combo["timeframe"])
            if key not in self._tested_combos:
                untested.append(combo)
            else:
                tested.append(combo)

        # 2. Score candidates with session preference
        def _score_candidate(combo: dict[str, Any], is_untested: bool) -> float:
            score = 0.0
            # Untested bonus
            if is_untested:
                score += 10.0
            # Session preference
            if combo["market"] in preferred_markets:
                score += 5.0
            # Past performance bonus for tested combos (favour top performers)
            key = self._combo_key(combo["market"], combo["strategy"],
                                  combo["symbol"], combo["timeframe"])
            past = self._combo_scores.get(key, [])
            if past:
                avg = sum(past) / len(past)
                if avg > 0:
                    score += avg * 2.0  # Favour winners
                elif avg < -0.5:
                    score -= 3.0  # Penalise consistent losers
            # Randomness for exploration
            score += random.uniform(0, 3.0)
            return score

        all_scored = [
            (combo, _score_candidate(combo, combo in untested))
            for combo in (untested + tested)
        ]
        all_scored.sort(key=lambda x: x[1], reverse=True)

        # Pick from top candidates with some randomness
        top_n = min(10, len(all_scored))
        chosen_combo = random.choice([c for c, _ in all_scored[:top_n]])

        # 3. Generate params — favour variations of top performers
        strat = chosen_combo["strategy"]
        params = self._generate_smart_params(strat)

        # Check if this param space has consistently failed
        ps_key = self._param_space_key(strat, params)
        if self._failed_param_spaces.get(ps_key, 0) >= 3:
            # Re-roll params to explore different space
            params = self._generate_param_variation(strat)

        chosen_combo["params"] = params
        chosen_combo["session"] = session

        return chosen_combo

    def _generate_smart_params(self, strat: str) -> dict[str, Any]:
        """Generate params that favour variations of top-performing configurations."""
        # Find best result for this strategy family
        best_for_strat = [
            cr for cr in self.cycle_history
            if cr.hypothesis.strategy_family == strat and cr.score > 0
        ]
        best_for_strat.sort(key=lambda x: x.score, reverse=True)

        if best_for_strat and random.random() < 0.6:
            # 60% chance: perturb the best-known params
            top = best_for_strat[0]
            try:
                # Extract params from the result notes
                notes = top.result.notes or ""
                params_start = notes.find("params=")
                if params_start >= 0:
                    params_json = notes[params_start + 7:]
                    # Handle trailing text after the JSON
                    try:
                        base_params = json.loads(params_json)
                    except json.JSONDecodeError:
                        # Try to find the end of JSON
                        for end_idx in range(len(params_json), 0, -1):
                            try:
                                base_params = json.loads(params_json[:end_idx])
                                break
                            except json.JSONDecodeError:
                                continue
                        else:
                            return self._generate_param_variation(strat)

                    # Perturb each numeric value by +/-10%
                    perturbed: dict[str, Any] = {}
                    for k, v in base_params.items():
                        if isinstance(v, (int, float)):
                            mult = random.uniform(0.9, 1.1)
                            new_val = v * mult
                            perturbed[k] = int(round(new_val)) if isinstance(v, int) else round(new_val, 2)
                        else:
                            perturbed[k] = v
                    return perturbed
            except Exception:
                pass

        # Fallback: standard random variation
        return self._generate_param_variation(strat)

    def _generate_param_variation(self, strat: str) -> dict[str, Any]:
        """Generate slight parameter variations for exploration."""
        base_params: dict[str, Any] = {}
        variation = random.uniform(0.8, 1.2)  # +/-20%

        if strat == "day":
            fast = random.choice([3, 5, 8, 10])
            slow = random.choice([15, 20, 25, 30])
            base_params = {
                "fast_ma": fast,
                "slow_ma": slow,
                "rsi_period": random.choice([10, 14, 21]),
                "rsi_overbought": random.choice([65, 70, 75, 80]),
                "rsi_oversold": random.choice([20, 25, 30, 35]),
                "atr_period": 14,
                "atr_multiplier_sl": round(1.5 * variation, 2),
                "atr_multiplier_tp": round(2.5 * variation, 2),
            }
        elif strat == "swing":
            base_params = {
                "ma_short": random.choice([20, 30, 50]),
                "ma_long": random.choice([100, 150, 200]),
                "macd_fast": random.choice([8, 12, 16]),
                "macd_slow": random.choice([21, 26, 30]),
                "macd_signal": random.choice([7, 9, 11]),
                "atr_period": 14,
                "atr_multiplier_sl": round(2.0 * variation, 2),
                "atr_multiplier_tp": round(3.5 * variation, 2),
            }
        elif strat == "scalp":
            base_params = {
                "lookback": random.choice([8, 10, 14, 20]),
                "z_entry": round(random.uniform(0.8, 1.5), 2),
                "z_exit": round(random.uniform(0.1, 0.4), 2),
                "bb_period": random.choice([15, 20, 25]),
                "bb_std": round(random.uniform(1.5, 2.5), 2),
                "atr_period": 14,
                "atr_multiplier_sl": round(1.0 * variation, 2),
            }
        return base_params

    # ------------------------------------------------------------------
    # Adaptive cycle speed
    # ------------------------------------------------------------------

    def _adaptive_pause(self) -> float:
        """Compute cycle pause based on recent performance trend.

        - Consecutive failures -> slow down (longer pause, more exploration).
        - Recent improvements -> speed up (shorter pause, focused search).
        """
        if self._consecutive_failures >= 5:
            # Major slowdown — many failures in a row
            pause = self._max_pause
            logger.info(
                f"Adaptive pause: SLOW ({pause:.1f}s) after "
                f"{self._consecutive_failures} consecutive failures"
            )
        elif self._consecutive_failures >= 3:
            # Moderate slowdown
            pause = self._base_pause * 2.0
        elif len(self.cycle_history) >= 3:
            # Check recent trend
            recent = self.cycle_history[-3:]
            improving = all(
                recent[i].score > recent[i - 1].score
                for i in range(1, len(recent))
            )
            if improving:
                pause = self._min_pause
                logger.info(f"Adaptive pause: FAST ({pause:.1f}s) — improving trend")
            else:
                pause = self._base_pause
        else:
            pause = self._base_pause

        return pause

    # ------------------------------------------------------------------
    # Single cycle execution
    # ------------------------------------------------------------------

    def run_single_cycle(self, experiment: dict[str, Any] | None = None) -> CycleResult:
        """Execute one full research cycle."""
        if experiment is None:
            experiment = self._pick_next_experiment()

        market = experiment["market"]
        strat = experiment["strategy"]
        symbol = experiment["symbol"]
        timeframe = experiment["timeframe"]
        params = experiment.get("params", {})
        session = experiment.get("session", self._session_context())

        logger.info(
            f"CYCLE {self.total_cycles + 1}: {strat}/{market}/{symbol}/{timeframe} "
            f"session={session.get('session', '?')} params={params}"
        )

        # Track this combo
        combo_key = self._combo_key(market, strat, symbol, timeframe)
        self._tested_combos.add(combo_key)

        # 1. Create strategy
        strategy_cls = STRATEGY_FACTORIES[strat]
        strategy_impl = strategy_cls()

        # 2. Build context with params
        context = StrategyContext(
            market=market,
            strategy_family=strat,
            symbol=symbol,
            timeframe=timeframe,
        )
        # Inject params if strategy context supports it
        if hasattr(context, "params"):
            context.params = params

        # 3. Get data — try real data, fall back to synthetic
        data = self._fetch_data(market, symbol, timeframe)

        # 4. Create hypothesis
        ceo_profile = self.registry.find_by_name("Atlas CEO")
        hypothesis = Hypothesis(
            title=f"{strategy_impl.name}_{market}_{symbol}_{timeframe}_v{self.total_cycles}",
            thesis=f"Testing {strat} strategy on {symbol} ({market}) with params: {json.dumps(params)}",
            market=market,
            strategy_family=strat,
            timeframe=timeframe,
            invalidation="Risk-adjusted metrics fail promotion gates.",
            expected_edge=f"{strat} edge on {market} via indicator confluence.",
            author_agent_id=ceo_profile.agent_id if ceo_profile else "unknown",
        )
        self.memory.save_hypothesis(hypothesis)

        # 5. Run backtest
        engine = BacktestEngine()
        result = engine.run(strategy_impl, data, context)

        # 6. Risk gate
        risk_report = self.risk_gate.approve(result)
        risk_approved = risk_report.approved
        risk_reason = risk_report.summary

        # 6b. Check multi-timeframe portfolio limits before promotion
        monthly_dd = self.portfolio.monthly_pnl / self.portfolio.total_capital if self.portfolio.total_capital > 0 else 0.0
        daily_frac = self.portfolio.daily_pnl / self.portfolio.total_capital if self.portfolio.total_capital > 0 else 0.0
        weekly_frac = self.portfolio.weekly_pnl / self.portfolio.total_capital if self.portfolio.total_capital > 0 else 0.0

        limits_ok, breaches = self.risk_gate.check_daily_weekly_monthly_limits(
            daily_pnl=daily_frac,
            weekly_pnl=weekly_frac,
            monthly_dd=monthly_dd,
        )
        if not limits_ok:
            risk_approved = False
            risk_reason += "; " + "; ".join(breaches)
            logger.warning(f"Portfolio limits breached: {breaches}")

        result.promoted = result.promoted and risk_approved

        # 7. Score
        score = composite_score(result)
        result.notes = (
            f"{result.notes}; risk_gate={risk_reason}; "
            f"composite_score={score:.4f}; session={session.get('session', '?')}; "
            f"params={json.dumps(params)}"
        )

        # 8. Memory
        self.memory.save_result(result)

        # 9. CEO review
        decision_obj = self.ceo.review_experiment(result)
        self.memory.save_decision(decision_obj)

        # 10. Debate (if available)
        debate_summary = None
        try:
            from oto_bot.agents.debate import AgentDebater
            debater = AgentDebater()
            debate = debater.debate(
                topic=f"Should we promote {hypothesis.title}?",
                experiment_result=result,
                participants=["Sentinel Risk", "Sigma Quant", "Nova StrategyRND", "Pulse Analytics", "Atlas CEO"],
                memory_manager=self.memory,
            )
            debate_summary = debate.conclusion
        except (ImportError, Exception) as e:
            logger.warning(f"Debate skipped: {e}")

        # 11. Log to ledger
        self.ledger.log({
            "hypothesis": hypothesis.title,
            "market": market,
            "strategy": strat,
            "symbol": symbol,
            "timeframe": timeframe,
            "params": params,
            "session": session.get("session", "unknown"),
            "result": {k: v for k, v in result.__dict__.items()
                       if k != "created_at"} | {"created_at": result.created_at.isoformat()},
            "decision": {k: v for k, v in decision_obj.__dict__.items()
                         if k != "created_at"} | {"created_at": decision_obj.created_at.isoformat()},
            "debate": debate_summary,
            "composite_score": score,
            "portfolio_state": self.portfolio.to_dict(),
        })

        # 12. Track stats
        self.total_cycles += 1
        cycle_result = CycleResult(
            hypothesis=hypothesis,
            result=result,
            risk_approved=risk_approved,
            risk_reason=risk_reason,
            decision=decision_obj.decision,
            score=score,
            debate_summary=debate_summary,
        )
        self.cycle_history.append(cycle_result)

        # 12b. Update combo tracking for smart selection
        self._combo_scores[combo_key].append(score)
        ps_key = self._param_space_key(strat, params)
        if not risk_approved or score < 0:
            self._failed_param_spaces[ps_key] = self._failed_param_spaces.get(ps_key, 0) + 1
            self._consecutive_failures += 1
        else:
            self._consecutive_failures = 0

        # 12c. Update portfolio state
        simulated_cycle_pnl = result.roi * self.portfolio.total_capital * 0.01  # scaled
        self.portfolio.update_pnl_tracking(simulated_cycle_pnl)
        if result.promoted:
            self.portfolio.add_promoted_strategy(
                name=hypothesis.title,
                risk_pct=self.risk_gate.policy.max_single_trade_risk_pct,
                roi=result.roi,
            )
            self.portfolio.unrealized_pnl += simulated_cycle_pnl

        if score > self.best_score:
            self.best_score = score
            self.best_result = cycle_result
            logger.info(f"NEW BEST: score={score:.4f} | {hypothesis.title}")

        return cycle_result

    def _fetch_data(self, market: str, symbol: str, timeframe: str):
        """Try real data providers, fall back to synthetic."""
        try:
            from oto_bot.data.factory import DataProviderFactory
            provider = DataProviderFactory.get_provider(market)
            data = provider.fetch_ohlcv(symbol, timeframe, limit=500)
            if data is not None and len(data) >= 100:
                logger.info(f"Real data fetched: {symbol} {timeframe} ({len(data)} bars)")
                return data
        except Exception as e:
            logger.warning(f"Real data unavailable for {symbol}: {e}")

        logger.info(f"Using synthetic data for {symbol}")
        return make_synthetic_ohlc(500)

    # ------------------------------------------------------------------
    # Autonomous loop
    # ------------------------------------------------------------------

    def run_autonomous(self) -> None:
        """Run continuous autonomous research cycles."""
        console.print("\n[bold green]═══ OTO-BOT AUTONOMOUS MODE ACTIVATED ═══[/bold green]")
        console.print(f"Markets: {self.markets}")
        console.print(f"Strategies: {self.strategies}")
        console.print(f"Max cycles: {self.max_cycles or '∞'}")
        console.print(f"Initial capital: ${self.portfolio.total_capital:,.0f}")
        session = self._session_context()
        console.print(f"Current session: {session['session']} (size_mult={session['size_mult']})")
        console.print("[dim]Press Ctrl+C to stop[/dim]\n")

        try:
            while True:
                if self.max_cycles and self.total_cycles >= self.max_cycles:
                    console.print("\n[bold yellow]Max cycles reached. Stopping.[/bold yellow]")
                    break

                cycle_result = self.run_single_cycle()
                self._print_cycle_summary(cycle_result)

                # Every 10 cycles, print executive brief
                if self.total_cycles % 10 == 0:
                    self._print_executive_brief()

                # Adaptive pause
                pause = self._adaptive_pause()
                time.sleep(pause)

        except KeyboardInterrupt:
            console.print("\n[bold yellow]Autonomous mode stopped by user.[/bold yellow]")

        self._print_final_report()

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def _print_cycle_summary(self, cr: CycleResult) -> None:
        """Print concise cycle result."""
        r = cr.result
        status = "[green]✓ PROMOTED[/green]" if r.promoted else "[yellow]⟳ ITERATE[/yellow]"

        console.print(
            f"[bold]Cycle {self.total_cycles}[/bold] | "
            f"{cr.hypothesis.title} | "
            f"Score: {cr.score:.3f} | "
            f"Sharpe: {r.sharpe:.2f} | "
            f"ROI: {r.roi:.1%} | "
            f"WR: {r.win_rate:.1%} | "
            f"DD: {r.max_drawdown:.1%} | "
            f"Risk util: {self.portfolio.risk_utilization_pct:.0%} | "
            f"{status}"
        )

    def _print_executive_brief(self) -> None:
        """Print executive summary every N cycles."""
        console.print("\n[bold cyan]═══ EXECUTIVE BRIEF ═══[/bold cyan]")

        # Portfolio state
        ps = self.portfolio
        console.print(
            f"[bold]Portfolio:[/bold] Capital=${ps.total_capital:,.0f} | "
            f"Unrealized={ps.unrealized_pnl:+,.0f} | "
            f"Effective=${ps.effective_capital:,.0f} | "
            f"Risk Util={ps.risk_utilization_pct:.0%} | "
            f"Promoted={len(ps.promoted_strategies)}"
        )
        console.print(
            f"  Daily P&L: {ps.daily_pnl:+,.0f} | "
            f"Weekly: {ps.weekly_pnl:+,.0f} | "
            f"Monthly: {ps.monthly_pnl:+,.0f}"
        )

        # Session info
        session = self._session_context()
        console.print(f"  Session: {session['session']} | Preferred: {session['preferred_markets']}")

        # Adaptive speed
        console.print(
            f"  Consecutive failures: {self._consecutive_failures} | "
            f"Tested combos: {len(self._tested_combos)} | "
            f"Current pause: {self._adaptive_pause():.1f}s"
        )

        table = Table(title=f"Top 5 Results (of {self.total_cycles} cycles)")
        table.add_column("Strategy", style="cyan")
        table.add_column("Score", justify="right")
        table.add_column("Sharpe", justify="right")
        table.add_column("ROI", justify="right")
        table.add_column("Win Rate", justify="right")
        table.add_column("Max DD", justify="right")
        table.add_column("Status", justify="center")

        sorted_history = sorted(self.cycle_history, key=lambda x: x.score, reverse=True)[:5]
        for cr in sorted_history:
            r = cr.result
            table.add_row(
                cr.hypothesis.title[:40],
                f"{cr.score:.3f}",
                f"{r.sharpe:.2f}",
                f"{r.roi:.1%}",
                f"{r.win_rate:.1%}",
                f"{r.max_drawdown:.1%}",
                "PROMOTED" if r.promoted else "HOLD",
            )

        console.print(table)

        # Worst failures
        worst = sorted(self.cycle_history, key=lambda x: x.score)[:3]
        console.print("\n[bold red]Worst 3:[/bold red]")
        for cr in worst:
            console.print(f"  • {cr.hypothesis.title[:50]} | Score: {cr.score:.3f} | {cr.risk_reason}")

        console.print("")

    def _print_final_report(self) -> None:
        """Print final comprehensive report."""
        console.print("\n[bold green]═══ FINAL REPORT ═══[/bold green]")
        console.print(f"Total cycles: {self.total_cycles}")

        if not self.cycle_history:
            console.print("[dim]No cycles completed.[/dim]")
            return

        promoted = [cr for cr in self.cycle_history if cr.result.promoted]
        console.print(f"Promoted strategies: {len(promoted)}")
        console.print(f"Best score: {self.best_score:.4f}")

        # Portfolio summary
        ps = self.portfolio
        console.print(
            f"\n[bold]Portfolio State:[/bold] Capital=${ps.total_capital:,.0f} | "
            f"Unrealized={ps.unrealized_pnl:+,.0f} | "
            f"Risk Util={ps.risk_utilization_pct:.0%}"
        )

        if self.best_result:
            r = self.best_result.result
            console.print(f"\n[bold]Best Strategy:[/bold] {self.best_result.hypothesis.title}")
            console.print(f"  Sharpe: {r.sharpe:.2f} | Sortino: {getattr(r, 'sortino', 'N/A')}")
            console.print(f"  ROI: {r.roi:.1%} | Win Rate: {r.win_rate:.1%}")
            console.print(f"  Max Drawdown: {r.max_drawdown:.1%}")
            console.print(f"  Profit Factor: {r.profit_factor:.2f}")

        # Strategy family breakdown
        console.print("\n[bold]By Strategy Family:[/bold]")
        for strat in self.strategies:
            strat_results = [cr for cr in self.cycle_history if cr.hypothesis.strategy_family == strat]
            if strat_results:
                avg_score = sum(cr.score for cr in strat_results) / len(strat_results)
                best = max(strat_results, key=lambda x: x.score)
                console.print(
                    f"  {strat}: {len(strat_results)} tests | "
                    f"Avg Score: {avg_score:.3f} | "
                    f"Best: {best.score:.3f}"
                )

        # Market breakdown
        console.print("\n[bold]By Market:[/bold]")
        for market in self.markets:
            market_results = [cr for cr in self.cycle_history if cr.hypothesis.market == market]
            if market_results:
                avg_score = sum(cr.score for cr in market_results) / len(market_results)
                console.print(f"  {market}: {len(market_results)} tests | Avg Score: {avg_score:.3f}")

        # Exploration stats
        console.print(f"\n[bold]Exploration:[/bold]")
        console.print(f"  Unique combos tested: {len(self._tested_combos)}")
        console.print(f"  Failed param spaces: {len(self._failed_param_spaces)}")
        console.print(f"  Final consecutive failures: {self._consecutive_failures}")

        console.print("\n[bold green]═══ END OF REPORT ═══[/bold green]\n")
