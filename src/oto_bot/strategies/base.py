from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import pandas as pd


@dataclass
class StrategyContext:
    """Immutable context passed to every strategy invocation."""

    market: str
    strategy_family: str
    symbol: str
    timeframe: str
    params: dict[str, Any] = field(default_factory=dict)


class Strategy(ABC):
    """Abstract base for all trading strategies.

    Subclasses must implement ``generate_signals`` and ``get_default_params``.
    The base class provides a shared ATR-based position sizing helper.
    """

    name: str

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    def generate_signals(
        self, data: pd.DataFrame, context: StrategyContext
    ) -> pd.DataFrame:
        """Return *data* augmented with at least ``signal``, ``stop_loss``,
        ``take_profit``, and ``position_size`` columns."""
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def get_default_params(cls) -> dict[str, Any]:
        """Return a dict of default tunable parameters for this strategy."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def compute_position_size(
        capital: float,
        risk_per_trade: float,
        atr: float,
        entry_price: float,
        atr_sl_multiplier: float = 1.5,
    ) -> float:
        """Compute position size so that a 1-ATR adverse move risks only
        ``risk_per_trade`` fraction of ``capital``.

        Parameters
        ----------
        capital:
            Total available capital.
        risk_per_trade:
            Fraction of capital risked per trade (e.g. 0.01 = 1 %).
        atr:
            Current ATR value.
        entry_price:
            Expected entry price (used to convert dollar risk to units).
        atr_sl_multiplier:
            How many ATRs define the stop-loss distance.

        Returns
        -------
        float
            Number of units/contracts/shares to trade.  Always >= 0.
        """
        if atr <= 0 or entry_price <= 0:
            return 0.0
        dollar_risk = capital * risk_per_trade
        stop_distance = atr * atr_sl_multiplier
        units = dollar_risk / stop_distance
        return max(units, 0.0)

    # ------------------------------------------------------------------
    # Parameter resolution
    # ------------------------------------------------------------------

    def _resolve_params(self, context: StrategyContext) -> dict[str, Any]:
        """Merge context-supplied params over class defaults."""
        defaults = self.get_default_params()
        defaults.update(context.params)
        return defaults
