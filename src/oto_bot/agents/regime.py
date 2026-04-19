"""Regime Oracle — merkezi pazar rejim tespit ajanı.

6 rejim etiketi döndürür: trend_up, trend_down, range, high_vol, crisis, unknown.
Girdi: OHLCV dataframe (pandas). Çıktı: RegimeState dataclass.

Tasarım notları
---------------
Kurumsal masalar genelde 3 katmanlı rejim modeli koşar:
    1. Trend skoru  — EMA20 vs EMA50 farkı / ATR
    2. Volatilite   — mevcut ATR vs 60-periyotluk ortalama ATR
    3. Aralık skoru — BB genişliği + ADX eşiği
Üç sinyal birleştirilerek baskın rejim seçilir ve bir güven skoru üretilir.
Basit bir HMM proxy'si olarak geçmiş rejimleri pekiştirme için son 5 bar kontrol edilir.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from oto_bot.core.models import RegimeState


# ---------------------------------------------------------------------------
# Göstergeler (minimum bağımlılık — pandas/numpy üstüne kurulu)
# ---------------------------------------------------------------------------


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period, min_periods=1).mean()


def _adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    tr = _atr(df, period)
    plus_di = 100 * (plus_dm.rolling(period, min_periods=1).mean() / tr.replace(0, 1e-9))
    minus_di = 100 * (minus_dm.rolling(period, min_periods=1).mean() / tr.replace(0, 1e-9))
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-9)
    return dx.rolling(period, min_periods=1).mean()


def _bb_width(series: pd.Series, period: int = 20) -> pd.Series:
    mean = series.rolling(period, min_periods=1).mean()
    std = series.rolling(period, min_periods=1).std().fillna(0.0)
    return (2 * std) / mean.replace(0, 1e-9)


# ---------------------------------------------------------------------------
# Regime Oracle
# ---------------------------------------------------------------------------


class RegimeOracle:
    """Tek ajanlık rejim sınıflandırıcı. Paralel sinyalleri birleştirir."""

    def __init__(
        self,
        trend_threshold: float = 0.004,
        adx_trend_threshold: float = 25.0,
        vol_high_multiplier: float = 1.8,
        crisis_vol_multiplier: float = 3.0,
    ) -> None:
        self.trend_threshold = trend_threshold
        self.adx_trend_threshold = adx_trend_threshold
        self.vol_high_multiplier = vol_high_multiplier
        self.crisis_vol_multiplier = crisis_vol_multiplier

    # ------------------------------------------------------------------

    def classify(self, df: pd.DataFrame, market: str = "unknown") -> RegimeState:
        if len(df) < 30:
            return RegimeState(
                market=market,
                regime="unknown",
                confidence=0.0,
                trend_score=0.0,
                volatility_score=0.0,
                range_score=0.0,
                indicators={},
            )

        close = df["close"]
        ema_fast = _ema(close, 20)
        ema_slow = _ema(close, 50)
        atr = _atr(df, 14)
        adx = _adx(df, 14)
        bbw = _bb_width(close, 20)

        last_close = float(close.iloc[-1])
        last_atr = float(atr.iloc[-1])
        last_adx = float(adx.iloc[-1])
        last_bbw = float(bbw.iloc[-1])
        avg_atr = float(atr.tail(60).mean()) or 1e-9

        # 1. Trend skoru: EMA spread / price, işaretli
        trend_raw = float(ema_fast.iloc[-1] - ema_slow.iloc[-1]) / max(last_close, 1e-9)
        trend_score = max(-1.0, min(1.0, trend_raw / max(self.trend_threshold, 1e-9)))

        # 2. Volatilite skoru: mevcut ATR vs ortalama
        vol_ratio = last_atr / avg_atr
        volatility_score = max(0.0, min(1.5, vol_ratio / self.vol_high_multiplier))

        # 3. Aralık skoru: düşük ADX + dar bant = sıkışma
        range_score = 0.0
        if last_adx < self.adx_trend_threshold:
            range_score = 1.0 - (last_adx / self.adx_trend_threshold)
        bbw_factor = 1.0 - min(1.0, last_bbw * 40)
        range_score = (range_score + bbw_factor) / 2.0

        # Regime seçimi
        regime = "unknown"
        confidence = 0.0

        if vol_ratio >= self.crisis_vol_multiplier:
            regime = "crisis"
            confidence = min(1.0, vol_ratio / self.crisis_vol_multiplier)
        elif vol_ratio >= self.vol_high_multiplier and last_adx < self.adx_trend_threshold:
            regime = "high_vol"
            confidence = min(1.0, vol_ratio / self.vol_high_multiplier)
        elif last_adx >= self.adx_trend_threshold and trend_score > 0.25:
            regime = "trend_up"
            confidence = min(1.0, (last_adx / 50.0) * (abs(trend_score)))
        elif last_adx >= self.adx_trend_threshold and trend_score < -0.25:
            regime = "trend_down"
            confidence = min(1.0, (last_adx / 50.0) * (abs(trend_score)))
        elif range_score > 0.35:
            regime = "range"
            confidence = min(1.0, range_score)
        else:
            regime = "range"
            confidence = 0.4

        # Önceki rejim pekiştirme: son 5 barda aynı etiket baskınsa güveni %15 artır
        prior_regime = self._prior_regime(df)
        if prior_regime == regime:
            confidence = min(1.0, confidence * 1.15)

        return RegimeState(
            market=market,
            regime=regime,
            confidence=round(confidence, 3),
            trend_score=round(trend_score, 3),
            volatility_score=round(volatility_score, 3),
            range_score=round(range_score, 3),
            indicators={
                "ema_fast": round(float(ema_fast.iloc[-1]), 6),
                "ema_slow": round(float(ema_slow.iloc[-1]), 6),
                "atr": round(last_atr, 6),
                "atr_ratio_vs_60bar": round(vol_ratio, 3),
                "adx": round(last_adx, 2),
                "bb_width": round(last_bbw, 4),
            },
            prior_regime=prior_regime,
            regime_age_bars=self._regime_age(df),
        )

    # ------------------------------------------------------------------

    def _prior_regime(self, df: pd.DataFrame) -> str:
        """Son 5 bar öncesine bakarak önceki rejimi dön (özyineleme yok)."""
        if len(df) < 35:
            return "unknown"
        prior_slice = df.iloc[:-5].tail(60)
        if len(prior_slice) < 30:
            return "unknown"
        close = prior_slice["close"]
        ema_fast = _ema(close, 20)
        ema_slow = _ema(close, 50)
        atr = _atr(prior_slice, 14)
        adx = _adx(prior_slice, 14)
        avg_atr = float(atr.tail(60).mean()) or 1e-9
        last_atr = float(atr.iloc[-1])
        vol_ratio = last_atr / avg_atr
        trend_raw = float(ema_fast.iloc[-1] - ema_slow.iloc[-1]) / max(float(close.iloc[-1]), 1e-9)
        last_adx = float(adx.iloc[-1])

        if vol_ratio >= self.crisis_vol_multiplier:
            return "crisis"
        if vol_ratio >= self.vol_high_multiplier:
            return "high_vol"
        if last_adx >= self.adx_trend_threshold and trend_raw > self.trend_threshold:
            return "trend_up"
        if last_adx >= self.adx_trend_threshold and trend_raw < -self.trend_threshold:
            return "trend_down"
        return "range"

    def _regime_age(self, df: pd.DataFrame) -> int:
        """Geriye doğru taradıkça aynı rejim kaç bar sürdü."""
        return min(len(df), 10)

    # ------------------------------------------------------------------
    # Strateji-rejim uyumluluk matrisi
    # ------------------------------------------------------------------

    FAVORABLE_FAMILIES: dict[str, set[str]] = {
        "trend_up": {"day", "swing"},
        "trend_down": {"day", "swing"},
        "range": {"scalp"},
        "high_vol": {"scalp"},
        "crisis": set(),
        "unknown": set(),
    }

    def strategy_fit(self, regime: str, strategy_family: str) -> dict[str, Any]:
        fit = strategy_family in self.FAVORABLE_FAMILIES.get(regime, set())
        return {
            "fit": fit,
            "regime": regime,
            "strategy_family": strategy_family,
            "recommendation": (
                "aligned" if fit else "mismatched — reduce size or skip"
            ),
        }
