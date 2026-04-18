from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
import uuid


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class AgentProfile:
    name: str
    role: str
    department: str
    mandate: str
    active: bool = True
    agent_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=now_utc)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Hypothesis:
    title: str
    thesis: str
    market: str
    strategy_family: str
    timeframe: str
    invalidation: str
    expected_edge: str
    author_agent_id: str
    created_at: datetime = field(default_factory=now_utc)


@dataclass
class ExperimentResult:
    experiment_id: str
    hypothesis_title: str
    market: str
    strategy_family: str
    roi: float
    win_rate: float
    profit_factor: float
    sharpe: float
    max_drawdown: float
    expectancy: float
    stability_score: float
    promoted: bool
    notes: str
    # --- New extended metrics ---
    sortino: float = 0.0
    cagr: float = 0.0
    calmar: float = 0.0
    total_trades: int = 0
    avg_trade_duration: float = 0.0
    regime: str = "unknown"
    walkforward_sharpe: float | None = None
    montecarlo_95_drawdown: float | None = None
    created_at: datetime = field(default_factory=now_utc)


@dataclass
class ExecutiveDecision:
    decision: str
    reasoning: str
    action_items: list[str]
    hires: list[str] = field(default_factory=list)
    fires: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=now_utc)
