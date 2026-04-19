"""
PHASE 1: Diagnose why Scalper V2.1d fails in 2026
- Fetch 1h data for all coins, 2025 full year + 2026 Q1
- Run strategy signals
- Analyze: regime, volatility, signal quality, long vs short PnL
- Portfolio-level realistic backtest with commission, slippage, margin
"""
import json, sys, time, warnings
import numpy as np
import pandas as pd
from datetime import datetime, timezone

warnings.filterwarnings('ignore')
sys.path.insert(0, 'C:/Users/koray/projeler/oto-bot/src')

from oto_bot.data.crypto import CryptoDataProvider
from oto_bot.strategies.base import StrategyContext
from oto_bot.strategies.scalper_v2 import ScalperV2Strategy

# ── Config ──────────────────────────────────────────────
COINS = ['BTC/USDT','ETH/USDT','SOL/USDT','ADA/USDT','DOGE/USDT','ALGO/USDT',
         'OP/USDT','LTC/USDT','FET/USDT','RENDER/USDT','SUI/USDT','LINK/USDT',
         'INJ/USDT','PEPE/USDT','WLD/USDT','AAVE/USDT','NEAR/USDT',
         'BNB/USDT','XRP/USDT','AVAX/USDT','DOT/USDT','UNI/USDT','APT/USDT','ARB/USDT','ATOM/USDT']

PARAMS = json.load(open('C:/Users/koray/projeler/oto-bot/artifacts/v5_regime_fix.json'))['params']

# Realistic costs
COMMISSION_RATE = 0.001  # 0.1% per side
SLIPPAGE_RATE = 0.0005   # 0.05% per side
NOTIONAL = 600.0
MAX_POSITIONS = 6
DAILY_LOSS_CAP = 300.0
INITIAL_CAPITAL = 10000.0

# ── Fetch data ──────────────────────────────────────────
provider = CryptoDataProvider()

