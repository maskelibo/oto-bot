"""Robustness Suite — promosyon adaylarını çoklu varyantta doğrula.

Bir strateji tek (symbol, timeframe) kombinasyonunda iyi sonuç verdi diye
gerçekten robust olduğunu söyleyemeyiz. Kurumsal pratik: aynı parametre
setini **başka 8-12 kombinasyonda** koş, Sharpe dağılımına bak.

Bu modül:
    - `RobustnessQueue` — pending varyant testleri için persistent kuyruk
      (artifacts/robustness_queue.jsonl).
    - `schedule_variants(origin_experiment)` — bir deney için otomatik
      varyantlar üretir ve kuyruğa yazar.
    - `next_pending()` — orchestrator cycle başında bu kuyruktan ilk test'i
      çeker (FIFO).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Kopya import: orchestrator universe'i
MARKET_SYMBOLS: dict[str, list[str]] = {
    "crypto": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT"],
    "forex": ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF"],
    "us_equities": ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "TSLA", "META"],
    "bist": ["THYAO", "GARAN", "ASELS", "SISE", "EREGL", "KCHOL"],
}
TIMEFRAMES: dict[str, list[str]] = {
    "day": ["1h", "4h"],
    "swing": ["4h", "1d"],
    "scalp": ["5m", "15m"],
}


@dataclass
class RobustnessTest:
    test_id: str
    origin_experiment_id: str
    origin_title: str
    market: str
    strategy: str
    symbol: str
    timeframe: str
    params: dict[str, Any]
    reason: str
    created_at: str
    # Opsiyonel: bu test özellikle N yıllık pencere istiyor
    data_window_years: float | None = None


class RobustnessQueue:
    def __init__(self, path: str | Path = "artifacts/robustness_queue.jsonl") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()

    # ------------------------------------------------------------------

    def push(self, test: RobustnessTest) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(test), ensure_ascii=False) + "\n")

    def push_many(self, tests: list[RobustnessTest]) -> int:
        if not tests:
            return 0
        with self.path.open("a", encoding="utf-8") as fh:
            for t in tests:
                fh.write(json.dumps(asdict(t), ensure_ascii=False) + "\n")
        return len(tests)

    # ------------------------------------------------------------------

    def next_pending(self) -> RobustnessTest | None:
        """Dequeue en eski test'i (FIFO). Queue boşsa None."""
        batch = self.next_pending_batch(1)
        return batch[0] if batch else None

    def next_pending_batch(self, n: int) -> list[RobustnessTest]:
        """Dequeue en eski N test'i (FIFO). Tek dosya yazımı = O(N) yerine O(file_size)."""
        if n <= 0:
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        pending = [l for l in lines if l.strip()]
        if not pending:
            return []

        head = pending[:n]
        remaining = pending[n:]
        out: list[RobustnessTest] = []
        for raw in head:
            try:
                out.append(RobustnessTest(**json.loads(raw)))
            except Exception:
                continue
        # Atomic-ish rewrite: tmp dosyaya yaz, sonra os.replace ile değiştir
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        body = "\n".join(remaining) + ("\n" if remaining else "")
        tmp.write_text(body, encoding="utf-8")
        import os as _os
        _os.replace(tmp, self.path)
        return out

    def peek_all(self, limit: int = 500) -> list[dict[str, Any]]:
        lines = self.path.read_text(encoding="utf-8").splitlines()
        out = []
        for l in lines[:limit]:
            if l.strip():
                try:
                    out.append(json.loads(l))
                except Exception:
                    continue
        return out

    def count(self) -> int:
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return sum(1 for l in lines if l.strip())

    def clear(self) -> int:
        n = self.count()
        self.path.write_text("", encoding="utf-8")
        return n


# ---------------------------------------------------------------------------
# Variant generation
# ---------------------------------------------------------------------------


def generate_variants(
    origin: dict[str, Any],
    include_timeframes: bool = True,
    max_variants: int = 12,
    reason: str = "cross_validation",
) -> list[RobustnessTest]:
    """Bir deneyin params'ı + family'si ile başka (symbol, timeframe) varyantları üret."""
    market = origin.get("market") or "crypto"
    strategy = origin.get("strategy_family") or origin.get("strategy") or "day"
    origin_symbol = origin.get("symbol") or ""
    origin_tf = origin.get("bar_timeframe") or origin.get("timeframe") or "1h"
    params = origin.get("strategy_params") or origin.get("params") or {}
    origin_exp_id = origin.get("experiment_id") or ""
    origin_title = origin.get("hypothesis_title") or ""

    symbols = MARKET_SYMBOLS.get(market, [])
    if not symbols:
        return []
    tfs = TIMEFRAMES.get(strategy, [origin_tf])

    tests: list[RobustnessTest] = []
    now = datetime.now(timezone.utc).isoformat()

    # Diğer semboller + aynı timeframe
    for sym in symbols:
        if sym == origin_symbol:
            continue
        tests.append(RobustnessTest(
            test_id=str(uuid.uuid4()),
            origin_experiment_id=origin_exp_id,
            origin_title=origin_title,
            market=market, strategy=strategy,
            symbol=sym, timeframe=origin_tf,
            params=dict(params), reason=reason,
            created_at=now,
        ))
        if len(tests) >= max_variants // 2:
            break

    # Farklı timeframe varyantları (aynı sembol + başka tf)
    if include_timeframes:
        other_tfs = [tf for tf in tfs if tf != origin_tf]
        for tf in other_tfs:
            for sym in symbols[:3]:
                if sym == origin_symbol and tf == origin_tf:
                    continue
                tests.append(RobustnessTest(
                    test_id=str(uuid.uuid4()),
                    origin_experiment_id=origin_exp_id,
                    origin_title=origin_title,
                    market=market, strategy=strategy,
                    symbol=sym, timeframe=tf,
                    params=dict(params), reason=reason,
                    created_at=now,
                ))
                if len(tests) >= max_variants:
                    return tests

    return tests[:max_variants]


# ---------------------------------------------------------------------------
# Auto-schedule trigger
# ---------------------------------------------------------------------------


def is_promising(result: dict[str, Any]) -> bool:
    """Bir sonuç robustness suite'e alınmayı hak ediyor mu?"""
    sharpe = float(result.get("sharpe") or 0)
    cagr = float(result.get("cagr") or 0)
    pf = float(result.get("profit_factor") or 0)
    trades = int(result.get("total_trades") or 0)
    dd = float(result.get("max_drawdown") or 0)

    # Sharpe yüksek VE CAGR anlamlı VE örneklem makul VE DD kabul edilebilir
    return (
        sharpe >= 1.5
        and cagr >= 0.30
        and trades >= 30
        and dd >= -0.15
        and pf >= 1.2
    )
