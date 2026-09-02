import yfinance as yf
import pandas as pd
import numpy as np
from collections import defaultdict
from datetime import datetime, date

# Re-use our fetched data or fetch fresh
TICKERS = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS",
    "SBIN.NS", "AXISBANK.NS", "KOTAKBANK.NS", "LT.NS", "BHARTIARTL.NS",
    "BAJFINANCE.NS", "WIPRO.NS", "HCLTECH.NS", "TITAN.NS", "SUNPHARMA.NS",
]

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
COST_PER_TRADE = 50  # realistic equity intraday cost
RISK_PER_TRADE = 2000

def test_model(name, runner_fn, max_daily_trades=2):
    trades = []
    daily_pnl = defaultdict(float)
    
    for day in all_days:
        day_signals = []
        for t, df in data.items():
            today = df[df.index.date == day]
            if len(today) < 4:
                continue
            sigs = runner_fn(t, df, today, precalc[t], day)
            for s in sigs:
                day_signals.append((t, s))
        
        # Sort by quality/strength and limit daily trades
        day_signals = sorted(day_signals, key=lambda x: x[1].get('score', 0), reverse=True)[:max_daily_trades]
        
        for t, sig in day_signals:
            df = data[t]
            today = df[df.index.date == day]
            entry = sig['entry']
            stop = sig['stop']
            target = sig['target']
            direction = sig['dir']
            bar_idx = sig['bar_idx']
            
            risk = abs(entry - stop)
            if risk <= 0: continue
            qty = max(1, int(RISK_PER_TRADE / risk))
            
            exit_price = None
            result = "squareoff"
            for j in range(bar_idx + 1, len(today)):
                bar = today.iloc[j]
                if j == len(today) - 1:
                    exit_price = bar['close']
                    result = 'squareoff'
                    break
                if direction == 'LONG':
                    if bar['low'] <= stop:
                        exit_price = stop
                        result = 'stop'
                        break
                    if bar['high'] >= target:
                        exit_price = target
                        result = 'target'
                        break
                else:
                    if bar['high'] >= stop:
                        exit_price = stop
                        result = 'stop'
                        break
                    if bar['low'] <= target:
                        exit_price = target
                        result = 'target'
                        break
            
            if exit_price is None: continue
            pnl = (exit_price - entry) * qty if direction == 'LONG' else (entry - exit_price) * qty
            net_pnl = pnl - COST_PER_TRADE
            trades.append({"net_pnl": net_pnl, "result": result, "dir": direction})
            daily_pnl[day] += net_pnl
            
    if not trades:
        print(f"[{name}] No trades")
        return
        
    wins = [t for t in trades if t['net_pnl'] > 0]
    total_net = sum(t['net_pnl'] for t in trades)
    win_rate = len(wins) / len(trades) * 100
    loss_days = sum(1 for v in daily_pnl.values() if v < 0)
    profit_days = sum(1 for v in daily_pnl.values() if v > 0)
    
    print(f"=== {name} ===")
    print(f"Trades: {len(trades)} | Win Rate: {win_rate:.1f}% | Net P&L: ₹{total_net:,.0f} | Profit Days: {profit_days}/{len(all_days)} ({profit_days/len(all_days)*100:.1f}%) | Loss Days: {loss_days}")
    if wins and len(wins) < len(trades):
        gross_win = sum(t['net_pnl'] for t in wins)
        gross_loss = abs(sum(t['net_pnl'] for t in trades if t['net_pnl'] <= 0))
        pf = gross_win / gross_loss if gross_loss > 0 else 99
        print(f"Profit Factor: {pf:.2f} | Avg Win: ₹{gross_win/len(wins):,.0f} | Avg Loss: ₹{gross_loss/(len(trades)-len(wins)):,.0f}")
    print()

