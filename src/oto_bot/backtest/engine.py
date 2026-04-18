from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import uuid

import numpy as np
import pandas as pd

from oto_bot.core.models import ExperimentResult
from oto_bot.strategies.base import Strategy, StrategyContext


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class BacktestConfig:
    # --- Core ---
    fee_bps: float = 8.0
    slippage_bps: float = 4.0           # Fallback; overridden by volume-based
    initial_capital: float = 100_000.0
    risk_per_trade: float = 0.01        # 1% risk per trade
    use_position_sizing: bool = True
    use_volume_slippage: bool = True     # Use volume-proportional slippage

    # --- Leverage / Margin ---
    leverage: float = 1.0               # 1.0 = no leverage

    # --- Trailing Stop (ADX-based) ---
    trail_activation_atr: float = 1.0   # Activate after 1×ATR in profit
    trail_distance_atr: float = 1.5     # Default trail distance
    use_adx_trail: bool = True          # Dynamic trail based on ADX

    # --- Partial Take Profit ---
    partial_tp_atr: float = 2.0         # Close 50% at 2×ATR profit
    partial_tp_fraction: float = 0.5    # Fraction to close

    # --- Consecutive Loss Management ---
    consec_halve: int = 3               # After N losses → size × 0.5
    consec_stop: int = 5                # After N losses → skip for stop_bars
    consec_stop_bars: int = 10          # Bars to skip after consec_stop

    # --- Daily Loss Circuit Breaker ---
    daily_loss_limit: float = 0.05      # 5% daily loss → halt

    # --- Profit Protection (Ratcheting) ---
    protect_levels: list[tuple[float, float]] = field(
        default_factory=lambda: [(0.30, 0.8), (0.60, 0.6), (1.00, 0.4)]
    )

    # --- Funding Rate (Crypto Perpetuals) ---
    funding_rate: float = 0.0001        # 0.01% per interval
    funding_interval_h: float = 8.0
    apply_funding: bool = False         # Off by default; enable for perps

    # --- Position Limits ---
    max_total_open: int = 8
    max_same_direction: int = 4
    max_per_symbol: int = 1

    # --- Strategy Family Hint ---
    strategy_family: str = "day"        # "day", "swing", "scalper"


# ---------------------------------------------------------------------------
# Trade record
# ---------------------------------------------------------------------------

@dataclass
class PartialExit:
    bar_idx: int
    price: float
    size: float
    pnl: float


@dataclass
class TradeRecord:
    entry_idx: int
    exit_idx: int
    entry_price: float
    exit_price: float
    direction: int               # +1 long, -1 short
    size: float
    pnl: float
    return_pct: float
    duration_bars: int
    # --- Enhanced fields ---
    symbol: str = ""
    leverage: float = 1.0
    margin_used: float = 0.0
    partial_exits: list[PartialExit] = field(default_factory=list)
    trail_activated: bool = False
    exit_reason: str = "signal"  # sl / tp / trail / signal / circuit_breaker / partial_tp / end_of_data
    funding_cost: float = 0.0


# ---------------------------------------------------------------------------
# Open Position (internal tracking)
# ---------------------------------------------------------------------------

@dataclass
class _OpenPosition:
    symbol: str
    entry_idx: int
    entry_price: float
    direction: int
    size: float                  # Current remaining size (decreases on partial)
    original_size: float
    margin: float                # Margin blocked = notional / leverage
    stop_loss: float
    take_profit: float
    atr_at_entry: float
    peak_price: float            # Best price seen (for trailing)
    trail_active: bool = False
    trail_price: float = 0.0     # Current trailing stop price
    partial_taken: bool = False
    partial_exits: list[PartialExit] = field(default_factory=list)
    funding_accumulated: float = 0.0
    last_funding_bar: int = 0


# ---------------------------------------------------------------------------
# Volume-based slippage
# ---------------------------------------------------------------------------

