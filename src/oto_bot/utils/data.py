"""Synthetic OHLCV generator -- used as **fallback** when real data providers
(ccxt, yfinance) are unavailable or fail.  Production code should always
prefer the real providers in ``oto_bot.data``."""
from __future__ import annotations

import numpy as np
import pandas as pd


def make_synthetic_ohlc(rows: int = 500, seed: int = 42) -> pd.DataFrame:
    """Generate synthetic OHLCV bars for testing and fallback purposes.

    WARNING: This produces random walk data.  Do NOT use for strategy
    validation -- use real market data from ``oto_bot.data`` providers.
    """
    rng = np.random.default_rng(seed)
    ret = rng.normal(0.0005, 0.01, size=rows)
    price = 100 * np.cumprod(1 + ret)
    df = pd.DataFrame({"close": price})
    df["open"] = df["close"].shift(1).fillna(df["close"])
    df["high"] = df[["open", "close"]].max(axis=1) * (1 + rng.uniform(0.0, 0.01, size=rows))
    df["low"] = df[["open", "close"]].min(axis=1) * (1 - rng.uniform(0.0, 0.01, size=rows))
    df["volume"] = rng.integers(100, 1000, size=rows)
    return df
