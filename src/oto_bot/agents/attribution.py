"""PnL Attribution — trade serisini sürücü bileşenlere ayırır.

Kurumsal masalar her seansın sonunda P&L attribution çalıştırır:
    "Bugün $X kazandık; bunun $A'sı alpha, $B'si beta, $C'si slippage idi."
Bu tür ayrışma olmadan edge bozulsa bile fark edilmez.

Bu ajan:
    - Trade listesinden (dict listesi) toplam PnL'i hesaplar.
    - Sinyal, sembol, rejim, saat bazında dağılım çıkarır.
    - Alpha vs beta: beta kabaca "pazar yönüyle aynı hareketten gelen" kâr
      (directional bias * market return), kalanı alpha.
    - Slippage ve fees (trade dict'lerinde anahtar varsa) ayıklanır.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from oto_bot.core.models import PnLAttribution


class PnLAttributor:
    """Trade listesinden detaylı PnL ayrışması üretir."""

    def attribute(
        self,
        experiment_id: str,
        trades: list[dict[str, Any]],
        market_return_pct: float = 0.0,
    ) -> PnLAttribution:
        """Trade'leri ayrıştır.

        Beklenen trade dict anahtarları (hepsi opsiyonel, olmayanlar 0):
            pnl         : net P&L
            signal      : "bb_band" / "rsi" / "breakout" vs.
            symbol      : "BTC/USDT" vs.
            regime      : "trend_up" / "range" vs.
            hour        : 0-23
            direction   : "long" / "short"
            fees        : işlem ücreti
            slippage    : piyasa etkisi
            funding     : perp funding ödemesi
            notional    : pozisyon büyüklüğü
        """
        total = 0.0
        by_signal: dict[str, float] = defaultdict(float)
        by_symbol: dict[str, float] = defaultdict(float)
        by_regime: dict[str, float] = defaultdict(float)
        by_hour: dict[str, float] = defaultdict(float)
        fees_sum = 0.0
        slip_sum = 0.0
        funding_sum = 0.0

        directional_pnl = 0.0
        directional_notional = 0.0

        for t in trades:
            pnl = float(t.get("pnl", 0.0))
            total += pnl
            by_signal[t.get("signal", "unknown")] += pnl
            by_symbol[t.get("symbol", "unknown")] += pnl
            by_regime[t.get("regime", "unknown")] += pnl
            h = t.get("hour")
            if h is not None:
                by_hour[str(int(h))] += pnl
            fees_sum += float(t.get("fees", 0.0))
            slip_sum += float(t.get("slippage", 0.0))
            funding_sum += float(t.get("funding", 0.0))

            # Beta proxy
            notional = float(t.get("notional", 0.0))
            direction = t.get("direction", "long")
            if notional > 0:
                sign = 1.0 if direction == "long" else -1.0
                directional_pnl += sign * notional * market_return_pct
                directional_notional += notional

        beta_pnl = directional_pnl
        alpha_pnl = total - beta_pnl
        net_edge = total - fees_sum - slip_sum - funding_sum

        return PnLAttribution(
            experiment_id=experiment_id,
            total_pnl=round(total, 6),
            by_signal={k: round(v, 6) for k, v in by_signal.items()},
            by_symbol={k: round(v, 6) for k, v in by_symbol.items()},
            by_regime={k: round(v, 6) for k, v in by_regime.items()},
            by_hour={k: round(v, 6) for k, v in by_hour.items()},
            alpha_pnl=round(alpha_pnl, 6),
            beta_pnl=round(beta_pnl, 6),
            fees_paid=round(fees_sum, 6),
            slippage_paid=round(slip_sum, 6),
            funding_paid=round(funding_sum, 6),
            net_edge_after_costs=round(net_edge, 6),
        )

    # ------------------------------------------------------------------

    @staticmethod
    def narrative(attribution: PnLAttribution) -> str:
        """Attribution dataclass'ını tek cümlelik yorum olarak döndürür."""
        top_signals = sorted(attribution.by_signal.items(), key=lambda kv: kv[1], reverse=True)[:3]
        worst_signals = sorted(attribution.by_signal.items(), key=lambda kv: kv[1])[:2]
        signals_str = ", ".join(f"{s}={v:+.2f}" for s, v in top_signals)
        losers_str = ", ".join(f"{s}={v:+.2f}" for s, v in worst_signals)
        return (
            f"Net PnL={attribution.total_pnl:+.2f} "
            f"(alpha={attribution.alpha_pnl:+.2f}, beta={attribution.beta_pnl:+.2f}); "
            f"costs fees={attribution.fees_paid:.2f} slip={attribution.slippage_paid:.2f} "
            f"fund={attribution.funding_paid:.2f}. "
            f"Top signals: {signals_str}. Worst: {losers_str}."
        )
