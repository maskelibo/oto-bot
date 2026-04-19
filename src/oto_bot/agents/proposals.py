"""ProposalQueue — ajanların CEO / insan onayı için sunduğu öneriler.

Proposal tipleri:
    - integration        : dış kaynaktan (GitHub repo vb.) bir özelliği sisteme al
    - new_agent          : yeni bir ajan role'u ekle
    - new_strategy       : yeni bir strateji ailesi
    - new_feature        : yeni bir modül / dashboard widget
    - param_override     : mevcut bir parametreyi değiştir (risk limit, eşik, vs)
    - curriculum         : dış eğitim materyali (borsanınizinden, babypips vs)

Her öneri:
    - başlık
    - gerekçe (özet)
    - detay (markdown)
    - tahmini etki (benefit)
    - tahmini risk
    - önerilen uygulama adımları
    - author_agent
    - status: pending / approved / rejected / implemented
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS proposals (
    id                TEXT PRIMARY KEY,
    timestamp         TEXT NOT NULL,
    decided_at        TEXT,
    proposal_type     TEXT NOT NULL,
    title             TEXT NOT NULL,
    author_agent      TEXT NOT NULL,
    summary           TEXT NOT NULL,
    detail_markdown   TEXT,
    estimated_benefit TEXT,
    estimated_risk    TEXT,
    action_steps      TEXT,
    source_url        TEXT,
    metadata          TEXT,
    status            TEXT DEFAULT 'pending',
    decided_by        TEXT,
    decision_reason   TEXT
);
CREATE INDEX IF NOT EXISTS idx_prop_status ON proposals(status);
CREATE INDEX IF NOT EXISTS idx_prop_type   ON proposals(proposal_type);
CREATE INDEX IF NOT EXISTS idx_prop_ts     ON proposals(timestamp);
"""


@dataclass
class Proposal:
    proposal_id: str
    proposal_type: str
    title: str
    author_agent: str
    summary: str
    detail_markdown: str = ""
    estimated_benefit: str = ""
    estimated_risk: str = ""
    action_steps: list[str] = field(default_factory=list)
    source_url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_reason: str | None = None


class ProposalQueue:
    def __init__(self, db_path: str | Path = "artifacts/experiments.sqlite3") -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_CREATE_SQL)
        self._conn.commit()

    # ------------------------------------------------------------------

    def submit(self, proposal: Proposal) -> str:
        if not proposal.proposal_id:
            proposal.proposal_id = str(uuid.uuid4())
        self._conn.execute(
            """
            INSERT OR REPLACE INTO proposals
              (id, timestamp, decided_at, proposal_type, title, author_agent,
               summary, detail_markdown, estimated_benefit, estimated_risk,
               action_steps, source_url, metadata, status, decided_by, decision_reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                proposal.proposal_id,
                proposal.created_at.isoformat() if isinstance(proposal.created_at, datetime) else proposal.created_at,
                proposal.decided_at.isoformat() if isinstance(proposal.decided_at, datetime) else proposal.decided_at,
                proposal.proposal_type,
                proposal.title,
                proposal.author_agent,
                proposal.summary,
                proposal.detail_markdown,
                proposal.estimated_benefit,
                proposal.estimated_risk,
                json.dumps(proposal.action_steps, ensure_ascii=False),
                proposal.source_url,
                json.dumps(proposal.metadata, ensure_ascii=False),
                proposal.status,
                proposal.decided_by,
                proposal.decision_reason,
            ),
        )
        self._conn.commit()
        return proposal.proposal_id

    # ------------------------------------------------------------------

    def pending(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM proposals WHERE status = 'pending' ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def all(self, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if status:
            rows = self._conn.execute(
                "SELECT * FROM proposals WHERE status = ? ORDER BY timestamp DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM proposals ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get(self, proposal_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) c FROM proposals GROUP BY status"
        ).fetchall()
        out = {r["status"]: r["c"] for r in rows}
        for s in ("pending", "approved", "rejected", "implemented"):
            out.setdefault(s, 0)
        return out

    # ------------------------------------------------------------------

    def decide(self, proposal_id: str, decision: str, reason: str = "", by: str = "human") -> bool:
        if decision not in ("approved", "rejected", "implemented"):
            raise ValueError(f"invalid decision: {decision}")
        now = datetime.now(timezone.utc).isoformat()
        cur = self._conn.execute(
            "UPDATE proposals SET status = ?, decided_at = ?, decided_by = ?, decision_reason = ? WHERE id = ?",
            (decision, now, by, reason, proposal_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        try:
            d["action_steps"] = json.loads(d.get("action_steps") or "[]")
        except Exception:
            d["action_steps"] = []
        try:
            d["metadata"] = json.loads(d.get("metadata") or "{}")
        except Exception:
            d["metadata"] = {}
        return d
