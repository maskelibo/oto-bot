"""Stocks data provider backed by yfinance (US equities and BIST)."""
from __future__ import annotations

import logging

import pandas as pd

from oto_bot.data.provider import DataProvider
from oto_bot.utils.data import make_synthetic_ohlc

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# BIST symbol mapping: internal name -> Yahoo Finance ticker
# ---------------------------------------------------------------------------

_BIST_SUFFIX = ".IS"

_BIST_SYMBOLS: dict[str, str] = {
    "THYAO": "THYAO.IS",
    "GARAN": "GARAN.IS",
    "AKBNK": "AKBNK.IS",
    "EREGL": "EREGL.IS",
    "SISE": "SISE.IS",
    "KCHOL": "KCHOL.IS",
    "ASELS": "ASELS.IS",
    "BIMAS": "BIMAS.IS",
    "SAHOL": "SAHOL.IS",
    "TUPRS": "TUPRS.IS",
    "PGSUS": "PGSUS.IS",
    "TCELL": "TCELL.IS",
    "SASA": "SASA.IS",
    "TOASO": "TOASO.IS",
    "FROTO": "FROTO.IS",
}

# yfinance interval string mapping (our timeframe -> yfinance valid_periods)
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

# yfinance period defaults per interval
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


class StocksDataProvider(DataProvider):
    """Fetch US equities or BIST stocks via yfinance."""

    def __init__(self, market: str = "us_equities") -> None:
        self._market = market

    # ------------------------------------------------------------------
    # Symbol resolution
    # ------------------------------------------------------------------

    def _resolve_symbol(self, symbol: str) -> str:
        """Map an internal symbol to a Yahoo Finance ticker."""
        if self._market == "bist":
            if symbol in _BIST_SYMBOLS:
                return _BIST_SYMBOLS[symbol]
            # Auto-add .IS suffix if not already present
            if not symbol.endswith(_BIST_SUFFIX):
                return f"{symbol}{_BIST_SUFFIX}"
        return symbol

    # ------------------------------------------------------------------
    # DataProvider interface
    # ------------------------------------------------------------------

    def fetch_ohlcv(
        self,
        symbol: str = "AAPL",
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
        if self._market == "bist":
            return list(_BIST_SYMBOLS.keys())
        # Returning a curated list for US equities; extend as needed
        return [
            "AAPL", "MSFT", "GOOG", "AMZN", "NVDA", "META", "TSLA",
            "BRK-B", "JPM", "V", "UNH", "XOM", "JNJ", "WMT", "PG",
            "MA", "HD", "CVX", "MRK", "ABBV", "LLY", "PEP", "KO",
            "BAC", "COST", "AVGO", "TMO", "MCD", "CSCO", "CRM",
        ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalise(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure lowercase column names, UTC index, standard columns."""
    df = df.copy()
    # yfinance may return MultiIndex columns for single tickers
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.lower() for c in df.columns]

    # Keep only standard columns
    for col in ("open", "high", "low", "close", "volume"):
        if col not in df.columns:
            logger.warning("Missing column %s after download, filling with NaN", col)
            df[col] = float("nan")

    df = df[["open", "high", "low", "close", "volume"]]

    # Ensure UTC-aware datetime index
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
