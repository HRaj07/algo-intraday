"""
The one candidate that survived, tested as hard as 59 days allows.

    python3 study_candidate.py

WHAT SURVIVED AND WHY THIS ONE
------------------------------
study_selection.py graded four features that are not obviously related to each
other, and every one of them sloped the same way (all figures net of the
0.0955% round-trip cost, held to the session close):

    RSI          60-65 -0.126%   65-70 -0.026%   70-75 +0.017%   75+  +0.147%
    rel volume   1.2-2 -0.050%   2-4   +0.014%   4+    +0.128%
    dev >vwap    0.8-1.2 0.000%  1.2-1.8 +0.082% 1.8-3 +0.117%
    ATR          0.4-0.6 -0.017% 0.6-0.9 +0.062% 0.9+  +0.318%

One bucket looking good is luck. Four unrelated features each grading
monotonically in the same direction is a relationship: the return concentrates
in strong, heavily-traded, volatile extensions. Three of the top buckets also
cleared cost in July, August AND September separately.

WHAT DID NOT SURVIVE, AND IS THE REASON THIS FILE IS CAUTIOUS
------------------------------------------------------------
Section 7 of that study stacked filters until the number was beautiful:

    early + NIFTY above VWAP + rvol>=1.2 + ATR>=0.6% + dev>=1.2%
    net +0.341%   t=+3.73   n=321
    Jul +0.465   Aug +0.474   Sep -0.227          <- September

It was built by piling on whatever looked good, including a time-of-day cut
that was ALREADY September-negative on its own. That is overfitting, it was
produced in the course of this very analysis, and it is what every row below
is being checked against. A cut is only reported as surviving if all three
months clear cost independently.

WHAT THIS STILL CANNOT DO
-------------------------
Every threshold here was chosen after looking at the data it is scored on.
yfinance serves 60 days of 15-minute bars and no more, so there is no held-out
period, and "all three months positive" is three observations, not a
validation. Nothing here has been proven. The output is a hypothesis to run
forward in paper, at which point the trades ARE the out-of-sample test.
"""
import pickle
from datetime import time as dtime

import numpy as np
import pandas as pd

from config import STRATEGY, SYSTEM, RISK, INDEX_TICKER, VIX_TICKER
from costs import VARIABLE_ROUNDTRIP_PCT, cost_in_rupees
from data.fetcher import TechnicalIndicators

CACHE = "cache_recent_15m.pkl"
TI = TechnicalIndicators()
COST = VARIABLE_ROUNDTRIP_PCT


def tstat(x):
    x = np.asarray(x, dtype=float)
    if len(x) < 2:
        return 0.0
    sd = x.std(ddof=1)
    return 0.0 if sd == 0 else x.mean() / (sd / np.sqrt(len(x)))


def build(data):
    first = dtime(*map(int, SYSTEM["first_entry_time"].split(":")))
    last = dtime(*map(int, SYSTEM["last_entry_time"].split(":")))
    rows = []
    for tkr, df in data.items():
        if tkr in (INDEX_TICKER, VIX_TICKER) or len(df) < 100:
            continue
        df = df.sort_index()
        rsi = TI.rsi(df, STRATEGY["rsi_period"])
        atr = TI.atr(df, STRATEGY["atr_period"])
        vwap = TI.vwap(df)
        close, opn, high, low, vol = (df["close"], df["open"], df["high"],
                                      df["low"], df["volume"])
        dates = pd.Series(df.index.date, index=df.index)

        entry = opn.shift(-1)
        entry[dates.shift(-1) != dates] = np.nan
        eod_close = close.groupby(dates.values).transform("last")

        rev_low = low[::-1].groupby(dates.values[::-1]).cummin()[::-1]
        rev_high = high[::-1].groupby(dates.values[::-1]).cummax()[::-1]

        rows.append(pd.DataFrame({
            "ticker": tkr, "date": dates,
            "bar_no": dates.groupby(dates.values).cumcount(),
            "dev_above": (close - vwap) / vwap,
            "rsi": rsi,
            "atr_pct": atr / close,
            "rvol": vol / vol.rolling(50, min_periods=20).median(),
            "turnover": (close * vol).rolling(50, min_periods=20).median(),
            "entry": entry,
            "eod": eod_close / entry - 1.0,
            "mae": rev_low.shift(-1) / entry - 1.0,
            "mfe": rev_high.shift(-1) / entry - 1.0,
        })[(pd.Series(df.index.time, index=df.index) >= first)
           & (pd.Series(df.index.time, index=df.index) <= last)
           & entry.notna() & vwap.notna() & rsi.notna() & atr.notna()])
    return pd.concat(rows, ignore_index=True)


def report(label, r, months=None, sub=None):
    r = pd.Series(r).dropna()
    if len(r) < 40:
        print(f"  {label:<34}{len(r):>7,}   (too few to judge)")
        return
    line = (f"  {label:<34}{len(r):>7,}  {r.mean()*100:>+7.3f}%  "
            f"{(r.mean()-COST)*100:>+7.3f}%  {(r>0).mean()*100:>5.1f}%  {tstat(r):>+6.2f}")
    if months is not None and sub is not None:
        per = []
        for mo in ("2026-07", "2026-08", "2026-09"):
            m = r[months.loc[sub.index] == mo]
            per.append(f"{(m.mean()-COST)*100:+.3f}" if len(m) >= 20 else "  --   ")
        line += "  " + " ".join(f"{p:>7}" for p in per)
        if all(p.strip().startswith("+") for p in per):
            line += "  3/3"
    print(line)


