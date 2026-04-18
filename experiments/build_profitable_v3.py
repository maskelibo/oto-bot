"""
PHASE 5: Build a profitable strategy based on what we KNOW works

From edge search:
- RSI 20/80 at 24h: +0.79% avg return, 54.7% WR (best raw signal)
- Volume breakout at 8h: +0.26% avg return, 50.4% WR
- BB 2.5 at 1h: 53.1% WR but negative avg return (mean reversion fades quickly)

Strategy: Ultra-selective RSI + Volume + Trend alignment
- Only trade when RSI is extremely oversold/overbought (20/80)
- Confirm with volume spike (information-rich)
- Align with higher timeframe trend (don't counter-trend)
- Hold for 24-48 hours (where the edge actually exists)
- Very few trades but each with real statistical edge
- Wide SL (2 ATR) to avoid noise stops
- Trail profits to capture extended moves
"""
import json, sys, time, warnings, pickle
import numpy as np
import pandas as pd
from datetime import datetime, timezone

warnings.filterwarnings('ignore')
sys.path.insert(0, '/Users/ibrahimpeyman/Documents/oto-bot/src')

import ta

CACHE_FILE = '/Users/ibrahimpeyman/Documents/oto-bot/artifacts/data_cache.pkl'
with open(CACHE_FILE, 'rb') as f:
    all_data = pickle.load(f)
print(f"Loaded {len(all_data)} coins")

COMMISSION_RATE = 0.001
SLIPPAGE_RATE = 0.0005
NOTIONAL = 600.0
MAX_POSITIONS = 6
DAILY_LOSS_CAP = 300.0
INITIAL_CAPITAL = 10000.0


