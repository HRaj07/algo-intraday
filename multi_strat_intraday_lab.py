import yfinance as yf
import pandas as pd
import numpy as np
from collections import defaultdict

TICKERS = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS",
    "SBIN.NS", "AXISBANK.NS", "KOTAKBANK.NS", "LT.NS", "BHARTIARTL.NS",
    "BAJFINANCE.NS", "WIPRO.NS", "HCLTECH.NS", "TITAN.NS", "SUNPHARMA.NS",
    "MARUTI.NS", "ADANIENT.NS", "NTPC.NS", "POWERGRID.NS", "TATASTEEL.NS"
]

print("📥 Fetching 60-day 15m high-resolution intraday dataset + 1-year daily dataset...")

data_15m = {}
for t in TICKERS:
    try:
        df = yf.download(t, period="60d", interval="15m", auto_adjust=True, progress=False, multi_level_index=False)
        if not df.empty:
            df.columns = [c.lower() for c in df.columns]
            if df.index.tz is not None: df.index = df.index.tz_localize(None)
            data_15m[t] = df.dropna()
    except Exception: pass

data_d = {}
for t in TICKERS:
    try:
        df = yf.download(t, period="1y", interval="1d", auto_adjust=True, progress=False, multi_level_index=False)
        if not df.empty:
            df.columns = [c.lower() for c in df.columns]
            if df.index.tz is not None: df.index = df.index.tz_localize(None)
            data_d[t] = df.dropna()
    except Exception: pass

# Fetch benchmark daily data
bench_d = yf.download("^NSEI", period="1y", interval="1d", auto_adjust=True, progress=False, multi_level_index=False)
bench_d.columns = [c.lower() for c in bench_d.columns]
if bench_d.index.tz is not None: bench_d.index = bench_d.index.tz_localize(None)

# 15m benchmark data
bench_15m = yf.download("^NSEI", period="60d", interval="15m", auto_adjust=True, progress=False, multi_level_index=False)
bench_15m.columns = [c.lower() for c in bench_15m.columns]
if bench_15m.index.tz is not None: bench_15m.index = bench_15m.index.tz_localize(None)

def vwap_15m(df):
    df = df.copy()
    df["date"] = df.index.date
    df["tp"] = (df["high"] + df["low"] + df["close"]) / 3
    df["tpv"] = df["tp"] * df["volume"]
    return df.groupby("date")["tpv"].cumsum() / df.groupby("date")["volume"].cumsum()

def ema_calc(series, span):
    return series.ewm(span=span, adjust=False).mean()

def atr_calc(df, period=14):
    high, low, close = df['high'], df['low'], df['close']
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean().fillna(0)

# Precalculate indicators
precalc = {}
for t, df in data_15m.items():
    df_d = data_d.get(t, pd.DataFrame())
    precalc[t] = {
        "vwap": vwap_15m(df),
        "ema9": ema_calc(df['close'], 9),
        "ema21": ema_calc(df['close'], 21),
        "atr": atr_calc(df, 14),
        "daily_df": df_d,
    }

all_days = sorted(set(list(data_15m.values())[0].index.date))
COST_PER_TRADE = 50
RISK_PER_TRADE = 2000
CAPITAL = 500000

print(f"✅ Loaded {len(data_15m)} stocks across {len(all_days)} trading days.")

