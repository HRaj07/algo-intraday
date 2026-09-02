import yfinance as yf
import pandas as pd
import numpy as np
from collections import defaultdict

TICKERS = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS",
    "SBIN.NS", "AXISBANK.NS", "KOTAKBANK.NS", "LT.NS", "BHARTIARTL.NS",
    "BAJFINANCE.NS", "WIPRO.NS", "HCLTECH.NS", "TITAN.NS", "SUNPHARMA.NS",
    "MARUTI.NS", "ADANIENT.NS", "NTPC.NS", "POWERGRID.NS", "TATASTEEL.NS",
    "M&M.NS", "ASIANPAINT.NS", "ULTRACEMCO.NS", "DIVISLAB.NS"
]

print("📥 Fetching 60d 15m dataset...")
data_15m = {}
for t in TICKERS:
    try:
        df = yf.download(t, period="60d", interval="15m", auto_adjust=True, progress=False, multi_level_index=False)
        if not df.empty and len(df) > 50:
            df.columns = [c.lower() for c in df.columns]
            if df.index.tz is not None: df.index = df.index.tz_localize(None)
            data_15m[t] = df.dropna()
    except Exception: pass

# Benchmark
bench_15m = yf.download("^NSEI", period="60d", interval="15m", auto_adjust=True, progress=False, multi_level_index=False)
bench_15m.columns = [c.lower() for c in bench_15m.columns]
if bench_15m.index.tz is not None: bench_15m.index = bench_15m.index.tz_localize(None)

def vwap_15m(df):
    df = df.copy()
    df["date"] = df.index.date
    df["tp"] = (df["high"] + df["low"] + df["close"]) / 3
    df["tpv"] = df["tp"] * df["volume"]
    return df.groupby("date")["tpv"].cumsum() / df.groupby("date")["volume"].cumsum()

def adx_calc(df, period=14):
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

precalc = {}
for t, df in data_15m.items():
    precalc[t] = {
        "vwap": vwap_15m(df),
        "adx": adx_calc(df),
    }

all_days = sorted(set(list(data_15m.values())[0].index.date))
COST_PER_TRADE = 50
RISK_PER_TRADE = 2000

print(f"Loaded {len(data_15m)} stocks across {len(all_days)} trading days.\n")

