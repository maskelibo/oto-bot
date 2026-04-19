"""Atlas CEO — institutional Head of Trading.

Kurumsal seviye karar verici. BofA / GS / Citadel masa başkanı gibi düşünür:

    * Sabah briefing'i üretir (regime, macro, book riski, top/bottom pod).
    * Pod sermaye tahsislerini rebalans eder (Sharpe + DD + korelasyon).
    * Committee oturumu (Investment + Risk) ile strateji promotion kararı.
    * Pre-mortem + post-mortem tetikler.
    * Hire/fire & agent lifecycle kararı verir.

Bu sınıf eski `review_experiment` API'sini korur ama karar protokolü
enstitüsyonel hale getirilmiştir:

    1. Apex Portfolio Risk değerlendirmesi (veto yetkisi)
    2. Cassandra PreMortem (veto yetkisi)
    3. Mercury Macro overlay (bias)
    4. Sigma/Sentinel/Nova/Pulse (ayrıntılı perspektifler)
    5. Tariq TCA (exekusyon gerçekçiliği)
    6. CEO ağırlıklı karar
    7. Decision + action_items + committee topics

Dış bağımlılıkları tamamen opsiyonel tutulur; pod_allocator / stress_lab /
macro sağlanmazsa CEO varsayılanlarla çalışır — bu sayede eski çağrı
imzası kırılmaz.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from oto_bot.agents.debate import AgentDebater
from oto_bot.agents.pod_allocator import PodAllocator
from oto_bot.agents.portfolio_risk import ApexPortfolioRisk, RiskVerdict
from oto_bot.agents.premortem import CassandraPreMortem
from oto_bot.agents.registry import AgentRegistry
from oto_bot.core.models import (
    AgentProfile,
    CommitteeVerdict,
    ExecutiveDecision,
    ExperimentResult,
    MorningBriefing,
    PortfolioSnapshot,
)
from oto_bot.memory.manager import MemoryManager


class CEOAgent:
    """Head of Trading davranışlı, committee-temelli executive ajan."""

    # Promotion eşikleri (HoT'ın kendi takdirini ağırlıklandırır, hard gate değildir)
    SHARPE_MIN = 1.2
    MAX_DD_MIN = -0.12
    PF_MIN = 1.2
    STABILITY_MIN = 0.6

    def __init__(
        self,
        registry: AgentRegistry,
        memory_manager: MemoryManager | None = None,
        pod_allocator: PodAllocator | None = None,
        portfolio_risk: ApexPortfolioRisk | None = None,
        pre_mortem: CassandraPreMortem | None = None,
    ) -> None:
        self.registry = registry
        self.memory = memory_manager or MemoryManager()
        self._debater = AgentDebater()
        self.pod_allocator = pod_allocator
        self.portfolio_risk = portfolio_risk or ApexPortfolioRisk()
        self.pre_mortem = pre_mortem or CassandraPreMortem()

    # ------------------------------------------------------------------
    # Agent management
    # ------------------------------------------------------------------

    def hire_agent(self, name: str, role: str, department: str, mandate: str) -> AgentProfile:
        return self.registry.add(AgentProfile(name=name, role=role, department=department, mandate=mandate))

    def fire_agent(self, agent_name: str) -> bool:
        agent = self.registry.find_by_name(agent_name)
        if not agent:
            return False
        self.registry.retire(agent.agent_id)
        return True

    # ------------------------------------------------------------------
    # Experiment review — kurumsal committee protokolü
    # ------------------------------------------------------------------

    def review_experiment(
        self,
        result: ExperimentResult,
        macro_context: dict[str, Any] | None = None,
        tca_summary: dict[str, Any] | None = None,
        apex_verdict: RiskVerdict | None = None,
        book_correlation: float | None = None,
    ) -> ExecutiveDecision:
        result_data = asdict(result)
        result_data["created_at"] = result.created_at.isoformat()

        # Trend
        trend = self.memory.get_improvement_trend(result.strategy_family)

        # Pre-mortem
        pm_report = self.pre_mortem.evaluate(
            strategy_family=result.strategy_family,
            result=result_data,
            book_correlation=book_correlation,
        )

        # Debate context: bütün kurumsal sesleri besle
        debate_ctx: dict[str, Any] = {}
        if macro_context:
            debate_ctx["macro"] = macro_context
        if tca_summary:
            debate_ctx["tca"] = tca_summary
        if apex_verdict:
            debate_ctx["apex"] = {
                "status": apex_verdict.status,
                "score": apex_verdict.score,
                "breaches": apex_verdict.breaches,
                "recommendations": apex_verdict.recommendations,
            }
        debate_ctx["premortem"] = {
            "risk_score": pm_report.risk_score,
            "verdict": pm_report.verdict,
            "top_risks": pm_report.top_risks,
        }

        debate = self._debater.debate(
            topic=f"Promotion review: {result.hypothesis_title} ({result.strategy_family})",
            experiment_result=result_data,
            memory_manager=self.memory,
            context=debate_ctx,
        )

        # Eşik kontrolleri — HoT'ın yorumu için bilgi
        passes_thresholds = (
            result.sharpe >= self.SHARPE_MIN
            and result.max_drawdown >= self.MAX_DD_MIN
            and result.profit_factor >= self.PF_MIN
            and result.stability_score >= self.STABILITY_MIN
        )

        is_declining = trend.get("trend") == "declining"
        conclusion = debate.conclusion
        debate_approved = conclusion.startswith("APPROVE") or conclusion.startswith("CONDITIONAL APPROVE")
        debate_rejected = conclusion.startswith("REJECT")
        pm_red = pm_report.risk_score >= 70
        apex_blocked = apex_verdict and apex_verdict.status in ("red", "black")

        # Long-horizon bias: day/swing için kısa pencere promote yok
        # Koray'ın doktrinel ricası: 3 günlük testler anlamsız, minimum 6 ay gerekli
        duration_bars = getattr(result, "duration_bars", 0) or 0
        tf = getattr(result, "bar_timeframe", "1h") or "1h"
        tf_minutes = {"1m":1,"5m":5,"15m":15,"30m":30,"1h":60,"4h":240,"1d":1440}.get(tf, 60)
        duration_months = (duration_bars * tf_minutes) / (60 * 24 * 30) if duration_bars else 0
        is_short_window_promote_family = result.strategy_family in ("day", "swing")
        short_window_block = (
            is_short_window_promote_family and duration_months < 6
        )

        # Karar ağacı
        if short_window_block:
            decision = ExecutiveDecision(
                decision="reject_short_window",
                reasoning=(
                    f"Short-window reject: {result.strategy_family} ailesi için backtest penceresi "
                    f"{duration_months:.1f} ay (min 6 ay şart). 3-günlük pozitif sonuçlar "
                    f"12 ay boyunca tutmaz — uzun vade testi olmadan promote yok."
                ),
                action_items=[
                    f"Daha uzun veri penceresinde yeniden test et (min 6 ay)",
                    "1h timeframe'de ≥8760 bar, 4h'da ≥4380 bar kullan",
                    "Yeniden submit et",
                ],
            )
            self.memory.save_decision(decision)
            return decision

        if apex_blocked:
            decision = ExecutiveDecision(
                decision="block_book_risk",
                reasoning=(
                    f"Apex flagged book-level risk ({apex_verdict.status}). "
                    f"Breaches: {'; '.join(apex_verdict.breaches[:2])}"
                ),
                action_items=[
                    "Address book-level breaches before any new promotion",
                    *apex_verdict.recommendations[:3],
                ],
            )
        elif pm_red:
            decision = ExecutiveDecision(
                decision="block_premortem",
                reasoning=f"Pre-mortem RED (score {pm_report.risk_score:.0f}). Top risks: {'; '.join(pm_report.top_risks[:2])}",
                action_items=[
                    "Remediate top pre-mortem risks",
                    "Re-run backtest with realistic assumptions",
                    "Expand out-of-sample window",
                ],
            )
        elif debate_rejected:
            decision = ExecutiveDecision(
                decision="reject",
                reasoning=f"Panel rejected promotion. {conclusion}",
                action_items=[
                    "Full postmortem with failure-mode taxonomy",
                    "Document anti-pattern to failure_memory",
                    "Do not retest this configuration",
                ],
            )
        elif passes_thresholds and debate_approved and not is_declining:
            decision = ExecutiveDecision(
                decision="promote_to_paper_trading",
                reasoning=(
                    f"Thresholds met; panel conclusion: {conclusion}. "
                    f"Trend={trend.get('trend', 'n/a')} (delta={trend.get('sharpe_delta', 'n/a')}). "
                    f"Pre-mortem={pm_report.risk_score:.0f}/100."
                ),
                action_items=[
                    "Open paper trading pod with kill-switch armed",
                    "Run weekly committee re-review for 4 weeks",
                    "Monitor book correlation; halve if > 0.5 in first 2 weeks",
                    "Trigger stress lab on top-3 scenarios",
                ],
            )
        elif passes_thresholds and is_declining:
            decision = ExecutiveDecision(
                decision="hold_and_monitor",
                reasoning=(
                    f"Thresholds met but Sharpe trend declining (delta={trend.get('sharpe_delta', 'n/a')}). "
                    "Hold promotion until regime stabilizes."
                ),
                action_items=[
                    "Investigate cause of degradation (regime shift vs. parameter decay)",
                    "Re-test with latest 30-bar window",
                    "Flag to Iris COS for follow-up next cycle",
                ],
            )
        elif passes_thresholds and not debate_approved:
            decision = ExecutiveDecision(
                decision="hold_and_iterate",
                reasoning=(
                    f"Thresholds met but panel raised concerns. {conclusion}"
                ),
                action_items=[
                    "Address concerns from debate (see arguments)",
                    "Re-run robustness gate (WF + MC)",
                    "Resubmit after mitigation",
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
                    f"Panel: {conclusion}. Trend: {trend.get('trend', 'n/a')}."
                ),
                action_items=[
                    "Postmortem & failure taxonomy",
                    "Adjust params conservatively — avoid overfit",
                    "Retest with walk-forward validation",
                ],
            )

        self.memory.save_decision(decision)
        return decision

    # ------------------------------------------------------------------
    # Morning briefing — Head of Trading'in günlük ritüeli
    # ------------------------------------------------------------------

    def morning_briefing(
        self,
        portfolio_snapshot: PortfolioSnapshot | None = None,
        regime_summaries: list[str] | None = None,
        macro_summary: str = "",
        active_alerts: list[str] | None = None,
    ) -> MorningBriefing:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        regime_text = " | ".join(regime_summaries or []) or "No regime read available."

        best = self.memory.get_best_results(metric="sharpe", n=3)
        failures = self.memory.get_failure_patterns()
        recent = self.memory.get_recent_results(n=20)

        top = [
            {
                "title": r["data"].get("hypothesis_title", "N/A"),
                "sharpe": r["data"].get("sharpe", 0),
                "pf": r["data"].get("profit_factor", 0),
                "dd": r["data"].get("max_drawdown", 0),
            }
            for r in best
        ]

        # En kötü 3 (Sharpe'ı negatif olanlar)
        ranked = sorted(
            recent,
            key=lambda r: float(r["data"].get("sharpe", 0)),
        )[:3]
        bottom = [
            {
                "title": r["data"].get("hypothesis_title", "N/A"),
                "sharpe": r["data"].get("sharpe", 0),
                "pf": r["data"].get("profit_factor", 0),
                "dd": r["data"].get("max_drawdown", 0),
            }
            for r in ranked
        ]

        capital_moves: list[str] = []
        if self.pod_allocator:
            for pod in self.pod_allocator.all():
                if pod.status == "halved":
                    capital_moves.append(
                        f"{pod.strategy_family}/{pod.market}: halved at DD {pod.drawdown_pct:.2%}"
                    )
                elif pod.status == "retired":
                    capital_moves.append(
                        f"{pod.strategy_family}/{pod.market}: RETIRED at DD {pod.drawdown_pct:.2%}"
                    )

        priorities = [
            "Execute stress scenarios on top-3 performers",
            "Rebalance pod capital by Sharpe + DD",
            "Review macro overlay before new promotions",
        ]
        if failures.get("total_failures", 0) > 0:
            top_failing_strat = max(
                failures.get("strategy_counts", {}),
                key=failures["strategy_counts"].get,
                default=None,
            )
            if top_failing_strat:
                priorities.insert(0, f"Investigate repeated failures in '{top_failing_strat}'")

        committee_topics = [
            "Weekly risk committee: review pod drawdowns, VaR breaches",
            "Weekly investment committee: vote on new paper-trade candidates",
        ]

        return MorningBriefing(
            date=today,
            regime_summary=regime_text,
            macro_summary=macro_summary or "macro data not supplied",
            portfolio_snapshot=portfolio_snapshot,
            top_performers=top,
            bottom_performers=bottom,
            active_alerts=active_alerts or [],
            capital_moves=capital_moves,
            day_priorities=priorities,
            committee_topics=committee_topics,
        )

    # ------------------------------------------------------------------
    # Geriye uyum — klasik executive brief (text)
    # ------------------------------------------------------------------

    def generate_daily_brief(self, memory_manager: MemoryManager | None = None) -> str:
        mm = memory_manager or self.memory
        recent = mm.get_recent_results(n=20)
        best = mm.get_best_results(metric="sharpe", n=3)
        failures = mm.get_failure_patterns()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        lines = [
            f"=== DAILY EXECUTIVE BRIEF — {today} ===",
            "",
            f"Experiments reviewed (last 20): {len(recent)}",
            "",
            "--- TOP PERFORMERS (by Sharpe) ---",
        ]
        for i, r in enumerate(best, 1):
            d = r["data"]
            lines.append(
                f"  {i}. {d.get('hypothesis_title', 'N/A')} | "
                f"Sharpe={d.get('sharpe', 0):.2f} | "
                f"PF={d.get('profit_factor', 0):.2f} | "
                f"DD={d.get('max_drawdown', 0):.2%}"
            )

        lines.append("")
        lines.append("--- FAILURE PATTERNS ---")
        lines.append(f"  Total failures recorded: {failures['total_failures']}")
        for reason, cnt in failures.get("reason_counts", {}).items():
            lines.append(f"  - {reason}: {cnt}")

        lines.append("")
        lines.append("--- POD STATUS ---")
        if self.pod_allocator:
            for p in self.pod_allocator.all():
                lines.append(
                    f"  {p.strategy_family}/{p.market}: cap={p.current_capital:,.0f} "
                    f"DD={p.drawdown_pct:.2%} Sharpe30={p.sharpe_30d:.2f} status={p.status}"
                )
        else:
            lines.append("  (pod_allocator not wired)")

        lines.append("")
        lines.append("--- STAFFING RECOMMENDATIONS ---")
        active_agents = self.registry.active()
        if len(active_agents) < 8:
            lines.append("  Consider hiring: specialist agents may be needed.")
        else:
            lines.append(f"  Active agents: {len(active_agents)} — staffing adequate.")

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
    # Committee session orchestration
    # ------------------------------------------------------------------

    def committee_session(
        self,
        committee: str,
        topic: str,
        supporting_data: dict[str, Any] | None = None,
    ) -> CommitteeVerdict:
        """Committee kararı: risk/investment oturumlarını modellediği kısa yapı.

        Sadece günlüğe yansımak için biçimsel; eğer supporting_data içinde bir
        ExperimentResult varsa debate ile eşleştirilebilir (çağıran yerin
        sorumluluğunda).
        """
        rationale = (supporting_data or {}).get("rationale", "")
        dissents = (supporting_data or {}).get("dissents", [])
        followups = (supporting_data or {}).get("followups", [])
        verdict = (supporting_data or {}).get("verdict", "PENDING")
        return CommitteeVerdict(
            committee=committee,
            topic=topic,
            verdict=verdict,
            rationale=rationale,
            dissents=dissents,
            followups=followups,
        )

    # ------------------------------------------------------------------
    # Suggest next experiments (korunuyor, genişletildi)
    # ------------------------------------------------------------------

    def suggest_next_experiments(
        self,
        memory_manager: MemoryManager | None = None,
    ) -> list[dict[str, Any]]:
        mm = memory_manager or self.memory
        recent = mm.get_recent_results(n=50)
        best = mm.get_best_results(metric="sharpe", n=5)
        failures = mm.get_failure_patterns()

        suggestions: list[dict[str, Any]] = []

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

        tested_combos: set[tuple[str, str]] = set()
        for r in recent:
            d = r["data"]
            tested_combos.add((d.get("strategy_family", ""), d.get("market", "")))

        failed_strategies = set(failures.get("strategy_counts", {}).keys())
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

        # Stress-lab suggestion: top performerlar için mutlaka stress test öner
        for r in best[:2]:
            d = r["data"]
            suggestions.append({
                "type": "stress_test",
                "strategy_family": d.get("strategy_family", "unknown"),
                "market": d.get("market", "unknown"),
                "reason": "Institutional protocol: promoted candidates must survive stress book.",
                "suggested_actions": [
                    "Run covid_march_2020",
                    "Run luna_may_2022",
                    "Run flash_crash_2010",
                ],
            })

        return suggestions