def generate_signals_v3(df, params):
    """
    Ultra-selective signal generation.
    Only fires when multiple independent edge sources align.
    """
    d = df.copy()

    # ── Indicators ──────────────────────────────────────
    # RSI
    rsi = ta.momentum.RSIIndicator(d['close'], window=params['rsi_period']).rsi()
    d['rsi'] = rsi

    # ATR
    atr_ind = ta.volatility.AverageTrueRange(high=d['high'], low=d['low'], close=d['close'], window=14)
    d['atr'] = atr_ind.average_true_range()
    d['natr'] = d['atr'] / d['close'] * 100

    # Bollinger Bands
    bb = ta.volatility.BollingerBands(d['close'], window=20, window_dev=2.0)
    d['bb_lower'] = bb.bollinger_lband()
    d['bb_upper'] = bb.bollinger_hband()
    d['bb_mid'] = bb.bollinger_mavg()

    # Volume
    d['vol_ma'] = d['volume'].rolling(20).mean()
    d['vol_ratio'] = d['volume'] / d['vol_ma'].replace(0, np.nan)

    # Trend: EMA 50 and 100
    d['ema50'] = d['close'].ewm(span=50).mean()
    d['ema100'] = d['close'].ewm(span=100).mean()
    d['ema200'] = d['close'].ewm(span=200).mean()

    # ADX
    adx_ind = ta.trend.ADXIndicator(high=d['high'], low=d['low'], close=d['close'], window=14)
    d['adx'] = adx_ind.adx()
    d['plus_di'] = adx_ind.adx_pos()
    d['minus_di'] = adx_ind.adx_neg()

    # MACD
    macd_ind = ta.trend.MACD(d['close'], window_slow=26, window_fast=12, window_sign=9)
    d['macd_hist'] = macd_ind.macd_diff()

    # Candle structure
    body = (d['close'] - d['open']).abs()
    full_range = (d['high'] - d['low']).replace(0, np.nan)
    d['lower_wick'] = (d[['close', 'open']].min(axis=1) - d['low']) / full_range
    d['upper_wick'] = (d['high'] - d[['close', 'open']].max(axis=1)) / full_range
    d['body_ratio'] = body / full_range

    # Recent price change (momentum context)
    d['ret_24h'] = d['close'].pct_change(24)
    d['ret_48h'] = d['close'].pct_change(48)

    # ── Signals ─────────────────────────────────────────
    d['signal'] = 0
    d['signal_mode'] = ''
    d['score'] = 0.0
    d['stop_loss'] = np.nan
    d['take_profit'] = np.nan
    d['leverage'] = 1.0

    warmup = 200

    for i in range(warmup, len(d)):
        rsi_val = d['rsi'].iloc[i]
        atr_val = d['atr'].iloc[i]
        natr = d['natr'].iloc[i]
        close = d['close'].iloc[i]
        adx_val = d['adx'].iloc[i]
        vol_ratio = d['vol_ratio'].iloc[i]

        if pd.isna(rsi_val) or pd.isna(atr_val) or atr_val <= 0:
            continue

        signal = 0
        score = 0.0
        mode = ''

        # ── STRATEGY A: RSI EXTREME REVERSAL ───────────
        # Only trigger at very extreme RSI levels with confirmation
        rsi_os = params['rsi_oversold']
        rsi_ob = params['rsi_overbought']

        if rsi_val < rsi_os:
            # Oversold → potential long
            score = 0.0

            # 1. RSI extremity (deeper = better)
            rsi_score = min((rsi_os - rsi_val) / (rsi_os - 10), 1.0)
            score += rsi_score * 0.25

            # 2. Volume confirmation (more = better)
            if vol_ratio > params['vol_threshold']:
                score += min(vol_ratio / 3.0, 1.0) * 0.20

            # 3. Below BB (double extreme)
            if close < d['bb_lower'].iloc[i]:
                score += 0.15

            # 4. MACD improving (momentum turning)
            if d['macd_hist'].iloc[i] > d['macd_hist'].iloc[i-1]:
                score += 0.15

            # 5. Rejection wick (buyers stepping in)
            if d['lower_wick'].iloc[i] > 0.4:
                score += 0.10

            # 6. Extended selloff (more oversold = higher reversion probability)
            if d['ret_24h'].iloc[i] < -0.03:  # down >3% in 24h
                score += 0.10

            # 7. NOT in massive crash (ADX kill zone)
            if adx_val > params.get('adx_kill', 45):
                score *= 0.3  # massive penalty in extreme trends

            # Trend alignment bonus/penalty
            if close > d['ema100'].iloc[i]:
                score *= 1.15  # aligned with bigger trend
            elif close < d['ema200'].iloc[i]:
                score *= 0.7   # deep counter-trend, risky

            if score >= params['min_score']:
                signal = 1
                mode = 'rsi_reversal_long'

        elif rsi_val > rsi_ob:
            # Overbought → potential short
            score = 0.0

            rsi_score = min((rsi_val - rsi_ob) / (90 - rsi_ob), 1.0)
            score += rsi_score * 0.25

            if vol_ratio > params['vol_threshold']:
                score += min(vol_ratio / 3.0, 1.0) * 0.20

            if close > d['bb_upper'].iloc[i]:
                score += 0.15

            if d['macd_hist'].iloc[i] < d['macd_hist'].iloc[i-1]:
                score += 0.15

            if d['upper_wick'].iloc[i] > 0.4:
                score += 0.10

            if d['ret_24h'].iloc[i] > 0.03:
                score += 0.10

            if adx_val > params.get('adx_kill', 45):
                score *= 0.3

            if close < d['ema100'].iloc[i]:
                score *= 1.15
            elif close > d['ema200'].iloc[i]:
                score *= 0.7

            if score >= params['min_score']:
                signal = -1
                mode = 'rsi_reversal_short'

        # ── STRATEGY B: VOLUME BREAKOUT ────────────────
        # Only when Strategy A didn't fire
        if signal == 0 and vol_ratio > params['vol_breakout_threshold']:
            bull_candle = d['close'].iloc[i] > d['open'].iloc[i]
            bear_candle = d['close'].iloc[i] < d['open'].iloc[i]
            strong_body = d['body_ratio'].iloc[i] > 0.6

            if bull_candle and strong_body:
                score = 0.0
                # Volume strength
                score += min(vol_ratio / 4.0, 1.0) * 0.30

                # Trend aligned
                if d['ema50'].iloc[i] > d['ema100'].iloc[i]:
                    score += 0.20
                # Above EMA
                if close > d['ema50'].iloc[i]:
                    score += 0.15
                # ADX showing trend strength
                if adx_val > 20 and d['plus_di'].iloc[i] > d['minus_di'].iloc[i]:
                    score += 0.15
                # RSI not already overbought
                if rsi_val < 65:
                    score += 0.10

                if adx_val > params.get('adx_kill', 45):
                    score *= 0.5

                if score >= params['min_score_breakout']:
                    signal = 1
                    mode = 'volume_breakout_long'

            elif bear_candle and strong_body:
                score = 0.0
                score += min(vol_ratio / 4.0, 1.0) * 0.30

                if d['ema50'].iloc[i] < d['ema100'].iloc[i]:
                    score += 0.20
                if close < d['ema50'].iloc[i]:
                    score += 0.15
                if adx_val > 20 and d['minus_di'].iloc[i] > d['plus_di'].iloc[i]:
                    score += 0.15
                if rsi_val > 35:
                    score += 0.10

                if adx_val > params.get('adx_kill', 45):
                    score *= 0.5

                if score >= params['min_score_breakout']:
                    signal = -1
                    mode = 'volume_breakout_short'

        if signal == 0:
            continue

        # ── SL/TP ──────────────────────────────────────
        if 'reversal' in mode:
            sl_mult = params['reversal_sl_atr']
            tp_mult = params['reversal_tp_atr']
        else:
            sl_mult = params['breakout_sl_atr']
            tp_mult = params['breakout_tp_atr']

        if signal == 1:
            sl = close - atr_val * sl_mult
            tp = close + atr_val * tp_mult
        else:
            sl = close + atr_val * sl_mult
            tp = close - atr_val * tp_mult

        lev = 1.0
        if score >= 0.65:
            lev = min(2.0, params.get('max_leverage', 2.0))
        elif score >= 0.55:
            lev = min(1.5, params.get('max_leverage', 2.0))

        d.iat[i, d.columns.get_loc('signal')] = signal
        d.iat[i, d.columns.get_loc('signal_mode')] = mode
        d.iat[i, d.columns.get_loc('score')] = score
        d.iat[i, d.columns.get_loc('stop_loss')] = sl
        d.iat[i, d.columns.get_loc('take_profit')] = tp
        d.iat[i, d.columns.get_loc('leverage')] = lev

    return d


