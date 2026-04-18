from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BrokerAdapter(ABC):
    name: str

    @abstractmethod
    def place_order(self, symbol: str, side: str, qty: float, order_type: str = "market") -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def get_positions(self) -> list[dict[str, Any]]:
        raise NotImplementedError
