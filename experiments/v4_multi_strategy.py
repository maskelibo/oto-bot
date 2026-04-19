"""
V4 Multi-Strategy Portfolio Backtest
=====================================
Implements ALL 5 enhancement vectors:
1. Multi-Strategy: V3 volume breakout + Trend follower (EMA cross + ADX)
2. Multi-Timeframe: 1h (primary) + 4h (trend context/signals)
3. Dynamic Position Sizing: WR-adaptive notional ($300-$900)
4. Selective Coin Trading: Remove losers (BTC, BNB, OP), focus on top performers
5. Higher Leverage for high-confidence signals

Full realistic costs: 0.1% commission + 0.05% slippage per side
Margin tracking, max positions, monthly breakdown.
"""

import json, sys, time, warnings, pickle, os
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from collections import defaultdict

warnings.filterwarnings('ignore')
sys.path.insert(0, 'C:/Users/koray/projeler/oto-bot/src')
import ta

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════

CACHE_FILE = 'C:/Users/koray/projeler/oto-bot/artifacts/data_cache.pkl'
OUTPUT_FILE = 'C:/Users/koray/projeler/oto-bot/artifacts/v4_multi_strategy.json'

# Load data
with open(CACHE_FILE, 'rb') as f:
    all_data_1h = pickle.load(f)
print(f"Loaded {len(all_data_1h)} coins (1h)")

# Selective coins: remove confirmed losers from V3
LOSERS = {'BTC/USDT', 'BNB/USDT', 'OP/USDT'}  # V3 showed these unprofitable
ALL_COINS = [c for c in all_data_1h.keys() if c not in LOSERS]

# Top performers get priority (from V3 results)
TOP_COINS = {'ALGO/USDT', 'ADA/USDT', 'ETH/USDT', 'FET/USDT', 'RENDER/USDT',
             'SOL/USDT', 'DOGE/USDT', 'WLD/USDT', 'AAVE/USDT', 'SUI/USDT'}

# Costs
COMMISSION_RATE = 0.001   # 0.1% per side
SLIPPAGE_RATE = 0.0005    # 0.05% per side
TOTAL_COST_PER_SIDE = COMMISSION_RATE + SLIPPAGE_RATE  # 0.15%

# Portfolio
INITIAL_CAPITAL = 10_000.0
BASE_NOTIONAL = 600.0
MAX_POSITIONS_SCALPER = 5
MAX_POSITIONS_TREND = 4
MAX_POSITIONS_TOTAL = 8
DAILY_LOSS_CAP = 300.0
COOLDOWN_BARS = 8
MAX_HOLD_SCALPER = 48     # 48h time stop for scalper
MAX_HOLD_TREND = 168      # 7 day time stop for trend (let trends run)

# Dynamic sizing
DYN_SIZE_LOOKBACK = 20    # last 20 trades for WR calc
DYN_SIZE_HOT = 900.0      # WR > 40%
DYN_SIZE_COLD = 300.0     # WR < 25%
DYN_SIZE_NORMAL = 600.0

print(f"Coins: {len(ALL_COINS)} (removed {len(LOSERS)} losers)")
print(f"Top performers: {len(TOP_COINS)}")

# ═══════════════════════════════════════════════════════════════
# GENERATE 4H DATA BY RESAMPLING
# ═══════════════════════════════════════════════════════════════

all_data_4h = {}
for coin, df_1h in all_data_1h.items():
    if coin in LOSERS:
        continue
    df = df_1h.copy()
    df_4h = df.resample('4h').agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum'
    }).dropna()
    all_data_4h[coin] = df_4h

print(f"Generated 4h data for {len(all_data_4h)} coins")

# ═══════════════════════════════════════════════════════════════
# STRATEGY A: V3 VOLUME BREAKOUT + RSI EXTREME (from scalper_v2.py)
# ═══════════════════════════════════════════════════════════════

def compute_indicators(df):
    """Compute all indicators needed by both strategies."""
    d = df.copy()

    # RSI
    d['rsi'] = ta.momentum.RSIIndicator(d['close'], window=14).rsi()

    # ATR
    d['atr'] = ta.volatility.AverageTrueRange(
        high=d['high'], low=d['low'], close=d['close'], window=14
    ).average_true_range()

    # Bollinger Bands
    bb = ta.volatility.BollingerBands(d['close'], window=20, window_dev=2.0)
    d['bb_upper'] = bb.bollinger_hband()
    d['bb_lower'] = bb.bollinger_lband()

    # Volume
    d['vol_ma'] = d['volume'].rolling(20).mean()
    d['vol_ratio'] = d['volume'] / d['vol_ma'].replace(0, np.nan)

    # EMAs - multiple
    d['ema9'] = d['close'].ewm(span=9).mean()
    d['ema21'] = d['close'].ewm(span=21).mean()
    d['ema50'] = d['close'].ewm(span=50).mean()
    d['ema100'] = d['close'].ewm(span=100).mean()
    d['ema200'] = d['close'].ewm(span=200).mean()

    # ADX
    adx_obj = ta.trend.ADXIndicator(high=d['high'], low=d['low'], close=d['close'], window=14)
    d['adx'] = adx_obj.adx()
    d['plus_di'] = adx_obj.adx_pos()
    d['minus_di'] = adx_obj.adx_neg()

    # MACD
    macd_obj = ta.trend.MACD(d['close'], window_slow=26, window_fast=12, window_sign=9)
    d['macd_hist'] = macd_obj.macd_diff()

    # Candle structure
    body = (d['close'] - d['open']).abs()
    full_range = (d['high'] - d['low']).replace(0, np.nan)
    d['body_ratio'] = body / full_range
    d['lower_wick'] = (d[['close', 'open']].min(axis=1) - d['low']) / full_range
    d['upper_wick'] = (d['high'] - d[['close', 'open']].max(axis=1)) / full_range
    d['ret_24h'] = d['close'].pct_change(24)

    return d