def realistic_backtest(all_signals, start_date, end_date, label="",
                       max_hold_bars=48, trailing_stop_atr=None,
                       breakeven_bars=None, cooldown_bars=8):
    trades = []

    for coin, df in all_signals.items():
        mask = (df.index >= start_date) & (df.index <= end_date)
        period_df = df[mask].copy()
        if len(period_df) == 0:
            continue

        last_exit_bar = -cooldown_bars - 1

        i = 0
        while i < len(period_df):
            row = period_df.iloc[i]
            if row['signal'] == 0 or pd.isna(row['stop_loss']) or pd.isna(row['take_profit']):
                i += 1
                continue

            if i - last_exit_bar < cooldown_bars:
                i += 1
                continue

            signal = int(row['signal'])
            entry_price = row['close']
            sl = row['stop_loss']
            tp = row['take_profit']
            leverage = row.get('leverage', 1.0)
            score = row.get('score', 0)
            atr_val = row.get('atr', 0)
            mode = row.get('signal_mode', '')

            if signal == 1:
                actual_entry = entry_price * (1 + SLIPPAGE_RATE)
            else:
                actual_entry = entry_price * (1 - SLIPPAGE_RATE)

            exit_price = None
            exit_reason = None
            exit_time = None
            current_sl = sl
            best_price = actual_entry

            for j in range(i+1, min(i + max_hold_bars + 1, len(period_df))):
                bar = period_df.iloc[j]
                bars_held = j - i

                if signal == 1:
                    best_price = max(best_price, bar['high'])
                else:
                    best_price = min(best_price, bar['low'])

                # Trailing stop
                if trailing_stop_atr and bars_held >= 6 and atr_val > 0:
                    if signal == 1:
                        trail_sl = best_price - atr_val * trailing_stop_atr
                        current_sl = max(current_sl, trail_sl)
                    else:
                        trail_sl = best_price + atr_val * trailing_stop_atr
                        current_sl = min(current_sl, trail_sl)

                # Breakeven after N bars
                if breakeven_bars and bars_held >= breakeven_bars:
                    if signal == 1 and bar['close'] > actual_entry * 1.003:
                        current_sl = max(current_sl, actual_entry * 1.001)
                    elif signal == -1 and bar['close'] < actual_entry * 0.997:
                        current_sl = min(current_sl, actual_entry * 0.999)

                if signal == 1:
                    if bar['low'] <= current_sl:
                        exit_price = current_sl * (1 - SLIPPAGE_RATE)
                        exit_reason = 'SL'
                        exit_time = bar.name
                        break
                    if bar['high'] >= tp:
                        exit_price = tp * (1 - SLIPPAGE_RATE)
                        exit_reason = 'TP'
                        exit_time = bar.name
                        break
                else:
                    if bar['high'] >= current_sl:
                        exit_price = current_sl * (1 + SLIPPAGE_RATE)
                        exit_reason = 'SL'
                        exit_time = bar.name
                        break
                    if bar['low'] <= tp:
                        exit_price = tp * (1 + SLIPPAGE_RATE)
                        exit_reason = 'TP'
                        exit_time = bar.name
                        break

            if exit_price is None:
                last_idx = min(i + max_hold_bars, len(period_df) - 1)
                last_bar = period_df.iloc[last_idx]
                exit_price = last_bar['close']
                if signal == 1:
                    exit_price *= (1 - SLIPPAGE_RATE)
                else:
                    exit_price *= (1 + SLIPPAGE_RATE)
                exit_reason = 'TIME'
                exit_time = last_bar.name

            if signal == 1:
                pnl_pct = (exit_price - actual_entry) / actual_entry
            else:
                pnl_pct = (actual_entry - exit_price) / actual_entry

            commission = NOTIONAL * COMMISSION_RATE * 2
            dollar_pnl = NOTIONAL * leverage * pnl_pct - commission

            trades.append({
                'coin': coin, 'entry_time': row.name, 'exit_time': exit_time,
                'signal': signal, 'entry_price': actual_entry, 'exit_price': exit_price,
                'leverage': leverage, 'pnl_pct': pnl_pct, 'dollar_pnl': dollar_pnl,
                'commission': commission, 'exit_reason': exit_reason,
                'score': score, 'mode': mode,
            })

            if exit_time is not None:
                exit_idx = period_df.index.get_loc(exit_time)
                last_exit_bar = exit_idx
                i = exit_idx + 1
            else:
                i += 1

    if not trades:
        return pd.DataFrame(), {}

    trades_df = pd.DataFrame(trades).sort_values('entry_time').reset_index(drop=True)

    # Portfolio constraints
    final_trades = []
    consec_losses = 0
    skip_until = None
    daily_loss = {}
    open_positions = []

    for _, trade in trades_df.iterrows():
        entry_t = trade['entry_time']
        open_positions = [(et, c) for et, c in open_positions if et > entry_t]
        if len(open_positions) >= MAX_POSITIONS:
            continue
        if skip_until is not None and entry_t < skip_until:
            continue
        skip_until = None

        day_key = entry_t.date()
        if daily_loss.get(day_key, 0) >= DAILY_LOSS_CAP:
            continue

        size_mult = 0.5 if consec_losses >= 3 else 1.0
        adjusted_pnl = trade['dollar_pnl'] * size_mult

        if adjusted_pnl < 0:
            consec_losses += 1
            if consec_losses >= 5:
                skip_until = entry_t + pd.Timedelta(hours=10)
                consec_losses = 0
        else:
            consec_losses = 0

        daily_loss[day_key] = daily_loss.get(day_key, 0) + max(0, -adjusted_pnl)
        open_positions.append((trade['exit_time'], trade['coin']))

        trade_copy = trade.to_dict()
        trade_copy['adjusted_pnl'] = adjusted_pnl
        final_trades.append(trade_copy)

    if not final_trades:
        return pd.DataFrame(), {}

    result_df = pd.DataFrame(final_trades)
    result_df['month'] = pd.to_datetime(result_df['entry_time']).dt.to_period('M')

    monthly = result_df.groupby('month').agg(
        trades=('adjusted_pnl', 'count'),
        pnl=('adjusted_pnl', 'sum'),
        wins=('adjusted_pnl', lambda x: (x > 0).sum()),
    ).reset_index()
    monthly['wr'] = (monthly['wins'] / monthly['trades'] * 100).round(1)
    monthly['cum_pnl'] = monthly['pnl'].cumsum()

    total_pnl = result_df['adjusted_pnl'].sum()
    total_trades = len(result_df)
    wins = (result_df['adjusted_pnl'] > 0).sum()

    cum = result_df['adjusted_pnl'].cumsum()
    max_dd = (cum - cum.cummax()).min()
    losing_sum = abs(result_df.loc[result_df['adjusted_pnl'] <= 0, 'adjusted_pnl'].sum())
    winning_sum = result_df.loc[result_df['adjusted_pnl'] > 0, 'adjusted_pnl'].sum()

    # Exit reason stats
    exit_stats = result_df.groupby('exit_reason')['adjusted_pnl'].agg(['count', 'sum', 'mean']).to_dict('index')

    # Mode stats
    mode_stats = {}
    for m in result_df['mode'].unique():
        mdf = result_df[result_df['mode'] == m]
        mode_stats[m] = {
            'trades': len(mdf), 'pnl': round(mdf['adjusted_pnl'].sum(), 2),
            'wr': round((mdf['adjusted_pnl'] > 0).mean() * 100, 1),
        }

    stats = {
        'label': label, 'total_pnl': round(total_pnl, 2),
        'total_trades': total_trades,
        'win_rate': round(wins / total_trades * 100, 1),
        'avg_trade': round(total_pnl / total_trades, 2),
        'roi_pct': round(total_pnl / INITIAL_CAPITAL * 100, 2),
        'max_drawdown': round(max_dd, 2),
        'profit_factor': round(winning_sum / losing_sum, 2) if losing_sum > 0 else 999,
        'exit_stats': exit_stats,
        'mode_stats': mode_stats,
    }

    return result_df, {'stats': stats, 'monthly': monthly.to_dict('records')}


