"""
The momentum effect is real but thin. Can SELECTION make it tradeable?

    python3 study_selection.py

Where this comes from
---------------------
study_momentum.py established, on tradeable entries:

    long, stretched above VWAP, RSI>=60, held to the close
        gross +0.127%   t=+6.34   n=5,132
        both halves of the sample positive, 59% of names positive,
        still +0.074% with the best five names removed

and it established the problem in the same table:

    net of the 0.0955% round-trip cost      +0.032%
    net, excluding the best five names      -0.021%
    net, month by month        Jul -0.042%   Aug +0.174%   Sep -0.027%

So the effect exists and friction eats almost all of it. One month out of
three carried the whole result.

But that number is the average of EVERY occurrence - 5,132 signals over 59
days is about 87 a day, and the book can hold three. A real system does not
take the average signal, it takes the best three. The question this file
answers is whether "best" means anything here: is there any feature available
at entry that sorts these signals into a subset worth trading?

If the top slice by some criterion clears cost by a real margin, there is a
strategy. If every slice looks like the average, there is not - and the honest
conclusion is that this effect is visible but not harvestable at retail cost.

Danger
------
This file searches for a subset that looks good in a 59-day sample, which is
precisely how overfitting happens. Every cut below is reported with its sample
size and t-stat, and a cut that only works in one of the three months is
reported as failing even if its total looks strong. The monthly consistency
column is the one to read first.
"""
import pickle
from datetime import time as dtime

import numpy as np
import pandas as pd

from config import STRATEGY, SYSTEM, INDEX_TICKER, VIX_TICKER
from costs import VARIABLE_ROUNDTRIP_PCT
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

    index_df = data.get(INDEX_TICKER)
    idx_state = {}
    if index_df is not None and not index_df.empty:
        idf = index_df.sort_index()
        ivwap = TI.vwap(idf)
        idates = pd.Series(idf.index.date, index=idf.index)
        iopen = idf["open"].groupby(idates.values).transform("first")
        idx_state = pd.DataFrame({
            "idx_above_vwap": (idf["close"] >= ivwap).astype(float),
            "idx_day_move": idf["close"] / iopen - 1.0,
        })

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
        r_eod = eod_close / entry - 1.0

        rev_low = low[::-1].groupby(dates.values[::-1]).cummin()[::-1]
        mae = rev_low.shift(-1) / entry - 1.0

        # relative volume: this bar's volume against this name's own recent
        # median. For momentum the sign of the bet is the opposite of the
        # reversion filter - volume means participation, not danger.
        rvol = vol / vol.rolling(50, min_periods=20).median()

        # how far into the day, and how much day is left
        bar_no = dates.groupby(dates.values).cumcount()

        # distance above vwap, and whether the move is extending or stalling
        dev_above = (close - vwap) / vwap
        ret_1 = close / close.shift(1) - 1.0
        ret_4 = close / close.shift(4) - 1.0

        t = pd.Series(df.index.time, index=df.index)
        keep = ((t >= first) & (t <= last) & entry.notna() & vwap.notna()
                & rsi.notna() & atr.notna() & (close > 0))

        sub = pd.DataFrame({
            "ticker": tkr, "date": dates, "bar_no": bar_no,
            "dev_above": dev_above, "rsi": rsi, "atr_pct": atr / close,
            "rvol": rvol, "ret_1": ret_1, "ret_4": ret_4,
            "turnover": (close * vol).rolling(50, min_periods=20).median(),
            "eod": r_eod, "mae": mae,
        })[keep]
        if len(idx_state):
            sub = sub.join(idx_state, how="left")
        rows.append(sub)

    return pd.concat(rows, ignore_index=True)


HEAD = (f"  {'cut':<32}{'n':>7}  {'gross':>8}  {'net':>8}  {'win%':>6}  "
        f"{'t':>6}  {'Jul':>7} {'Aug':>7} {'Sep':>7}")


def row(label, sub, months):
    x = sub["eod"].dropna()
    if len(x) < 40:
        print(f"  {label:<32}{len(x):>7,}   (too few)")
        return
    per = []
    for mo in ("2026-07", "2026-08", "2026-09"):
        m = sub.loc[months.loc[sub.index] == mo, "eod"].dropna()
        per.append(f"{(m.mean()-COST)*100:+.3f}" if len(m) >= 20 else "   --  ")
    ok = sum(1 for p in per if p.strip() not in ("--", "") and p.strip()[0] == "+")
    flag = "  <-- 3/3" if ok == 3 else ""
    print(f"  {label:<32}{len(x):>7,}  {x.mean()*100:>+7.3f}%  "
          f"{(x.mean()-COST)*100:>+7.3f}%  {(x>0).mean()*100:>5.1f}%  "
          f"{tstat(x):>+6.2f}  " + " ".join(f"{p:>7}" for p in per) + flag)