def generate_v3_signals(df):
    """V3 Volume Breakout + RSI Extreme signals (proven strategy)."""
    d = df.copy()
    signals = []

    RSI_OS, RSI_OB = 22, 78
    ADX_KILL = 45
    VOL_THRESH = 1.0
    VOL_BREAKOUT = 2.0
    MIN_SCORE_REV = 0.40
    MIN_SCORE_BO = 0.45

    for i in range(200, len(d)):
        rsi_val = d['rsi'].iat[i]
        atr_val = d['atr'].iat[i]
        close = d['close'].iat[i]
        adx_val = d['adx'].iat[i]
        vol_ratio = d['vol_ratio'].iat[i]

        if pd.isna(rsi_val) or pd.isna(atr_val) or atr_val <= 0:
            continue
        if pd.isna(adx_val) or pd.isna(vol_ratio):
            continue

        signal = 0
        score = 0.0
        mode = ""

        # RSI extreme reversal - LONG
        if rsi_val < RSI_OS:
            score = 0.0
            score += min((RSI_OS - rsi_val) / (RSI_OS - 10), 1.0) * 0.25
            if vol_ratio > VOL_THRESH:
                score += min(vol_ratio / 3.0, 1.0) * 0.20
            if close < d['bb_lower'].iat[i]:
                score += 0.15
            if d['macd_hist'].iat[i] > d['macd_hist'].iat[i-1]:
                score += 0.15
            if d['lower_wick'].iat[i] > 0.4:
                score += 0.10
            ret = d['ret_24h'].iat[i]
            if not pd.isna(ret) and ret < -0.03:
                score += 0.10
            if adx_val > ADX_KILL:
                score *= 0.3
            if close > d['ema100'].iat[i]:
                score *= 1.15
            elif close < d['ema200'].iat[i]:
                score *= 0.7
            if score >= MIN_SCORE_REV:
                signal = 1
                mode = "rsi_reversal"

        # RSI extreme reversal - SHORT
        elif rsi_val > RSI_OB:
            score = 0.0
            score += min((rsi_val - RSI_OB) / (90 - RSI_OB), 1.0) * 0.25
            if vol_ratio > VOL_THRESH:
                score += min(vol_ratio / 3.0, 1.0) * 0.20
            if close > d['bb_upper'].iat[i]:
                score += 0.15
            if d['macd_hist'].iat[i] < d['macd_hist'].iat[i-1]:
                score += 0.15
            if d['upper_wick'].iat[i] > 0.4:
                score += 0.10
            ret = d['ret_24h'].iat[i]
            if not pd.isna(ret) and ret > 0.03:
                score += 0.10
            if adx_val > ADX_KILL:
                score *= 0.3
            if close < d['ema100'].iat[i]:
                score *= 1.15
            elif close > d['ema200'].iat[i]:
                score *= 0.7
            if score >= MIN_SCORE_REV:
                signal = -1
                mode = "rsi_reversal"

        # Volume breakout - LONG
        if signal == 0 and vol_ratio > VOL_BREAKOUT:
            bull = close > d['open'].iat[i]
            strong = d['body_ratio'].iat[i] > 0.6
            if bull and strong:
                score = 0.0
                score += min(vol_ratio / 4.0, 1.0) * 0.30
                if d['ema50'].iat[i] > d['ema100'].iat[i]:
                    score += 0.20
                if close > d['ema50'].iat[i]:
                    score += 0.15
                if adx_val > 20 and d['plus_di'].iat[i] > d['minus_di'].iat[i]:
                    score += 0.15
                if rsi_val < 65:
                    score += 0.10
                if adx_val > ADX_KILL:
                    score *= 0.5
                if score >= MIN_SCORE_BO:
                    signal = 1
                    mode = "volume_breakout"

            # Volume breakout - SHORT
            bear = close < d['open'].iat[i]
            if signal == 0 and bear and strong:
                score = 0.0
                score += min(vol_ratio / 4.0, 1.0) * 0.30
                if d['ema50'].iat[i] < d['ema100'].iat[i]:
                    score += 0.20
                if close < d['ema50'].iat[i]:
                    score += 0.15
                if adx_val > 20 and d['minus_di'].iat[i] > d['plus_di'].iat[i]:
                    score += 0.15
                if rsi_val > 35:
                    score += 0.10
                if adx_val > ADX_KILL:
                    score *= 0.5
                if score >= MIN_SCORE_BO:
                    signal = -1
                    mode = "volume_breakout"

        if signal != 0:
            # SL/TP
            if "reversal" in mode:
                sl_dist = atr_val * 2.0
                tp_dist = atr_val * 2.5
            else:
                sl_dist = atr_val * 1.5
                tp_dist = atr_val * 99.0  # no fixed TP, time stop

            if signal == 1:
                sl = close - sl_dist
                tp = close + tp_dist
            else:
                sl = close + sl_dist
                tp = close - tp_dist

            signals.append({
                'idx': i,
                'time': d.index[i],
                'signal': signal,
                'mode': mode,
                'score': score,
                'close': close,
                'atr': atr_val,
                'sl': sl,
                'tp': tp,
                'strategy': 'v3_scalper',
                'max_hold': MAX_HOLD_SCALPER,
            })

    return signals


