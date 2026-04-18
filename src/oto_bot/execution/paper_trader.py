"""Paper trading engine with Atlas Trading-grade risk management.

Features:
    - Mark-to-market capital management with margin tracking
    - Full position tracking (entry, SL, trailing stop, peak, leverage, margin)
    - Trailing stop with ATR-based ratcheting
    - Partial take-profit at 2x ATR
    - Consecutive loss management (halve after 3, block after 5)
    - Daily circuit breaker (5% daily loss cap)
    - Position limits (total, directional, per-symbol)
    - Session-based size adjustment
    - Volume-based slippage model
    - Profit protection via ratcheting size reduction
    - Enhanced performance reporting
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PaperPortfolio:
    """Portfolio state for the paper trader."""

    balance: float = 100_000.0
    positions: dict[str, dict[str, Any]] = field(default_factory=dict)
    closed_trades: list[dict[str, Any]] = field(default_factory=list)
    consec_losses: int = 0
    daily_start_balance: float = 100_000.0
    daily_trading_blocked: bool = False
    peak_balance: float = 100_000.0

    # Legacy alias --------------------------------------------------------
    @property
    def trades(self) -> list[dict[str, Any]]:
        """Backward-compatible alias for ``closed_trades``."""
        return self.closed_trades


@dataclass
class PaperTraderConfig:
    """Configuration knobs for the paper trading engine."""

    # Slippage
    slippage_bps_min: float = 0.5
    slippage_bps_max: float = 5.0

    # Latency simulation
    latency_ms_min: float = 10.0
    latency_ms_max: float = 150.0
    simulate_latency: bool = False

    # Daily circuit breaker (percentage of daily start balance)
    daily_loss_cap_pct: float = 0.05

    # Legacy absolute cap (used if > 0 and pct would be larger)
    daily_loss_cap: float = 0.0

    # Position limits
    max_total_open: int = 8
    max_same_direction: int = 4
    max_per_symbol: int = 1

    # Consecutive-loss management
    consec_loss_halve: int = 3
    consec_loss_block: int = 5

    # Default leverage
    default_leverage: float = 1.0

    # Trailing stop / partial TP (ATR multiples)
    trailing_activation_atr: float = 1.0
    trailing_distance_atr: float = 1.5
    partial_tp_atr: float = 2.0
    partial_tp_fraction: float = 0.5

    # Profit-protection ratchet thresholds -> size multipliers
    profit_ratchet: list[tuple[float, float]] = field(
        default_factory=lambda: [
            (1.00, 0.4),   # cumulative return >= 100% -> size * 0.4
            (0.60, 0.6),   # cumulative return >= 60%  -> size * 0.6
            (0.30, 0.8),   # cumulative return >= 30%  -> size * 0.8
        ]
    )

    # Default risk-per-trade as fraction of free capital
    risk_per_trade: float = 0.02

    # Initial capital for cumulative-return calculations
    initial_capital: float = 100_000.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def session_multiplier(hour_utc: int) -> float:
    """Return a position-size multiplier based on session liquidity."""
    if hour_utc < 8:
        return 0.7          # Asia -- noisy / thin
    elif 14 <= hour_utc < 22:
        return 1.2           # US -- liquid
    return 1.0               # Europe


def _apply_volume_slippage(
    price: float,
    direction: str,
    pos_value: float,
    candle_volume: float,
    base_bps_min: float = 0.5,
    base_bps_max: float = 5.0,
) -> float:
    """Volume-aware slippage model.

    ``candle_volume`` is denominated in quote currency (e.g. USD notional).
    When position value is a large share of candle volume the slippage
    increases super-linearly.
    """
    denominator = max(candle_volume * price, 1.0)
    volume_ratio = pos_value / denominator

    # Base random component
    base_bps = random.uniform(base_bps_min, base_bps_max)

    # Scale: small orders get base; large orders get amplified
    if volume_ratio < 0.01:
        multiplier = 1.0
    elif volume_ratio < 0.05:
        multiplier = 1.0 + volume_ratio * 10.0
    elif volume_ratio < 0.20:
        multiplier = 1.5 + volume_ratio * 15.0
    else:
        multiplier = 3.0 + volume_ratio * 30.0

    slip = price * base_bps * multiplier / 10_000.0
    if direction == "long":
        return price + slip   # buying higher
    return price - slip       # selling lower (shorting entry)


# ---------------------------------------------------------------------------
# PaperTrader
# ---------------------------------------------------------------------------

class PaperTrader:
    """Paper trading engine with Atlas Trading-grade risk controls.

    Public API is backward-compatible with the original ``execute()`` method
    while adding rich position management via ``open_position`` /
    ``update_prices`` / ``close_position``.
    """

    def __init__(
        self,
        portfolio: PaperPortfolio | None = None,
        config: PaperTraderConfig | None = None,
    ) -> None:
        self.portfolio = portfolio or PaperPortfolio()
        self.config = config or PaperTraderConfig()

        # Latest known prices per symbol (for mark-to-market)
        self._latest_prices: dict[str, float] = {}

        # Daily tracking
        self._daily_date: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._daily_pnl: float = 0.0
        self._killed: bool = False

        # Circuit breaker event log
        self._circuit_breaker_events: list[dict[str, Any]] = []

        # Full trade history (includes rejected)
        self._trade_history: list[dict[str, Any]] = []

        # Stats accumulators
        self._max_simultaneous_positions: int = 0
        self._trailing_stop_closes: int = 0
        self._partial_tp_count: int = 0
        self._partial_tp_pnl: float = 0.0

    # ------------------------------------------------------------------
    # Capital helpers
    # ------------------------------------------------------------------

    def calc_unrealized_pnl(self) -> float:
        """Total unrealized P&L across all open positions."""
        unrealized = 0.0
        for sym, pos in self.portfolio.positions.items():
            cur = self._latest_prices.get(sym, pos["entry"])
            lev = pos.get("leverage", 1.0)
            if pos["dir"] == "long":
                unrealized += pos["size"] * (cur - pos["entry"]) * lev
            else:
                unrealized += pos["size"] * (pos["entry"] - cur) * lev
        return unrealized

    def calc_margin_used(self) -> float:
        """Total margin currently blocked by open positions."""
        margin = 0.0
        for pos in self.portfolio.positions.values():
            margin += pos["size"] * pos["entry"] / pos.get("leverage", 1.0)
        return margin

    def calc_free_capital(self) -> float:
        """Available capital for new positions (mark-to-market)."""
        margin_used = self.calc_margin_used()
        effective = self.portfolio.balance + self.calc_unrealized_pnl()
        return max(effective - margin_used, 0.0)

    def calc_effective_balance(self) -> float:
        """Balance + unrealized P&L."""
        return self.portfolio.balance + self.calc_unrealized_pnl()

    def _cumulative_return(self) -> float:
        initial = self.config.initial_capital
        if initial <= 0:
            return 0.0
        return (self.calc_effective_balance() - initial) / initial

    # ------------------------------------------------------------------
    # Position sizing
    # ------------------------------------------------------------------

    def calc_position_size(
        self,
        price: float,
        atr: float,
        direction: str,
        leverage: float | None = None,
    ) -> float:
        """Calculate risk-adjusted position size using free capital.

        Returns number of units (size).  Returns 0.0 if trading is blocked.
        """
        if self._is_blocked():
            return 0.0

        leverage = leverage or self.config.default_leverage
        free = self.calc_free_capital()
        risk_amount = free * self.config.risk_per_trade

        # Session multiplier
        hour = datetime.now(timezone.utc).hour
        risk_amount *= session_multiplier(hour)

        # Consecutive-loss halving
        if self.portfolio.consec_losses >= self.config.consec_loss_halve:
            risk_amount *= 0.5

        # Profit-protection ratchet
        cum_ret = self._cumulative_return()
        for threshold, mult in self.config.profit_ratchet:
            if cum_ret >= threshold:
                risk_amount *= mult
                break

        # Size = risk / (ATR * leverage_factor)
        # We risk ``risk_amount``; stop distance is 1.5 * ATR by default
        stop_distance = self.config.trailing_distance_atr * atr
        if stop_distance <= 0:
            return 0.0

        size = risk_amount / (stop_distance * leverage)
        # Clamp so margin does not exceed free capital
        margin_needed = size * price / leverage
        if margin_needed > free:
            size = free * leverage / price

        return max(size, 0.0)

    # ------------------------------------------------------------------
    # Guard checks
    # ------------------------------------------------------------------

    def _is_blocked(self) -> bool:
        """Return True if new entries should be blocked."""
        self._reset_daily_if_needed()
        if self._killed or self.portfolio.daily_trading_blocked:
            return True
        if self.portfolio.consec_losses >= self.config.consec_loss_block:
            return True
        return False

    def _check_position_limits(self, direction: str, symbol: str) -> str | None:
        """Return a rejection reason string, or None if within limits."""
        positions = self.portfolio.positions

        if symbol in positions:
            return f"already_has_position_{symbol}"

        if len(positions) >= self.config.max_total_open:
            return "max_total_open_reached"

        same_dir = sum(1 for p in positions.values() if p["dir"] == direction)
        if same_dir >= self.config.max_same_direction:
            return f"max_same_direction_{direction}"

        return None

    def _check_circuit_breaker(self) -> None:
        """Activate daily circuit breaker if loss exceeds threshold."""
        if self.portfolio.daily_trading_blocked:
            return
        effective = self.calc_effective_balance()
        daily_start = self.portfolio.daily_start_balance

        # Percentage-based cap
        pct_cap = daily_start * self.config.daily_loss_cap_pct
        # Legacy absolute cap
        abs_cap = self.config.daily_loss_cap if self.config.daily_loss_cap > 0 else float("inf")
        cap = min(pct_cap, abs_cap)

        loss = daily_start - effective
        if loss >= cap:
            self.portfolio.daily_trading_blocked = True
            self._killed = True
            event = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "daily_start": round(daily_start, 2),
                "effective_balance": round(effective, 2),
                "loss": round(loss, 2),
                "cap": round(cap, 2),
            }
            self._circuit_breaker_events.append(event)

    # ------------------------------------------------------------------
    # Daily reset
    # ------------------------------------------------------------------

    def _reset_daily_if_needed(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._daily_date:
            self._daily_pnl = 0.0
            self._daily_date = today
            self._killed = False
            self.portfolio.daily_trading_blocked = False
            self.portfolio.daily_start_balance = self.calc_effective_balance()

    # ------------------------------------------------------------------
    # Slippage / latency
    # ------------------------------------------------------------------

    def _apply_slippage(
        self,
        price: float,
        side: str,
        pos_value: float = 0.0,
        candle_volume: float = 0.0,
    ) -> float:
        """Apply slippage -- volume-aware when volume data is available."""
        if candle_volume > 0 and pos_value > 0:
            direction = "long" if side.lower() in ("buy", "long") else "short"
            return _apply_volume_slippage(
                price,
                direction,
                pos_value,
                candle_volume,
                self.config.slippage_bps_min,
                self.config.slippage_bps_max,
            )
        # Fallback: simple random BPS
        bps = random.uniform(self.config.slippage_bps_min, self.config.slippage_bps_max)
        slip = price * bps / 10_000.0
        return price + slip if side.lower() in ("buy", "long") else price - slip

    def _simulate_latency(self) -> float:
        latency_ms = random.uniform(self.config.latency_ms_min, self.config.latency_ms_max)
        if self.config.simulate_latency:
            time.sleep(latency_ms / 1000.0)
        return latency_ms

    # ------------------------------------------------------------------
    # Open position (new API)
    # ------------------------------------------------------------------

    def open_position(
        self,
        symbol: str,
        direction: str,
        price: float,
        size: float,
        atr: float,
        leverage: float | None = None,
        candle_volume: float = 0.0,
    ) -> dict[str, Any]:
        """Open a new position with full tracking.

        Parameters
        ----------
        symbol : str
            Instrument symbol.
        direction : str
            ``"long"`` or ``"short"``.
        price : float
            Requested entry price.
        size : float
            Number of units.  Use ``calc_position_size`` to derive this.
        atr : float
            ATR at entry time (used for SL / trailing / partial TP levels).
        leverage : float, optional
            Position leverage (defaults to config).
        candle_volume : float, optional
            Candle volume for volume-based slippage.

        Returns
        -------
        dict
            Trade record with status ``"filled"`` or ``"rejected"``.
        """
        self._reset_daily_if_needed()
        direction = direction.lower()
        leverage = leverage or self.config.default_leverage

        # --- Guard checks ---
        if self._is_blocked():
            reason = "daily_blocked" if self.portfolio.daily_trading_blocked else "consec_loss_block"
            if self._killed and not self.portfolio.daily_trading_blocked:
                reason = "circuit_breaker"
            return self._reject(symbol, direction, size, price, reason)

        limit_reason = self._check_position_limits(direction, symbol)
        if limit_reason:
            return self._reject(symbol, direction, size, price, limit_reason)

        # --- Latency / slippage ---
        latency_ms = self._simulate_latency()
        pos_value = size * price
        fill_price = self._apply_slippage(price, direction, pos_value, candle_volume)

        # --- Margin check ---
        margin = size * fill_price / leverage
        free = self.calc_free_capital()
        if margin > free:
            return self._reject(symbol, direction, size, price, "insufficient_margin")

        # --- Build position dict ---
        now = datetime.now(timezone.utc)
        sl_distance = self.config.trailing_distance_atr * atr
        if direction == "long":
            sl = fill_price - sl_distance
        else:
            sl = fill_price + sl_distance

        position: dict[str, Any] = {
            "dir": direction,
            "entry": fill_price,
            "sl": sl,
            "trail": sl,
            "peak": fill_price,
            "size": size,
            "leverage": leverage,
            "entry_atr": atr,
            "margin": margin,
            "partial_taken": False,
            "opened_at": now.isoformat(),
        }

        # Deduct margin from balance
        self.portfolio.balance -= margin
        self.portfolio.positions[symbol] = position
        self._latest_prices[symbol] = fill_price

        # Track max simultaneous positions
        self._max_simultaneous_positions = max(
            self._max_simultaneous_positions, len(self.portfolio.positions)
        )

        trade: dict[str, Any] = {
            "status": "filled",
            "action": "open",
            "symbol": symbol,
            "side": direction,
            "qty": size,
            "requested_price": price,
            "fill_price": round(fill_price, 6),
            "slippage": round(fill_price - price, 6),
            "latency_ms": round(latency_ms, 2),
            "timestamp": now.isoformat(),
            "balance_after": round(self.portfolio.balance, 2),
            "margin_blocked": round(margin, 2),
        }
        self._trade_history.append(trade)
        return trade

    # ------------------------------------------------------------------
    # Close position
    # ------------------------------------------------------------------

    def close_position(
        self,
        symbol: str,
        price: float,
        reason: str = "manual",
        fraction: float = 1.0,
        candle_volume: float = 0.0,
    ) -> dict[str, Any]:
        """Close (or partially close) a position.

        Parameters
        ----------
        fraction : float
            Fraction to close (1.0 = full close, 0.5 = half).
        reason : str
            Why the position is being closed (e.g. ``"trailing_stop"``,
            ``"partial_tp"``, ``"signal"``).
        """
        if symbol not in self.portfolio.positions:
            return {"status": "rejected", "reason": "no_position", "symbol": symbol}

        pos = self.portfolio.positions[symbol]
        close_size = pos["size"] * min(max(fraction, 0.0), 1.0)
        if close_size <= 0:
            return {"status": "rejected", "reason": "zero_size", "symbol": symbol}

        latency_ms = self._simulate_latency()
        pos_value = close_size * price
        # Slippage on exit is adverse: longs sell (short side), shorts buy (long side)
        exit_side = "short" if pos["dir"] == "long" else "long"
        fill_price = self._apply_slippage(price, exit_side, pos_value, candle_volume)

        # P&L
        lev = pos.get("leverage", 1.0)
        if pos["dir"] == "long":
            pnl = close_size * (fill_price - pos["entry"]) * lev
        else:
            pnl = close_size * (pos["entry"] - fill_price) * lev

        # Release margin proportionally
        margin_release = pos["margin"] * (close_size / pos["size"])
        self.portfolio.balance += margin_release + pnl

        now = datetime.now(timezone.utc)

        trade: dict[str, Any] = {
            "status": "filled",
            "action": "close",
            "symbol": symbol,
            "side": pos["dir"],
            "qty": round(close_size, 8),
            "requested_price": price,
            "fill_price": round(fill_price, 6),
            "slippage": round(fill_price - price, 6),
            "pnl": round(pnl, 2),
            "reason": reason,
            "latency_ms": round(latency_ms, 2),
            "timestamp": now.isoformat(),
            "balance_after": round(self.portfolio.balance, 2),
        }
        self._trade_history.append(trade)
        self.portfolio.closed_trades.append(trade)

        # Update daily P&L
        self._daily_pnl += pnl

        # Consecutive-loss tracking
        if fraction >= 1.0 or reason in ("trailing_stop", "stop_loss"):
            if pnl < 0:
                self.portfolio.consec_losses += 1
            else:
                self.portfolio.consec_losses = 0

        # Track trailing-stop stat
        if reason == "trailing_stop":
            self._trailing_stop_closes += 1

        # Partial TP stat
        if reason == "partial_tp":
            self._partial_tp_count += 1
            self._partial_tp_pnl += pnl

        # Update position or remove
        remaining = pos["size"] - close_size
        if remaining < 1e-12:
            del self.portfolio.positions[symbol]
        else:
            pos["size"] = remaining
            pos["margin"] -= margin_release

        # Update peak balance
        eff = self.calc_effective_balance()
        if eff > self.portfolio.peak_balance:
            self.portfolio.peak_balance = eff

        # Check circuit breaker after every close
        self._check_circuit_breaker()

        return trade

    # ------------------------------------------------------------------
    # Price update / trailing stop / partial TP engine
    # ------------------------------------------------------------------

    def update_prices(self, prices: dict[str, float]) -> list[dict[str, Any]]:
        """Feed latest prices; returns list of auto-generated close trades.

        This method:
        1. Updates ``_latest_prices``.
        2. For each open position, updates peak and trailing stop.
        3. Triggers partial take-profit if threshold hit.
        4. Triggers stop-loss / trailing-stop exit if breached.
        """
        self._latest_prices.update(prices)
        events: list[dict[str, Any]] = []

        # Iterate over a snapshot since closes mutate the dict
        for symbol in list(self.portfolio.positions.keys()):
            if symbol not in prices:
                continue
            cur = prices[symbol]
            pos = self.portfolio.positions.get(symbol)
            if pos is None:
                continue

            atr = pos["entry_atr"]
            entry = pos["entry"]
            direction = pos["dir"]

            # --- Update peak ---
            if direction == "long":
                if cur > pos["peak"]:
                    pos["peak"] = cur
            else:
                if cur < pos["peak"]:
                    pos["peak"] = cur

            peak = pos["peak"]

            # --- Trailing stop activation & ratchet ---
            activation = self.config.trailing_activation_atr * atr
            trail_dist = self.config.trailing_distance_atr * atr

            if direction == "long":
                if peak >= entry + activation:
                    new_trail = peak - trail_dist
                    if new_trail > pos["trail"]:
                        pos["trail"] = new_trail
            else:
                if peak <= entry - activation:
                    new_trail = peak + trail_dist
                    if new_trail < pos["trail"]:
                        pos["trail"] = new_trail

            # --- Partial take-profit ---
            partial_threshold = self.config.partial_tp_atr * atr
            if not pos["partial_taken"]:
                trigger = False
                if direction == "long" and peak >= entry + partial_threshold:
                    trigger = True
                elif direction == "short" and peak <= entry - partial_threshold:
                    trigger = True
                if trigger:
                    pos["partial_taken"] = True
                    result = self.close_position(
                        symbol,
                        cur,
                        reason="partial_tp",
                        fraction=self.config.partial_tp_fraction,
                    )
                    events.append(result)
                    # Position may still exist with remaining size
                    if symbol not in self.portfolio.positions:
                        continue

            # --- Stop-loss / trailing-stop check ---
            pos = self.portfolio.positions.get(symbol)
            if pos is None:
                continue

            stopped = False
            if direction == "long":
                stop_level = max(pos["sl"], pos["trail"])
                if cur <= stop_level:
                    stopped = True
            else:
                stop_level = min(pos["sl"], pos["trail"])
                if cur >= stop_level:
                    stopped = True

            if stopped:
                reason = "trailing_stop" if pos["trail"] != pos["sl"] else "stop_loss"
                result = self.close_position(symbol, cur, reason=reason)
                events.append(result)

        # Circuit breaker after batch update
        self._check_circuit_breaker()

        return events

    # ------------------------------------------------------------------
    # Legacy execute() -- backward compatible
    # ------------------------------------------------------------------

    def execute(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        atr: float = 0.0,
        leverage: float | None = None,
        candle_volume: float = 0.0,
    ) -> dict[str, Any]:
        """Backward-compatible execution entry point.

        Maps ``buy``/``sell`` to ``open``/``close`` semantics:
        - ``buy`` with no existing position -> open long
        - ``sell`` with existing long -> close position
        - ``sell`` with no position -> open short
        - ``buy`` with existing short -> close position
        """
        self._reset_daily_if_needed()

        side_lower = side.lower()
        existing = self.portfolio.positions.get(symbol)

        # Close path
        if existing:
            if (existing["dir"] == "long" and side_lower == "sell") or \
               (existing["dir"] == "short" and side_lower == "buy"):
                return self.close_position(
                    symbol, price, reason="signal", candle_volume=candle_volume
                )

        # Open path
        direction = "long" if side_lower == "buy" else "short"
        effective_atr = atr if atr > 0 else price * 0.01  # fallback 1% of price
        return self.open_position(
            symbol=symbol,
            direction=direction,
            price=price,
            size=qty,
            atr=effective_atr,
            leverage=leverage,
            candle_volume=candle_volume,
        )

    # ------------------------------------------------------------------
    # Reject helper
    # ------------------------------------------------------------------

    def _reject(
        self,
        symbol: str,
        direction: str,
        qty: float,
        price: float,
        reason: str,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        rec: dict[str, Any] = {
            "status": "rejected",
            "reason": reason,
            "symbol": symbol,
            "side": direction,
            "qty": qty,
            "price": price,
            "timestamp": now.isoformat(),
        }
        self._trade_history.append(rec)
        return rec

    # ------------------------------------------------------------------
    # Query helpers (backward compatible)
    # ------------------------------------------------------------------

    def get_position(self, symbol: str) -> float | dict[str, Any] | None:
        """Return position dict if exists, else 0.0 for backward compat."""
        pos = self.portfolio.positions.get(symbol)
        if pos is None:
            return 0.0
        return pos

    def get_position_detail(self, symbol: str) -> dict[str, Any] | None:
        """Return full position dict or None."""
        return self.portfolio.positions.get(symbol)

    def get_all_positions(self) -> dict[str, dict[str, Any]]:
        return dict(self.portfolio.positions)

    @property
    def daily_pnl(self) -> float:
        self._reset_daily_if_needed()
        return round(self._daily_pnl, 2)

    @property
    def is_killed(self) -> bool:
        self._reset_daily_if_needed()
        return self._killed

    def trade_count_today(self) -> int:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return sum(1 for t in self._trade_history if t["timestamp"].startswith(today))

    # ------------------------------------------------------------------
    # Performance report
    # ------------------------------------------------------------------

    def performance_report(self) -> dict[str, Any]:
        """Comprehensive performance report with Atlas Trading metrics."""
        all_trades = self._trade_history
        closed = self.portfolio.closed_trades

        if not all_trades:
            return {"total_trades": 0, "balance": self.portfolio.balance}

        filled = [t for t in all_trades if t["status"] == "filled"]
        rejected = [t for t in all_trades if t["status"] == "rejected"]
        opens = [t for t in filled if t.get("action") == "open"]
        closes = [t for t in filled if t.get("action") == "close"]

        # Slippage stats
        total_slippage = sum(abs(t.get("slippage", 0)) for t in filled)
        avg_slippage = total_slippage / max(len(filled), 1)
        avg_latency = sum(t.get("latency_ms", 0) for t in filled) / max(len(filled), 1)

        # P&L from closed trades
        total_pnl = sum(t.get("pnl", 0) for t in closed)
        wins = [t for t in closed if t.get("pnl", 0) > 0]
        losses = [t for t in closed if t.get("pnl", 0) < 0]
        win_rate = len(wins) / max(len(closed), 1)

        # Trailing stop effectiveness
        ts_closes = [t for t in closed if t.get("reason") == "trailing_stop"]
        ts_pnl = sum(t.get("pnl", 0) for t in ts_closes)

        # Partial TP stats
        pt_closes = [t for t in closed if t.get("reason") == "partial_tp"]
        pt_pnl = sum(t.get("pnl", 0) for t in pt_closes)

        # Margin utilization
        current_margin = self.calc_margin_used()
        effective_bal = self.calc_effective_balance()
        margin_util = current_margin / max(effective_bal, 1.0)

        # Drawdown
        peak = self.portfolio.peak_balance
        drawdown = (peak - effective_bal) / max(peak, 1.0)

        # Cumulative return
        cum_return = self._cumulative_return()

        # Profit factor
        gross_profit = sum(t.get("pnl", 0) for t in wins)
        gross_loss = abs(sum(t.get("pnl", 0) for t in losses))
        profit_factor = gross_profit / max(gross_loss, 1e-9)

        return {
            # Core
            "total_trades": len(all_trades),
            "filled": len(filled),
            "rejected": len(rejected),
            "open_count": len(opens),
            "close_count": len(closes),
            "balance": round(self.portfolio.balance, 2),
            "effective_balance": round(effective_bal, 2),
            "open_positions": len(self.portfolio.positions),
            "daily_pnl": self.daily_pnl,
            "kill_switch_active": self._killed,
            # P&L
            "total_pnl": round(total_pnl, 2),
            "cumulative_return": round(cum_return, 4),
            "win_rate": round(win_rate, 4),
            "profit_factor": round(profit_factor, 4),
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(-gross_loss, 2),
            # Execution quality
            "avg_slippage": round(avg_slippage, 6),
            "avg_latency_ms": round(avg_latency, 2),
            "total_slippage_cost": round(total_slippage, 6),
            # Risk
            "consecutive_losses": self.portfolio.consec_losses,
            "max_drawdown_pct": round(drawdown, 4),
            "peak_balance": round(peak, 2),
            # Margin
            "margin_used": round(current_margin, 2),
            "margin_utilization": round(margin_util, 4),
            "free_capital": round(self.calc_free_capital(), 2),
            # Position limits
            "max_simultaneous_positions": self._max_simultaneous_positions,
            # Circuit breaker
            "circuit_breaker_events": len(self._circuit_breaker_events),
            "circuit_breaker_log": self._circuit_breaker_events,
            # Trailing stop
            "trailing_stop_closes": self._trailing_stop_closes,
            "trailing_stop_pnl": round(ts_pnl, 2),
            # Partial TP
            "partial_tp_count": self._partial_tp_count,
            "partial_tp_pnl": round(pt_pnl, 2),
        }
