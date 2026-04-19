"""DataDownloader — 5 yıl geçmiş kripto + hisse verisi indirir, parquet cache'ler.

Neden gerekli:
    yfinance intraday hard-limit (1h=730g, 5m=60g) → uzun horizon imkansız.
    ccxt / Binance public API → 1h/4h/1d için 5+ yıl pagination ile erişilebilir.
    yfinance 1d → 10+ yıl stocks için ücretsiz.

Cache: artifacts/data_cache/<market>/<symbol>_<tf>.parquet
Ilk fetch yavaş (paginate), sonra anlık.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger("oto_bot.data.downloader")

_CACHE_ROOT = Path("artifacts/data_cache")

TF_TO_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}


def _cache_path(market: str, symbol: str, timeframe: str) -> Path:
    safe_symbol = symbol.replace("/", "-")
    return _CACHE_ROOT / market / f"{safe_symbol}_{timeframe}.parquet"


def _is_fresh(path: Path, max_age_hours: float = 24.0) -> bool:
    if not path.exists():
        return False
    age = datetime.now(timezone.utc).timestamp() - path.stat().st_mtime
    return age < max_age_hours * 3600


def load_cache(market: str, symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
    path = _cache_path(market, symbol, timeframe)
    if not path.exists():
        return None
    try:
        return pd.read_parquet(path)
    except Exception:
        return None


def save_cache(market: str, symbol: str, timeframe: str, df: pd.DataFrame) -> None:
    path = _cache_path(market, symbol, timeframe)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        df.to_parquet(path)
    except Exception:
        # Fallback: CSV
        path = path.with_suffix(".csv")
        df.to_csv(path, index=False)


# ---------------------------------------------------------------------------
# Crypto (ccxt) — paginated historical
# ---------------------------------------------------------------------------


def fetch_crypto_historical(
    symbol: str,
    timeframe: str = "1h",
    years: float = 5.0,
    exchange_id: str = "binance",
) -> Optional[pd.DataFrame]:
    """ccxt ile pagination ile 5+ yıl geçmişi indir. Binance public API — no key."""
    try:
        import ccxt
    except ImportError:
        logger.warning("ccxt not available")
        return None

    exchange_class = getattr(ccxt, exchange_id)
    exchange = exchange_class({"enableRateLimit": True})
    exchange.load_markets()

    # Convert symbol if needed (e.g., BTCUSDT → BTC/USDT)
    if "/" not in symbol:
        base = symbol.replace("USDT", "")
        symbol_ccxt = f"{base}/USDT"
    else:
        symbol_ccxt = symbol

    if symbol_ccxt not in exchange.markets:
        logger.warning(f"{symbol_ccxt} not on {exchange_id}")
        return None

    tf_min = TF_TO_MINUTES.get(timeframe, 60)
    tf_ms = tf_min * 60 * 1000

    end_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ts = end_ts - int(years * 365 * 24 * 60 * 60 * 1000)

    all_ohlcv: list[list] = []
    since = start_ts
    per_page = 1000
    max_iterations = int(years * 365 * 24 * 60 / tf_min / per_page) + 10

    for _ in range(max_iterations):
        try:
            batch = exchange.fetch_ohlcv(symbol_ccxt, timeframe, since=since, limit=per_page)
        except Exception as e:
            logger.warning(f"ccxt fetch error {symbol_ccxt} {timeframe}: {e}")
            break
        if not batch:
            break
        all_ohlcv.extend(batch)
        last_ts = batch[-1][0]
        if last_ts <= since or last_ts >= end_ts:
            break
        since = last_ts + tf_ms
        time.sleep(exchange.rateLimit / 1000.0)

    if not all_ohlcv:
        return None

    df = pd.DataFrame(all_ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Stocks (yfinance) — 1d unlimited, 1h capped at 730 days
# ---------------------------------------------------------------------------


def fetch_stock_historical(
    symbol: str,
    timeframe: str = "1d",
    years: float = 10.0,
) -> Optional[pd.DataFrame]:
    """yfinance ile ne kadar verirse al. 1d → 10yr, 1h → 2yr (Yahoo sınırı)."""
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance not available")
        return None

    interval_map = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
                    "1h": "1h", "4h": "1h", "1d": "1d"}  # 4h → 1h sonra resample
    interval = interval_map.get(timeframe, timeframe)

    # Yahoo hard limits
    if interval == "1m":
        period = "7d"
    elif interval == "5m":
        period = "60d"
    elif interval in ("15m", "30m"):
        period = "60d"
    elif interval == "1h":
        period = "730d"  # max ≈ 2 yıl
    else:
        period = f"{int(years)}y"  # 1d için 10yr

    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period, interval=interval, auto_adjust=True)
    except Exception as e:
        logger.warning(f"yfinance error {symbol} {timeframe}: {e}")
        return None

    if df is None or df.empty:
        return None

    df = df.reset_index()
    time_col = "Datetime" if "Datetime" in df.columns else "Date"
    df = df.rename(columns={
        time_col: "timestamp", "Open": "open", "High": "high",
        "Low": "low", "Close": "close", "Volume": "volume",
    })
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    # 4h aggregation if requested (pandas 3.0 lowercase 'h')
    if timeframe == "4h" and interval == "1h":
        df = _resample(df, "4h")

    return df[["timestamp", "open", "high", "low", "close", "volume"]]


def _resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    df = df.set_index("timestamp")
    agg = df.resample(rule).agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna()
    return agg.reset_index()


# ---------------------------------------------------------------------------
# Forex (yfinance, via stocks symbol format)
# ---------------------------------------------------------------------------


def fetch_forex_historical(
    pair: str,
    timeframe: str = "1h",
    years: float = 5.0,
) -> Optional[pd.DataFrame]:
    """EURUSD → EURUSD=X"""
    if not pair.endswith("=X"):
        sym = f"{pair.replace('/', '')}=X"
    else:
        sym = pair
    return fetch_stock_historical(sym, timeframe=timeframe, years=years)


# ---------------------------------------------------------------------------
# Unified API
# ---------------------------------------------------------------------------


def download(
    market: str,
    symbol: str,
    timeframe: str,
    years: float = 5.0,
    use_cache: bool = True,
    cache_max_age_hours: float = 24.0,
) -> Optional[pd.DataFrame]:
    """Market'e göre ilgili downloader'ı çağır + cache yönet."""
    cache_path = _cache_path(market, symbol, timeframe)

    if use_cache and _is_fresh(cache_path, cache_max_age_hours):
        df = load_cache(market, symbol, timeframe)
        if df is not None and len(df) > 100:
            logger.info(f"Cache hit: {symbol} {timeframe} ({len(df)} bars)")
            return df

    df: Optional[pd.DataFrame] = None
    if market == "crypto":
        df = fetch_crypto_historical(symbol, timeframe, years=years)
    elif market == "us_equities":
        df = fetch_stock_historical(symbol, timeframe, years=years)
    elif market == "bist":
        # BIST sembolleri Yahoo'da .IS suffix'i ile listelenir (THYAO → THYAO.IS)
        yahoo_sym = symbol if symbol.endswith(".IS") else f"{symbol}.IS"
        df = fetch_stock_historical(yahoo_sym, timeframe, years=years)
    elif market == "forex":
        df = fetch_forex_historical(symbol, timeframe, years=years)

    if df is not None and len(df) > 100:
        save_cache(market, symbol, timeframe, df)
        logger.info(f"Downloaded + cached: {market}/{symbol} {timeframe} ({len(df)} bars)")
        return df

    # Fallback to existing cache even if stale
    if use_cache:
        fallback = load_cache(market, symbol, timeframe)
        if fallback is not None and len(fallback) > 100:
            logger.info(f"Using stale cache for {symbol} {timeframe}")
            return fallback

    return None


# ---------------------------------------------------------------------------
# Bulk backfill (CLI-style usage)
# ---------------------------------------------------------------------------


DEFAULT_UNIVERSE: dict[str, list[str]] = {
    "crypto": ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "ADA/USDT"],
    "us_equities": ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "TSLA", "META"],
    "forex": ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"],
}

DEFAULT_TIMEFRAMES_CRYPTO = ["15m", "1h", "4h", "1d"]  # 15m scalper için
DEFAULT_TIMEFRAMES_STOCKS = ["1h", "1d"]
DEFAULT_TIMEFRAMES_FOREX = ["1h", "1d"]


def backfill_all(years: float = 5.0, verbose: bool = True) -> dict:
    """Tüm default universe için 5 yıl tarihsel veri çek."""
    summary = {"market_counts": {}, "failed": [], "total_bars": 0}

    for market, symbols in DEFAULT_UNIVERSE.items():
        tfs = (
            DEFAULT_TIMEFRAMES_CRYPTO if market == "crypto" else
            DEFAULT_TIMEFRAMES_STOCKS if market == "us_equities" else
            DEFAULT_TIMEFRAMES_FOREX
        )
        for sym in symbols:
            for tf in tfs:
                if verbose:
                    print(f"[{market}] {sym} {tf} ...", end=" ", flush=True)
                df = download(market, sym, tf, years=years, use_cache=False)
                if df is None or len(df) < 100:
                    summary["failed"].append(f"{market}/{sym}/{tf}")
                    if verbose:
                        print("FAIL")
                else:
                    summary["market_counts"][market] = summary["market_counts"].get(market, 0) + 1
                    summary["total_bars"] += len(df)
                    if verbose:
                        print(f"OK ({len(df)} bars)")

    return summary
