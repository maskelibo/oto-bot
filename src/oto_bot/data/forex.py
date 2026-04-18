"""Forex data provider backed by yfinance."""
from __future__ import annotations

import logging

import pandas as pd

from oto_bot.data.provider import DataProvider
from oto_bot.utils.data import make_synthetic_ohlc

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Symbol mapping: internal pair name -> Yahoo Finance format
# ---------------------------------------------------------------------------

_FOREX_PAIRS: dict[str, str] = {
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "USDCHF": "USDCHF=X",
    "AUDUSD": "AUDUSD=X",
    "USDCAD": "USDCAD=X",
    "NZDUSD": "NZDUSD=X",
    "EURGBP": "EURGBP=X",
    "EURJPY": "EURJPY=X",
    "GBPJPY": "GBPJPY=X",
    "USDTRY": "USDTRY=X",
    "EURTRY": "EURTRY=X",
}

_TIMEFRAME_MAP: dict[str, str] = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "1d": "1d",
    "1w": "1wk",
    "1M": "1mo",
}

_DEFAULT_PERIOD: dict[str, str] = {
    "1m": "7d",
    "5m": "60d",
    "15m": "60d",
    "30m": "60d",
    "1h": "730d",
    "1d": "2y",
    "1wk": "5y",
    "1mo": "max",
}


class ForexDataProvider(DataProvider):
    """Fetch forex OHLCV data via yfinance."""

    # ------------------------------------------------------------------
    # Symbol resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_symbol(symbol: str) -> str:
        """Convert internal pair name to Yahoo Finance ticker."""
        clean = symbol.upper().replace("/", "").replace("-", "")
        if clean in _FOREX_PAIRS:
            return _FOREX_PAIRS[clean]
        # If already in Yahoo format (e.g. "EURUSD=X"), pass through
        if symbol.endswith("=X"):
            return symbol
        return f"{clean}=X"

    # ------------------------------------------------------------------
    # DataProvider interface
    # ------------------------------------------------------------------

    def fetch_ohlcv(
        self,
        symbol: str = "EURUSD",
        timeframe: str = "1d",
        since: str | None = None,
        limit: int = 500,
    ) -> pd.DataFrame:
        try:
            import yfinance as yf  # type: ignore[import-untyped]
        except ImportError:
            logger.error("yfinance is not installed")
            return _synthetic_fallback(limit)

        ticker = self._resolve_symbol(symbol)
        interval = _TIMEFRAME_MAP.get(timeframe, timeframe)

        try:
            if since is not None:
                df: pd.DataFrame = yf.download(
                    ticker,
                    start=since,
                    interval=interval,
                    progress=False,
                    auto_adjust=True,
                )
            else:
                period = _DEFAULT_PERIOD.get(interval, "2y")
                df = yf.download(
                    ticker,
                    period=period,
                    interval=interval,
                    progress=False,
                    auto_adjust=True,
                )

            if df.empty:
                logger.warning("yfinance returned empty data for %s", ticker)
                return _synthetic_fallback(limit)

            df = _normalise(df)
            if limit and len(df) > limit:
                df = df.iloc[-limit:]
            return df

        except Exception as exc:
            logger.error("yfinance fetch failed for %s: %s", ticker, exc)
            return _synthetic_fallback(limit)

    def available_symbols(self) -> list[str]:
        return list(_FOREX_PAIRS.keys())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalise(df: pd.DataFrame) -> pd.DataFrame:
    """Lowercase columns, UTC index, standard OHLCV columns."""
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.lower() for c in df.columns]

    for col in ("open", "high", "low", "close", "volume"):
        if col not in df.columns:
            df[col] = float("nan")

    df = df[["open", "high", "low", "close", "volume"]]

    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    df.index.name = None

    return df


def _synthetic_fallback(rows: int = 500) -> pd.DataFrame:
    """Generate synthetic OHLCV when yfinance is unavailable."""
    logger.info("Using synthetic fallback data (%d rows)", rows)
    df = make_synthetic_ohlc(rows=rows)
    df.index = pd.date_range(end=pd.Timestamp.now(tz="UTC"), periods=len(df), freq="D")
    df.index.name = None
    return df
