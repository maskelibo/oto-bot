"""Structured institutional debate between agent perspectives.

Önceki sürüm 4 perspektif içeriyordu (Risk, Quant, Strategy, Analytics).
Bu sürüm tier-1 masa pratiklerini yansıtır ve 8 sesli bir panel sunar:

    - Sentinel Risk         — tail risk, drawdown, leverage
    - Sigma Quant           — statistical significance, overfitting
    - Nova StrategyRND      — edge narrative, regime alignment
    - Pulse Analytics       — metric consistency, asymmetry
    - Mercury Macro         — macro/cross-asset overlay
    - Apex Portfolio Risk   — book-level exposure, VaR, correlation
    - Tariq TCA             — execution realism, slippage sanity
    - Cassandra PreMortem   — failure-mode scan

Her perspektif bir "position" (ör. supportive / cautious) + gerekçe + kanıt
döndürür. Son sözü CEO kapanışı söyler; kapanış nitelikli çoğunluk +
tehlike flag'lerine göre ağırlıklı karar verir.
"""

from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

logger = logging.getLogger("oto_bot.debate")

if TYPE_CHECKING:
    from oto_bot.memory.manager import MemoryManager


@dataclass
class DebateRecord:
    debate_id: str
    topic: str
    participants: list[str]
    arguments: list[dict[str, Any]]
    conclusion: str
    severity: str = "info"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Perspektif üreticiler
# ---------------------------------------------------------------------------