HEAD = (f"  {'':<34}{'n':>7}  {'gross':>8}  {'net':>8}  {'win%':>6}  {'t':>6}"
        f"  {'Jul':>7} {'Aug':>7} {'Sep':>7}")


def main():
    data = pickle.load(open(CACHE, "rb"))
    tab = build(data)
    months = pd.to_datetime(pd.Series(tab.date, index=tab.index)).dt.strftime("%Y-%m")
    ndays = tab.date.nunique()
    print(f"{len(tab):,} bars, {ndays} days, cost {COST*100:.4f}% round trip")
    print("  'net' is after cost. 3/3 = cleared cost in July, August AND September.\n")

    base = tab[(tab.dev_above >= 0.008) & (tab.rsi >= 60)]

    print("=" * 104)
    print("  1. THE THREE MONTH-ROBUST CUTS, SEPARATELY AND TOGETHER")
    print("=" * 104)
    print(HEAD)
    report("unselected momentum", base.eod, months, base)
    cuts = {
        "RSI >= 75": base[base.rsi >= 75],
        "rvol >= 4": base[base.rvol >= 4],
        "dev >= 1.2%": base[base.dev_above >= 0.012],
    }
    for k, v in cuts.items():
        report(k, v.eod, months, v)
    print()
    a = base[(base.rsi >= 75) & (base.rvol >= 4)]
    report("RSI>=75 AND rvol>=4", a.eod, months, a)
    b = base[(base.rsi >= 75) & (base.dev_above >= 0.012)]
    report("RSI>=75 AND dev>=1.2%", b.eod, months, b)
    c = base[(base.rsi >= 75) & (base.rvol >= 4) & (base.dev_above >= 0.012)]
    report("all three", c.eod, months, c)

    print()
    print("=" * 104)
    print("  2. THE SAME CUTS WITH A REAL STOP (and the trade held to the close)")
    print("=" * 104)
    print(HEAD)
    for name, v in [("RSI>=75", cuts["RSI >= 75"]), ("RSI>=75 AND rvol>=4", a)]:
        for s in (0.006, 0.008, 0.012, 0.016):
            r = pd.Series(np.where(v.mae <= -s, -s, v.eod), index=v.index)
            report(f"{name}, {s*100:.1f}% stop", r, months, v)
        print()

    print("=" * 104)
    print("  3. IS IT TRADEABLE? — signals per day, and what the book can hold")
    print("=" * 104)
    for k, v in [("unselected momentum", base), ("RSI >= 75", cuts["RSI >= 75"]),
                 ("RSI>=75 AND rvol>=4", a), ("all three", c)]:
        per_day = len(v) / ndays
        liquid = v[v.turnover >= RISK["min_median_15m_turnover"]]
        print(f"  {k:<26}{per_day:>7.1f}/day   "
              f"{len(liquid)/ndays:>6.1f}/day pass the turnover floor   "
              f"({v.date.nunique()} of {ndays} days had one)")
    print(f"\n  the book holds {RISK['max_concurrent_positions']} positions, "
          f"{RISK['max_entries_per_day']} entries/day")

    print()
    print("=" * 104)
    print("  4. IS IT A FEW NAMES? (RSI>=75 AND rvol>=4)")
    print("=" * 104)
    g = a.groupby("ticker")["eod"]
    per = pd.DataFrame({"n": g.size(), "mean": g.mean()})
    per = per[per.n >= 10]
    print(f"  {len(per)} names with >=10 signals, "
          f"{(per['mean'] > COST).sum()} of them beat cost "
          f"({(per['mean'] > COST).mean()*100:.0f}%)")
    print(f"  median name  {per['mean'].median()*100:+.3f}% gross")
    top5 = per.sort_values("mean", ascending=False).head(5).index
    rest = a[~a.ticker.isin(top5)]
    print(HEAD)
    report("excluding the best 5 names", rest.eod, months, rest)

    print()
    print("=" * 104)
    print("  5. WHAT A TRADE ACTUALLY LOOKS LIKE")
    print("=" * 104)
    v = a
    print(f"  median adverse excursion   {v.mae.median()*100:+.3f}%")
    print(f"  median favourable          {v.mfe.median()*100:+.3f}%")
    print(f"  median ATR                 {v.atr_pct.median()*100:.3f}%")
    print(f"  median bar entered         #{int(v.bar_no.median())} of ~25")
    eq = SYSTEM["initial_capital"]
    risk_r = eq * RISK["risk_pct_per_trade"]
    for s in (0.008, 0.012):
        notional = risk_r / s
        fr = cost_in_rupees(notional)
        print(f"  at a {s*100:.1f}% stop: Rs{notional:,.0f} notional, "
              f"Rs{fr:,.0f} friction = {fr/risk_r*100:.0f}% of the Rs{risk_r:,.0f} risked")

    print()
    print("=" * 104)
    print("  6. WHAT THIS IS")
    print("=" * 104)
    print("  A hypothesis, chosen after looking at the only 59 days of 15-minute")
    print("  data that exist. Three positive months is three observations. The")
    print("  honest test is forward, in paper, where the trades have not been")
    print("  seen before - and README.md's go-live bar (t>2 on live paper trades,")
    print("  not on this) is unchanged by anything in this file.")


if __name__ == "__main__":
    main()
