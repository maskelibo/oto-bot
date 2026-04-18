"""Crypto data provider backed by ccxt (Binance by default)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from oto_bot.data.provider import DataProvider
from oto_bot.utils.data import make_synthetic_ohlc

logger = logging.getLogger(__name__)

# Timeframe string -> ccxt-compatible millisecond multiplier is handled by ccxt
# itself, so we just pass the string through.

_DEFAULT_EXCHANGE = "binance"


class CryptoDataProvider(DataProvider):
    """Fetch real crypto OHLCV via ccxt with automatic synthetic fallback."""

    def __init__(self, exchange_id: str = _DEFAULT_EXCHANGE, sandbox: bool = False) -> None:
        self._exchange_id = exchange_id
        self._sandbox = sandbox
        self._exchange: Any | None = None

    # ------------------------------------------------------------------
    # Lazy exchange init (avoids import cost when unused)
    # ------------------------------------------------------------------

    def _get_exchange(self) -> Any:
        if self._exchange is not None:
            return self._exchange
        try:
            import ccxt  # type: ignore[import-untyped]

            exchange_class = getattr(ccxt, self._exchange_id)
            self._exchange = exchange_class(
                {
                    "enableRateLimit": True,
                    "options": {"defaultType": "spot"},
                }
            )
            if self._sandbox:
                self._exchange.set_sandbox_mode(True)
            self._exchange.load_markets()
        except Exception as exc:
            logger.warning("ccxt exchange init failed (%s): %s", self._exchange_id, exc)
            self._exchange = None
        return self._exchange

    # ------------------------------------------------------------------
    # DataProvider interface
    # ------------------------------------------------------------------

    def fetch_ohlcv(
        self,
        symbol: str = "BTC/USDT",
        timeframe: str = "1d",
        since: str | None = None,
        limit: int = 500,
    ) -> pd.DataFrame:
        exchange = self._get_exchange()
        if exchange is None:
            logger.warning(
                "Exchange unavailable, returning synthetic data for %s", symbol
            )
            return _synthetic_fallback(limit)

        try:
            since_ms: int | None = None
            if since is not None:
                dt = datetime.fromisoformat(since).replace(tzinfo=timezone.utc)
                since_ms = int(dt.timestamp() * 1000)

            raw = exchange.fetch_ohlcv(symbol, timeframe, since=since_ms, limit=limit)
            df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            df.set_index("timestamp", inplace=True)
            df.index.name = None
            return df

        except Exception as exc:
            logger.error("ccxt fetch_ohlcv failed for %s: %s", symbol, exc)
            return _synthetic_fallback(limit)

    def available_symbols(self) -> list[str]:
        exchange = self._get_exchange()
        if exchange is None:
            return ["BTC/USDT", "ETH/USDT"]  # minimal fallback list
        try:
            return list(exchange.markets.keys())
        except Exception as exc:
            logger.error("Failed to list crypto symbols: %s", exc)
            return ["BTC/USDT", "ETH/USDT"]


# ---------------------------------------------------------------------------
# Synthetic fallback
# ---------------------------------------------------------------------------

def _synthetic_fallback(rows: int = 500) -> pd.DataFrame:
    """Generate synthetic OHLCV when the real API is unavailable."""
    logger.info("Using synthetic fallback data (%d rows)", rows)
    df = make_synthetic_ohlc(rows=rows)
    # Attach a datetime index to match the standard contract
    df.index = pd.date_range(end=pd.Timestamp.now(tz="UTC"), periods=len(df), freq="D")
    df.index.name = None
    return df
