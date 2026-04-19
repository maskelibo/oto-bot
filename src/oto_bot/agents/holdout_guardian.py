"""HoldoutGuardian — sembol başına son %25 veriyi mühürler ve OOS doğrular.

Faz 2 anti-overfit kapısı: bir bot promote edilmeden önce, sembolün
**daha önce hiç görmediği** son %25 dilimi üzerinde tekrar koşulur.
Train Sharpe vs holdout Sharpe karşılaştırması yapılır.

Anti-tampering:
    - Holdout dilimi disk dışında, in-memory dict'te tutulur.
    - ``register_holdout`` aynı sembol için ikinci kez çağrıldığında
      overwrite YAPMAZ — ilk mühür korunur.
    - Public API `get_holdout` yoktur. Holdout DataFrame'ine yalnızca
      ``validate_promotion`` içinden, private accessor üzerinden ulaşılır.
    - ``artifacts/holdout_register.json`` yalnız metadata yazar (bar
      sayısı, son tarih, hash); raw veri diske düşmez.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from oto_bot.backtest.engine import BacktestEngine
from oto_bot.core.models import ExperimentResult
from oto_bot.strategies.base import Strategy, StrategyContext

logger = logging.getLogger("oto_bot.agents.holdout_guardian")


# ---------------------------------------------------------------------------
# Karar eşikleri
# ---------------------------------------------------------------------------

DEFAULT_OOS_FRACTION_OF_IS: float = 0.5  # OOS Sharpe >= IS Sharpe * 0.5
DEFAULT_OOS_FLOOR: float = 0.7           # OOS Sharpe >= 0.7 mutlak

HOLDOUT_FRACTION: float = 0.25
TRAIN_FRACTION: float = 1.0 - HOLDOUT_FRACTION


@dataclass
class HoldoutVerdict:
    """`validate_promotion` çıktısı."""

    passed: bool
    oos_sharpe: float
    expected_min: float
    reason: str


# ---------------------------------------------------------------------------
# HoldoutGuardian
# ---------------------------------------------------------------------------

class HoldoutGuardian:
    """Sembol başına holdout register + OOS validator.

    Tasarım kuralları:
        1. ``register_holdout(symbol, full_data)`` ilk çağrıda holdout'u
           mühürler; sonraki çağrılar no-op'tur.
        2. ``get_train(symbol, full_data)`` herkese açık — train kısmı
           backtest input'u olarak kullanılır.
        3. Holdout DataFrame'ine doğrudan public erişim yok; yalnızca
           ``validate_promotion`` içinden ``_get_sealed_holdout`` private
           method üzerinden okunur.
    """

    def __init__(
        self,
        register_path: str | Path = "artifacts/holdout_register.json",
        oos_fraction_of_is: float = DEFAULT_OOS_FRACTION_OF_IS,
        oos_floor: float = DEFAULT_OOS_FLOOR,
    ) -> None:
        self.register_path = Path(register_path)
        self.register_path.parent.mkdir(parents=True, exist_ok=True)
        self.oos_fraction_of_is = float(oos_fraction_of_is)
        self.oos_floor = float(oos_floor)

        # In-memory holdout deposu — sembol → DataFrame
        self._holdouts: dict[str, pd.DataFrame] = {}
        # Metadata register (sembol → meta dict). Diske yazılır.
        self._meta: dict[str, dict[str, Any]] = self._load_meta()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Train/Holdout split
    # ------------------------------------------------------------------

    @staticmethod
    def split_indices(n: int) -> tuple[int, int]:
        """N satır için (train_end, holdout_start) indekslerini döner."""
        train_end = int(n * TRAIN_FRACTION)
        return train_end, train_end

    def get_train(self, symbol: str, full_data: pd.DataFrame) -> pd.DataFrame:
        """İlk %75 — backtest input'u. Saf, idempotent."""
        if full_data is None or len(full_data) == 0:
            return full_data
        train_end, _ = self.split_indices(len(full_data))
        if train_end <= 0:
            return full_data.iloc[0:0].copy()
        return full_data.iloc[:train_end].copy().reset_index(drop=True)

    @staticmethod
    def _compute_holdout(full_data: pd.DataFrame) -> pd.DataFrame:
        n = len(full_data)
        train_end = int(n * TRAIN_FRACTION)
        return full_data.iloc[train_end:].copy().reset_index(drop=True)

    # ------------------------------------------------------------------
    # Register / mühürleme
    # ------------------------------------------------------------------

    def register_holdout(self, symbol: str, full_data: pd.DataFrame) -> bool:
        """Holdout'u mühürle. İkinci çağrıda overwrite yapmaz, False döner.

        Returns
        -------
        True  — yeni mühür yapıldı.
        False — sembol zaten kayıtlıydı; veri korundu.
        """
        if full_data is None or len(full_data) < 8:
            logger.warning(f"Holdout: {symbol} için veri çok kısa, register atlandı")
            return False

        with self._lock:
            if symbol in self._holdouts:
                # Anti-tampering: ikinci kayıt yok
                return False

            holdout = self._compute_holdout(full_data)
            if len(holdout) == 0:
                logger.warning(f"Holdout: {symbol} için boş holdout — atlandı")
                return False

            self._holdouts[symbol] = holdout
            meta = self._build_meta(symbol, full_data, holdout)
            self._meta[symbol] = meta
            self._persist_meta()
            logger.info(
                f"Holdout sealed: {symbol} train={len(full_data) - len(holdout)} "
                f"holdout={len(holdout)} fingerprint={meta['fingerprint'][:8]}"
            )
            return True

    def is_registered(self, symbol: str) -> bool:
        return symbol in self._holdouts

    def metadata(self, symbol: str) -> dict[str, Any] | None:
        m = self._meta.get(symbol)
        return dict(m) if m else None

    # ------------------------------------------------------------------
    # Promotion validation — TEK public OOS doğrulayıcı
    # ------------------------------------------------------------------

    def validate_promotion(
        self,
        strategy: Strategy,
        params: dict[str, Any],
        symbol: str,
        market: str,
        in_sample_sharpe: float,
        engine: BacktestEngine,
        context: StrategyContext | None = None,
    ) -> HoldoutVerdict:
        """Holdout veri üzerinde engine.run koş, OOS Sharpe karşılaştır."""
        holdout = self._get_sealed_holdout(symbol)
        if holdout is None or len(holdout) < 10:
            return HoldoutVerdict(
                passed=False,
                oos_sharpe=0.0,
                expected_min=self._expected_min(in_sample_sharpe),
                reason="no_holdout_registered",
            )

        ctx = context or StrategyContext(
            market=market,
            strategy_family=getattr(strategy, "name", "unknown"),
            symbol=symbol,
            timeframe=getattr(context, "timeframe", "1h") if context else "1h",
            params=dict(params),
        )
        # Context.params'ı zorla override et (caller yanlış params verirse koru)
        try:
            ctx.params = dict(params)
        except Exception:
            pass

        try:
            result: ExperimentResult = engine.run(strategy, holdout, ctx)
            oos_sharpe = float(result.sharpe)
        except Exception as e:
            logger.warning(f"Holdout validate engine error {symbol}: {e}")
            return HoldoutVerdict(
                passed=False,
                oos_sharpe=0.0,
                expected_min=self._expected_min(in_sample_sharpe),
                reason=f"engine_error:{type(e).__name__}",
            )

        expected_min = self._expected_min(in_sample_sharpe)
        passed = oos_sharpe >= expected_min and oos_sharpe >= self.oos_floor

        if passed:
            reason = "ok"
        elif oos_sharpe < self.oos_floor:
            reason = f"below_floor({oos_sharpe:.2f}<{self.oos_floor:.2f})"
        else:
            reason = (
                f"oos_below_expected({oos_sharpe:.2f}<{expected_min:.2f}; "
                f"is={in_sample_sharpe:.2f})"
            )

        return HoldoutVerdict(
            passed=passed,
            oos_sharpe=oos_sharpe,
            expected_min=expected_min,
            reason=reason,
        )

    # ------------------------------------------------------------------
    # Private accessor — sadece validate_promotion çağırır
    # ------------------------------------------------------------------

    def _get_sealed_holdout(self, symbol: str) -> pd.DataFrame | None:
        """Mühürlü holdout'u döner. Yalnızca validate_promotion'dan çağrılır.

        Public method değil — caller'ın doğrudan kullanması beklenmez.
        """
        df = self._holdouts.get(symbol)
        if df is None:
            return None
        # Defensive copy — caller mutate ederse mühür bozulmasın
        return df.copy()

    # ------------------------------------------------------------------
    # Meta persistence
    # ------------------------------------------------------------------

    def _expected_min(self, in_sample_sharpe: float) -> float:
        """OOS Sharpe için hedef alt sınır = max(IS * frac, floor)."""
        return max(in_sample_sharpe * self.oos_fraction_of_is, self.oos_floor)

    def _build_meta(
        self,
        symbol: str,
        full_data: pd.DataFrame,
        holdout: pd.DataFrame,
    ) -> dict[str, Any]:
        # Hash: holdout'un kapanış değerleri (anti-tamper kanıtı)
        try:
            close_bytes = holdout.get("close", pd.Series([], dtype=float)).to_numpy().tobytes()
        except Exception:
            close_bytes = b""
        fingerprint = hashlib.sha256(close_bytes).hexdigest()

        first_idx = self._idx_to_str(full_data.index[0]) if len(full_data) else ""
        train_end_pos = len(full_data) - len(holdout)
        holdout_first = self._idx_to_str(holdout.index[0]) if len(holdout) else ""
        holdout_last = self._idx_to_str(holdout.index[-1]) if len(holdout) else ""

        return {
            "symbol": symbol,
            "total_bars": int(len(full_data)),
            "train_bars": int(train_end_pos),
            "holdout_bars": int(len(holdout)),
            "train_first": first_idx,
            "holdout_first": holdout_first,
            "holdout_last": holdout_last,
            "fingerprint": fingerprint,
            "registered_at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _idx_to_str(idx: Any) -> str:
        try:
            if isinstance(idx, pd.Timestamp):
                return idx.isoformat()
            return str(idx)
        except Exception:
            return ""

    def _load_meta(self) -> dict[str, dict[str, Any]]:
        if not self.register_path.exists():
            return {}
        try:
            return json.loads(self.register_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Holdout meta load failed: {e} — yeni register açılıyor")
            return {}

    def _persist_meta(self) -> None:
        try:
            tmp = self.register_path.with_suffix(self.register_path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._meta, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.register_path)
        except Exception as e:
            logger.warning(f"Holdout meta persist failed: {e}")
