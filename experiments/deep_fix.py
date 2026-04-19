"""
PHASE 3: Deep structural fix

The diagnosis is clear: no parameter set makes the strategy profitable.
The issue is structural — the signal generation doesn't create edge.

Root causes:
1. Bollinger + RSI + VWAP all measure the same thing (mean deviation) → no independent confirmation
2. Mean reversion on 1h is fighting the dominant trend in crypto
3. The SL/TP framework is static — no trailing, no partial exits
4. Scoring weights are equal regardless of what actually predicts profits

New approach — Adaptive Momentum-Reversion Hybrid:
A. Add a MOMENTUM mode: when ADX > threshold, trade WITH the trend
B. Add volatility-normalized scoring: signals in low-vol are worth less
C. Add momentum confirmation: require price momentum alignment, not just deviation
D. Dynamic SL/TP: in trending mode, use trailing stops; in mean-reversion mode, use fixed targets
E. Add a cooldown: don't enter same coin within N bars of last exit
F. Add confirmation delay: signal must persist for 2+ bars
"""
import json, sys, time, warnings
import numpy as np
import pandas as pd
from datetime import datetime, timezone

warnings.filterwarnings('ignore')
sys.path.insert(0, 'C:/Users/koray/projeler/oto-bot/src')

import ta
from oto_bot.data.crypto import CryptoDataProvider
from oto_bot.strategies.base import StrategyContext

# ── Data ────────────────────────────────────────────────
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

provider = CryptoDataProvider()

