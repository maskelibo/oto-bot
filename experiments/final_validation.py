"""
PHASE 6: Final validation of best approaches

Two winners:
1. ultra_selective: volume breakout + RSI extreme, fixed TP (WR=38%, PF=1.17)
2. no_tp_trailing: volume breakout, no fixed TP, let winners run (WR=16%, PF=1.21)

Need to validate:
- Monthly consistency
- Is edge stable or concentrated in a few lucky trades?
- Robustness to parameter changes (±10%)
- Does it work on held-out coins?

Then implement the winner in scalper_v2.py
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
    d = df.copy()

    rsi = ta.momentum.RSIIndicator(d['close'], window=params['rsi_period']).rsi()
    d['rsi'] = rsi

    atr_ind = ta.volatility.AverageTrueRange(high=d['high'], low=d['low'], close=d['close'], window=14)
    d['atr'] = atr_ind.average_true_range()
    d['natr'] = d['atr'] / d['close'] * 100

    bb = ta.volatility.BollingerBands(d['close'], window=20, window_dev=2.0)
    d['bb_lower'] = bb.bollinger_lband()
    d['bb_upper'] = bb.bollinger_hband()

    d['vol_ma'] = d['volume'].rolling(20).mean()
    d['vol_ratio'] = d['volume'] / d['vol_ma'].replace(0, np.nan)

    d['ema50'] = d['close'].ewm(span=50).mean()
    d['ema100'] = d['close'].ewm(span=100).mean()
    d['ema200'] = d['close'].ewm(span=200).mean()

    adx_ind = ta.trend.ADXIndicator(high=d['high'], low=d['low'], close=d['close'], window=14)
    d['adx'] = adx_ind.adx()
    d['plus_di'] = adx_ind.adx_pos()
    d['minus_di'] = adx_ind.adx_neg()

    macd_ind = ta.trend.MACD(d['close'], window_slow=26, window_fast=12, window_sign=9)
    d['macd_hist'] = macd_ind.macd_diff()

    body = (d['close'] - d['open']).abs()
    full_range = (d['high'] - d['low']).replace(0, np.nan)
    d['lower_wick'] = (d[['close', 'open']].min(axis=1) - d['low']) / full_range
    d['upper_wick'] = (d['high'] - d[['close', 'open']].max(axis=1)) / full_range
    d['body_ratio'] = body / full_range
    d['ret_24h'] = d['close'].pct_change(24)

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
        close = d['close'].iloc[i]
        adx_val = d['adx'].iloc[i]
        vol_ratio = d['vol_ratio'].iloc[i]

        if pd.isna(rsi_val) or pd.isna(atr_val) or atr_val <= 0:
            continue

        signal = 0
        score = 0.0
        mode = ''

        # RSI extreme reversal
        rsi_os = params['rsi_oversold']
        rsi_ob = params['rsi_overbought']

        if rsi_val < rsi_os:
            score = 0.0
            rsi_score = min((rsi_os - rsi_val) / (rsi_os - 10), 1.0)
            score += rsi_score * 0.25
            if vol_ratio > params['vol_threshold']:
                score += min(vol_ratio / 3.0, 1.0) * 0.20
            if close < d['bb_lower'].iloc[i]:
                score += 0.15
            if d['macd_hist'].iloc[i] > d['macd_hist'].iloc[i-1]:
                score += 0.15
            if d['lower_wick'].iloc[i] > 0.4:
                score += 0.10
            if d['ret_24h'].iloc[i] < -0.03:
                score += 0.10
            if adx_val > params.get('adx_kill', 45):
                score *= 0.3
            if close > d['ema100'].iloc[i]:
                score *= 1.15
            elif close < d['ema200'].iloc[i]:
                score *= 0.7
            if score >= params['min_score']:
                signal = 1
                mode = 'rsi_reversal_long'

        elif rsi_val > rsi_ob:
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

        # Volume breakout
        if signal == 0 and vol_ratio > params['vol_breakout_threshold']:
            bull = d['close'].iloc[i] > d['open'].iloc[i]
            bear = d['close'].iloc[i] < d['open'].iloc[i]
            strong = d['body_ratio'].iloc[i] > 0.6

            if bull and strong:
                score = 0.0
                score += min(vol_ratio / 4.0, 1.0) * 0.30
                if d['ema50'].iloc[i] > d['ema100'].iloc[i]: score += 0.20
                if close > d['ema50'].iloc[i]: score += 0.15
                if adx_val > 20 and d['plus_di'].iloc[i] > d['minus_di'].iloc[i]: score += 0.15
                if rsi_val < 65: score += 0.10
                if adx_val > params.get('adx_kill', 45): score *= 0.5
                if score >= params['min_score_breakout']:
                    signal = 1
                    mode = 'volume_breakout_long'

            elif bear and strong:
                score = 0.0
                score += min(vol_ratio / 4.0, 1.0) * 0.30
                if d['ema50'].iloc[i] < d['ema100'].iloc[i]: score += 0.20
                if close < d['ema50'].iloc[i]: score += 0.15
                if adx_val > 20 and d['minus_di'].iloc[i] > d['plus_di'].iloc[i]: score += 0.15
                if rsi_val > 35: score += 0.10
                if adx_val > params.get('adx_kill', 45): score *= 0.5
                if score >= params['min_score_breakout']:
                    signal = -1
                    mode = 'volume_breakout_short'

        if signal == 0:
            continue

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
        if score >= 0.65: lev = min(2.0, params.get('max_leverage', 2.0))
        elif score >= 0.55: lev = min(1.5, params.get('max_leverage', 2.0))

        d.iat[i, d.columns.get_loc('signal')] = signal
        d.iat[i, d.columns.get_loc('signal_mode')] = mode
        d.iat[i, d.columns.get_loc('score')] = score
        d.iat[i, d.columns.get_loc('stop_loss')] = sl
        d.iat[i, d.columns.get_loc('take_profit')] = tp
        d.iat[i, d.columns.get_loc('leverage')] = lev

    return d


def realistic_backtest(all_signals, start_date, end_date, label="",
                       max_hold_bars=48, cooldown_bars=8):
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
            mode = row.get('signal_mode', '')

            if signal == 1:
                actual_entry = entry_price * (1 + SLIPPAGE_RATE)
            else:
                actual_entry = entry_price * (1 - SLIPPAGE_RATE)

            exit_price = None
            exit_reason = None
            exit_time = None

            for j in range(i+1, min(i + max_hold_bars + 1, len(period_df))):
                bar = period_df.iloc[j]
                if signal == 1:
                    if bar['low'] <= sl:
                        exit_price = sl * (1 - SLIPPAGE_RATE)
                        exit_reason = 'SL'
                        exit_time = bar.name
                        break
                    if bar['high'] >= tp:
                        exit_price = tp * (1 - SLIPPAGE_RATE)
                        exit_reason = 'TP'
                        exit_time = bar.name
                        break
                else:
                    if bar['high'] >= sl:
                        exit_price = sl * (1 + SLIPPAGE_RATE)
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
        'mode_stats': mode_stats,
    }

    return result_df, {'stats': stats, 'monthly': monthly.to_dict('records')}


# ── BLENDED STRATEGY: combine both winners ──────────────
# Use ultra_selective for entries (it has better WR and lower DD),
# but with a hybrid TP approach:
# - volume_breakout: no fixed TP (let winners run)
# - rsi_reversal: fixed TP at 2.5 ATR (mean reversion has target)

BLENDED_PARAMS = {
    'rsi_period': 14,
    'rsi_oversold': 22, 'rsi_overbought': 78,
    'vol_threshold': 1.0,
    'vol_breakout_threshold': 2.0,
    'min_score': 0.40,
    'min_score_breakout': 0.45,
    'adx_kill': 45,
    'reversal_sl_atr': 2.0, 'reversal_tp_atr': 2.5,
    'breakout_sl_atr': 1.5, 'breakout_tp_atr': 99.0,  # no fixed TP for breakout
    'max_leverage': 2.0,
}

print("\n" + "="*70)
print("BLENDED STRATEGY VALIDATION")
print("="*70)

# Generate signals
blended_signals = {}
for coin, df in all_data.items():
    try:
        blended_signals[coin] = generate_signals_v3(df, BLENDED_PARAMS)
    except Exception as e:
        print(f"  {coin}: error - {e}")

total_sigs = sum((df['signal'] != 0).sum() for df in blended_signals.values())
print(f"Total signals: {total_sigs}")

# Full period backtest
print("\n--- Full Period: 2025-01 to 2026-04 ---")
trades_full, r_full = realistic_backtest(blended_signals, '2025-01-01', '2026-04-12', 'full')
if r_full:
    s = r_full['stats']
    print(f"  PnL: ${s['total_pnl']:,.2f} | Trades: {s['total_trades']} | WR: {s['win_rate']}% | ROI: {s['roi_pct']}%")
    print(f"  MaxDD: ${s['max_drawdown']:.2f} | PF: {s['profit_factor']}")
    for m in s.get('mode_stats', {}).items():
        print(f"    [{m[0]}] {m[1]['trades']}t, ${m[1]['pnl']:.2f}, WR={m[1]['wr']}%")
    print(f"\n  Monthly:")
    for m in r_full['monthly']:
        print(f"    {m['month']}: {m['trades']}t, ${m['pnl']:.2f}, WR={m['wr']}%, Cum=${m['cum_pnl']:.2f}")

# ── Robustness test: perturb params ±10-15% ────────────
print("\n\n" + "="*70)
print("ROBUSTNESS TEST: Parameter perturbation")
print("="*70)

np.random.seed(42)
robustness_results = []

for trial in range(10):
    perturbed = BLENDED_PARAMS.copy()
    # Perturb numeric params by ±15%
    for key in ['rsi_oversold', 'rsi_overbought', 'vol_threshold', 'vol_breakout_threshold',
                'min_score', 'min_score_breakout', 'reversal_sl_atr', 'reversal_tp_atr',
                'breakout_sl_atr']:
        factor = 1 + np.random.uniform(-0.15, 0.15)
        perturbed[key] = perturbed[key] * factor

    # Keep thresholds sensible
    perturbed['rsi_oversold'] = max(15, min(35, perturbed['rsi_oversold']))
    perturbed['rsi_overbought'] = max(65, min(85, perturbed['rsi_overbought']))
    perturbed['min_score'] = max(0.30, min(0.60, perturbed['min_score']))
    perturbed['min_score_breakout'] = max(0.30, min(0.60, perturbed['min_score_breakout']))

    pert_signals = {}
    for coin, df in all_data.items():
        try:
            pert_signals[coin] = generate_signals_v3(df, perturbed)
        except:
            pass

    _, r25 = realistic_backtest(pert_signals, '2025-01-01', '2025-12-31', f'trial_{trial}_2025')
    _, r26 = realistic_backtest(pert_signals, '2026-01-01', '2026-04-12', f'trial_{trial}_2026')

    p25 = r25.get('stats', {}).get('total_pnl', 0) if r25 else 0
    p26 = r26.get('stats', {}).get('total_pnl', 0) if r26 else 0
    wr25 = r25.get('stats', {}).get('win_rate', 0) if r25 else 0
    wr26 = r26.get('stats', {}).get('win_rate', 0) if r26 else 0

    robustness_results.append({
        'trial': trial, 'pnl_2025': p25, 'pnl_2026': p26,
        'wr_2025': wr25, 'wr_2026': wr26, 'combined': p25 + p26,
    })

    print(f"  Trial {trial}: 2025=${p25:>8.2f} WR={wr25:.1f}% | 2026=${p26:>8.2f} WR={wr26:.1f}% | Combined=${p25+p26:>8.2f}")

rob_df = pd.DataFrame(robustness_results)
print(f"\n  Robustness Summary:")
print(f"    2025 PnL range: ${rob_df['pnl_2025'].min():.2f} to ${rob_df['pnl_2025'].max():.2f}")
print(f"    2026 PnL range: ${rob_df['pnl_2026'].min():.2f} to ${rob_df['pnl_2026'].max():.2f}")
print(f"    Combined range: ${rob_df['combined'].min():.2f} to ${rob_df['combined'].max():.2f}")
print(f"    % profitable (combined): {(rob_df['combined'] > 0).mean()*100:.0f}%")
print(f"    % 2025 profitable: {(rob_df['pnl_2025'] > 0).mean()*100:.0f}%")
print(f"    % 2026 profitable: {(rob_df['pnl_2026'] > 0).mean()*100:.0f}%")


# ── Coin-level analysis ────────────────────────────────
print("\n\n" + "="*70)
print("PER-COIN ANALYSIS (Blended Strategy)")
print("="*70)

if len(trades_full) > 0:
    coin_stats = trades_full.groupby('coin')['adjusted_pnl'].agg(['sum', 'count', 'mean']).sort_values('sum', ascending=False)
    for coin, row in coin_stats.iterrows():
        coin_trades = trades_full[trades_full['coin'] == coin]
        wr = (coin_trades['adjusted_pnl'] > 0).mean() * 100
        print(f"  {coin:<15} ${row['sum']:>8.2f} | {int(row['count']):>4}t | WR={wr:.0f}% | Avg=${row['mean']:.2f}")

    profitable_coins = (coin_stats['sum'] > 0).sum()
    print(f"\n  Profitable coins: {profitable_coins}/{len(coin_stats)} ({profitable_coins/len(coin_stats)*100:.0f}%)")


# Save final results
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

final_data = {
    'strategy': 'Scalper V3 - Volume Breakout + RSI Extreme Hybrid',
    'params': BLENDED_PARAMS,
    'full_period_results': r_full,
    'robustness': {
        'trials': 10,
        'param_perturbation': '±15%',
        'pct_profitable_combined': round((rob_df['combined'] > 0).mean()*100, 1),
        'pct_profitable_2025': round((rob_df['pnl_2025'] > 0).mean()*100, 1),
        'pct_profitable_2026': round((rob_df['pnl_2026'] > 0).mean()*100, 1),
    },
}

with open('/Users/ibrahimpeyman/Documents/oto-bot/artifacts/v3_final_validation.json', 'w') as f:
    json.dump(make_serializable(final_data), f, indent=2, default=str)

print("\nFinal results saved to artifacts/v3_final_validation.json")