def print_full_results(label, results):
    if not results:
        print(f"\n{label}: No results")
        return
    s = results['stats']
    print(f"\n  {label}:")
    print(f"    PnL: ${s['total_pnl']:,.2f} | Trades: {s['total_trades']} | WR: {s['win_rate']}% | ROI: {s['roi_pct']}%")
    print(f"    MaxDD: ${s['max_drawdown']:.2f} | PF: {s['profit_factor']} | Avg: ${s['avg_trade']:.2f}")
    if s.get('mode_stats'):
        for mode, ms in s['mode_stats'].items():
            print(f"      [{mode}] {ms['trades']}t, ${ms['pnl']:.2f}, WR={ms['wr']}%")
    if s.get('exit_stats'):
        for reason, es in s['exit_stats'].items():
            print(f"      Exit {reason}: {es['count']:.0f}t, ${es['sum']:.2f}, avg=${es['mean']:.2f}")
    for m in results['monthly']:
        print(f"      {m['month']}: {m['trades']}t, ${m['pnl']:.2f}, WR={m['wr']}%, Cum=${m['cum_pnl']:.2f}")


# ── Configurations to test ──────────────────────────────
CONFIGS = {
    'ultra_selective': {
        'rsi_period': 14,
        'rsi_oversold': 20, 'rsi_overbought': 80,
        'vol_threshold': 1.3,
        'vol_breakout_threshold': 2.5,
        'min_score': 0.45,
        'min_score_breakout': 0.50,
        'adx_kill': 45,
        'reversal_sl_atr': 2.0, 'reversal_tp_atr': 2.5,
        'breakout_sl_atr': 1.5, 'breakout_tp_atr': 2.5,
        'max_leverage': 2.0,
    },
    'rsi_only_wide': {
        'rsi_period': 14,
        'rsi_oversold': 22, 'rsi_overbought': 78,
        'vol_threshold': 1.0,
        'vol_breakout_threshold': 99,  # disable breakout
        'min_score': 0.40,
        'min_score_breakout': 99,
        'adx_kill': 45,
        'reversal_sl_atr': 2.0, 'reversal_tp_atr': 3.0,
        'breakout_sl_atr': 2.0, 'breakout_tp_atr': 3.0,
        'max_leverage': 2.0,
    },
    'rsi_tight': {
        'rsi_period': 14,
        'rsi_oversold': 18, 'rsi_overbought': 82,
        'vol_threshold': 1.0,
        'vol_breakout_threshold': 99,
        'min_score': 0.35,
        'min_score_breakout': 99,
        'adx_kill': 40,
        'reversal_sl_atr': 1.5, 'reversal_tp_atr': 2.0,
        'breakout_sl_atr': 1.5, 'breakout_tp_atr': 2.0,
        'max_leverage': 2.0,
    },
    'combined_moderate': {
        'rsi_period': 14,
        'rsi_oversold': 25, 'rsi_overbought': 75,
        'vol_threshold': 1.2,
        'vol_breakout_threshold': 2.0,
        'min_score': 0.45,
        'min_score_breakout': 0.45,
        'adx_kill': 45,
        'reversal_sl_atr': 1.5, 'reversal_tp_atr': 2.0,
        'breakout_sl_atr': 1.5, 'breakout_tp_atr': 2.0,
        'max_leverage': 2.0,
    },
    'no_tp_trailing': {
        'rsi_period': 14,
        'rsi_oversold': 22, 'rsi_overbought': 78,
        'vol_threshold': 1.0,
        'vol_breakout_threshold': 2.0,
        'min_score': 0.40,
        'min_score_breakout': 0.45,
        'adx_kill': 45,
        'reversal_sl_atr': 2.0, 'reversal_tp_atr': 99.0,  # no fixed TP
        'breakout_sl_atr': 1.5, 'breakout_tp_atr': 99.0,   # no fixed TP
        'max_leverage': 2.0,
    },
}

