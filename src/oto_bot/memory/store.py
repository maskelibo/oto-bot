from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import uuid

import numpy as np


class _NumpyEncoder(json.JSONEncoder):
    """Handle numpy types when serialising to JSON."""
    def default(self, obj):
        if isinstance(obj, (np.bool_,)):
            return bool(obj)
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)


# ---------------------------------------------------------------------------
# Legacy JSONL store – kept for backward compatibility & migration
# ---------------------------------------------------------------------------

class JsonlStore:
    """Original JSONL store. Retained so old code paths still work."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()

    def append(self, payload: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def read_all(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rows.append(json.loads(line))
        return rows


# ---------------------------------------------------------------------------
# SQLite memory store
# ---------------------------------------------------------------------------

_TABLES = (
    "experiments",
    "decisions",
    "hypotheses",
    "agent_logs",
    "debate_records",
)

_CATEGORY_MAP: dict[str, str] = {
    "working": "hypotheses",
    "episodic": "experiments",
    "semantic": "decisions",
    "failure": "experiments",
    "promotion": "experiments",
}

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS {table} (
    id          TEXT PRIMARY KEY,
    timestamp   TEXT NOT NULL,
    category    TEXT NOT NULL,
    data        TEXT NOT NULL,
    agent_id    TEXT
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_{table}_category   ON {table}(category);
CREATE INDEX IF NOT EXISTS idx_{table}_timestamp  ON {table}(timestamp);
CREATE INDEX IF NOT EXISTS idx_{table}_agent_id   ON {table}(agent_id);
"""


class SQLiteStore:
    """Central SQLite-backed memory store for the trading lab."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or os.environ.get(
            "EXPERIMENT_DB_PATH", "artifacts/experiments.sqlite3"
        )
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        self._maybe_migrate_jsonl()

    # ------------------------------------------------------------------
    # Schema bootstrap
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        for table in _TABLES:
            cur.executescript(
                _CREATE_TABLE_SQL.format(table=table)
                + _CREATE_INDEX_SQL.format(table=table)
            )
        self._conn.commit()

    # ------------------------------------------------------------------
    # JSONL migration (one-time, idempotent)
    # ------------------------------------------------------------------

    _JSONL_CATEGORY_FILES: dict[str, tuple[str, str]] = {
        "memories/working_memory.jsonl": ("hypotheses", "working"),
        "memories/episodic_memory.jsonl": ("experiments", "episodic"),
        "memories/semantic_memory.jsonl": ("decisions", "semantic"),
        "memories/failure_memory.jsonl": ("experiments", "failure"),
        "memories/promotion_memory.jsonl": ("experiments", "promotion"),
    }

    def _maybe_migrate_jsonl(self) -> None:
        for jsonl_path, (table, category) in self._JSONL_CATEGORY_FILES.items():
            p = Path(jsonl_path)
            if not p.exists() or p.stat().st_size == 0:
                continue
            # Check if we already migrated (simple heuristic: table has rows)
            existing = self._conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE category = ?", (category,)
            ).fetchone()[0]
            if existing > 0:
                continue
            store = JsonlStore(jsonl_path)
            rows = store.read_all()
            for row in rows:
                self.insert(
                    table=table,
                    category=category,
                    data=row,
                    agent_id=row.get("author_agent_id") or row.get("agent_id"),
                    timestamp=row.get("created_at"),
                )

    # ------------------------------------------------------------------
    # Core CRUD
    # ------------------------------------------------------------------

    def insert(
        self,
        table: str,
        category: str,
        data: dict[str, Any],
        agent_id: str | None = None,
        timestamp: str | None = None,
        record_id: str | None = None,
    ) -> str:
        if table not in _TABLES:
            raise ValueError(f"Unknown table: {table}")
        rid = record_id or str(uuid.uuid4())
        ts = timestamp or datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            f"INSERT OR IGNORE INTO {table} (id, timestamp, category, data, agent_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (rid, ts, category, json.dumps(data, ensure_ascii=False, cls=_NumpyEncoder), agent_id),
        )
        self._conn.commit()
        return rid

    def query(
        self,
        table: str,
        category: str | None = None,
        filters: dict[str, Any] | None = None,
        limit: int = 50,
        order: str = "DESC",
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if category:
            clauses.append("category = ?")
            params.append(category)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM {table} {where} ORDER BY timestamp {order} LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        results: list[dict[str, Any]] = []
        for r in rows:
            entry = dict(r)
            entry["data"] = json.loads(entry["data"])
            # Apply in-memory JSON field filters
            if filters:
                if all(entry["data"].get(k) == v for k, v in filters.items()):
                    results.append(entry)
            else:
                results.append(entry)
        return results

    def get_latest(self, table: str, category: str | None = None, n: int = 10) -> list[dict[str, Any]]:
        return self.query(table, category=category, limit=n, order="DESC")

    def get_by_agent(self, table: str, agent_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            f"SELECT * FROM {table} WHERE agent_id = ? ORDER BY timestamp DESC LIMIT ?",
            (agent_id, limit),
        ).fetchall()
        results: list[dict[str, Any]] = []
        for r in rows:
            entry = dict(r)
            entry["data"] = json.loads(entry["data"])
            results.append(entry)
        return results

    def count(self, table: str, category: str | None = None) -> int:
        if category:
            row = self._conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE category = ?", (category,)
            ).fetchone()
        else:
            row = self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return row[0]

    def summarize_recent(self, table: str, n: int = 20) -> dict[str, Any]:
        rows = self.get_latest(table, n=n)
        categories: dict[str, int] = {}
        for r in rows:
            cat = r["category"]
            categories[cat] = categories.get(cat, 0) + 1
        return {
            "total_records": self.count(table),
            "recent_n": len(rows),
            "category_distribution": categories,
            "latest_timestamp": rows[0]["timestamp"] if rows else None,
        }

    # ------------------------------------------------------------------
    # Convenience helpers used by MemoryManager
    # ------------------------------------------------------------------

    def insert_experiment(self, data: dict[str, Any], category: str = "episodic", agent_id: str | None = None) -> str:
        return self.insert("experiments", category, data, agent_id)

    def insert_decision(self, data: dict[str, Any], agent_id: str | None = None) -> str:
        return self.insert("decisions", "semantic", data, agent_id)

    def insert_hypothesis(self, data: dict[str, Any], agent_id: str | None = None) -> str:
        return self.insert("hypotheses", "working", data, agent_id)

    def insert_debate(self, data: dict[str, Any], agent_id: str | None = None) -> str:
        return self.insert("debate_records", "debate", data, agent_id)

    def insert_agent_log(self, data: dict[str, Any], agent_id: str | None = None) -> str:
        return self.insert("agent_logs", "log", data, agent_id)

    def close(self) -> None:
        self._conn.close()
