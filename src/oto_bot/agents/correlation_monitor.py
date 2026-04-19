"""CorrelationMonitor — pod return correlation gözcüsü.

Amac:
    Yeni bir pod aday adayinin, halihazirda yasayan pod'larin trade-return
    serileriyle yuksek korelasyon tasiyip tasimadigini denetlemek. Yuksek
    korelasyonlu pod'lar ayni risk faktorune iki kez maruz kalmak demektir
    (Apex PortfolioRisk doktrini): kabul edilirse "diversification illusion"
    yaratir.

Persistans:
    artifacts/correlation_state.json — yalniz return history'leri tutulur
    (her pod icin son maxlen return). Korelasyon matrisi her cagriligida
    yeniden hesaplanir; dolayisiyla onbellek tutulmaz.

Notlar:
    - Pearson korelasyonu icin numpy.corrcoef kullanilir.
    - En az 3 ortak gozlem yoksa korelasyon hesaplanmaz (None).
    - Veri esitsizliginde son N (= min uzunluk) noktasi karsilastirilir.
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Deque, Iterable

import numpy as np


class CorrelationMonitor:
    """Promote edilmis pod'lar arasi return korelasyonunu izler."""

    def __init__(
        self,
        path: str | Path = "artifacts/correlation_state.json",
        maxlen: int = 200,
        min_overlap: int = 3,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.maxlen = maxlen
        self.min_overlap = min_overlap
        self._returns: dict[str, Deque[float]] = {}
        self.load()

    # ------------------------------------------------------------------
    # Persistans
    # ------------------------------------------------------------------

    def load(self) -> None:
        if not self.path.exists():
            self._returns = {}
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self._returns = {}
            return
        self._returns = {}
        for sid, hist in raw.items():
            if not isinstance(hist, list):
                continue
            self._returns[sid] = deque(
                (float(x) for x in hist if isinstance(x, (int, float))),
                maxlen=self.maxlen,
            )

    def save(self) -> None:
        payload = {sid: list(hist) for sid, hist in self._returns.items()}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # ------------------------------------------------------------------
    # Public API — yazma
    # ------------------------------------------------------------------

    def register_strategy_returns(
        self, strategy_id: str, returns: Iterable[float]
    ) -> None:
        """Yeni bir stratejinin son N return'unu kaydet (maxlen ile sinirlanir)."""
        cleaned = [float(x) for x in returns if isinstance(x, (int, float))]
        self._returns[strategy_id] = deque(cleaned, maxlen=self.maxlen)
        self.save()

    def update_returns(self, strategy_id: str, new_return: float) -> None:
        """Mevcut bir stratejiye yeni cycle return'unu ekle."""
        if not isinstance(new_return, (int, float)):
            return
        dq = self._returns.get(strategy_id)
        if dq is None:
            dq = deque(maxlen=self.maxlen)
            self._returns[strategy_id] = dq
        dq.append(float(new_return))
        self.save()

    def remove(self, strategy_id: str) -> None:
        """Pod retire edildiginde history'sini at."""
        if strategy_id in self._returns:
            del self._returns[strategy_id]
            self.save()

    # ------------------------------------------------------------------
    # Public API — okuma / analiz
    # ------------------------------------------------------------------

    def returns_for(self, strategy_id: str) -> list[float]:
        return list(self._returns.get(strategy_id, []))

    def known_ids(self) -> list[str]:
        return list(self._returns.keys())

    def correlation_matrix(self) -> dict[tuple[str, str], float]:
        """Tum strateji ciftleri arasi pearson korelasyonu.

        Donus: {(id1, id2): corr} — id1 < id2 sirasiyla; nan veya
        hesaplanamayanlar atlanir.
        """
        ids = sorted(self._returns.keys())
        out: dict[tuple[str, str], float] = {}
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = ids[i], ids[j]
                corr = self._pairwise(
                    list(self._returns[a]), list(self._returns[b])
                )
                if corr is None:
                    continue
                out[(a, b)] = corr
        return out

    def check_new_pod(
        self,
        candidate_returns: Iterable[float],
        threshold: float = 0.7,
    ) -> tuple[bool, str]:
        """Aday return serisini mevcut tum pod'larla karsilastirir.

        |corr| > threshold tespit edilirse (False, "id @ corr=...") doner.
        Aksi halde (True, "ok"). Hic karsilastirma yapilamamissa True doner
        (cold-start: ilk pod'lar her zaman gecer).
        """
        candidate = [float(x) for x in candidate_returns if isinstance(x, (int, float))]
        if not candidate or not self._returns:
            return True, "ok"

        worst_id: str | None = None
        worst_corr: float = 0.0
        for sid, hist in self._returns.items():
            corr = self._pairwise(candidate, list(hist))
            if corr is None:
                continue
            if abs(corr) > abs(worst_corr):
                worst_corr = corr
                worst_id = sid

        if worst_id is not None and abs(worst_corr) > threshold:
            return False, f"corr={worst_corr:+.2f} vs {worst_id} > {threshold:.2f}"
        return True, "ok"

    # ------------------------------------------------------------------
    # Yardimcilar
    # ------------------------------------------------------------------

    def _pairwise(self, a: list[float], b: list[float]) -> float | None:
        """Iki seri icin pearson korelasyonu; uzunluk farkini son N ile hizalar."""
        n = min(len(a), len(b))
        if n < self.min_overlap:
            return None
        # Son N noktayi karsilastir (yeni return'ler daha bilgilendirici)
        a_arr = np.asarray(a[-n:], dtype=float)
        b_arr = np.asarray(b[-n:], dtype=float)
        if a_arr.std() == 0 or b_arr.std() == 0:
            return None
        try:
            corr = float(np.corrcoef(a_arr, b_arr)[0, 1])
        except Exception:
            return None
        if not np.isfinite(corr):
            return None
        return corr
