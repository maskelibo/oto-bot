"""MemoryRetriever — bağlam-bilinçli seçici ders erişimi.

Her cycle başlangıcında ajanlar TÜM hafızayı yüklemez. Yalnızca mevcut
**context**'e (market + strategy + regime + symbol) göre **en ilgili K dersi**
alır. Bu:
    - Token maliyetini dramatik düşürür
    - Ajanın kararını bu cycle'da en ilgili geçmiş bilgiyle besler
    - Aramayı SQLite filter + relevance skoruyla yapar

Relevance skoru formülü (basit, deterministik):
    score = tag_match * 0.5 + recency_decay * 0.25 + severity_boost * 0.15 +
            reference_boost * 0.10
    recency_decay = exp(-age_days / half_life_days)

Not: bu eski LLM embedding yerine daha ucuz/stable bir skorlama —
başlangıç için yeterli. İleride embed tabanlı retrieval eklenebilir.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from oto_bot.core.models import Lesson
from oto_bot.memory.journal import LearningJournal


@dataclass
class RetrievalContext:
    """Retriever'ın eşleştireceği anlık durum."""

    market: str = "*"
    strategy_family: str = "*"
    regime: str = "*"
    symbol: str = "*"
    author_agent: str | None = None  # yalnızca bu ajanın dersleri ile sınırla
    include_global: bool = True       # "*" tag'li global dersleri dahil et


class MemoryRetriever:
    """Context-aware TOP-K lesson retrieval."""

    def __init__(
        self,
        journal: LearningJournal | None = None,
        half_life_days: float = 14.0,
    ) -> None:
        self.journal = journal or LearningJournal()
        self.half_life_days = half_life_days

    # ------------------------------------------------------------------

    def retrieve(
        self,
        ctx: RetrievalContext,
        k: int = 5,
        increment_references: bool = True,
    ) -> list[tuple[Lesson, float]]:
        """En ilgili K dersi (lesson, score) olarak dön.

        İlk filtreleme SQL'de yapılır (tag eşleşmesi). Puanlama Python
        tarafında yapılır. K genelde 3-10 arası — default 5.
        """
        candidates = self.journal.query(
            author_agent=ctx.author_agent if not ctx.include_global else None,
            market=ctx.market,
            strategy_family=ctx.strategy_family,
            regime=ctx.regime,
            symbol=ctx.symbol,
            limit=100,
        )
        if not candidates:
            return []

        scored: list[tuple[Lesson, float]] = []
        now = datetime.now(timezone.utc)

        for lesson in candidates:
            if ctx.author_agent and lesson.author_agent != ctx.author_agent:
                continue

            score = 0.0

            # 1. Tag match — non-wildcard eşleşmeleri ödüllendir
            tag_hits = 0
            if ctx.market != "*" and lesson.market in (ctx.market, "*"):
                tag_hits += 1 if lesson.market == ctx.market else 0.4
            if ctx.strategy_family != "*" and lesson.strategy_family in (ctx.strategy_family, "*"):
                tag_hits += 1 if lesson.strategy_family == ctx.strategy_family else 0.4
            if ctx.regime != "*" and lesson.regime in (ctx.regime, "*"):
                tag_hits += 1 if lesson.regime == ctx.regime else 0.3
            if ctx.symbol != "*" and lesson.symbol in (ctx.symbol, "*"):
                tag_hits += 1 if lesson.symbol == ctx.symbol else 0.2
            score += (tag_hits / 4) * 0.5

            # 2. Recency decay
            age_days = (now - lesson.created_at).total_seconds() / 86400.0
            decay = math.exp(-age_days / max(self.half_life_days, 1e-6))
            score += decay * 0.25

            # 3. Severity boost
            sev_weights = {"critical": 1.0, "high": 0.8, "medium": 0.5, "info": 0.3, "low": 0.1}
            score += sev_weights.get(lesson.severity, 0.3) * 0.15

            # 4. Reference boost (sık okunanlar öncelikli)
            ref_score = min(1.0, lesson.times_referenced / 20.0)
            score += ref_score * 0.10

            scored.append((lesson, round(score, 4)))

        scored.sort(key=lambda x: x[1], reverse=True)
        top = scored[:k]

        if increment_references:
            for lesson, _ in top:
                self.journal.increment_reference(lesson.lesson_id)

        return top

    # ------------------------------------------------------------------

    def retrieve_text(self, ctx: RetrievalContext, k: int = 5) -> str:
        """İnsan/ajan için tek bir metin blok — cycle başında loglanır."""
        hits = self.retrieve(ctx, k=k, increment_references=True)
        if not hits:
            return "(ilgili ders yok — bu context'te hafıza boş.)"
        lines = []
        for lesson, score in hits:
            sev = lesson.severity.upper() if lesson.severity != "info" else ""
            prefix = f"[{sev}] " if sev else ""
            lines.append(
                f"- {prefix}{lesson.content}  "
                f"(by {lesson.author_agent}, score={score:.2f})"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------

    def summarize_memory_state(self) -> dict[str, Any]:
        """Dashboard için özet: her ajan kaç ders tutuyor."""
        per_agent = self.journal.all_ids_by_agent()
        total = sum(per_agent.values())
        return {
            "total_lessons": total,
            "per_agent": per_agent,
        }