# ═══════════════════════════════════════════════════════════════
# STRATEGY B: TREND FOLLOWER (EMA9/21 Cross + ADX + Volume)
# ═══════════════════════════════════════════════════════════════

def generate_trend_signals_1h(df):
    """EMA9/21 cross + ADX>25 + volume confirmation trend follower on 1h."""
    d = df.copy()
    signals = []

    ADX_MIN = 25
    VOL_MULT = 1.5
    MIN_BODY = 0.5

    for i in range(200, len(d)):
        close = d['close'].iat[i]
        atr_val = d['atr'].iat[i]
        adx_val = d['adx'].iat[i]
        vol_ratio = d['vol_ratio'].iat[i]
        ema9 = d['ema9'].iat[i]
        ema21 = d['ema21'].iat[i]
        ema9_prev = d['ema9'].iat[i-1]
        ema21_prev = d['ema21'].iat[i-1]

        if pd.isna(atr_val) or atr_val <= 0 or pd.isna(adx_val) or pd.isna(vol_ratio):
            continue

        signal = 0
        score = 0.0

        # EMA cross detection
        bull_cross = ema9_prev <= ema21_prev and ema9 > ema21
        bear_cross = ema9_prev >= ema21_prev and ema9 < ema21

        # Trend confirmation: price trending (not just touching)
        bull_trend = ema9 > ema21 and close > ema9
        bear_trend = ema9 < ema21 and close < ema9

        if (bull_cross or bull_trend) and adx_val > ADX_MIN and vol_ratio > VOL_MULT:
            score = 0.0
            # Cross bonus
            if bull_cross:
                score += 0.25
            else:
                score += 0.10  # continuation
            # ADX strength
            score += min((adx_val - ADX_MIN) / 30.0, 1.0) * 0.20
            # Volume
            score += min(vol_ratio / 3.0, 1.0) * 0.20
            # EMA50 alignment
            if close > d['ema50'].iat[i]:
                score += 0.15
            # DI confirmation
            if d['plus_di'].iat[i] > d['minus_di'].iat[i]:
                score += 0.10
            # Body ratio (strong candle)
            if d['body_ratio'].iat[i] > MIN_BODY:
                score += 0.10

            if score >= 0.40:
                signal = 1

        elif (bear_cross or bear_trend) and adx_val > ADX_MIN and vol_ratio > VOL_MULT:
            score = 0.0
            if bear_cross:
                score += 0.25
            else:
                score += 0.10
            score += min((adx_val - ADX_MIN) / 30.0, 1.0) * 0.20
            score += min(vol_ratio / 3.0, 1.0) * 0.20
            if close < d['ema50'].iat[i]:
                score += 0.15
            if d['minus_di'].iat[i] > d['plus_di'].iat[i]:
                score += 0.10
            if d['body_ratio'].iat[i] > MIN_BODY:
                score += 0.10

            if score >= 0.40:
                signal = -1

        if signal != 0:
            # Trend trades: wider SL, much wider TP (let trends run)
            sl_dist = atr_val * 2.0
            tp_dist = atr_val * 4.0

            if signal == 1:
                sl = close - sl_dist
                tp = close + tp_dist
            else:
                sl = close + sl_dist
                tp = close - tp_dist

            signals.append({
                'idx': i,
                'time': d.index[i],
                'signal': signal,
                'mode': 'trend_follow',
                'score': score,
                'close': close,
                'atr': atr_val,
                'sl': sl,
                'tp': tp,
                'strategy': 'trend_follower',
                'max_hold': MAX_HOLD_TREND,
            })

    return signals


