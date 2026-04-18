from __future__ import annotations

from typing import Any

from oto_bot.adapters.base import BrokerAdapter


class MockBrokerAdapter(BrokerAdapter):
    name = "mock"

    def __init__(self) -> None:
        self.positions: list[dict[str, Any]] = []

    def place_order(self, symbol: str, side: str, qty: float, order_type: str = "market") -> dict[str, Any]:
        order = {"symbol": symbol, "side": side, "qty": qty, "order_type": order_type, "status": "filled"}
        self.positions.append(order)
        return order

    def get_positions(self) -> list[dict[str, Any]]:
        return self.positions
