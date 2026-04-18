from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from oto_bot.agents.debate import AgentDebater, DebateRecord
from oto_bot.agents.registry import AgentRegistry
from oto_bot.core.models import AgentProfile, ExecutiveDecision, ExperimentResult
from oto_bot.memory.manager import MemoryManager


class CEOAgent:
    """Executive agent: reviews experiments, triggers debates, decides promotions."""

    # Promotion thresholds
    SHARPE_MIN = 1.2
    MAX_DD_MIN = -0.12
    PF_MIN = 1.2
    STABILITY_MIN = 0.6

    def __init__(
        self,
        registry: AgentRegistry,
        memory_manager: MemoryManager | None = None,
    ) -> None:
        self.registry = registry
        self.memory = memory_manager or MemoryManager()
        self._debater = AgentDebater()

    # ------------------------------------------------------------------
    # Agent management (unchanged API)
    # ------------------------------------------------------------------

    def hire_agent(self, name: str, role: str, department: str, mandate: str) -> AgentProfile:
        profile = AgentProfile(name=name, role=role, department=department, mandate=mandate)
        return self.registry.add(profile)

    def fire_agent(self, agent_name: str) -> bool:
        agent = self.registry.find_by_name(agent_name)
        if not agent:
            return False
        self.registry.retire(agent.agent_id)
        return True

    # ------------------------------------------------------------------
    # Experiment review (enhanced)
    # ------------------------------------------------------------------

    def review_experiment(self, result: ExperimentResult) -> ExecutiveDecision:
        """
        1. Pull recent history from memory
        2. Check improvement trend
        3. Trigger debate
        4. Decide based on debate + thresholds + trend
        5. Return detailed ExecutiveDecision
        """
        result_data = asdict(result)
        result_data["created_at"] = result.created_at.isoformat()

        # 1. Trend analysis (result should already be saved by caller)
        trend = self.memory.get_improvement_trend(result.strategy_family)

        # 3. Debate
        debate = self._debater.debate(
            topic=f"Review: {result.hypothesis_title} ({result.strategy_family})",
            experiment_result=result_data,
            memory_manager=self.memory,
        )

        # 4. Threshold check
        passes_thresholds = (
            result.sharpe >= self.SHARPE_MIN
            and result.max_drawdown >= self.MAX_DD_MIN
            and result.profit_factor >= self.PF_MIN
            and result.stability_score >= self.STABILITY_MIN
        )

        # 5. Decision logic
        is_declining = trend.get("trend") == "declining"
        debate_approved = debate.conclusion.startswith("APPROVE") or debate.conclusion.startswith("CONDITIONAL APPROVE")

        if passes_thresholds and debate_approved and not is_declining:
            decision = ExecutiveDecision(
                decision="promote_to_paper_trading",
                reasoning=(
                    f"Thresholds passed. Debate: {debate.conclusion} "
                    f"Trend: {trend.get('trend', 'n/a')} (sharpe delta={trend.get('sharpe_delta', 'n/a')})."
                ),
                action_items=[
                    "Paper trade candidate with kill-switch enabled",
                    "Run expanded regime checks",
                    "Monitor for 5 trading days minimum",
                ],
            )
        elif passes_thresholds and is_declining:
            decision = ExecutiveDecision(
                decision="hold_and_monitor",
                reasoning=(
                    f"Thresholds passed but strategy trend is declining "
                    f"(sharpe delta={trend.get('sharpe_delta', 'n/a')}). "
                    "Hold promotion until trend stabilizes."
                ),
                action_items=[
                    "Investigate performance degradation",
                    "Check for regime shift",
                    "Re-test with recent data window",
                ],
            )
        elif passes_thresholds and not debate_approved:
            decision = ExecutiveDecision(
                decision="hold_and_iterate",
                reasoning=(
                    f"Thresholds passed but debate raised concerns: {debate.conclusion}"
                ),
                action_items=[
                    "Address debate concerns",
                    "Run additional robustness checks",
                    "Re-submit for review",
                ],
            )
        else:
            failed: list[str] = []
            if result.sharpe < self.SHARPE_MIN:
                failed.append(f"sharpe={result.sharpe:.2f} < {self.SHARPE_MIN}")
            if result.max_drawdown < self.MAX_DD_MIN:
                failed.append(f"max_dd={result.max_drawdown:.2%} < {self.MAX_DD_MIN:.2%}")
            if result.profit_factor < self.PF_MIN:
                failed.append(f"pf={result.profit_factor:.2f} < {self.PF_MIN}")
            if result.stability_score < self.STABILITY_MIN:
                failed.append(f"stability={result.stability_score:.2f} < {self.STABILITY_MIN}")

            decision = ExecutiveDecision(
                decision="hold_and_iterate",
                reasoning=(
                    f"Failed thresholds: {'; '.join(failed)}. "
                    f"Debate: {debate.conclusion} "
                    f"Trend: {trend.get('trend', 'n/a')}."
                ),
                action_items=[
                    "Run postmortem analysis",
                    "Adjust parameters conservatively",
                    "Retest with walk-forward validation",
                ],
            )

        self.memory.save_decision(decision)
        return decision

    # ------------------------------------------------------------------
    # Daily brief
    # ------------------------------------------------------------------

    def generate_daily_brief(self, memory_manager: MemoryManager | None = None) -> str:
        mm = memory_manager or self.memory
        recent = mm.get_recent_results(n=20)
        best = mm.get_best_results(metric="sharpe", n=3)
        failures = mm.get_failure_patterns()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        lines: list[str] = [
            f"=== DAILY EXECUTIVE BRIEF — {today} ===",
            "",
            f"Experiments reviewed (last 20): {len(recent)}",
        ]

        # Best performers
        lines.append("")
        lines.append("--- TOP PERFORMERS (by Sharpe) ---")
        for i, r in enumerate(best, 1):
            d = r["data"]
            lines.append(
                f"  {i}. {d.get('hypothesis_title', 'N/A')} | "
                f"Sharpe={d.get('sharpe', 0):.2f} | "
                f"PF={d.get('profit_factor', 0):.2f} | "
                f"DD={d.get('max_drawdown', 0):.2%}"
            )

        # Worst / failure patterns
        lines.append("")
        lines.append("--- FAILURE PATTERNS ---")
        lines.append(f"  Total failures recorded: {failures['total_failures']}")
        for reason, cnt in failures.get("reason_counts", {}).items():
            lines.append(f"  - {reason}: {cnt}")

        # Hiring / firing recommendations
        lines.append("")
        lines.append("--- STAFFING RECOMMENDATIONS ---")
        active_agents = self.registry.active()
        if len(active_agents) < 8:
            lines.append("  Consider hiring: specialist agents may be needed for emerging workloads.")
        else:
            lines.append(f"  Active agents: {len(active_agents)} — staffing adequate.")

        # Next priorities
        lines.append("")
        lines.append("--- NEXT PRIORITIES ---")
        if failures["total_failures"] > 0:
            top_failing = max(failures.get("strategy_counts", {}), key=failures["strategy_counts"].get, default=None)
            if top_failing:
                lines.append(f"  1. Investigate repeated failures in '{top_failing}' strategy family.")
        lines.append("  2. Expand regime-diversity testing for top performers.")
        lines.append("  3. Compress old memories if backlog exceeds 30 days.")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Suggest next experiments
    # ------------------------------------------------------------------

    def suggest_next_experiments(self, memory_manager: MemoryManager | None = None) -> list[dict[str, Any]]:
        mm = memory_manager or self.memory
        recent = mm.get_recent_results(n=50)
        best = mm.get_best_results(metric="sharpe", n=5)
        failures = mm.get_failure_patterns()

        suggestions: list[dict[str, Any]] = []

        # 1. Parameter variations for promising strategies
        seen_strategies: set[str] = set()
        for r in best:
            d = r["data"]
            strat = d.get("strategy_family", "unknown")
            if strat in seen_strategies:
                continue
            seen_strategies.add(strat)
            suggestions.append({
                "type": "parameter_variation",
                "strategy_family": strat,
                "reason": f"Top performer (Sharpe={d.get('sharpe', 0):.2f}). Test adjacent parameter sets.",
                "suggested_actions": [
                    "Widen stop-loss by 10-20%",
                    "Test on adjacent timeframes",
                    "Run on different market pairs",
                ],
            })

        # 2. Avoid repeating failed configurations
        failed_strategies = set(failures.get("strategy_counts", {}).keys())
        failed_markets = set(failures.get("market_counts", {}).keys())

        # 3. Identify untested combinations
        tested_combos: set[tuple[str, str]] = set()
        for r in recent:
            d = r["data"]
            tested_combos.add((d.get("strategy_family", ""), d.get("market", "")))

        # Suggest new market/strategy combos
        all_strategies = seen_strategies | failed_strategies
        known_markets = {"BTC/USDT", "ETH/USDT", "EUR/USD", "SPY", "BIST100"}
        for strat in all_strategies:
            for market in known_markets:
                if (strat, market) not in tested_combos:
                    suggestions.append({
                        "type": "new_combination",
                        "strategy_family": strat,
                        "market": market,
                        "reason": f"Untested combination: {strat} on {market}.",
                    })
                    if len(suggestions) >= 10:
                        break
            if len(suggestions) >= 10:
                break

        return suggestions
