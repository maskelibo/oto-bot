"""RollingPerformanceMonitor — Faz 5 promote pod'lari icin rolling metric.

Doktrin:
    Pod_allocator HARD_STOP_DD (-7.5%) / SOFT_STOP_DD (-5%) ile sermaye
    bazli kill switch'i devrede tutar; ancak Sharpe / win-rate gibi
    risk-adjusted metricleri izlemez. RollingPerformanceMonitor her promoted
    pod icin son N cycle'in:
        - rolling Sharpe (mean(pnl) / std(pnl) * sqrt(252) yaklasimi)
        - rolling drawdown (peak-to-trough equity)
        - rolling win-rate (pozitif cycle pay)
    hesaplar; uc yeni alarm seviyesi tanimlar:
        - retire: rolling_sharpe < 1.0 (3 ardisik review) veya
          rolling_dd <= -0.10 → pod kapatma onerisi.
        - demote: 1 review'da rolling_sharpe < 1.0 veya rolling_dd <= -0.05
          → "challenger seviyesine indir" onerisi (ileride sermaye yarila).

Persistans:
    artifacts/rolling_monitor.json — pod_id'lere gore (cycle, pnl, equity)
    serisi + ardisik dusuk-Sharpe sayaclari, atomic write.

Notlar:
    - Annualizasyon faktoru 252; intraday cycle'lar icin de kabul edilebilir
      bir aproximasyondur (CLAUDE.md: tum risk metrics annualized).
    - Tek bir gozlem icin Sharpe hesaplanmaz (std=0 → bolme hatasi); n_obs>=2
      zorunludur.
    - Ringbuffer: 200 cycle (token & RAM koruma).
"""

from __future__ import annotations

import json
import math
import os
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Deque

# ---------------------------------------------------------------------------
# Veri tipleri
# ---------------------------------------------------------------------------


@dataclass
class CycleObservation:
    """Tek cycle icin (pnl, equity, cycle_no) snapshot'i."""

    cycle: int
    pnl: float
    equity: float


# ---------------------------------------------------------------------------
# Ana sinif
# ---------------------------------------------------------------------------


