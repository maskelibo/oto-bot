"""BotRegistry — Faz 6: Sabit slotlu bot kişilik kayıt defteri.

Felsefe değişimi
----------------
Önceki mimari: orchestrator her cycle yeni bir
``(strategy_family, market, params)`` kombinasyonu deniyordu →
binlerce promotion satırı. Niceliğe yatırım yapıldı, niteliğe değil.

Bu modül felsefeyi tersine çevirir:

* En çok ``MAX_SLOTS`` (default 5) **bot kişiliği** vardır. Her slot
  bir ``(strategy_family, market)`` çiftine sabitlenir.
* Her cycle: bir slot seçilir → o slotun **mevcut params**'ı için
  iyileştirme önerisi denenir. Yeni metrik mevcut lifetime best'i
  geçerse params güncellenir; geçmezse ``iterations`` sayacı artar.
* Promote = yeni kayıt DEĞİL, slotun ``current_params`` ve
  ``lifetime_best_*`` alanlarının UPDATE edilmesi. Bot **aynı bot**;
  sadece kişiliği güçlenir.
* Slot retire koşulu: ``iterations >= 50 AND
  lifetime_best_sharpe < 1.0``. Slot boşalınca yeni
  ``(family, market)`` adayı için yeniden seed edilebilir.

Persistans
----------
``artifacts/bot_registry.json`` dosyasında, atomic write
(``tmp + os.replace``) ile saklanır. Yarım yazılmış JSON dashboard
veya başka okuyucu tarafından görülmez.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("oto_bot.agents.bot_registry")


# Varsayılan başlangıç slot tohumları — kullanıcının istediği 5 kişilik:
#   1. swing  / us_equities
#   2. day    / crypto
#   3. scalp  / forex
#   4. swing  / bist
#   5. day    / us_equities
#
# Marketler / stratejiler genişlerse round-robin ile rotate edilir,
# ama hiçbir koşulda MAX_SLOTS aşılmaz.
DEFAULT_SLOT_SEEDS: list[tuple[str, str]] = [
    ("swing", "us_equities"),
    ("day", "crypto"),
    ("swing", "crypto"),    # scalp/forex yerine — 15m forex için 4 yıl veri yok
    ("swing", "bist"),
    ("swing", "forex"),     # day/us_equities yerine — Yahoo 1h max 730 gün
]


# Slot başına minimum iyileşme metriği — 50 cycle iterasyon sonrası
# Sharpe < 1.0 ise slot retire olur.
RETIRE_AFTER_ITER: int = 50
RETIRE_BELOW_SHARPE: float = 1.0


@dataclass
class BotSlot:
    """Tek bir bot kişiliği. Slot, bir ``(family, market)`` çiftine bağlanır
    ve cycle'lar boyunca params iyileştirilerek geliştirilir."""

    slot_id: int
    strategy_family: str
    market: str
    current_params: dict[str, Any] = field(default_factory=dict)
    lifetime_best_params: dict[str, Any] = field(default_factory=dict)
    current_basket_sharpe: float = 0.0
    lifetime_best_sharpe: float = 0.0
    lifetime_best_oos_sharpe: float = 0.0
    iterations: int = 0
    accepted_updates: int = 0
    created_cycle: int = 0
    last_improvement_cycle: int = 0
    last_attempted_cycle: int = 0  # skip dahil her cycle güncellenir → round-robin için
    consecutive_skips: int = 0     # ardışık no_data sayacı — 10+ ise re-seed
    data_window_start: str = "2022-01-01"
    data_window_end: str | None = None  # None = bugün
    status: str = "active"  # active | retiring | empty
    notes: str = ""

    # ------------------------------------------------------------------
    # Serileştirme
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BotSlot":
        # Legacy/eksik alanlar için savunmacı default'lar.
        return cls(
            slot_id=int(data["slot_id"]),
            strategy_family=str(data["strategy_family"]),
            market=str(data["market"]),
            current_params=dict(data.get("current_params") or {}),
            lifetime_best_params=dict(data.get("lifetime_best_params") or {}),
            current_basket_sharpe=float(data.get("current_basket_sharpe") or 0.0),
            lifetime_best_sharpe=float(data.get("lifetime_best_sharpe") or 0.0),
            lifetime_best_oos_sharpe=float(data.get("lifetime_best_oos_sharpe") or 0.0),
            iterations=int(data.get("iterations") or 0),
            accepted_updates=int(data.get("accepted_updates") or 0),
            created_cycle=int(data.get("created_cycle") or 0),
            last_improvement_cycle=int(data.get("last_improvement_cycle") or 0),
            last_attempted_cycle=int(data.get("last_attempted_cycle") or 0),
            consecutive_skips=int(data.get("consecutive_skips") or 0),
            data_window_start=str(data.get("data_window_start") or "2022-01-01"),
            data_window_end=data.get("data_window_end"),
            status=str(data.get("status") or "active"),
            notes=str(data.get("notes") or ""),
        )


