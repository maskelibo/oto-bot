"""Factory that maps market names to the correct DataProvider implementation."""
from __future__ import annotations

from oto_bot.data.crypto import CryptoDataProvider
from oto_bot.data.forex import ForexDataProvider
from oto_bot.data.provider import DataProvider
from oto_bot.data.stocks import StocksDataProvider


class DataProviderFactory:
    """Instantiate the right DataProvider for a given market string."""

    _REGISTRY: dict[str, type[DataProvider] | tuple[type[DataProvider], dict[str, str]]] = {
        "crypto": CryptoDataProvider,
        "forex": ForexDataProvider,
        "us_equities": (StocksDataProvider, {"market": "us_equities"}),
        "bist": (StocksDataProvider, {"market": "bist"}),
    }

    @classmethod
    def get_provider(cls, market: str) -> DataProvider:
        """Return a DataProvider instance for *market*.

        Raises
        ------
        ValueError
            If the market is not recognised.
        """
        entry = cls._REGISTRY.get(market)
        if entry is None:
            supported = ", ".join(sorted(cls._REGISTRY.keys()))
            raise ValueError(
                f"Unknown market '{market}'. Supported: {supported}"
            )

        if isinstance(entry, tuple):
            provider_cls, kwargs = entry
            return provider_cls(**kwargs)  # type: ignore[call-arg]
        return entry()
