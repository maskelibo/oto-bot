from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from oto_bot.core.models import ExperimentResult


# ---------------------------------------------------------------------------
# Default config search path
# ---------------------------------------------------------------------------
_DEFAULT_RISK_YAML = Path(__file__).resolve().parents[3] / "configs" / "risk.yaml"


# ---------------------------------------------------------------------------
# Risk policy
# ---------------------------------------------------------------------------

@dataclass
class RiskPolicy:
    # --- Core backtest promotion gates ---
    max_drawdown_limit: float = -0.15
    min_sharpe: float = 1.0
    min_profit_factor: float = 1.1
    single_strategy_daily_loss_cap: float = 0.02
    portfolio_daily_loss_cap: float = 0.04
    # Extended checks
    min_sortino: float = 0.8
    min_total_trades: int = 30
    max_correlation: float = 0.70

    # --- Atlas Trading immutable hard risk rules ---
    max_single_trade_risk_pct: float = 0.02       # Never >2% per trade
    max_daily_loss_pct: float = 0.05              # 5% daily loss = circuit breaker
    max_weekly_loss_pct: float = 0.10             # 10% weekly
    max_monthly_drawdown_pct: float = 0.15        # 15% monthly
    max_correlated_risk_pct: float = 0.08         # Correlated positions max 8%
    circuit_breaker_pct: float = 0.05             # 5% loss triggers breaker
    max_single_position_pct: float = 0.20         # No single position >20% capital
    max_total_positions: int = 8
    max_same_direction: int = 4
    max_leverage: float = 5.0
    consec_loss_halve: int = 3                    # Halve size after N consecutive losses
    consec_loss_stop: int = 5                     # Stop trading after N consecutive losses
    funding_rate: float = 0.0001                  # 0.01% per 8h (crypto perpetuals)
    funding_interval_hours: int = 8

    @classmethod
    def from_yaml(cls, path: str | Path | None = None) -> RiskPolicy:
        """Load policy from a YAML config file.

        Keys in the YAML that match dataclass fields are applied; unknown
        keys are silently ignored so the config file can carry extra data.
        """
        yaml_path = Path(path) if path else _DEFAULT_RISK_YAML
        if not yaml_path.exists():
            return cls()

        with open(yaml_path, "r", encoding="utf-8") as fh:
            raw: dict[str, Any] = yaml.safe_load(fh) or {}

        known_fields = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in raw.items() if k in known_fields}
        return cls(**filtered)


# ---------------------------------------------------------------------------
# Approval report
# ---------------------------------------------------------------------------

@dataclass
class ApprovalReport:
    approved: bool
    checks: dict[str, bool] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "checks": self.checks,
            "reasons": self.reasons,
            "summary": self.summary,
        }


# ---------------------------------------------------------------------------
# Risk gate
# ---------------------------------------------------------------------------

