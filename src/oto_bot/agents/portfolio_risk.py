"""Apex Portfolio Risk — bağımsız kitap seviyesi risk yöneticisi.

Citadel'in Portfolio Construction & Risk Group modelinde olduğu gibi Apex
**stratejilerden bağımsız** çalışır ve yalnızca CEO'ya raporlar. Stratejiyi
kim yazdıysa yazsın, Apex veto ederse pozisyon açılmaz.

Sorumlulukları:
    - Kitap seviyesi VaR 95% (parametrik + tarihsel)
    - Expected Shortfall (CVaR) 95%
    - Gross / Net / Delta exposure hesabı
    - Pod-pod korelasyon matrisi
    - Konsantrasyon ölçümü (tek pod > max_concentration_pct)
    - Portföy drawdown / circuit breaker onaylama

Not: Bu ajan tek başına pozisyon kapatma emri vermez; CEO ve RiskGate ile
birlikte çalışır. Raporladığı risk_score seviyeleri:
    green (< 0.5)      → normal işlem
    amber (0.5-0.75)   → yeni risk eklenmez, mevcut pozisyon korunur
    red   (> 0.75)     → aktif risk azaltımı önerilir
    black (> 1.0 veya kill-switch) → acil halt
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from oto_bot.core.models import PodState, PortfolioSnapshot


@dataclass
class RiskVerdict:
    status: str
    score: float
    breaches: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)


class ApexPortfolioRisk:
    """Kitap seviyesi bağımsız risk kontrol ünitesi."""

    def __init__(
        self,
        max_concentration_pct: float = 0.20,
        max_correlation: float = 0.75,
        max_gross_leverage: float = 3.0,
        var_limit_pct: float = 0.04,
        es_limit_pct: float = 0.06,
        drawdown_halt_pct: float = 0.10,
    ) -> None:
        self.max_concentration_pct = max_concentration_pct
        self.max_correlation = max_correlation
        self.max_gross_leverage = max_gross_leverage
        self.var_limit_pct = var_limit_pct
        self.es_limit_pct = es_limit_pct
        self.drawdown_halt_pct = drawdown_halt_pct

    # ------------------------------------------------------------------
    # VaR / ES hesapları
    # ------------------------------------------------------------------

    @staticmethod
    def parametric_var(returns: list[float], confidence: float = 0.95) -> float:
        """Z-skor tabanlı parametrik VaR. Getiri serisinin mutlak değerini döner."""
        if not returns:
            return 0.0
        mu = sum(returns) / len(returns)
        var_sq = sum((r - mu) ** 2 for r in returns) / max(len(returns) - 1, 1)
        sigma = math.sqrt(var_sq)
        # 95% tek yönlü Z = 1.645, 99% = 2.326
        z = 1.645 if confidence < 0.975 else 2.326
        return round(abs(mu - z * sigma), 6)

    @staticmethod
    def historical_var(returns: list[float], confidence: float = 0.95) -> float:
        if not returns:
            return 0.0
        sorted_rets = sorted(returns)
        idx = int((1 - confidence) * len(sorted_rets))
        idx = max(0, min(idx, len(sorted_rets) - 1))
        return round(abs(sorted_rets[idx]), 6)

    @staticmethod
    def expected_shortfall(returns: list[float], confidence: float = 0.95) -> float:
        if not returns:
            return 0.0
        sorted_rets = sorted(returns)
        cut = int((1 - confidence) * len(sorted_rets))
        tail = sorted_rets[: max(cut, 1)]
        return round(abs(sum(tail) / len(tail)), 6)

    # ------------------------------------------------------------------
    # Korelasyon & konsantrasyon
    # ------------------------------------------------------------------

    @staticmethod
    def correlation_matrix(pod_returns: dict[str, list[float]]) -> dict[str, dict[str, float]]:
        """Pod bazlı getiri serilerinden pearson korelasyon matrisi."""
        keys = list(pod_returns.keys())
        matrix: dict[str, dict[str, float]] = {k: {} for k in keys}
        for i, a in enumerate(keys):
            for b in keys[i:]:
                ra, rb = pod_returns[a], pod_returns[b]
                n = min(len(ra), len(rb))
                if n < 5:
                    corr = 0.0
                else:
                    ra2, rb2 = ra[-n:], rb[-n:]
                    mu_a = sum(ra2) / n
                    mu_b = sum(rb2) / n
                    cov = sum((ra2[k] - mu_a) * (rb2[k] - mu_b) for k in range(n)) / n
                    va = sum((ra2[k] - mu_a) ** 2 for k in range(n)) / n
                    vb = sum((rb2[k] - mu_b) ** 2 for k in range(n)) / n
                    denom = math.sqrt(va * vb) or 1e-9
                    corr = cov / denom
                matrix[a][b] = round(corr, 3)
                matrix[b][a] = round(corr, 3)
        return matrix

    @staticmethod
    def concentration(pods: list[PodState]) -> float:
        """En büyük pod'un toplam sermayeye oranı (Herfindahl yerine basit max)."""
        total = sum(p.current_capital for p in pods) or 1e-9
        if not pods:
            return 0.0
        return round(max(p.current_capital for p in pods) / total, 3)

    # ------------------------------------------------------------------
    # Ana değerlendirme
    # ------------------------------------------------------------------

    def assess(
        self,
        pods: list[PodState],
        book_returns: list[float],
        pod_returns: dict[str, list[float]],
        total_capital: float,
        gross_exposure: float,
        net_exposure: float,
        peak_capital: float,
        daily_pnl: float,
        weekly_pnl: float,
        mtd_pnl: float,
    ) -> tuple[PortfolioSnapshot, RiskVerdict]:
        var95 = self.historical_var(book_returns, 0.95)
        es95 = self.expected_shortfall(book_returns, 0.95)
        conc = self.concentration(pods)
        corr_matrix = self.correlation_matrix(pod_returns)
        max_corr = 0.0
        keys = list(corr_matrix.keys())
        for i, a in enumerate(keys):
            for b in keys[i + 1 :]:
                max_corr = max(max_corr, abs(corr_matrix[a][b]))

        snapshot = PortfolioSnapshot(
            total_capital=total_capital,
            deployed_capital=sum(p.current_capital for p in pods if p.status == "active"),
            num_active_pods=sum(1 for p in pods if p.status == "active"),
            num_positions=0,
            gross_exposure=gross_exposure,
            net_exposure=net_exposure,
            max_pod_concentration=conc,
            correlation_max=round(max_corr, 3),
            var_95=var95,
            expected_shortfall_95=es95,
            daily_pnl=daily_pnl,
            weekly_pnl=weekly_pnl,
            month_to_date_pnl=mtd_pnl,
        )

        breaches: list[str] = []
        recs: list[str] = []
        score = 0.0

        if conc > self.max_concentration_pct:
            breaches.append(
                f"concentration {conc:.0%} exceeds {self.max_concentration_pct:.0%}"
            )
            recs.append("Reduce top pod allocation or allocate to uncorrelated pod.")
            score += 0.3

        if max_corr > self.max_correlation:
            breaches.append(
                f"pod correlation peak {max_corr:.2f} exceeds {self.max_correlation:.2f}"
            )
            recs.append("Halve size on most correlated pair; seek orthogonal alpha.")
            score += 0.25

        leverage = gross_exposure / max(total_capital, 1e-9)
        if leverage > self.max_gross_leverage:
            breaches.append(
                f"gross leverage {leverage:.2f}x exceeds {self.max_gross_leverage:.2f}x"
            )
            recs.append("Cut gross exposure to target leverage.")
            score += 0.4

        if var95 > self.var_limit_pct:
            breaches.append(f"VaR95 {var95:.2%} above limit {self.var_limit_pct:.2%}")
            recs.append("Hedge tail risk or reduce gross.")
            score += 0.25

        if es95 > self.es_limit_pct:
            breaches.append(f"ES95 {es95:.2%} above limit {self.es_limit_pct:.2%}")
            recs.append("Tail PnL severe — rerun stress scenarios.")
            score += 0.25

        current_dd = 0.0
        if peak_capital > 0:
            current_dd = (total_capital - peak_capital) / peak_capital
        if current_dd < -abs(self.drawdown_halt_pct):
            breaches.append(
                f"book drawdown {current_dd:.2%} breaches {-self.drawdown_halt_pct:.2%}"
            )
            recs.append("KILL SWITCH: halt new entries, liquidate riskiest pod.")
            score += 1.0

        if score < 0.5:
            status = "green"
        elif score < 0.75:
            status = "amber"
        elif score < 1.0:
            status = "red"
        else:
            status = "black"

        verdict = RiskVerdict(
            status=status,
            score=round(score, 3),
            breaches=breaches,
            recommendations=recs,
        )
        return snapshot, verdict
