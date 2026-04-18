"""
PHASE 2: Fix the Scalper V2 strategy and validate on both 2025 and 2026

Key problems diagnosed:
1. TP too far (2.58 ATR) vs SL too tight (0.87 ATR) → WR ~27% doesn't support 3:1 R:R
2. Min confluence score too low (0.40) → many marginal signals enter and lose
3. No volatility-adaptive exits → same SL/TP in all conditions
4. Strategy trades too much in unfavorable conditions

Fixes to implement:
A. Better SL/TP ratio: wider SL, more realistic TP (target ~1.5:1 R:R, need ~45% WR)
B. Higher confluence threshold → fewer but higher quality trades
C. Volatility regime filter: skip trades in very low vol (noise kills you)
D. Stronger trend filter: don't mean-revert in trends
E. Time-based exit: if trade goes nowhere in N bars, cut it
F. Adaptive leverage: lower leverage when conditions are marginal
"""
import json, sys, time, warnings, copy
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from itertools import product

warnings.filterwarnings('ignore')
sys.path.insert(0, '/Users/ibrahimpeyman/Documents/oto-bot/src')

from oto_bot.data.crypto import CryptoDataProvider
from oto_bot.strategies.base import StrategyContext
from oto_bot.strategies.scalper_v2 import ScalperV2Strategy

# ── Fetch data (reuse from diagnosis) ──────────────────
COINS = ['BTC/USDT','ETH/USDT','SOL/USDT','ADA/USDT','DOGE/USDT','ALGO/USDT',
         'OP/USDT','LTC/USDT','FET/USDT','RENDER/USDT','SUI/USDT','LINK/USDT',
         'INJ/USDT','PEPE/USDT','WLD/USDT','AAVE/USDT','NEAR/USDT',
         'BNB/USDT','XRP/USDT','AVAX/USDT','DOT/USDT','UNI/USDT','APT/USDT','ARB/USDT','ATOM/USDT']

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
    df.index.name = None
    return df

print("Fetching data...")
all_data = {}
for coin in COINS:
    try:
        df = fetch_coin(coin, "2024-11-01T00:00:00")  # Extra warmup
        all_data[coin] = df
        print(f"  {coin}: {len(df)} bars")
    except Exception as e:
        print(f"  {coin}: FAILED - {e}")
    time.sleep(0.15)

