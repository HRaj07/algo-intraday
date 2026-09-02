import yfinance as yf
import pandas as pd
import numpy as np
from collections import defaultdict
from datetime import datetime, date

TICKERS = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS",
    "SBIN.NS", "AXISBANK.NS", "KOTAKBANK.NS", "LT.NS", "BHARTIARTL.NS",
    "BAJFINANCE.NS", "WIPRO.NS", "HCLTECH.NS", "TITAN.NS", "SUNPHARMA.NS",
    "MARUTI.NS", "ADANIENT.NS", "NTPC.NS", "POWERGRID.NS"
]

print("Downloading dataset...")
data = {}
for t in TICKERS:
    try:
        df = yf.download(t, period="60d", interval="15m", auto_adjust=True, progress=False, multi_level_index=False)
        if not df.empty:
            df.columns = [c.lower() for c in df.columns]
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)
            data[t] = df.dropna()
    except Exception as e:
        pass

def vwap(df):
    df = df.copy()
    df["date"] = df.index.date
    df["tp"] = (df["high"] + df["low"] + df["close"]) / 3
    df["tpv"] = df["tp"] * df["volume"]
    return df.groupby("date")["tpv"].cumsum() / df.groupby("date")["volume"].cumsum()

def atr(df, period=14):
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"]-df["low"], (df["high"]-pc).abs(), (df["low"]-pc).abs()], axis=1).max(axis=1)
    return tr.ewm(com=period-1, adjust=False).mean()

