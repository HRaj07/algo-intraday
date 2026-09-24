"""
The one test that can still change the answer: the same momentum, over days.

    python3 study_swing_daily.py              # fetches ~5y of daily bars once
    python3 study_swing_daily.py --refetch     # throw the cache away

WHY THIS TEST AND NOT ANOTHER TUNING PASS
-----------------------------------------
The intraday work reached a clean, unhappy conclusion:

  * VWAP mean reversion is negative on 185,847 real 15-minute bars, before
    costs. Deeper oversold is worse, not better. It is not fixable.
  * Momentum - long, stretched above VWAP, high RSI, heavy volume - IS real.
    Several independent cuts, t above 5, positive in all three months.
  * Run as an account, that same momentum made +1.28% in 59 days at t=+0.20,
    and a 100-cell sweep over every threshold found ZERO settings that were
    positive in all three months while also surviving the removal of their
    best five trades. Best t in the whole sweep was +1.63 - the maximum of
    100 tries, which is worth nothing.

The reason is arithmetic, not strategy. Per trade the edge was about 0.17% of
notional and friction about 0.147%. Six-sevenths of the edge went to STT,
brokerage and slippage. No threshold changes that ratio, because the tax is
charged per trade and the move is what it is.

There is exactly one lever that does change it: hold longer. Friction is paid
once per trade whatever the holding period, so a signal that captures a 3% move
over six days pays the same toll as one capturing 0.2% over four hours. That is
a tenfold change in the only ratio that matters - and it is the one thing the
intraday data could not test, because a 60-day window cannot measure a six-day
hold with any power.

THE COST MODEL IS DIFFERENT HERE AND IT IS WORSE PER TRADE
----------------------------------------------------------
Overnight positions are CNC delivery, not MIS. That means no 5x leverage, and
STT of 0.1% on BOTH sides instead of 0.025% on the sell alone. Round trip is
roughly 0.25% against intraday's 0.0955% - two and a half times more per trade.
The bet is that the move is ten times bigger. If it is not, this fails too, and
that is a real possibility this file is designed to detect rather than hide.

WHAT MAKES THIS TEST WORTH TRUSTING WHEN THE OTHERS WERE NOT
------------------------------------------------------------
Daily bars go back years, so for the first time in this whole investigation
there is a genuine holdout. The rule is chosen on the TRAIN half and scored
once on the TEST half, which the selection never saw. Read the TEST block only.
A strategy that looks wonderful in train and ordinary in test is the thing this
analysis has caught itself doing twice already.

This file changes nothing about the live bot. It fetches data and prints a
table.
"""
import argparse
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from config import INTRADAY_UNIVERSE, INDEX_TICKER, SYSTEM, RISK, SECTOR

CACHE = Path("cache_daily_5y.pkl")

# ----------------------------------------------------------------- costs
# CNC delivery, discount broker, per side unless noted.
STT_BUY = 0.001          # 0.1% - intraday charges this on the sell only
STT_SELL = 0.001
EXCHANGE = 0.0000297
SEBI = 0.000001
STAMP_BUY = 0.00015
GST_ON = 0.18            # on brokerage + exchange + sebi
BROKERAGE = 0.0          # delivery is free at most discount brokers
SLIPPAGE = 0.0005        # per side; daily-bar entries are not precise


def roundtrip_pct() -> float:
    per_side = EXCHANGE + SEBI + BROKERAGE
    gst = per_side * GST_ON
    return (STT_BUY + STT_SELL + STAMP_BUY
            + 2 * (per_side + gst) + 2 * SLIPPAGE)


COST = roundtrip_pct()


