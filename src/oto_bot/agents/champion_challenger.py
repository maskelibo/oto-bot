"""ChampionChallenger — Faz 5 promote-sonrasi rotasyon motoru.

Doktrin:
    Bir kez promote olmus pod ("champion") sonsuza kadar yasamaz. Her
    (strategy_family, market) cifti icin bir aktif champion vardir.
    Promotion gate'inden gecemeyen ama yine de yuksek-Sharpe sergileyen
    candidate'lar "challenger" listesine alinir; gercek pod kurulmaz, sadece
    shadow paper trading rotasyonunda performansi takip edilir.

    Belirli bir gozlem penceresinden (default 10 cycle) sonra eger
    challenger'in ortalama Sharpe'i champion'unkini onceden tanimlanan bir
    yuzde (default %15) ile asarsa rotasyon onerilir; orchestrator champion'u
    arsivler ve challenger'i full pod'a terfi eder.

Persistans:
    artifacts/champion_challenger.json — full state snapshot, atomic write.

Notlar:
    - Type hints + Turkce yorumlar zorunludur (proje stili).
    - Deque maxlen=30: token-tasarrufu ve memory korumasi.
    - Strategy_id: champion icin pod_id; challenger icin
      "{family}_{market}_chal_{n}" formatinda otomatik uretilir.
    - "Archived" challenger / champion silinmez, sadece active=False ile
      isaretlenir; tarihsel rotasyon analizleri icin saklanir.
"""

from __future__ import annotations

import json
import os
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque

# ---------------------------------------------------------------------------
# Veri tipleri
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class PerformanceObservation:
    """Tek bir cycle icin gozlem (Sharpe / DD / ROI)."""

    cycle: int
    sharpe: float
    dd: float
    roi: float
    ts: str = field(default_factory=_now_iso)


@dataclass
class StrategyEntry:
    """Champion veya challenger icin kayit."""

    strategy_id: str
    role: str  # "champion" | "challenger"
    strategy_family: str
    market: str
    params: dict[str, Any]
    initial_sharpe: float
    active: bool = True
    archived_reason: str | None = None
    pod_id: str | None = None  # champion ise pod_allocator pod_id'si
    created_at: str = field(default_factory=_now_iso)
    archived_at: str | None = None
    # Performans gecmisi (max 30 cycle)
    history: Deque[PerformanceObservation] = field(
        default_factory=lambda: deque(maxlen=30)
    )

    def append_observation(self, obs: PerformanceObservation) -> None:
        self.history.append(obs)

    def avg_sharpe(self, window: int) -> float | None:
        """Son `window` cycle icin Sharpe ortalamasi (gozlem azsa None)."""
        if not self.history:
            return None
        recent = list(self.history)[-window:]
        if not recent:
            return None
        return sum(o.sharpe for o in recent) / len(recent)


# ---------------------------------------------------------------------------
# Ana sinif
# ---------------------------------------------------------------------------