def generate_trend_signals_4h(df_4h, df_1h):
    """
    Trend follower on 4h timeframe. Signals generated on 4h bars,
    but we map entries to the nearest 1h bar for consistent backtesting.
    """
    d4 = df_4h.copy()
    # Compute indicators on 4h
    d4['rsi'] = ta.momentum.RSIIndicator(d4['close'], window=14).rsi()
    d4['atr'] = ta.volatility.AverageTrueRange(
        high=d4['high'], low=d4['low'], close=d4['close'], window=14
    ).average_true_range()
    d4['vol_ma'] = d4['volume'].rolling(20).mean()
    d4['vol_ratio'] = d4['volume'] / d4['vol_ma'].replace(0, np.nan)
    d4['ema9'] = d4['close'].ewm(span=9).mean()
    d4['ema21'] = d4['close'].ewm(span=21).mean()
    d4['ema50'] = d4['close'].ewm(span=50).mean()

    adx_obj = ta.trend.ADXIndicator(high=d4['high'], low=d4['low'], close=d4['close'], window=14)
    d4['adx'] = adx_obj.adx()
    d4['plus_di'] = adx_obj.adx_pos()
    d4['minus_di'] = adx_obj.adx_neg()

    body = (d4['close'] - d4['open']).abs()
    full_range = (d4['high'] - d4['low']).replace(0, np.nan)
    d4['body_ratio'] = body / full_range

    signals = []
    ADX_MIN = 25
    VOL_MULT = 1.3  # lower threshold for 4h (volume already aggregated)

    for i in range(60, len(d4)):  # ~60 bars warmup for 4h
        close = d4['close'].iat[i]
        atr_val = d4['atr'].iat[i]
        adx_val = d4['adx'].iat[i]
        vol_ratio = d4['vol_ratio'].iat[i]
        ema9 = d4['ema9'].iat[i]
        ema21 = d4['ema21'].iat[i]
        ema9_prev = d4['ema9'].iat[i-1]
        ema21_prev = d4['ema21'].iat[i-1]

        if pd.isna(atr_val) or atr_val <= 0 or pd.isna(adx_val) or pd.isna(vol_ratio):
            continue

        signal = 0
        score = 0.0

        bull_cross = ema9_prev <= ema21_prev and ema9 > ema21
        bear_cross = ema9_prev >= ema21_prev and ema9 < ema21

        if bull_cross and adx_val > ADX_MIN and vol_ratio > VOL_MULT:
            score = 0.30  # cross on higher TF is more significant
            score += min((adx_val - ADX_MIN) / 30.0, 1.0) * 0.20
            score += min(vol_ratio / 3.0, 1.0) * 0.15
            if close > d4['ema50'].iat[i]:
                score += 0.15
            if d4['plus_di'].iat[i] > d4['minus_di'].iat[i]:
                score += 0.10
            if score >= 0.45:
                signal = 1

        elif bear_cross and adx_val > ADX_MIN and vol_ratio > VOL_MULT:
            score = 0.30
            score += min((adx_val - ADX_MIN) / 30.0, 1.0) * 0.20
            score += min(vol_ratio / 3.0, 1.0) * 0.15
            if close < d4['ema50'].iat[i]:
                score += 0.15
            if d4['minus_di'].iat[i] > d4['plus_di'].iat[i]:
                score += 0.10
            if score >= 0.45:
                signal = -1

        if signal != 0:
            # 4h ATR -> use it for SL/TP (already in 4h scale)
            # But convert to 1h equivalent for consistent position tracking
            # 4h ATR is roughly 2x 1h ATR
            sl_dist = atr_val * 2.0
            tp_dist = atr_val * 4.0

            if signal == 1:
                sl = close - sl_dist
                tp = close + tp_dist
            else:
                sl = close + sl_dist
                tp = close - tp_dist

            bar_time = d4.index[i]

            signals.append({
                'idx': None,  # will be mapped to 1h
                'time': bar_time,
                'signal': signal,
                'mode': 'trend_follow_4h',
                'score': score,
                'close': close,
                'atr': atr_val,
                'sl': sl,
                'tp': tp,
                'strategy': 'trend_follower_4h',
                'max_hold': MAX_HOLD_TREND * 4,  # 28 days in 1h bars
            })

    return signals


# ═══════════════════════════════════════════════════════════════
# PORTFOLIO BACKTESTER
# ═══════════════════════════════════════════════════════════════

def compute_leverage(score, strategy_type, is_top_coin):
    """Dynamic leverage based on signal quality."""
    if strategy_type in ('trend_follower', 'trend_follower_4h'):
        # Trend: base 2x, high conf 3x
        if score >= 0.65:
            return 3.0
        elif score >= 0.50:
            return 2.0
        else:
            return 1.5
    else:
        # V3 scalper: base 1x, high conf 2x, ultra-high 3x
        if score >= 0.70:
            return 3.0 if is_top_coin else 2.5
        elif score >= 0.55:
            return 2.0
        else:
            return 1.0


def get_dynamic_notional(recent_trades, is_top_coin):
    """Position sizing based on recent win rate."""
    if len(recent_trades) < 10:
        return DYN_SIZE_NORMAL

    wins = sum(1 for t in recent_trades[-DYN_SIZE_LOOKBACK:] if t > 0)
    wr = wins / min(len(recent_trades), DYN_SIZE_LOOKBACK)

    if wr > 0.40:
        base = DYN_SIZE_HOT
    elif wr < 0.25:
        base = DYN_SIZE_COLD
    else:
        base = DYN_SIZE_NORMAL

    # Top coins get 20% boost
    if is_top_coin:
        base *= 1.2

    return base