# ── Enhanced backtest with time-based exit ──────────────
def realistic_backtest(all_signals, start_date, end_date, label="",
                       max_hold_bars=48, trailing_stop=False,
                       breakeven_after=None):
    """
    Enhanced realistic portfolio backtest.
    New features:
    - max_hold_bars: force exit after N bars (time stop)
    - breakeven_after: move SL to entry after N bars if in profit
    """
    trades = []

    for coin, df in all_signals.items():
        mask = (df.index >= start_date) & (df.index <= end_date)
        period_df = df[mask].copy()
        if len(period_df) == 0:
            continue

        i = 0
        while i < len(period_df):
            row = period_df.iloc[i]
            if row['signal'] == 0 or pd.isna(row.get('stop_loss')) or pd.isna(row.get('take_profit')):
                i += 1
                continue

            signal = int(row['signal'])
            entry_price = row['close']
            sl = row['stop_loss']
            tp = row['take_profit']
            leverage = row.get('leverage', 1.0)
            prob = row.get('probability', 0.55)
            score = row['score_long'] if signal == 1 else row['score_short']
            adx_val = row.get('adx', 0)

            # Entry slippage
            entry_slip = entry_price * SLIPPAGE_RATE
            if signal == 1:
                actual_entry = entry_price + entry_slip
            else:
                actual_entry = entry_price - entry_slip

            # Walk forward
            exit_price = None
            exit_reason = None
            exit_time = None
            current_sl = sl

            for j in range(i+1, min(i + max_hold_bars + 1, len(period_df))):
                bar = period_df.iloc[j]
                bars_held = j - i

                # Breakeven stop: after N bars, if in profit, move SL to entry
                if breakeven_after and bars_held >= breakeven_after:
                    if signal == 1 and bar['close'] > actual_entry:
                        current_sl = max(current_sl, actual_entry)
                    elif signal == -1 and bar['close'] < actual_entry:
                        current_sl = min(current_sl, actual_entry)

                if signal == 1:
                    if bar['low'] <= current_sl:
                        exit_price = current_sl - current_sl * SLIPPAGE_RATE
                        exit_reason = 'SL'
                        exit_time = bar.name
                        break
                    if bar['high'] >= tp:
                        exit_price = tp - tp * SLIPPAGE_RATE
                        exit_reason = 'TP'
                        exit_time = bar.name
                        break
                else:
                    if bar['high'] >= current_sl:
                        exit_price = current_sl + current_sl * SLIPPAGE_RATE
                        exit_reason = 'SL'
                        exit_time = bar.name
                        break
                    if bar['low'] <= tp:
                        exit_price = tp + tp * SLIPPAGE_RATE
                        exit_reason = 'TP'
                        exit_time = bar.name
                        break

            if exit_price is None:
                # Time stop: exit at close
                last_idx = min(i + max_hold_bars, len(period_df) - 1)
                last_bar = period_df.iloc[last_idx]
                exit_price = last_bar['close']
                if signal == 1:
                    exit_price -= exit_price * SLIPPAGE_RATE
                else:
                    exit_price += exit_price * SLIPPAGE_RATE
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
                'probability': prob, 'score': score, 'adx': adx_val,
            })

            if exit_time is not None:
                exit_idx = period_df.index.get_loc(exit_time)
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
        day_loss = daily_loss.get(day_key, 0)
        if day_loss >= DAILY_LOSS_CAP:
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
        losses=('adjusted_pnl', lambda x: (x <= 0).sum()),
    ).reset_index()
    monthly['wr'] = (monthly['wins'] / monthly['trades'] * 100).round(1)
    monthly['cum_pnl'] = monthly['pnl'].cumsum()

    total_pnl = result_df['adjusted_pnl'].sum()
    total_trades = len(result_df)
    wins = (result_df['adjusted_pnl'] > 0).sum()

    stats = {
        'label': label,
        'total_pnl': round(total_pnl, 2),
        'total_trades': total_trades,
        'win_rate': round(wins / total_trades * 100, 1) if total_trades > 0 else 0,
        'avg_trade': round(total_pnl / total_trades, 2) if total_trades > 0 else 0,
        'roi_pct': round(total_pnl / INITIAL_CAPITAL * 100, 2),
    }

    return result_df, {'stats': stats, 'monthly': monthly.to_dict('records')}


def print_results(label, results):
    if not results:
        print(f"\n{label}: No results")
        return
    s = results['stats']
    print(f"\n  {label}:")
    print(f"    PnL: ${s['total_pnl']:,.2f} | Trades: {s['total_trades']} | WR: {s['win_rate']}% | ROI: {s['roi_pct']}%")
    for m in results['monthly']:
        print(f"      {m['month']}: {m['trades']}t, ${m['pnl']:.2f}, WR={m['wr']}%")


# ── Generate signals with different param sets ──────────
strategy = ScalperV2Strategy()

# Current params
CURRENT_PARAMS = json.load(open('/Users/ibrahimpeyman/Documents/oto-bot/artifacts/v5_regime_fix.json'))['params']

# Parameter grid to test
# Key insight: we need to FIX the R:R ratio
# Current: SL=0.87 ATR, TP=2.58 ATR → need ~75% TP hit rate, getting ~25%
# Fix: wider SL (1.5-2.0 ATR), lower TP (1.5-2.0 ATR) → need ~50% WR

