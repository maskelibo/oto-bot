"""Ledger Allocator — sermaye dağıtım motoru (pod-tabanlı).

Millennium ve Citadel'in pod modeli:
    - Her strateji bağımsız bir "pod" olarak çalışır.
    - Başlangıç tahsisi: sabit başlangıç sermayesi (bu laboratuvarda %100
      book'un eşit bölünmesi default'tur).
    - Sharpe ve drawdown'a göre sermaye dinamik olarak kayar.
    - Otomatik stop-out: %5 DD'de sermaye yarıya düşer ("halved"),
      %7.5 DD'de pod kapatılır ("retired").

Bu ajan:
    1. Mevcut pod'ları yönetir (add/update/retire).
    2. Yeni sermaye tahsisi önerir (rebalance).
    3. Stop-out kurallarını uygular.
    4. Pod state'lerini diske yazar (memories/pods.json).

Finansal gerçeklik: biz paper trading'deyiz. Sermaye sayıları sentetik,
hiçbir gerçek para değil.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from oto_bot.core.models import PodState


class PodAllocator:
    HARD_STOP_DD: float = -0.075  # %7.5 → pod kapanır
    SOFT_STOP_DD: float = -0.05   # %5   → sermaye yarıya iner

    def __init__(
        self,
        path: str | Path = "memories/pods.json",
        book_capital: float = 100_000.0,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.book_capital = book_capital
        self._pods: dict[str, PodState] = {}
        self.load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self) -> None:
        if not self.path.exists():
            self._pods = {}
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self._pods = {}
        for item in raw:
            item["last_updated"] = datetime.fromisoformat(item["last_updated"])
            for field_name in ("halved_at", "retired_at"):
                v = item.get(field_name)
                item[field_name] = datetime.fromisoformat(v) if v else None
            self._pods[item["pod_id"]] = PodState(**item)

    def save(self) -> None:
        payload = []
        for p in self._pods.values():
            row = asdict(p)
            row["last_updated"] = p.last_updated.isoformat()
            row["halved_at"] = p.halved_at.isoformat() if p.halved_at else None
            row["retired_at"] = p.retired_at.isoformat() if p.retired_at else None
            payload.append(row)
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    # Pod yaşam döngüsü
    # ------------------------------------------------------------------

    def create_pod(
        self,
        strategy_family: str,
        market: str,
        initial_capital: float | None = None,
    ) -> PodState:
        initial = initial_capital if initial_capital is not None else self.book_capital * 0.15
        pod = PodState(
            pod_id=str(uuid.uuid4()),
            strategy_family=strategy_family,
            market=market,
            allocated_capital=initial,
            current_capital=initial,
            peak_capital=initial,
            status="active",
        )
        self._pods[pod.pod_id] = pod
        self.save()
        return pod

    def update_pod(
        self,
        pod_id: str,
        current_capital: float | None = None,
        sharpe_30d: float | None = None,
        sortino_30d: float | None = None,
        trades_30d: int | None = None,
        win_rate_30d: float | None = None,
        correlation_to_book: float | None = None,
    ) -> PodState | None:
        pod = self._pods.get(pod_id)
        if pod is None:
            return None
        now = datetime.now(timezone.utc)
        if current_capital is not None:
            pod.current_capital = current_capital
            pod.peak_capital = max(pod.peak_capital, current_capital)
            pod.drawdown_pct = (pod.current_capital - pod.peak_capital) / max(pod.peak_capital, 1e-9)
        if sharpe_30d is not None:
            pod.sharpe_30d = sharpe_30d
        if sortino_30d is not None:
            pod.sortino_30d = sortino_30d
        if trades_30d is not None:
            pod.trades_30d = trades_30d
        if win_rate_30d is not None:
            pod.win_rate_30d = win_rate_30d
        if correlation_to_book is not None:
            pod.correlation_to_book = correlation_to_book
        pod.last_updated = now

        # Stop-out enforcement
        if pod.drawdown_pct <= self.HARD_STOP_DD and pod.status != "retired":
            pod.status = "retired"
            pod.retired_at = now
        elif pod.drawdown_pct <= self.SOFT_STOP_DD and pod.status == "active":
            pod.status = "halved"
            pod.halved_at = now
            pod.allocated_capital *= 0.5
            pod.current_capital = min(pod.current_capital, pod.allocated_capital)

        self.save()
        return pod

    def retire_pod(self, pod_id: str) -> bool:
        pod = self._pods.get(pod_id)
        if pod is None:
            return False
        pod.status = "retired"
        pod.retired_at = datetime.now(timezone.utc)
        self.save()
        return True

    # ------------------------------------------------------------------
    # Sorgu
    # ------------------------------------------------------------------

    def all(self) -> list[PodState]:
        return list(self._pods.values())

    def active(self) -> list[PodState]:
        return [p for p in self._pods.values() if p.status != "retired"]

    def by_family(self, strategy_family: str) -> list[PodState]:
        return [p for p in self._pods.values() if p.strategy_family == strategy_family]

    # ------------------------------------------------------------------
    # Dinamik rebalance
    # ------------------------------------------------------------------

    def rebalance(
        self,
        min_sharpe: float = 0.5,
        max_sharpe_boost: float = 1.3,
        dd_penalty_scale: float = 0.5,
    ) -> list[dict[str, Any]]:
        """Performansa göre sermaye payını yeniden dağıt.

        Her aktif pod için bir ağırlık skoru hesaplanır:
            weight = base * (sharpe_30d / min_sharpe)  — Sharpe pozitifse
            weight *= max(0.3, 1 + dd_pct)             — drawdown varsa cezalandır
            weight = clip(weight, 0.3, max_sharpe_boost)

        Toplam weight 1.0'a normalize edilir ve book_capital buna göre dağılır.
        Yalnızca "active" pod'lar dikkate alınır; "halved" pod'lar yarıda kalır,
        "retired" pod'lar sıfır payda kalır.
        """
        active_pods = [p for p in self._pods.values() if p.status == "active"]
        if not active_pods:
            return []

        weights: list[float] = []
        for p in active_pods:
            base = 1.0
            if p.sharpe_30d > 0:
                base *= min(max_sharpe_boost, max(0.3, p.sharpe_30d / max(min_sharpe, 1e-9)))
            else:
                base *= 0.3
            if p.drawdown_pct < 0:
                base *= max(0.3, 1.0 + (p.drawdown_pct * dd_penalty_scale / abs(self.SOFT_STOP_DD)))
            weights.append(base)

        total_w = sum(weights) or 1.0
        active_book = sum(p.current_capital for p in self.active())

        moves: list[dict[str, Any]] = []
        for pod, w in zip(active_pods, weights):
            target = active_book * (w / total_w)
            delta = target - pod.allocated_capital
            pct_change = (delta / max(pod.allocated_capital, 1e-9))
            if abs(pct_change) < 0.05:
                continue  # micro-move atla
            pod.allocated_capital = target
            pod.last_updated = datetime.now(timezone.utc)
            moves.append({
                "pod_id": pod.pod_id,
                "strategy_family": pod.strategy_family,
                "market": pod.market,
                "new_allocation": round(target, 2),
                "delta": round(delta, 2),
                "pct_change": round(pct_change, 3),
                "sharpe_30d": pod.sharpe_30d,
                "drawdown_pct": pod.drawdown_pct,
            })
        self.save()
        return moves