def apply_volume_slippage(
    price: float,
    direction: int,
    pos_value: float,
    candle_volume: float,
) -> float:
    """Volume-proportional slippage model."""
    if candle_volume <= 0 or price <= 0:
        return price
    volume_ratio = pos_value / (candle_volume * price)
    if volume_ratio < 0.001:
        slip = 0.0001
    elif volume_ratio < 0.005:
        slip = 0.0003
    elif volume_ratio < 0.02:
        slip = 0.0008
    elif volume_ratio < 0.05:
        slip = 0.002
    elif volume_ratio < 0.10:
        slip = 0.008
    else:
        slip = 0.025
    return price * (1 + slip) if direction == 1 else price * (1 - slip)


# ---------------------------------------------------------------------------
# Backtest Engine
# ---------------------------------------------------------------------------

class BacktestEngine:
    """Production backtest engine with:

    - Mark-to-market capital management
    - ADX-based trailing stops
    - Partial take profit
    - Consecutive loss management
    - Daily loss circuit breaker
    - Profit protection (ratcheting)
    - Funding rate costs
    - Position limits
    - Volume-based realistic slippage
    - Multi-symbol support
    - Walk-forward validation & Monte Carlo simulation
    """

    def __init__(self, config: BacktestConfig | None = None) -> None:
        self.config = config or BacktestConfig()

    # ------------------------------------------------------------------
    # Main run — supports single-symbol AND multi-symbol
    # ------------------------------------------------------------------

    def run(
        self,
        strategy: Strategy,
        data: pd.DataFrame,
        context: StrategyContext,
    ) -> ExperimentResult:
        """Run backtest. If data contains a 'symbol' column with multiple
        symbols, runs multi-symbol mode. Otherwise single-symbol."""
        if "symbol" in data.columns and data["symbol"].nunique() > 1:
            return self._run_multi(strategy, data, context)
        return self._run_single(strategy, data, context)

    # ------------------------------------------------------------------
    # Single-symbol core loop
    # ------------------------------------------------------------------

    def _run_single(
        self,
        strategy: Strategy,
        data: pd.DataFrame,
        context: StrategyContext,
        *,
        symbol: str = "",
        shared_state: dict[str, Any] | None = None,
    ) -> ExperimentResult:
        cfg = self.config
        fee_rate = cfg.fee_bps / 10_000.0

        # --- Shared state for multi-symbol (capital, positions, trades) ---
        if shared_state is not None:
            capital: float = shared_state["capital"]
            open_positions: list[_OpenPosition] = shared_state["positions"]
            all_trades: list[TradeRecord] = shared_state["trades"]
            equity_curve: list[float] = shared_state["equity_curve"]
            consec_losses: int = shared_state["consec_losses"]
            skip_until_bar: int = shared_state["skip_until_bar"]
            daily_start_capital: float = shared_state["daily_start_capital"]
            current_day: str = shared_state["current_day"]
            halted_today: bool = shared_state["halted_today"]
        else:
            capital = cfg.initial_capital
            open_positions = []
            all_trades = []
            equity_curve = [capital]
            consec_losses = 0
            skip_until_bar = 0
            daily_start_capital = capital
            current_day = ""
            halted_today = False

        if not symbol:
            symbol = context.symbol if hasattr(context, "symbol") else ""

        # 1. Generate signals
        frame = strategy.generate_signals(data, context).dropna().copy()
        if frame.empty or len(frame) < 2:
            if shared_state is not None:
                self._sync_shared(shared_state, capital, open_positions,
                                  all_trades, equity_curve, consec_losses,
                                  skip_until_bar, daily_start_capital,
                                  current_day, halted_today)
            return self._empty_result(strategy, context)

        has_stops = "stop_loss" in frame.columns and "take_profit" in frame.columns
        has_atr = "atr" in frame.columns
        has_adx = "adx" in frame.columns
        has_volume = "volume" in frame.columns

        # Determine timeframe hours-per-bar for funding calculation
        hours_per_bar = self._estimate_hours_per_bar(frame)

        # 2. Simulate bar by bar
        for i in range(1, len(frame)):
            row = frame.iloc[i]
            prev_row = frame.iloc[i - 1]
            signal = int(prev_row["signal"]) if not np.isnan(prev_row["signal"]) else 0
            close = float(row["close"])
            high = float(row["high"]) if "high" in frame.columns else close
            low = float(row["low"]) if "low" in frame.columns else close
            atr = float(row["atr"]) if has_atr and not np.isnan(row["atr"]) else close * 0.02
            adx = float(row["adx"]) if has_adx and not np.isnan(row["adx"]) else 0.0
            volume = float(row["volume"]) if has_volume and not np.isnan(row["volume"]) else 0.0

            # --- Daily circuit breaker check ---
            bar_day = self._get_bar_day(row, frame.index[i])
            if bar_day != current_day:
                current_day = bar_day
                daily_start_capital = capital
                halted_today = False

            if not halted_today:
                daily_pnl_pct = (capital - daily_start_capital) / daily_start_capital if daily_start_capital > 0 else 0.0
                if daily_pnl_pct <= -cfg.daily_loss_limit:
                    halted_today = True

            # --- Funding rate deduction on open positions ---
            if cfg.apply_funding:
                for pos in open_positions:
                    bars_since = i - pos.last_funding_bar
                    hours_elapsed = bars_since * hours_per_bar
                    if hours_elapsed >= cfg.funding_interval_h:
                        intervals = int(hours_elapsed // cfg.funding_interval_h)
                        notional = pos.size * close
                        cost = notional * cfg.funding_rate * intervals
                        pos.funding_accumulated += cost
                        capital -= cost
                        pos.last_funding_bar = i

            # --- Process open positions for this symbol ---
            positions_to_close: list[tuple[_OpenPosition, float, str]] = []
            for pos in open_positions:
                if pos.symbol != symbol:
                    continue

                # Update peak price
                if pos.direction == 1:
                    pos.peak_price = max(pos.peak_price, high)
                else:
                    pos.peak_price = min(pos.peak_price, low)

                exit_price = 0.0
                exit_reason = ""

                # --- Trailing stop logic ---
                profit_distance = (
                    (pos.peak_price - pos.entry_price) * pos.direction
                )
                if profit_distance >= cfg.trail_activation_atr * pos.atr_at_entry:
                    pos.trail_active = True
                    trail_mult = self._get_trail_multiplier(adx, cfg)
                    trail_dist = trail_mult * pos.atr_at_entry
                    if pos.direction == 1:
                        new_trail = pos.peak_price - trail_dist
                        pos.trail_price = max(pos.trail_price, new_trail)
                    else:
                        new_trail = pos.peak_price + trail_dist
                        pos.trail_price = (
                            min(pos.trail_price, new_trail)
                            if pos.trail_price > 0
                            else new_trail
                        )

                # --- Partial take profit ---
                if not pos.partial_taken and pos.atr_at_entry > 0:
                    pnl_distance = (close - pos.entry_price) * pos.direction
                    if pnl_distance >= cfg.partial_tp_atr * pos.atr_at_entry:
                        partial_size = pos.original_size * cfg.partial_tp_fraction
                        partial_pnl = partial_size * pnl_distance
                        # Apply fee on partial exit
                        partial_cost = partial_size * close * fee_rate
                        partial_pnl -= partial_cost
                        capital += partial_pnl
                        pos.size -= partial_size
                        pos.margin = (pos.size * close) / cfg.leverage
                        pos.partial_taken = True
                        pos.partial_exits.append(PartialExit(
                            bar_idx=i, price=close,
                            size=partial_size, pnl=partial_pnl,
                        ))

                # --- Check stop loss ---
                if has_stops:
                    sl = pos.stop_loss
                    tp = pos.take_profit
                    if pos.direction == 1:
                        if sl > 0 and low <= sl:
                            exit_price = sl
                            exit_reason = "sl"
                        elif tp > 0 and high >= tp:
                            exit_price = tp
                            exit_reason = "tp"
                    else:
                        if sl > 0 and high >= sl:
                            exit_price = sl
                            exit_reason = "sl"
                        elif tp > 0 and low <= tp:
                            exit_price = tp
                            exit_reason = "tp"

                # --- Check trailing stop ---
                if not exit_reason and pos.trail_active:
                    if pos.direction == 1 and low <= pos.trail_price:
                        exit_price = pos.trail_price
                        exit_reason = "trail"
                    elif pos.direction == -1 and high >= pos.trail_price:
                        exit_price = pos.trail_price
                        exit_reason = "trail"

                # --- Signal reversal or flat → exit ---
                if not exit_reason and signal != pos.direction:
                    exit_price = close
                    exit_reason = "signal"

                if exit_reason:
                    positions_to_close.append((pos, exit_price, exit_reason))

            # --- Execute closures ---
            for pos, exit_px, reason in positions_to_close:
                self._close_position(
                    pos, exit_px, reason, i, capital, fee_rate,
                    cfg, volume, all_trades,
                )
                pnl = all_trades[-1].pnl
                capital += pnl
                open_positions.remove(pos)
                # Track consecutive losses
                if pnl < 0:
                    consec_losses += 1
                    if consec_losses >= cfg.consec_stop:
                        skip_until_bar = i + cfg.consec_stop_bars
                else:
                    consec_losses = 0

            # --- New entry logic ---
            can_enter = (
                signal != 0
                and not halted_today
                and i >= skip_until_bar
                and not any(p.symbol == symbol for p in open_positions)
            )

            if can_enter:
                # Position limit checks
                total_open = len(open_positions)
                same_dir = sum(1 for p in open_positions if p.direction == signal)
                per_sym = sum(1 for p in open_positions if p.symbol == symbol)

                if (
                    total_open >= cfg.max_total_open
                    or same_dir >= cfg.max_same_direction
                    or per_sym >= cfg.max_per_symbol
                ):
                    can_enter = False

            if can_enter:
                direction = signal
                entry_price_raw = close

                # --- Compute free capital (mark-to-market) ---
                unrealized = self._calc_unrealized(open_positions, close)
                effective_capital = capital + unrealized
                margin_used = sum(p.margin for p in open_positions)
                free_capital = max(effective_capital - margin_used, 0.0)

                if free_capital <= 0:
                    can_enter = False

            if can_enter:
                # --- Profit protection ratchet ---
                size_mult = 1.0
                cumulative_return = (capital - cfg.initial_capital) / cfg.initial_capital
                for threshold, mult in sorted(cfg.protect_levels, key=lambda x: x[0], reverse=True):
                    if cumulative_return >= threshold:
                        size_mult = mult
                        break

                # --- Consecutive loss halving ---
                if consec_losses >= cfg.consec_halve:
                    size_mult *= 0.5

                # --- Position sizing ---
                sl_val = 0.0
                tp_val = 0.0
                if has_stops:
                    sl_raw = prev_row.get("stop_loss", np.nan)
                    tp_raw = prev_row.get("take_profit", np.nan)
                    sl_val = float(sl_raw) if not np.isnan(sl_raw) and sl_raw > 0 else 0.0
                    tp_val = float(tp_raw) if not np.isnan(tp_raw) and tp_raw > 0 else 0.0

                if cfg.use_position_sizing and "position_size" in frame.columns:
                    raw_size = float(prev_row["position_size"])
                elif cfg.use_position_sizing:
                    stop_dist = abs(entry_price_raw - sl_val) if sl_val > 0 else atr * 1.5
                    if stop_dist <= 0:
                        stop_dist = entry_price_raw * 0.02
                    raw_size = (free_capital * cfg.risk_per_trade) / stop_dist
                else:
                    raw_size = free_capital / entry_price_raw

                size = raw_size * size_mult
                if size <= 0:
                    equity_curve.append(self._snapshot_equity(capital, open_positions, close))
                    continue

                # Notional value and margin
                notional = size * entry_price_raw
                margin = notional / cfg.leverage

                # Ensure we don't exceed free capital
                if margin > free_capital:
                    size = (free_capital * cfg.leverage) / entry_price_raw
                    notional = size * entry_price_raw
                    margin = notional / cfg.leverage

                # --- Apply slippage ---
                if cfg.use_volume_slippage and volume > 0:
                    entry_price_final = apply_volume_slippage(
                        entry_price_raw, direction, notional, volume,
                    )
                else:
                    slip = cfg.slippage_bps / 10_000.0
                    entry_price_final = (
                        entry_price_raw * (1 + slip)
                        if direction == 1
                        else entry_price_raw * (1 - slip)
                    )

                # Entry fee
                entry_cost = notional * fee_rate
                capital -= entry_cost

                pos = _OpenPosition(
                    symbol=symbol,
                    entry_idx=i,
                    entry_price=entry_price_final,
                    direction=direction,
                    size=size,
                    original_size=size,
                    margin=margin,
                    stop_loss=sl_val,
                    take_profit=tp_val,
                    atr_at_entry=atr,
                    peak_price=entry_price_final,
                    last_funding_bar=i,
                )
                open_positions.append(pos)

            # --- Equity snapshot (mark-to-market) ---
            equity_curve.append(self._snapshot_equity(capital, open_positions, close))

        # --- Close remaining open positions at end ---
        final_close = float(frame.iloc[-1]["close"])
        final_volume = (
            float(frame.iloc[-1]["volume"])
            if has_volume and not np.isnan(frame.iloc[-1]["volume"])
            else 0.0
        )
        for pos in list(open_positions):
            if pos.symbol != symbol:
                continue
            self._close_position(
                pos, final_close, "end_of_data", len(frame) - 1,
                capital, fee_rate, cfg, final_volume, all_trades,
            )
            capital += all_trades[-1].pnl
            open_positions.remove(pos)

        if equity_curve:
            equity_curve[-1] = self._snapshot_equity(capital, open_positions, final_close)

        # --- Sync shared state ---
        if shared_state is not None:
            self._sync_shared(shared_state, capital, open_positions,
                              all_trades, equity_curve, consec_losses,
                              skip_until_bar, daily_start_capital,
                              current_day, halted_today)
            return self._empty_result(strategy, context)  # Metrics computed at multi level

        # 3. Compute metrics
        return self._compute_result(
            strategy, context, cfg, all_trades, equity_curve, frame,
        )

    # ------------------------------------------------------------------
    # Multi-symbol orchestration
    # ------------------------------------------------------------------

    def _run_multi(
        self,
        strategy: Strategy,
        data: pd.DataFrame,
        context: StrategyContext,
    ) -> ExperimentResult:
        cfg = self.config
        symbols = data["symbol"].unique().tolist()

        shared: dict[str, Any] = {
            "capital": cfg.initial_capital,
            "positions": [],
            "trades": [],
            "equity_curve": [cfg.initial_capital],
            "consec_losses": 0,
            "skip_until_bar": 0,
            "daily_start_capital": cfg.initial_capital,
            "current_day": "",
            "halted_today": False,
        }

        # Process each symbol through the shared state
        for sym in symbols:
            sym_data = data[data["symbol"] == sym].copy().reset_index(drop=True)
            sym_context = StrategyContext(
                market=context.market,
                strategy_family=context.strategy_family,
                symbol=sym,
                timeframe=context.timeframe,
                params=context.params,
            )
            self._run_single(
                strategy, sym_data, sym_context,
                symbol=sym, shared_state=shared,
            )

        # Close any positions still open
        # (already handled in _run_single per symbol)

        trades = shared["trades"]
        equity_curve = shared["equity_curve"]

        if not trades:
            return self._empty_result(strategy, context)

        # Build a minimal frame for regime detection
        frame = data.sort_values("close").head(0)  # dummy
        if "close" in data.columns:
            frame = data.drop_duplicates(subset=["close"]).head(len(data))

        return self._compute_result(
            strategy, context, cfg, trades, equity_curve, data,
        )

    # ------------------------------------------------------------------
    # Position close helper
    # ------------------------------------------------------------------

    def _close_position(
        self,
        pos: _OpenPosition,
        exit_price: float,
        exit_reason: str,
        bar_idx: int,
        capital: float,
        fee_rate: float,
        cfg: BacktestConfig,
        volume: float,
        trades: list[TradeRecord],
    ) -> None:
        """Close position, apply slippage/fees, append TradeRecord."""
        notional_exit = pos.size * exit_price

        # Apply slippage on exit
        exit_dir = -pos.direction  # Closing is opposite direction
        if cfg.use_volume_slippage and volume > 0:
            exit_price_final = apply_volume_slippage(
                exit_price, exit_dir, notional_exit, volume,
            )
        else:
            slip = cfg.slippage_bps / 10_000.0
            exit_price_final = (
                exit_price * (1 + slip)
                if exit_dir == 1
                else exit_price * (1 - slip)
            )

        # P&L calculation
        raw_return = pos.direction * (exit_price_final - pos.entry_price) / pos.entry_price
        exit_fee = pos.size * exit_price_final * fee_rate
        # Total partial P&L already credited to capital
        partial_pnl_sum = sum(pe.pnl for pe in pos.partial_exits)
        remaining_pnl = pos.size * pos.entry_price * raw_return - exit_fee - pos.funding_accumulated
        total_pnl = remaining_pnl  # Partial exits already settled in capital

        total_return = (
            (partial_pnl_sum + remaining_pnl)
            / (pos.original_size * pos.entry_price)
            if pos.original_size > 0 and pos.entry_price > 0
            else 0.0
        )

        trades.append(TradeRecord(
            entry_idx=pos.entry_idx,
            exit_idx=bar_idx,
            entry_price=pos.entry_price,
            exit_price=exit_price_final,
            direction=pos.direction,
            size=pos.original_size,
            pnl=remaining_pnl,
            return_pct=total_return,
            duration_bars=bar_idx - pos.entry_idx,
            symbol=pos.symbol,
            leverage=cfg.leverage,
            margin_used=pos.margin,
            partial_exits=list(pos.partial_exits),
            trail_activated=pos.trail_active,
            exit_reason=exit_reason,
            funding_cost=pos.funding_accumulated,
        ))

    # ------------------------------------------------------------------
    # ADX-based trailing multiplier
    # ------------------------------------------------------------------

    @staticmethod
    def _get_trail_multiplier(adx: float, cfg: BacktestConfig) -> float:
        if not cfg.use_adx_trail or adx <= 0:
            return cfg.trail_distance_atr
        if adx >= 50:
            return 2.5
        elif adx >= 40:
            return 2.0
        elif adx >= 30:
            return 1.5
        else:
            return 1.0

    # ------------------------------------------------------------------
    # Mark-to-market helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _calc_unrealized(positions: list[_OpenPosition], price: float) -> float:
        total = 0.0
        for pos in positions:
            total += pos.direction * (price - pos.entry_price) * pos.size
        return total

    @staticmethod
    def _snapshot_equity(
        capital: float,
        positions: list[_OpenPosition],
        price: float,
    ) -> float:
        unrealized = sum(
            pos.direction * (price - pos.entry_price) * pos.size
            for pos in positions
        )
        return max(capital + unrealized, 0.0)

    # ------------------------------------------------------------------
    # Day detection for circuit breaker
    # ------------------------------------------------------------------

    @staticmethod
    def _get_bar_day(row: pd.Series, idx: Any) -> str:
        """Extract date string from bar for daily grouping."""
        # Try index first (common: DatetimeIndex)
        if isinstance(idx, pd.Timestamp):
            return idx.strftime("%Y-%m-%d")
        # Try 'date' or 'datetime' columns
        for col in ("date", "datetime", "timestamp"):
            if col in row.index:
                val = row[col]
                if isinstance(val, (pd.Timestamp, str)):
                    return str(val)[:10]
        return ""

    # ------------------------------------------------------------------
    # Estimate bar duration for funding
    # ------------------------------------------------------------------

    @staticmethod
    def _estimate_hours_per_bar(frame: pd.DataFrame) -> float:
        """Best-effort hours per bar from the index or known columns."""
        if isinstance(frame.index, pd.DatetimeIndex) and len(frame) >= 2:
            delta = (frame.index[-1] - frame.index[0]).total_seconds()
            return (delta / (len(frame) - 1)) / 3600.0
        # Fallback: assume 1-hour bars
        return 1.0

    # ------------------------------------------------------------------
    # Shared state sync
    # ------------------------------------------------------------------

    @staticmethod
    def _sync_shared(
        shared: dict[str, Any],
        capital: float,
        positions: list[_OpenPosition],
        trades: list[TradeRecord],
        equity_curve: list[float],
        consec_losses: int,
        skip_until_bar: int,
        daily_start_capital: float,
        current_day: str,
        halted_today: bool,
    ) -> None:
        shared["capital"] = capital
        shared["positions"] = positions
        shared["trades"] = trades
        shared["equity_curve"] = equity_curve
        shared["consec_losses"] = consec_losses
        shared["skip_until_bar"] = skip_until_bar
        shared["daily_start_capital"] = daily_start_capital
        shared["current_day"] = current_day
        shared["halted_today"] = halted_today

    # ------------------------------------------------------------------
    # Compute result metrics
    # ------------------------------------------------------------------

    def _compute_result(
        self,
        strategy: Strategy,
        context: StrategyContext,
        cfg: BacktestConfig,
        trades: list[TradeRecord],
        equity_curve: list[float],
        frame: pd.DataFrame,
    ) -> ExperimentResult:
        equity_arr = np.array(equity_curve)
        total_trades = len(trades)

        if total_trades == 0:
            return self._empty_result(strategy, context)

        trade_returns = np.array([t.return_pct for t in trades])
        trade_pnls = np.array([t.pnl for t in trades])
        trade_durations = np.array([t.duration_bars for t in trades])

        wins = trade_returns[trade_returns > 0]
        losses = trade_returns[trade_returns <= 0]

        final_equity = float(equity_arr[-1]) if len(equity_arr) > 0 else cfg.initial_capital
        roi = (final_equity - cfg.initial_capital) / cfg.initial_capital

        win_rate = len(wins) / total_trades if total_trades > 0 else 0.0

        gross_profit = float(trade_pnls[trade_pnls > 0].sum()) if len(trade_pnls[trade_pnls > 0]) > 0 else 0.0
        gross_loss = abs(float(trade_pnls[trade_pnls < 0].sum())) if len(trade_pnls[trade_pnls < 0]) > 0 else 1e-9
        profit_factor = gross_profit / gross_loss

        # Max drawdown
        peak = np.maximum.accumulate(equity_arr)
        drawdown = equity_arr / peak - 1.0
        max_drawdown = float(drawdown.min())

        # CAGR (assume 252 trading days)
        n_bars = len(frame)
        years = max(n_bars / 252.0, 1 / 252.0)
        ending = max(final_equity, 1e-9)
        cagr = (ending / cfg.initial_capital) ** (1.0 / years) - 1.0

        calmar = cagr / abs(max_drawdown) if max_drawdown != 0 else 0.0

        # Sharpe
        mean_ret = float(trade_returns.mean())
        std_ret = float(trade_returns.std()) if len(trade_returns) > 1 else 1.0
        trades_per_year = total_trades / years if years > 0 else total_trades
        sharpe = (mean_ret / std_ret) * np.sqrt(trades_per_year) if std_ret > 0 else 0.0

        # Sortino
        downside = trade_returns[trade_returns < 0]
        downside_std = float(downside.std()) if len(downside) > 1 else 1.0
        sortino = (mean_ret / downside_std) * np.sqrt(trades_per_year) if downside_std > 0 else 0.0

        expectancy = float(trade_returns.mean())
        stability = max(0.0, min(1.0, 1.0 + max_drawdown * 2.0))
        avg_trade_duration = float(trade_durations.mean())

        regime = self._detect_regime(frame)

        # Promotion gate
        promoted = (
            sharpe >= 1.2
            and max_drawdown >= -0.12
            and profit_factor >= 1.2
            and total_trades >= 30
        )

        # Build notes with key risk info
        total_funding = sum(t.funding_cost for t in trades)
        partial_count = sum(1 for t in trades if t.partial_exits)
        trail_count = sum(1 for t in trades if t.trail_activated)
        notes = (
            f"MtM engine | "
            f"trailing={trail_count} | partials={partial_count} | "
            f"funding_cost={total_funding:.2f}"
        )

        return ExperimentResult(
            experiment_id=str(uuid.uuid4()),
            hypothesis_title=strategy.name,
            market=context.market,
            strategy_family=context.strategy_family,
            roi=float(roi),
            win_rate=float(win_rate),
            profit_factor=float(profit_factor),
            sharpe=float(sharpe),
            max_drawdown=float(max_drawdown),
            expectancy=float(expectancy),
            stability_score=float(stability),
            promoted=promoted,
            notes=notes,
            sortino=float(sortino),
            cagr=float(cagr),
            calmar=float(calmar),
            total_trades=total_trades,
            avg_trade_duration=float(avg_trade_duration),
            regime=regime,
            walkforward_sharpe=None,
            montecarlo_95_drawdown=None,
        )

    # ------------------------------------------------------------------
    # Walk-forward validation
    # ------------------------------------------------------------------

    def walk_forward(
        self,
        strategy: Strategy,
        data: pd.DataFrame,
        context: StrategyContext,
        n_splits: int = 5,
    ) -> float:
        """Split data into *n_splits* folds, train on each, test on next.
        Returns average out-of-sample Sharpe ratio."""
        n = len(data)
        fold_size = n // (n_splits + 1)
        if fold_size < 20:
            return 0.0

        oos_sharpes: list[float] = []
        for k in range(n_splits):
            train_end = fold_size * (k + 1)
            test_end = min(train_end + fold_size, n)
            if test_end <= train_end:
                continue

            oos_data = data.iloc[train_end:test_end].copy().reset_index(drop=True)
            if len(oos_data) < 10:
                continue

            result = self.run(strategy, oos_data, context)
            oos_sharpes.append(result.sharpe)

        return float(np.mean(oos_sharpes)) if oos_sharpes else 0.0

    # ------------------------------------------------------------------
    # Monte Carlo simulation
    # ------------------------------------------------------------------

    def monte_carlo(
        self,
        equity_curve: np.ndarray | list[float],
        n_simulations: int = 1000,
    ) -> float:
        """Shuffle trade returns, generate *n_simulations* random equity
        curves, and return the 95th-percentile worst drawdown."""
        eq = np.asarray(equity_curve, dtype=float)
        if len(eq) < 3:
            return 0.0

        returns = np.diff(eq) / eq[:-1]
        returns = returns[np.isfinite(returns)]
        if len(returns) < 2:
            return 0.0

        rng = np.random.default_rng(42)
        worst_drawdowns: list[float] = []

        for _ in range(n_simulations):
            shuffled = rng.permutation(returns)
            sim_equity = np.cumprod(1.0 + shuffled)
            peak = np.maximum.accumulate(sim_equity)
            dd = sim_equity / peak - 1.0
            worst_drawdowns.append(float(dd.min()))

        return float(np.percentile(worst_drawdowns, 5))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_regime(frame: pd.DataFrame) -> str:
        """Simple regime detection based on overall price trend."""
        if "close" not in frame.columns or len(frame) < 20:
            return "unknown"
        close = frame["close"].values
        first_quarter = close[: len(close) // 4].mean()
        last_quarter = close[-(len(close) // 4):].mean()
        change = (last_quarter - first_quarter) / first_quarter if first_quarter > 0 else 0.0
        if change > 0.05:
            return "bull"
        elif change < -0.05:
            return "bear"
        return "sideways"

    def _empty_result(self, strategy: Strategy, context: StrategyContext) -> ExperimentResult:
        """Return a neutral result when no trades are generated."""
        return ExperimentResult(
            experiment_id=str(uuid.uuid4()),
            hypothesis_title=strategy.name,
            market=context.market,
            strategy_family=context.strategy_family,
            roi=0.0,
            win_rate=0.0,
            profit_factor=0.0,
            sharpe=0.0,
            max_drawdown=0.0,
            expectancy=0.0,
            stability_score=0.0,
            promoted=False,
            notes="No trades generated",
            sortino=0.0,
            cagr=0.0,
            calmar=0.0,
            total_trades=0,
            avg_trade_duration=0.0,
            regime="unknown",
            walkforward_sharpe=None,
            montecarlo_95_drawdown=None,
        )
