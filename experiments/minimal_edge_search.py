"""
PHASE 4: Minimal edge search

Previous attempts showed that more signals = more losses.
The cost per trade is ~$1.80 ($1.20 commission + $0.60 slippage).
With $600 notional at 1x, that's 0.3% eaten per trade.

Need to find: a simple signal that has >50% accuracy on at least 1.5 ATR moves.

Approach: test SIMPLE single-indicator signals and measure their raw accuracy
BEFORE running full backtest. Find what actually predicts direction.
"""
import json, sys, time, warnings, pickle
import numpy as np
import pandas as pd
from datetime import datetime, timezone

warnings.filterwarnings('ignore')
sys.path.insert(0, '/Users/ibrahimpeyman/Documents/oto-bot/src')

import ta

# ── Load cached data ────────────────────────────────────
CACHE_FILE = '/Users/ibrahimpeyman/Documents/oto-bot/artifacts/data_cache.pkl'
with open(CACHE_FILE, 'rb') as f:
    all_data = pickle.load(f)
print(f"Loaded {len(all_data)} coins from cache")

COMMISSION_RATE = 0.001
SLIPPAGE_RATE = 0.0005
NOTIONAL = 600.0
INITIAL_CAPITAL = 10000.0


# ── Signal Accuracy Testing ────────────────────────────
# For each signal type, measure: given signal fires, what % of time
# does price move favorably by at least X ATR within N bars?

def test_signal_accuracy(all_data, signal_func, sl_atr, tp_atr, max_bars=24, label=""):
    """
    Test a signal function for raw accuracy across all coins.
    Returns stats about how often signals lead to TP vs SL.
    """
    results = []

    for coin, df in all_data.items():
        d = df.copy()

        # Standard indicators
        atr = ta.volatility.AverageTrueRange(high=d['high'], low=d['low'], close=d['close'], window=14)
        d['atr'] = atr.average_true_range()

        # Generate signals using the signal function
        signals = signal_func(d)

        # Filter to 2025-2026 period
        mask = d.index >= '2025-01-01'
        signals = signals[mask]
        d = d[mask]

        for idx in signals.index[signals != 0]:
            i = d.index.get_loc(idx)
            if i + max_bars >= len(d):
                continue

            sig = signals[idx]
            entry = d['close'].iloc[i]
            atr_val = d['atr'].iloc[i]

            if pd.isna(atr_val) or atr_val <= 0:
                continue

            sl_dist = atr_val * sl_atr
            tp_dist = atr_val * tp_atr

            if sig == 1:
                sl_price = entry - sl_dist
                tp_price = entry + tp_dist
            else:
                sl_price = entry + sl_dist
                tp_price = entry - tp_dist

            # Walk forward
            outcome = 'timeout'
            exit_bar = max_bars
            for j in range(i+1, min(i + max_bars + 1, len(d))):
                bar = d.iloc[j]
                if sig == 1:
                    if bar['low'] <= sl_price:
                        outcome = 'sl'
                        exit_bar = j - i
                        break
                    if bar['high'] >= tp_price:
                        outcome = 'tp'
                        exit_bar = j - i
                        break
                else:
                    if bar['high'] >= sl_price:
                        outcome = 'sl'
                        exit_bar = j - i
                        break
                    if bar['low'] <= tp_price:
                        outcome = 'tp'
                        exit_bar = j - i
                        break

            # For timeout, measure direction
            if outcome == 'timeout':
                final_price = d['close'].iloc[min(i + max_bars, len(d)-1)]
                if sig == 1:
                    pnl_pct = (final_price - entry) / entry
                else:
                    pnl_pct = (entry - final_price) / entry
                outcome = 'win' if pnl_pct > 0 else 'loss'

            month = d.index[i].to_period('M')

            results.append({
                'coin': coin, 'signal': sig, 'outcome': outcome,
                'exit_bar': exit_bar, 'month': month,
            })

    if not results:
        return {}

    rdf = pd.DataFrame(results)

    tp_count = (rdf['outcome'] == 'tp').sum()
    sl_count = (rdf['outcome'] == 'sl').sum()
    total = len(rdf)
    win_count = tp_count + (rdf['outcome'] == 'win').sum()

    # Monthly breakdown
    monthly = rdf.groupby('month').apply(
        lambda g: pd.Series({
            'n': len(g),
            'tp': (g['outcome'] == 'tp').sum(),
            'sl': (g['outcome'] == 'sl').sum(),
            'wr': ((g['outcome'].isin(['tp', 'win'])).sum() / len(g) * 100),
        })
    )

    return {
        'label': label,
        'total_signals': total,
        'tp_rate': round(tp_count / total * 100, 1) if total > 0 else 0,
        'sl_rate': round(sl_count / total * 100, 1) if total > 0 else 0,
        'directional_wr': round(win_count / total * 100, 1) if total > 0 else 0,
        'avg_exit_bar': round(rdf['exit_bar'].mean(), 1),
        'monthly': monthly.to_dict('index'),
    }


