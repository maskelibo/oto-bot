from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from oto_bot.memory.store import SQLiteStore


class ExperimentLedger:
    """Experiment ledger backed by SQLite.

    Maintains backward compatibility: if an old JSONL path is supplied,
    legacy reads still work via a thin adapter.
    """

    def __init__(
        self,
        store: SQLiteStore | None = None,
        legacy_path: str = "experiments/ledger.jsonl",
    ) -> None:
        self.store = store or SQLiteStore()
        self._legacy_path = Path(legacy_path)

        # Migrate old JSONL if present
        self._maybe_migrate_legacy()

    def _maybe_migrate_legacy(self) -> None:
        if not self._legacy_path.exists() or self._legacy_path.stat().st_size == 0:
            return
        existing = self.store.count("experiments")
        if existing > 0:
            return  # already has data, skip
        with self._legacy_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                payload = json.loads(line)
                self.store.insert_experiment(
                    payload,
                    category=payload.get("category", "episodic"),
                )

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def log(self, payload: dict[str, Any], category: str = "episodic") -> str:
        return self.store.insert_experiment(payload, category=category)

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    def get_by_strategy(self, strategy_family: str, limit: int = 50) -> list[dict[str, Any]]:
        return self.store.query(
            "experiments",
            filters={"strategy_family": strategy_family},
            limit=limit,
        )

    def get_by_market(self, market: str, limit: int = 50) -> list[dict[str, Any]]:
        return self.store.query(
            "experiments",
            filters={"market": market},
            limit=limit,
        )

    def get_by_date_range(
        self,
        start: str | datetime,
        end: str | datetime | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        if isinstance(start, datetime):
            start = start.isoformat()
        if end is None:
            end = datetime.now(timezone.utc).isoformat()
        elif isinstance(end, datetime):
            end = end.isoformat()

        rows = self.store._conn.execute(
            "SELECT * FROM experiments WHERE timestamp BETWEEN ? AND ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (start, end, limit),
        ).fetchall()
        results: list[dict[str, Any]] = []
        for r in rows:
            entry = dict(r)
            entry["data"] = json.loads(entry["data"])
            results.append(entry)
        return results

    def get_all(self, limit: int = 200) -> list[dict[str, Any]]:
        return self.store.get_latest("experiments", n=limit)
