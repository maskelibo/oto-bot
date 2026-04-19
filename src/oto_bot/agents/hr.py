"""HR — otomatik hiring / firing politikası.

Gerçek bir şirket gibi: bir departmanın iş yükü kalıcı olarak kapasiteyi
aşıyorsa CEO specialist işe alır. Kimsenin 200 cycle'da bir çıktı üretmediği
ajan ise retire edilir.

Kurallar (deterministik, LLM kullanmaz):

**HIRE**
1. Aynı (strategy_family, market) çifti 50+ cycle'da >%70 başarısızlık oranı
   üretiyorsa → o scope için **Strategy R&D specialist** işe al.
   Örn: "Nova-Scalp-Forex" — sadece scalp/forex hipotezleri üretir.

2. Belirli bir rejim (crisis / high_vol) son 100 cycle'da %20'den fazla
   görüldüyse → **Regime specialist** al.
   Örn: "Oracle-HighVol" — yüksek volatilite için özel rejim ajanı.

3. Belirli bir strateji ailesinde 200 cycle'da ≤ 3 lesson çıktıysa →
   **Insight miner** al. Örn: "Sigma-Deep-Swing" — swing için derin analiz.

4. Herhangi bir departman 30 cycle'da 10+ backlog iş biriktirdiyse →
   jeneralist specialist al (genel kapasite artışı).

**FIRE / RETIRE**
1. Ajan 200 cycle'da 0 debate oyu + 0 lesson + 0 hypothesis çıkarmadıysa →
   retire.

2. İki ajan aynı scope'ta aynı departmanda çalışıyor + daha yaşlı olan
   son 100 cycle'da daha az etkinse → older'ı retire.

Not: hiring_log tablosu her kararı kalıcı kaydeder; dashboard bu olayları
gösterir.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from oto_bot.agents.registry import AgentRegistry
from oto_bot.core.models import AgentProfile
from oto_bot.memory.journal import LearningJournal


_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS hiring_log (
    id              TEXT PRIMARY KEY,
    timestamp       TEXT NOT NULL,
    action          TEXT NOT NULL,
    agent_name      TEXT NOT NULL,
    role            TEXT,
    department      TEXT,
    mandate         TEXT,
    reason          TEXT,
    cycle_number    INTEGER DEFAULT 0,
    metadata        TEXT
);
CREATE INDEX IF NOT EXISTS idx_hiring_ts  ON hiring_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_hiring_act ON hiring_log(action);
"""


@dataclass
class HiringProposal:
    action: str
    agent_name: str
    role: str
    department: str
    mandate: str
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)