def main():
    data = pickle.load(open(CACHE, "rb"))
    tab = build(data)
    months = pd.to_datetime(pd.Series(tab.date, index=tab.index)).dt.strftime("%Y-%m")
    dev_floor = STRATEGY["min_vwap_deviation"]
    base = tab[(tab.dev_above >= dev_floor) & (tab.rsi >= 60)].copy()
    print(f"{len(tab):,} bars -> {len(base):,} momentum signals "
          f"({len(base)/tab.date.nunique():.0f}/day). cost {COST*100:.4f}%")
    print("  net columns are AFTER cost. A cut only counts if all three months clear it.\n")

    print("=" * 100)
    print("  0. THE UNSELECTED AVERAGE — everything below must beat this")
    print("=" * 100)
    print(HEAD)
    row("all momentum signals", base, months)

    print()
    print("=" * 100)
    print("  1. TIME OF DAY — is 'hold to close' just 'hold longer'?")
    print("=" * 100)
    print(HEAD)
    for lo, hi, lbl in [(0, 4, "bars 0-3 (09:15-10:15)"), (4, 8, "bars 4-7 (10:15-11:15)"),
                        (8, 12, "bars 8-11 (11:15-12:15)"), (12, 16, "bars 12-15 (12:15-13:15)"),
                        (16, 30, "bars 16+ (13:15-)")]:
        row(lbl, base[(base.bar_no >= lo) & (base.bar_no < hi)], months)

    print()
    print("=" * 100)
    print("  2. INDEX REGIME — does it need the market on its side?")
    print("=" * 100)
    print(HEAD)
    if "idx_above_vwap" in base:
        row("NIFTY above its VWAP", base[base.idx_above_vwap == 1], months)
        row("NIFTY below its VWAP", base[base.idx_above_vwap == 0], months)
        row("NIFTY up >0.3% on day", base[base.idx_day_move > 0.003], months)
        row("NIFTY down on day", base[base.idx_day_move < 0], months)

    print()
    print("=" * 100)
    print("  3. RELATIVE VOLUME — participation, or exhaustion?")
    print("=" * 100)
    print(HEAD)
    for lo, hi in [(0, 0.8), (0.8, 1.2), (1.2, 2.0), (2.0, 4.0), (4.0, 99)]:
        row(f"rvol {lo}-{hi}", base[(base.rvol >= lo) & (base.rvol < hi)], months)

    print()
    print("=" * 100)
    print("  4. HOW FAR ABOVE VWAP")
    print("=" * 100)
    print(HEAD)
    for lo, hi in [(0.008, 0.012), (0.012, 0.018), (0.018, 0.030), (0.030, 1.0)]:
        row(f"dev {lo*100:.1f}-{hi*100:.1f}% above", base[(base.dev_above >= lo) & (base.dev_above < hi)], months)

    print()
    print("=" * 100)
    print("  5. RSI LEVEL")
    print("=" * 100)
    print(HEAD)
    for lo, hi in [(60, 65), (65, 70), (70, 75), (75, 100)]:
        row(f"RSI {lo}-{hi}", base[(base.rsi >= lo) & (base.rsi < hi)], months)

    print()
    print("=" * 100)
    print("  6. VOLATILITY — cheap names move less than costs")
    print("=" * 100)
    print(HEAD)
    for lo, hi in [(0, 0.004), (0.004, 0.006), (0.006, 0.009), (0.009, 1)]:
        row(f"ATR {lo*100:.1f}-{hi*100:.1f}%", base[(base.atr_pct >= lo) & (base.atr_pct < hi)], months)

    print()
    print("=" * 100)
    print("  7. THE COMBINATION THAT THE ABOVE POINTS AT")
    print("=" * 100)
    print(HEAD)
    early = base.bar_no < 12
    if "idx_above_vwap" in base:
        c = base[early & (base.idx_above_vwap == 1)]
        row("early + NIFTY above VWAP", c, months)
        c2 = c[c.rvol >= 1.2]
        row("  + rvol>=1.2", c2, months)
        c3 = c2[c2.atr_pct >= 0.006]
        row("    + ATR>=0.6%", c3, months)
        c4 = c3[c3.dev_above >= 0.012]
        row("      + dev>=1.2%", c4, months)

    print()
    print("=" * 100)
    print("  8. READ THIS BEFORE BELIEVING ANY ROW ABOVE")
    print("=" * 100)
    print("  Every cut here was chosen after seeing the data it is scored on.")
    print("  With 59 days and this many slices, some will look good by luck. The")
    print("  monthly columns are the only defence: a cut that clears cost in all")
    print("  three months on a few hundred signals is a hypothesis; one that")
    print("  clears only in August is August.")


if __name__ == "__main__":
    main()