PARAM_VARIANTS = {
    'baseline': CURRENT_PARAMS,
    'fix_a': {**CURRENT_PARAMS,
        'atr_sl_multiplier': 1.5,   # wider SL
        'atr_tp_multiplier': 1.8,   # lower TP → better WR
        'min_confluence_score': 0.48, # higher quality filter
    },
    'fix_b': {**CURRENT_PARAMS,
        'atr_sl_multiplier': 1.8,
        'atr_tp_multiplier': 2.0,
        'min_confluence_score': 0.50,
        'adx_hard_max': 25,          # tighter trend filter
        'regime_penalty': 0.3,
    },
    'fix_c': {**CURRENT_PARAMS,
        'atr_sl_multiplier': 1.5,
        'atr_tp_multiplier': 1.5,    # 1:1 R:R → need only ~55% WR
        'min_confluence_score': 0.52, # high quality only
        'adx_hard_max': 25,
        'regime_penalty': 0.3,
        'adx_trend_penalty': 0.3,
    },
    'fix_d': {**CURRENT_PARAMS,
        'atr_sl_multiplier': 2.0,
        'atr_tp_multiplier': 2.5,    # wider both
        'min_confluence_score': 0.50,
        'adx_hard_max': 25,
        'regime_penalty': 0.3,
    },
    'fix_e': {**CURRENT_PARAMS,
        'atr_sl_multiplier': 1.3,
        'atr_tp_multiplier': 1.5,
        'min_confluence_score': 0.55, # very selective
        'adx_hard_max': 25,
        'regime_penalty': 0.2,
        'adx_trend_penalty': 0.2,
    },
    'fix_f': {**CURRENT_PARAMS,
        'atr_sl_multiplier': 1.5,
        'atr_tp_multiplier': 2.0,
        'min_confluence_score': 0.45,
        'adx_hard_max': 28,
        'regime_penalty': 0.35,
        'regime_slope_threshold': 0.02,
        'regime_lookback': 72,  # 3 days lookback for regime
    },
}

# ── Run all variants ────────────────────────────────────
all_results = {}

for variant_name, params in PARAM_VARIANTS.items():
    print(f"\n{'='*60}")
    print(f"Testing: {variant_name}")
    print(f"  SL={params['atr_sl_multiplier']}, TP={params['atr_tp_multiplier']}, "
          f"min_score={params['min_confluence_score']}, adx_hard={params.get('adx_hard_max', 30)}")
    print(f"{'='*60}")

    # Generate signals
    variant_signals = {}
    for coin, df in all_data.items():
        ctx = StrategyContext(
            market="crypto", strategy_family="scalper",
            symbol=coin, timeframe="1h", params=params
        )
        try:
            variant_signals[coin] = strategy.generate_signals(df, ctx)
        except:
            pass

    # Backtest 2025
    _, r2025 = realistic_backtest(
        variant_signals, '2025-01-01', '2025-12-31',
        label=f'{variant_name}_2025',
        max_hold_bars=48, breakeven_after=12
    )
    print_results('2025', r2025)

    # Backtest 2026
    _, r2026 = realistic_backtest(
        variant_signals, '2026-01-01', '2026-04-12',
        label=f'{variant_name}_2026',
        max_hold_bars=48, breakeven_after=12
    )
    print_results('2026 Q1', r2026)

    all_results[variant_name] = {
        '2025': r2025,
        '2026': r2026,
    }

# ── Summary comparison ──────────────────────────────────
print("\n\n" + "="*80)
print("COMPARISON SUMMARY")
print("="*80)
print(f"{'Variant':<15} {'2025 PnL':>10} {'2025 WR':>8} {'2026 PnL':>10} {'2026 WR':>8} {'2025 ROI':>9} {'2026 ROI':>9}")
print("-"*80)
for name, res in all_results.items():
    r25 = res.get('2025', {}).get('stats', {})
    r26 = res.get('2026', {}).get('stats', {})
    print(f"{name:<15} ${r25.get('total_pnl',0):>9,.2f} {r25.get('win_rate',0):>7.1f}% "
          f"${r26.get('total_pnl',0):>9,.2f} {r26.get('win_rate',0):>7.1f}% "
          f"{r25.get('roi_pct',0):>8.1f}% {r26.get('roi_pct',0):>8.1f}%")

# ── Save results ────────────────────────────────────────
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

with open('/Users/ibrahimpeyman/Documents/oto-bot/artifacts/fix_comparison.json', 'w') as f:
    json.dump(make_serializable(all_results), f, indent=2, default=str)

print("\nResults saved to artifacts/fix_comparison.json")
