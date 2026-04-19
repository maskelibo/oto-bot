"""BasketBacktester — sepet bazlı çoklu sembol backtest motoru.

Faz 2 mimari değişikliğin temel parçası: tek bir
``(strategy_family, params, market_basket)`` üçlüsü, market sepetinin
N sembolü üzerinde çalıştırılıp **sepet metriği** üretilir. Tek sembolde
yüksek Sharpe yakalama "winner" sayılmaz; sepet ortalaması, dağılımı ve
kâr oranı tatmin etmelidir.

Bu modül `BacktestEngine.run()` imzasına dokunmaz, onu sembol başına
çağırır. Veri sağlayıcı dışarıdan callable olarak verilir, böylece
``Orchestrator._fetch_data`` doğrudan reuse edilir.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd

from oto_bot.backtest.engine import BacktestEngine
from oto_bot.core.models import ExperimentResult
from oto_bot.strategies.base import Strategy, StrategyContext

logger = logging.getLogger("oto_bot.backtest.basket")


# ---------------------------------------------------------------------------
# Market universe — orchestrator constants ile bilerek senkron tutuldu.
# Tek kaynak yapma uğruna circular import açmamak için kopyalandı.
# ---------------------------------------------------------------------------

MARKET_SYMBOLS: dict[str, list[str]] = {
    "crypto": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"],
    "forex": ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF"],
    "us_equities": ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "TSLA", "META"],
    "bist": ["THYAO", "GARAN", "ASELS", "SISE", "EREGL", "KCHOL"],
}

TIMEFRAMES: dict[str, list[str]] = {
    "day": ["1h", "4h"],
    "swing": ["4h", "1d"],
    "scalp": ["5m", "15m"],
}


# Type alias — orchestrator._fetch_data ile aynı imza:
# (market, symbol, timeframe) -> pd.DataFrame | None
DataProvider = Callable[[str, str, str], "pd.DataFrame | None"]


# ---------------------------------------------------------------------------
# Promotion gate — sepet bazlı eşikler
# ---------------------------------------------------------------------------

DEFAULT_BASKET_SHARPE_MEAN: float = 1.2
DEFAULT_BASKET_SHARPE_MIN: float = 0.5
DEFAULT_PROFIT_RATIO: float = 0.6


@dataclass
class BasketResult:
    """Bir basket koşumunun toplu sonucu."""

    market: str
    strategy_family: str
    timeframe: str
    symbols: list[str]
    per_symbol: dict[str, ExperimentResult] = field(default_factory=dict)
    basket_sharpe_mean: float = 0.0
    basket_sharpe_std: float = 0.0
    basket_sharpe_min: float = 0.0
    basket_max_drawdown_worst: float = 0.0
    profit_count: int = 0
    profit_ratio: float = 0.0
    qualified: bool = False
    skipped_symbols: list[str] = field(default_factory=list)
    notes: str = ""

    @property
    def total_symbols(self) -> int:
        return len(self.per_symbol)

    def to_summary_dict(self) -> dict[str, Any]:
        """Kompakt özet — winners.jsonl ve notes alanına yazmak için."""
        return {
            "sharpe_mean": round(float(self.basket_sharpe_mean), 4),
            "sharpe_std": round(float(self.basket_sharpe_std), 4),
            "sharpe_min": round(float(self.basket_sharpe_min), 4),
            "max_drawdown_worst": round(float(self.basket_max_drawdown_worst), 4),
            "profit_count": int(self.profit_count),
            "profit_ratio": round(float(self.profit_ratio), 4),
            "total_symbols": int(self.total_symbols),
            "qualified": bool(self.qualified),
            "symbols": list(self.symbols),
            "skipped": list(self.skipped_symbols),
        }


# ---------------------------------------------------------------------------
# BasketBacktester
# ---------------------------------------------------------------------------

class BasketBacktester:
    """Sepet üzerinde paralel backtest koşumu + sepet metrik agregasyonu.

    Kullanım::

        bt = BasketBacktester(data_provider=orchestrator._fetch_data)
        basket = bt.run(strategy, params, market="us_equities", timeframe="4h")
        if basket.qualified:
            ...

    ``data_provider`` çağrısı None döndürürse veya çok kısa veri verirse o
    sembol skip edilir. Engine içinde fırlayan istisnalar logger.warning
    ile yutulur — bir sembol hatası diğerlerini bozmaz.
    """

    MIN_BARS: int = 50  # bu altında sembol skip edilir

    def __init__(
        self,
        data_provider: DataProvider,
        engine: BacktestEngine | None = None,
        n_symbols: int = 5,
        max_workers: int = 3,
        sharpe_mean_min: float = DEFAULT_BASKET_SHARPE_MEAN,
        sharpe_min_min: float = DEFAULT_BASKET_SHARPE_MIN,
        profit_ratio_min: float = DEFAULT_PROFIT_RATIO,
    ) -> None:
        self.data_provider = data_provider
        self.engine = engine or BacktestEngine()
        self.n_symbols = max(1, int(n_symbols))
        self.max_workers = max(1, int(max_workers))
        self.sharpe_mean_min = float(sharpe_mean_min)
        self.sharpe_min_min = float(sharpe_min_min)
        self.profit_ratio_min = float(profit_ratio_min)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def select_symbols(self, market: str, override: list[str] | None = None) -> list[str]:
        """Markete göre ilk N sembolü seç. Override verilirse onu kullan."""
        if override:
            return list(override[: self.n_symbols])
        pool = MARKET_SYMBOLS.get(market, [])
        return list(pool[: self.n_symbols])

    def run(
        self,
        strategy: Strategy,
        params: dict[str, Any],
        market: str,
        timeframe: str,
        *,
        symbols: list[str] | None = None,
        data_overrides: dict[str, pd.DataFrame] | None = None,
        strategy_family: str | None = None,
    ) -> BasketResult:
        """Sepet üzerinde backtest koş.

        Parameters
        ----------
        strategy:
            Çalıştırılacak strategy instance.
        params:
            Strategy parametre sözlüğü; her sembol için aynı.
        market:
            Sembol seçimi için market anahtarı.
        timeframe:
            Bar timeframe (örn "1h", "4h").
        symbols:
            Override liste; None ise market sepetinden ilk N alınır.
        data_overrides:
            ``{symbol: DataFrame}`` — verilen sembol için provider'ı bypass
            eder (ör: HoldoutGuardian.get_train ile train kısmını enjekte
            etmek için).
        strategy_family:
            Result'a yazılacak family adı; None ise strategy.name kullanılır.
        """
        chosen = self.select_symbols(market, override=symbols)
        if not chosen:
            logger.warning(f"BasketBacktester: market={market} için sembol bulunamadı")
            return BasketResult(
                market=market,
                strategy_family=strategy_family or getattr(strategy, "name", "unknown"),
                timeframe=timeframe,
                symbols=[],
                notes="empty_basket",
            )

        family = strategy_family or getattr(strategy, "name", "unknown")
        per_symbol: dict[str, ExperimentResult] = {}
        skipped: list[str] = []

        # --- Paralel koşum: numpy/pandas GIL bırakır → modest gain ---
        futures = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
            for sym in chosen:
                # Veri kaynağı: override > provider
                df: pd.DataFrame | None = None
                if data_overrides and sym in data_overrides:
                    df = data_overrides[sym]
                else:
                    try:
                        df = self.data_provider(market, sym, timeframe)
                    except Exception as e:
                        logger.warning(f"Basket: data fetch fail {sym}: {e}")
                        df = None

                if df is None or len(df) < self.MIN_BARS:
                    logger.warning(
                        f"Basket: {sym} skip — veri yok veya çok kısa "
                        f"(len={0 if df is None else len(df)})"
                    )
                    skipped.append(sym)
                    continue

                ctx = StrategyContext(
                    market=market,
                    strategy_family=family,
                    symbol=sym,
                    timeframe=timeframe,
                    params=dict(params),
                )
                fut = ex.submit(self._run_single, strategy, df, ctx)
                futures[fut] = sym

            for fut in as_completed(futures):
                sym = futures[fut]
                try:
                    res = fut.result()
                    per_symbol[sym] = res
                except Exception as e:
                    logger.warning(f"Basket: {sym} engine error: {e}")
                    skipped.append(sym)

        result = self._aggregate(
            market=market,
            strategy_family=family,
            timeframe=timeframe,
            symbols=chosen,
            per_symbol=per_symbol,
            skipped=skipped,
        )
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _run_single(
        self,
        strategy: Strategy,
        data: pd.DataFrame,
        context: StrategyContext,
    ) -> ExperimentResult:
        """Engine wrapper — testte mocklanması kolay olsun diye ayrı method."""
        return self.engine.run(strategy, data, context)

    def _aggregate(
        self,
        market: str,
        strategy_family: str,
        timeframe: str,
        symbols: list[str],
        per_symbol: dict[str, ExperimentResult],
        skipped: list[str],
    ) -> BasketResult:
        if not per_symbol:
            return BasketResult(
                market=market,
                strategy_family=strategy_family,
                timeframe=timeframe,
                symbols=symbols,
                per_symbol={},
                skipped_symbols=skipped,
                qualified=False,
                notes="all_symbols_skipped",
            )

        sharpes = [float(r.sharpe) for r in per_symbol.values()]
        dds = [float(r.max_drawdown) for r in per_symbol.values()]

        n = len(sharpes)
        mean = sum(sharpes) / n
        # Numpy bağımlılığı olmadan std (örnek std değil populasyon std değil
        # — pratik olarak küçük N için fark önemsiz; numpy.std default ddof=0)
        var = sum((s - mean) ** 2 for s in sharpes) / n
        std = var ** 0.5
        s_min = min(sharpes)
        dd_worst = min(dds) if dds else 0.0  # en negatif

        profit_count = sum(1 for s in sharpes if s > 0)
        profit_ratio = profit_count / n if n > 0 else 0.0

        qualified = (
            mean >= self.sharpe_mean_min
            and s_min >= self.sharpe_min_min
            and profit_ratio >= self.profit_ratio_min
        )

        notes_bits = [
            f"basket={n}/{len(symbols)}",
            f"sharpe_mean={mean:.3f}",
            f"sharpe_min={s_min:.3f}",
            f"profit_ratio={profit_ratio:.2f}",
            f"qualified={qualified}",
        ]
        if skipped:
            notes_bits.append(f"skipped={','.join(skipped)}")

        return BasketResult(
            market=market,
            strategy_family=strategy_family,
            timeframe=timeframe,
            symbols=symbols,
            per_symbol=per_symbol,
            basket_sharpe_mean=mean,
            basket_sharpe_std=std,
            basket_sharpe_min=s_min,
            basket_max_drawdown_worst=dd_worst,
            profit_count=profit_count,
            profit_ratio=profit_ratio,
            qualified=qualified,
            skipped_symbols=skipped,
            notes=" | ".join(notes_bits),
        )