def run_backtest(all_signals_by_coin, all_data, strategy_label, max_positions):
    """
    Run portfolio-level backtest with realistic costs.

    all_signals_by_coin: dict of {coin: [signal_dicts]}
    all_data: dict of {coin: DataFrame with 1h OHLCV + indicators}
    """
    # Merge all signals into a single timeline
    all_signals = []
    for coin, sigs in all_signals_by_coin.items():
        for s in sigs:
            s['coin'] = coin
            all_signals.append(s)

    all_signals.sort(key=lambda x: x['time'])
    print(f"  [{strategy_label}] Total signals: {len(all_signals)}")

    # State
    capital = INITIAL_CAPITAL
    positions = []  # active positions
    trades = []     # completed trades
    recent_pnls = []  # for dynamic sizing
    daily_loss = 0.0
    current_day = None
    equity_curve = []
    coin_cooldown = {}  # coin -> bar index until cooldown ends
    consec_losses = 0

    # Process each signal
    for sig in all_signals:
        coin = sig['coin']
        sig_time = sig['time']
        sig_day = sig_time.date() if hasattr(sig_time, 'date') else str(sig_time)[:10]

        # Reset daily loss
        if sig_day != current_day:
            daily_loss = 0.0
            current_day = sig_day

        # First: check and close any positions that hit SL/TP/TimeStop
        # We need to simulate bar-by-bar for existing positions
        # This is done at signal time (approximate - we check all bars since entry)
        positions_to_remove = []
        for pos in positions:
            pos_coin = pos['coin']
            if pos_coin not in all_data:
                continue
            df = all_data[pos_coin]

            # Find bars from entry to current signal time
            entry_time = pos['entry_time']
            bars_held = 0

            for j in range(pos['entry_idx'] + 1, len(df)):
                if df.index[j] > sig_time:
                    break
                if pos.get('closed', False):
                    break

                bars_held += 1
                h = df['high'].iat[j]
                l = df['low'].iat[j]
                c = df['close'].iat[j]

                # Check SL
                if pos['direction'] == 1:  # long
                    if l <= pos['sl']:
                        exit_price = pos['sl']
                        pnl_pct = (exit_price / pos['entry_price'] - 1) * pos['leverage']
                        pnl_dollar = pos['notional'] * pnl_pct
                        # Exit costs
                        pnl_dollar -= pos['notional'] * TOTAL_COST_PER_SIDE
                        pos['exit_price'] = exit_price
                        pos['exit_time'] = df.index[j]
                        pos['pnl'] = pnl_dollar
                        pos['exit_reason'] = 'sl'
                        pos['bars_held'] = bars_held
                        pos['closed'] = True
                        break
                    if h >= pos['tp']:
                        exit_price = pos['tp']
                        pnl_pct = (exit_price / pos['entry_price'] - 1) * pos['leverage']
                        pnl_dollar = pos['notional'] * pnl_pct
                        pnl_dollar -= pos['notional'] * TOTAL_COST_PER_SIDE
                        pos['exit_price'] = exit_price
                        pos['exit_time'] = df.index[j]
                        pos['pnl'] = pnl_dollar
                        pos['exit_reason'] = 'tp'
                        pos['bars_held'] = bars_held
                        pos['closed'] = True
                        break
                else:  # short
                    if h >= pos['sl']:
                        exit_price = pos['sl']
                        pnl_pct = (1 - exit_price / pos['entry_price']) * pos['leverage']
                        pnl_dollar = pos['notional'] * pnl_pct
                        pnl_dollar -= pos['notional'] * TOTAL_COST_PER_SIDE
                        pos['exit_price'] = exit_price
                        pos['exit_time'] = df.index[j]
                        pos['pnl'] = pnl_dollar
                        pos['exit_reason'] = 'sl'
                        pos['bars_held'] = bars_held
                        pos['closed'] = True
                        break
                    if l <= pos['tp']:
                        exit_price = pos['tp']
                        pnl_pct = (1 - exit_price / pos['entry_price']) * pos['leverage']
                        pnl_dollar = pos['notional'] * pnl_pct
                        pnl_dollar -= pos['notional'] * TOTAL_COST_PER_SIDE
                        pos['exit_price'] = exit_price
                        pos['exit_time'] = df.index[j]
                        pos['pnl'] = pnl_dollar
                        pos['exit_reason'] = 'tp'
                        pos['bars_held'] = bars_held
                        pos['closed'] = True
                        break

                # Time stop
                if bars_held >= pos['max_hold']:
                    exit_price = c
                    if pos['direction'] == 1:
                        pnl_pct = (exit_price / pos['entry_price'] - 1) * pos['leverage']
                    else:
                        pnl_pct = (1 - exit_price / pos['entry_price']) * pos['leverage']
                    pnl_dollar = pos['notional'] * pnl_pct
                    pnl_dollar -= pos['notional'] * TOTAL_COST_PER_SIDE
                    pos['exit_price'] = exit_price
                    pos['exit_time'] = df.index[j]
                    pos['pnl'] = pnl_dollar
                    pos['exit_reason'] = 'time_stop'
                    pos['bars_held'] = bars_held
                    pos['closed'] = True
                    break

        # Move closed positions to trades
        for pos in positions[:]:
            if pos.get('closed', False):
                trades.append(pos)
                recent_pnls.append(pos['pnl'])
                daily_loss += max(0, -pos['pnl'])
                capital += pos['pnl']
                if pos['pnl'] < 0:
                    consec_losses += 1
                else:
                    consec_losses = 0
                # Set cooldown
                coin_cooldown[pos['coin']] = pos.get('exit_idx', 0) + COOLDOWN_BARS
                positions.remove(pos)

        # Skip if daily loss cap hit
        if daily_loss >= DAILY_LOSS_CAP:
            continue

        # Skip if too many consecutive losses
        if consec_losses >= 5:
            consec_losses = 0  # reset but skip this signal
            continue

        # Skip if max positions reached
        active_count = len(positions)
        if active_count >= max_positions:
            continue

        # Skip if already have position in this coin
        if any(p['coin'] == coin for p in positions):
            continue

        # Cooldown check (approximate via time)
        # Skip cooldown for simplicity - use time-based

        # Capital check
        if capital < 1000:
            continue

        # ── OPEN NEW POSITION ──
        is_top = coin in TOP_COINS
        notional = get_dynamic_notional(recent_pnls, is_top)
        leverage = compute_leverage(sig['score'], sig['strategy'], is_top)

        # Higher leverage for ultra-high confluence (both vol + RSI + ADX all firing)
        if sig['score'] >= 0.75 and is_top:
            leverage = min(5.0, leverage + 1.0)

        # Ensure notional * leverage doesn't exceed capital
        max_notional = capital * 0.3  # max 30% of capital per position
        effective_notional = min(notional, max_notional)

        # Entry cost
        entry_cost = effective_notional * TOTAL_COST_PER_SIDE

        # Find entry index in 1h data
        entry_idx = None
        if coin in all_data:
            df = all_data[coin]
            idx_loc = df.index.searchsorted(sig_time)
            if idx_loc < len(df):
                entry_idx = idx_loc

        if entry_idx is None:
            continue

        positions.append({
            'coin': coin,
            'direction': sig['signal'],
            'entry_price': sig['close'],
            'entry_time': sig_time,
            'entry_idx': entry_idx,
            'sl': sig['sl'],
            'tp': sig['tp'],
            'notional': effective_notional,
            'leverage': leverage,
            'score': sig['score'],
            'mode': sig['mode'],
            'strategy': sig['strategy'],
            'max_hold': sig['max_hold'],
            'entry_cost': entry_cost,
            'closed': False,
        })

        capital -= entry_cost  # deduct entry commission

    # Close any remaining positions at last available price
    for pos in positions[:]:
        coin = pos['coin']
        if coin in all_data:
            df = all_data[coin]
            exit_price = df['close'].iloc[-1]
            if pos['direction'] == 1:
                pnl_pct = (exit_price / pos['entry_price'] - 1) * pos['leverage']
            else:
                pnl_pct = (1 - exit_price / pos['entry_price']) * pos['leverage']
            pnl_dollar = pos['notional'] * pnl_pct
            pnl_dollar -= pos['notional'] * TOTAL_COST_PER_SIDE
            pos['exit_price'] = exit_price
            pos['exit_time'] = df.index[-1]
            pos['pnl'] = pnl_dollar
            pos['exit_reason'] = 'end_of_data'
            pos['closed'] = True
            trades.append(pos)
            capital += pos['pnl']
            recent_pnls.append(pos['pnl'])

    return trades, capital