class ChampionChallenger:
    """Promote pod'lar icin shadow paper trading rotasyonu."""

    HISTORY_MAX: int = 30

    def __init__(
        self,
        path: str | Path = "artifacts/champion_challenger.json",
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # strategy_id -> StrategyEntry
        self._entries: dict[str, StrategyEntry] = {}
        # (family, market) -> sayac (challenger id'si icin)
        self._chal_counter: dict[tuple[str, str], int] = {}
        self.load()

    # ------------------------------------------------------------------
    # Persistans
    # ------------------------------------------------------------------

    def load(self) -> None:
        if not self.path.exists():
            self._entries = {}
            self._chal_counter = {}
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self._entries = {}
            self._chal_counter = {}
            return

        self._entries = {}
        for sid, payload in (raw.get("entries") or {}).items():
            hist_raw = payload.get("history") or []
            hist: Deque[PerformanceObservation] = deque(maxlen=self.HISTORY_MAX)
            for h in hist_raw:
                try:
                    hist.append(
                        PerformanceObservation(
                            cycle=int(h["cycle"]),
                            sharpe=float(h["sharpe"]),
                            dd=float(h["dd"]),
                            roi=float(h["roi"]),
                            ts=str(h.get("ts") or _now_iso()),
                        )
                    )
                except Exception:
                    continue
            entry = StrategyEntry(
                strategy_id=sid,
                role=str(payload.get("role") or "champion"),
                strategy_family=str(payload.get("strategy_family") or ""),
                market=str(payload.get("market") or ""),
                params=dict(payload.get("params") or {}),
                initial_sharpe=float(payload.get("initial_sharpe") or 0.0),
                active=bool(payload.get("active", True)),
                archived_reason=payload.get("archived_reason"),
                pod_id=payload.get("pod_id"),
                created_at=str(payload.get("created_at") or _now_iso()),
                archived_at=payload.get("archived_at"),
                history=hist,
            )
            self._entries[sid] = entry

        self._chal_counter = {}
        for k, v in (raw.get("chal_counter") or {}).items():
            try:
                fam, mkt = k.split("||", 1)
                self._chal_counter[(fam, mkt)] = int(v)
            except Exception:
                continue

    def save(self) -> None:
        payload: dict[str, Any] = {
            "entries": {},
            "chal_counter": {
                f"{fam}||{mkt}": n for (fam, mkt), n in self._chal_counter.items()
            },
            "updated_at": _now_iso(),
        }
        for sid, entry in self._entries.items():
            row = asdict(entry)
            # deque'u list'e cevir
            row["history"] = [asdict(o) for o in entry.history]
            payload["entries"][sid] = row

        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------
    # Public API — kayit
    # ------------------------------------------------------------------

    def register_champion(
        self,
        pod_id: str,
        strategy_family: str,
        market: str,
        params: dict[str, Any],
        sharpe: float,
    ) -> StrategyEntry:
        """Promote olmus pod'u champion olarak kaydet.

        Ayni pod_id ile cagrilirsa mevcut kaydi gunceller (idempotent).
        """
        existing = self._entries.get(pod_id)
        if existing is not None:
            existing.role = "champion"
            existing.strategy_family = strategy_family
            existing.market = market
            existing.params = dict(params)
            existing.initial_sharpe = float(sharpe)
            existing.active = True
            existing.pod_id = pod_id
            self.save()
            return existing

        entry = StrategyEntry(
            strategy_id=pod_id,
            role="champion",
            strategy_family=strategy_family,
            market=market,
            params=dict(params),
            initial_sharpe=float(sharpe),
            pod_id=pod_id,
        )
        self._entries[pod_id] = entry
        self.save()
        return entry

    def propose_challenger(
        self,
        family: str,
        market: str,
        params: dict[str, Any],
        sharpe: float,
    ) -> StrategyEntry:
        """Promotion gate'inden gecemeyen ama Sharpe yuksek candidate.

        Otomatik id: "{family}_{market}_chal_{n}".
        """
        key = (family, market)
        n = self._chal_counter.get(key, 0)
        sid = f"{family}_{market}_chal_{n}"
        # Carpisma korumasi (load sonrasi sayac dusuk kaldiysa)
        while sid in self._entries:
            n += 1
            sid = f"{family}_{market}_chal_{n}"
        self._chal_counter[key] = n + 1

        entry = StrategyEntry(
            strategy_id=sid,
            role="challenger",
            strategy_family=family,
            market=market,
            params=dict(params),
            initial_sharpe=float(sharpe),
        )
        self._entries[sid] = entry
        self.save()
        return entry

    def record_outcome(
        self,
        strategy_id: str,
        sharpe: float,
        dd: float,
        roi: float,
        cycle_number: int,
    ) -> bool:
        """Champion / challenger'a yeni cycle gozlemini ekle.

        Bilinmeyen strategy_id sessizce False doner (orchestrator'in akisini
        bozmaz; Faz 5 sonrasi entegrasyon hatalari icin yumusak fail).
        """
        entry = self._entries.get(strategy_id)
        if entry is None:
            return False
        if not entry.active:
            return False
        entry.append_observation(
            PerformanceObservation(
                cycle=int(cycle_number),
                sharpe=float(sharpe),
                dd=float(dd),
                roi=float(roi),
            )
        )
        self.save()
        return True

    def retire_underperformer(self, strategy_id: str, reason: str) -> bool:
        """Pod_allocator'da kill mekanizmasi yoksa internal arsiv isareti.

        Entry silinmez (tarihsel iz icin); active=False ve archived_reason
        doldurulur.
        """
        entry = self._entries.get(strategy_id)
        if entry is None:
            return False
        entry.active = False
        entry.archived_reason = reason
        entry.archived_at = _now_iso()
        self.save()
        return True

    # ------------------------------------------------------------------
    # Public API — okuma / analiz
    # ------------------------------------------------------------------

    def get(self, strategy_id: str) -> StrategyEntry | None:
        return self._entries.get(strategy_id)

    def all(self) -> list[StrategyEntry]:
        return list(self._entries.values())

    def active_champions(self) -> list[StrategyEntry]:
        return [
            e for e in self._entries.values()
            if e.active and e.role == "champion"
        ]

    def active_challengers(self) -> list[StrategyEntry]:
        return [
            e for e in self._entries.values()
            if e.active and e.role == "challenger"
        ]

    def by_family_market(
        self, family: str, market: str
    ) -> tuple[StrategyEntry | None, list[StrategyEntry]]:
        """(champion, [challengers]) doner — yalnizca aktif olanlar."""
        champ: StrategyEntry | None = None
        chals: list[StrategyEntry] = []
        for e in self._entries.values():
            if not e.active:
                continue
            if e.strategy_family != family or e.market != market:
                continue
            if e.role == "champion":
                # Birden fazla aktif champion teorik olarak olmamali; ilkini al.
                if champ is None:
                    champ = e
            elif e.role == "challenger":
                chals.append(e)
        return champ, chals

    # ------------------------------------------------------------------
    # Public API — rotasyon karari
    # ------------------------------------------------------------------

    def evaluate_rotation(
        self,
        min_window: int = 10,
        outperformance_pct: float = 0.15,
    ) -> list[dict[str, Any]]:
        """Her (family, market) icin champion vs challenger karsilastirmasi.

        Karar uretme kurali:
            - Hem champion hem challenger >=`min_window` gozleme sahip olmali.
            - Challenger'in son `min_window` Sharpe ortalamasi, champion'unkini
              en az `outperformance_pct` ile asmali (yani avg_chal >=
              avg_champ * (1 + pct)).
            - Champion'un avg Sharpe'i pozitif olmalidir; sifir/negatifse
              "rotate" yine onerilir (zaten dusen champion).

        Donus: rotate adaylarinin listesi:
            {family, market, champion_id, challenger_id,
             champion_avg_sharpe, challenger_avg_sharpe, uplift_pct,
             window, decision: "rotate"}
        """
        decisions: list[dict[str, Any]] = []
        # Family/market gruplari
        groups: dict[tuple[str, str], list[StrategyEntry]] = {}
        for e in self._entries.values():
            if not e.active:
                continue
            groups.setdefault((e.strategy_family, e.market), []).append(e)

        for (fam, mkt), entries in groups.items():
            champ = next((e for e in entries if e.role == "champion"), None)
            challengers = [e for e in entries if e.role == "challenger"]
            if champ is None or not challengers:
                continue

            champ_avg = champ.avg_sharpe(min_window)
            if champ_avg is None or len(champ.history) < min_window:
                continue

            best_chal: StrategyEntry | None = None
            best_avg: float | None = None
            for chal in challengers:
                chal_avg = chal.avg_sharpe(min_window)
                if chal_avg is None or len(chal.history) < min_window:
                    continue
                if best_avg is None or chal_avg > best_avg:
                    best_avg = chal_avg
                    best_chal = chal

            if best_chal is None or best_avg is None:
                continue

            # Karsilastirma esigi
            if champ_avg > 0:
                threshold = champ_avg * (1.0 + outperformance_pct)
            else:
                # Champion zaten negatifse, herhangi bir pozitif challenger rotate edilebilir.
                threshold = max(0.0, champ_avg * (1.0 + outperformance_pct))

            if best_avg < threshold:
                continue

            uplift = (
                (best_avg - champ_avg) / abs(champ_avg)
                if champ_avg != 0 else float("inf")
            )
            decisions.append({
                "family": fam,
                "market": mkt,
                "champion_id": champ.strategy_id,
                "challenger_id": best_chal.strategy_id,
                "champion_avg_sharpe": round(float(champ_avg), 4),
                "challenger_avg_sharpe": round(float(best_avg), 4),
                "uplift_pct": (round(float(uplift), 4)
                               if uplift != float("inf") else None),
                "window": min_window,
                "decision": "rotate",
            })

        return decisions