class RollingPerformanceMonitor:
    """Promoted pod'lar icin rolling Sharpe / DD / win-rate izleyicisi."""

    HISTORY_MAX: int = 200
    DEFAULT_WINDOW: int = 20
    ANNUALIZATION: float = 252.0

    # Esikler — Faz 5 doktrini
    RETIRE_SHARPE: float = 1.0
    RETIRE_DD: float = -0.10
    RETIRE_CONSECUTIVE: int = 3

    DEMOTE_SHARPE: float = 1.0
    DEMOTE_DD: float = -0.05

    def __init__(
        self,
        path: str | Path = "artifacts/rolling_monitor.json",
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # pod_id -> deque[CycleObservation]
        self._history: dict[str, Deque[CycleObservation]] = {}
        # pod_id -> ardisik review'da Sharpe esik altinda kalma sayaci
        self._consecutive_below: dict[str, int] = {}
        self.load()

    # ------------------------------------------------------------------
    # Persistans
    # ------------------------------------------------------------------

    def load(self) -> None:
        if not self.path.exists():
            self._history = {}
            self._consecutive_below = {}
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self._history = {}
            self._consecutive_below = {}
            return

        self._history = {}
        for pid, hist_raw in (raw.get("history") or {}).items():
            dq: Deque[CycleObservation] = deque(maxlen=self.HISTORY_MAX)
            if isinstance(hist_raw, list):
                for h in hist_raw:
                    try:
                        dq.append(CycleObservation(
                            cycle=int(h["cycle"]),
                            pnl=float(h["pnl"]),
                            equity=float(h["equity"]),
                        ))
                    except Exception:
                        continue
            self._history[pid] = dq

        self._consecutive_below = {}
        for pid, n in (raw.get("consecutive_below_threshold") or {}).items():
            try:
                self._consecutive_below[str(pid)] = int(n)
            except Exception:
                continue

    def save(self) -> None:
        payload: dict[str, Any] = {
            "history": {
                pid: [asdict(o) for o in dq]
                for pid, dq in self._history.items()
            },
            "consecutive_below_threshold": dict(self._consecutive_below),
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------
    # Yazma
    # ------------------------------------------------------------------

    def record(
        self,
        pod_id: str,
        cycle_pnl: float,
        equity: float,
        cycle_number: int,
    ) -> None:
        """Pod icin yeni cycle gozlemini ekle (HISTORY_MAX ile sinirli)."""
        dq = self._history.get(pod_id)
        if dq is None:
            dq = deque(maxlen=self.HISTORY_MAX)
            self._history[pod_id] = dq
        dq.append(CycleObservation(
            cycle=int(cycle_number),
            pnl=float(cycle_pnl),
            equity=float(equity),
        ))
        self.save()

    def remove(self, pod_id: str) -> bool:
        """Pod retire edildikten sonra state'i tamamen at."""
        removed = False
        if pod_id in self._history:
            del self._history[pod_id]
            removed = True
        if pod_id in self._consecutive_below:
            del self._consecutive_below[pod_id]
            removed = True
        if removed:
            self.save()
        return removed

    # ------------------------------------------------------------------
    # Hesaplama
    # ------------------------------------------------------------------

    def rolling_metrics(
        self,
        pod_id: str,
        window: int = DEFAULT_WINDOW,
    ) -> dict[str, Any]:
        """Son `window` gozlem uzerinden rolling Sharpe / DD / win-rate.

        Donus alanlari:
            rolling_sharpe: float | None  — gozlem yetersizse None
            rolling_dd:     float          — peak-to-trough (negatif)
            rolling_winrate: float | None  — pozitif cycle pay (None: gozlem yok)
            n_obs:          int            — kullanilan gozlem sayisi
        """
        dq = self._history.get(pod_id)
        if dq is None or len(dq) == 0:
            return {
                "rolling_sharpe": None,
                "rolling_dd": 0.0,
                "rolling_winrate": None,
                "n_obs": 0,
            }

        recent = list(dq)[-window:]
        n = len(recent)

        # PnL'den getiri serisi turet (pnl / equity_baslangic)
        # equity her gozlemde "guncel equity" oldugu icin onceki equity'i
        # baz alarak yuzde getiri hesabi daha duzgun.
        rets: list[float] = []
        for i, obs in enumerate(recent):
            if i == 0:
                # Ilk gozlemde onceki equity bilinmiyor — pnl/equity fallback
                base = obs.equity - obs.pnl
                if base <= 0:
                    base = obs.equity if obs.equity > 0 else 1.0
                rets.append(obs.pnl / base if base != 0 else 0.0)
            else:
                prev_eq = recent[i - 1].equity
                if prev_eq <= 0:
                    rets.append(0.0)
                else:
                    rets.append(obs.pnl / prev_eq)

        # Sharpe — annualized
        rolling_sharpe: float | None
        if n < 2:
            rolling_sharpe = None
        else:
            mean_r = sum(rets) / n
            var = sum((r - mean_r) ** 2 for r in rets) / (n - 1)
            std = math.sqrt(var) if var > 0 else 0.0
            if std <= 0:
                rolling_sharpe = None
            else:
                rolling_sharpe = (mean_r / std) * math.sqrt(self.ANNUALIZATION)

        # Drawdown — peak-to-trough equity
        peak = -math.inf
        max_dd = 0.0
        for obs in recent:
            if obs.equity > peak:
                peak = obs.equity
            if peak > 0:
                dd = (obs.equity - peak) / peak
                if dd < max_dd:
                    max_dd = dd

        # Win-rate
        wins = sum(1 for r in rets if r > 0)
        winrate = wins / n if n > 0 else None

        return {
            "rolling_sharpe": (round(float(rolling_sharpe), 4)
                               if rolling_sharpe is not None else None),
            "rolling_dd": round(float(max_dd), 4),
            "rolling_winrate": (round(float(winrate), 4)
                                if winrate is not None else None),
            "n_obs": n,
        }

    # ------------------------------------------------------------------
    # Karar mekanizmalari
    # ------------------------------------------------------------------

    def should_retire(
        self,
        pod_id: str,
        window: int = DEFAULT_WINDOW,
    ) -> tuple[bool, str]:
        """Retire onerisi:
            - rolling_dd <= RETIRE_DD (anlik kill).
            - rolling_sharpe < RETIRE_SHARPE 3 ardisik review boyunca.
        """
        m = self.rolling_metrics(pod_id, window=window)
        n = int(m.get("n_obs") or 0)
        # Yeterince gozlem yoksa retire edilmez
        if n < 2:
            return False, f"insufficient observations (n={n})"

        dd = float(m.get("rolling_dd") or 0.0)
        if dd <= self.RETIRE_DD:
            # Sayaci sifirla — direkt karar
            self._consecutive_below[pod_id] = 0
            self.save()
            return True, f"rolling_dd={dd:.2%} <= {self.RETIRE_DD:.2%}"

        sharpe = m.get("rolling_sharpe")
        if sharpe is None or sharpe < self.RETIRE_SHARPE:
            self._consecutive_below[pod_id] = self._consecutive_below.get(pod_id, 0) + 1
            self.save()
            if self._consecutive_below[pod_id] >= self.RETIRE_CONSECUTIVE:
                # Sayaci sifirla — kapatma karari verildi
                self._consecutive_below[pod_id] = 0
                self.save()
                sh_str = f"{sharpe:.2f}" if sharpe is not None else "None"
                return True, (
                    f"rolling_sharpe={sh_str} < {self.RETIRE_SHARPE:.2f} "
                    f"for {self.RETIRE_CONSECUTIVE} consecutive reviews"
                )
            sh_str = f"{sharpe:.2f}" if sharpe is not None else "None"
            return False, (
                f"rolling_sharpe={sh_str} below {self.RETIRE_SHARPE:.2f} "
                f"({self._consecutive_below[pod_id]}/{self.RETIRE_CONSECUTIVE})"
            )

        # Esik ustunde — sayaci resetle
        if self._consecutive_below.get(pod_id, 0) > 0:
            self._consecutive_below[pod_id] = 0
            self.save()
        return False, f"rolling_sharpe={sharpe:.2f} >= {self.RETIRE_SHARPE:.2f}"

    def should_demote(
        self,
        pod_id: str,
        window: int = DEFAULT_WINDOW,
    ) -> tuple[bool, str]:
        """Demote onerisi (tek review'da tetiklenir):
            - rolling_sharpe < DEMOTE_SHARPE  veya
            - rolling_dd <= DEMOTE_DD
        """
        m = self.rolling_metrics(pod_id, window=window)
        n = int(m.get("n_obs") or 0)
        if n < 2:
            return False, f"insufficient observations (n={n})"

        dd = float(m.get("rolling_dd") or 0.0)
        if dd <= self.DEMOTE_DD:
            return True, f"rolling_dd={dd:.2%} <= {self.DEMOTE_DD:.2%}"

        sharpe = m.get("rolling_sharpe")
        if sharpe is None or sharpe < self.DEMOTE_SHARPE:
            sh_str = f"{sharpe:.2f}" if sharpe is not None else "None"
            return True, f"rolling_sharpe={sh_str} < {self.DEMOTE_SHARPE:.2f}"

        return False, f"rolling_sharpe={sharpe:.2f} >= {self.DEMOTE_SHARPE:.2f}"

    # ------------------------------------------------------------------
    # Sorgu yardimcilari
    # ------------------------------------------------------------------

    def known_ids(self) -> list[str]:
        return list(self._history.keys())

    def history_for(self, pod_id: str) -> list[CycleObservation]:
        dq = self._history.get(pod_id)
        return list(dq) if dq is not None else []