class HRManager:
    """CEO'nun eli; personel kararlarını verir."""

    def __init__(
        self,
        registry: AgentRegistry,
        journal: LearningJournal | None = None,
        db_path: str | Path = "artifacts/experiments.sqlite3",
    ) -> None:
        self.registry = registry
        self.journal = journal or LearningJournal(db_path)
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_CREATE_SQL)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Analiz: son N cycle'dan iş yükü çıkar
    # ------------------------------------------------------------------

    def analyze_workload(self, window: int = 200) -> dict[str, Any]:
        """Son N experiment'tan heuristik iş-yükü çıkarımı."""
        rows = self._conn.execute(
            "SELECT data FROM experiments ORDER BY timestamp DESC LIMIT ?",
            (window,),
        ).fetchall()
        failure_by_scope: dict[tuple[str, str], int] = defaultdict(int)
        total_by_scope: dict[tuple[str, str], int] = defaultdict(int)
        regime_counts: Counter = Counter()
        strategy_family_lessons: dict[str, int] = defaultdict(int)

        parsed: list[dict[str, Any]] = []
        for r in rows:
            try:
                d = json.loads(r["data"])
                parsed.append(d)
                fam = d.get("strategy_family", "unknown")
                mkt = d.get("market", "unknown")
                promoted = d.get("promoted", False)
                regime = d.get("regime", "unknown")
                total_by_scope[(fam, mkt)] += 1
                if not promoted:
                    failure_by_scope[(fam, mkt)] += 1
                regime_counts[regime] += 1
            except Exception:
                continue

        # Insights per strategy family
        all_lessons = self.journal.query(limit=1000)
        for lesson in all_lessons:
            strategy_family_lessons[lesson.strategy_family or "unknown"] += 1

        return {
            "window": window,
            "total_experiments": len(parsed),
            "failure_by_scope": {f"{k[0]}|{k[1]}": v for k, v in failure_by_scope.items()},
            "total_by_scope": {f"{k[0]}|{k[1]}": v for k, v in total_by_scope.items()},
            "regime_counts": dict(regime_counts),
            "lessons_per_strategy": dict(strategy_family_lessons),
        }

    # ------------------------------------------------------------------
    # Öneri: kimleri alalım, kimleri retire edelim
    # ------------------------------------------------------------------

    def propose(self, workload: dict[str, Any], cycle_number: int = 0) -> list[HiringProposal]:
        proposals: list[HiringProposal] = []

        total_exp = workload.get("total_experiments", 0)
        failure_scope = workload.get("failure_by_scope", {}) or {}
        total_scope = workload.get("total_by_scope", {}) or {}

        # 1. Scope-based Nova specialist
        for scope, fails in failure_scope.items():
            total = total_scope.get(scope, 0)
            if total < 50:
                continue
            fail_rate = fails / max(total, 1)
            if fail_rate < 0.70:
                continue
            fam, mkt = scope.split("|", 1)
            specialist = f"Nova-{fam}-{mkt}"
            if self.registry.find_by_name(specialist):
                continue
            proposals.append(HiringProposal(
                action="hire",
                agent_name=specialist,
                role="Strategy R&D Specialist",
                department="Research",
                mandate=(
                    f"Scope: {fam} / {mkt}. Failure rate {fail_rate:.0%} over {total} cycles. "
                    f"Focus: izole edilmiş hipotezler, specialized param searches."
                ),
                reason=f"HIRE_SCOPE_OVERLOAD: {scope} failure rate {fail_rate:.0%}",
                metadata={"scope": scope, "failure_rate": round(fail_rate, 3), "sample": total},
            ))

        # 2. Regime specialist (crisis / high_vol)
        regime_counts = workload.get("regime_counts", {}) or {}
        total_reg = sum(regime_counts.values()) or 1
        for regime, ct in regime_counts.items():
            if regime not in ("crisis", "high_vol"):
                continue
            if ct / total_reg < 0.20:
                continue
            specialist = f"Oracle-{regime}"
            if self.registry.find_by_name(specialist):
                continue
            proposals.append(HiringProposal(
                action="hire",
                agent_name=specialist,
                role="Regime Specialist",
                department="Research",
                mandate=(
                    f"{regime} rejimi için özel rejim ajanı. "
                    f"{regime} {ct/total_reg:.0%} oranında görülüyor — genel Oracle yetersiz."
                ),
                reason=f"HIRE_REGIME_PREVALENCE: {regime} at {ct/total_reg:.0%}",
                metadata={"regime": regime, "share": round(ct/total_reg, 3)},
            ))

        # 3. Insight miner — strateji ailesinde ders azsa
        lessons_per_strat = workload.get("lessons_per_strategy", {}) or {}
        for fam in ("day", "swing", "scalp"):
            count = lessons_per_strat.get(fam, 0)
            family_total = sum(v for k, v in total_scope.items() if k.startswith(fam + "|"))
            if family_total < 200:
                continue
            if count > 3:
                continue
            specialist = f"Sigma-Deep-{fam}"
            if self.registry.find_by_name(specialist):
                continue
            proposals.append(HiringProposal(
                action="hire",
                agent_name=specialist,
                role="Insight Miner",
                department="Research",
                mandate=(
                    f"{fam} ailesinde derin post-mortem. {family_total} cycle'da sadece {count} "
                    f"ders çıktı — insight production dar."
                ),
                reason=f"HIRE_LOW_INSIGHT: {fam} only {count} lessons in {family_total} cycles",
                metadata={"strategy_family": fam, "lesson_count": count, "family_volume": family_total},
            ))

        # 4. Retire önerileri — aktivitesi olmayan ajanlar
        # Debate katılımı yok + hipotez yok + ders yok
        active_agents = self.registry.active()
        for agent in active_agents:
            # Executive + ana departman ajanlarını (default kadroyu) retire etme
            if agent.name in (
                "Atlas CEO", "Iris ChiefOfStaff", "Vega MarketIntel", "Nova StrategyRND",
                "Sigma Quant", "Mercury Macro", "Regime Oracle", "Helix Backtest",
                "Shockwave StressLab", "Sentinel Risk", "Apex PortfolioRisk",
                "Cassandra PreMortem", "Forge Execution", "Tariq TCA",
                "Ledger Allocator", "Archive Memory", "Pulse Analytics",
                "Ledger Attribution",
            ):
                continue
            # Sadece specialist-lerde retire önerisi
            lesson_count = sum(
                1 for l in self.journal.query(author_agent=agent.name, limit=500)
            )
            if lesson_count == 0:
                age_cycles = cycle_number - int(agent.metadata.get("hired_at_cycle", 0) or 0)
                if age_cycles >= 200:
                    proposals.append(HiringProposal(
                        action="retire",
                        agent_name=agent.name,
                        role=agent.role,
                        department=agent.department,
                        mandate=agent.mandate,
                        reason=f"RETIRE_IDLE: 0 lessons + 0 hypotheses over {age_cycles} cycles",
                        metadata={"age_cycles": age_cycles, "lesson_count": lesson_count},
                    ))

        return proposals

    # ------------------------------------------------------------------
    # Uygula
    # ------------------------------------------------------------------

    def apply(self, proposals: list[HiringProposal], cycle_number: int = 0) -> list[dict[str, Any]]:
        applied: list[dict[str, Any]] = []
        for p in proposals:
            if p.action == "hire":
                profile = AgentProfile(
                    name=p.agent_name,
                    role=p.role,
                    department=p.department,
                    mandate=p.mandate,
                    metadata={"hired_at_cycle": cycle_number, "hired_by": "Atlas CEO", **p.metadata},
                )
                self.registry.add(profile)
                self._log(p, cycle_number)
                applied.append({"action": "hire", "agent": p.agent_name})
            elif p.action == "retire":
                agent = self.registry.find_by_name(p.agent_name)
                if agent:
                    self.registry.retire(agent.agent_id)
                    self._log(p, cycle_number)
                    applied.append({"action": "retire", "agent": p.agent_name})
        return applied

    # ------------------------------------------------------------------
    # Log
    # ------------------------------------------------------------------

    def _log(self, p: HiringProposal, cycle_number: int) -> None:
        self._conn.execute(
            """
            INSERT INTO hiring_log
              (id, timestamp, action, agent_name, role, department, mandate, reason, cycle_number, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                datetime.now(timezone.utc).isoformat(),
                p.action, p.agent_name, p.role, p.department,
                p.mandate, p.reason, cycle_number,
                json.dumps(p.metadata, ensure_ascii=False),
            ),
        )
        self._conn.commit()

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM hiring_log ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["metadata"] = json.loads(d["metadata"]) if d["metadata"] else {}
            except Exception:
                d["metadata"] = {}
            out.append(d)
        return out

    def summary(self) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT action, COUNT(*) as c FROM hiring_log GROUP BY action"
        ).fetchall()
        counts = {r["action"]: r["c"] for r in row}
        return {
            "total_hires": counts.get("hire", 0),
            "total_retires": counts.get("retire", 0),
            "active_specialists": sum(
                1 for a in self.registry.active()
                if "-" in a.name and a.name.split("-")[0] in ("Nova", "Oracle", "Sigma")
            ),
        }

    # ------------------------------------------------------------------
    # Convenience run
    # ------------------------------------------------------------------

    def review(self, cycle_number: int = 0) -> dict[str, Any]:
        """Full HR review: analyze → propose → apply. Returns applied changes."""
        wl = self.analyze_workload()
        proposals = self.propose(wl, cycle_number=cycle_number)
        applied = self.apply(proposals, cycle_number=cycle_number)
        return {
            "workload": wl,
            "proposals_count": len(proposals),
            "applied": applied,
        }
