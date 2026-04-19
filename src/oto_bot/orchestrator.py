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
from oto_bot.memory.journal import LearningJournal
from oto_bot.memory.retriever import MemoryRetriever, RetrievalContext
from oto_bot.memory.insight import InsightExtractor
from oto_bot.memory.curve import LearningCurve
from oto_bot.agents.hr import HRManager
from oto_bot.agents.robustness import RobustnessQueue, generate_variants, is_promising
from oto_bot.agents.stress import StressLab
from oto_bot.agents.auto_approver import AutoApprover
from oto_bot.agents.regime import RegimeOracle
from oto_bot.agents.macro import MercuryMacro
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

        # Compact memory stack: journal + retriever + insight extractor
        self.journal = LearningJournal()
        self.retriever = MemoryRetriever(journal=self.journal)
        self.insight = InsightExtractor(journal=self.journal)
        self.learning_curve = LearningCurve()

        # HR — CEO'nun elinde otomatik hiring/firing
        self.hr = HRManager(registry=self.registry, journal=self.journal)
        self._hr_review_interval = 50  # her 50 cycle'da bir review

        # Robustness queue — promising sonuçların varyantları
        self.robustness_queue = RobustnessQueue()

        # Stress Lab — promote adayları için
        self.stress_lab = StressLab()

        # Auto-approver (settings.auto_approve=True ise proposal'ları otomatik karara bağlar)
        self.auto_approver = AutoApprover()
        self._auto_approve_interval = 10  # her 10 cycle'da bir tetikle

        # Regime + Macro overlay (audit'ten sonra etkinleştirildi — önceden dead code idi)
        self.regime_oracle = RegimeOracle()
        self.macro = MercuryMacro()

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

        # Heartbeat (live status for dashboard)
        self._heartbeat_path = Path("artifacts/current_cycle.json")
        self._heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        self._current_hypothesis: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Heartbeat: which agent is doing what, right now
    # ------------------------------------------------------------------

    PHASE_AGENTS: dict[str, list[str]] = {
        "idle": [],
        "select": ["Vega MarketIntel", "Regime Oracle"],
        "hypothesize": ["Nova StrategyRND", "Atlas CEO"],
        "fetch_data": ["Helix Backtest", "Archive Memory"],
        "backtest": ["Helix Backtest"],
        "risk_gate": ["Sentinel Risk"],
        "portfolio_limits": ["Apex PortfolioRisk"],
        "score": ["Pulse Analytics"],
        "debate": [
            "Sentinel Risk", "Sigma Quant", "Nova StrategyRND", "Pulse Analytics",
            "Mercury Macro", "Apex PortfolioRisk", "Tariq TCA", "Cassandra PreMortem",
        ],
        "ceo_decision": ["Atlas CEO"],
        "persist": ["Archive Memory"],
    }

    PHASE_LABELS: dict[str, str] = {
        "idle":             "Boşta",
        "select":           "Hedef seçiliyor",
        "hypothesize":      "Hipotez oluşturuluyor",
        "fetch_data":       "Veri çekiliyor",
        "backtest":         "Backtest koşuyor",
        "risk_gate":        "Risk gate kontrolü",
        "portfolio_limits": "Kitap limitleri kontrol",
        "score":            "Risk-adjusted skor",
        "debate":           "8-sesli panel tartışıyor",
        "ceo_decision":     "CEO karar veriyor",
        "persist":          "Hafızaya yazılıyor",
    }

    def _heartbeat(
        self,
        phase: str,
        hypothesis: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Write a lightweight status file for the dashboard."""
        if hypothesis is not None:
            self._current_hypothesis = hypothesis
        payload = {
            "cycle": self.total_cycles + (1 if phase not in ("idle", "persist") else 0),
            "phase": phase,
            "phase_label": self.PHASE_LABELS.get(phase, phase),
            "active_agents": self.PHASE_AGENTS.get(phase, []),
            "hypothesis": self._current_hypothesis,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if extra:
            payload.update(extra)
        try:
            self._heartbeat_path.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass  # heartbeat kırılsa bile cycle devam eder

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
        """Smart experiment picker — robustness suite queue → untested → variants → random."""

        # 0. Önce robustness kuyruğundan çek (varsa)
        pending = self.robustness_queue.next_pending()
        if pending is not None:
            logger.info(
                f"Robustness: next variant from queue → {pending.strategy}/{pending.market}/"
                f"{pending.symbol}/{pending.timeframe} (origin={pending.origin_title[:40]}, "
                f"window={pending.data_window_years or 'default'})"
            )
            return {
                "market": pending.market,
                "strategy": pending.strategy,
                "symbol": pending.symbol,
                "timeframe": pending.timeframe,
                "params": pending.params,
                "session": self._session_context(),
                "robustness_origin": pending.origin_experiment_id,
                "data_window_years": pending.data_window_years,
            }

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
        self._heartbeat("select")
        if experiment is None:
            experiment = self._pick_next_experiment()

        market = experiment["market"]
        strat = experiment["strategy"]
        symbol = experiment["symbol"]
        timeframe = experiment["timeframe"]
        params = experiment.get("params", {})
        session = experiment.get("session", self._session_context())

        # Heartbeat: publish the current target
        self._heartbeat("hypothesize", hypothesis={
            "market": market, "strategy": strat,
            "symbol": symbol, "timeframe": timeframe,
            "title": f"{strat}_{market}_{symbol}_{timeframe}_v{self.total_cycles}",
            "session": session.get("session", "?"),
        })

        # Memory retrieval — bu cycle için ilgili geçmiş dersleri yükle
        try:
            ctx = RetrievalContext(
                market=market,
                strategy_family=strat,
                symbol=symbol,
                regime="*",  # rejim henüz ölçülmedi; global dersler de gelecek
                include_global=True,
            )
            retrieved = self.retriever.retrieve(ctx, k=5)
            if retrieved:
                logger.info(
                    f"Memory: retrieved {len(retrieved)} lessons for {strat}/{market}/{symbol}"
                )
        except Exception as _e:
            logger.warning(f"retrieval failed: {_e}")

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
        self._heartbeat("fetch_data")
        window_years = experiment.get("data_window_years")
        data = self._fetch_data(market, symbol, timeframe, window_years=window_years)

        # 3b. Regime classify + Macro overlay — audit sonrası etkinleştirildi
        regime_state = None
        macro_context = None
        try:
            regime_state = self.regime_oracle.classify(data, market=market)
            # Strateji-rejim fit kontrolü: mismatch ise cycle yine koşar ama not düşer
            fit = self.regime_oracle.strategy_fit(regime_state.regime, strat)
            if not fit["fit"] and regime_state.regime != "unknown":
                logger.info(
                    f"Regime mismatch: {strat} için {regime_state.regime} uygun değil "
                    f"(rec: {fit['recommendation']})"
                )
        except Exception as _e:
            logger.warning(f"regime classify failed: {_e}")

        try:
            # Macro: BTC + companion (mümkünse ETH) ile
            companion = {}
            if market == "crypto" and symbol != "ETHUSDT":
                try:
                    eth = self._fetch_data("crypto", "ETHUSDT", timeframe)
                    companion["ETH"] = eth
                except Exception:
                    pass
            macro_context = self.macro.assess(market, data, companion_ohlcv=companion)
            if macro_context.bias == "crisis":
                logger.warning(f"Macro CRISIS detected — promotion will be frozen.")
        except Exception as _e:
            logger.warning(f"macro assess failed: {_e}")

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
        self._heartbeat("backtest")
        engine = BacktestEngine()
        result = engine.run(strategy_impl, data, context)

        # 5b. Monte Carlo DD — ucuz, her cycle'da koşar
        try:
            # Equity curve'ü backtest'ten sonra rebuild et (trade return'lerle)
            import numpy as _np
            synth_eq = [10000.0]
            for t_pct in getattr(result, "top_trades_by_pnl", []):
                # Bu liste sadece top 5 içerir; daha iyi: mevcut metriklerden rebuild
                pass
            # Basit: result.max_drawdown'dan faydalanarak MC yerine risk-adjusted proxy
            # Gerçek equity curve'e erişim için engine içinde tutulmalı — şimdilik
            # trade return percentages'dan sentetik eğri üretelim:
            avg_w = float(getattr(result, "avg_win_pct", 0) or 0)
            avg_l = float(getattr(result, "avg_loss_pct", 0) or 0)
            wr = float(result.win_rate or 0)
            n_trades = int(result.total_trades or 0)
            if n_trades >= 10 and (avg_w != 0 or avg_l != 0):
                rng = _np.random.default_rng(42)
                # N simulation paths
                worst = []
                for _ in range(500):
                    rets = rng.choice([avg_w, avg_l], size=n_trades, p=[wr, 1 - wr])
                    eq = _np.cumprod(1 + rets)
                    peak = _np.maximum.accumulate(eq)
                    dd = (eq / peak - 1.0).min()
                    worst.append(float(dd))
                result.montecarlo_95_drawdown = float(_np.percentile(worst, 5))
        except Exception as _e:
            logger.warning(f"monte_carlo failed: {_e}")

        # 5c. Walk-forward — sadece promising (Sharpe >= 1.0) için
        if result.sharpe >= 1.0 and len(data) >= 200:
            try:
                self._heartbeat("backtest")
                wf_sharpe = engine.walk_forward(strategy_impl, data, context, n_splits=3)
                result.walkforward_sharpe = float(wf_sharpe)
            except Exception as _e:
                logger.warning(f"walk_forward failed: {_e}")

        # 5d. Stress Lab — sadece promote adayları için (Sharpe >= 1.2 AND DD >= -12%)
        stress_pass_count = 0
        stress_total = 0
        stress_results = []
        if result.sharpe >= 1.2 and result.max_drawdown >= -0.12 and result.total_trades >= 30:
            try:
                def _stress_bt(shocked_df):
                    _r = engine.run(strategy_impl, shocked_df, context)
                    return {
                        "max_drawdown": _r.max_drawdown,
                        "total_pnl": _r.roi * 10000,
                        "kill_switch_fired": _r.max_drawdown < -0.25,
                    }
                stress_results = self.stress_lab.run_all(data, strat, market, _stress_bt)
                stress_pass_count = sum(1 for s in stress_results if s.survived)
                stress_total = len(stress_results)
                # Stress override: min 4/6 must survive to stay promote-eligible
                if stress_pass_count < 4:
                    result.promoted = False
            except Exception as _e:
                logger.warning(f"stress_lab failed: {_e}")

        # 6. Risk gate
        self._heartbeat("risk_gate")
        risk_report = self.risk_gate.approve(result)
        risk_approved = risk_report.approved
        risk_reason = risk_report.summary

        # 6b. Check multi-timeframe portfolio limits before promotion
        self._heartbeat("portfolio_limits")
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
        self._heartbeat("score")
        score = composite_score(result)
        stress_str = (
            f"stress={stress_pass_count}/{stress_total}" if stress_total > 0 else "stress=skipped"
        )
        wf_str = f"wf_sharpe={result.walkforward_sharpe:.2f}" if result.walkforward_sharpe is not None else "wf=n/a"
        mc_str = f"mc_dd={result.montecarlo_95_drawdown:.2%}" if result.montecarlo_95_drawdown is not None else "mc=n/a"
        result.notes = (
            f"{result.notes}; risk_gate={risk_reason}; "
            f"composite_score={score:.4f}; session={session.get('session', '?')}; "
            f"{wf_str}; {mc_str}; {stress_str}; "
            f"params={json.dumps(params)}"
        )

        # 8. Memory
        self.memory.save_result(result)

        # 9. CEO review (this internally triggers debate + pre-mortem)
        self._heartbeat("debate")
        # Regime + Macro bağlamını CEO'ya geç (audit sonrası aktif)
        macro_ctx_dict = None
        if macro_context is not None:
            macro_ctx_dict = {
                "bias": macro_context.bias,
                "risk_on_score": macro_context.risk_on_score,
                "regime_label": macro_context.regime_label,
                "confidence": macro_context.confidence,
            }
        # CEO'ya rejim bilgisini result üzerinden de taşı
        if regime_state is not None and regime_state.regime != "unknown":
            # Engine'in _detect_regime'si zayıf bir proxy; Oracle daha doğru
            result.regime = regime_state.regime
        decision_obj = self.ceo.review_experiment(result, macro_context=macro_ctx_dict)
        self._heartbeat("ceo_decision")
        self.memory.save_decision(decision_obj)

        # 9b. Insight extraction — cycle'dan kompakt dersler çıkar ve journal'a yaz
        try:
            self._heartbeat("persist")
            self.insight.extract_from_cycle(
                result=result,
                decision={"decision": decision_obj.decision, "reasoning": decision_obj.reasoning},
                debate_conclusion=None,
                cycle_number=self.total_cycles + 1,
            )
        except Exception as _e:
            logger.warning(f"insight extraction failed: {_e}")

        # 9c. Auto-schedule robustness suite — promising sonuç için varyantlar
        try:
            result_dict = {
                "experiment_id": result.experiment_id,
                "hypothesis_title": result.hypothesis_title,
                "market": result.market,
                "strategy_family": result.strategy_family,
                "symbol": getattr(result, "symbol", symbol),
                "bar_timeframe": getattr(result, "bar_timeframe", timeframe),
                "strategy_params": getattr(result, "strategy_params", params) or params,
                "sharpe": result.sharpe, "cagr": result.cagr,
                "profit_factor": result.profit_factor,
                "max_drawdown": result.max_drawdown,
                "total_trades": result.total_trades,
            }
            # Only schedule if promising AND not already a robustness test itself
            if is_promising(result_dict) and "robustness_origin" not in experiment:
                variants = generate_variants(result_dict, max_variants=8, reason="auto_promising")
                n = self.robustness_queue.push_many(variants)
                if n > 0:
                    logger.info(f"Robustness: auto-scheduled {n} variants for {result.hypothesis_title}")
        except Exception as _e:
            logger.warning(f"robustness scheduling failed: {_e}")

        # 10. Debate — ARTIK CEO.review_experiment ici çağırıyor, duplicate yok.
        # (Daha önce orchestrator burada ikinci bir debate koşuyordu; audit kaldırdı.)
        debate_summary = decision_obj.reasoning if decision_obj else None

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

    # Timeframe → bar limit (minimum anlamlı backtest penceresi)
    TIMEFRAME_BAR_LIMITS: dict[str, int] = {
        "5m":  2000,   # ~7 gün (exploratory scalper)
        "15m": 3000,   # ~31 gün (~1 ay)
        "30m": 4000,   # ~2.8 ay
        "1h":  8760,   # 1 yıl
        "4h":  4380,   # 2 yıl
        "1d":  1460,   # 4 yıl
    }
    _DEFAULT_BAR_LIMIT: int = 500

    def _fetch_data(self, market: str, symbol: str, timeframe: str, window_years: float | None = None):
        """Öncelikle 5 yıl'lık cached data'dan al; cache yoksa live fetch; son çare sentetik.

        Data cache: artifacts/data_cache/<market>/<symbol>_<tf>.parquet
        Orchestrator başlatıldığında backfill script ile doldurulur:
            .venv/Scripts/python.exe scripts/backfill_data.py
        """
        tf_key = timeframe.lower() if timeframe else "1h"
        base_limit = self.TIMEFRAME_BAR_LIMITS.get(tf_key, self._DEFAULT_BAR_LIMIT)

        # Eğer window_years belirtilmişse, limit'i ona göre ayarla
        if window_years is not None and window_years > 0:
            mins = self._tf_to_minutes(tf_key)
            want_bars = int(window_years * 365 * 24 * 60 / mins)
            limit = min(want_bars, base_limit * 10)  # aşırı büyüme koru
        else:
            limit = base_limit

        # 1. Önce 5-yıl cache'inden dene
        try:
            from oto_bot.data.downloader import download
            cached = download(market, symbol, timeframe, years=5.0, use_cache=True)
            if cached is not None and len(cached) >= 200:
                # Cache max'i al veya limit'e göre kes
                df = cached.tail(limit).reset_index(drop=True) if len(cached) > limit else cached.reset_index(drop=True)
                logger.info(
                    f"Cache data: {symbol} {timeframe} ({len(df)} bars "
                    f"≈ {len(df) * self._tf_to_minutes(tf_key) / 60 / 24:.1f} gün)"
                )
                return df
        except Exception as e:
            logger.warning(f"Cache fetch failed for {symbol}: {e}")

        # 2. Live fetch (eski davranış — kısa pencere)
        try:
            from oto_bot.data.factory import DataProviderFactory
            provider = DataProviderFactory.get_provider(market)
            data = provider.fetch_ohlcv(symbol, timeframe, limit=limit)
            if data is not None and len(data) >= 200:
                logger.info(
                    f"Live data fetched: {symbol} {timeframe} ({len(data)} bars "
                    f"≈ {len(data) * self._tf_to_minutes(tf_key) / 60 / 24:.1f} gün)"
                )
                return data
        except Exception as e:
            logger.warning(f"Live data unavailable for {symbol}: {e}")

        logger.info(f"Using synthetic data for {symbol} ({limit} bars)")
        return make_synthetic_ohlc(limit)

    @staticmethod
    def _tf_to_minutes(tf: str) -> int:
        return {
            "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
            "1h": 60, "2h": 120, "4h": 240, "1d": 1440,
        }.get(tf, 60)

    def _prune_failed_bots(
        self,
        min_avg_sharpe: float = 0.0,
        min_avg_roi: float = -0.05,
        min_runs: int = 2,
    ) -> int:
        """Başarısız botların tüm deneylerini sil. Memory store üzerinde çalışır."""
        import sqlite3
        try:
            conn = self.memory.store._conn
            rows = conn.execute("SELECT id, data FROM experiments").fetchall()
        except Exception:
            return 0
        from collections import defaultdict
        by_bot: dict[str, list] = defaultdict(list)
        for r in rows:
            try:
                d = json.loads(r["data"]) if isinstance(r["data"], str) else r["data"]
                inner = d.get("result") if isinstance(d.get("result"), dict) else d
            except Exception:
                continue
            fam = inner.get("strategy_family") or d.get("strategy") or ""
            params = inner.get("strategy_params") or d.get("params") or {}
            # Basit fingerprint — server'daki ile aynı olacak şekilde deterministik
            import hashlib
            payload = f"{fam}|" + "|".join(f"{k}={params[k]}" for k in sorted(params.keys()))
            bid = hashlib.md5(payload.encode("utf-8")).hexdigest()[:10]
            by_bot[bid].append((r["id"], inner))

        ids_to_delete: list[str] = []
        for bid, runs in by_bot.items():
            if len(runs) < min_runs:
                continue
            avg_s = sum((r[1].get("sharpe") or 0) for r in runs) / len(runs)
            avg_r = sum((r[1].get("roi") or 0) for r in runs) / len(runs)
            if avg_s < min_avg_sharpe and avg_r < min_avg_roi:
                ids_to_delete.extend([r[0] for r in runs])

        if not ids_to_delete:
            return 0
        placeholders = ",".join("?" for _ in ids_to_delete)
        conn.execute(f"DELETE FROM experiments WHERE id IN ({placeholders})", ids_to_delete)
        conn.commit()
        return len(ids_to_delete)

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
                self._check_winner(cycle_result)

                # Every 10 cycles, print executive brief
                if self.total_cycles % 10 == 0:
                    self._print_executive_brief()

                # Her 200 cycle'da failed-bot prune
                if self.total_cycles > 0 and self.total_cycles % 200 == 0:
                    try:
                        n = self._prune_failed_bots()
                        if n > 0:
                            console.print(f"[bold red]🗑️ Auto-prune:[/bold red] {n} başarısız bot deneyi silindi")
                    except Exception as _e:
                        logger.warning(f"prune failed: {_e}")

                # Auto-approve check her 10 cycle (dakika mertebesi)
                if self.total_cycles > 0 and self.total_cycles % self._auto_approve_interval == 0:
                    try:
                        aa_report = self.auto_approver.run()
                        if aa_report.get("approved") or aa_report.get("rejected"):
                            console.print(
                                f"[bold yellow]🤖 AutoApprover:[/bold yellow] "
                                f"approved {aa_report['approved']}, rejected {aa_report['rejected']}"
                            )
                    except Exception as _e:
                        logger.warning(f"auto-approver failed: {_e}")

                # HR review her N cycle'da bir
                if self.total_cycles > 0 and self.total_cycles % self._hr_review_interval == 0:
                    try:
                        review = self.hr.review(cycle_number=self.total_cycles)
                        if review.get("applied"):
                            console.print(
                                f"[bold cyan]👑 HR review (cycle {self.total_cycles}):[/bold cyan] "
                                f"{len(review['applied'])} change(s): "
                                + ", ".join(f"{a['action']} {a['agent']}" for a in review["applied"])
                            )
                    except Exception as _e:
                        logger.warning(f"HR review failed: {_e}")

                # Adaptive pause
                pause = self._adaptive_pause()
                time.sleep(pause)

        except KeyboardInterrupt:
            console.print("\n[bold yellow]Autonomous mode stopped by user.[/bold yellow]")

        self._print_final_report()

    # ------------------------------------------------------------------
    # Winner detection
    # ------------------------------------------------------------------

    # Hedef: yıllık >=60% CAGR + Sharpe >=1.5 + DD >=-15% + >=100 trade + promoted
    WINNER_CAGR_MIN: float = 0.60
    WINNER_SHARPE_MIN: float = 1.5
    WINNER_MAX_DD_MIN: float = -0.15
    WINNER_TRADES_MIN: int = 100

    def _check_winner(self, cr: "CycleResult") -> None:
        r = cr.result
        if not r.promoted:
            return
        if (
            r.cagr >= self.WINNER_CAGR_MIN
            and r.sharpe >= self.WINNER_SHARPE_MIN
            and r.max_drawdown >= self.WINNER_MAX_DD_MIN
            and r.total_trades >= self.WINNER_TRADES_MIN
        ):
            banner = (
                "\n"
                + "*" * 78 + "\n"
                + f"*** WINNER FOUND — cycle {self.total_cycles} ***\n"
                + f"*** {cr.hypothesis.title}\n"
                + f"*** CAGR={r.cagr:.1%} Sharpe={r.sharpe:.2f} "
                  f"DD={r.max_drawdown:.1%} trades={r.total_trades} WR={r.win_rate:.1%}\n"
                + "*" * 78 + "\n"
            )
            console.print(f"[bold green]{banner}[/bold green]")

            import json as _json
            winners_file = Path("artifacts/winners.jsonl")
            winners_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "cycle": self.total_cycles,
                "title": cr.hypothesis.title,
                "market": cr.hypothesis.market,
                "strategy_family": cr.hypothesis.strategy_family,
                "timeframe": cr.hypothesis.timeframe,
                "cagr": r.cagr,
                "sharpe": r.sharpe,
                "max_drawdown": r.max_drawdown,
                "profit_factor": r.profit_factor,
                "total_trades": r.total_trades,
                "win_rate": r.win_rate,
                "stability_score": r.stability_score,
                "experiment_id": r.experiment_id,
                "discovered_at": _now().isoformat(),
            }
            with open(winners_file, "a", encoding="utf-8") as fh:
                fh.write(_json.dumps(payload) + "\n")

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