# Strategy 1: High Conviction 15m ORB with ADX > 25, Volume > 2x, 200 EMA alignment, 1:2.5 RR
def orb_filtered(t, df, today, ind, day):
    orb = today.iloc[0]
    orb_hi, orb_lo = orb['high'], orb['low']
    rng = orb_hi - orb_lo
    rng_pct = rng / orb['close']
    if rng_pct > 0.025 or rng_pct < 0.003: return []
    
    # Morning volume average (from 60d)
    avg_v = today['volume'].iloc[:3].mean()
    adx_val = ind['adx'].loc[today.index[0]] if today.index[0] in ind['adx'].index else 0
    if adx_val < 22: return [] # Filter choppy days
    
    trend_200 = ind['ema200'].loc[today.index[0]]
    vw = ind['vwap']
    
    sigs = []
    # Only evaluate first 4 bars (9:30 to 10:30)
    for i in range(1, min(5, len(today))):
        bar = today.iloc[i]
        prev = today.iloc[i-1]
        vol_ok = bar['volume'] >= avg_v * 1.5
        vw_val = vw.loc[today.index[i]]
        
        # Long
        if bar['close'] > orb_hi and prev['close'] <= orb_hi and vol_ok and bar['close'] > vw_val and bar['close'] > trend_200:
            stop = orb_lo
            risk = bar['close'] - stop
            target = bar['close'] + 2.5 * risk
            sigs.append({'dir': 'LONG', 'entry': bar['close'], 'stop': stop, 'target': target, 'bar_idx': i, 'score': (bar['volume']/avg_v) * adx_val})
            break
        # Short
        if bar['close'] < orb_lo and prev['close'] >= orb_lo and vol_ok and bar['close'] < vw_val and bar['close'] < trend_200:
            stop = orb_hi
            risk = stop - bar['close']
            target = bar['close'] - 2.5 * risk
            sigs.append({'dir': 'SHORT', 'entry': bar['close'], 'stop': stop, 'target': target, 'bar_idx': i, 'score': (bar['volume']/avg_v) * adx_val})
            break
    return sigs

# Strategy 2: 30-min Opening Range Breakout (Much lower noise on NSE)
def orb30_strategy(t, df, today, ind, day):
    if len(today) < 3: return []
    # 30-min range is first 2 candles of 15m
    r30_hi = max(today.iloc[0]['high'], today.iloc[1]['high'])
    r30_lo = min(today.iloc[0]['low'], today.iloc[1]['low'])
    rng = r30_hi - r30_lo
    if rng / today.iloc[1]['close'] > 0.03 or rng / today.iloc[1]['close'] < 0.003: return []
    
    adx_val = ind['adx'].loc[today.index[1]] if today.index[1] in ind['adx'].index else 0
    if adx_val < 20: return []
    
    vw = ind['vwap']
    sigs = []
    for i in range(2, min(7, len(today))):
        bar = today.iloc[i]
        prev = today.iloc[i-1]
        vw_val = vw.loc[today.index[i]]
        if bar['close'] > r30_hi and prev['close'] <= r30_hi and bar['close'] > vw_val:
            stop = r30_lo
            risk = bar['close'] - stop
            target = bar['close'] + 2.0 * risk
            sigs.append({'dir': 'LONG', 'entry': bar['close'], 'stop': stop, 'target': target, 'bar_idx': i, 'score': adx_val})
            break
        if bar['close'] < r30_lo and prev['close'] >= r30_lo and bar['close'] < vw_val:
            stop = r30_hi
            risk = stop - bar['close']
            target = bar['close'] - 2.0 * risk
            sigs.append({'dir': 'SHORT', 'entry': bar['close'], 'stop': stop, 'target': target, 'bar_idx': i, 'score': adx_val})
            break
    return sigs

