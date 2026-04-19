"""LearningJournal — kompakt ders defteri.

Her ajan kendi öğrenme notlarını burada tutar. Raw deney verisi **ayrı**
tabloda kalır; journal yalnızca distile edilmiş **insight**'ları saklar.

SQLite tablosu: `lessons`.
    id, timestamp, author_agent, content, tags (JSON), market, strategy_family,
    regime, symbol, severity, evidence_experiment_id, source_cycle,
    times_referenced.

Neden ayrı tablo?
    Retriever'ın tarama maliyeti düşük olsun; full-text search'i olmadan
    bile tag + kolon-filter ile hızlı top-K dönsün. Ayrıca compress/archive
    operasyonu raw experiments'tan bağımsız kalsın.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from oto_bot.core.models import Lesson


_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS lessons (
    id                      TEXT PRIMARY KEY,
    timestamp               TEXT NOT NULL,
    author_agent            TEXT NOT NULL,
    content                 TEXT NOT NULL,
    tags                    TEXT NOT NULL,
    market                  TEXT DEFAULT '*',
    strategy_family         TEXT DEFAULT '*',
    regime                  TEXT DEFAULT '*',
    symbol                  TEXT DEFAULT '*',
    severity                TEXT DEFAULT 'info',
    evidence_experiment_id  TEXT,
    source_cycle            INTEGER DEFAULT 0,
    times_referenced        INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_lessons_agent   ON lessons(author_agent);
CREATE INDEX IF NOT EXISTS idx_lessons_market  ON lessons(market);
CREATE INDEX IF NOT EXISTS idx_lessons_strat   ON lessons(strategy_family);
CREATE INDEX IF NOT EXISTS idx_lessons_regime  ON lessons(regime);
CREATE INDEX IF NOT EXISTS idx_lessons_time    ON lessons(timestamp);
"""


class LearningJournal:
    """Ders kaydeden / sorgulayan ince bir API."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = str(db_path) if db_path else "artifacts/experiments.sqlite3"
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_CREATE_SQL)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save(self, lesson: Lesson) -> str:
        """Bir dersi yaz. Dedupe: aynı (content, market, strategy_family, regime, symbol)
        kayıt varsa yeni satır eklemek yerine mevcut satırın times_referenced'ı +1,
        source_cycle MAX olarak güncellenir. Token ekonomisi + retriever sinyal kalitesi.
        Trend izleme `source_cycle`'ın güncellenmesi ile sağlanır.
        """
        if not lesson.lesson_id:
            lesson.lesson_id = str(uuid.uuid4())
        ts = lesson.created_at.isoformat() if isinstance(lesson.created_at, datetime) else lesson.created_at

        # Dedupe lookup — aynı semantik anahtar var mı?
        existing = self._conn.execute(
            """
            SELECT id, source_cycle FROM lessons
            WHERE content = ? AND market = ? AND strategy_family = ?
              AND regime = ? AND symbol = ?
            LIMIT 1
            """,
            (
                lesson.content,
                lesson.market,
                lesson.strategy_family,
                lesson.regime,
                lesson.symbol,
            ),
        ).fetchone()

        if existing is not None:
            new_cycle = max(int(existing["source_cycle"] or 0), int(lesson.source_cycle or 0))
            self._conn.execute(
                """
                UPDATE lessons
                SET times_referenced = times_referenced + 1,
                    source_cycle = ?,
                    timestamp = ?
                WHERE id = ?
                """,
                (new_cycle, ts, existing["id"]),
            )
            self._conn.commit()
            return existing["id"]

        self._conn.execute(
            """
            INSERT OR REPLACE INTO lessons (
                id, timestamp, author_agent, content, tags,
                market, strategy_family, regime, symbol,
                severity, evidence_experiment_id, source_cycle, times_referenced
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lesson.lesson_id,
                ts,
                lesson.author_agent,
                lesson.content,
                json.dumps(lesson.tags, ensure_ascii=False),
                lesson.market,
                lesson.strategy_family,
                lesson.regime,
                lesson.symbol,
                lesson.severity,
                lesson.evidence_experiment_id,
                lesson.source_cycle,
                lesson.times_referenced,
            ),
        )
        # Dedupe için kompozit index — sonraki SELECT'leri hızlandırır.
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_lessons_dedupe "
            "ON lessons(content, market, strategy_family, regime, symbol)"
        )
        self._conn.commit()
        return lesson.lesson_id

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def query(
        self,
        *,
        author_agent: str | None = None,
        market: str | None = None,
        strategy_family: str | None = None,
        regime: str | None = None,
        symbol: str | None = None,
        severity: str | None = None,
        limit: int = 50,
    ) -> list[Lesson]:
        clauses = []
        params: list[Any] = []
        for key, val in [
            ("author_agent", author_agent),
            ("market", market),
            ("strategy_family", strategy_family),
            ("regime", regime),
            ("symbol", symbol),
            ("severity", severity),
        ]:
            if val is not None:
                clauses.append(f"({key} = ? OR {key} = '*')")
                params.append(val)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM lessons {where} ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_lesson(r) for r in rows]

    def increment_reference(self, lesson_id: str) -> None:
        self._conn.execute(
            "UPDATE lessons SET times_referenced = times_referenced + 1 WHERE id = ?",
            (lesson_id,),
        )
        self._conn.commit()

    def count(self, author_agent: str | None = None) -> int:
        if author_agent:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM lessons WHERE author_agent = ?",
                (author_agent,),
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) FROM lessons").fetchone()
        return row[0]

    def recent(self, n: int = 20) -> list[Lesson]:
        rows = self._conn.execute(
            "SELECT * FROM lessons ORDER BY timestamp DESC LIMIT ?",
            (n,),
        ).fetchall()
        return [self._row_to_lesson(r) for r in rows]

    def all_ids_by_agent(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT author_agent, COUNT(*) as c FROM lessons GROUP BY author_agent"
        ).fetchall()
        return {r["author_agent"]: r["c"] for r in rows}

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def prune_old(self, days: int = 90, min_references: int = 1) -> int:
        """Eski + referans edilmemiş dersleri sil.

        Sık okunan dersler kalır; hiç okunmamış eskiyenler silinir.
        Token ekonomisinin anahtar adımı.
        """
        cutoff = (datetime.now(timezone.utc).timestamp() - days * 86400)
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        cur = self._conn.execute(
            """
            DELETE FROM lessons
            WHERE timestamp < ? AND times_referenced < ?
            """,
            (cutoff_iso, min_references),
        )
        self._conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_lesson(row: sqlite3.Row) -> Lesson:
        try:
            tags = json.loads(row["tags"]) if row["tags"] else []
        except Exception:
            tags = []
        return Lesson(
            lesson_id=row["id"],
            author_agent=row["author_agent"],
            content=row["content"],
            tags=tags,
            market=row["market"] or "*",
            strategy_family=row["strategy_family"] or "*",
            regime=row["regime"] or "*",
            symbol=row["symbol"] or "*",
            severity=row["severity"] or "info",
            evidence_experiment_id=row["evidence_experiment_id"],
            source_cycle=int(row["source_cycle"] or 0),
            times_referenced=int(row["times_referenced"] or 0),
            created_at=datetime.fromisoformat(row["timestamp"]) if row["timestamp"] else datetime.now(timezone.utc),
        )
