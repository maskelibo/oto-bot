"""Faz 6 — Slot-bazlı orchestrator.

Felsefe değişimi
----------------
Eski orchestrator her cycle yeni bir ``(strategy_family, market, params)``
kombinasyonu deniyordu → binlerce promotion satırı, niceliğe yatırım
yapıldı niteliğe değil. Yeni mimari:

* En fazla **5 slot** (``BotRegistry.MAX_SLOTS``). Her slot bir bot
  kişiliği = ``(strategy_family, market)``.
* Her cycle **bir slot seçilir** ve onun mevcut ``current_params`` içine
  Bayesian'dan iyileştirme önerisi getirilir; basket backtest + holdout
  doğrulaması yapılır; daha iyi ise slot params güncellenir.
* Promote = yeni satır DEĞİL, slot'un alanlarının UPDATE edilmesi.
* 50 iterasyon sonrası Sharpe < 1.0 olan slot retire olur ve yeniden
  seed edilebilir.

Kaldırılan eski akışlar (Faz 1-5 izleri)
----------------------------------------
- Random combo seçici (`_pick_next_experiment`) — slot seçimi onun yerini aldı.
- RobustnessQueue + drain burst — slot iteration zaten varyant deniyor.
- Promotion-bazlı pod create / correlation veto / champion-challenger
  rotasyon (`_review_pods`) — slot içi karar tek nokta.
- HoldoutGuardian.validate_promotion (per-symbol verdict) — basket OOS
  re-run yeterli.
- `_check_winner` (winners.jsonl) — slot lifetime metrikleri zaten registry'de.
- StressLab + walk-forward + Monte Carlo — slot iteration zaten 4+ yıl
  veride basket koşuyor; ek katman gereksiz.

Bu dosyada ChampionChallenger / RollingPerformanceMonitor /
CorrelationMonitor import'ları geriye dönük uyumluluk ve manuel
testler için BIRAKILDI ama otomatik akıştan ÇIKARILDI.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from oto_bot.agents.bayesian_optimizer import BayesianOptimizer, STRATEGY_PARAM_SPACE
from oto_bot.agents.bot_registry import BotRegistry, BotSlot
from oto_bot.agents.ceo import CEOAgent
from oto_bot.agents.educator_loop import EducatorLoop
from oto_bot.agents.holdout_guardian import HoldoutGuardian
from oto_bot.agents.pod_allocator import PodAllocator
from oto_bot.agents.proposals import ProposalQueue
from oto_bot.agents.registry import AgentRegistry
from oto_bot.backtest.basket import BasketBacktester, BasketResult
from oto_bot.backtest.engine import BacktestEngine
from oto_bot.core.models import ExperimentResult, Hypothesis
from oto_bot.experiments.ledger import ExperimentLedger
from oto_bot.governance.risk import RiskGate
from oto_bot.memory.curve import LearningCurve
from oto_bot.memory.insight import InsightExtractor
from oto_bot.memory.journal import LearningJournal
from oto_bot.memory.manager import MemoryManager
from oto_bot.memory.retriever import MemoryRetriever, RetrievalContext
from oto_bot.strategies.base import Strategy, StrategyContext
from oto_bot.strategies.day_trader import DayTraderStrategy
from oto_bot.strategies.scalper import ScalperStrategy
from oto_bot.strategies.swing_trader import SwingTraderStrategy
from oto_bot.utils.data import make_synthetic_ohlc

console = Console()
logger = logging.getLogger("oto_bot.orchestrator")


# ---------------------------------------------------------------------------
# Market & strategy universe
# ---------------------------------------------------------------------------

MARKET_SYMBOLS: dict[str, list[str]] = {
    "crypto": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"],
    "forex": ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF"],
    "us_equities": ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "TSLA", "META"],
    "bist": ["THYAO", "GARAN", "ASELS", "SISE", "EREGL", "KCHOL"],
}

# Strateji ailesi başına tercih edilen timeframe — slot iyileştirme cycle'ı
# tek bir tf üzerinden koşar (basket çoklu sembol, tek tf).
SLOT_TIMEFRAME: dict[str, str] = {
    "day": "1h",
    "swing": "1d",
    "scalp": "15m",
}

STRATEGY_FACTORIES: dict[str, type[Strategy]] = {
    "day": DayTraderStrategy,
    "swing": SwingTraderStrategy,
    "scalp": ScalperStrategy,
}


# Min bar eşikleri — 4 yıl (252 trading days/yr) baz alınır.
# Eğer dönen DataFrame bu eşikten az ise slot skip edilir.
MIN_BARS_PER_TF: dict[str, int] = {
    "1d": 4 * 252,
    "4h": 4 * 252 * 6,
    "1h": 4 * 252 * 24,
    "15m": 4 * 252 * 24 * 4,
    "5m": 4 * 252 * 24 * 12,
}


# Veri penceresi — Faz 6 zorunluluğu.
DATA_WINDOW_START: str = "2022-01-01"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Portfolio state tracker (eski mimariden korundu)
# ---------------------------------------------------------------------------

@dataclass
class PortfolioState:
    """Slot bazlı sermaye dağılımı: ``total_capital / MAX_SLOTS`` her slota.

    Compound bug'lı eski ``rebalance()`` yok; her slotun sermayesi sabit
    kalır. P&L tracking ayrı bir kanal (``daily/weekly/monthly_pnl``)
    üzerinden yapılır.
    """

    initial_capital: float = 100_000.0
    total_capital: float = 100_000.0
    unrealized_pnl: float = 0.0
    promoted_strategies: list[dict[str, Any]] = field(default_factory=list)
    daily_pnl: float = 0.0
    weekly_pnl: float = 0.0
    monthly_pnl: float = 0.0
    last_daily_reset: str = ""
    last_weekly_reset: str = ""
    last_monthly_reset: str = ""

    @property
    def risk_utilization_pct(self) -> float:
        if not self.promoted_strategies or self.total_capital <= 0:
            return 0.0
        total_risk = sum(s.get("risk_pct", 0.0) for s in self.promoted_strategies)
        return min(total_risk, 1.0)

    @property
    def effective_capital(self) -> float:
        return self.total_capital + self.unrealized_pnl

    def update_pnl_tracking(self, cycle_pnl: float) -> None:
        now = _now()
        today = now.strftime("%Y-%m-%d")
        week = now.strftime("%Y-W%W")
        month = now.strftime("%Y-%m")
        if self.last_daily_reset != today:
            self.daily_pnl = 0.0
            self.last_daily_reset = today
        if self.last_weekly_reset != week:
            self.weekly_pnl = 0.0
            self.last_weekly_reset = week
        if self.last_monthly_reset != month:
            self.monthly_pnl = 0.0
            self.last_monthly_reset = month
        self.daily_pnl += cycle_pnl
        self.weekly_pnl += cycle_pnl
        self.monthly_pnl += cycle_pnl

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_capital": self.total_capital,
            "unrealized_pnl": self.unrealized_pnl,
            "effective_capital": self.effective_capital,
            "risk_utilization_pct": f"{self.risk_utilization_pct:.1%}",
            "promoted_count": len(self.promoted_strategies),
            "daily_pnl": self.daily_pnl,
            "weekly_pnl": self.weekly_pnl,
            "monthly_pnl": self.monthly_pnl,
        }


@dataclass
class SlotCycleResult:
    """Tek bir slot iterasyonunun çıktısı."""

    slot_id: int
    strategy_family: str
    market: str
    candidate_params: dict[str, Any]
    basket_sharpe_mean: float
    basket_sharpe_min: float
    profit_ratio: float
    oos_sharpe_mean: float
    improved: bool
    skipped_reason: str | None = None
    notes: str = ""


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class Orchestrator:
    """Slot-bazlı autonomous loop.

    Her cycle: ``BotRegistry.pick_next_slot`` → Bayesian suggest →
    basket backtest (train) → basket OOS holdout → ``try_update`` →
    ``retire_if_stale``. ESKİ promotion / pod / champion / robustness
    zincirleri TAMAMEN KALDIRILDI.
    """

    HEARTBEAT_PHASES: dict[str, str] = {
        "idle":      "Boşta",
        "pick":      "Slot seçiliyor",
        "suggest":   "Bayesian param öneriyor",
        "fetch":     "Veri çekiliyor (4+ yıl)",
        "backtest":  "Basket backtest (train)",
        "holdout":   "OOS holdout doğrulama",
        "decide":    "Slot iyileştirme kararı",
        "persist":   "Hafıza + registry yazılıyor",
    }

    def __init__(
        self,
        markets: list[str] | None = None,
        strategies: list[str] | None = None,
        cycle_pause_seconds: float = 2.0,
        max_cycles: int | None = None,
        initial_capital: float = 100_000.0,
        bot_registry_path: str | Path = "artifacts/bot_registry.json",
    ) -> None:
        self.markets = markets or list(MARKET_SYMBOLS.keys())
        self.strategies = strategies or list(STRATEGY_FACTORIES.keys())
        self.cycle_pause = cycle_pause_seconds
        self.max_cycles = max_cycles

        # Core registries / memory
        self.registry = AgentRegistry()
        self.registry.seed_defaults()
        self.memory = MemoryManager()
        self.ledger = ExperimentLedger()
        self.risk_gate = RiskGate()

        # Faz 6 — bot kişiliği kayıt defteri
        self.bot_registry = BotRegistry(path=bot_registry_path)
        self.bot_registry.seed_initial(
            markets=self.markets,
            strategies=self.strategies,
        )

        # Faz 6 — slot başına sabit sermaye allocation
        # PodAllocator burada sadece bookkeeping için tutuluyor (telemetry/CEO panel).
        # Compound rebalance YOK — slot başına fixed total_capital / MAX_SLOTS.
        self.pod_allocator = PodAllocator(book_capital=initial_capital)
        self._slot_capital: float = initial_capital / max(BotRegistry.MAX_SLOTS, 1)

        # CEO — slot raporları için pod allocator referansı
        self.ceo = CEOAgent(self.registry, pod_allocator=self.pod_allocator)

        # Compact memory stack
        self.journal = LearningJournal()
        self.retriever = MemoryRetriever(journal=self.journal)
        self.insight = InsightExtractor(journal=self.journal)
        self.learning_curve = LearningCurve()

        # Backtest motoru
        self.basket = BasketBacktester(
            data_provider=self._fetch_data,
            n_symbols=5,
            max_workers=3,
        )
        self.holdout = HoldoutGuardian()
        self.bayesian = BayesianOptimizer()

        # Portfolio state (sabit sermaye, compound bug yok)
        self.portfolio = PortfolioState(
            initial_capital=initial_capital,
            total_capital=initial_capital,
        )

        # Stats
        self.total_cycles: int = 0
        self.cycle_history: deque[SlotCycleResult] = deque(maxlen=10_000)

        # Heartbeat
        self._heartbeat_path = Path("artifacts/current_cycle.json")
        self._heartbeat_path.parent.mkdir(parents=True, exist_ok=True)

        self._setup_logging()

        # Faz 7 — Eğitim agent'ları daimi mode (background daemon thread'ler).
        # Talos (curriculum) 30 dk'da, Hermes (git_research) 60 dk'da bir tetiklenir.
        # Hata düşmesi ana cycle'ı bloke etmez — thread içinde 5 dk backoff.
        self.proposals = ProposalQueue()
        try:
            self.educator = EducatorLoop(
                journal=self.journal,
                proposals=self.proposals,
            )
            self.educator.start()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"EducatorLoop start failed: {exc}")
            self.educator = None  # type: ignore[assignment]

    # ------------------------------------------------------------------
    # Logging & heartbeat
    # ------------------------------------------------------------------

    def _setup_logging(self) -> None:
        if getattr(logger, "_oto_file_handler_attached", False):
            return
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        fh = logging.FileHandler(log_dir / "orchestrator.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
        logger.addHandler(fh)
        logger.setLevel(logging.INFO)
        logger._oto_file_handler_attached = True  # type: ignore[attr-defined]

    def _heartbeat(self, phase: str, slot: BotSlot | None = None, extra: dict[str, Any] | None = None) -> None:
        payload = {
            "cycle": self.total_cycles + (1 if phase != "idle" else 0),
            "phase": phase,
            "phase_label": self.HEARTBEAT_PHASES.get(phase, phase),
            "timestamp": _now().isoformat(),
        }
        if slot is not None:
            payload["slot"] = {
                "slot_id": slot.slot_id,
                "strategy_family": slot.strategy_family,
                "market": slot.market,
                "iterations": slot.iterations,
                "accepted_updates": slot.accepted_updates,
                "lifetime_best_sharpe": slot.lifetime_best_sharpe,
                "status": slot.status,
            }
        if extra:
            payload.update(extra)
        # Educator status (opsiyonel — cycle'ı bloke etmez)
        try:
            if getattr(self, "educator", None) is not None:
                payload["educator"] = self.educator.status()
        except Exception:
            pass
        try:
            tmp = self._heartbeat_path.with_suffix(self._heartbeat_path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._heartbeat_path)
        except Exception:
            pass  # heartbeat kırılsa cycle devam eder

    # ------------------------------------------------------------------
    # Veri çekme — 4+ yıl zorunluluğu
    # ------------------------------------------------------------------

    TIMEFRAME_MINUTES: dict[str, int] = {
        "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
        "1h": 60, "2h": 120, "4h": 240, "1d": 1440,
    }

    def _fetch_data(
        self,
        market: str,
        symbol: str,
        timeframe: str,
        window_years: float | None = None,
    ):
        """4+ yıl tarihsel veri çek; ``DATA_WINDOW_START`` ile filtrele.

        Eski ``window_years`` parametresi backward-compat için kabul ediliyor
        ama Faz 6'da kullanılmıyor — slot her zaman 4+ yıl ister.

        Returns
        -------
        pd.DataFrame veya None — yetersiz veri varsa None (caller skip eder).
        """
        del window_years  # noqa: F841
        tf_key = (timeframe or "1h").lower()
        min_bars = MIN_BARS_PER_TF.get(tf_key, 4 * 252 * 24)

        # 1. 5 yıllık cache'ten dene
        df = None
        try:
            from oto_bot.data.downloader import download
            df = download(market, symbol, timeframe, years=5.0, use_cache=True)
        except Exception as e:
            logger.warning(f"data cache fetch failed {market}/{symbol}: {e}")
            df = None

        # 2. Live fallback (cache yoksa)
        if df is None or len(df) < 100:
            try:
                from oto_bot.data.factory import DataProviderFactory
                provider = DataProviderFactory.get_provider(market)
                df = provider.fetch_ohlcv(symbol, timeframe, limit=min_bars)
            except Exception as e:
                logger.warning(f"live data unavailable for {symbol}: {e}")
                df = None

        # 3. start_date filtre uygula (DATA_WINDOW_START sonrası)
        if df is not None and len(df) > 0:
            df = self._filter_after_start(df, DATA_WINDOW_START)

        # 4. Min bar eşik kontrolü
        if df is None or len(df) < min_bars:
            n = 0 if df is None else len(df)
            logger.warning(
                f"insufficient_history skip {symbol} {timeframe} "
                f"({n} bars < {min_bars} required for 4y window)"
            )
            return None

        return df

    @staticmethod
    def _filter_after_start(df, start: str):
        """``start`` (ISO date) sonrasındaki barları döndür.

        Hem datetime indekslerini hem de "timestamp"/"date" kolonunu
        toleranslı şekilde destekler.
        """
        try:
            import pandas as pd
            ts = pd.Timestamp(start, tz="UTC")
            # Index datetime mı?
            if isinstance(df.index, pd.DatetimeIndex):
                idx = df.index
                if idx.tz is None:
                    idx = idx.tz_localize("UTC")
                mask = idx >= ts
                return df.loc[mask].reset_index(drop=False) if mask.any() else df.iloc[0:0]
            # Kolonlar arasında timestamp var mı?
            for col in ("timestamp", "date", "datetime"):
                if col in df.columns:
                    s = pd.to_datetime(df[col], utc=True, errors="coerce")
                    mask = s >= ts
                    if mask.any():
                        return df[mask].reset_index(drop=True)
                    return df.iloc[0:0]
        except Exception as e:
            logger.warning(f"_filter_after_start failed: {e} — full df dönüyor")
        return df

    # ------------------------------------------------------------------
    # Faz 7 — Lesson retrieve (slot iyileştirme öncesi context)
    # ------------------------------------------------------------------

    def _retrieve_lessons_for_slot(self, slot: BotSlot, k: int = 3):
        """Slot'a uygun en alakalı K dersi MemoryRetriever'dan çek.

        Curriculum + git_research + insight kanallarından gelen tüm
        ders'ler ortak filtreden geçer (market + strategy_family).
        Hata durumunda boş liste döner — slot iterasyonu bloke olmaz.
        """
        try:
            ctx = RetrievalContext(
                market=slot.market,
                strategy_family=slot.strategy_family,
                regime="*",
                symbol="*",
                include_global=True,
            )
            hits = self.retriever.retrieve(ctx, k=k, increment_references=True)
            return [lesson for lesson, _score in hits]
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"_retrieve_lessons_for_slot({slot.slot_id}) failed: {exc}")
            return []

    @staticmethod
    def _summarize_lesson_tags(lessons: list) -> str:
        """Dersleri kompakt etiket listesi olarak özetle (notes/heartbeat için)."""
        if not lessons:
            return "none"
        tags: list[str] = []
        for lesson in lessons[:5]:
            # En anlamlı tag'i seç — source_domain veya tag_hint
            picks: list[str] = []
            for tag in lesson.tags:
                if tag.startswith(("source_domain:", "tag_hint:", "repo:")):
                    picks.append(tag.split(":", 1)[1])
            if picks:
                tags.append(picks[0][:24])
            else:
                # author kısa formu — Talos / Hermes
                tags.append(lesson.author_agent.split()[0][:8])
        return ",".join(tags)[:120]

    # ------------------------------------------------------------------
    # Slot iteration — TEK karar noktası
    # ------------------------------------------------------------------

    def _basket_data_for_slot(
        self,
        slot: BotSlot,
        timeframe: str,
    ) -> dict[str, Any]:
        """Slot için 5 sembolün 4+ yıl verisini topla.

        Eksik veriyi (None döndüren sembolleri) atlar; caller min 3 sembol
        bekler.
        """
        symbols = self.basket.select_symbols(slot.market)
        out: dict[str, Any] = {}
        for sym in symbols:
            df = self._fetch_data(slot.market, sym, timeframe)
            if df is not None and len(df) >= 80:
                out[sym] = df
        return out

    def _split_train_holdout(self, data_per_symbol: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """HoldoutGuardian.get_train + holdout slice — basket bazlı."""
        train: dict[str, Any] = {}
        holdout: dict[str, Any] = {}
        for sym, df in data_per_symbol.items():
            self.holdout.register_holdout(sym, df)
            train[sym] = self.holdout.get_train(sym, df)
            # Holdout dilimini doğrudan compute (private accessor kullanmıyoruz —
            # _compute_holdout statik ve idempotent)
            holdout[sym] = HoldoutGuardian._compute_holdout(df)
        return train, holdout

    def run_single_cycle(self) -> SlotCycleResult:
        """Tek bir slot iterasyonu — slot seç, iyileştir, kaydet."""
        cycle = self.total_cycles + 1

        # 1. Slot seç (boşsa yeniden seed)
        self._heartbeat("pick")
        slot = self.bot_registry.pick_next_slot(cycle)
        if slot.status == "empty":
            slot = self.bot_registry.seed_slot(slot.slot_id, current_cycle=cycle)

        timeframe = SLOT_TIMEFRAME.get(slot.strategy_family, "1h")
        logger.info(
            f"CYCLE {cycle} | slot {slot.slot_id} = "
            f"{slot.strategy_family}/{slot.market} | tf={timeframe} | "
            f"iter={slot.iterations} best_sharpe={slot.lifetime_best_sharpe:.2f}"
        )
        # 1.5 — Faz 7: ilgili dersleri çek. Bayesian'a context olarak verilir,
        # prior'a etki etmez (TPE saf data-driven), AMA slot.notes'a hangi
        # lesson tag'leri kullanıldığı yazılır → trace edilebilir.
        retrieved_lessons = self._retrieve_lessons_for_slot(slot, k=3)
        lesson_tags = self._summarize_lesson_tags(retrieved_lessons)
        if retrieved_lessons:
            try:
                self.bot_registry.append_note(
                    slot.slot_id, f"cycle{cycle}:retrieved:{lesson_tags}"
                )
            except Exception:
                pass

        self._heartbeat("suggest", slot=slot, extra={"retrieved_lessons": lesson_tags})

        # 2. Bayesian suggest — slot'a özel study
        space = STRATEGY_PARAM_SPACE.get(slot.strategy_family) or {}
        if not space:
            self.bot_registry.append_note(slot.slot_id, f"cycle{cycle}:no_param_space")
            return self._finish_cycle(slot, {}, None, None, "no_param_space")

        try:
            candidate_params = self.bayesian.suggest(
                family=slot.strategy_family,
                market=slot.market,
                base_space=space,
                study_key=f"slot_{slot.slot_id}",
                context_lessons=retrieved_lessons,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"bayesian.suggest failed for slot {slot.slot_id}: {exc}")
            self.bot_registry.append_note(slot.slot_id, f"cycle{cycle}:suggest_fail")
            return self._finish_cycle(slot, {}, None, None, "suggest_fail")

        # 3. Veri çek (basket × 4+ yıl)
        self._heartbeat("fetch", slot=slot)
        data_per_symbol = self._basket_data_for_slot(slot, timeframe)
        if not data_per_symbol or len(data_per_symbol) < 3:
            self.bot_registry.append_note(slot.slot_id, f"cycle{cycle}:no_data({len(data_per_symbol)})")
            logger.warning(
                f"slot {slot.slot_id}: yeterli sembol yok ({len(data_per_symbol)} < 3) — skip"
            )
            return self._finish_cycle(slot, candidate_params, None, None, "no_data")

        # 4. Train/holdout split
        train_per_sym, holdout_per_sym = self._split_train_holdout(data_per_symbol)

        # 5. Train basket backtest
        self._heartbeat("backtest", slot=slot)
        strategy_cls = STRATEGY_FACTORIES[slot.strategy_family]
        strategy_impl = strategy_cls()
        try:
            basket_result: BasketResult = self.basket.run(
                strategy=strategy_impl,
                params=candidate_params,
                market=slot.market,
                timeframe=timeframe,
                symbols=list(train_per_sym.keys()),
                data_overrides=train_per_sym,
                strategy_family=slot.strategy_family,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"basket train run failed slot {slot.slot_id}: {exc}")
            self.bot_registry.append_note(slot.slot_id, f"cycle{cycle}:train_fail")
            return self._finish_cycle(slot, candidate_params, None, None, "train_fail")

        # 6. OOS basket re-run (her durumda — feedback için)
        self._heartbeat("holdout", slot=slot)
        oos_sharpe = 0.0
        try:
            oos_result: BasketResult = self.basket.run(
                strategy=strategy_impl,
                params=candidate_params,
                market=slot.market,
                timeframe=timeframe,
                symbols=list(holdout_per_sym.keys()),
                data_overrides=holdout_per_sym,
                strategy_family=slot.strategy_family,
            )
            oos_sharpe = float(oos_result.basket_sharpe_mean)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"basket OOS run failed slot {slot.slot_id}: {exc}")
            oos_result = None  # type: ignore[assignment]

        # 7. Slot iyileştirme kararı
        self._heartbeat("decide", slot=slot)
        metrics = {
            "sharpe": float(basket_result.basket_sharpe_mean),
            "oos_sharpe": oos_sharpe,
            "min_sharpe": float(basket_result.basket_sharpe_min),
            "profit_ratio": float(basket_result.profit_ratio),
        }
        improved = self.bot_registry.try_update(
            slot.slot_id, candidate_params, metrics, cycle=cycle
        )

        # 8. Bayesian'a feedback (sharpe ana sinyal)
        try:
            self.bayesian.report(
                family=slot.strategy_family,
                market=slot.market,
                params=candidate_params,
                score=float(metrics["sharpe"]),
                study_key=f"slot_{slot.slot_id}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"bayesian.report failed slot {slot.slot_id}: {exc}")

        # 9. Retire kontrolü
        retired = self.bot_registry.retire_if_stale(slot.slot_id, cycle)
        if retired:
            console.print(
                f"[bold red]SLOT RETIRE:[/bold red] slot {slot.slot_id} "
                f"({slot.strategy_family}/{slot.market}) — iter={slot.iterations} "
                f"best_sharpe={slot.lifetime_best_sharpe:.2f}"
            )

        # 10. Memory: cycle özeti tek satır
        return self._finish_cycle(slot, candidate_params, basket_result, metrics, None, improved=improved)

    def _finish_cycle(
        self,
        slot: BotSlot,
        candidate_params: dict[str, Any],
        basket_result: BasketResult | None,
        metrics: dict[str, Any] | None,
        skipped_reason: str | None,
        improved: bool = False,
    ) -> SlotCycleResult:
        """Cycle artifaktlarını yaz, telemetri/log günle, ``SlotCycleResult`` döndür."""
        cycle = self.total_cycles + 1
        self._heartbeat("persist", slot=slot)

        # Skip ise mark_attempted ile rotation kilidini aç (try_update real-run'da çağrılır).
        if skipped_reason:
            self.bot_registry.mark_attempted(slot.slot_id, cycle, skip_reason=skipped_reason)

        sharpe = float((metrics or {}).get("sharpe") or 0.0)
        oos = float((metrics or {}).get("oos_sharpe") or 0.0)
        min_s = float((metrics or {}).get("min_sharpe") or 0.0)
        pr = float((metrics or {}).get("profit_ratio") or 0.0)

        result = SlotCycleResult(
            slot_id=slot.slot_id,
            strategy_family=slot.strategy_family,
            market=slot.market,
            candidate_params=dict(candidate_params or {}),
            basket_sharpe_mean=sharpe,
            basket_sharpe_min=min_s,
            profit_ratio=pr,
            oos_sharpe_mean=oos,
            improved=bool(improved),
            skipped_reason=skipped_reason,
            notes=(basket_result.notes if basket_result else (skipped_reason or "")),
        )

        # Memory: tek satır slot_iteration
        try:
            # Test ufkunu kayda al — dashboard horizon bucket'ları için.
            tf = SLOT_TIMEFRAME.get(slot.strategy_family, "1d")
            duration_bars = 0
            symbol_basket: list[str] = []
            if basket_result is not None and basket_result.per_symbol:
                # Sembol başına bar — basket'te ortalama yerine max al (en uzun seri).
                for sym, exp_res in basket_result.per_symbol.items():
                    symbol_basket.append(sym)
                    db = int(getattr(exp_res, "duration_bars", 0) or 0)
                    if db > duration_bars:
                        duration_bars = db
            payload = {
                "category": "slot_iteration",
                "slot_id": slot.slot_id,
                "cycle": cycle,
                "strategy_family": slot.strategy_family,
                "market": slot.market,
                "candidate_params": result.candidate_params,
                "sharpe": result.basket_sharpe_mean,
                "oos_sharpe": result.oos_sharpe_mean,
                "min_sharpe": result.basket_sharpe_min,
                "profit_ratio": result.profit_ratio,
                "improved": result.improved,
                "accepted_updates": slot.accepted_updates,
                "lifetime_best_sharpe": slot.lifetime_best_sharpe,
                "lifetime_best_oos_sharpe": slot.lifetime_best_oos_sharpe,
                "iterations": slot.iterations,
                "skipped_reason": skipped_reason,
                # FAZ 6 horizon kayıt — dashboard "<1yr/5yr" bucket'ları için zorunlu.
                "bar_timeframe": tf,
                "duration_bars": duration_bars,
                "symbol": "BASKET:" + ",".join(symbol_basket[:5]) if symbol_basket else None,
                "data_window_start": slot.data_window_start,
                "strategy_params": result.candidate_params,
                "promoted": bool(result.improved),
                "created_at": _now().isoformat(),
            }
            self.memory.store.insert("experiments", "slot_iteration", payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"memory persist failed slot {slot.slot_id}: {exc}")

        # Ledger özet
        try:
            self.ledger.log({
                "category": "slot_iteration",
                "cycle": cycle,
                "slot_id": slot.slot_id,
                "strategy": slot.strategy_family,
                "market": slot.market,
                "params": result.candidate_params,
                "metrics": {
                    "sharpe": result.basket_sharpe_mean,
                    "oos_sharpe": result.oos_sharpe_mean,
                    "min_sharpe": result.basket_sharpe_min,
                    "profit_ratio": result.profit_ratio,
                },
                "improved": result.improved,
                "skipped_reason": skipped_reason,
                "portfolio_state": self.portfolio.to_dict(),
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"ledger log failed: {exc}")

        # Insight extract — sadece anlamlı (skip değil) iterasyonlar için
        if basket_result is not None and basket_result.per_symbol:
            try:
                ref = next(iter(basket_result.per_symbol.values()))
                # ref'i deep-copy etmeye gerek yok; insight read-only kullanıyor.
                ref.sharpe = float(result.basket_sharpe_mean)
                ref.promoted = bool(result.improved)
                self.insight.extract_from_cycle(
                    result=ref,
                    decision={"decision": "improved" if result.improved else "iterate",
                              "reasoning": f"slot {slot.slot_id} sharpe={sharpe:.2f}"},
                    debate_conclusion=None,
                    cycle_number=cycle,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"insight extract failed: {exc}")

        # Portfolio P&L (sabit slot capital, ROI proxy)
        cycle_pnl = 0.0
        if result.improved and basket_result is not None:
            try:
                # ROI proxy: ortalama Sharpe sabit kapital üzerinde küçük bir
                # paper-trading return. Compound bug yok — sermaye sabit.
                cycle_pnl = self._slot_capital * 0.001 * sharpe
            except Exception:
                cycle_pnl = 0.0
        self.portfolio.update_pnl_tracking(cycle_pnl)
        if result.improved:
            self.portfolio.unrealized_pnl += cycle_pnl

        self.total_cycles += 1
        self.cycle_history.append(result)
        return result

    # ------------------------------------------------------------------
    # Autonomous loop
    # ------------------------------------------------------------------

    def run_autonomous(self) -> None:
        """Slot-bazlı sürekli iteration loop'u."""
        console.print("\n[bold green]═══ OTO-BOT FAZ 6 — SLOT MODE ═══[/bold green]")
        console.print(f"Markets: {self.markets}")
        console.print(f"Strategies: {self.strategies}")
        console.print(f"Slots ({len(self.bot_registry.all_slots())}/{BotRegistry.MAX_SLOTS}):")
        for s in self.bot_registry.all_slots():
            console.print(
                f"  [{s.slot_id}] {s.strategy_family}/{s.market} "
                f"status={s.status} iter={s.iterations} best_sharpe={s.lifetime_best_sharpe:.2f}"
            )
        console.print(f"Slot capital: ${self._slot_capital:,.0f} (sabit)")
        console.print(f"Max cycles: {self.max_cycles or '∞'}")
        console.print("[dim]Press Ctrl+C to stop[/dim]\n")

        try:
            while True:
                if self.max_cycles is not None and self.total_cycles >= self.max_cycles:
                    console.print("\n[bold yellow]Max cycles reached. Stopping.[/bold yellow]")
                    break

                cycle_result = self.run_single_cycle()
                self._print_cycle_summary(cycle_result)

                if self.total_cycles % 10 == 0:
                    self._print_executive_brief()

                time.sleep(self.cycle_pause)

        except KeyboardInterrupt:
            console.print("\n[bold yellow]Stopped by user.[/bold yellow]")

        # Faz 7 — eğitim daemon'unu graceful kapat
        try:
            if getattr(self, "educator", None) is not None:
                self.educator.stop()
                console.print("[dim]EducatorLoop stopped.[/dim]")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"EducatorLoop stop failed: {exc}")

        self._print_final_report()

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def _print_cycle_summary(self, cr: SlotCycleResult) -> None:
        marker = "[green]★ IMPROVED[/green]" if cr.improved else (
            f"[dim]skip:{cr.skipped_reason}[/dim]" if cr.skipped_reason else "[yellow]⟳ iterate[/yellow]"
        )
        console.print(
            f"[bold]Cycle {self.total_cycles}[/bold] | slot {cr.slot_id} "
            f"{cr.strategy_family}/{cr.market} | "
            f"sharpe={cr.basket_sharpe_mean:.2f} oos={cr.oos_sharpe_mean:.2f} "
            f"min={cr.basket_sharpe_min:.2f} pr={cr.profit_ratio:.2f} | {marker}"
        )

    def _print_executive_brief(self) -> None:
        console.print("\n[bold cyan]═══ EXECUTIVE BRIEF ═══[/bold cyan]")
        ps = self.portfolio
        console.print(
            f"[bold]Portfolio:[/bold] Capital=${ps.total_capital:,.0f} | "
            f"Unrealized={ps.unrealized_pnl:+,.0f} | "
            f"Slot capital=${self._slot_capital:,.0f}"
        )
        table = Table(title=f"Slots — cycle {self.total_cycles}")
        table.add_column("Slot", justify="right")
        table.add_column("Family")
        table.add_column("Market")
        table.add_column("Status")
        table.add_column("Iter", justify="right")
        table.add_column("Acc", justify="right")
        table.add_column("Best Sharpe", justify="right")
        table.add_column("Best OOS", justify="right")
        for s in self.bot_registry.all_slots():
            table.add_row(
                str(s.slot_id),
                s.strategy_family,
                s.market,
                s.status,
                str(s.iterations),
                str(s.accepted_updates),
                f"{s.lifetime_best_sharpe:.2f}",
                f"{s.lifetime_best_oos_sharpe:.2f}",
            )
        console.print(table)

    def _print_final_report(self) -> None:
        console.print("\n[bold green]═══ FINAL REPORT (Slot Mode) ═══[/bold green]")
        console.print(f"Total cycles: {self.total_cycles}")
        improved_n = sum(1 for c in self.cycle_history if c.improved)
        skipped_n = sum(1 for c in self.cycle_history if c.skipped_reason)
        console.print(f"Improvements: {improved_n}  |  Skipped: {skipped_n}")
        console.print(f"\n[bold]Slot summary:[/bold]")
        for s in self.bot_registry.all_slots():
            console.print(
                f"  [{s.slot_id}] {s.strategy_family}/{s.market} "
                f"status={s.status} iter={s.iterations} accepted={s.accepted_updates} "
                f"best_sharpe={s.lifetime_best_sharpe:.2f} "
                f"best_oos={s.lifetime_best_oos_sharpe:.2f}"
            )
        console.print("\n[bold green]═══ END ═══[/bold green]\n")
