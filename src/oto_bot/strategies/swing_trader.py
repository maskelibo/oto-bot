from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import ta

from oto_bot.strategies.base import Strategy, StrategyContext


class SwingTraderStrategy(Strategy):
    """Trend-following strategy with MACD and ADX confirmation.

    Signal logic
    ------------
    1. MA crossover (fast_ma > slow_ma -> bullish bias).
    2. MACD confirmation: MACD line must cross above/below signal line in
       agreement with the MA bias.
    3. ADX filter: only trade when ADX > threshold (strong trend).
    4. ATR-based stop-loss and take-profit (wider multiples than day trader).
    """

    name = "swing_trader_trend"

    # ------------------------------------------------------------------
    # Default parameters
    # ------------------------------------------------------------------

    @classmethod
    def get_default_params(cls) -> dict[str, Any]:
        return {
            # Moving averages
            "ma_fast": 50,
            "ma_slow": 200,
            # MACD
            "macd_fast": 12,
            "macd_slow": 26,
            "macd_signal": 9,
            # ADX
            "adx_period": 14,
            "adx_threshold": 25,
            # ATR & risk
            "atr_period": 14,
            "atr_sl_multiplier": 2.5,
            "atr_tp_multiplier": 4.0,
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
        df["ma_fast"] = df["close"].rolling(window=p["ma_fast"]).mean()
        df["ma_slow"] = df["close"].rolling(window=p["ma_slow"]).mean()

        macd_obj = ta.trend.MACD(
            close=df["close"],
            window_fast=p["macd_fast"],
            window_slow=p["macd_slow"],
            window_sign=p["macd_signal"],
        )
        df["macd"] = macd_obj.macd()
        df["macd_signal"] = macd_obj.macd_signal()
        df["macd_diff"] = macd_obj.macd_diff()  # histogram

        adx_obj = ta.trend.ADXIndicator(
            high=df["high"],
            low=df["low"],
            close=df["close"],
            window=p["adx_period"],
        )
        df["adx"] = adx_obj.adx()

        atr_obj = ta.volatility.AverageTrueRange(
            high=df["high"], low=df["low"], close=df["close"], window=p["atr_period"]
        )
        df["atr"] = atr_obj.average_true_range()

        # -- MA crossover direction --------------------------------------
        ma_bullish = df["ma_fast"] > df["ma_slow"]
        ma_bearish = df["ma_fast"] < df["ma_slow"]

        # -- MACD confirmation -------------------------------------------
        macd_bullish = df["macd"] > df["macd_signal"]
        macd_bearish = df["macd"] < df["macd_signal"]

        # -- ADX filter --------------------------------------------------
        strong_trend = df["adx"] > p["adx_threshold"]

        # -- Combine -----------------------------------------------------
        df["signal"] = 0
        df.loc[ma_bullish & macd_bullish & strong_trend, "signal"] = 1
        df.loc[ma_bearish & macd_bearish & strong_trend, "signal"] = -1

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
