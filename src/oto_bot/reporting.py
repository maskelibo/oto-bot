"""Executive reporting module — generates daily briefs and dashboards."""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.text import Text


console = Console()


class ExecutiveReporter:
    """Generates executive-level reports from experiment data."""

    def __init__(self, memory_manager=None, ledger=None):
        self.memory = memory_manager
        self.ledger = ledger

    def daily_brief(self, cycle_history: list) -> str:
        """Generate a daily executive brief."""
        if not cycle_history:
            return "No experiments run today."

        today = datetime.now(timezone.utc).date()
        today_results = [
            cr for cr in cycle_history
            if hasattr(cr, 'result') and cr.result.created_at.date() == today
        ]

        if not today_results:
            today_results = cycle_history  # Use all if no today filter match

        total = len(today_results)
        promoted = sum(1 for cr in today_results if cr.result.promoted)
        avg_score = sum(cr.score for cr in today_results) / total if total else 0
        best = max(today_results, key=lambda x: x.score) if today_results else None
        worst = min(today_results, key=lambda x: x.score) if today_results else None

        lines = [
            "╔═══════════════════════════════════════════════╗",
            "║        OTO-BOT DAILY EXECUTIVE BRIEF         ║",
            f"║  Date: {today.isoformat():<39} ║",
            "╠═══════════════════════════════════════════════╣",
            f"║  Total Experiments: {total:<26} ║",
            f"║  Promoted: {promoted:<35} ║",
            f"║  Avg Composite Score: {avg_score:<24.4f} ║",
            "╠═══════════════════════════════════════════════╣",
        ]

        if best:
            lines.extend([
                "║  BEST PERFORMER:                              ║",
                f"║  {best.hypothesis.title[:44]:<45} ║",
                f"║  Score: {best.score:<38.4f} ║",
                f"║  Sharpe: {best.result.sharpe:<37.2f} ║",
                f"║  ROI: {best.result.roi:<39.1%} ║",
                "╠═══════════════════════════════════════════════╣",
            ])

        if worst:
            lines.extend([
                "║  WORST PERFORMER:                             ║",
                f"║  {worst.hypothesis.title[:44]:<45} ║",
                f"║  Score: {worst.score:<38.4f} ║",
                f"║  Reason: {worst.risk_reason[:36]:<37} ║",
                "╠═══════════════════════════════════════════════╣",
            ])

        # Strategy family stats
        strat_stats: dict[str, list[float]] = {}
        for cr in today_results:
            fam = cr.hypothesis.strategy_family
            strat_stats.setdefault(fam, []).append(cr.score)

        lines.append("║  BY STRATEGY FAMILY:                          ║")
        for fam, scores in sorted(strat_stats.items()):
            avg = sum(scores) / len(scores)
            lines.append(f"║    {fam:<12} → {len(scores):>3} tests | avg {avg:<11.3f}    ║")

        # Market stats
        market_stats: dict[str, list[float]] = {}
        for cr in today_results:
            m = cr.hypothesis.market
            market_stats.setdefault(m, []).append(cr.score)

        lines.append("║  BY MARKET:                                   ║")
        for m, scores in sorted(market_stats.items()):
            avg = sum(scores) / len(scores)
            lines.append(f"║    {m:<14} → {len(scores):>3} tests | avg {avg:<9.3f}    ║")

        lines.append("╚═══════════════════════════════════════════════╝")

        return "\n".join(lines)

    def strategy_comparison_table(self, cycle_history: list) -> None:
        """Print a rich comparison table of all tested strategies."""
        if not cycle_history:
            console.print("[dim]No data to compare.[/dim]")
            return

        table = Table(
            title="Strategy Comparison Dashboard",
            show_header=True,
            header_style="bold magenta",
        )
        table.add_column("#", style="dim", width=4)
        table.add_column("Strategy", style="cyan", max_width=35)
        table.add_column("Market", style="green")
        table.add_column("Score", justify="right", style="bold")
        table.add_column("Sharpe", justify="right")
        table.add_column("Sortino", justify="right")
        table.add_column("ROI", justify="right")
        table.add_column("WR", justify="right")
        table.add_column("PF", justify="right")
        table.add_column("Max DD", justify="right", style="red")
        table.add_column("Status", justify="center")

        sorted_history = sorted(cycle_history, key=lambda x: x.score, reverse=True)

        for i, cr in enumerate(sorted_history[:20], 1):
            r = cr.result
            sortino = getattr(r, "sortino", 0.0) or 0.0
            status = "[green]✓[/green]" if r.promoted else "[yellow]⟳[/yellow]"

            table.add_row(
                str(i),
                cr.hypothesis.title[:35],
                cr.hypothesis.market,
                f"{cr.score:.3f}",
                f"{r.sharpe:.2f}",
                f"{sortino:.2f}",
                f"{r.roi:.1%}",
                f"{r.win_rate:.1%}",
                f"{r.profit_factor:.2f}",
                f"{r.max_drawdown:.1%}",
                status,
            )

        console.print(table)

    def save_brief_to_file(self, brief: str, path: str = "artifacts/daily_brief.txt") -> None:
        """Save daily brief to a file."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filepath = p.parent / f"brief_{timestamp}.txt"
        filepath.write_text(brief, encoding="utf-8")
        console.print(f"[dim]Brief saved to {filepath}[/dim]")

    def promotion_candidates(self, cycle_history: list) -> list:
        """Return list of strategies that passed all gates."""
        return [
            cr for cr in cycle_history
            if cr.result.promoted
        ]

    def improvement_analysis(self, cycle_history: list) -> dict[str, Any]:
        """Analyze if strategies are improving over time."""
        analysis: dict[str, Any] = {}

        # Group by strategy family
        by_family: dict[str, list] = {}
        for cr in cycle_history:
            fam = cr.hypothesis.strategy_family
            by_family.setdefault(fam, []).append(cr)

        for fam, results in by_family.items():
            if len(results) < 2:
                analysis[fam] = {"trend": "insufficient_data", "count": len(results)}
                continue

            scores = [cr.score for cr in results]
            first_half = scores[:len(scores)//2]
            second_half = scores[len(scores)//2:]

            avg_first = sum(first_half) / len(first_half)
            avg_second = sum(second_half) / len(second_half)

            trend = "improving" if avg_second > avg_first else "declining"
            delta = avg_second - avg_first

            analysis[fam] = {
                "trend": trend,
                "count": len(results),
                "avg_first_half": round(avg_first, 4),
                "avg_second_half": round(avg_second, 4),
                "delta": round(delta, 4),
                "best_score": round(max(scores), 4),
            }

        return analysis
