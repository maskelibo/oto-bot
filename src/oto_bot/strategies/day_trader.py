from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import ta

from oto_bot.strategies.base import Strategy, StrategyContext


class DayTraderStrategy(Strategy):
    """Multi-indicator momentum strategy for intraday trading.

    Signal logic
    ------------
    1. EMA crossover (fast > slow -> bullish, fast < slow -> bearish).
    2. RSI filter: suppress long when RSI > overbought, suppress short when
       RSI < oversold.
    3. Volume confirmation: volume must exceed its own moving average by a
       configurable multiplier.
    4. ATR-based stop-loss, take-profit, and position sizing.
    """

    name = "day_trader_momentum"

    # ------------------------------------------------------------------
    # Default parameters
    # ------------------------------------------------------------------

    @classmethod
    def get_default_params(cls) -> dict[str, Any]:
        return {
            # EMA
            "ema_fast": 9,
            "ema_slow": 21,
            # RSI
            "rsi_period": 14,
            "rsi_overbought": 70,
            "rsi_oversold": 30,
            # Volume
            "volume_ma_period": 20,
            "volume_multiplier": 1.5,
            # ATR & risk
            "atr_period": 14,
            "atr_sl_multiplier": 1.5,
            "atr_tp_multiplier": 2.5,
            # Position sizing
            "capital": 100_000.0,
            "risk_per_trade": 0.01,
        }

    # ------------------------------------------------------------------
    # Signal generation
    # ------------------------------------------------------------------

    def generate_signals(
        self, data: pd.DataFrame, context: StrategyContext
    ) -> pd.DataFrame:
        p = self._resolve_params(context)
        df = data.copy()

        # -- Indicators --------------------------------------------------
        df["ema_fast"] = ta.trend.ema_indicator(df["close"], window=p["ema_fast"])
        df["ema_slow"] = ta.trend.ema_indicator(df["close"], window=p["ema_slow"])

        rsi_indicator = ta.momentum.RSIIndicator(df["close"], window=p["rsi_period"])
        df["rsi"] = rsi_indicator.rsi()

        df["volume_ma"] = df["volume"].rolling(window=p["volume_ma_period"]).mean()

        atr_indicator = ta.volatility.AverageTrueRange(
            high=df["high"], low=df["low"], close=df["close"], window=p["atr_period"]
        )
        df["atr"] = atr_indicator.average_true_range()

        # -- Raw crossover direction -------------------------------------
        df["signal"] = 0
        bullish_cross = df["ema_fast"] > df["ema_slow"]
        bearish_cross = df["ema_fast"] < df["ema_slow"]

        df.loc[bullish_cross, "signal"] = 1
        df.loc[bearish_cross, "signal"] = -1

        # -- RSI filter --------------------------------------------------
        df.loc[(df["signal"] == 1) & (df["rsi"] > p["rsi_overbought"]), "signal"] = 0
        df.loc[(df["signal"] == -1) & (df["rsi"] < p["rsi_oversold"]), "signal"] = 0

        # -- Volume confirmation -----------------------------------------
        volume_ok = df["volume"] > (df["volume_ma"] * p["volume_multiplier"])
        df.loc[~volume_ok, "signal"] = 0

        # -- Stop-loss & take-profit -------------------------------------
        df["stop_loss"] = np.where(
            df["signal"] == 1,
            df["close"] - df["atr"] * p["atr_sl_multiplier"],
            np.where(
                df["signal"] == -1,
                df["close"] + df["atr"] * p["atr_sl_multiplier"],
                np.nan,
            ),
        )

        df["take_profit"] = np.where(
            df["signal"] == 1,
            df["close"] + df["atr"] * p["atr_tp_multiplier"],
            np.where(
                df["signal"] == -1,
                df["close"] - df["atr"] * p["atr_tp_multiplier"],
                np.nan,
            ),
        )

        # -- Position sizing ---------------------------------------------
        df["position_size"] = df.apply(
            lambda row: self.compute_position_size(
                capital=p["capital"],
                risk_per_trade=p["risk_per_trade"],
                atr=row["atr"],
                entry_price=row["close"],
                atr_sl_multiplier=p["atr_sl_multiplier"],
            )
            if row["signal"] != 0
            else 0.0,
            axis=1,
        )

        return df
