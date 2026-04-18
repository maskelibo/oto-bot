from __future__ import annotations

from oto_bot.core.models import ExperimentResult


def composite_score(result: ExperimentResult) -> float:
    """Weighted composite score using all available metrics.

    Weight allocation (sums to 1.0):
        Sharpe           0.20
        Sortino          0.15
        Profit factor    0.15
        Stability        0.15
        Calmar           0.10
        Win rate         0.10
        Drawdown safety  0.10
        Walk-forward     0.05
    """
    # Clamp individual components to reasonable ranges to avoid a single
    # extreme metric from dominating the score.
    sharpe_c = min(max(result.sharpe, -2.0), 5.0)
    sortino_c = min(max(result.sortino, -2.0), 8.0)
    pf_c = min(max(result.profit_factor, 0.0), 5.0)
    stability_c = min(max(result.stability_score, 0.0), 1.0)
    calmar_c = min(max(result.calmar, -2.0), 10.0)
    win_rate_c = min(max(result.win_rate, 0.0), 1.0)
    dd_safety = max(0.0, 1.0 + result.max_drawdown)  # 0..1 range

    # Walk-forward bonus: only applied when available
    wf_bonus = 0.0
    if result.walkforward_sharpe is not None:
        wf_bonus = min(max(result.walkforward_sharpe, 0.0), 5.0)

    score = (
        sharpe_c * 0.20
        + sortino_c * 0.15
        + pf_c * 0.15
        + stability_c * 0.15
        + calmar_c * 0.10
        + win_rate_c * 0.10
        + dd_safety * 0.10
        + wf_bonus * 0.05
    )
    return float(score)


def rank_experiments(results: list[ExperimentResult]) -> list[ExperimentResult]:
    """Sort experiments by composite score, highest first."""
    return sorted(results, key=composite_score, reverse=True)
