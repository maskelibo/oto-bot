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
    sortino: float = 0.0
    cagr: float = 0.0
    calmar: float = 0.0
    total_trades: int = 0
    avg_trade_duration: float = 0.0
    regime: str = "unknown"
    walkforward_sharpe: float | None = None
    montecarlo_95_drawdown: float | None = None
    # --- Trade-level aggregates (en yüksek kazanç, en yüklü kayıp vs.) ---
    largest_win_pct: float = 0.0
    largest_loss_pct: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    duration_bars: int = 0
    bar_timeframe: str = "1h"
    gross_profit_pct: float = 0.0
    gross_loss_pct: float = 0.0
    # --- Top trades + symbol + exit reasons (for detail modal) ---
    symbol: str = ""
    top_trades_by_pnl: list[dict[str, Any]] = field(default_factory=list)
    top_trades_by_leverage: list[dict[str, Any]] = field(default_factory=list)
    exit_reason_counts: dict[str, int] = field(default_factory=dict)
    avg_leverage: float = 1.0
    max_leverage_used: float = 1.0
    long_trades: int = 0
    short_trades: int = 0
    strategy_params: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=now_utc)


@dataclass
class ExecutiveDecision:
    decision: str
    reasoning: str
    action_items: list[str]
    hires: list[str] = field(default_factory=list)
    fires: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=now_utc)


# ---------------------------------------------------------------------------
# Institutional extensions — pod structure, regime, attribution, stress, macro
# ---------------------------------------------------------------------------


@dataclass
class PodState:
    """Per-strategy allocation sleeve. Modelled after Millennium/Citadel pods.

    Lifecycle:
        - allocated_capital: notional dollars assigned to this strategy
        - current_capital:   allocated + realized PnL
        - drawdown_pct:      running peak-to-trough
        - status:            active | halved | retired
    """

    pod_id: str
    strategy_family: str
    market: str
    allocated_capital: float
    current_capital: float
    peak_capital: float
    drawdown_pct: float = 0.0
    sharpe_30d: float = 0.0
    sortino_30d: float = 0.0
    trades_30d: int = 0
    win_rate_30d: float = 0.0
    correlation_to_book: float = 0.0
    status: str = "active"
    halved_at: datetime | None = None
    retired_at: datetime | None = None
    last_updated: datetime = field(default_factory=now_utc)

    @property
    def return_pct(self) -> float:
        if self.allocated_capital <= 0:
            return 0.0
        return (self.current_capital - self.allocated_capital) / self.allocated_capital


@dataclass
class RegimeState:
    """Current market regime snapshot.

    Regimes: trend_up | trend_down | range | high_vol | crisis | unknown
    """

    market: str
    regime: str
    confidence: float
    trend_score: float
    volatility_score: float
    range_score: float
    indicators: dict[str, float] = field(default_factory=dict)
    prior_regime: str = "unknown"
    regime_age_bars: int = 0
    created_at: datetime = field(default_factory=now_utc)


@dataclass
class PnLAttribution:
    """Decomposes a PnL amount into its driving components."""

    experiment_id: str
    total_pnl: float
    by_signal: dict[str, float] = field(default_factory=dict)
    by_symbol: dict[str, float] = field(default_factory=dict)
    by_regime: dict[str, float] = field(default_factory=dict)
    by_hour: dict[str, float] = field(default_factory=dict)
    alpha_pnl: float = 0.0
    beta_pnl: float = 0.0
    fees_paid: float = 0.0
    slippage_paid: float = 0.0
    funding_paid: float = 0.0
    net_edge_after_costs: float = 0.0
    created_at: datetime = field(default_factory=now_utc)


@dataclass
class StressScenario:
    """A named, reproducible stress test."""

    scenario_id: str
    name: str
    description: str
    price_shock_pct: float
    volatility_multiplier: float
    correlation_jump: float
    liquidity_haircut: float
    duration_bars: int
    historical_reference: str | None = None
    created_at: datetime = field(default_factory=now_utc)