BACKTEST_CONFIGS = [
    # (max_hold, trailing_atr, breakeven_bars, cooldown, label_suffix)
    (48, None, 12, 8, 'fixed_48h'),
    (48, 2.0, 12, 8, 'trail_48h'),
    (72, 2.5, 18, 12, 'trail_72h'),
    (24, None, 8, 6, 'fixed_24h'),
]

# ── Run tests ───────────────────────────────────────────
all_results = {}

for config_name, params in CONFIGS.items():
    print(f"\n{'='*70}")
    print(f"Config: {config_name}")
    print(f"{'='*70}")

    # Generate signals
    config_signals = {}
    for coin, df in all_data.items():
        try:
            config_signals[coin] = generate_signals_v3(df, params)
        except Exception as e:
            print(f"  {coin}: error - {e}")

    total_sigs = sum((df['signal'] != 0).sum() for df in config_signals.values())
    print(f"  Total signals: {total_sigs}")

    best_combined = {'pnl': -99999}

    for max_hold, trail_atr, be_bars, cd_bars, bt_label in BACKTEST_CONFIGS:
        full_label = f"{config_name}_{bt_label}"

        _, r2025 = realistic_backtest(config_signals, '2025-01-01', '2025-12-31',
                                       label=full_label + '_2025',
                                       max_hold_bars=max_hold, trailing_stop_atr=trail_atr,
                                       breakeven_bars=be_bars, cooldown_bars=cd_bars)

        _, r2026 = realistic_backtest(config_signals, '2026-01-01', '2026-04-12',
                                       label=full_label + '_2026',
                                       max_hold_bars=max_hold, trailing_stop_atr=trail_atr,
                                       breakeven_bars=be_bars, cooldown_bars=cd_bars)

        p25 = r2025.get('stats', {}).get('total_pnl', 0) if r2025 else 0
        p26 = r2026.get('stats', {}).get('total_pnl', 0) if r2026 else 0
        wr25 = r2025.get('stats', {}).get('win_rate', 0) if r2025 else 0
        wr26 = r2026.get('stats', {}).get('win_rate', 0) if r2026 else 0
        t25 = r2025.get('stats', {}).get('total_trades', 0) if r2025 else 0
        t26 = r2026.get('stats', {}).get('total_trades', 0) if r2026 else 0

        print(f"  {bt_label:<15} 2025: ${p25:>8.2f} WR={wr25:.1f}% ({t25}t) | 2026: ${p26:>8.2f} WR={wr26:.1f}% ({t26}t)")

        combined_pnl = p25 + p26
        if combined_pnl > best_combined['pnl']:
            best_combined = {
                'pnl': combined_pnl, 'bt_label': bt_label,
                'r2025': r2025, 'r2026': r2026,
            }

    # Show best for this config
    if best_combined['r2025'] or best_combined['r2026']:
        print(f"\n  BEST: {best_combined['bt_label']} (combined PnL: ${best_combined['pnl']:.2f})")
        print_full_results('2025', best_combined['r2025'])
        print_full_results('2026 Q1', best_combined['r2026'])

    all_results[config_name] = best_combined


