from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from oto_bot.memory.manager import MemoryManager


@dataclass
class DebateRecord:
    debate_id: str
    topic: str
    participants: list[str]
    arguments: list[dict[str, Any]]
    conclusion: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Rule-based perspective generators
# ---------------------------------------------------------------------------

def _risk_perspective(result_data: dict[str, Any]) -> dict[str, Any]:
    dd = result_data.get("max_drawdown", 0.0)
    mc_dd = result_data.get("montecarlo_95_drawdown")
    calmar = result_data.get("calmar", 0.0)

    issues: list[str] = []
    if dd < -0.15:
        issues.append(f"Max drawdown {dd:.2%} exceeds -15% comfort zone.")
    if mc_dd is not None and mc_dd < -0.20:
        issues.append(f"Monte Carlo 95th-pct drawdown {mc_dd:.2%} is severe.")
    if calmar < 0.5:
        issues.append(f"Calmar ratio {calmar:.2f} indicates poor return-per-drawdown.")

    if not issues:
        position = "acceptable"
        reasoning = "Drawdown and tail risk metrics are within acceptable bounds."
    else:
        position = "caution"
        reasoning = " ".join(issues)

    return {
        "agent_name": "Sentinel Risk",
        "position": position,
        "reasoning": reasoning,
        "evidence": {"max_drawdown": dd, "montecarlo_95_drawdown": mc_dd, "calmar": calmar},
    }


def _quant_perspective(result_data: dict[str, Any]) -> dict[str, Any]:
    sharpe = result_data.get("sharpe", 0.0)
    total_trades = result_data.get("total_trades", 0)
    wf_sharpe = result_data.get("walkforward_sharpe")
    stability = result_data.get("stability_score", 0.0)

    issues: list[str] = []
    if total_trades < 100:
        issues.append(f"Only {total_trades} trades — low statistical significance.")
    if wf_sharpe is not None and sharpe > 0 and wf_sharpe / max(sharpe, 0.01) < 0.5:
        issues.append("Walk-forward Sharpe degrades heavily vs. in-sample — overfitting risk.")
    if stability < 0.5:
        issues.append(f"Stability score {stability:.2f} suggests inconsistent performance.")

    if not issues:
        position = "confident"
        reasoning = f"Sharpe {sharpe:.2f} with {total_trades} trades is statistically sound."
    else:
        position = "skeptical"
        reasoning = " ".join(issues)

    return {
        "agent_name": "Sigma Quant",
        "position": position,
        "reasoning": reasoning,
        "evidence": {
            "sharpe": sharpe,
            "total_trades": total_trades,
            "walkforward_sharpe": wf_sharpe,
            "stability_score": stability,
        },
    }


def _strategy_perspective(result_data: dict[str, Any]) -> dict[str, Any]:
    regime = result_data.get("regime", "unknown")
    strategy = result_data.get("strategy_family", "unknown")
    pf = result_data.get("profit_factor", 0.0)
    edge = result_data.get("expected_edge", "")

    issues: list[str] = []
    if regime == "unknown":
        issues.append("Market regime not identified — edge source unclear.")
    if pf < 1.1:
        issues.append(f"Profit factor {pf:.2f} is marginal; edge may vanish with fees.")

    if not issues:
        position = "supportive"
        reasoning = f"Strategy '{strategy}' shows clear edge in {regime} regime (PF={pf:.2f})."
    else:
        position = "uncertain"
        reasoning = " ".join(issues)

    return {
        "agent_name": "Nova StrategyRND",
        "position": position,
        "reasoning": reasoning,
        "evidence": {"regime": regime, "strategy_family": strategy, "profit_factor": pf},
    }


def _analytics_perspective(result_data: dict[str, Any]) -> dict[str, Any]:
    sharpe = result_data.get("sharpe", 0.0)
    sortino = result_data.get("sortino", 0.0)
    win_rate = result_data.get("win_rate", 0.0)
    expectancy = result_data.get("expectancy", 0.0)

    issues: list[str] = []
    if sharpe > 0 and sortino > 0 and abs(sortino - sharpe) / max(sharpe, 0.01) > 1.0:
        issues.append("Large Sharpe/Sortino divergence — asymmetric return distribution.")
    if win_rate > 0.75 and expectancy < 0.01:
        issues.append("High win rate but low expectancy — many small wins, few large losses pattern.")
    if expectancy < 0:
        issues.append(f"Negative expectancy ({expectancy:.4f}) — strategy loses money per trade.")

    if not issues:
        position = "positive"
        reasoning = f"Metrics are consistent: Sharpe={sharpe:.2f}, Sortino={sortino:.2f}, expectancy={expectancy:.4f}."
    else:
        position = "flagged"
        reasoning = " ".join(issues)

    return {
        "agent_name": "Pulse Analytics",
        "position": position,
        "reasoning": reasoning,
        "evidence": {
            "sharpe": sharpe,
            "sortino": sortino,
            "win_rate": win_rate,
            "expectancy": expectancy,
        },
    }


def _ceo_conclusion(arguments: list[dict[str, Any]]) -> str:
    """CEO weighs all perspectives and makes a final call."""
    negative_positions = {"caution", "skeptical", "uncertain", "flagged"}
    negatives = sum(1 for a in arguments if a["position"] in negative_positions)
    total = len(arguments)

    if negatives == 0:
        return "APPROVE: All agents support promotion. Proceed to next lifecycle stage."
    elif negatives <= 1:
        cautious = [a["agent_name"] for a in arguments if a["position"] in negative_positions]
        return (
            f"CONDITIONAL APPROVE: Minor concern from {', '.join(cautious)}. "
            "Proceed with enhanced monitoring."
        )
    elif negatives <= 2:
        return (
            f"HOLD: {negatives}/{total} agents raised concerns. "
            "Iterate on weaknesses before re-evaluation."
        )
    else:
        return (
            f"REJECT: {negatives}/{total} agents flagged issues. "
            "Strategy needs significant rework."
        )


# ---------------------------------------------------------------------------
# Main debater
# ---------------------------------------------------------------------------

_PERSPECTIVE_FUNCS = {
    "Sentinel Risk": _risk_perspective,
    "Sigma Quant": _quant_perspective,
    "Nova StrategyRND": _strategy_perspective,
    "Pulse Analytics": _analytics_perspective,
}

_DEFAULT_PARTICIPANTS = list(_PERSPECTIVE_FUNCS.keys()) + ["Atlas CEO"]


class AgentDebater:
    """Orchestrates a structured debate among rule-based agent perspectives."""

    def debate(
        self,
        topic: str,
        experiment_result: dict[str, Any] | Any,
        participants: list[str] | None = None,
        memory_manager: MemoryManager | None = None,
    ) -> DebateRecord:
        # Accept dataclass or dict
        if not isinstance(experiment_result, dict):
            from dataclasses import asdict as _asdict
            experiment_result = _asdict(experiment_result)

        participants = participants or _DEFAULT_PARTICIPANTS

        arguments: list[dict[str, Any]] = []
        for name in participants:
            if name == "Atlas CEO":
                continue  # CEO concludes at the end
            func = _PERSPECTIVE_FUNCS.get(name)
            if func:
                arguments.append(func(experiment_result))

        conclusion = _ceo_conclusion(arguments)

        record = DebateRecord(
            debate_id=str(uuid.uuid4()),
            topic=topic,
            participants=participants,
            arguments=arguments,
            conclusion=conclusion,
        )

        if memory_manager is not None:
            debate_data = asdict(record)
            debate_data["timestamp"] = record.timestamp.isoformat()
            memory_manager.save_debate(debate_data)

        return record