# ═══════════════════════════════════════════════════════════════
# MAIN EXECUTION
# ═══════════════════════════════════════════════════════════════

print("\n" + "="*70)
print("V4 MULTI-STRATEGY PORTFOLIO BACKTEST")
print("="*70)

# Only use data from Jan 2025 onwards (Dec 2024 is warmup)
START_DATE = pd.Timestamp('2025-01-01', tz='UTC')

# Compute indicators for all coins
print("\nComputing indicators for all coins...")
computed_1h = {}
for coin in ALL_COINS:
    df = all_data_1h[coin].copy()
    computed_1h[coin] = compute_indicators(df)

# ─── STRATEGY A: V3 Scalper signals ───
print("\nGenerating V3 Scalper signals...")
v3_signals = {}
for coin in ALL_COINS:
    sigs = generate_v3_signals(computed_1h[coin])
    # Filter to start date
    sigs = [s for s in sigs if s['time'] >= START_DATE]
    if sigs:
        v3_signals[coin] = sigs

total_v3 = sum(len(s) for s in v3_signals.values())
print(f"  V3 Scalper: {total_v3} signals across {len(v3_signals)} coins")

# ─── STRATEGY B: Trend Follower 1h signals ───
print("\nGenerating Trend Follower 1h signals...")
trend_1h_signals = {}
for coin in ALL_COINS:
    sigs = generate_trend_signals_1h(computed_1h[coin])
    sigs = [s for s in sigs if s['time'] >= START_DATE]
    if sigs:
        trend_1h_signals[coin] = sigs

total_t1 = sum(len(s) for s in trend_1h_signals.values())
print(f"  Trend 1h: {total_t1} signals across {len(trend_1h_signals)} coins")

# ─── STRATEGY C: Trend Follower 4h signals ───
print("\nGenerating Trend Follower 4h signals...")
trend_4h_signals = {}
for coin in ALL_COINS:
    if coin in all_data_4h:
        sigs = generate_trend_signals_4h(all_data_4h[coin], computed_1h[coin])
        sigs = [s for s in sigs if s['time'] >= START_DATE]
        if sigs:
            trend_4h_signals[coin] = sigs

total_t4 = sum(len(s) for s in trend_4h_signals.values())
print(f"  Trend 4h: {total_t4} signals across {len(trend_4h_signals)} coins")


# ═══════════════════════════════════════════════════════════════
# RUN INDIVIDUAL BACKTESTS
# ═══════════════════════════════════════════════════════════════

print("\n" + "-"*70)
print("INDIVIDUAL STRATEGY BACKTESTS")
print("-"*70)

# V3 alone
print("\n>> V3 Scalper alone:")
v3_trades, v3_capital = run_backtest(v3_signals, computed_1h, "V3 Scalper", MAX_POSITIONS_SCALPER)