class BotRegistry:
    """Sabit slotlu kayıt defteri (max 5 bot kişiliği).

    Disk üzerinde tek bir JSON dosyasında tutulur (atomic write). Tüm
    public API'ler thread-safe değildir; orchestrator tek thread'den
    çağırdığı için lock minimum tutulmuştur (load/save kritik bölgesi).
    """

    MAX_SLOTS: int = 5

    def __init__(self, path: str | Path = "artifacts/bot_registry.json") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._slots: dict[int, BotSlot] = {}
        self._lock = threading.Lock()
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            self._slots = {}
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"BotRegistry: load failed ({exc}) — boş başlatılıyor")
            self._slots = {}
            return
        out: dict[int, BotSlot] = {}
        for item in raw or []:
            try:
                slot = BotSlot.from_dict(item)
                out[slot.slot_id] = slot
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"BotRegistry: bozuk satır atlandı: {exc}")
        self._slots = out

    def _save(self) -> None:
        """Atomic write — Windows'ta concurrent reader (dashboard) ile race olursa
        WinError 5 (access denied) gelebilir; 3 kez 50ms backoff ile retry yap."""
        import time as _time
        payload = [s.to_dict() for s in self._slots.values()]
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                tmp = self.path.with_suffix(self.path.suffix + ".tmp")
                tmp.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                os.replace(tmp, self.path)
                return  # success
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                _time.sleep(0.05 * (attempt + 1))
        # 3 deneme de başarısız → log uyarı (cycle yine devam etsin, yutulur)
        if last_exc is not None:
            logger.warning(f"BotRegistry: persist failed after 3 retries: {last_exc}")

    # ------------------------------------------------------------------
    # Seeding
    # ------------------------------------------------------------------

    def seed_initial(
        self,
        markets: list[str] | None = None,
        strategies: list[str] | None = None,
    ) -> list[BotSlot]:
        """Kayıt defteri boşsa ilk ``MAX_SLOTS`` slotu oluştur.

        Önce ``DEFAULT_SLOT_SEEDS`` kullanılır; yetmezse ya da geçersiz
        kombinasyon varsa ``markets x strategies`` round-robin'e düşer.
        Zaten dolu slotlar **dokunulmaz** — idempotent.
        """
        if len(self._slots) >= self.MAX_SLOTS:
            return self.all_slots()

        markets_avail = list(markets) if markets else None
        strategies_avail = list(strategies) if strategies else None

        # Önce default seed'leri filtrele (verilmiş kısıtlara göre)
        candidates: list[tuple[str, str]] = []
        for fam, mkt in DEFAULT_SLOT_SEEDS:
            if markets_avail and mkt not in markets_avail:
                continue
            if strategies_avail and fam not in strategies_avail:
                continue
            candidates.append((fam, mkt))

        # Yetersiz olursa round-robin doldur
        if len(candidates) < self.MAX_SLOTS:
            ms = markets_avail or [m for _, m in DEFAULT_SLOT_SEEDS]
            ss = strategies_avail or [f for f, _ in DEFAULT_SLOT_SEEDS]
            existing = set(candidates)
            i, j = 0, 0
            while len(candidates) < self.MAX_SLOTS and ms and ss:
                fam = ss[j % len(ss)]
                mkt = ms[i % len(ms)]
                key = (fam, mkt)
                if key not in existing:
                    candidates.append(key)
                    existing.add(key)
                i += 1
                # Aile sırasını da kaydır → çeşitlilik
                if i % len(ms) == 0:
                    j += 1
                # Sonsuz döngü güvenliği
                if i > len(ms) * len(ss) * 2:
                    break

        # Mevcut slot id'leri bul, eksikleri doldur
        existing_ids = set(self._slots.keys())
        next_id = 1
        for fam, mkt in candidates[: self.MAX_SLOTS]:
            while next_id in existing_ids and next_id <= self.MAX_SLOTS:
                next_id += 1
            if next_id > self.MAX_SLOTS:
                break
            slot = BotSlot(
                slot_id=next_id,
                strategy_family=fam,
                market=mkt,
                created_cycle=0,
            )
            self._slots[slot.slot_id] = slot
            existing_ids.add(slot.slot_id)
            next_id += 1

        self._save()
        return self.all_slots()

    def seed_slot(
        self,
        slot_id: int,
        strategy_family: str | None = None,
        market: str | None = None,
        current_cycle: int = 0,
    ) -> BotSlot:
        """Boş (veya yeni) bir slotu doldur.

        Aile/market verilmezse, mevcut DEFAULT_SLOT_SEEDS'ten henüz
        kullanılmamış ilk eşleşme alınır.
        """
        if not (1 <= slot_id <= self.MAX_SLOTS):
            raise ValueError(f"slot_id {slot_id} aralık dışı (1..{self.MAX_SLOTS})")

        # Otomatik seçim — kullanılmamış default'ları topla
        if strategy_family is None or market is None:
            in_use: set[tuple[str, str]] = {
                (s.strategy_family, s.market)
                for s in self._slots.values()
                if s.status == "active" and s.slot_id != slot_id
            }
            for fam, mkt in DEFAULT_SLOT_SEEDS:
                if (fam, mkt) not in in_use:
                    strategy_family = strategy_family or fam
                    market = market or mkt
                    break
            # Hâlâ None ise default ilk seed'e düş
            if strategy_family is None or market is None:
                strategy_family = strategy_family or DEFAULT_SLOT_SEEDS[0][0]
                market = market or DEFAULT_SLOT_SEEDS[0][1]

        slot = BotSlot(
            slot_id=slot_id,
            strategy_family=strategy_family,
            market=market,
            created_cycle=int(current_cycle),
            last_improvement_cycle=int(current_cycle),
        )
        self._slots[slot_id] = slot
        self._save()
        return slot

    # ------------------------------------------------------------------
    # Picking
    # ------------------------------------------------------------------

    def pick_next_slot(self, cycle: int) -> BotSlot:
        """Round-robin: en uzun süredir iyileşmemiş AKTİF slotu seç.

        ``status == "empty"`` slotlar daha yüksek önceliğe sahiptir
        (yeniden seed edilebilsin diye). Eğer hiç slot yoksa
        ``KeyError`` fırlatır — caller önce ``seed_initial`` çağırmalı.
        """
        if not self._slots:
            raise KeyError("BotRegistry boş — önce seed_initial çağırın")

        # Önce empty slotları döndür (caller seed'leyecek)
        empties = [s for s in self._slots.values() if s.status == "empty"]
        if empties:
            empties.sort(key=lambda s: s.slot_id)
            return empties[0]

        # Sonra aktif olanlar arasından en eski iyileşme tarihli olan
        active = [s for s in self._slots.values() if s.status != "empty"]
        if not active:
            # Hiç aktif yok ama empty de yok — ilk slotu döndür (defensive)
            return next(iter(self._slots.values()))
        # Round-robin: en az iterasyon almış slot öncelik.
        # iter-bazlı sıralama orchestrator restart'larından bağımsız çalışır.
        # Tie-breaker: last_attempted_cycle (eski → önce), sonra slot_id.
        active.sort(key=lambda s: (s.iterations, s.last_attempted_cycle, s.slot_id))
        return active[0]

    # ------------------------------------------------------------------
    # Update / iterate
    # ------------------------------------------------------------------

    def try_update(
        self,
        slot_id: int,
        new_params: dict[str, Any],
        new_metrics: dict[str, Any],
        cycle: int = 0,
    ) -> bool:
        """Yeni metriği mevcut lifetime best ile karşılaştır.

        Daha iyi ise: ``current_params`` + ``lifetime_best_*`` güncellenir,
        ``accepted_updates += 1``, ``last_improvement_cycle = cycle``.
        Daha kötü/eşit ise: yalnızca ``iterations += 1``.

        Returns
        -------
        True  — params iyileştirildi.
        False — değişiklik yok, sadece iterasyon sayıldı.
        """
        slot = self._slots.get(slot_id)
        if slot is None:
            logger.warning(f"try_update: slot {slot_id} yok")
            return False

        sharpe = float(new_metrics.get("sharpe", 0.0) or 0.0)
        oos_sharpe = float(new_metrics.get("oos_sharpe", 0.0) or 0.0)
        slot.iterations += 1
        slot.last_attempted_cycle = int(cycle)
        slot.consecutive_skips = 0  # gerçek run → skip sayacı sıfırla
        slot.current_basket_sharpe = sharpe

        improved = sharpe > slot.lifetime_best_sharpe
        if improved:
            slot.current_params = dict(new_params or {})
            slot.lifetime_best_params = dict(new_params or {})
            slot.lifetime_best_sharpe = sharpe
            slot.lifetime_best_oos_sharpe = oos_sharpe
            slot.accepted_updates += 1
            slot.last_improvement_cycle = int(cycle)

        self._save()
        return improved

    def mark_attempted(self, slot_id: int, cycle: int, skip_reason: str | None = None) -> None:
        """Skip durumunda da çağrılır → round-robin'in slotu kilitlemesini engeller.
        ``skip_reason`` verilirse ``consecutive_skips`` artar; 10+'da slot retire'a alınır."""
        slot = self._slots.get(slot_id)
        if slot is None:
            return
        slot.last_attempted_cycle = int(cycle)
        if skip_reason:
            slot.consecutive_skips += 1
            if slot.consecutive_skips >= 10 and slot.status == "active":
                slot.status = "empty"
                slot.notes = (slot.notes + f" | retired_after_{slot.consecutive_skips}_skips:{skip_reason}").strip(" |")
                logger.warning(f"slot {slot_id} ({slot.strategy_family}/{slot.market}): {slot.consecutive_skips} ardışık skip → empty")
        self._save()

    def append_note(self, slot_id: int, note: str) -> None:
        """Slot notes alanına kısa bir takip metni ekle (ör. skip sebebi)."""
        slot = self._slots.get(slot_id)
        if slot is None:
            return
        # Notları kısa tut — son 1 KB ile sınırla
        new_notes = (slot.notes + " | " + note).strip(" |")
        if len(new_notes) > 1024:
            new_notes = new_notes[-1024:]
        slot.notes = new_notes
        self._save()

    # ------------------------------------------------------------------
    # Retire
    # ------------------------------------------------------------------

    def retire_if_stale(self, slot_id: int, current_cycle: int) -> bool:
        """Stale slotu boşalt (status='empty', current_params={}).

        Kural: ``iterations >= RETIRE_AFTER_ITER`` AND
        ``lifetime_best_sharpe < RETIRE_BELOW_SHARPE`` → retire.
        """
        slot = self._slots.get(slot_id)
        if slot is None:
            return False
        if slot.status == "empty":
            return False
        if slot.iterations < RETIRE_AFTER_ITER:
            return False
        if slot.lifetime_best_sharpe >= RETIRE_BELOW_SHARPE:
            return False

        old_family = slot.strategy_family
        old_market = slot.market
        slot.status = "empty"
        slot.current_params = {}
        slot.notes = (
            (slot.notes + f" | retired@{current_cycle} "
             f"(iter={slot.iterations}, best_sharpe={slot.lifetime_best_sharpe:.2f}, "
             f"was={old_family}/{old_market})")
            .strip(" |")
        )
        self._save()
        logger.warning(
            f"BotRegistry: slot {slot_id} ({old_family}/{old_market}) "
            f"retired — iter={slot.iterations} sharpe={slot.lifetime_best_sharpe:.2f}"
        )
        return True

    # ------------------------------------------------------------------
    # Sorgular
    # ------------------------------------------------------------------

    def all_slots(self) -> list[BotSlot]:
        return sorted(self._slots.values(), key=lambda s: s.slot_id)

    def get(self, slot_id: int) -> BotSlot | None:
        return self._slots.get(slot_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_slots": self.MAX_SLOTS,
            "n_slots": len(self._slots),
            "slots": [s.to_dict() for s in self.all_slots()],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], path: str | Path | None = None) -> "BotRegistry":
        """Test/restore yardımcısı — verilen dict'ten registry üret."""
        reg = cls(path=path or "artifacts/bot_registry.json")
        reg._slots = {}
        for item in data.get("slots") or []:
            slot = BotSlot.from_dict(item)
            reg._slots[slot.slot_id] = slot
        reg._save()
        return reg