# -------------------------------------------------------------
# STRATEGY 1: CPR Breakout + Relative Strength (RS vs Nifty)
# -------------------------------------------------------------
def test_cpr_rs(max_trades=2, rr=2.0, trail_step=1.0):
    trades = []
    daily_pnl = defaultdict(float)

    for day in all_days:
        # Calculate Nifty RS baseline for today
        b_today = bench_15m[bench_15m.index.date == day]
        if len(b_today) < 2: continue
        bench_ret_open = (b_today.iloc[1]['close'] - b_today.iloc[0]['open']) / b_today.iloc[0]['open']
        
        candidates = []
        for t, df in data_15m.items():
            today = df[df.index.date == day]
            if len(today) < 4: continue
            
            df_d = precalc[t]['daily_df']
            past_d = df_d.loc[:pd.Timestamp(day)]
            if len(past_d) < 3: continue
            
            # Yesterday's OHLC for CPR
            y_high = past_d.iloc[-2]['high']
            y_low = past_d.iloc[-2]['low']
            y_close = past_d.iloc[-2]['close']
            
            # Pivot, Top Central (TC), Bottom Central (BC)
            pivot = (y_high + y_low + y_close) / 3
            bc = (y_high + y_low) / 2
            tc = (pivot - bc) + pivot
            cpr_top = max(tc, bc)
            cpr_bottom = min(tc, bc)
            cpr_width = (cpr_top - cpr_bottom) / pivot
            
            # Narrow CPR filter (trending day candidate: width < 0.35%)
            if cpr_width > 0.0045: continue
            
            # Stock return vs Nifty return (Relative Strength)
            stock_ret_open = (today.iloc[1]['close'] - today.iloc[0]['open']) / today.iloc[0]['open']
            rs_score = stock_ret_open - bench_ret_open
            
            vw = precalc[t]['vwap']
            
            # Scan bars from 9:45 (bar 2) to 11:30 (bar 8)
            for i in range(2, min(9, len(today))):
                bar = today.iloc[i]
                prev = today.iloc[i-1]
                vw_val = vw.loc[today.index[i]]
                
                # Bullish Breakout: Price crosses above CPR Top & VWAP with positive RS
                if bar['close'] > cpr_top and prev['close'] <= cpr_top and bar['close'] > vw_val and rs_score > 0.002:
                    stop = min(cpr_bottom, bar['low'] * 0.997)
                    risk = bar['close'] - stop
                    if risk > 0 and risk / bar['close'] < 0.015:
                        target = bar['close'] + rr * risk
                        candidates.append({
                            'ticker': t, 'dir': 'LONG', 'entry': bar['close'], 'stop': stop,
                            'target': target, 'bar_idx': i, 'score': rs_score, 'risk': risk
                        })
                    break
                    
                # Bearish Breakdown: Price crosses below CPR Bottom & VWAP with negative RS
                if bar['close'] < cpr_bottom and prev['close'] >= cpr_bottom and bar['close'] < vw_val and rs_score < -0.002:
                    stop = max(cpr_top, bar['high'] * 1.003)
                    risk = stop - bar['close']
                    if risk > 0 and risk / bar['close'] < 0.015:
                        target = bar['close'] - rr * risk
                        candidates.append({
                            'ticker': t, 'dir': 'SHORT', 'entry': bar['close'], 'stop': stop,
                            'target': target, 'bar_idx': i, 'score': abs(rs_score), 'risk': risk
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
                    
                # Trailing logic: at +1R move to breakeven + 0.15R
                if direction == 'LONG' and bar['high'] >= entry + trail_step * risk:
                    curr_stop = max(curr_stop, entry + 0.15 * risk)
                elif direction == 'SHORT' and bar['low'] <= entry - trail_step * risk:
                    curr_stop = min(curr_stop, entry - 0.15 * risk)
                    
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
    gross_win = sum(t['net_pnl'] for t in wins)
    gross_loss = abs(sum(t['net_pnl'] for t in trades if t['net_pnl'] <= 0))
    pf = gross_win / gross_loss if gross_loss > 0 else 99
    print(f"\n[Strategy A: Narrow CPR Breakout + Relative Strength (RS)]")
    print(f"  Trades: {len(trades)} | Win Rate: {win_rate:.1f}% | Net P&L: ₹{total_net:+,.0f} | PF: {pf:.2f}")

# -------------------------------------------------------------
# STRATEGY 2: VWAP Pullback + EMA 9/21 Trend Ribbon
# -------------------------------------------------------------
def test_vwap_pullback(max_trades=2, rr=2.0):
    trades = []
    daily_pnl = defaultdict(float)

    for day in all_days:
        candidates = []
        for t, df in data_15m.items():
            today = df[df.index.date == day]
            if len(today) < 5: continue
            
            vw = precalc[t]['vwap']
            e9 = precalc[t]['ema9']
            e21 = precalc[t]['ema21']
            
            # Scan between 10:00 AM (bar 3) and 1:30 PM (bar 16)
            for i in range(3, min(16, len(today))):
                bar = today.iloc[i]
                prev = today.iloc[i-1]
                vw_val = vw.loc[today.index[i]]
                e9_val = e9.loc[today.index[i]]
                e21_val = e21.loc[today.index[i]]
                
                # Trend Alignment: EMA9 > EMA21 and both above VWAP
                is_uptrend = e9_val > e21_val and bar['close'] > vw_val
                is_downtrend = e9_val < e21_val and bar['close'] < vw_val
                
                # Bullish Pullback to VWAP / EMA21: low touches near VWAP/EMA21 and closes strong green
                if is_uptrend and prev['low'] <= vw_val * 1.002 and bar['close'] > bar['open'] and bar['close'] > e9_val:
                    stop = min(bar['low'], vw_val * 0.997)
                    risk = bar['close'] - stop
                    if risk > 0 and risk / bar['close'] < 0.015:
                        target = bar['close'] + rr * risk
                        score = (bar['close'] - bar['open']) / bar['close']
                        candidates.append({
                            'ticker': t, 'dir': 'LONG', 'entry': bar['close'], 'stop': stop,
                            'target': target, 'bar_idx': i, 'score': score, 'risk': risk
                        })
                    break
                    
                # Bearish Pullback to VWAP / EMA21: high touches near VWAP/EMA21 and closes strong red
                if is_downtrend and prev['high'] >= vw_val * 0.998 and bar['close'] < bar['open'] and bar['close'] < e9_val:
                    stop = max(bar['high'], vw_val * 1.003)
                    risk = stop - bar['close']
                    if risk > 0 and risk / bar['close'] < 0.015:
                        target = bar['close'] - rr * risk
                        score = (bar['open'] - bar['close']) / bar['close']
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
                    
                # Trailing logic: at +1R move to breakeven + 0.15R
                if direction == 'LONG' and bar['high'] >= entry + risk:
                    curr_stop = max(curr_stop, entry + 0.15 * risk)
                elif direction == 'SHORT' and bar['low'] <= entry - risk:
                    curr_stop = min(curr_stop, entry - 0.15 * risk)
                    
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
    gross_win = sum(t['net_pnl'] for t in wins)
    gross_loss = abs(sum(t['net_pnl'] for t in trades if t['net_pnl'] <= 0))
    pf = gross_win / gross_loss if gross_loss > 0 else 99
    print(f"\n[Strategy B: VWAP Pullback + EMA Trend Ribbon]")
    print(f"  Trades: {len(trades)} | Win Rate: {win_rate:.1f}% | Net P&L: ₹{total_net:+,.0f} | PF: {pf:.2f}")

test_cpr_rs()
test_vwap_pullback()