def _risk_perspective(result: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    dd = result.get("max_drawdown", 0.0)
    mc_dd = result.get("montecarlo_95_drawdown")
    calmar = result.get("calmar", 0.0)

    issues: list[str] = []
    if dd < -0.15:
        issues.append(f"Max drawdown {dd:.2%} exceeds -15% comfort zone.")
    if mc_dd is not None and mc_dd < -0.20:
        issues.append(f"Monte Carlo 95%-DD {mc_dd:.2%} is severe.")
    if calmar < 0.5:
        issues.append(f"Calmar {calmar:.2f} indicates poor return/DD.")

    position = "caution" if issues else "acceptable"
    reasoning = " ".join(issues) if issues else "Drawdown and tail metrics in bounds."
    return {
        "agent_name": "Sentinel Risk",
        "position": position,
        "reasoning": reasoning,
        "evidence": {"max_drawdown": dd, "montecarlo_95_drawdown": mc_dd, "calmar": calmar},
    }


def _quant_perspective(result: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    sharpe = result.get("sharpe", 0.0)
    trades = result.get("total_trades", 0)
    wf = result.get("walkforward_sharpe")
    stab = result.get("stability_score", 0.0)

    issues: list[str] = []
    if trades < 100:
        issues.append(f"Only {trades} trades — low statistical power.")
    if wf is not None and sharpe > 0 and wf / max(sharpe, 0.01) < 0.5:
        issues.append("WF Sharpe < 50% of IS — overfitting risk.")
    if stab < 0.5:
        issues.append(f"Stability {stab:.2f} is low.")

    position = "skeptical" if issues else "confident"
    reasoning = " ".join(issues) if issues else (
        f"Sharpe {sharpe:.2f} / {trades} trades — sound."
    )
    return {
        "agent_name": "Sigma Quant",
        "position": position,
        "reasoning": reasoning,
        "evidence": {
            "sharpe": sharpe, "total_trades": trades,
            "walkforward_sharpe": wf, "stability_score": stab,
        },
    }


def _strategy_perspective(result: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    regime = result.get("regime", "unknown")
    family = result.get("strategy_family", "unknown")
    pf = result.get("profit_factor", 0.0)

    issues: list[str] = []
    if regime == "unknown":
        issues.append("Regime not identified — edge source unclear.")
    if pf < 1.1:
        issues.append(f"PF {pf:.2f} marginal; fees may kill edge.")

    position = "uncertain" if issues else "supportive"
    reasoning = " ".join(issues) if issues else (
        f"'{family}' shows edge in {regime} regime (PF={pf:.2f})."
    )
    return {
        "agent_name": "Nova StrategyRND",
        "position": position,
        "reasoning": reasoning,
        "evidence": {"regime": regime, "strategy_family": family, "profit_factor": pf},
    }


def _analytics_perspective(result: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    sharpe = result.get("sharpe", 0.0)
    sortino = result.get("sortino", 0.0)
    wr = result.get("win_rate", 0.0)
    exp = result.get("expectancy", 0.0)

    issues: list[str] = []
    if sharpe > 0 and sortino > 0 and abs(sortino - sharpe) / max(sharpe, 0.01) > 1.0:
        issues.append("Large Sharpe/Sortino gap — asymmetric distribution.")
    if wr > 0.75 and exp < 0.01:
        issues.append("High WR with low expectancy — many small wins / few big losses.")
    if exp < 0:
        issues.append(f"Negative expectancy ({exp:.4f}).")

    position = "flagged" if issues else "positive"
    reasoning = " ".join(issues) if issues else (
        f"Metrics consistent: Sharpe={sharpe:.2f}, Sortino={sortino:.2f}, exp={exp:.4f}."
    )
    return {
        "agent_name": "Pulse Analytics",
        "position": position,
        "reasoning": reasoning,
        "evidence": {"sharpe": sharpe, "sortino": sortino, "win_rate": wr, "expectancy": exp},
    }


def _macro_perspective(result: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """ctx['macro'] beklenir: {bias, risk_on_score, regime_label}."""
    macro = ctx.get("macro") or {}
    bias = macro.get("bias", "neutral")
    score = macro.get("risk_on_score", 0.0)
    regime_label = macro.get("regime_label", "unknown")

    family = result.get("strategy_family", "unknown")
    issues: list[str] = []
    if bias == "crisis":
        issues.append(f"Macro crisis ({score:+.2f}) — withhold promotion until regime stabilizes.")
    elif bias == "risk_off" and family in {"scalp", "day"}:
        issues.append(f"Risk-off macro ({score:+.2f}) — short-horizon strats under stress.")

    position = "block" if bias == "crisis" else ("caution" if issues else "supportive")
    reasoning = " ".join(issues) if issues else (
        f"Macro {bias} ({regime_label}); conducive for '{family}'."
    )
    return {
        "agent_name": "Mercury Macro",
        "position": position,
        "reasoning": reasoning,
        "evidence": {"bias": bias, "risk_on_score": score, "regime_label": regime_label},
    }


def _portfolio_risk_perspective(result: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """ctx['apex'] beklenir: {status, score, breaches, recommendations}."""
    apex = ctx.get("apex") or {}
    status = apex.get("status", "green")
    breaches = apex.get("breaches") or []

    if status in ("red", "black"):
        position = "block"
        reasoning = f"Book risk status '{status}'. Breaches: {'; '.join(breaches[:2])}."
    elif status == "amber":
        position = "caution"
        reasoning = f"Book risk amber. Breaches: {'; '.join(breaches[:2])}."
    else:
        position = "supportive"
        reasoning = "Book-level risk green; adding this strategy is permissible."

    return {
        "agent_name": "Apex Portfolio Risk",
        "position": position,
        "reasoning": reasoning,
        "evidence": {"status": status, "breaches": breaches[:3]},
    }


def _tca_perspective(result: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """ctx['tca'] beklenir: {avg_slippage_bps, avg_market_impact_bps, alerts}."""
    tca = ctx.get("tca") or {}
    slip = tca.get("avg_slippage_bps")
    impact = tca.get("avg_market_impact_bps")
    alerts = tca.get("alerts") or []

    if not tca:
        return {
            "agent_name": "Tariq TCA",
            "position": "caution",
            "reasoning": "No TCA data — execution realism unverified.",
            "evidence": {},
        }

    if alerts:
        position = "caution"
        reasoning = "Execution warnings: " + "; ".join(alerts[:2])
    else:
        position = "supportive"
        reasoning = f"Execution clean (slip≈{slip:.1f}bps, impact≈{impact:.1f}bps)."
    return {
        "agent_name": "Tariq TCA",
        "position": position,
        "reasoning": reasoning,
        "evidence": {"slippage_bps": slip, "market_impact_bps": impact, "alerts": alerts},
    }


def _premortem_perspective(result: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """ctx['premortem'] beklenir: {risk_score, verdict, top_risks}."""
    pm = ctx.get("premortem") or {}
    score = pm.get("risk_score")
    verdict = pm.get("verdict", "unknown")
    risks = pm.get("top_risks") or []

    if score is None:
        return {
            "agent_name": "Cassandra PreMortem",
            "position": "caution",
            "reasoning": "Pre-mortem not run.",
            "evidence": {},
        }
    if score >= 70:
        position = "block"
    elif score >= 40:
        position = "caution"
    else:
        position = "supportive"
    reasoning = f"Pre-mortem risk {score:.0f}/100 — {verdict}. Top: {'; '.join(risks[:2])}"
    return {
        "agent_name": "Cassandra PreMortem",
        "position": position,
        "reasoning": reasoning,
        "evidence": {"risk_score": score, "verdict": verdict, "top_risks": risks[:3]},
    }


# ---------------------------------------------------------------------------
# CEO kapanışı
# ---------------------------------------------------------------------------


_NEGATIVE_POSITIONS = {"caution", "skeptical", "uncertain", "flagged"}
_BLOCKER_POSITIONS = {"block"}


def _ceo_conclusion(arguments: list[dict[str, Any]]) -> tuple[str, str]:
    """Ağırlıklı karar: tek 'block' oyu bile veto sayılır."""
    blockers = [a for a in arguments if a["position"] in _BLOCKER_POSITIONS]
    negatives = [a for a in arguments if a["position"] in _NEGATIVE_POSITIONS]
    total = len(arguments)

    if blockers:
        names = ", ".join(a["agent_name"] for a in blockers)
        return (
            f"REJECT (veto): {names} blocked promotion.",
            "critical",
        )
    if not negatives:
        return (
            "APPROVE: full consensus; proceed to next lifecycle stage.",
            "info",
        )
    if len(negatives) == 1:
        who = negatives[0]["agent_name"]
        return (
            f"CONDITIONAL APPROVE: one caution from {who}. Proceed with enhanced monitoring.",
            "warning",
        )
    if len(negatives) <= max(2, total // 3):
        names = ", ".join(a["agent_name"] for a in negatives)
        return (
            f"HOLD: concerns from {names}. Iterate before re-review.",
            "warning",
        )
    return (
        f"REJECT: {len(negatives)}/{total} flagged. Significant rework required.",
        "critical",
    )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


_PERSPECTIVE_FUNCS = {
    "Sentinel Risk": _risk_perspective,
    "Sigma Quant": _quant_perspective,
    "Nova StrategyRND": _strategy_perspective,
    "Pulse Analytics": _analytics_perspective,
    "Mercury Macro": _macro_perspective,
    "Apex Portfolio Risk": _portfolio_risk_perspective,
    "Tariq TCA": _tca_perspective,
    "Cassandra PreMortem": _premortem_perspective,
}

_DEFAULT_PARTICIPANTS = list(_PERSPECTIVE_FUNCS.keys()) + ["Atlas CEO"]


class AgentDebater:
    """8-sesli kurumsal panel."""

    def debate(
        self,
        topic: str,
        experiment_result: dict[str, Any] | Any,
        participants: list[str] | None = None,
        memory_manager: "MemoryManager | None" = None,
        context: dict[str, Any] | None = None,
    ) -> DebateRecord:
        if not isinstance(experiment_result, dict):
            from dataclasses import asdict as _asdict
            experiment_result = _asdict(experiment_result)

        context = context or {}
        participants = participants or _DEFAULT_PARTICIPANTS

        # Faz 3 — perspektifleri paralel çalıştır (her biri pure fonksiyon,
        # GIL'i NumPy/pandas okumalarda bırakır → ~4s'den ~1.5s'e iner).
        active_names: list[str] = [
            n for n in participants
            if n != "Atlas CEO" and n in _PERSPECTIVE_FUNCS
        ]
        results_by_name: dict[str, dict[str, Any]] = {}
        if active_names:
            with ThreadPoolExecutor(
                max_workers=min(4, len(active_names)),
                thread_name_prefix="debate-perspective",
            ) as ex:
                futures = {
                    ex.submit(_PERSPECTIVE_FUNCS[name], experiment_result, context): name
                    for name in active_names
                }
                for fut in as_completed(futures):
                    name = futures[fut]
                    try:
                        results_by_name[name] = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        # Bir perspektif kırılırsa geri kalanı bozma.
                        logger.warning(f"perspective '{name}' failed: {exc}")

        # Çıktı sırasını giriş katılımcı sırasıyla hizala (deterministik log).
        arguments: list[dict[str, Any]] = [
            results_by_name[n] for n in active_names if n in results_by_name
        ]

        conclusion, severity = _ceo_conclusion(arguments)

        record = DebateRecord(
            debate_id=str(uuid.uuid4()),
            topic=topic,
            participants=participants,
            arguments=arguments,
            conclusion=conclusion,
            severity=severity,
        )

        if memory_manager is not None:
            data = asdict(record)
            data["timestamp"] = record.timestamp.isoformat()
            memory_manager.save_debate(data)

        return record
