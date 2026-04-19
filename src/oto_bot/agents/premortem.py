"""Cassandra Pre-Mortem — dağıtım öncesi başarısızlık senaryoları üretir.

Gary Klein'in "pre-mortem" tekniği: "Stratejiyi 3 ay sonra promote ettiğimizi
ve büyük kayıplar verdiğini hayal et. Kötü giden ne oldu?" diye sor. Bu
ajan mekanik bir şekilde kurumsal failure modlarını tarar:

    1. Overfitting riski (IS vs OOS Sharpe gap)
    2. Regime fragility (tek rejimde kalite)
    3. Small-sample illusion (< 100 trade)
    4. Low-edge trap (fees sonrası PF çok düşük)
    5. Win-rate trap (yüksek WR ama negatif expectancy)
    6. Correlation leak (book'a çok benzer)
    7. Execution naivety (backtest slippage varsayımı gerçekçi değil)
    8. Tail blindness (MC %95 DD çok kötü)
    9. Capacity constraint (trade notional piyasa için büyük)
   10. Data leakage (kararda future bar kullanımı belirtileri)

Çıktı: her risk için flag (PASS / CAUTION / FAIL) ve kısa açıklama.
Risk skoru 0-100 arası; > 70 ise CEO'ya "KIRMIZI BAYRAK" sinyali.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PreMortemReport:
    strategy_family: str
    risk_score: float
    verdict: str
    flags: list[dict[str, Any]] = field(default_factory=list)
    top_risks: list[str] = field(default_factory=list)


class CassandraPreMortem:
    """Strateji promote edilmeden önce "neler kötü gidebilir?" sorusunu kurumsal
    failure taksonomisiyle sistematik olarak tarar."""

    def evaluate(
        self,
        strategy_family: str,
        result: dict[str, Any],
        book_correlation: float | None = None,
        expected_daily_notional: float | None = None,
        market_adv_proxy: float | None = None,
    ) -> PreMortemReport:
        flags: list[dict[str, Any]] = []
        score = 0.0

        def add(name: str, verdict: str, reason: str, weight: float) -> None:
            nonlocal score
            flags.append({"name": name, "verdict": verdict, "reason": reason, "weight": weight})
            if verdict == "FAIL":
                score += weight
            elif verdict == "CAUTION":
                score += weight * 0.5

        # 1. Overfitting
        sharpe = float(result.get("sharpe", 0.0))
        wf_sharpe = result.get("walkforward_sharpe")
        if wf_sharpe is not None and sharpe > 0:
            ratio = wf_sharpe / max(sharpe, 1e-9)
            if ratio < 0.4:
                add("overfitting", "FAIL", f"walk-forward Sharpe only {ratio:.0%} of in-sample", 15)
            elif ratio < 0.7:
                add("overfitting", "CAUTION", f"walk-forward Sharpe {ratio:.0%} of in-sample", 15)
            else:
                add("overfitting", "PASS", f"WF ratio {ratio:.0%}", 0)
        else:
            add("overfitting", "CAUTION", "walk-forward Sharpe not provided", 8)

        # 2. Regime fragility
        regime = result.get("regime", "unknown")
        if regime == "unknown":
            add("regime_coverage", "CAUTION", "no regime label attached", 10)
        else:
            add("regime_coverage", "PASS", f"regime={regime}", 0)

        # 3. Sample size
        trades = int(result.get("total_trades", 0))
        if trades < 30:
            add("sample_size", "FAIL", f"only {trades} trades", 15)
        elif trades < 100:
            add("sample_size", "CAUTION", f"{trades} trades — marginal significance", 10)
        else:
            add("sample_size", "PASS", f"{trades} trades", 0)

        # 4. Low-edge
        pf = float(result.get("profit_factor", 0.0))
        if pf < 1.05:
            add("low_edge", "FAIL", f"PF {pf:.2f} near breakeven", 15)
        elif pf < 1.2:
            add("low_edge", "CAUTION", f"PF {pf:.2f} marginal", 10)
        else:
            add("low_edge", "PASS", f"PF {pf:.2f}", 0)

        # 5. Win-rate trap
        wr = float(result.get("win_rate", 0.0))
        exp = float(result.get("expectancy", 0.0))
        if wr > 0.75 and exp < 0.005:
            add("wr_trap", "FAIL", f"WR {wr:.0%} but expectancy {exp:.4f} — payoff asymmetry", 15)
        elif wr > 0.70 and exp < 0.01:
            add("wr_trap", "CAUTION", f"WR {wr:.0%} / low expectancy — watch for tail losses", 8)
        else:
            add("wr_trap", "PASS", f"WR={wr:.0%}, exp={exp:.4f}", 0)

        # 6. Correlation leak
        if book_correlation is not None:
            if abs(book_correlation) > 0.75:
                add("correlation_leak", "FAIL", f"correlation to book {book_correlation:.2f}", 12)
            elif abs(book_correlation) > 0.5:
                add("correlation_leak", "CAUTION", f"correlation {book_correlation:.2f}", 6)
            else:
                add("correlation_leak", "PASS", f"correlation {book_correlation:.2f}", 0)
        else:
            add("correlation_leak", "CAUTION", "book correlation not measured", 5)

        # 7. Execution naivety
        notes = str(result.get("notes", "")).lower()
        if "no slippage" in notes or "slippage=0" in notes:
            add("execution_naive", "FAIL", "backtest has no slippage assumption", 12)
        else:
            add("execution_naive", "PASS", "slippage model present", 0)

        # 8. Tail blindness
        mc_dd = result.get("montecarlo_95_drawdown")
        if mc_dd is not None and mc_dd < -0.25:
            add("tail_blindness", "FAIL", f"MC 95% DD {mc_dd:.2%}", 15)
        elif mc_dd is None:
            add("tail_blindness", "CAUTION", "no Monte Carlo DD provided", 8)
        else:
            add("tail_blindness", "PASS", f"MC95 DD {mc_dd:.2%}", 0)

        # 9. Capacity
        if expected_daily_notional is not None and market_adv_proxy is not None and market_adv_proxy > 0:
            share = expected_daily_notional / market_adv_proxy
            if share > 0.05:
                add("capacity", "FAIL", f"daily notional {share:.1%} of ADV", 10)
            elif share > 0.01:
                add("capacity", "CAUTION", f"daily notional {share:.1%} of ADV", 5)
            else:
                add("capacity", "PASS", f"{share:.2%} of ADV", 0)
        else:
            add("capacity", "CAUTION", "capacity not assessed", 5)

        # 10. Data leakage — naif heuristik
        if "lookahead" in notes or "future" in notes:
            add("data_leakage", "FAIL", "notes mention look-ahead/future reference", 20)
        else:
            add("data_leakage", "PASS", "no leakage markers in notes", 0)

        score = min(100.0, score)
        if score >= 70:
            verdict = "RED — significant failure modes; do not promote"
        elif score >= 40:
            verdict = "AMBER — address flagged items before promotion"
        else:
            verdict = "GREEN — acceptable pre-mortem risk"

        top = sorted(
            [f for f in flags if f["verdict"] != "PASS"],
            key=lambda f: f["weight"],
            reverse=True,
        )[:3]

        return PreMortemReport(
            strategy_family=strategy_family,
            risk_score=round(score, 1),
            verdict=verdict,
            flags=flags,
            top_risks=[f"{f['name']}: {f['reason']}" for f in top],
        )