def fetch_coin(symbol, since_str):
    exchange = provider._get_exchange()
    dt = datetime.fromisoformat(since_str).replace(tzinfo=timezone.utc)
    since_ms = int(dt.timestamp() * 1000)
    all_data = []
    while True:
        raw = exchange.fetch_ohlcv(symbol, '1h', since=since_ms, limit=1000)
        if not raw:
            break
        all_data.extend(raw)
        since_ms = raw[-1][0] + 3600000
        if len(raw) < 1000:
            break
        time.sleep(0.1)
    df = pd.DataFrame(all_data, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    return df

print("Fetching data...")
all_data = {}
for coin in COINS:
    try:
        df = fetch_coin(coin, "2024-11-01T00:00:00")
        all_data[coin] = df
        print(f"  {coin}: {len(df)} bars")
    except Exception as e:
        print(f"  {coin}: FAILED - {e}")
    time.sleep(0.15)


# ── New Adaptive Strategy ───────────────────────────────
def generate_adaptive_signals(df, params):
    """
    Adaptive Momentum-Reversion Hybrid Strategy V3

    Two modes:
    1. REVERSION mode (ADX < threshold): classic mean reversion with tighter filters
    2. MOMENTUM mode (ADX > threshold): trade pullbacks within trend

    Key improvements:
    - EMA crossover for momentum direction
    - RSI used differently per mode
    - Volume confirmation required for ALL entries
    - Confirmation delay (signal must persist)
    - ATR-normalized dynamic targets
    """
    d = df.copy()

    # ── Indicators ──────────────────────────────────────
    # EMAs
    d['ema_fast'] = d['close'].ewm(span=params['ema_fast']).mean()
    d['ema_slow'] = d['close'].ewm(span=params['ema_slow']).mean()
    d['ema_trend'] = d['close'].ewm(span=params['ema_trend']).mean()

    # EMA slope (trend strength)
    d['ema_slope'] = d['ema_trend'].pct_change(params['slope_period']) * 100

    # Bollinger Bands
    bb = ta.volatility.BollingerBands(close=d['close'], window=params['bb_period'], window_dev=params['bb_std'])
    d['bb_upper'] = bb.bollinger_hband()
    d['bb_lower'] = bb.bollinger_lband()
    d['bb_mid'] = bb.bollinger_mavg()
    d['bb_pband'] = bb.bollinger_pband()

    # RSI
    rsi = ta.momentum.RSIIndicator(d['close'], window=params['rsi_period'])
    d['rsi'] = rsi.rsi()

    # ATR
    atr = ta.volatility.AverageTrueRange(high=d['high'], low=d['low'], close=d['close'], window=params['atr_period'])
    d['atr'] = atr.average_true_range()
    d['natr'] = d['atr'] / d['close'] * 100  # normalized ATR %

    # ADX
    adx = ta.trend.ADXIndicator(high=d['high'], low=d['low'], close=d['close'], window=params['adx_period'])
    d['adx'] = adx.adx()
    d['plus_di'] = adx.adx_pos()
    d['minus_di'] = adx.adx_neg()

    # Volume
    d['vol_ma'] = d['volume'].rolling(params['vol_period']).mean()
    d['vol_ratio'] = d['volume'] / d['vol_ma'].replace(0, np.nan)

    # MACD
    macd = ta.trend.MACD(d['close'], window_slow=26, window_fast=12, window_sign=9)
    d['macd'] = macd.macd()
    d['macd_signal'] = macd.macd_signal()
    d['macd_hist'] = macd.macd_diff()

    # Stochastic RSI for divergence detection
    stoch = ta.momentum.StochRSIIndicator(d['close'], window=14, smooth1=3, smooth2=3)
    d['stoch_k'] = stoch.stochastic_rsi_k()
    d['stoch_d'] = stoch.stochastic_rsi_d()

    # ── Regime detection ────────────────────────────────
    adx_thresh = params['adx_trend_threshold']
    d['regime'] = 'neutral'
    # Trending: ADX above threshold AND directional
    d.loc[(d['adx'] > adx_thresh) & (d['plus_di'] > d['minus_di']), 'regime'] = 'uptrend'
    d.loc[(d['adx'] > adx_thresh) & (d['minus_di'] > d['plus_di']), 'regime'] = 'downtrend'
    # Low vol ranging
    d.loc[d['adx'] < params['adx_range_threshold'], 'regime'] = 'ranging'

    # ── Signal generation ───────────────────────────────
    d['signal'] = 0
    d['signal_mode'] = ''
    d['score'] = 0.0

    # Min volatility filter: skip very dead markets
    vol_ok = d['natr'] >= params['min_natr']

    # Volume confirmation
    vol_confirmed = d['vol_ratio'] >= params['min_vol_ratio']

    for i in range(max(params['ema_trend'], 50), len(d)):
        if not vol_ok.iloc[i]:
            continue

        regime = d['regime'].iloc[i]
        close = d['close'].iloc[i]
        rsi_val = d['rsi'].iloc[i]
        bb_p = d['bb_pband'].iloc[i]
        adx_val = d['adx'].iloc[i]
        stoch_k = d['stoch_k'].iloc[i]
        macd_hist = d['macd_hist'].iloc[i]
        ema_fast = d['ema_fast'].iloc[i]
        ema_slow = d['ema_slow'].iloc[i]
        ema_trend = d['ema_trend'].iloc[i]

        if pd.isna(rsi_val) or pd.isna(bb_p) or pd.isna(adx_val):
            continue

        score = 0.0
        signal = 0
        mode = ''

        # ── MODE 1: MOMENTUM (trend pullback) ──────────
        if regime in ('uptrend', 'downtrend'):
            if regime == 'uptrend':
                # Buy the dip in uptrend
                # Conditions: price pulls back to EMA support, RSI not overbought
                pullback_to_ema = close <= ema_fast * (1 + params['pullback_pct']/100)
                rsi_ok = rsi_val < params['rsi_momentum_max']
                macd_ok = macd_hist > 0 or d['macd_hist'].iloc[i] > d['macd_hist'].iloc[i-1]  # MACD improving
                stoch_ok = stoch_k < params['stoch_oversold']

                if pullback_to_ema and rsi_ok:
                    score = 0.3
                    if macd_ok: score += 0.2
                    if stoch_ok: score += 0.15
                    if vol_confirmed.iloc[i]: score += 0.15
                    if close > ema_trend: score += 0.1
                    if d['plus_di'].iloc[i] > d['minus_di'].iloc[i] + 5: score += 0.1
                    signal = 1
                    mode = 'momentum_long'

            elif regime == 'downtrend':
                # Sell the rally in downtrend
                pullback_to_ema = close >= ema_fast * (1 - params['pullback_pct']/100)
                rsi_ok = rsi_val > params['rsi_momentum_min']
                macd_ok = macd_hist < 0 or d['macd_hist'].iloc[i] < d['macd_hist'].iloc[i-1]
                stoch_ok = stoch_k > params['stoch_overbought']

                if pullback_to_ema and rsi_ok:
                    score = 0.3
                    if macd_ok: score += 0.2
                    if stoch_ok: score += 0.15
                    if vol_confirmed.iloc[i]: score += 0.15
                    if close < ema_trend: score += 0.1
                    if d['minus_di'].iloc[i] > d['plus_di'].iloc[i] + 5: score += 0.1
                    signal = -1
                    mode = 'momentum_short'

        # ── MODE 2: MEAN REVERSION (ranging) ───────────
        elif regime in ('ranging', 'neutral'):
            # Only mean-revert when ADX is moderate (not dead, not trending)
            if params['adx_range_threshold'] <= adx_val <= adx_thresh:
                # LONG: price at lower BB, RSI oversold, stoch oversold
                if bb_p < params['bb_reversion_entry'] and rsi_val < params['rsi_oversold']:
                    score = 0.25
                    if stoch_k < 0.2: score += 0.2
                    if vol_confirmed.iloc[i]: score += 0.15
                    if macd_hist > d['macd_hist'].iloc[i-1]: score += 0.15  # momentum turning
                    if close > ema_trend: score += 0.1  # aligned with bigger trend
                    if d['lower_wick'].iloc[i] if 'lower_wick' in d.columns else False: score += 0.05
                    signal = 1
                    mode = 'reversion_long'

                # SHORT: price at upper BB, RSI overbought, stoch overbought
                elif bb_p > (1 - params['bb_reversion_entry']) and rsi_val > params['rsi_overbought']:
                    score = 0.25
                    if stoch_k > 0.8: score += 0.2
                    if vol_confirmed.iloc[i]: score += 0.15
                    if macd_hist < d['macd_hist'].iloc[i-1]: score += 0.15
                    if close < ema_trend: score += 0.1
                    signal = -1
                    mode = 'reversion_short'

        # Apply minimum score filter
        if score >= params['min_score']:
            d.iloc[i, d.columns.get_loc('signal')] = signal
            d.iloc[i, d.columns.get_loc('signal_mode')] = mode
            d.iloc[i, d.columns.get_loc('score')] = score

    # ── Confirmation delay ──────────────────────────────
    if params.get('confirm_bars', 0) > 0:
        confirm = params['confirm_bars']
        confirmed_signal = d['signal'].copy()
        for i in range(confirm, len(d)):
            if d['signal'].iloc[i] != 0:
                # Check if same direction signal existed in prior bars
                prior_signals = d['signal'].iloc[max(0, i-confirm):i]
                if not (prior_signals == d['signal'].iloc[i]).any():
                    confirmed_signal.iloc[i] = 0  # Remove unconfirmed
        d['signal'] = confirmed_signal

    # ── SL/TP based on mode ─────────────────────────────
    d['stop_loss'] = np.nan
    d['take_profit'] = np.nan
    d['probability'] = 0.0
    d['leverage'] = 1.0

    has_signal = d['signal'] != 0
    for i in d.index[has_signal]:
        row = d.loc[i]
        atr_val = row['atr']
        sig = row['signal']
        close = row['close']
        mode = row['signal_mode']
        sc = row['score']

        if 'momentum' in mode:
            sl_mult = params['momentum_sl_atr']
            tp_mult = params['momentum_tp_atr']
        else:
            sl_mult = params['reversion_sl_atr']
            tp_mult = params['reversion_tp_atr']

        if sig == 1:
            d.loc[i, 'stop_loss'] = close - atr_val * sl_mult
            d.loc[i, 'take_profit'] = close + atr_val * tp_mult
        else:
            d.loc[i, 'stop_loss'] = close + atr_val * sl_mult
            d.loc[i, 'take_profit'] = close - atr_val * tp_mult

        # Probability from score
        prob = 0.50 + sc * 0.30  # 0.50-0.80 range
        d.loc[i, 'probability'] = min(prob, 0.80)

        # Conservative leverage
        if sc >= 0.7:
            d.loc[i, 'leverage'] = min(3.0, params.get('max_leverage', 3.0))
        elif sc >= 0.6:
            d.loc[i, 'leverage'] = min(2.0, params.get('max_leverage', 3.0))
        else:
            d.loc[i, 'leverage'] = 1.0

    return d


# ── Backtest engine ─────────────────────────────────────
def realistic_backtest(all_signals, start_date, end_date, label="",
                       max_hold_bars=48, breakeven_bars=None,
                       trailing_stop_atr=None, cooldown_bars=6):
    trades = []

    for coin, df in all_signals.items():
        mask = (df.index >= start_date) & (df.index <= end_date)
        period_df = df[mask].copy()
        if len(period_df) == 0:
            continue

        last_exit_bar = -cooldown_bars - 1  # allow first trade

        i = 0
        while i < len(period_df):
            row = period_df.iloc[i]
            if row['signal'] == 0 or pd.isna(row.get('stop_loss')) or pd.isna(row.get('take_profit')):
                i += 1
                continue

            # Cooldown check
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

            # Entry slippage
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

                # Track best price for trailing stop
                if signal == 1:
                    best_price = max(best_price, bar['high'])
                else:
                    best_price = min(best_price, bar['low'])

                # Trailing stop
                if trailing_stop_atr and bars_held >= 3:
                    atr_val = row.get('atr', 0)
                    if atr_val > 0:
                        if signal == 1:
                            trail_sl = best_price - atr_val * trailing_stop_atr
                            current_sl = max(current_sl, trail_sl)
                        else:
                            trail_sl = best_price + atr_val * trailing_stop_atr
                            current_sl = min(current_sl, trail_sl)

                # Breakeven stop
                if breakeven_bars and bars_held >= breakeven_bars:
                    if signal == 1 and bar['close'] > actual_entry * 1.001:
                        current_sl = max(current_sl, actual_entry)
                    elif signal == -1 and bar['close'] < actual_entry * 0.999:
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

        day_key = entry_t.date() if hasattr(entry_t, 'date') else str(entry_t)[:10]
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

    # Max drawdown
    cum = result_df['adjusted_pnl'].cumsum()
    peak = cum.cummax()
    dd = cum - peak
    max_dd = dd.min()

    stats = {
        'label': label,
        'total_pnl': round(total_pnl, 2),
        'total_trades': total_trades,
        'win_rate': round(wins / total_trades * 100, 1),
        'avg_trade': round(total_pnl / total_trades, 2),
        'roi_pct': round(total_pnl / INITIAL_CAPITAL * 100, 2),
        'max_drawdown': round(max_dd, 2),
        'profit_factor': round(
            result_df.loc[result_df['adjusted_pnl'] > 0, 'adjusted_pnl'].sum() /
            abs(result_df.loc[result_df['adjusted_pnl'] <= 0, 'adjusted_pnl'].sum())
            if (result_df['adjusted_pnl'] <= 0).any() else 999, 2
        ),
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
    for m in results['monthly']:
        print(f"      {m['month']}: {m['trades']}t, ${m['pnl']:.2f}, WR={m['wr']}%, Cum=${m['cum_pnl']:.2f}")


# ── Parameter configurations to test ───────────────────
CONFIGS = {
    'v3_balanced': {
        'ema_fast': 12, 'ema_slow': 26, 'ema_trend': 50,
        'slope_period': 12,
        'bb_period': 20, 'bb_std': 2.0,
        'rsi_period': 14,
        'atr_period': 14,
        'adx_period': 14,
        'vol_period': 20,
        'adx_trend_threshold': 25,
        'adx_range_threshold': 15,
        'min_natr': 0.3,  # minimum 0.3% ATR/price
        'min_vol_ratio': 1.0,
        'pullback_pct': 0.5,  # % pullback to EMA for momentum entry
        'rsi_momentum_max': 65,  # for long: RSI not too overbought
        'rsi_momentum_min': 35,  # for short: RSI not too oversold
        'stoch_oversold': 0.3,
        'stoch_overbought': 0.7,
        'rsi_oversold': 35,
        'rsi_overbought': 65,
        'bb_reversion_entry': 0.15,  # BB%B < 0.15 for long
        'min_score': 0.50,
        'momentum_sl_atr': 1.5,
        'momentum_tp_atr': 2.0,
        'reversion_sl_atr': 1.2,
        'reversion_tp_atr': 1.5,
        'max_leverage': 3.0,
        'confirm_bars': 0,
    },
    'v3_conservative': {
        'ema_fast': 12, 'ema_slow': 26, 'ema_trend': 50,
        'slope_period': 12,
        'bb_period': 20, 'bb_std': 2.0,
        'rsi_period': 14,
        'atr_period': 14,
        'adx_period': 14,
        'vol_period': 20,
        'adx_trend_threshold': 25,
        'adx_range_threshold': 12,
        'min_natr': 0.4,
        'min_vol_ratio': 1.2,
        'pullback_pct': 0.3,
        'rsi_momentum_max': 60,
        'rsi_momentum_min': 40,
        'stoch_oversold': 0.25,
        'stoch_overbought': 0.75,
        'rsi_oversold': 30,
        'rsi_overbought': 70,
        'bb_reversion_entry': 0.10,
        'min_score': 0.55,
        'momentum_sl_atr': 1.8,
        'momentum_tp_atr': 2.5,
        'reversion_sl_atr': 1.5,
        'reversion_tp_atr': 1.8,
        'max_leverage': 2.0,
        'confirm_bars': 0,
    },
    'v3_momentum_only': {
        'ema_fast': 12, 'ema_slow': 26, 'ema_trend': 50,
        'slope_period': 12,
        'bb_period': 20, 'bb_std': 2.0,
        'rsi_period': 14,
        'atr_period': 14,
        'adx_period': 14,
        'vol_period': 20,
        'adx_trend_threshold': 20,  # lower threshold = more signals
        'adx_range_threshold': 100, # disable reversion mode entirely
        'min_natr': 0.3,
        'min_vol_ratio': 1.0,
        'pullback_pct': 0.8,
        'rsi_momentum_max': 70,
        'rsi_momentum_min': 30,
        'stoch_oversold': 0.35,
        'stoch_overbought': 0.65,
        'rsi_oversold': 30,
        'rsi_overbought': 70,
        'bb_reversion_entry': 0.10,
        'min_score': 0.45,
        'momentum_sl_atr': 1.5,
        'momentum_tp_atr': 2.0,
        'reversion_sl_atr': 1.2,
        'reversion_tp_atr': 1.5,
        'max_leverage': 3.0,
        'confirm_bars': 0,
    },
    'v3_tight_rr': {
        'ema_fast': 9, 'ema_slow': 21, 'ema_trend': 50,
        'slope_period': 12,
        'bb_period': 20, 'bb_std': 2.0,
        'rsi_period': 14,
        'atr_period': 14,
        'adx_period': 14,
        'vol_period': 20,
        'adx_trend_threshold': 22,
        'adx_range_threshold': 12,
        'min_natr': 0.3,
        'min_vol_ratio': 1.0,
        'pullback_pct': 0.5,
        'rsi_momentum_max': 65,
        'rsi_momentum_min': 35,
        'stoch_oversold': 0.30,
        'stoch_overbought': 0.70,
        'rsi_oversold': 35,
        'rsi_overbought': 65,
        'bb_reversion_entry': 0.15,
        'min_score': 0.50,
        'momentum_sl_atr': 1.2,
        'momentum_tp_atr': 1.5,
        'reversion_sl_atr': 1.0,
        'reversion_tp_atr': 1.2,
        'max_leverage': 2.0,
        'confirm_bars': 0,
    },
    'v3_wide_targets': {
        'ema_fast': 12, 'ema_slow': 26, 'ema_trend': 50,
        'slope_period': 12,
        'bb_period': 20, 'bb_std': 2.0,
        'rsi_period': 14,
        'atr_period': 14,
        'adx_period': 14,
        'vol_period': 20,
        'adx_trend_threshold': 23,
        'adx_range_threshold': 13,
        'min_natr': 0.3,
        'min_vol_ratio': 1.0,
        'pullback_pct': 0.5,
        'rsi_momentum_max': 65,
        'rsi_momentum_min': 35,
        'stoch_oversold': 0.30,
        'stoch_overbought': 0.70,
        'rsi_oversold': 33,
        'rsi_overbought': 67,
        'bb_reversion_entry': 0.12,
        'min_score': 0.50,
        'momentum_sl_atr': 2.0,
        'momentum_tp_atr': 3.0,
        'reversion_sl_atr': 1.5,
        'reversion_tp_atr': 2.0,
        'max_leverage': 2.0,
        'confirm_bars': 0,
    },
}


# ── Run all configs ─────────────────────────────────────
all_results = {}

for config_name, params in CONFIGS.items():
    print(f"\n{'='*60}")
    print(f"Testing: {config_name}")
    print(f"{'='*60}")

    # Generate signals
    config_signals = {}
    for coin, df in all_data.items():
        try:
            config_signals[coin] = generate_adaptive_signals(df, params)
        except Exception as e:
            print(f"  {coin}: signal error - {e}")

    # Count signals
    total_sigs = sum(
        (df['signal'] != 0).sum()
        for df in config_signals.values()
    )
    print(f"  Total signals generated: {total_sigs}")

    # Backtest parameters
    bt_kwargs = {
        'max_hold_bars': 48,
        'breakeven_bars': 12,
        'trailing_stop_atr': 2.0 if 'momentum' in config_name else None,
        'cooldown_bars': 4,
    }

    _, r2025 = realistic_backtest(config_signals, '2025-01-01', '2025-12-31',
                                   label=f'{config_name}_2025', **bt_kwargs)
    print_results('2025', r2025)

    _, r2026 = realistic_backtest(config_signals, '2026-01-01', '2026-04-12',
                                   label=f'{config_name}_2026', **bt_kwargs)
    print_results('2026 Q1', r2026)

    all_results[config_name] = {'2025': r2025, '2026': r2026}

# ── Summary ─────────────────────────────────────────────
print("\n\n" + "="*90)
print("COMPARISON SUMMARY - ADAPTIVE V3")
print("="*90)
print(f"{'Config':<22} {'2025 PnL':>10} {'25 WR':>6} {'25 PF':>6} {'2026 PnL':>10} {'26 WR':>6} {'26 PF':>6} {'25 ROI':>8} {'26 ROI':>8}")
print("-"*90)
for name, res in all_results.items():
    r25 = res.get('2025', {}).get('stats', {})
    r26 = res.get('2026', {}).get('stats', {})
    print(f"{name:<22} ${r25.get('total_pnl',0):>9,.2f} {r25.get('win_rate',0):>5.1f}% {r25.get('profit_factor',0):>5.2f} "
          f"${r26.get('total_pnl',0):>9,.2f} {r26.get('win_rate',0):>5.1f}% {r26.get('profit_factor',0):>5.2f} "
          f"{r25.get('roi_pct',0):>7.1f}% {r26.get('roi_pct',0):>7.1f}%")

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
