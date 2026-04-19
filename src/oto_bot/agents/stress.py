"""Shockwave Stress Lab — isimli senaryo kütüphanesi ve stres motoru.

Kurumsal standart: masalar her ay VaR'a ek olarak **named stress scenarios**
koşar: 1987 Black Monday, 2008 Lehman, 2020 COVID-Mart çöküşü, 2022 tahvil
şoku gibi. Amaç: stratejinin tail riskte nasıl davrandığını görmek.

Bu modül:
    1. Yerleşik senaryo kütüphanesi sunar (kripto ağırlıklı).
    2. Verilen bir OHLCV serisini şok vektörüyle deforme eder
       ("stress apply") ve yeni bir dataframe döndürür.
    3. Stratejinin şoklanmış seride backtest sonuçlarını StressResult olarak
       kaydeder.

Not: Bu katman backtest engine'ini import etmez — bağımsızlık için stress
motoru bir callable ister (run_backtest_fn). Böylece çift bağımlılık olmaz.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Any, Callable

import pandas as pd

from oto_bot.core.models import StressResult, StressScenario


# ---------------------------------------------------------------------------
# Yerleşik kütüphane
# ---------------------------------------------------------------------------


BUILTIN_SCENARIOS: list[StressScenario] = [
    StressScenario(
        scenario_id="covid_march_2020",
        name="COVID March 2020 Crash",
        description="60% price drop over 30 bars; vol x3; correlation spikes to 0.95.",
        price_shock_pct=-0.60,
        volatility_multiplier=3.0,
        correlation_jump=0.95,
        liquidity_haircut=0.30,
        duration_bars=30,
        historical_reference="2020-03-08..2020-03-20",
    ),
    StressScenario(
        scenario_id="luna_may_2022",
        name="Terra/Luna Collapse",
        description="99% alt wipe over 14 bars; vol x4; cross-crypto contagion.",
        price_shock_pct=-0.80,
        volatility_multiplier=4.0,
        correlation_jump=0.90,
        liquidity_haircut=0.50,
        duration_bars=14,
        historical_reference="2022-05-09..2022-05-14",
    ),
    StressScenario(
        scenario_id="ftx_nov_2022",
        name="FTX Exchange Collapse",
        description="BTC -25% over 5 bars; vol x2.5; exchange risk premium.",
        price_shock_pct=-0.25,
        volatility_multiplier=2.5,
        correlation_jump=0.85,
        liquidity_haircut=0.40,
        duration_bars=5,
        historical_reference="2022-11-06..2022-11-11",
    ),
    StressScenario(
        scenario_id="flash_crash_2010",
        name="Flash Crash Style (5min spike)",
        description="-10% in 1 bar followed by recovery; simulates liquidity vacuum.",
        price_shock_pct=-0.10,
        volatility_multiplier=5.0,
        correlation_jump=0.80,
        liquidity_haircut=0.60,
        duration_bars=2,
        historical_reference="2010-05-06 flash crash analog",
    ),
    StressScenario(
        scenario_id="slow_bleed",
        name="Slow Bleed (trendless grinder)",
        description="-20% over 90 bars with normal vol; kills mean-reversion.",
        price_shock_pct=-0.20,
        volatility_multiplier=1.2,
        correlation_jump=0.50,
        liquidity_haircut=0.10,
        duration_bars=90,
    ),
    StressScenario(
        scenario_id="vol_compression_regime",
        name="Extreme Vol Compression",
        description="ATR collapses to 30% of normal; signals get noisy; breakout traps.",
        price_shock_pct=0.0,
        volatility_multiplier=0.3,
        correlation_jump=0.4,
        liquidity_haircut=0.05,
        duration_bars=60,
    ),
]


# ---------------------------------------------------------------------------
# Şok uygulayıcısı
# ---------------------------------------------------------------------------


class StressLab:
    """Strateji şoklarını çalıştıran stres motoru."""

    def __init__(self, scenarios: list[StressScenario] | None = None) -> None:
        self.scenarios: dict[str, StressScenario] = {
            s.scenario_id: s for s in (scenarios or BUILTIN_SCENARIOS)
        }

    # ------------------------------------------------------------------

    def list_scenarios(self) -> list[dict[str, Any]]:
        return [asdict(s) for s in self.scenarios.values()]

    def get(self, scenario_id: str) -> StressScenario | None:
        return self.scenarios.get(scenario_id)

    # ------------------------------------------------------------------
    # Şok uygulama
    # ------------------------------------------------------------------

    @staticmethod
    def apply_shock(df: pd.DataFrame, scenario: StressScenario) -> pd.DataFrame:
        """Verilen OHLCV'ye senaryo şokunu uygular; yeni dataframe döndürür.

        Yaklaşım:
            - Sonda `duration_bars` kadar bar seç.
            - Fiyat şoku: bu pencerenin kapanışlarına doğrusal bir çürüme ekle.
            - Volatilite çarpanı: high/low yayılımını çarpanla genişlet.
            - Orjinal DataFrame mutasyona uğramaz.
        """
        if scenario.duration_bars <= 0 or len(df) < scenario.duration_bars:
            return df.copy()

        d = df.copy()
        n = scenario.duration_bars
        idx = d.index[-n:]

        # Doğrusal şok: ilk bar +0, son bar +price_shock_pct
        for i, ts in enumerate(idx):
            factor = 1.0 + scenario.price_shock_pct * ((i + 1) / n)
            d.loc[ts, "close"] = float(d.loc[ts, "close"]) * factor
            d.loc[ts, "open"] = float(d.loc[ts, "open"]) * factor
            # Yüksek/düşük için vol çarpanı
            mid = float(d.loc[ts, "close"])
            spread = abs(float(d.loc[ts, "high"]) - float(d.loc[ts, "low"])) * scenario.volatility_multiplier
            d.loc[ts, "high"] = mid + spread / 2
            d.loc[ts, "low"] = mid - spread / 2
            # Likidite haircut: volume'u azalt
            d.loc[ts, "volume"] = float(d.loc[ts, "volume"]) * (1 - scenario.liquidity_haircut)
        return d

    # ------------------------------------------------------------------
    # Tam strateji stres testi
    # ------------------------------------------------------------------

    def run(
        self,
        scenario_id: str,
        base_df: pd.DataFrame,
        strategy_family: str,
        market: str,
        run_backtest_fn: Callable[[pd.DataFrame], dict[str, Any]],
    ) -> StressResult:
        """Bir senaryoyu çalıştırır ve StressResult döndürür.

        Parameters
        ----------
        scenario_id:
            Bilinen senaryo kimliği.
        base_df:
            Şoklanacak temel OHLCV verisi.
        strategy_family:
            Raporlama etiketi.
        market:
            Raporlama etiketi.
        run_backtest_fn:
            Şoklanmış df'yi alıp sonuç dict'i döndürecek callable.
            Beklenen dict anahtarları: max_drawdown, total_pnl, kill_switch_fired (bool).
        """
        scenario = self.scenarios.get(scenario_id)
        if scenario is None:
            raise KeyError(f"Unknown scenario: {scenario_id}")

        shocked = self.apply_shock(base_df, scenario)
        raw = run_backtest_fn(shocked)

        dd = float(raw.get("max_drawdown", 0.0))
        pnl = float(raw.get("total_pnl", 0.0))
        killed = bool(raw.get("kill_switch_fired", False))
        survived = dd >= -0.25 and not killed

        return StressResult(
            scenario_id=scenario_id,
            strategy_family=strategy_family,
            market=market,
            survived=survived,
            max_drawdown_under_stress=dd,
            pnl_under_stress=pnl,
            kill_switch_fired=killed,
            notes=(
                "SURVIVED" if survived else
                f"FAILED (dd={dd:.2%}, killed={killed})"
            ),
        )

    # ------------------------------------------------------------------
    # Tüm kütüphane üzerinde paralel çalıştırma
    # ------------------------------------------------------------------

    def run_all(
        self,
        base_df: pd.DataFrame,
        strategy_family: str,
        market: str,
        run_backtest_fn: Callable[[pd.DataFrame], dict[str, Any]],
    ) -> list[StressResult]:
        results: list[StressResult] = []
        for sid in self.scenarios:
            try:
                results.append(self.run(sid, base_df, strategy_family, market, run_backtest_fn))
            except Exception as exc:  # noqa: BLE001
                results.append(
                    StressResult(
                        scenario_id=sid,
                        strategy_family=strategy_family,
                        market=market,
                        survived=False,
                        max_drawdown_under_stress=0.0,
                        pnl_under_stress=0.0,
                        kill_switch_fired=True,
                        notes=f"ERROR: {exc}",
                    )
                )
        return results