# ── Final Summary ───────────────────────────────────────
print("\n\n" + "="*90)
print("FINAL COMPARISON")
print("="*90)
for name, res in all_results.items():
    r25 = res.get('r2025', {}).get('stats', {}) if res.get('r2025') else {}
    r26 = res.get('r2026', {}).get('stats', {}) if res.get('r2026') else {}
    print(f"\n  {name} ({res.get('bt_label', 'N/A')}):")
    print(f"    2025: ${r25.get('total_pnl',0):,.2f} | WR={r25.get('win_rate',0):.1f}% | PF={r25.get('profit_factor',0):.2f} | Trades={r25.get('total_trades',0)}")
    print(f"    2026: ${r26.get('total_pnl',0):,.2f} | WR={r26.get('win_rate',0):.1f}% | PF={r26.get('profit_factor',0):.2f} | Trades={r26.get('total_trades',0)}")
    print(f"    Combined: ${r25.get('total_pnl',0) + r26.get('total_pnl',0):,.2f}")


# Save best results
def make_serializable(obj):
    if isinstance(obj, dict):
        return {str(k): make_serializable(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [make_serializable(item) for item in obj]
    elif isinstance(obj, (pd.Period, pd.Timestamp)):
        return str(obj)
    elif isinstance(obj, (np.integer,)):
        return int(obj)
    elif isinstance(obj, (np.floating,)):
        return float(obj)
    elif isinstance(obj, np.bool_):
        return bool(obj)
    return obj

save_data = {}
for name, res in all_results.items():
    save_data[name] = {
        'bt_label': res.get('bt_label'),
        '2025': res.get('r2025', {}),
        '2026': res.get('r2026', {}),
    }

with open('/Users/ibrahimpeyman/Documents/oto-bot/artifacts/v3_profitable_results.json', 'w') as f:
    json.dump(make_serializable(save_data), f, indent=2, default=str)

print("\nResults saved to artifacts/v3_profitable_results.json")