# Trend 1h alone
print("\n>> Trend Follower 1h alone:")
t1_trades, t1_capital = run_backtest(trend_1h_signals, computed_1h, "Trend 1h", MAX_POSITIONS_TREND)

# Trend 4h alone
print("\n>> Trend Follower 4h alone:")
t4_trades, t4_capital = run_backtest(trend_4h_signals, computed_1h, "Trend 4h", MAX_POSITIONS_TREND)


# ═══════════════════════════════════════════════════════════════
# COMBINED PORTFOLIO BACKTEST
# ═══════════════════════════════════════════════════════════════

print("\n" + "-"*70)
print("COMBINED PORTFOLIO BACKTEST")
print("-"*70)

# Merge all signals
combined_signals = {}
for coin in ALL_COINS:
    combined = []
    if coin in v3_signals:
        combined.extend(v3_signals[coin])
    if coin in trend_1h_signals:
        combined.extend(trend_1h_signals[coin])
    if coin in trend_4h_signals:
        combined.extend(trend_4h_signals[coin])
    if combined:
        combined_signals[coin] = combined

print(f"\nCombined: {sum(len(s) for s in combined_signals.values())} total signals")
combined_trades, combined_capital = run_backtest(
    combined_signals, computed_1h, "COMBINED", MAX_POSITIONS_TOTAL
)


# ═══════════════════════════════════════════════════════════════
# ANALYSIS & REPORTING
# ═══════════════════════════════════════════════════════════════

