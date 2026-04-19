"""InsightExtractor — cycle sonuçlarını kompakt derslere çevirir.

Her cycle bitince raw metrikler + debate + CEO kararı elimizde. Ama 1000
cycle sonra bunları tek tek okumak token israfı. Çözüm: her cycle'dan
**1-2 insight** çıkar, tag'le, journal'a yaz. Gelecekte ajan sadece bu
derslerin TOP-K'sını okur.

Kurallı + deterministik çıkarım. Hiçbir LLM çağrısı yok.

Örnek çıktılar:
    "[high] range rejiminde scalp crypto BTCUSDT: WR 0.62 ama PF 1.05 → fee sonrası edge yok"
    "[critical] trend_up day forex EURUSD: consecutive_losses 7, pre-mortem veto"
    "[info] swing us_equities MSFT: Sharpe 1.82, promote için sample büyütülmeli (42 trade)"
"""

from __future__ import annotations

from typing import Any

from oto_bot.core.models import ExperimentResult, Lesson
from oto_bot.memory.journal import LearningJournal


class InsightExtractor:
    """Kurallı distillation — ajan-özel lesson üretir."""

    def __init__(self, journal: LearningJournal | None = None) -> None:
        self.journal = journal or LearningJournal()

    # ------------------------------------------------------------------

    def extract_from_cycle(
        self,
        result: ExperimentResult,
        decision: dict[str, Any] | None = None,
        debate_conclusion: str | None = None,
        cycle_number: int = 0,
    ) -> list[Lesson]:
        """Raw cycle sonucundan 0-3 kompakt ders üret.

        Tekrar aynı shape'teki dersleri sürekli yazmaktan kaçınmak için
        journal persistance katmanı *INSERT OR REPLACE* değil, her dersi
        ayrı tutar; prune aşamasında referans az olanlar silinir.
        """
        lessons: list[Lesson] = []

        sharpe = result.sharpe
        pf = result.profit_factor
        dd = result.max_drawdown
        wr = result.win_rate
        trades = result.total_trades
        regime = getattr(result, "regime", "unknown") or "unknown"
        market = result.market
        strategy = result.strategy_family
        exp_id = result.experiment_id
        symbol = self._guess_symbol(result)

        # --- Kural 1: promote edildi + Sharpe yüksek ---
        if result.promoted and sharpe >= 1.5:
            severity = "high" if sharpe >= 2.0 else "medium"
            content = (
                f"{regime} rejiminde {strategy} {market}/{symbol}: "
                f"Sharpe {sharpe:.2f}, PF {pf:.2f}, DD {dd:.1%}, {trades} trade — "
                f"promote edildi, bu parametre alanı güvenilir."
            )
            lessons.append(self._mk(
                "Pulse Analytics", content, severity,
                market, strategy, regime, symbol, exp_id, cycle_number,
                extra_tags=["outcome:promoted", f"sharpe_bucket:{self._sharpe_bucket(sharpe)}"],
            ))

        # --- Kural 2: Low edge trap (yüksek WR, düşük PF) ---
        if trades >= 30 and wr > 0.55 and pf < 1.1:
            content = (
                f"{regime} rejiminde {strategy} {market}/{symbol}: "
                f"WR {wr:.0%} yüksek AMA PF {pf:.2f} — fee/slippage sonrası edge erir. "
                f"Bu kombinasyonu promote etme."
            )
            lessons.append(self._mk(
                "Sigma Quant", content, "high",
                market, strategy, regime, symbol, exp_id, cycle_number,
                extra_tags=["outcome:wr_trap"],
            ))

        # --- Kural 3: Severe drawdown ---
        if dd < -0.15:
            content = (
                f"{regime} rejiminde {strategy} {market}/{symbol}: "
                f"Max DD {dd:.1%} — hedef (-%15) aşıldı. Bu param set'i riskli."
            )
            lessons.append(self._mk(
                "Sentinel Risk", content, "critical",
                market, strategy, regime, symbol, exp_id, cycle_number,
                extra_tags=["outcome:dd_breach"],
            ))

        # --- Kural 4: Regime mismatch ---
        if regime != "unknown" and result.promoted is False and sharpe < 0:
            fit_map = {
                "scalp": {"range", "high_vol"},
                "day": {"trend_up", "trend_down"},
                "swing": {"trend_up", "trend_down"},
            }
            favorable = fit_map.get(strategy, set())
            if regime not in favorable and favorable:
                content = (
                    f"{regime} rejiminde {strategy} uygun değil (Sharpe {sharpe:.2f}). "
                    f"Favor rejim: {'/'.join(sorted(favorable))}. "
                    f"Gelecekte {strategy} için regime filter et."
                )
                lessons.append(self._mk(
                    "Regime Oracle", content, "high",
                    market, strategy, regime, symbol, exp_id, cycle_number,
                    extra_tags=["outcome:regime_mismatch"],
                ))

        # --- Kural 5: Insufficient sample ---
        if trades < 30 and result.promoted is False:
            content = (
                f"{strategy} {market}/{symbol}: sadece {trades} trade — "
                "örneklem küçük, güvenilir karar için uzun veri penceresi şart."
            )
            lessons.append(self._mk(
                "Sigma Quant", content, "info",
                market, strategy, regime, symbol, exp_id, cycle_number,
                extra_tags=["outcome:small_sample"],
            ))

        # --- Kural 6: Debate veto ---
        if debate_conclusion and debate_conclusion.startswith("REJECT"):
            content = (
                f"{strategy} {market}/{symbol} ({regime}): panel REJECT. "
                f"Sharpe {sharpe:.2f}, PF {pf:.2f}, DD {dd:.1%}. "
                f"Benzer pattern'i tekrar test etmeden düzelt."
            )
            lessons.append(self._mk(
                "Atlas CEO", content, "high",
                market, strategy, regime, symbol, exp_id, cycle_number,
                extra_tags=["outcome:panel_reject"],
            ))

        # Kaydet
        for lesson in lessons:
            self.journal.save(lesson)

        return lessons

    # ------------------------------------------------------------------

    @staticmethod
    def _mk(
        author: str,
        content: str,
        severity: str,
        market: str,
        strategy: str,
        regime: str,
        symbol: str,
        exp_id: str,
        cycle: int,
        extra_tags: list[str] | None = None,
    ) -> Lesson:
        tags = [f"market:{market}", f"strategy:{strategy}", f"regime:{regime}", f"symbol:{symbol}"]
        if extra_tags:
            tags.extend(extra_tags)
        return Lesson(
            lesson_id="",  # journal.save uuid atar
            author_agent=author,
            content=content,
            tags=tags,
            market=market,
            strategy_family=strategy,
            regime=regime,
            symbol=symbol,
            severity=severity,
            evidence_experiment_id=exp_id,
            source_cycle=cycle,
        )

    @staticmethod
    def _sharpe_bucket(sharpe: float) -> str:
        if sharpe >= 2.5:
            return "elite"
        if sharpe >= 1.5:
            return "good"
        if sharpe >= 0.5:
            return "acceptable"
        return "poor"

    @staticmethod
    def _guess_symbol(result: ExperimentResult) -> str:
        """ExperimentResult'da symbol alanı yok — title'dan tahmin et."""
        title = (result.hypothesis_title or "").upper()
        # crypto: BTCUSDT, ETHUSDT gibi; forex: EURUSD gibi; equity: AAPL
        for sym in ("BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT","ADAUSDT",
                    "EURUSD","GBPUSD","USDJPY","AUDUSD","USDCHF",
                    "AAPL","MSFT","GOOGL","AMZN","NVDA","TSLA","META"):
            if sym in title:
                return sym
        return "*"