@dataclass
class StressResult:
    scenario_id: str
    strategy_family: str
    market: str
    survived: bool
    max_drawdown_under_stress: float
    pnl_under_stress: float
    kill_switch_fired: bool
    notes: str = ""
    created_at: datetime = field(default_factory=now_utc)


@dataclass
class MarketContext:
    """Macro overlay snapshot: risk-on/off, dominance, cross-asset signals."""

    market: str
    regime_label: str
    risk_on_score: float
    btc_dominance: float | None = None
    dxy_proxy: float | None = None
    cross_correlation: float | None = None
    fear_greed_proxy: float | None = None
    headline_events: list[str] = field(default_factory=list)
    bias: str = "neutral"
    confidence: float = 0.5
    created_at: datetime = field(default_factory=now_utc)


@dataclass
class PortfolioSnapshot:
    """Book-level view across all pods at a moment in time."""

    total_capital: float
    deployed_capital: float
    num_active_pods: int
    num_positions: int
    gross_exposure: float
    net_exposure: float
    max_pod_concentration: float
    correlation_max: float
    var_95: float
    expected_shortfall_95: float
    daily_pnl: float
    weekly_pnl: float
    month_to_date_pnl: float
    created_at: datetime = field(default_factory=now_utc)


@dataclass
class MorningBriefing:
    """Pre-market executive summary produced by CEO each cycle."""

    date: str
    regime_summary: str
    macro_summary: str
    portfolio_snapshot: PortfolioSnapshot | None
    top_performers: list[dict[str, Any]] = field(default_factory=list)
    bottom_performers: list[dict[str, Any]] = field(default_factory=list)
    active_alerts: list[str] = field(default_factory=list)
    capital_moves: list[str] = field(default_factory=list)
    day_priorities: list[str] = field(default_factory=list)
    committee_topics: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=now_utc)


@dataclass
class ExecutionReport:
    """Per-order TCA record."""

    order_id: str
    symbol: str
    side: str
    intended_price: float
    filled_price: float
    intended_qty: float
    filled_qty: float
    arrival_slippage_bps: float
    implementation_shortfall_bps: float
    market_impact_bps: float
    fees_bps: float
    latency_ms: float
    venue: str = "paper"
    created_at: datetime = field(default_factory=now_utc)


@dataclass
class CommitteeVerdict:
    """Output of a committee session (investment / risk)."""

    committee: str
    topic: str
    verdict: str
    rationale: str
    dissents: list[str] = field(default_factory=list)
    followups: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=now_utc)


# ---------------------------------------------------------------------------
# Memory architecture — compact, retrievable, token-efficient
# ---------------------------------------------------------------------------


@dataclass
class Lesson:
    """Tek satırlık öğrenilen ders. Ajanlar raw log yerine bunu okur.

    Tag örnekleri:
        ["market:crypto", "strategy:scalp", "regime:range", "symbol:BTCUSDT",
         "outcome:win", "severity:high"]

    Content: 1-3 cümle, doğal dilde bir genelleme. Örn:
        "Range rejiminde BTCUSDT 5m scalper v23, WR 0.62 ama PF 1.05 — fee sonrası edge ölüyor."
    """

    lesson_id: str
    author_agent: str
    content: str
    tags: list[str]
    market: str = "*"
    strategy_family: str = "*"
    regime: str = "*"
    symbol: str = "*"
    severity: str = "info"
    evidence_experiment_id: str | None = None
    source_cycle: int = 0
    times_referenced: int = 0
    created_at: datetime = field(default_factory=now_utc)


@dataclass
class LearningCurvePoint:
    """Tek ölçüm noktası — zaman içindeki öğrenme trendi için."""

    bucket: str
    agent: str
    scope: str
    sample_size: int
    avg_sharpe: float
    avg_pf: float
    promotion_rate: float
    insight_count: int
    created_at: datetime = field(default_factory=now_utc)