# Strategy 3: Mean Reversion / VWAP Band Fade (Extreme Extension Reversal)
# In rangebound/choppy Indian markets, fading extreme extensions (RSI < 20 or > 80 from VWAP) is highly profitable
def vwap_mean_reversion(t, df, today, ind, day):
    if len(today) < 6: return []
    vw = ind['vwap']
    at = ind['atr']
    sigs = []
    
    # Run only between 10:30 AM and 1:30 PM (Mid-day mean reversion)
    for i in range(5, min(18, len(today))):
        bar = today.iloc[i]
        prev = today.iloc[i-1]
        vw_val = vw.loc[today.index[i]]
        at_val = at.loc[today.index[i]]
        if at_val == 0: continue
        
        dist = (bar['close'] - vw_val) / at_val
        
        # If stretched 2.5 ATR above VWAP and shows rejection candle -> Short to VWAP
        if dist > 2.2 and bar['close'] < prev['close'] and bar['high'] > prev['high']:
            stop = bar['high'] + 0.5 * at_val
            target = vw_val
            risk = stop - bar['close']
            if risk > 0 and (bar['close'] - target) / risk >= 1.5:
                sigs.append({'dir': 'SHORT', 'entry': bar['close'], 'stop': stop, 'target': target, 'bar_idx': i, 'score': dist})
                break
                
        # If stretched 2.2 ATR below VWAP and shows bounce -> Long to VWAP
        if dist < -2.2 and bar['close'] > prev['close'] and bar['low'] < prev['low']:
            stop = bar['low'] - 0.5 * at_val
            target = vw_val
            risk = bar['close'] - stop
            if risk > 0 and (target - bar['close']) / risk >= 1.5:
                sigs.append({'dir': 'LONG', 'entry': bar['close'], 'stop': stop, 'target': target, 'bar_idx': i, 'score': abs(dist)})
                break
    return sigs

# Strategy 4: High-Probability Momentum Pullback (EMA 20 Pullback in strong trend)
def ema_pullback(t, df, today, ind, day):
    if len(today) < 8: return []
    ema20 = ind['ema20']
    ema50 = ind['ema50']
    adx_val = ind['adx'].loc[today.index[0]] if today.index[0] in ind['adx'].index else 0
    if adx_val < 25: return [] # Only in strong trends!
    
    at = ind['atr']
    vw = ind['vwap']
    sigs = []
    
    for i in range(4, min(16, len(today))):
        bar = today.iloc[i]
        prev = today.iloc[i-1]
        e20 = ema20.loc[today.index[i]]
        e50 = ema50.loc[today.index[i]]
        at_val = at.loc[today.index[i]]
        vw_val = vw.loc[today.index[i]]
        
        # Bull trend: EMA20 > EMA50 > VWAP
        if e20 > e50 and bar['close'] > vw_val:
            # Low dipped to EMA20 and close bounced
            if bar['low'] <= e20 and bar['close'] > e20 and bar['close'] > prev['close']:
                stop = bar['low'] - 0.5 * at_val
                risk = bar['close'] - stop
                if risk > 0:
                    target = bar['close'] + 2.0 * risk
                    sigs.append({'dir': 'LONG', 'entry': bar['close'], 'stop': stop, 'target': target, 'bar_idx': i, 'score': adx_val})
                    break
                    
        # Bear trend: EMA20 < EMA50 < VWAP
        if e20 < e50 and bar['close'] < vw_val:
            if bar['high'] >= e20 and bar['close'] < e20 and bar['close'] < prev['close']:
                stop = bar['high'] + 0.5 * at_val
                risk = stop - bar['close']
                if risk > 0:
                    target = bar['close'] - 2.0 * risk
                    sigs.append({'dir': 'SHORT', 'entry': bar['close'], 'stop': stop, 'target': target, 'bar_idx': i, 'score': adx_val})
                    break
    return sigs

test_model("1. ORB Filtered (15m + ADX>22 + Vol>1.5x + 200 EMA)", orb_filtered, max_daily_trades=1)
test_model("2. 30-min ORB Breakout (Reduced Noise)", orb30_strategy, max_daily_trades=1)
test_model("3. VWAP Mean Reversion (Extreme Extension Fading)", vwap_mean_reversion, max_daily_trades=2)
test_model("4. Strong Trend EMA Pullback (ADX>25 + Bounce)", ema_pullback, max_daily_trades=1)