# ── Define simple signal functions ──────────────────────

def signal_rsi_extreme(df, period=14, oversold=25, overbought=75):
    """RSI extreme bounce/rejection."""
    rsi = ta.momentum.RSIIndicator(df['close'], window=period).rsi()
    signals = pd.Series(0, index=df.index)
    signals[rsi < oversold] = 1   # oversold -> long
    signals[rsi > overbought] = -1 # overbought -> short
    return signals

def signal_bb_extreme(df, period=20, std=2.5):
    """Price touching extreme Bollinger Bands."""
    bb = ta.volatility.BollingerBands(df['close'], window=period, window_dev=std)
    signals = pd.Series(0, index=df.index)
    signals[df['close'] < bb.bollinger_lband()] = 1
    signals[df['close'] > bb.bollinger_hband()] = -1
    return signals

def signal_ema_cross(df, fast=9, slow=21):
    """EMA crossover - trend following."""
    ema_fast = df['close'].ewm(span=fast).mean()
    ema_slow = df['close'].ewm(span=slow).mean()
    signals = pd.Series(0, index=df.index)
    # Bullish cross: fast crosses above slow
    cross_up = (ema_fast > ema_slow) & (ema_fast.shift(1) <= ema_slow.shift(1))
    cross_down = (ema_fast < ema_slow) & (ema_fast.shift(1) >= ema_slow.shift(1))
    signals[cross_up] = 1
    signals[cross_down] = -1
    return signals

def signal_momentum_pullback(df):
    """Trend + pullback: price above EMA50, pulls back to EMA20, bounces."""
    ema20 = df['close'].ewm(span=20).mean()
    ema50 = df['close'].ewm(span=50).mean()
    rsi = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
    adx = ta.trend.ADXIndicator(high=df['high'], low=df['low'], close=df['close'], window=14)

    signals = pd.Series(0, index=df.index)

    # Long: uptrend + pullback to support + RSI recovering
    uptrend = (ema20 > ema50) & (adx.adx() > 20)
    near_ema20 = (df['close'] >= ema20 * 0.995) & (df['close'] <= ema20 * 1.01)
    rsi_ok = (rsi > 35) & (rsi < 60)
    signals[uptrend & near_ema20 & rsi_ok] = 1

    # Short: downtrend + rally to resistance
    downtrend = (ema20 < ema50) & (adx.adx() > 20)
    near_ema20_resist = (df['close'] <= ema20 * 1.005) & (df['close'] >= ema20 * 0.99)
    rsi_high = (rsi > 40) & (rsi < 65)
    signals[downtrend & near_ema20_resist & rsi_high] = -1

    return signals

def signal_volume_breakout(df):
    """Volume spike + directional candle."""
    vol_ma = df['volume'].rolling(20).mean()
    vol_spike = df['volume'] > vol_ma * 2.0
    body = df['close'] - df['open']
    big_body = body.abs() > (df['high'] - df['low']) * 0.6  # strong candle

    signals = pd.Series(0, index=df.index)
    signals[vol_spike & big_body & (body > 0)] = 1   # bullish breakout
    signals[vol_spike & big_body & (body < 0)] = -1  # bearish breakout
    return signals

def signal_macd_divergence(df):
    """MACD histogram turning from extreme."""
    macd = ta.trend.MACD(df['close'])
    hist = macd.macd_diff()
    signals = pd.Series(0, index=df.index)

    # Histogram turning positive from deep negative
    turning_up = (hist > hist.shift(1)) & (hist.shift(1) < hist.shift(2)) & (hist < 0)
    turning_down = (hist < hist.shift(1)) & (hist.shift(1) > hist.shift(2)) & (hist > 0)
    signals[turning_up] = 1
    signals[turning_down] = -1
    return signals

