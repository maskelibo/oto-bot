"""
PHASE 3 (fixed): Deep structural fix with adaptive momentum-reversion hybrid.
Fixes: StochRSI API, data fetching reliability, rate limits.
"""
import json, sys, time, warnings, pickle, os
import numpy as np
import pandas as pd
from datetime import datetime, timezone

warnings.filterwarnings('ignore')
sys.path.insert(0, 'C:/Users/koray/projeler/oto-bot/src')

import ta
from oto_bot.data.crypto import CryptoDataProvider

# ── Config ──────────────────────────────────────────────
COINS = ['BTC/USDT','ETH/USDT','SOL/USDT','DOGE/USDT','ALGO/USDT',
         'OP/USDT','LTC/USDT','FET/USDT','RENDER/USDT','SUI/USDT','LINK/USDT',
         'AAVE/USDT','NEAR/USDT','BNB/USDT','XRP/USDT','AVAX/USDT',
         'DOT/USDT','UNI/USDT','APT/USDT','ARB/USDT','ATOM/USDT']

COMMISSION_RATE = 0.001
SLIPPAGE_RATE = 0.0005
NOTIONAL = 600.0
MAX_POSITIONS = 6
DAILY_LOSS_CAP = 300.0
INITIAL_CAPITAL = 10000.0

CACHE_FILE = 'C:/Users/koray/projeler/oto-bot/artifacts/data_cache.pkl'

# ── Fetch data with caching ────────────────────────────
provider = CryptoDataProvider()