# ----------------------------------------------------------------- data
def fetch(refetch=False):
    if CACHE.exists() and not refetch:
        print(f"using {CACHE} (--refetch to replace)")
        return pickle.load(open(CACHE, "rb"))
    try:
        import yfinance as yf
    except ImportError:
        sys.exit("yfinance is not installed here. pip install yfinance")
    syms = INTRADAY_UNIVERSE + [INDEX_TICKER]
    print(f"fetching 5y of daily bars for {len(syms)} symbols "
          f"(a few minutes, once)...")
    out = {}
    for i in range(0, len(syms), 40):
        chunk = syms[i:i + 40]
        raw = yf.download(chunk, period="5y", interval="1d", group_by="ticker",
                          auto_adjust=False, progress=False, threads=True)
        for s in chunk:
            try:
                df = raw[s].dropna() if len(chunk) > 1 else raw.dropna()
            except Exception:
                continue
            if len(df) < 300:
                continue
            df.columns = [c.lower() for c in df.columns]
            out[s] = df[["open", "high", "low", "close", "volume"]]
        print(f"  {min(i+40, len(syms))}/{len(syms)}", flush=True)
    pickle.dump(out, open(CACHE, "wb"))
    print(f"cached {len(out)} symbols")
    return out


def features(data):
    """One row per (name, day) with everything a rule may look at, plus the
    forward path. Every feature uses only data available at that day's close."""
    rows = []
    for tkr, df in data.items():
        if tkr == INDEX_TICKER or len(df) < 300:
            continue
        df = df.sort_index()
        c, h, l, v = df.close, df.high, df.low, df.volume
        ret = c.pct_change()

        sma20, sma50, sma200 = c.rolling(20).mean(), c.rolling(50).mean(), c.rolling(200).mean()
        hi20 = h.rolling(20).max().shift(1)
        atr = (pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()],
                         axis=1).max(axis=1).ewm(com=13, adjust=False).mean())
        delta = c.diff()
        rsi = 100 - 100 / (1 + delta.clip(lower=0).ewm(com=13, adjust=False).mean()
                           / delta.clip(upper=0).abs().ewm(com=13, adjust=False).mean()
                           .replace(0, 1e-9))
        rvol = v / v.rolling(50).median()
        turnover = (c * v).rolling(50).median()

        # entry is the NEXT day's open - the first price an order can get
        entry = df.open.shift(-1)

        # forward path for holds of 3, 5 and 10 sessions, from that entry
        fwd, mae = {}, {}
        for n in (3, 5, 10):
            fwd[n] = c.shift(-(n + 1)) / entry - 1.0
            mae[n] = l.shift(-1).rolling(n).min().shift(-(n - 1)) / entry - 1.0

        rows.append(pd.DataFrame({
            "ticker": tkr, "sector": SECTOR.get(tkr, "OTHER"), "date": df.index,
            "close": c, "entry": entry, "atr_pct": atr / c, "rsi": rsi,
            "rvol": rvol, "turnover": turnover,
            "above_sma20": (c > sma20).astype(int),
            "above_sma50": (c > sma50).astype(int),
            "above_sma200": (c > sma200).astype(int),
            "breakout20": (c > hi20).astype(int),
            "mom20": c / c.shift(20) - 1.0,
            "mom60": c / c.shift(60) - 1.0,
            "vol20": ret.rolling(20).std(),
            "f3": fwd[3], "f5": fwd[5], "f10": fwd[10],
            "mae3": mae[3], "mae5": mae[5], "mae10": mae[10],
        }).dropna(subset=["entry", "rsi", "rvol", "turnover"]))
    return pd.concat(rows, ignore_index=True)


def stat(x):
    x = np.asarray(pd.Series(x).dropna(), dtype=float)
    if len(x) < 30:
        return None
    sd = x.std(ddof=1)
    return dict(n=len(x), mean=x.mean(), net=x.mean() - COST,
                win=(x > 0).mean(), t=(x.mean() / (sd / np.sqrt(len(x)))) if sd else 0)


def show(label, x):
    s = stat(x)
    if not s:
        print(f"  {label:<38}{'(too few)':>12}")
        return
    print(f"  {label:<38}{s['n']:>8,}  {s['mean']*100:>+7.2f}%  "
          f"{s['net']*100:>+7.2f}%  {s['win']*100:>5.1f}%  {s['t']:>+6.2f}")