def signal_combined_strict(df):
    """
    Combined signal: only fires when multiple independent signals agree.
    Requires: RSI extreme + BB extreme + volume confirmation.
    """
    rsi = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
    bb = ta.volatility.BollingerBands(df['close'], window=20, window_dev=2.0)
    vol_ma = df['volume'].rolling(20).mean()
    vol_ok = df['volume'] > vol_ma * 1.3
    adx = ta.trend.ADXIndicator(high=df['high'], low=df['low'], close=df['close'], window=14).adx()

    signals = pd.Series(0, index=df.index)

    # Long: RSI < 30 + price below lower BB + volume above average + low ADX
    long_cond = (rsi < 30) & (df['close'] < bb.bollinger_lband()) & vol_ok & (adx < 25)
    # Short: RSI > 70 + price above upper BB + volume + low ADX
    short_cond = (rsi > 70) & (df['close'] > bb.bollinger_hband()) & vol_ok & (adx < 25)

    signals[long_cond] = 1
    signals[short_cond] = -1
    return signals

def signal_trend_follow_adx(df):
    """Pure trend following: enter with trend when ADX confirms, exit when weakens."""
    ema20 = df['close'].ewm(span=20).mean()
    ema50 = df['close'].ewm(span=50).mean()
    adx_ind = ta.trend.ADXIndicator(high=df['high'], low=df['low'], close=df['close'], window=14)
    adx = adx_ind.adx()
    plus_di = adx_ind.adx_pos()
    minus_di = adx_ind.adx_neg()

    signals = pd.Series(0, index=df.index)

    # Long: ADX rising, +DI > -DI, price above EMA20 > EMA50
    strong_up = (adx > 25) & (plus_di > minus_di) & (ema20 > ema50)
    # Only signal on first bar of confirmation (avoid repeat signals)
    entry_up = strong_up & (~strong_up.shift(1).fillna(False))
    signals[entry_up] = 1

    # Short: ADX rising, -DI > +DI, price below EMA20 < EMA50
    strong_down = (adx > 25) & (minus_di > plus_di) & (ema20 < ema50)
    entry_down = strong_down & (~strong_down.shift(1).fillna(False))
    signals[entry_down] = -1

    return signals


# ── Run accuracy tests ──────────────────────────────────
SIGNAL_TESTS = [
    ('RSI_25_75', lambda df: signal_rsi_extreme(df, 14, 25, 75)),
    ('RSI_20_80', lambda df: signal_rsi_extreme(df, 14, 20, 80)),
    ('BB_2.0', lambda df: signal_bb_extreme(df, 20, 2.0)),
    ('BB_2.5', lambda df: signal_bb_extreme(df, 20, 2.5)),
    ('EMA_9_21_cross', lambda df: signal_ema_cross(df, 9, 21)),
    ('EMA_12_26_cross', lambda df: signal_ema_cross(df, 12, 26)),
    ('Momentum_pullback', signal_momentum_pullback),
    ('Volume_breakout', signal_volume_breakout),
    ('MACD_divergence', signal_macd_divergence),
    ('Combined_strict', signal_combined_strict),
    ('Trend_ADX', signal_trend_follow_adx),
]

SL_TP_CONFIGS = [
    (1.0, 1.0, 24),   # 1:1, 24h
    (1.0, 1.5, 24),   # 1:1.5, 24h
    (1.5, 1.5, 48),   # 1:1, 48h
    (1.5, 2.0, 48),   # 1:1.33, 48h
    (2.0, 3.0, 48),   # 1:1.5, 48h
]

print("\n" + "="*100)
print("SIGNAL ACCURACY TESTS")
print("="*100)

best_results = []

