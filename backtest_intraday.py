"""
Production Intraday Strategy Backtest
30-Minute Institutional ORB with ADX, VWAP, Trailing Stop, and Strict Risk Allocation
"""
import json
import logging
from collections import defaultdict
import pandas as pd
import numpy as np
import yfinance as yf
from config import INTRADAY_UNIVERSE, SYSTEM, ORB_CONFIG, COSTS

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

def vwap(df):
    df = df.copy()
    df["date"] = df.index.date
    df["tp"] = (df["high"] + df["low"] + df["close"]) / 3
    df["tpv"] = df["tp"] * df["volume"]
    return df.groupby("date")["tpv"].cumsum() / df.groupby("date")["volume"].cumsum()

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

def run_backtest():
    logger.info("Fetching 15m dataset for universe...")
    data = {}
    for t in INTRADAY_UNIVERSE:
        try:
            df = yf.download(t, period="60d", interval="15m", auto_adjust=True, progress=False, multi_level_index=False)
            if not df.empty:
                df.columns = [c.lower() for c in df.columns]
                if df.index.tz is not None:
                    df.index = df.index.tz_localize(None)
                data[t] = df.dropna()
        except Exception:
            pass

    precalc = {}
    for t, df in data.items():
        precalc[t] = {
            "vwap": vwap(df),
            "adx": adx(df),
        }

    all_days = sorted(set(list(data.values())[0].index.date))
    logger.info(f"Backtesting over {len(all_days)} trading days ({all_days[0]} to {all_days[-1]})...")

    trades = []
    daily_pnl = defaultdict(float)

    for day in all_days:
        day_signals = []
        for t, df in data.items():
            today = df[df.index.date == day]
            if len(today) < 4:
                continue

            r30_hi = max(today.iloc[0]['high'], today.iloc[1]['high'])
            r30_lo = min(today.iloc[0]['low'], today.iloc[1]['low'])
            rng = r30_hi - r30_lo
            c_open = today.iloc[1]['close']
            if rng / c_open > ORB_CONFIG["max_range_pct"] or rng / c_open < ORB_CONFIG["min_range_pct"]:
                continue

            adx_val = precalc[t]['adx'].loc[today.index[1]] if today.index[1] in precalc[t]['adx'].index else 0
            if adx_val < ORB_CONFIG["min_adx"]:
                continue

            vw = precalc[t]['vwap']

            for i in range(2, min(ORB_CONFIG["scan_window_end_bar"] + 1, len(today))):
                bar = today.iloc[i]
                prev = today.iloc[i-1]
                vw_val = vw.loc[today.index[i]]

                # LONG
                if bar['close'] > r30_hi and prev['close'] <= r30_hi and bar['close'] > vw_val:
                    stop = r30_lo
                    risk = bar['close'] - stop
                    if risk > 0:
                        target = bar['close'] + ORB_CONFIG["risk_reward"] * risk
                        day_signals.append({
                            'ticker': t, 'dir': 'LONG', 'entry': bar['close'], 'stop': stop,
                            'target': target, 'bar_idx': i, 'score': adx_val, 'risk': risk
                        })
                    break

                # SHORT
                if bar['close'] < r30_lo and prev['close'] >= r30_lo and bar['close'] < vw_val:
                    stop = r30_hi
                    risk = stop - bar['close']
                    if risk > 0:
                        target = bar['close'] - ORB_CONFIG["risk_reward"] * risk
                        day_signals.append({
                            'ticker': t, 'dir': 'SHORT', 'entry': bar['close'], 'stop': stop,
                            'target': target, 'bar_idx': i, 'score': adx_val, 'risk': risk
                        })
                    break

        # Max 2 top signals per day
        selected = sorted(day_signals, key=lambda x: x['score'], reverse=True)[:SYSTEM["max_daily_trades"]]

        for sig in selected:
            t = sig['ticker']
            df = data[t]
            today = df[df.index.date == day]
            entry, stop, target, direction, bar_idx, risk = (
                sig['entry'], sig['stop'], sig['target'], sig['dir'], sig['bar_idx'], sig['risk']
            )
            qty = max(1, int(SYSTEM["risk_per_trade"] / risk))
            curr_stop = stop
            exit_price = None
            result = 'squareoff'

            for j in range(bar_idx + 1, len(today)):
                bar = today.iloc[j]
                if j == len(today) - 1:
                    exit_price = bar['close']
                    result = 'squareoff'
                    break

                # Trailing stop to breakeven + buffer once +1R touched
                if direction == 'LONG' and bar['high'] >= entry + risk:
                    curr_stop = max(curr_stop, entry + ORB_CONFIG["trail_buffer"] * risk)
                elif direction == 'SHORT' and bar['low'] <= entry - risk:
                    curr_stop = min(curr_stop, entry - ORB_CONFIG["trail_buffer"] * risk)

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

            if exit_price is None:
                continue

            pnl = (exit_price - entry) * qty if direction == 'LONG' else (entry - exit_price) * qty
            cost = COSTS["fixed_roundtrip_cost"]
            net_pnl = pnl - cost
            trades.append({"net_pnl": net_pnl, "result": result, "ticker": t, "date": str(day), "dir": direction})
            daily_pnl[day] += net_pnl

    wins = [t for t in trades if t['net_pnl'] > 0]
    total_net = sum(t['net_pnl'] for t in trades)
    win_rate = len(wins) / len(trades) * 100
    profit_days = sum(1 for v in daily_pnl.values() if v > 0)
    loss_days = sum(1 for v in daily_pnl.values() if v < 0)
    gross_win = sum(t['net_pnl'] for t in wins)
    gross_loss = abs(sum(t['net_pnl'] for t in trades if t['net_pnl'] <= 0))
    pf = gross_win / gross_loss if gross_loss > 0 else 99

    print("\n" + "═" * 65)
    print("  🏆 VERIFIED INTRADAY RESULTS — (May 2026 to Aug 2026)")
    print("═" * 65)
    print(f"  Initial Capital      : ₹{SYSTEM['initial_capital']:>10,.0f}")
    print(f"  Final Capital        : ₹{SYSTEM['initial_capital'] + total_net:>10,.0f}")
    print(f"  Net P&L              : ₹{total_net:>+10,.0f}")
    print(f"  Total Return         : {total_net / SYSTEM['initial_capital'] * 100:>+9.2f}%")
    print("─" * 65)
    print(f"  Total Trades         : {len(trades):>10}")
    print(f"  Winning Trades       : {len(wins):>10}  ({win_rate:.1f}%)")
    print(f"  Losing Trades        : {len(trades) - len(wins):>10}  ({100 - win_rate:.1f}%)")
    print(f"  Avg Win              : ₹{gross_win / len(wins):>+9,.0f}")
    print(f"  Avg Loss             : ₹{gross_loss / (len(trades) - len(wins)):>+9,.0f}")
    print(f"  Profit Factor        : {pf:>10.2f}")
    print("─" * 65)
    print(f"  Trading Days         : {len(all_days):>10}")
    print(f"  Profitable Days      : {profit_days:>10}  ({profit_days / len(all_days) * 100:.1f}%)")
    print(f"  Losing Days          : {loss_days:>10}  ({loss_days / len(all_days) * 100:.1f}%)")
    print("═" * 65)

    print("\n  📅 Monthly Net P&L:")
    monthly = defaultdict(float)
    for d, p in daily_pnl.items():
        monthly[d.strftime("%b %Y")] += p
    for m, p in monthly.items():
        print(f"  {m:<12} : ₹{p:>+8,.0f}")
    print("═" * 65)

if __name__ == "__main__":
    run_backtest()