def run_test(name, orb_bars=2, max_trades=1, min_adx=20, vol_filter=True, rr=2.0, trail_threshold=1.0, trail_buffer=0.2):
    trades = []
    daily_pnl = defaultdict(float)

    for day in all_days:
        b_today = bench_15m[bench_15m.index.date == day]
        bench_ret = 0
        if len(b_today) >= 2:
            bench_ret = (b_today.iloc[1]['close'] - b_today.iloc[0]['open']) / b_today.iloc[0]['open']

        candidates = []
        for t, df in data_15m.items():
            today = df[df.index.date == day]
            if len(today) < orb_bars + 3: continue
            
            # ORB defined over orb_bars (e.g. 2 bars = 30 min, 3 bars = 45 min)
            orb_slice = today.iloc[:orb_bars]
            r_hi = orb_slice['high'].max()
            r_lo = orb_slice['low'].min()
            rng = r_hi - r_lo
            c_open = orb_slice.iloc[0]['open']
            
            if rng / c_open > 0.025 or rng / c_open < 0.003: continue
            
            # Relative strength
            stock_ret = (orb_slice.iloc[-1]['close'] - c_open) / c_open
            rs_score = stock_ret - bench_ret
            
            # Volume surge filter
            avg_vol = df['volume'].rolling(20).mean().loc[today.index[0]] if today.index[0] in df.index else 1
            vol_ratio = orb_slice['volume'].sum() / (avg_vol * orb_bars + 1)
            if vol_filter and vol_ratio < 1.1: continue
            
            adx_val = precalc[t]['adx'].loc[today.index[0]] if today.index[0] in precalc[t]['adx'].index else 0
            if adx_val < min_adx: continue
            
            vw = precalc[t]['vwap']
            
            # Look for breakout in the next 4 bars after opening range
            for i in range(orb_bars, min(orb_bars + 6, len(today))):
                bar = today.iloc[i]
                prev = today.iloc[i-1]
                vw_val = vw.loc[today.index[i]]
                
                # LONG: Break above ORB High & above VWAP & Relative Strength positive
                if bar['close'] > r_hi and prev['close'] <= r_hi and bar['close'] > vw_val and rs_score > 0:
                    stop = r_lo
                    risk = bar['close'] - stop
                    if risk > 0 and risk / bar['close'] < 0.018:
                        target = bar['close'] + rr * risk
                        score = adx_val * 100 + rs_score * 10000 + vol_ratio * 10
                        candidates.append({
                            'ticker': t, 'dir': 'LONG', 'entry': bar['close'], 'stop': stop,
                            'target': target, 'bar_idx': i, 'score': score, 'risk': risk
                        })
                    break
                    
                # SHORT: Break below ORB Low & below VWAP & Relative Strength negative
                if bar['close'] < r_lo and prev['close'] >= r_lo and bar['close'] < vw_val and rs_score < 0:
                    stop = r_hi
                    risk = stop - bar['close']
                    if risk > 0 and risk / bar['close'] < 0.018:
                        target = bar['close'] - rr * risk
                        score = adx_val * 100 + abs(rs_score) * 10000 + vol_ratio * 10
                        candidates.append({
                            'ticker': t, 'dir': 'SHORT', 'entry': bar['close'], 'stop': stop,
                            'target': target, 'bar_idx': i, 'score': score, 'risk': risk
                        })
                    break

        selected = sorted(candidates, key=lambda x: x['score'], reverse=True)[:max_trades]
        
        for sig in selected:
            t = sig['ticker']
            df = data_15m[t]
            today = df[df.index.date == day]
            entry, stop, target, direction, bar_idx, risk = sig['entry'], sig['stop'], sig['target'], sig['dir'], sig['bar_idx'], sig['risk']
            qty = max(1, int(RISK_PER_TRADE / risk))
            curr_stop = stop
            exit_price = None
            result = 'squareoff'
            
            for j in range(bar_idx + 1, len(today)):
                bar = today.iloc[j]
                if j == len(today) - 1:
                    exit_price = bar['close']
                    result = 'squareoff'
                    break
                    
                # Trailing Stop: once in profit >= trail_threshold * risk, move stop to entry + trail_buffer * risk
                if direction == 'LONG' and bar['high'] >= entry + trail_threshold * risk:
                    curr_stop = max(curr_stop, entry + trail_buffer * risk)
                elif direction == 'SHORT' and bar['low'] <= entry - trail_threshold * risk:
                    curr_stop = min(curr_stop, entry - trail_buffer * risk)
                    
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
            trades.append({"net_pnl": net_pnl, "result": result, "ticker": t, "date": str(day), "dir": direction})
            daily_pnl[day] += net_pnl

    wins = [t for t in trades if t['net_pnl'] > 0]
    total_net = sum(t['net_pnl'] for t in trades)
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    profit_days = sum(1 for v in daily_pnl.values() if v > 0)
    loss_days = sum(1 for v in daily_pnl.values() if v < 0)
    gross_win = sum(t['net_pnl'] for t in wins)
    gross_loss = abs(sum(t['net_pnl'] for t in trades if t['net_pnl'] <= 0))
    pf = gross_win / gross_loss if gross_loss > 0 else 99

    print(f"[{name}]")
    print(f"  Trades: {len(trades)} | Win Rate: {win_rate:.1f}% | Net P&L: ₹{total_net:+,.0f} | Profit Factor: {pf:.2f}")
    print(f"  Profitable Days: {profit_days}/{len(all_days)} | Loss Days: {loss_days}")
    print(f"  Avg Win: ₹{gross_win/len(wins) if wins else 0:,.0f} | Avg Loss: ₹{gross_loss/(len(trades)-len(wins)) if len(trades)>len(wins) else 0:,.0f}\n")

run_test("1. Baseline 30m ORB (Max 2 trades, ADX>18)", orb_bars=2, max_trades=2, min_adx=18, vol_filter=False, rr=2.0)
run_test("2. Institutional 30m ORB + Volume Surge + RS Score (Max 1 trade/day)", orb_bars=2, max_trades=1, min_adx=20, vol_filter=True, rr=2.0, trail_threshold=0.9, trail_buffer=0.2)
run_test("3. Institutional 30m ORB + Volume Surge + RS Score (Max 2 trades/day)", orb_bars=2, max_trades=2, min_adx=20, vol_filter=True, rr=2.0, trail_threshold=0.9, trail_buffer=0.2)
run_test("4. 45m ORB (3 Bars) + Volume Surge + RS Score (Max 1 trade/day)", orb_bars=3, max_trades=1, min_adx=20, vol_filter=True, rr=2.0, trail_threshold=0.9, trail_buffer=0.2)