for sl_atr, tp_atr, max_bars in SL_TP_CONFIGS:
    print(f"\n--- SL={sl_atr} ATR, TP={tp_atr} ATR, MaxBars={max_bars} ---")
    print(f"{'Signal':<25} {'Total':>6} {'TP%':>6} {'SL%':>6} {'Dir WR%':>7} {'AvgBar':>7} {'Edge':>6}")

    for name, func in SIGNAL_TESTS:
        try:
            result = test_signal_accuracy(all_data, func, sl_atr, tp_atr, max_bars, name)
            if not result or result['total_signals'] < 50:
                continue

            # Calculate expected edge: TP_rate * TP_pnl - SL_rate * SL_pnl - costs
            # TP pnl per trade (approx): tp_atr * avg_ATR_pct * notional * leverage - costs
            # For simplicity: tp_rate * tp_atr - sl_rate * sl_atr (in ATR units)
            tp_r = result['tp_rate'] / 100
            sl_r = result['sl_rate'] / 100
            timeout_r = 1 - tp_r - sl_r
            # Expected move in ATR units (TP gives + tp_atr, SL gives - sl_atr, timeout ~0)
            expected_atr = tp_r * tp_atr - sl_r * sl_atr
            # Cost in ATR units (roughly: 0.3% / avg_natr %)
            # avg natr is about 1.2%, so cost_atr ~= 0.3/1.2 = 0.25 ATR
            cost_atr = 0.25
            edge = expected_atr - cost_atr

            print(f"{name:<25} {result['total_signals']:>6} {result['tp_rate']:>5.1f}% "
                  f"{result['sl_rate']:>5.1f}% {result['directional_wr']:>6.1f}% "
                  f"{result['avg_exit_bar']:>6.1f} {edge:>+5.2f}")

            if edge > 0:
                best_results.append({
                    'signal': name, 'sl_atr': sl_atr, 'tp_atr': tp_atr,
                    'max_bars': max_bars, 'edge': edge, **result,
                })

        except Exception as e:
            print(f"{name:<25} ERROR: {e}")

# ── Show best results ───────────────────────────────────
print("\n\n" + "="*80)
print("POSITIVE EDGE SIGNALS (sorted by edge)")
print("="*80)

if best_results:
    best_results.sort(key=lambda x: -x['edge'])
    for r in best_results[:10]:
        print(f"\n  {r['signal']} (SL={r['sl_atr']}, TP={r['tp_atr']}, bars={r['max_bars']})")
        print(f"    Edge: {r['edge']:+.3f} ATR | Signals: {r['total_signals']} | TP: {r['tp_rate']}% | SL: {r['sl_rate']}%")
        if r.get('monthly'):
            print(f"    Monthly WR:")
            for month, mstats in sorted(r['monthly'].items(), key=lambda x: str(x[0])):
                print(f"      {month}: n={mstats['n']:.0f}, WR={mstats['wr']:.1f}%")
else:
    print("\n  NO signal type showed positive edge after costs.")
    print("  This confirms: standard technical indicators on 1h crypto")
    print("  do not generate alpha after realistic commission + slippage.")


# ── Raw directional test (no SL/TP, just direction after N bars) ──
print("\n\n" + "="*80)
print("RAW DIRECTIONAL TEST (future return after signal)")
print("="*80)

for name, func in SIGNAL_TESTS:
    try:
        all_returns = {1: [], 4: [], 8: [], 24: []}
        for coin, df in all_data.items():
            d = df.copy()
            sigs = func(d)
            mask = d.index >= '2025-01-01'
            sigs = sigs[mask]
            d = d[mask]

            for idx in sigs.index[sigs != 0]:
                i = d.index.get_loc(idx)
                sig = sigs[idx]
                entry = d['close'].iloc[i]

                for horizon in [1, 4, 8, 24]:
                    if i + horizon < len(d):
                        future = d['close'].iloc[i + horizon]
                        if sig == 1:
                            ret = (future - entry) / entry
                        else:
                            ret = (entry - future) / entry
                        all_returns[horizon].append(ret)

        if all_returns[1]:
            n = len(all_returns[1])
            if n < 50:
                continue
            results_str = []
            for h in [1, 4, 8, 24]:
                rets = np.array(all_returns[h])
                avg = rets.mean() * 100
                wr = (rets > 0).mean() * 100
                results_str.append(f"{h}h: avg={avg:+.3f}% WR={wr:.1f}%")
            print(f"  {name:<25} n={n:>5}  " + " | ".join(results_str))

    except Exception as e:
        print(f"  {name:<25} ERROR: {e}")


# Save
with open('/Users/ibrahimpeyman/Documents/oto-bot/artifacts/edge_search_results.json', 'w') as f:
    json.dump({'best_results': best_results, 'note': 'Edge = expected ATR move - cost ATR'},
              f, indent=2, default=str)

print("\nResults saved to artifacts/edge_search_results.json")