HEAD = f"  {'':<38}{'n':>8}  {'gross':>8}  {'net':>8}  {'win%':>5}  {'t':>6}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refetch", action="store_true")
    a = ap.parse_args()

    tab = features(fetch(a.refetch))
    tab = tab[tab.turnover >= 5e7]          # Rs5cr/day median - tradeable size
    dates = np.sort(tab.date.unique())
    split = dates[int(len(dates) * 0.6)]
    train, test = tab[tab.date < split], tab[tab.date >= split]

    print(f"\n{len(tab):,} name-days, {tab.ticker.nunique()} names, "
          f"{pd.Timestamp(dates[0]).date()} to {pd.Timestamp(dates[-1]).date()}")
    print(f"delivery round-trip cost {COST*100:.3f}% "
          f"(intraday was 0.0955% - this is 2.6x worse per trade)")
    print(f"TRAIN {pd.Timestamp(dates[0]).date()} .. {pd.Timestamp(split).date()}   "
          f"({len(train):,} rows)")
    print(f"TEST  {pd.Timestamp(split).date()} .. {pd.Timestamp(dates[-1]).date()}   "
          f"({len(test):,} rows)   <- the only block that counts\n")

    rules = {
        "baseline (every name-day)":
            lambda d: d,
        "above 20/50/200 sma":
            lambda d: d[(d.above_sma20 == 1) & (d.above_sma50 == 1) & (d.above_sma200 == 1)],
        "20-day breakout":
            lambda d: d[d.breakout20 == 1],
        "breakout + above 200sma":
            lambda d: d[(d.breakout20 == 1) & (d.above_sma200 == 1)],
        "breakout + rvol>=2":
            lambda d: d[(d.breakout20 == 1) & (d.rvol >= 2)],
        "breakout + rvol>=2 + 200sma":
            lambda d: d[(d.breakout20 == 1) & (d.rvol >= 2) & (d.above_sma200 == 1)],
        "mom20 top decile":
            lambda d: d[d.mom20 >= d.mom20.quantile(0.90)],
        "mom60 top decile + 200sma":
            lambda d: d[(d.mom60 >= d.mom60.quantile(0.90)) & (d.above_sma200 == 1)],
        "rsi>=70 + above 200sma":
            lambda d: d[(d.rsi >= 70) & (d.above_sma200 == 1)],
    }

    for block_name, block in (("TRAIN — choose here", train), ("TEST — judge here", test)):
        print("=" * 86)
        print(f"  {block_name}   (hold 5 sessions, exit at the close)")
        print("=" * 86)
        print(HEAD)
        for label, fn in rules.items():
            show(label, fn(block)["f5"])
        print()

    print("=" * 86)
    print("  HOLDING PERIOD — does the edge grow faster than the cost? (TEST only)")
    print("=" * 86)
    print(HEAD)
    best = test[(test.breakout20 == 1) & (test.rvol >= 2) & (test.above_sma200 == 1)]
    for n in (3, 5, 10):
        show(f"breakout+rvol+200sma, {n} sessions", best[f"f{n}"])

    print()
    print("=" * 86)
    print("  WITH A STOP (TEST only, 5-session hold)")
    print("=" * 86)
    print(HEAD)
    for k in (1.5, 2.5, 4.0):
        s = best.atr_pct * k
        r = np.where(best.mae5 <= -s, -s, best.f5)
        show(f"stop at {k}x ATR", r)

    print()
    print("=" * 86)
    print("  WHAT TO CONCLUDE")
    print("=" * 86)
    print("  Read the TEST block and nothing else. The rules above were written")
    print("  after a long look at intraday data and at the TRAIN half, so their")
    print("  TRAIN numbers are not evidence of anything.")
    print()
    print("  If TEST net is comfortably positive with t>3 on a few thousand")
    print("  observations, holding for days is the fix and it is worth building.")
    print("  If TEST net hovers near zero, then the momentum is real at every")
    print("  horizon and monetisable at none of them, and the right answer is to")
    print("  stop trading this account and say so plainly.")


if __name__ == "__main__":
    main()