def fetch_coin(symbol, since_str, limit=1500):
    """Fetch OHLCV, handling ccxt's 1000-bar limit with pagination."""
    import ccxt
    exchange = provider._get_exchange()
    dt = datetime.fromisoformat(since_str).replace(tzinfo=timezone.utc)
    since_ms = int(dt.timestamp() * 1000)

    all_data = []
    while True:
        raw = exchange.fetch_ohlcv(symbol, '1h', since=since_ms, limit=1000)
        if not raw:
            break
        all_data.extend(raw)
        since_ms = raw[-1][0] + 3600000  # next hour
        if len(raw) < 1000:
            break
        time.sleep(0.1)

    df = pd.DataFrame(all_data, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    df.index.name = None
    return df

print("Fetching data for all coins...")
all_data = {}
for coin in COINS:
    try:
        df = fetch_coin(coin, "2024-12-01T00:00:00")  # Start a bit early for warmup
        all_data[coin] = df
        print(f"  {coin}: {len(df)} bars, {df.index[0].date()} to {df.index[-1].date()}")
    except Exception as e:
        print(f"  {coin}: FAILED - {e}")
    time.sleep(0.15)

# ── Generate signals for all coins ──────────────────────
strategy = ScalperV2Strategy()

def get_signals(symbol, df):
    ctx = StrategyContext(
        market="crypto", strategy_family="scalper",
        symbol=symbol, timeframe="1h", params=PARAMS
    )
    return strategy.generate_signals(df, ctx)

print("\nGenerating signals...")
all_signals = {}
for coin, df in all_data.items():
    try:
        sig_df = get_signals(coin, df)
        all_signals[coin] = sig_df
    except Exception as e:
        print(f"  {coin}: signal generation failed - {e}")

# ── Realistic Portfolio Backtest ────────────────────────
def realistic_backtest(all_signals, start_date, end_date, label=""):
    """
    Realistic portfolio backtest with:
    - $600 notional per trade, leverage-adjusted margin
    - Commission + slippage on entry AND exit
    - Max 6 simultaneous positions
    - 3 consecutive losses -> halve position
    - 5 consecutive losses -> skip 10 bars
    - Daily loss cap $300
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

            # Entry slippage (worse entry)
            entry_slip = entry_price * SLIPPAGE_RATE
            if signal == 1:
                actual_entry = entry_price + entry_slip
            else:
                actual_entry = entry_price - entry_slip

            # Simulate exit: walk forward bar by bar
            exit_price = None
            exit_reason = None
            exit_time = None

            for j in range(i+1, min(i+100, len(period_df))):  # max 100 bars hold
                bar = period_df.iloc[j]

                if signal == 1:  # LONG
                    # Check SL hit (low touches SL)
                    if bar['low'] <= sl:
                        exit_price = sl - sl * SLIPPAGE_RATE  # SL slippage (worse)
                        exit_reason = 'SL'
                        exit_time = bar.name
                        break
                    # Check TP hit (high touches TP)
                    if bar['high'] >= tp:
                        exit_price = tp - tp * SLIPPAGE_RATE  # TP slippage (worse)
                        exit_reason = 'TP'
                        exit_time = bar.name
                        break
                else:  # SHORT
                    # Check SL hit (high touches SL)
                    if bar['high'] >= sl:
                        exit_price = sl + sl * SLIPPAGE_RATE
                        exit_reason = 'SL'
                        exit_time = bar.name
                        break
                    # Check TP hit (low touches TP)
                    if bar['low'] <= tp:
                        exit_price = tp + tp * SLIPPAGE_RATE
                        exit_reason = 'TP'
                        exit_time = bar.name
                        break

            if exit_price is None:
                # Timeout: exit at last bar's close with slippage
                last_bar = period_df.iloc[min(i+99, len(period_df)-1)]
                exit_price = last_bar['close']
                if signal == 1:
                    exit_price -= exit_price * SLIPPAGE_RATE
                else:
                    exit_price += exit_price * SLIPPAGE_RATE
                exit_reason = 'TIMEOUT'
                exit_time = last_bar.name

            # PnL calculation
            if signal == 1:
                pnl_pct = (exit_price - actual_entry) / actual_entry
            else:
                pnl_pct = (actual_entry - exit_price) / actual_entry

            # Commission on both sides
            commission = NOTIONAL * COMMISSION_RATE * 2

            # Dollar PnL
            dollar_pnl = NOTIONAL * leverage * pnl_pct - commission

            trades.append({
                'coin': coin,
                'entry_time': row.name,
                'exit_time': exit_time,
                'signal': signal,
                'entry_price': actual_entry,
                'exit_price': exit_price,
                'leverage': leverage,
                'pnl_pct': pnl_pct,
                'dollar_pnl': dollar_pnl,
                'commission': commission,
                'exit_reason': exit_reason,
                'probability': prob,
                'score': score,
                'adx': adx_val,
            })

            # Skip to after exit
            if exit_time is not None:
                exit_idx = period_df.index.get_loc(exit_time)
                i = exit_idx + 1
            else:
                i += 1
            continue

    if not trades:
        return pd.DataFrame(), {}

    trades_df = pd.DataFrame(trades)

    # ── Portfolio-level constraints ──────────────────────
    # Sort by entry time, then apply constraints
    trades_df = trades_df.sort_values('entry_time').reset_index(drop=True)

    # Apply consecutive loss halving and skip logic
    final_trades = []
    consec_losses = 0
    skip_until = None
    daily_loss = {}

    open_positions = []  # list of (exit_time, coin)

    for _, trade in trades_df.iterrows():
        entry_t = trade['entry_time']

        # Clean expired positions
        open_positions = [(et, c) for et, c in open_positions if et > entry_t]

        # Max positions check
        if len(open_positions) >= MAX_POSITIONS:
            continue

        # Skip logic (5 consecutive losses)
        if skip_until is not None and entry_t < skip_until:
            continue
        skip_until = None

        # Daily loss cap
        day_key = entry_t.date() if hasattr(entry_t, 'date') else str(entry_t)[:10]
        day_loss = daily_loss.get(day_key, 0)
        if day_loss >= DAILY_LOSS_CAP:
            continue

        # Position halving on 3 consecutive losses
        size_mult = 0.5 if consec_losses >= 3 else 1.0
        adjusted_pnl = trade['dollar_pnl'] * size_mult

        # Update trackers
        if adjusted_pnl < 0:
            consec_losses += 1
            if consec_losses >= 5:
                # Skip next 10 bars (10 hours)
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

    # Monthly breakdown
    result_df['month'] = pd.to_datetime(result_df['entry_time']).dt.to_period('M')
    monthly = result_df.groupby('month').agg(
        trades=('adjusted_pnl', 'count'),
        pnl=('adjusted_pnl', 'sum'),
        wins=('adjusted_pnl', lambda x: (x > 0).sum()),
        losses=('adjusted_pnl', lambda x: (x <= 0).sum()),
        avg_win=('adjusted_pnl', lambda x: x[x > 0].mean() if (x > 0).any() else 0),
        avg_loss=('adjusted_pnl', lambda x: x[x <= 0].mean() if (x <= 0).any() else 0),
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

# ── Run backtests ───────────────────────────────────────
print("\n" + "="*70)
print("BACKTEST: 2025 Full Year")
print("="*70)
trades_2025, results_2025 = realistic_backtest(
    all_signals, '2025-01-01', '2025-12-31', label='2025'
)

print("\n" + "="*70)
print("BACKTEST: 2026 Q1 (Jan-Apr 12)")
print("="*70)
trades_2026, results_2026 = realistic_backtest(
    all_signals, '2026-01-01', '2026-04-12', label='2026_Q1'
)

# Print results
for label, results in [('2025', results_2025), ('2026 Q1', results_2026)]:
    if not results:
        print(f"\n{label}: No results")
        continue
    s = results['stats']
    print(f"\n{label} Summary:")
    print(f"  Total PnL: ${s['total_pnl']:,.2f}")
    print(f"  Total Trades: {s['total_trades']}")
    print(f"  Win Rate: {s['win_rate']}%")
    print(f"  Avg Trade: ${s['avg_trade']:.2f}")
    print(f"  ROI: {s['roi_pct']}%")
    print(f"\n  Monthly Breakdown:")
    for m in results['monthly']:
        print(f"    {m['month']}: {m['trades']} trades, PnL=${m['pnl']:.2f}, WR={m['wr']}%, Cum=${m['cum_pnl']:.2f}")

# ── Diagnose 2026 issues ───────────────────────────────
print("\n" + "="*70)
print("DIAGNOSIS: 2026 Signal Analysis")
print("="*70)

if len(trades_2026) > 0:
    t26 = trades_2026.copy()

    # Long vs Short breakdown
    longs = t26[t26['signal'] == 1]
    shorts = t26[t26['signal'] == -1]
    print(f"\n  LONG trades: {len(longs)}, PnL=${longs['adjusted_pnl'].sum():.2f}, WR={(longs['adjusted_pnl']>0).mean()*100:.1f}%")
    print(f"  SHORT trades: {len(shorts)}, PnL=${shorts['adjusted_pnl'].sum():.2f}, WR={(shorts['adjusted_pnl']>0).mean()*100:.1f}%")

    # Exit reason breakdown
    print(f"\n  Exit reasons:")
    for reason in ['SL', 'TP', 'TIMEOUT']:
        subset = t26[t26['exit_reason'] == reason]
        print(f"    {reason}: {len(subset)} trades, PnL=${subset['adjusted_pnl'].sum():.2f}")

    # ADX distribution
    print(f"\n  ADX at entry (mean): {t26['adx'].mean():.1f}")
    print(f"  ADX at entry (median): {t26['adx'].median():.1f}")

    # Probability/Score distribution
    print(f"\n  Avg score: {t26['score'].mean():.3f}")
    print(f"  Avg probability: {t26['probability'].mean():.3f}")

    # Per-coin breakdown for 2026
    print(f"\n  Per-coin PnL (2026):")
    coin_pnl = t26.groupby('coin')['adjusted_pnl'].agg(['sum', 'count']).sort_values('sum')
    for coin, row in coin_pnl.iterrows():
        print(f"    {coin}: ${row['sum']:.2f} ({int(row['count'])} trades)")

    # Monthly direction analysis
    print(f"\n  Monthly Long vs Short:")
    t26['month'] = pd.to_datetime(t26['entry_time']).dt.to_period('M')
    for month in sorted(t26['month'].unique()):
        m_data = t26[t26['month'] == month]
        m_longs = m_data[m_data['signal'] == 1]
        m_shorts = m_data[m_data['signal'] == -1]
        print(f"    {month}:")
        print(f"      LONG:  {len(m_longs)} trades, PnL=${m_longs['adjusted_pnl'].sum():.2f}, WR={(m_longs['adjusted_pnl']>0).mean()*100:.1f}%")
        print(f"      SHORT: {len(m_shorts)} trades, PnL=${m_shorts['adjusted_pnl'].sum():.2f}, WR={(m_shorts['adjusted_pnl']>0).mean()*100:.1f}%")

# ── Market regime analysis ──────────────────────────────
print("\n" + "="*70)
print("DIAGNOSIS: Market Regime Comparison")
print("="*70)

for period_label, start, end in [
    ('2025 H1', '2025-01-01', '2025-06-30'),
    ('2025 H2', '2025-07-01', '2025-12-31'),
    ('2026 Q1', '2026-01-01', '2026-04-12'),
]:
    print(f"\n  {period_label}:")
    returns = []
    adx_vals = []
    vol_vals = []
    for coin, df in all_signals.items():
        mask = (df.index >= start) & (df.index <= end)
        pdf = df[mask]
        if len(pdf) < 10:
            continue
        ret = (pdf['close'].iloc[-1] / pdf['close'].iloc[0] - 1) * 100
        returns.append(ret)
        if 'adx' in pdf.columns:
            adx_vals.extend(pdf['adx'].dropna().values)
        if 'atr' in pdf.columns:
            # Normalized ATR (ATR/close)
            norm_atr = (pdf['atr'] / pdf['close']).dropna()
            vol_vals.extend(norm_atr.values)

    print(f"    Avg coin return: {np.mean(returns):.1f}%")
    print(f"    Median coin return: {np.median(returns):.1f}%")
    print(f"    % coins positive: {sum(1 for r in returns if r > 0)/len(returns)*100:.0f}%")
    if adx_vals:
        print(f"    Avg ADX: {np.mean(adx_vals):.1f}")
        print(f"    % time ADX > 20: {sum(1 for a in adx_vals if a > 20)/len(adx_vals)*100:.0f}%")
        print(f"    % time ADX > 30: {sum(1 for a in adx_vals if a > 30)/len(adx_vals)*100:.0f}%")
    if vol_vals:
        print(f"    Avg normalized ATR: {np.mean(vol_vals)*100:.3f}%")

# ── Save diagnostic data ───────────────────────────────
diag = {
    'results_2025': results_2025,
    'results_2026': results_2026,
}

# Convert Period objects to strings for JSON serialization
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

with open('C:/Users/koray/projeler/oto-bot/artifacts/diagnosis_2026.json', 'w') as f:
    json.dump(make_serializable(diag), f, indent=2, default=str)

print("\n\nDiagnostic data saved to artifacts/diagnosis_2026.json")
