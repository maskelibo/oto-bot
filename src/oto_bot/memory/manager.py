from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any

from oto_bot.core.models import ExecutiveDecision, ExperimentResult, Hypothesis
from oto_bot.memory.store import SQLiteStore


class MemoryManager:
    """Manages all memory operations via SQLite store."""

    def __init__(self, store: SQLiteStore | None = None) -> None:
        self.store = store or SQLiteStore()

    # ------------------------------------------------------------------
    # Write helpers
    # ------------------------------------------------------------------

    def save_hypothesis(self, hypothesis: Hypothesis) -> str:
        data = asdict(hypothesis)
        data["created_at"] = hypothesis.created_at.isoformat()
        return self.store.insert_hypothesis(data, agent_id=hypothesis.author_agent_id)

    def save_result(self, result: ExperimentResult) -> str:
        data = asdict(result)
        data["created_at"] = result.created_at.isoformat()
        category = "promotion" if result.promoted else "episodic"
        rid = self.store.insert_experiment(data, category=category)
        if not result.promoted:
            self.store.insert_experiment(
                {
                    "hypothesis_title": result.hypothesis_title,
                    "strategy_family": result.strategy_family,
                    "market": result.market,
                    "notes": result.notes,
                    "reason": "not_promoted",
                    "created_at": result.created_at.isoformat(),
                },
                category="failure",
            )
        return rid

    def save_decision(self, decision: ExecutiveDecision) -> str:
        data = asdict(decision)
        data["created_at"] = decision.created_at.isoformat()
        return self.store.insert_decision(data)

    def save_debate(self, debate_data: dict[str, Any]) -> str:
        return self.store.insert_debate(debate_data)

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    def get_recent_results(self, n: int = 10) -> list[dict[str, Any]]:
        """Return the *n* most recent experiment results (all categories)."""
        return self.store.get_latest("experiments", n=n)

    def get_best_results(self, metric: str = "sharpe", n: int = 5) -> list[dict[str, Any]]:
        """Return top *n* results sorted by *metric* descending."""
        all_rows = self.store.query("experiments", limit=500)
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in all_rows:
            val = row["data"].get(metric)
            if val is not None:
                try:
                    scored.append((float(val), row))
                except (TypeError, ValueError):
                    continue
        scored.sort(key=lambda x: x[0], reverse=True)
        return [row for _, row in scored[:n]]

    def get_failure_patterns(self) -> dict[str, Any]:
        """Aggregate common failure reasons from failure memory."""
        failures = self.store.query("experiments", category="failure", limit=500)
        reasons: list[str] = []
        strategies: list[str] = []
        markets: list[str] = []
        for f in failures:
            d = f["data"]
            reasons.append(d.get("reason", "unknown"))
            strategies.append(d.get("strategy_family", "unknown"))
            markets.append(d.get("market", "unknown"))
        return {
            "total_failures": len(failures),
            "reason_counts": dict(Counter(reasons)),
            "strategy_counts": dict(Counter(strategies)),
            "market_counts": dict(Counter(markets)),
        }

    def get_improvement_trend(self, strategy_name: str, n: int = 10) -> dict[str, Any]:
        """Check if *strategy_name* is improving over recent runs."""
        rows = self.store.query(
            "experiments",
            filters={"strategy_family": strategy_name},
            limit=n * 5,  # fetch extra to compensate filter
            order="ASC",
        )
        # Only keep the latest n matches
        rows = rows[-n:] if len(rows) > n else rows
        if len(rows) < 2:
            return {"strategy": strategy_name, "trend": "insufficient_data", "points": len(rows)}

        sharpes = [r["data"].get("sharpe", 0.0) for r in rows]
        first_half = sharpes[: len(sharpes) // 2]
        second_half = sharpes[len(sharpes) // 2 :]
        avg_first = sum(first_half) / max(len(first_half), 1)
        avg_second = sum(second_half) / max(len(second_half), 1)
        delta = avg_second - avg_first

        if delta > 0.1:
            trend = "improving"
        elif delta < -0.1:
            trend = "declining"
        else:
            trend = "stable"

        return {
            "strategy": strategy_name,
            "trend": trend,
            "sharpe_delta": round(delta, 4),
            "avg_first_half": round(avg_first, 4),
            "avg_second_half": round(avg_second, 4),
            "points": len(rows),
        }

    def compress_old_memories(self, older_than_days: int = 30) -> dict[str, int]:
        """Summarize records older than *older_than_days* into compact entries."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
        compressed_counts: dict[str, int] = {}

        for table in ("experiments", "decisions", "hypotheses"):
            rows = self.store._conn.execute(
                f"SELECT id, data, category FROM {table} WHERE timestamp < ?",
                (cutoff,),
            ).fetchall()
            if not rows:
                compressed_counts[table] = 0
                continue

            # Group by category, summarize, replace
            groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
            ids_to_delete: list[str] = []
            for r in rows:
                groups[r["category"]].append(json.loads(r["data"]) if isinstance(r["data"], str) else r["data"])
                ids_to_delete.append(r["id"])

            for category, items in groups.items():
                summary = {
                    "type": "compressed_summary",
                    "original_count": len(items),
                    "compressed_at": datetime.now(timezone.utc).isoformat(),
                    "category": category,
                    "sample_keys": list(items[0].keys()) if items else [],
                }
                # Capture aggregate stats for experiment data
                if table == "experiments":
                    sharpes = [it.get("sharpe", 0) for it in items if "sharpe" in it]
                    if sharpes:
                        summary["avg_sharpe"] = round(sum(sharpes) / len(sharpes), 4)
                        summary["max_sharpe"] = round(max(sharpes), 4)
                    strategies = [it.get("strategy_family", "unknown") for it in items]
                    summary["strategies_seen"] = list(set(strategies))

                self.store.insert(table, category, summary)

            # Remove originals
            placeholders = ",".join("?" for _ in ids_to_delete)
            self.store._conn.execute(
                f"DELETE FROM {table} WHERE id IN ({placeholders})", ids_to_delete
            )
            self.store._conn.commit()
            compressed_counts[table] = len(ids_to_delete)

        return compressed_counts
