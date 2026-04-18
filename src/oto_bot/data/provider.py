"""Abstract base class for all market data providers."""
from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd


class DataProvider(ABC):
    """Uniform interface for fetching OHLCV data across markets.

    Every concrete provider must return a DataFrame with:
        - DatetimeIndex (UTC)
        - Columns: open, high, low, close, volume
    """

    @abstractmethod
    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1d",
        since: str | None = None,
        limit: int = 500,
    ) -> pd.DataFrame:
        """Fetch OHLCV bars for *symbol*.

        Parameters
        ----------
        symbol:
            Ticker / pair identifier (e.g. "BTC/USDT", "AAPL", "EURUSD").
        timeframe:
            Bar interval string such as "1m", "5m", "1h", "1d".
        since:
            ISO-8601 start date string (e.g. "2024-01-01"). Provider
            implementations convert this to whatever their backend needs.
        limit:
            Maximum number of bars to return.

        Returns
        -------
        pd.DataFrame
            Columns: open, high, low, close, volume.  DatetimeIndex in UTC.
        """
        ...

    @abstractmethod
    def available_symbols(self) -> list[str]:
        """Return a list of symbols supported by this provider."""
        ...