def fetch_coin(symbol, since_str, max_retries=3):
    exchange = provider._get_exchange()
    dt = datetime.fromisoformat(since_str).replace(tzinfo=timezone.utc)
    since_ms = int(dt.timestamp() * 1000)
    all_data = []
    retries = 0
    while True:
        try:
            raw = exchange.fetch_ohlcv(symbol, '1h', since=since_ms, limit=1000)
        except Exception as e:
            retries += 1
            if retries > max_retries:
                raise
            time.sleep(2)
            continue
        if not raw:
            break
        all_data.extend(raw)
        since_ms = raw[-1][0] + 3600000
        if len(raw) < 1000:
            break
        time.sleep(0.25)
    df = pd.DataFrame(all_data, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    return df

# Try cache first
if os.path.exists(CACHE_FILE):
    print("Loading cached data...")
    with open(CACHE_FILE, 'rb') as f:
        all_data = pickle.load(f)
    print(f"  Loaded {len(all_data)} coins from cache")
    # Check if data is recent enough
    sample = next(iter(all_data.values()))
    if sample.index[-1] < pd.Timestamp('2026-04-10', tz='UTC'):
        print("  Cache too old, re-fetching...")
        all_data = {}
else:
    all_data = {}

if not all_data:
    print("Fetching data from Binance...")
    for coin in COINS:
        try:
            df = fetch_coin(coin, "2024-12-01T00:00:00")
            all_data[coin] = df
            print(f"  {coin}: {len(df)} bars, {df.index[0].date()} to {df.index[-1].date()}")
        except Exception as e:
            print(f"  {coin}: FAILED - {e}")
        time.sleep(0.3)

    with open(CACHE_FILE, 'wb') as f:
        pickle.dump(all_data, f)
    print(f"  Saved {len(all_data)} coins to cache")


# ── Adaptive Strategy V3 ───────────────────────────────
def generate_signals_v3(df, params):
    """
    Adaptive Momentum-Reversion Hybrid.
    Mode 1 (MOMENTUM): ADX > threshold, trade pullbacks with trend.
    Mode 2 (REVERSION): ADX in range, mean-revert at extremes.
    Mode 3 (FLAT): ADX very low or very high, DON'T TRADE.
    """
    d = df.copy()

    # ── Indicators ──────────────────────────────────────
    d['ema_fast'] = d['close'].ewm(span=params['ema_fast']).mean()
    d['ema_slow'] = d['close'].ewm(span=params['ema_slow']).mean()
    d['ema_trend'] = d['close'].ewm(span=params['ema_trend']).mean()

    # Bollinger Bands
    bb = ta.volatility.BollingerBands(close=d['close'], window=params['bb_period'], window_dev=params['bb_std'])
    d['bb_upper'] = bb.bollinger_hband()
    d['bb_lower'] = bb.bollinger_lband()
    d['bb_pband'] = bb.bollinger_pband()

    # RSI
    rsi_ind = ta.momentum.RSIIndicator(d['close'], window=params['rsi_period'])
    d['rsi'] = rsi_ind.rsi()

    # ATR
    atr_ind = ta.volatility.AverageTrueRange(high=d['high'], low=d['low'], close=d['close'], window=params['atr_period'])
    d['atr'] = atr_ind.average_true_range()
    d['natr'] = d['atr'] / d['close'] * 100

    # ADX with DI
    adx_ind = ta.trend.ADXIndicator(high=d['high'], low=d['low'], close=d['close'], window=params['adx_period'])
    d['adx'] = adx_ind.adx()
    d['plus_di'] = adx_ind.adx_pos()
    d['minus_di'] = adx_ind.adx_neg()

    # Volume
    d['vol_ma'] = d['volume'].rolling(params['vol_period']).mean()
    d['vol_ratio'] = d['volume'] / d['vol_ma'].replace(0, np.nan)

    # MACD
    macd_ind = ta.trend.MACD(d['close'], window_slow=26, window_fast=12, window_sign=9)
    d['macd_hist'] = macd_ind.macd_diff()

    # Stochastic RSI
    stoch = ta.momentum.StochRSIIndicator(d['close'], window=14, smooth1=3, smooth2=3)
    d['stoch_k'] = stoch.stochrsi_k()

    # Candle structure
    body = (d['close'] - d['open']).abs()
    full_range = (d['high'] - d['low']).replace(0, np.nan)
    d['lower_wick'] = (d[['close', 'open']].min(axis=1) - d['low']) / full_range
    d['upper_wick'] = (d['high'] - d[['close', 'open']].max(axis=1)) / full_range

    # ── Signal generation ───────────────────────────────
    d['signal'] = 0
    d['signal_mode'] = ''
    d['score'] = 0.0
    d['stop_loss'] = np.nan
    d['take_profit'] = np.nan
    d['probability'] = 0.0
    d['leverage'] = 1.0

    warmup = max(params['ema_trend'], 60)

    for i in range(warmup, len(d)):
        adx_val = d['adx'].iloc[i]
        natr = d['natr'].iloc[i]

        if pd.isna(adx_val) or pd.isna(natr):
            continue

        # Min volatility
        if natr < params['min_natr']:
            continue

        close = d['close'].iloc[i]
        rsi_val = d['rsi'].iloc[i]
        bb_p = d['bb_pband'].iloc[i]
        stoch_k = d['stoch_k'].iloc[i]
        macd_hist = d['macd_hist'].iloc[i]
        ema_fast = d['ema_fast'].iloc[i]
        ema_slow = d['ema_slow'].iloc[i]
        ema_trend = d['ema_trend'].iloc[i]
        plus_di = d['plus_di'].iloc[i]
        minus_di = d['minus_di'].iloc[i]
        vol_ratio = d['vol_ratio'].iloc[i]
        atr_val = d['atr'].iloc[i]

        if pd.isna(rsi_val) or pd.isna(bb_p) or pd.isna(stoch_k):
            continue

        score = 0.0
        signal = 0
        mode = ''

        # ── Regime classification ──────────────────────
        is_trending = adx_val > params['adx_trend_threshold']
        is_ranging = adx_val < params['adx_range_threshold']
        is_dead = adx_val < params.get('adx_dead_threshold', 8)
        is_too_strong = adx_val > params.get('adx_kill_threshold', 45)

        # Skip dead or extremely trending markets
        if is_dead or is_too_strong:
            continue

        # ── MOMENTUM MODE ──────────────────────────────
        if is_trending:
            uptrend = plus_di > minus_di
            downtrend = minus_di > plus_di

            if uptrend:
                # Long: pullback in uptrend
                pullback = close <= ema_fast * (1 + params['pullback_pct']/100)
                above_trend = close > ema_trend
                rsi_ok = rsi_val < params['rsi_momentum_max']
                macd_improving = macd_hist > d['macd_hist'].iloc[i-1] if i > 0 else False

                if pullback and rsi_ok:
                    score = 0.30
                    if above_trend: score += 0.15
                    if macd_improving: score += 0.15
                    if stoch_k < params.get('stoch_oversold', 0.3): score += 0.10
                    if vol_ratio >= params['min_vol_ratio']: score += 0.10
                    if plus_di - minus_di > 5: score += 0.10
                    if d['lower_wick'].iloc[i] > 0.4: score += 0.10  # rejection wick
                    signal = 1
                    mode = 'momentum_long'

            elif downtrend:
                # Short: rally in downtrend
                rally = close >= ema_fast * (1 - params['pullback_pct']/100)
                below_trend = close < ema_trend
                rsi_ok = rsi_val > params['rsi_momentum_min']
                macd_worsening = macd_hist < d['macd_hist'].iloc[i-1] if i > 0 else False

                if rally and rsi_ok:
                    score = 0.30
                    if below_trend: score += 0.15
                    if macd_worsening: score += 0.15
                    if stoch_k > params.get('stoch_overbought', 0.7): score += 0.10
                    if vol_ratio >= params['min_vol_ratio']: score += 0.10
                    if minus_di - plus_di > 5: score += 0.10
                    if d['upper_wick'].iloc[i] > 0.4: score += 0.10
                    signal = -1
                    mode = 'momentum_short'

        # ── REVERSION MODE ─────────────────────────────
        elif not is_trending and not is_dead:
            # Long: oversold bounce
            if bb_p < params['bb_reversion_entry'] and rsi_val < params['rsi_oversold']:
                score = 0.25
                if stoch_k < 0.2: score += 0.20
                if vol_ratio >= params['min_vol_ratio']: score += 0.15
                if macd_hist > d['macd_hist'].iloc[i-1]: score += 0.15
                if close > ema_trend: score += 0.10
                if d['lower_wick'].iloc[i] > 0.5: score += 0.10
                signal = 1
                mode = 'reversion_long'

            # Short: overbought rejection
            elif bb_p > (1 - params['bb_reversion_entry']) and rsi_val > params['rsi_overbought']:
                score = 0.25
                if stoch_k > 0.8: score += 0.20
                if vol_ratio >= params['min_vol_ratio']: score += 0.15
                if macd_hist < d['macd_hist'].iloc[i-1]: score += 0.15
                if close < ema_trend: score += 0.10
                if d['upper_wick'].iloc[i] > 0.5: score += 0.10
                signal = -1
                mode = 'reversion_short'

        # Apply minimum score
        if score < params['min_score'] or signal == 0:
            continue

        # ── Set SL/TP ──────────────────────────────────
        if 'momentum' in mode:
            sl_mult = params['momentum_sl_atr']
            tp_mult = params['momentum_tp_atr']
        else:
            sl_mult = params['reversion_sl_atr']
            tp_mult = params['reversion_tp_atr']

        if signal == 1:
            sl = close - atr_val * sl_mult
            tp = close + atr_val * tp_mult
        else:
            sl = close + atr_val * sl_mult
            tp = close - atr_val * tp_mult

        # Probability & leverage
        prob = min(0.50 + score * 0.30, 0.80)
        lev = 1.0
        if score >= 0.70:
            lev = min(3.0, params.get('max_leverage', 3.0))
        elif score >= 0.60:
            lev = min(2.0, params.get('max_leverage', 3.0))

        d.iat[i, d.columns.get_loc('signal')] = signal
        d.iat[i, d.columns.get_loc('signal_mode')] = mode
        d.iat[i, d.columns.get_loc('score')] = score
        d.iat[i, d.columns.get_loc('stop_loss')] = sl
        d.iat[i, d.columns.get_loc('take_profit')] = tp
        d.iat[i, d.columns.get_loc('probability')] = prob
        d.iat[i, d.columns.get_loc('leverage')] = lev

    return d


# ── Backtest engine ─────────────────────────────────────
def realistic_backtest(all_signals, start_date, end_date, label="",
                       max_hold_bars=48, breakeven_bars=12,
                       trailing_stop_atr=None, cooldown_bars=4):
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
            prob = row.get('probability', 0.55)
            score = row.get('score', 0)
            adx_val = row.get('adx', 0)
            mode = row.get('signal_mode', '')
            atr_val = row.get('atr', 0)

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
                if trailing_stop_atr and bars_held >= 3 and atr_val > 0:
                    if signal == 1:
                        trail_sl = best_price - atr_val * trailing_stop_atr
                        current_sl = max(current_sl, trail_sl)
                    else:
                        trail_sl = best_price + atr_val * trailing_stop_atr
                        current_sl = min(current_sl, trail_sl)

                # Breakeven stop
                if breakeven_bars and bars_held >= breakeven_bars:
                    if signal == 1 and bar['close'] > actual_entry * 1.002:
                        current_sl = max(current_sl, actual_entry)
                    elif signal == -1 and bar['close'] < actual_entry * 0.998:
                        current_sl = min(current_sl, actual_entry)

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
                'probability': prob, 'score': score, 'adx': adx_val, 'mode': mode,
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
        trade_copy['size_mult'] = size_mult
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

    # Mode breakdown
    mode_stats = {}
    for mode_name in result_df['mode'].unique():
        mdf = result_df[result_df['mode'] == mode_name]
        mode_stats[mode_name] = {
            'trades': len(mdf),
            'pnl': round(mdf['adjusted_pnl'].sum(), 2),
            'wr': round((mdf['adjusted_pnl'] > 0).mean() * 100, 1),
        }

    stats = {
        'label': label,
        'total_pnl': round(total_pnl, 2),
        'total_trades': total_trades,
        'win_rate': round(wins / total_trades * 100, 1),
        'avg_trade': round(total_pnl / total_trades, 2),
        'roi_pct': round(total_pnl / INITIAL_CAPITAL * 100, 2),
        'max_drawdown': round(max_dd, 2),
        'profit_factor': round(winning_sum / losing_sum, 2) if losing_sum > 0 else 999,
        'mode_breakdown': mode_stats,
    }

    return result_df, {'stats': stats, 'monthly': monthly.to_dict('records')}


def print_results(label, results):
    if not results:
        print(f"\n{label}: No results")
        return
    s = results['stats']
    print(f"\n  {label}:")
    print(f"    PnL: ${s['total_pnl']:,.2f} | Trades: {s['total_trades']} | WR: {s['win_rate']}% | ROI: {s['roi_pct']}%")
    print(f"    MaxDD: ${s['max_drawdown']:.2f} | PF: {s['profit_factor']} | Avg: ${s['avg_trade']:.2f}")
    if s.get('mode_breakdown'):
        for mode, ms in s['mode_breakdown'].items():
            print(f"      [{mode}] {ms['trades']}t, ${ms['pnl']:.2f}, WR={ms['wr']}%")
    for m in results['monthly']:
        print(f"      {m['month']}: {m['trades']}t, ${m['pnl']:.2f}, WR={m['wr']}%, Cum=${m['cum_pnl']:.2f}")


# ── Parameter configurations ───────────────────────────
CONFIGS = {
    'v3a_balanced': {
        'ema_fast': 12, 'ema_slow': 26, 'ema_trend': 50,
        'bb_period': 20, 'bb_std': 2.0,
        'rsi_period': 14, 'atr_period': 14, 'adx_period': 14, 'vol_period': 20,
        'adx_trend_threshold': 25, 'adx_range_threshold': 15,
        'adx_dead_threshold': 8, 'adx_kill_threshold': 45,
        'min_natr': 0.3, 'min_vol_ratio': 1.0,
        'pullback_pct': 0.5,
        'rsi_momentum_max': 65, 'rsi_momentum_min': 35,
        'stoch_oversold': 0.3, 'stoch_overbought': 0.7,
        'rsi_oversold': 35, 'rsi_overbought': 65,
        'bb_reversion_entry': 0.15,
        'min_score': 0.50,
        'momentum_sl_atr': 1.5, 'momentum_tp_atr': 2.0,
        'reversion_sl_atr': 1.2, 'reversion_tp_atr': 1.5,
        'max_leverage': 3.0,
    },
    'v3b_selective': {
        'ema_fast': 12, 'ema_slow': 26, 'ema_trend': 50,
        'bb_period': 20, 'bb_std': 2.0,
        'rsi_period': 14, 'atr_period': 14, 'adx_period': 14, 'vol_period': 20,
        'adx_trend_threshold': 25, 'adx_range_threshold': 15,
        'adx_dead_threshold': 10, 'adx_kill_threshold': 40,
        'min_natr': 0.4, 'min_vol_ratio': 1.2,
        'pullback_pct': 0.3,
        'rsi_momentum_max': 60, 'rsi_momentum_min': 40,
        'stoch_oversold': 0.25, 'stoch_overbought': 0.75,
        'rsi_oversold': 30, 'rsi_overbought': 70,
        'bb_reversion_entry': 0.10,
        'min_score': 0.55,
        'momentum_sl_atr': 1.5, 'momentum_tp_atr': 2.0,
        'reversion_sl_atr': 1.5, 'reversion_tp_atr': 1.8,
        'max_leverage': 2.0,
    },
    'v3c_momentum_focused': {
        'ema_fast': 12, 'ema_slow': 26, 'ema_trend': 50,
        'bb_period': 20, 'bb_std': 2.0,
        'rsi_period': 14, 'atr_period': 14, 'adx_period': 14, 'vol_period': 20,
        'adx_trend_threshold': 20, 'adx_range_threshold': 100,  # disable reversion
        'adx_dead_threshold': 8, 'adx_kill_threshold': 50,
        'min_natr': 0.3, 'min_vol_ratio': 0.8,
        'pullback_pct': 0.8,
        'rsi_momentum_max': 70, 'rsi_momentum_min': 30,
        'stoch_oversold': 0.35, 'stoch_overbought': 0.65,
        'rsi_oversold': 30, 'rsi_overbought': 70,
        'bb_reversion_entry': 0.10,
        'min_score': 0.45,
        'momentum_sl_atr': 1.5, 'momentum_tp_atr': 2.5,
        'reversion_sl_atr': 1.2, 'reversion_tp_atr': 1.5,
        'max_leverage': 3.0,
    },
    'v3d_tight_stops': {
        'ema_fast': 9, 'ema_slow': 21, 'ema_trend': 50,
        'bb_period': 20, 'bb_std': 2.0,
        'rsi_period': 14, 'atr_period': 14, 'adx_period': 14, 'vol_period': 20,
        'adx_trend_threshold': 22, 'adx_range_threshold': 12,
        'adx_dead_threshold': 8, 'adx_kill_threshold': 45,
        'min_natr': 0.3, 'min_vol_ratio': 1.0,
        'pullback_pct': 0.5,
        'rsi_momentum_max': 65, 'rsi_momentum_min': 35,
        'stoch_oversold': 0.30, 'stoch_overbought': 0.70,
        'rsi_oversold': 35, 'rsi_overbought': 65,
        'bb_reversion_entry': 0.15,
        'min_score': 0.50,
        'momentum_sl_atr': 1.0, 'momentum_tp_atr': 1.5,
        'reversion_sl_atr': 0.8, 'reversion_tp_atr': 1.2,
        'max_leverage': 2.0,
    },
    'v3e_wide_trail': {
        'ema_fast': 12, 'ema_slow': 26, 'ema_trend': 50,
        'bb_period': 20, 'bb_std': 2.0,
        'rsi_period': 14, 'atr_period': 14, 'adx_period': 14, 'vol_period': 20,
        'adx_trend_threshold': 23, 'adx_range_threshold': 13,
        'adx_dead_threshold': 8, 'adx_kill_threshold': 45,
        'min_natr': 0.3, 'min_vol_ratio': 1.0,
        'pullback_pct': 0.5,
        'rsi_momentum_max': 65, 'rsi_momentum_min': 35,
        'stoch_oversold': 0.30, 'stoch_overbought': 0.70,
        'rsi_oversold': 33, 'rsi_overbought': 67,
        'bb_reversion_entry': 0.12,
        'min_score': 0.50,
        'momentum_sl_atr': 2.0, 'momentum_tp_atr': 3.0,
        'reversion_sl_atr': 1.5, 'reversion_tp_atr': 2.0,
        'max_leverage': 2.0,
    },
}


# ── Run all configs ─────────────────────────────────────
all_results = {}

for config_name, params in CONFIGS.items():
    print(f"\n{'='*60}")
    print(f"Testing: {config_name}")
    print(f"{'='*60}")

    config_signals = {}
    for coin, df in all_data.items():
        try:
            config_signals[coin] = generate_signals_v3(df, params)
        except Exception as e:
            print(f"  {coin}: signal error - {e}")

    total_sigs = sum((df['signal'] != 0).sum() for df in config_signals.values())
    print(f"  Total signals: {total_sigs}")

    # Determine if trailing stop should be used
    use_trail = 2.0 if 'momentum' in config_name or 'trail' in config_name else None

    _, r2025 = realistic_backtest(config_signals, '2025-01-01', '2025-12-31',
                                   label=f'{config_name}_2025',
                                   max_hold_bars=48, breakeven_bars=12,
                                   trailing_stop_atr=use_trail, cooldown_bars=4)
    print_results('2025', r2025)

    _, r2026 = realistic_backtest(config_signals, '2026-01-01', '2026-04-12',
                                   label=f'{config_name}_2026',
                                   max_hold_bars=48, breakeven_bars=12,
                                   trailing_stop_atr=use_trail, cooldown_bars=4)
    print_results('2026 Q1', r2026)

    all_results[config_name] = {'2025': r2025, '2026': r2026}


# ── Summary ─────────────────────────────────────────────
print("\n\n" + "="*95)
print("COMPARISON SUMMARY - ADAPTIVE V3")
print("="*95)
print(f"{'Config':<25} {'2025 PnL':>10} {'25 WR':>6} {'25 PF':>6} {'25 Tr':>6} {'2026 PnL':>10} {'26 WR':>6} {'26 PF':>6} {'26 Tr':>6}")
print("-"*95)
for name, res in all_results.items():
    r25 = res.get('2025', {}).get('stats', {})
    r26 = res.get('2026', {}).get('stats', {})
    print(f"{name:<25} ${r25.get('total_pnl',0):>9,.2f} {r25.get('win_rate',0):>5.1f}% {r25.get('profit_factor',0):>5.2f} "
          f"{r25.get('total_trades',0):>5} "
          f"${r26.get('total_pnl',0):>9,.2f} {r26.get('win_rate',0):>5.1f}% {r26.get('profit_factor',0):>5.2f} "
          f"{r26.get('total_trades',0):>5}")


# Save
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

with open('C:/Users/koray/projeler/oto-bot/artifacts/v3_adaptive_results.json', 'w') as f:
    json.dump(make_serializable(all_results), f, indent=2, default=str)

print("\nResults saved to artifacts/v3_adaptive_results.json")