class RiskGate:
    """Evaluates experiment results against the risk policy and returns a
    detailed approval report."""

    def __init__(
        self,
        policy: RiskPolicy | None = None,
        yaml_path: str | Path | None = None,
    ) -> None:
        if policy is not None:
            self.policy = policy
        else:
            self.policy = RiskPolicy.from_yaml(yaml_path)

    # ------------------------------------------------------------------
    # Primary approval
    # ------------------------------------------------------------------

    def approve(self, result: ExperimentResult) -> ApprovalReport:
        """Run all risk checks and return a structured report."""
        p = self.policy
        checks: dict[str, bool] = {}
        reasons: list[str] = []

        # Max drawdown
        ok = result.max_drawdown >= p.max_drawdown_limit
        checks["max_drawdown"] = ok
        if not ok:
            reasons.append(
                f"Max drawdown {result.max_drawdown:.2%} exceeds limit {p.max_drawdown_limit:.2%}"
            )

        # Sharpe
        ok = result.sharpe >= p.min_sharpe
        checks["sharpe"] = ok
        if not ok:
            reasons.append(
                f"Sharpe {result.sharpe:.2f} below minimum {p.min_sharpe:.2f}"
            )

        # Profit factor
        ok = result.profit_factor >= p.min_profit_factor
        checks["profit_factor"] = ok
        if not ok:
            reasons.append(
                f"Profit factor {result.profit_factor:.2f} below minimum {p.min_profit_factor:.2f}"
            )

        # Sortino
        ok = result.sortino >= p.min_sortino
        checks["sortino"] = ok
        if not ok:
            reasons.append(
                f"Sortino {result.sortino:.2f} below minimum {p.min_sortino:.2f}"
            )

        # Minimum total trades (reject low sample size)
        ok = result.total_trades >= p.min_total_trades
        checks["total_trades"] = ok
        if not ok:
            reasons.append(
                f"Total trades {result.total_trades} below minimum {p.min_total_trades}"
            )

        approved = all(checks.values())
        summary = "APPROVED" if approved else f"REJECTED ({len(reasons)} issue(s))"

        return ApprovalReport(
            approved=approved,
            checks=checks,
            reasons=reasons,
            summary=summary,
        )

    # ------------------------------------------------------------------
    # Correlation check (portfolio-level)
    # ------------------------------------------------------------------

    def check_correlation(self, correlation: float) -> tuple[bool, str]:
        """Check whether a strategy's correlation with the existing
        portfolio exceeds the allowed limit."""
        ok = abs(correlation) <= self.policy.max_correlation
        msg = (
            "correlation acceptable"
            if ok
            else f"correlation {correlation:.2f} exceeds max {self.policy.max_correlation:.2f}"
        )
        return ok, msg

    # ------------------------------------------------------------------
    # Position size check (Atlas hard rule)
    # ------------------------------------------------------------------

    def check_position_size(
        self,
        trade_risk_pct: float,
        capital: float,
    ) -> tuple[bool, str]:
        """Ensure no single trade risks more than the allowed percentage.

        Parameters
        ----------
        trade_risk_pct:
            Fraction of capital at risk for this trade (e.g. 0.015 = 1.5%).
        capital:
            Current account capital (used for context in messages only).

        Returns
        -------
        (passed, message)
        """
        limit = self.policy.max_single_trade_risk_pct
        ok = trade_risk_pct <= limit
        if ok:
            msg = f"Position size OK: {trade_risk_pct:.2%} <= {limit:.2%} limit"
        else:
            msg = (
                f"Position size REJECTED: {trade_risk_pct:.2%} exceeds "
                f"{limit:.2%} max single-trade risk (capital={capital:,.2f})"
            )
        return ok, msg

    # ------------------------------------------------------------------
    # Portfolio exposure check (Atlas hard rule)
    # ------------------------------------------------------------------

    def check_portfolio_exposure(
        self,
        positions: list[dict[str, Any]],
        capital: float,
    ) -> tuple[bool, list[str]]:
        """Validate portfolio-level exposure constraints.

        Each position dict is expected to have at minimum:
            - ``size_pct``: position size as fraction of capital
            - ``direction``: ``"long"`` or ``"short"``
            - ``correlated_group``: optional string grouping correlated assets

        Returns
        -------
        (all_passed, list_of_issues)
        """
        p = self.policy
        issues: list[str] = []

        # 1. Total position count
        if len(positions) > p.max_total_positions:
            issues.append(
                f"Too many positions: {len(positions)} > {p.max_total_positions}"
            )

        # 2. Single position size cap
        for pos in positions:
            size_pct = pos.get("size_pct", 0.0)
            if size_pct > p.max_single_position_pct:
                symbol = pos.get("symbol", "?")
                issues.append(
                    f"Position {symbol} is {size_pct:.2%} of capital, "
                    f"exceeds {p.max_single_position_pct:.2%} limit"
                )

        # 3. Same-direction count
        long_count = sum(1 for pos in positions if pos.get("direction") == "long")
        short_count = sum(1 for pos in positions if pos.get("direction") == "short")
        if long_count > p.max_same_direction:
            issues.append(
                f"Too many long positions: {long_count} > {p.max_same_direction}"
            )
        if short_count > p.max_same_direction:
            issues.append(
                f"Too many short positions: {short_count} > {p.max_same_direction}"
            )

        # 4. Correlated group exposure
        group_totals: dict[str, float] = {}
        for pos in positions:
            group = pos.get("correlated_group")
            if group:
                group_totals[group] = group_totals.get(group, 0.0) + pos.get("size_pct", 0.0)
        for group, total in group_totals.items():
            if total > p.max_correlated_risk_pct:
                issues.append(
                    f"Correlated group '{group}' exposure {total:.2%} "
                    f"exceeds {p.max_correlated_risk_pct:.2%} limit"
                )

        return (len(issues) == 0, issues)

    # ------------------------------------------------------------------
    # Leverage check (Atlas hard rule)
    # ------------------------------------------------------------------

    def check_leverage(
        self,
        leverage: float,
        max_allowed: float | None = None,
    ) -> tuple[bool, str]:
        """Ensure leverage is within allowed bounds.

        Parameters
        ----------
        leverage:
            Effective leverage of the trade / portfolio.
        max_allowed:
            Override for the policy default if provided.
        """
        limit = max_allowed if max_allowed is not None else self.policy.max_leverage
        ok = leverage <= limit
        if ok:
            msg = f"Leverage OK: {leverage:.1f}x <= {limit:.1f}x limit"
        else:
            msg = f"Leverage REJECTED: {leverage:.1f}x exceeds {limit:.1f}x limit"
        return ok, msg

    # ------------------------------------------------------------------
    # Multi-timeframe loss limits (Atlas hard rule)
    # ------------------------------------------------------------------

    def check_daily_weekly_monthly_limits(
        self,
        daily_pnl: float,
        weekly_pnl: float,
        monthly_dd: float,
    ) -> tuple[bool, list[str]]:
        """Check daily / weekly / monthly loss thresholds.

        All values should be expressed as fractions of capital
        (e.g. -0.03 means a 3% loss).

        Returns
        -------
        (all_passed, list_of_breaches)
        """
        p = self.policy
        breaches: list[str] = []

        if daily_pnl < -abs(p.max_daily_loss_pct):
            breaches.append(
                f"CIRCUIT BREAKER: daily loss {daily_pnl:.2%} "
                f"exceeds -{p.max_daily_loss_pct:.2%} limit"
            )

        if weekly_pnl < -abs(p.max_weekly_loss_pct):
            breaches.append(
                f"Weekly loss {weekly_pnl:.2%} exceeds "
                f"-{p.max_weekly_loss_pct:.2%} limit"
            )

        if monthly_dd < -abs(p.max_monthly_drawdown_pct):
            breaches.append(
                f"Monthly drawdown {monthly_dd:.2%} exceeds "
                f"-{p.max_monthly_drawdown_pct:.2%} limit"
            )

        return (len(breaches) == 0, breaches)

    # ------------------------------------------------------------------
    # Kill switch
    # ------------------------------------------------------------------

    def kill_switch_check(
        self,
        current_drawdown: float,
        daily_pnl: float,
    ) -> bool:
        """Return ``True`` if the kill switch should fire (i.e. halt
        trading immediately).

        Triggers:
        - Current drawdown exceeds the max drawdown limit.
        - Daily P&L loss exceeds the portfolio daily loss cap.
        - Daily P&L loss exceeds the circuit breaker threshold.
        """
        if current_drawdown < self.policy.max_drawdown_limit:
            return True
        if daily_pnl < -abs(self.policy.portfolio_daily_loss_cap):
            return True
        if daily_pnl < -abs(self.policy.circuit_breaker_pct):
            return True
        return False