def adx(df, period=14):
    high, low, close = df['high'], df['low'], df['close']
    prev_close = close.shift(1)
    plus_dm = high.diff().clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)
    plus_dm[high.diff() <= -low.diff()] = 0
    minus_dm[-low.diff() <= high.diff()] = 0
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    a = tr.ewm(com=period - 1, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(com=period - 1, adjust=False).mean() / a.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(com=period - 1, adjust=False).mean() / a.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(com=period - 1, adjust=False).mean().fillna(0)

# Precalculate indicators
precalc = {}
for t, df in data.items():
    precalc[t] = {
        "vwap": vwap(df),
        "atr": atr(df),
        "adx": adx(df),
        "ema20": df['close'].ewm(span=20, adjust=False).mean(),
        "ema50": df['close'].ewm(span=50, adjust=False).mean(),
        "ema200": df['close'].ewm(span=200, adjust=False).mean(),
    }

all_days = sorted(set(list(data.values())[0].index.date))
COST_PER_TRADE = 50
RISK_PER_TRADE = 2000

# Strategy A: 30-min Institutional ORB
# Logic: First 30 min (two 15m candles) establishes high/low. Breakout between 9:45 and 11:15 AM.
# Requires: ADX >= 18, VWAP alignment, Vol >= 1.3x 5-bar avg, 1:2.0 R:R with Breakeven Trailing Stop at +1R
def get_orb30_signals(day):
    sigs = []
    for t, df in data.items():
        today = df[df.index.date == day]
        if len(today) < 4: continue
        
        r30_hi = max(today.iloc[0]['high'], today.iloc[1]['high'])
        r30_lo = min(today.iloc[0]['low'], today.iloc[1]['low'])
        rng = r30_hi - r30_lo
        c_open = today.iloc[1]['close']
        if rng / c_open > 0.028 or rng / c_open < 0.003: continue
        
        adx_val = precalc[t]['adx'].loc[today.index[1]] if today.index[1] in precalc[t]['adx'].index else 0
        if adx_val < 18: continue
        
        vw = precalc[t]['vwap']
        avg_v = today['volume'].iloc[:3].mean()
        
        for i in range(2, min(9, len(today))):
            bar = today.iloc[i]
            prev = today.iloc[i-1]
            vw_val = vw.loc[today.index[i]]
            vol_ok = bar['volume'] >= avg_v * 1.3
            
            # LONG
            if bar['close'] > r30_hi and prev['close'] <= r30_hi and bar['close'] > vw_val and vol_ok:
                stop = r30_lo
                risk = bar['close'] - stop
                if risk > 0:
                    target = bar['close'] + 2.0 * risk
                    score = adx_val * (bar['volume'] / avg_v if avg_v > 0 else 1)
                    sigs.append({
                        'ticker': t, 'dir': 'LONG', 'entry': bar['close'], 'stop': stop,
                        'target': target, 'bar_idx': i, 'score': score, 'risk': risk, 'strat': 'ORB30'
                    })
                break
                
            # SHORT
            if bar['close'] < r30_lo and prev['close'] >= r30_lo and bar['close'] < vw_val and vol_ok:
                stop = r30_hi
                risk = stop - bar['close']
                if risk > 0:
                    target = bar['close'] - 2.0 * risk
                    score = adx_val * (bar['volume'] / avg_v if avg_v > 0 else 1)
                    sigs.append({
                        'ticker': t, 'dir': 'SHORT', 'entry': bar['close'], 'stop': stop,
                        'target': target, 'bar_idx': i, 'score': score, 'risk': risk, 'strat': 'ORB30'
                    })
                break
    return sigs

# Strategy B: VWAP Momentum Bounce / Retest
# Logic: Price in trend (ADX > 22, EMA20 > EMA50) retests VWAP between 10:15 AM and 1:30 PM, then bounces
def get_vwap_retest_signals(day):
    sigs = []
    for t, df in data.items():
        today = df[df.index.date == day]
        if len(today) < 8: continue
        
        adx_val = precalc[t]['adx'].loc[today.index[0]] if today.index[0] in precalc[t]['adx'].index else 0
        if adx_val < 22: continue
        
        vw = precalc[t]['vwap']
        at = precalc[t]['atr']
        e20 = precalc[t]['ema20']
        e50 = precalc[t]['ema50']
        
        for i in range(4, min(16, len(today))):
            bar = today.iloc[i]
            prev = today.iloc[i-1]
            vw_val = vw.loc[today.index[i]]
            at_val = at.loc[today.index[i]]
            e20_v = e20.loc[today.index[i]]
            e50_v = e50.loc[today.index[i]]
            
            # LONG Bounce: Trend UP, touches VWAP within 0.15%, closes green above VWAP
            if e20_v > e50_v and bar['low'] <= vw_val * 1.0015 and bar['close'] > vw_val and bar['close'] > prev['close']:
                stop = min(bar['low'], vw_val) - 0.5 * at_val
                risk = bar['close'] - stop
                if risk > 0:
                    target = bar['close'] + 2.0 * risk
                    sigs.append({
                        'ticker': t, 'dir': 'LONG', 'entry': bar['close'], 'stop': stop,
                        'target': target, 'bar_idx': i, 'score': adx_val, 'risk': risk, 'strat': 'VWAP_Bounce'
                    })
                break
                
            # SHORT Rejection: Trend DOWN, touches VWAP within 0.15%, closes red below VWAP
            if e20_v < e50_v and bar['high'] >= vw_val * 0.9985 and bar['close'] < vw_val and bar['close'] < prev['close']:
                stop = max(bar['high'], vw_val) + 0.5 * at_val
                risk = stop - bar['close']
                if risk > 0:
                    target = bar['close'] - 2.0 * risk
                    sigs.append({
                        'ticker': t, 'dir': 'SHORT', 'entry': bar['close'], 'stop': stop,
                        'target': target, 'bar_idx': i, 'score': adx_val, 'risk': risk, 'strat': 'VWAP_Bounce'
                    })
                break
    return sigs

# Run Combined System Simulation
def run_master_system(max_daily_trades=2):
    trades = []
    daily_pnl = defaultdict(float)
    strat_perf = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0})
    
    for day in all_days:
        sigs = []
        sigs += get_orb30_signals(day)
        sigs += get_vwap_retest_signals(day)
        
        # Max 1 signal per stock
        seen_tickers = set()
        unique_sigs = []
        for s in sorted(sigs, key=lambda x: x['score'], reverse=True):
            if s['ticker'] not in seen_tickers:
                seen_tickers.add(s['ticker'])
                unique_sigs.append(s)
                
        # Take top N signals for the day
        day_signals = unique_sigs[:max_daily_trades]
        
        for sig in day_signals:
            t = sig['ticker']
            df = data[t]
            today = df[df.index.date == day]
            entry = sig['entry']
            stop = sig['stop']
            target = sig['target']
            direction = sig['dir']
            bar_idx = sig['bar_idx']
            risk = sig['risk']
            qty = max(1, int(RISK_PER_TRADE / risk))
            
            curr_stop = stop
            exit_price = None
            result = "squareoff"
            
            for j in range(bar_idx + 1, len(today)):
                bar = today.iloc[j]
                if j == len(today) - 1:
                    exit_price = bar['close']
                    result = 'squareoff'
                    break
                
                # Breakeven trailing stop at +1R
                if direction == 'LONG' and bar['high'] >= entry + risk:
                    curr_stop = max(curr_stop, entry + 0.1 * risk)
                elif direction == 'SHORT' and bar['low'] <= entry - risk:
                    curr_stop = min(curr_stop, entry - 0.1 * risk)
                
                if direction == 'LONG':
                    if bar['low'] <= curr_stop:
                        exit_price = curr_stop
                        result = 'stop'
                        break
                    if bar['high'] >= target:
                        exit_price = target
                        result = 'target'
                        break
                else:
                    if bar['high'] >= curr_stop:
                        exit_price = curr_stop
                        result = 'stop'
                        break
                    if bar['low'] <= target:
                        exit_price = target
                        result = 'target'
                        break
                        
            if exit_price is None: continue
            pnl = (exit_price - entry) * qty if direction == 'LONG' else (entry - exit_price) * qty
            net_pnl = pnl - COST_PER_TRADE
            trades.append({"net_pnl": net_pnl, "result": result, "strat": sig['strat'], "ticker": t, "date": str(day)})
            daily_pnl[day] += net_pnl
            
            strat_perf[sig['strat']]["trades"] += 1
            strat_perf[sig['strat']]["pnl"] += net_pnl
            if net_pnl > 0:
                strat_perf[sig['strat']]["wins"] += 1

    wins = [t for t in trades if t['net_pnl'] > 0]
    total_net = sum(t['net_pnl'] for t in trades)
    win_rate = len(wins) / len(trades) * 100
    profit_days = sum(1 for v in daily_pnl.values() if v > 0)
    loss_days = sum(1 for v in daily_pnl.values() if v < 0)
    flat_days = len(all_days) - profit_days - loss_days
    gross_win = sum(t['net_pnl'] for t in wins)
    gross_loss = abs(sum(t['net_pnl'] for t in trades if t['net_pnl'] <= 0))
    pf = gross_win / gross_loss if gross_loss > 0 else 99
    
    print("\n" + "═"*65)
    print(" 🚀 REFINED INTRADAY SYSTEM RESULTS — 60 Days (May–Aug 2026)")
    print("═"*65)
    print(f" Initial Capital      : ₹500,000")
    print(f" Net P&L              : ₹{total_net:+,.0f}")
    print(f" Return               : {total_net/500000*100:+.2f}%")
    print(f" Total Trades         : {len(trades)} (Avg {len(trades)/len(all_days):.1f} per day)")
    print(f" Win Rate             : {win_rate:.1f}%")
    print(f" Profit Factor        : {pf:.2f}")
    print(f" Profitable Days      : {profit_days}/{len(all_days)} ({profit_days/len(all_days)*100:.1f}%)")
    print(f" Losing Days          : {loss_days}/{len(all_days)} ({loss_days/len(all_days)*100:.1f}%)")
    print(f" Flat/No-trade Days   : {flat_days}/{len(all_days)} ({flat_days/len(all_days)*100:.1f}%)")
    print("─"*65)
    print(" Strategy Breakdown:")
    for s, st in strat_perf.items():
        wr = st['wins'] / st['trades'] * 100 if st['trades'] else 0
        print(f"  {s:<15} | Trades: {st['trades']:>3} | Win Rate: {wr:>5.1f}% | Net P&L: ₹{st['pnl']:>+8,.0f}")
    print("═"*65)

run_master_system(max_daily_trades=2)
