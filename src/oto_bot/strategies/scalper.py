from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import ta

from oto_bot.strategies.base import Strategy, StrategyContext


class ScalperStrategy(Strategy):
    """Mean-reversion scalping strategy using Bollinger Bands and RSI.

    Signal logic
    ------------
    1. Bollinger Bands define the envelope; Z-score measures how far price
       has deviated from the mean.
    2. Long when Z-score drops below the negative threshold (oversold),
       short when Z-score exceeds the positive threshold (overbought).
    3. RSI divergence confirmation: RSI must agree with the mean-reversion
       direction (RSI < oversold for long, RSI > overbought for short).
    4. Tight ATR-based stop-loss and quick take-profit.
    """

    name = "scalper_mean_reversion"

    # ------------------------------------------------------------------
    # Default parameters
    # ------------------------------------------------------------------

    @classmethod
    def get_default_params(cls) -> dict[str, Any]:
        return {
            # Bollinger Bands
            "bb_period": 20,
            "bb_std": 2.0,
            # Z-score
            "zscore_period": 20,
            "zscore_entry": 1.5,
            # RSI
            "rsi_period": 14,
            "rsi_oversold": 35,
            "rsi_overbought": 65,
            # ATR & risk
            "atr_period": 14,
            "atr_sl_multiplier": 1.0,
            "atr_tp_multiplier": 1.5,
            # Position sizing
            "capital": 100_000.0,
            "risk_per_trade": 0.005,
        }

    # ------------------------------------------------------------------
    # Signal generation
    # ------------------------------------------------------------------

    def generate_signals(
        self, data: pd.DataFrame, context: StrategyContext
    ) -> pd.DataFrame:
        p = self._resolve_params(context)
        df = data.copy()

        # -- Bollinger Bands ---------------------------------------------
        bb = ta.volatility.BollingerBands(
            close=df["close"],
            window=p["bb_period"],
            window_dev=p["bb_std"],
        )
        df["bb_upper"] = bb.bollinger_hband()
        df["bb_mid"] = bb.bollinger_mavg()
        df["bb_lower"] = bb.bollinger_lband()

        # -- Z-score -----------------------------------------------------
        rolling_mean = df["close"].rolling(window=p["zscore_period"]).mean()
        rolling_std = df["close"].rolling(window=p["zscore_period"]).std()
        df["zscore"] = (df["close"] - rolling_mean) / rolling_std

        # -- RSI ---------------------------------------------------------
        rsi_obj = ta.momentum.RSIIndicator(df["close"], window=p["rsi_period"])
        df["rsi"] = rsi_obj.rsi()

        # -- ATR ---------------------------------------------------------
        atr_obj = ta.volatility.AverageTrueRange(
            high=df["high"], low=df["low"], close=df["close"], window=p["atr_period"]
        )
        df["atr"] = atr_obj.average_true_range()

        # -- Signal logic ------------------------------------------------
        # Mean-reversion: buy when price is depressed, sell when extended
        long_zscore = df["zscore"] < -p["zscore_entry"]
        short_zscore = df["zscore"] > p["zscore_entry"]

        long_rsi = df["rsi"] < p["rsi_oversold"]
        short_rsi = df["rsi"] > p["rsi_overbought"]

        df["signal"] = 0
        df.loc[long_zscore & long_rsi, "signal"] = 1
        df.loc[short_zscore & short_rsi, "signal"] = -1

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
