"""AutoApprover — pending proposal'ları settings'e göre otomatik karara bağlar.

Kurallar (varsayılan):
    - settings.auto_approve True ise çalışır
    - Proposal delay_minutes'tan eski olmalı (insan müdahale şansı)
    - Proposal tipi allowed_types içinde olmalı
    - estimated_risk içinde 'critical' geçiyorsa ve auto_reject_critical True ise → reject
    - Aksi halde → approve

Bu otonom karar insan müdahalesinin YEDEĞİ. İnsan her zaman öncelikli; bu
modül insan yokken fabrikayı durdurmasın diye var.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any

from oto_bot.agents.proposals import ProposalQueue
from oto_bot.agents.settings import load_settings

logger = logging.getLogger("oto_bot.auto_approver")


class AutoApprover:
    def __init__(self, queue: ProposalQueue | None = None) -> None:
        self.queue = queue or ProposalQueue()

    def run(self) -> dict[str, Any]:
        """Pending proposal'ları tara, eligible olanları karara bağla."""
        settings = load_settings()
        if not settings.auto_approve and not settings.auto_reject_critical:
            return {"processed": 0, "approved": 0, "rejected": 0, "reason": "disabled"}

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=settings.auto_approve_delay_minutes)
        cutoff_iso = cutoff.isoformat()

        pending = self.queue.pending(limit=500)
        approved = 0
        rejected = 0
        processed = 0

        for p in pending:
            ts = p.get("timestamp", "")
            if ts >= cutoff_iso:
                continue  # delay henüz dolmadı

            processed += 1
            risk = (p.get("estimated_risk") or "").lower()
            ptype = p.get("proposal_type")

            # Critical reddetme
            if settings.auto_reject_critical and "critical" in risk:
                self.queue.decide(
                    p["id"], "rejected",
                    reason="auto-reject: critical risk flag",
                    by="AutoApprover",
                )
                rejected += 1
                continue

            # Auto-approve
            if settings.auto_approve:
                allowed = settings.auto_approve_allowed_types or []
                if allowed and ptype not in allowed:
                    continue
                self.queue.decide(
                    p["id"], "approved",
                    reason=f"auto-approve: passed delay {settings.auto_approve_delay_minutes}min, type={ptype}",
                    by="AutoApprover",
                )
                approved += 1

        return {
            "processed": processed,
            "approved": approved,
            "rejected": rejected,
            "pending_remaining": len(pending) - approved - rejected,
        }
