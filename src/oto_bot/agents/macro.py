"""Mercury Macro — makro/çapraz varlık overlay ajanı.

Kurumsal masalarda Mercury'nin karşılığı **Macro Strategist** rolüdür.
Her sabah briefing'ten önce şunu sorar: "Bu ortamda kalıcı edge'imiz var mı,
yoksa mikroyapısal gürültüyü alfa olarak mı okuyoruz?"

Bu ajan, stratejiden bağımsız makro bağlamı raporlar — strateji motoru bu
bağlamı bir filtre ya da sermaye dağıtım girdisi olarak kullanır.

Girdi: istenen pazar + veri sağlayıcı (opsiyonel).
Çıktı: MarketContext dataclass.

Analiz ağacı
-----------
1. Kripto tarafında BTC dominansını proxy'le (BTC close / ETH close oranı
   düşerse risk-on altcoin rotasyonu, yükselirse risk-off BTC kaçışı).
2. Çapraz varlık korelasyonu: ETH↔BTC, SPX↔BTC proxy — yüksek korelasyon
   = sistemik risk dalgası. Düşük korelasyon = idiosyncratic sezon.
3. Fear/Greed proxy: son 14 barın getiri skew'ı + ATR genişlemesi birleşimi.
4. Risk-on skoru: yukarıdaki üç sinyalin ağırlıklı toplamı, [-1, +1] ölçekli.
5. Bias etiketi: risk_on | neutral | risk_off | crisis.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from oto_bot.core.models import MarketContext


class MercuryMacro:
    """Makro/rejim üstü overlay sağlayıcı. Strateji motoruna bağlam verir."""

    def __init__(self) -> None:
        pass

    # ------------------------------------------------------------------

    def assess(
        self,
        market: str,
        primary_ohlcv: pd.DataFrame,
        companion_ohlcv: dict[str, pd.DataFrame] | None = None,
        headlines: list[str] | None = None,
    ) -> MarketContext:
        """Makro bağlamı değerlendir.

        Parameters
        ----------
        market:
            "crypto" / "forex" / "us_equities" / "bist" — tek etiket.
        primary_ohlcv:
            Birincil sembol OHLCV (örn. BTC/USDT).
        companion_ohlcv:
            Diğer sembollerin OHLCV'si {"ETH": df, "SPY": df, ...}. Opsiyonel;
            korelasyon ve dominans sinyalleri için kullanılır.
        headlines:
            İsteğe bağlı makro başlıklar listesi.
        """
        headlines = headlines or []
        companion_ohlcv = companion_ohlcv or {}

        # --- Dominans proxy ---
        dominance = self._dominance_proxy(primary_ohlcv, companion_ohlcv)

        # --- DXY/risk-off proxy (yoksa None, kripto için zorunlu değil) ---
        dxy_proxy = companion_ohlcv.get("DXY")
        dxy_value = (
            float(dxy_proxy["close"].iloc[-1]) if dxy_proxy is not None and len(dxy_proxy) else None
        )

        # --- Çapraz korelasyon ---
        cross_corr = self._cross_correlation(primary_ohlcv, companion_ohlcv)

        # --- Fear/greed proxy ---
        fg_proxy = self._fear_greed_proxy(primary_ohlcv)

        # --- Risk-on skoru ---
        # Yüksek fg + düşük korelasyon + düşen dominans = risk-on
        risk_on = 0.0
        if fg_proxy is not None:
            risk_on += fg_proxy * 0.5
        if cross_corr is not None:
            risk_on += (1.0 - min(1.0, abs(cross_corr))) * 0.25
        if dominance is not None:
            risk_on -= (dominance - 0.5) * 0.5  # dominans yüksekse risk-off
        risk_on = max(-1.0, min(1.0, risk_on))

        # --- Bias ---
        if risk_on >= 0.4:
            bias = "risk_on"
            regime_label = "expansion"
        elif risk_on <= -0.5:
            bias = "crisis" if (cross_corr is not None and cross_corr > 0.85) else "risk_off"
            regime_label = "contraction"
        else:
            bias = "neutral"
            regime_label = "transition"

        confidence = round(min(1.0, abs(risk_on) + 0.3), 3)

        return MarketContext(
            market=market,
            regime_label=regime_label,
            risk_on_score=round(risk_on, 3),
            btc_dominance=dominance,
            dxy_proxy=dxy_value,
            cross_correlation=cross_corr,
            fear_greed_proxy=fg_proxy,
            headline_events=headlines[:5],
            bias=bias,
            confidence=confidence,
        )

    # ------------------------------------------------------------------
    # İç yardımcılar
    # ------------------------------------------------------------------

    def _dominance_proxy(
        self,
        primary: pd.DataFrame,
        companions: dict[str, pd.DataFrame],
    ) -> float | None:
        """BTC/ETH oranı gibi basit bir dominans proxy'si. 0-1 aralığına normalize eder."""
        if not companions:
            return None
        ref_key = next(iter(companions.keys()))
        companion = companions[ref_key]
        if len(primary) < 20 or len(companion) < 20:
            return None
        p_ret = primary["close"].pct_change().tail(20).fillna(0.0).sum()
        c_ret = companion["close"].pct_change().tail(20).fillna(0.0).sum()
        spread = p_ret - c_ret
        return round(0.5 + max(-0.5, min(0.5, spread * 5)), 3)

    def _cross_correlation(
        self,
        primary: pd.DataFrame,
        companions: dict[str, pd.DataFrame],
    ) -> float | None:
        if not companions:
            return None
        ref_key = next(iter(companions.keys()))
        comp = companions[ref_key]
        if len(primary) < 30 or len(comp) < 30:
            return None
        a = primary["close"].pct_change().tail(30).fillna(0.0)
        b = comp["close"].pct_change().tail(30).fillna(0.0)
        if len(a) != len(b):
            n = min(len(a), len(b))
            a, b = a.tail(n), b.tail(n)
        corr = float(a.corr(b)) if len(a) > 2 else 0.0
        if corr != corr:  # NaN guard
            return None
        return round(corr, 3)

    def _fear_greed_proxy(self, df: pd.DataFrame) -> float | None:
        if len(df) < 20:
            return None
        returns = df["close"].pct_change().tail(14).fillna(0.0)
        avg_ret = float(returns.mean())
        pos_ratio = float((returns > 0).sum()) / max(len(returns), 1)
        # [-1, +1] aralığına sıkıştır
        score = (avg_ret * 50) + ((pos_ratio - 0.5) * 2)
        return round(max(-1.0, min(1.0, score)), 3)

    # ------------------------------------------------------------------

    def advisory_note(self, context: MarketContext) -> str:
        """İnsan okunabilir kısa makro notu üretir — briefing'e konulabilir."""
        lines = [f"Market={context.market} bias={context.bias} regime={context.regime_label}"]
        if context.risk_on_score is not None:
            lines.append(f"risk_on={context.risk_on_score:+.2f} confidence={context.confidence:.2f}")
        if context.btc_dominance is not None:
            lines.append(f"dominance_proxy={context.btc_dominance:.2f}")
        if context.cross_correlation is not None:
            lines.append(f"cross_corr={context.cross_correlation:+.2f}")
        if context.bias == "crisis":
            lines.append("ACTION: halve gross exposure, halt new strategy promotions.")
        elif context.bias == "risk_off":
            lines.append("ACTION: tighten stops, favor mean-reversion over breakout.")
        elif context.bias == "risk_on":
            lines.append("ACTION: normal sizing allowed; trend/swing edges preferred.")
        return " | ".join(lines)
