"""LearningCurve — zaman içinde öğrenme trendi.

Her hafta her ajan/strateji/market için:
    - sample_size (o bucket'taki experiment sayısı)
    - avg_sharpe
    - avg_pf
    - promotion_rate (promote edilen / toplam)
    - insight_count (o bucket'ta çıkarılan ders)

SQLite tablosu: `learning_curve` — bucket bazlı aggregate.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from oto_bot.core.models import LearningCurvePoint


_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS learning_curve (
    id                TEXT PRIMARY KEY,
    timestamp         TEXT NOT NULL,
    bucket            TEXT NOT NULL,
    agent             TEXT NOT NULL,
    scope             TEXT NOT NULL,
    sample_size       INTEGER NOT NULL,
    avg_sharpe        REAL NOT NULL,
    avg_pf            REAL NOT NULL,
    promotion_rate    REAL NOT NULL,
    insight_count     INTEGER NOT NULL,
    UNIQUE(bucket, agent, scope)
);
CREATE INDEX IF NOT EXISTS idx_curve_bucket ON learning_curve(bucket);
CREATE INDEX IF NOT EXISTS idx_curve_agent  ON learning_curve(agent);
CREATE INDEX IF NOT EXISTS idx_curve_scope  ON learning_curve(scope);
"""


class LearningCurve:
    """Zaman bucket'ları bazında öğrenme metriği."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = str(db_path) if db_path else "artifacts/experiments.sqlite3"
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_CREATE_SQL)
        self._conn.commit()

    # ------------------------------------------------------------------

    @staticmethod
    def bucket_for(ts: datetime) -> str:
        """ISO hafta bucket'ı: 2026-W15 gibi."""
        year, week, _ = ts.isocalendar()
        return f"{year}-W{week:02d}"

    # ------------------------------------------------------------------

    def upsert(self, point: LearningCurvePoint) -> None:
        self._conn.execute(
            """
            INSERT INTO learning_curve (
                id, timestamp, bucket, agent, scope,
                sample_size, avg_sharpe, avg_pf, promotion_rate, insight_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bucket, agent, scope) DO UPDATE SET
                sample_size   = excluded.sample_size,
                avg_sharpe    = excluded.avg_sharpe,
                avg_pf        = excluded.avg_pf,
                promotion_rate= excluded.promotion_rate,
                insight_count = excluded.insight_count,
                timestamp     = excluded.timestamp
            """,
            (
                str(uuid.uuid4()),
                point.created_at.isoformat() if isinstance(point.created_at, datetime) else point.created_at,
                point.bucket,
                point.agent,
                point.scope,
                point.sample_size,
                point.avg_sharpe,
                point.avg_pf,
                point.promotion_rate,
                point.insight_count,
            ),
        )
        self._conn.commit()

    # ------------------------------------------------------------------

    def rebuild_from_experiments(self, main_db_path: str | Path | None = None) -> int:
        """Mevcut experiment tablosundan haftalık bucket özet üret.

        Global scope ("all") + strateji bazında ("strategy:day") + market
        bazında ("market:crypto") üç seviye curve inşa eder.
        """
        db = str(main_db_path) if main_db_path else self.db_path
        src = sqlite3.connect(db, check_same_thread=False)
        src.row_factory = sqlite3.Row

        rows = src.execute(
            "SELECT timestamp, data FROM experiments WHERE category != 'failure'"
        ).fetchall()
        if not rows:
            return 0

        import json
        buckets: dict[tuple[str, str, str], dict[str, Any]] = {}

        for r in rows:
            try:
                data = json.loads(r["data"]) if isinstance(r["data"], str) else r["data"]
            except Exception:
                continue
            ts_raw = r["timestamp"]
            try:
                ts = datetime.fromisoformat(ts_raw)
            except Exception:
                continue
            bucket = self.bucket_for(ts)

            sharpe = data.get("sharpe") or 0
            pf = data.get("profit_factor") or 0
            promoted = 1 if data.get("promoted") else 0
            strategy = data.get("strategy_family") or "unknown"
            market = data.get("market") or "unknown"

            for scope in ["all", f"strategy:{strategy}", f"market:{market}"]:
                key = (bucket, "orchestrator", scope)
                e = buckets.setdefault(key, {"n": 0, "sharpe_sum": 0.0, "pf_sum": 0.0, "promoted": 0})
                e["n"] += 1
                e["sharpe_sum"] += float(sharpe)
                e["pf_sum"] += float(pf)
                e["promoted"] += promoted

        # Insight count per bucket
        insight_per_bucket: dict[str, int] = {}
        try:
            ins_rows = src.execute("SELECT timestamp FROM lessons").fetchall()
            for ir in ins_rows:
                try:
                    t = datetime.fromisoformat(ir["timestamp"])
                    bkt = self.bucket_for(t)
                    insight_per_bucket[bkt] = insight_per_bucket.get(bkt, 0) + 1
                except Exception:
                    pass
        except Exception:
            pass

        count = 0
        for (bucket, agent, scope), e in buckets.items():
            n = max(e["n"], 1)
            point = LearningCurvePoint(
                bucket=bucket,
                agent=agent,
                scope=scope,
                sample_size=e["n"],
                avg_sharpe=round(e["sharpe_sum"] / n, 4),
                avg_pf=round(e["pf_sum"] / n, 4),
                promotion_rate=round(e["promoted"] / n, 4),
                insight_count=insight_per_bucket.get(bucket, 0),
            )
            self.upsert(point)
            count += 1
        src.close()
        return count

    # ------------------------------------------------------------------

    def get_points(self, scope: str = "all", agent: str = "orchestrator", limit: int = 52) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM learning_curve WHERE scope = ? AND agent = ? ORDER BY bucket ASC LIMIT ?",
            (scope, agent, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def scopes(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT scope FROM learning_curve ORDER BY scope"
        ).fetchall()
        return [r["scope"] for r in rows]