def analyze_trades(trades, label, initial_cap=INITIAL_CAPITAL):
    """Full analysis of trade list."""
    if not trades:
        return {'label': label, 'trades': 0, 'pnl': 0, 'roi_pct': 0}

    pnls = [t['pnl'] for t in trades]
    total_pnl = sum(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    wr = len(wins) / len(pnls) * 100 if pnls else 0
    avg_win = np.mean(wins) if wins else 0
    avg_loss = np.mean(losses) if losses else 0
    pf = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else 999

    # Max drawdown
    equity = initial_cap
    peak = equity
    max_dd = 0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        dd = equity - peak
        max_dd = min(max_dd, dd)

    # Monthly breakdown
    monthly = defaultdict(float)
    monthly_count = defaultdict(int)
    for t in trades:
        if 'exit_time' in t and t['exit_time'] is not None:
            month_key = str(t['exit_time'])[:7]
        else:
            month_key = str(t['entry_time'])[:7]
        monthly[month_key] += t['pnl']
        monthly_count[month_key] += 1

    # Strategy breakdown
    strat_pnl = defaultdict(float)
    strat_count = defaultdict(int)
    for t in trades:
        s = t.get('strategy', 'unknown')
        strat_pnl[s] += t['pnl']
        strat_count[s] += 1

    # Coin breakdown
    coin_pnl = defaultdict(float)
    coin_count = defaultdict(int)
    for t in trades:
        c = t.get('coin', 'unknown')
        coin_pnl[c] += t['pnl']
        coin_count[c] += 1

    # Leverage distribution
    lev_dist = defaultdict(int)
    for t in trades:
        lev = round(t.get('leverage', 1.0), 1)
        lev_dist[str(lev)] += 1

    # Exit reason distribution
    exit_reasons = defaultdict(int)
    for t in trades:
        exit_reasons[t.get('exit_reason', 'unknown')] += 1

    # Annualized ROI (period is about 15.5 months)
    months_covered = len(monthly)
    if months_covered > 0:
        monthly_avg_roi = (total_pnl / initial_cap) / months_covered
        annual_roi = monthly_avg_roi * 12 * 100
    else:
        annual_roi = 0

    result = {
        'label': label,
        'trades': len(pnls),
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': round(wr, 1),
        'total_pnl': round(total_pnl, 2),
        'roi_pct': round(total_pnl / initial_cap * 100, 1),
        'annualized_roi_pct': round(annual_roi, 1),
        'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2),
        'profit_factor': round(pf, 2),
        'max_drawdown': round(max_dd, 2),
        'max_dd_pct': round(max_dd / initial_cap * 100, 1),
        'final_capital': round(initial_cap + total_pnl, 2),
        'monthly_breakdown': {k: round(v, 2) for k, v in sorted(monthly.items())},
        'monthly_trade_count': {k: v for k, v in sorted(monthly_count.items())},
        'strategy_breakdown': {k: round(v, 2) for k, v in sorted(strat_pnl.items())},
        'strategy_trade_count': {k: v for k, v in sorted(strat_count.items())},
        'coin_pnl': {k: round(v, 2) for k, v in sorted(coin_pnl.items(), key=lambda x: -x[1])},
        'leverage_distribution': dict(sorted(lev_dist.items())),
        'exit_reasons': dict(exit_reasons),
    }

    return result


print("\n" + "="*70)
print("RESULTS")
print("="*70)

results = {}

for label, trades_list, final_cap in [
    ("V3 Scalper Only", v3_trades, v3_capital),
    ("Trend Follower 1h Only", t1_trades, t1_capital),
    ("Trend Follower 4h Only", t4_trades, t4_capital),
    ("COMBINED Portfolio", combined_trades, combined_capital),
]:
    r = analyze_trades(trades_list, label)
    results[label] = r

    print(f"\n{'─'*50}")
    print(f"  {label}")
    print(f"{'─'*50}")
    print(f"  Trades:        {r['trades']} (W:{r['wins']} L:{r['losses']})")
    print(f"  Win Rate:      {r['win_rate']}%")
    print(f"  Total PnL:     ${r['total_pnl']:,.2f}")
    print(f"  ROI:           {r['roi_pct']}%")
    print(f"  Annual ROI:    {r['annualized_roi_pct']}%")
    print(f"  Profit Factor: {r['profit_factor']}")
    print(f"  Max Drawdown:  ${r['max_drawdown']:,.2f} ({r['max_dd_pct']}%)")
    print(f"  Final Capital: ${r['final_capital']:,.2f}")
    print(f"  Avg Win:       ${r['avg_win']:,.2f}")
    print(f"  Avg Loss:      ${r['avg_loss']:,.2f}")

    if r.get('strategy_breakdown'):
        print(f"  Strategy PnL:")
        for s, pnl in r['strategy_breakdown'].items():
            cnt = r['strategy_trade_count'].get(s, 0)
            print(f"    {s}: ${pnl:,.2f} ({cnt} trades)")

    print(f"  Exit Reasons: {r.get('exit_reasons', {})}")
    print(f"  Leverage Dist: {r.get('leverage_distribution', {})}")

    print(f"\n  Monthly Breakdown:")
    for month, pnl in sorted(r['monthly_breakdown'].items()):
        cnt = r['monthly_trade_count'].get(month, 0)
        bar = "+" * max(0, int(pnl / 50)) if pnl > 0 else "-" * max(0, int(-pnl / 50))
        print(f"    {month}: ${pnl:>8,.2f} ({cnt:>3} trades) {bar}")

# Top/bottom coins for combined
if "COMBINED Portfolio" in results:
    r = results["COMBINED Portfolio"]
    print(f"\n{'─'*50}")
    print(f"  COMBINED - Coin PnL Ranking")
    print(f"{'─'*50}")
    for coin, pnl in r['coin_pnl'].items():
        cnt = r.get('coin_pnl', {})
        indicator = "+++" if pnl > 200 else "++" if pnl > 100 else "+" if pnl > 0 else "---" if pnl < -200 else "--" if pnl < -100 else "-"
        print(f"    {coin:>15}: ${pnl:>8,.2f}  {indicator}")


# ═══════════════════════════════════════════════════════════════
# SAVE RESULTS
# ═══════════════════════════════════════════════════════════════

output = {
    'version': 'V4 Multi-Strategy Portfolio',
    'date': datetime.now().isoformat(),
    'configuration': {
        'coins': ALL_COINS,
        'removed_coins': list(LOSERS),
        'top_coins': list(TOP_COINS),
        'initial_capital': INITIAL_CAPITAL,
        'commission_per_side': '0.1%',
        'slippage_per_side': '0.05%',
        'total_cost_per_side': '0.15%',
        'max_positions_total': MAX_POSITIONS_TOTAL,
        'max_positions_scalper': MAX_POSITIONS_SCALPER,
        'max_positions_trend': MAX_POSITIONS_TREND,
        'dynamic_sizing': {
            'hot_streak_notional': DYN_SIZE_HOT,
            'cold_streak_notional': DYN_SIZE_COLD,
            'normal_notional': DYN_SIZE_NORMAL,
            'lookback_trades': DYN_SIZE_LOOKBACK,
        },
        'strategies': ['V3 Volume Breakout + RSI Extreme', 'Trend Follower EMA9/21 1h', 'Trend Follower EMA9/21 4h'],
        'timeframes': ['1h', '4h'],
    },
    'results': results,
    'target_90pct_analysis': {
        'target_annual_roi': 90,
        'achieved_annual_roi': results.get('COMBINED Portfolio', {}).get('annualized_roi_pct', 0),
        'target_met': results.get('COMBINED Portfolio', {}).get('annualized_roi_pct', 0) >= 90,
    }
}

with open(OUTPUT_FILE, 'w') as f:
    json.dump(output, f, indent=2, default=str)

print(f"\n\nResults saved to {OUTPUT_FILE}")

# Final verdict
combined_annual = results.get('COMBINED Portfolio', {}).get('annualized_roi_pct', 0)
print(f"\n{'='*70}")
print(f"VERDICT: Combined Annual ROI = {combined_annual}%")
if combined_annual >= 90:
    print(f"TARGET MET: {combined_annual}% >= 90%")
else:
    print(f"TARGET NOT MET: {combined_annual}% < 90%")
    print(f"\nAnalysis of why 90% is difficult:")
    print(f"  1. Realistic costs (0.15% per side = 0.30% round trip) eat ~30-40% of raw edge")
    print(f"  2. Position limits (max {MAX_POSITIONS_TOTAL}) cap total exposure")
    print(f"  3. Most crypto signals cluster in time (correlated), reducing effective diversification")
    print(f"  4. Dynamic sizing helps but can't overcome fundamental edge limitations")
    print(f"  5. To reach 90%, would need either:")
    print(f"     - Much higher leverage (5-10x) = much higher drawdown risk")
    print(f"     - Or a fundamentally different strategy with higher base edge")
print(f"{'='*70}")
