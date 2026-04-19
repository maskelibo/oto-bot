"""Tariq TCA — transaction cost analysis ajanı.

Gerçek execution kalitesini ölçer: niyet fiyatı (arrival) vs dolum fiyatı,
market impact, implementation shortfall, fee/latency özeti.

Kurumsal masalarda TCA günlük üretilir ve execution desk'in performansını
ölçer. Bizde paper execution olduğundan bu ajan **model kalitesini** de
izler: eğer TCA kötüyse backtest varsayımları gerçekçi değildir.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from oto_bot.core.models import ExecutionReport


class TariqTCA:
    """TCA ajanı — execution raporları üzerinde metrik türetir."""

    def __init__(
        self,
        acceptable_slippage_bps: float = 15.0,
        impact_alert_bps: float = 25.0,
    ) -> None:
        self.acceptable_slippage_bps = acceptable_slippage_bps
        self.impact_alert_bps = impact_alert_bps

    # ------------------------------------------------------------------

    def build_report(
        self,
        order_id: str,
        symbol: str,
        side: str,
        intended_price: float,
        filled_price: float,
        intended_qty: float,
        filled_qty: float,
        fees_quote: float = 0.0,
        latency_ms: float = 0.0,
        venue: str = "paper",
    ) -> ExecutionReport:
        arrival_slippage_bps = self._bps(filled_price - intended_price, intended_price)
        # Implementation shortfall: eksik dolan pay * paper price move
        shortfall_bps = 0.0
        if intended_qty > 0:
            fill_ratio = filled_qty / intended_qty
            if fill_ratio < 1.0:
                missed_bps = self._bps(filled_price - intended_price, intended_price) * (1 - fill_ratio)
                shortfall_bps = arrival_slippage_bps + missed_bps
            else:
                shortfall_bps = arrival_slippage_bps
        impact_bps = arrival_slippage_bps
        fees_bps = 0.0
        if filled_qty > 0 and filled_price > 0:
            fees_bps = (fees_quote / (filled_qty * filled_price)) * 10_000

        return ExecutionReport(
            order_id=order_id,
            symbol=symbol,
            side=side,
            intended_price=intended_price,
            filled_price=filled_price,
            intended_qty=intended_qty,
            filled_qty=filled_qty,
            arrival_slippage_bps=round(arrival_slippage_bps, 2),
            implementation_shortfall_bps=round(shortfall_bps, 2),
            market_impact_bps=round(impact_bps, 2),
            fees_bps=round(fees_bps, 2),
            latency_ms=round(latency_ms, 2),
            venue=venue,
        )

    # ------------------------------------------------------------------

    def aggregate(self, reports: list[ExecutionReport]) -> dict[str, Any]:
        """Birden fazla raporu tek özet metriğe toparla."""
        if not reports:
            return {"count": 0}
        n = len(reports)
        avg_slip = sum(r.arrival_slippage_bps for r in reports) / n
        avg_shortfall = sum(r.implementation_shortfall_bps for r in reports) / n
        avg_impact = sum(r.market_impact_bps for r in reports) / n
        avg_fees = sum(r.fees_bps for r in reports) / n
        avg_lat = sum(r.latency_ms for r in reports) / n

        alerts: list[str] = []
        if avg_slip > self.acceptable_slippage_bps:
            alerts.append(
                f"average slippage {avg_slip:.1f}bps > accepted {self.acceptable_slippage_bps}bps"
            )
        if avg_impact > self.impact_alert_bps:
            alerts.append(
                f"market impact {avg_impact:.1f}bps over alert threshold {self.impact_alert_bps}bps"
            )

        worst = max(reports, key=lambda r: abs(r.arrival_slippage_bps))

        return {
            "count": n,
            "avg_slippage_bps": round(avg_slip, 2),
            "avg_shortfall_bps": round(avg_shortfall, 2),
            "avg_market_impact_bps": round(avg_impact, 2),
            "avg_fees_bps": round(avg_fees, 2),
            "avg_latency_ms": round(avg_lat, 2),
            "worst_order": asdict(worst),
            "alerts": alerts,
        }

    # ------------------------------------------------------------------

    @staticmethod
    def _bps(delta: float, reference: float) -> float:
        if reference <= 0:
            return 0.0
        return (delta / reference) * 10_000
