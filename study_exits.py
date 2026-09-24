"""
Which in-trade exit rules help, on identical entries?

    python3 study_exits.py

WHY THIS IS ITS OWN STUDY
-------------------------
The sweep in study_sweep.py tested one in-trade device - a breakeven trail -
and it lost money in all 50 cells it appeared in. That is a result about ONE
rule, not about managing open trades in general. Before the bot gets any logic
for "when to take it out", the candidates deserve the same test on the same
data, against the same baseline: enter identically, hold to the close with a
plain stop, and see whether each rule adds or subtracts.

Entries are the live v3 rule from config.MOMENTUM, so every policy below sees
exactly the same trades. Only the exit differs. That makes the comparison fair
in a way the per-signal averages elsewhere are not: a rule cannot win here by
picking different entries.

THE RULES
---------
  hold       stop at the floor, square off at the close     <- what v3 does
  trail k    stop ratchets to (highest high since entry - k x ATR), never down
  vwap       exit the bar price closes back below VWAP - the thesis is gone
  rsi<50     exit the bar RSI drops through 50
  index      exit if NIFTY has fallen 0.5% from where it was at entry
  half@1R    take half at +1R, run the rest to the close
  early      square off at 14:30 instead of 15:05

Each is scored net of the real cost model, month by month, and with its best
five trades removed - the check that every rule in the sweep failed.

WHAT IT CANNOT SAY
------------------
The same 59 days as everything else, and the same absence of a holdout. A rule
that wins here is a candidate for the walk-forward, not a rule to switch on.
"""
import pickle
from datetime import time as dtime

import numpy as np
import pandas as pd

from config import MOMENTUM, FILTERS, SYSTEM, INDEX_TICKER, VIX_TICKER
from costs import cost_in_rupees, VARIABLE_ROUNDTRIP_PCT
from data.fetcher import TechnicalIndicators

CACHE = "cache_recent_15m.pkl"
TI = TechnicalIndicators()
NOTIONAL = 300_000.0          # a fixed size, so exits are compared on returns


def paths(data):
    """
    One record per live-rule signal: the entry, and the bar-by-bar path from
    the entry bar to the close of that day, with the indicators an exit rule
    could have looked at on each bar.
    """
    first = dtime(*map(int, SYSTEM["first_entry_time"].split(":")))
    last = dtime(*map(int, SYSTEM["last_entry_time"].split(":")))
    sq = dtime(*map(int, SYSTEM["square_off_time"].split(":")))
    p = MOMENTUM

    idx = data.get(INDEX_TICKER)
    idx = idx.sort_index().rename_axis(None) if idx is not None else None

    out = []
    for tkr, df in data.items():
        if tkr in (INDEX_TICKER, VIX_TICKER) or len(df) < 100:
            continue
        df = df.sort_index().rename_axis(None).copy()
        df["rsi"] = TI.rsi(df, p["rsi_period"])
        df["atr"] = TI.atr(df, p["atr_period"])
        df["vwap"] = TI.vwap(df)
        df["rvol"] = df["volume"] / df["volume"].rolling(p["rvol_lookback_bars"],
                                                         min_periods=20).median()
        df["turn"] = (df["close"] * df["volume"]).rolling(50, min_periods=20).median()
        df["d"] = df.index.date
        df["t"] = df.index.time

        for day, g in df.groupby("d"):
            if len(g) < 8:
                continue
            g = g.reset_index()
            ig = idx[idx.index.date == day].reset_index() if idx is not None else None
            for i in range(len(g) - 2):
                b = g.iloc[i]
                if not (first <= b.t <= last):
                    continue
                if not np.isfinite([b.vwap, b.rsi, b.atr, b.rvol, b.turn]).all():
                    continue
                dev = (b.close - b.vwap) / b.vwap
                atr_pct = b.atr / b.close
                if (dev < p["min_dev_above_vwap"] or b.rsi < p["min_rsi"]
                        or b.rvol < p["min_rvol"] or atr_pct < p["min_atr_pct"]
                        or b.turn < FILTERS["min_median_15m_turnover"]):
                    continue
                stop_pct = max(p["stop_pct_floor"], p["stop_atr_mult"] * atr_pct)
                if stop_pct > p["stop_pct_cap"]:
                    continue
                entry = float(g.iloc[i + 1].open)
                if not np.isfinite(entry) or entry <= 0:
                    continue
                path = g.iloc[i + 1:].copy()
                path = path[path.t <= sq] if (path.t <= sq).any() else path
                # NIFTY level at entry and along the path, matched by timestamp
                if ig is not None and len(ig):
                    lvl = ig.set_index("index")["close"]
                    path["nifty"] = path["index"].map(lvl).ffill().values
                    n0 = float(lvl[lvl.index <= g.iloc[i]["index"]].iloc[-1]) \
                        if (lvl.index <= g.iloc[i]["index"]).any() else np.nan
                else:
                    path["nifty"] = np.nan
                    n0 = np.nan
                out.append({"ticker": tkr, "date": day, "entry": entry,
                            "stop_pct": stop_pct, "atr": float(b.atr),
                            "nifty0": n0, "path": path})
    return out


def run(rec, rule, k=None):
    """Return the net P&L of one trade under one exit rule."""
    e, sp, atr = rec["entry"], rec["stop_pct"], rec["atr"]
    qty = int(NOTIONAL / e)
    stop = e * (1 - sp)
    hi = e
    r1 = e * (1 + sp)
    half_done = False
    pnl_partial = 0.0
    px, why = None, None
    p = rec["path"]
    for j in range(len(p)):
        b = p.iloc[j]
        hi = max(hi, float(b.high))
        if rule == "trail":
            stop = max(stop, hi - k * atr)
        if float(b.low) <= stop:
            px, why = stop, "stop"
            break
        if rule == "half" and not half_done and float(b.high) >= r1:
            q = qty // 2
            pnl_partial += (r1 - e) * q - cost_in_rupees(r1 * q) / 2
            qty -= q
            half_done = True
        if j > 0:   # thesis rules read the bar's close, act at the next open
            if rule == "vwap" and float(b.close) < float(b.vwap):
                px, why = float(p.iloc[j + 1].open) if j + 1 < len(p) else float(b.close), "vwap"
                break
            if rule == "rsi" and float(b.rsi) < 50:
                px, why = float(p.iloc[j + 1].open) if j + 1 < len(p) else float(b.close), "rsi"
                break
            if rule == "index" and np.isfinite(rec["nifty0"]) and np.isfinite(b.nifty) \
                    and b.nifty < rec["nifty0"] * (1 - 0.005):
                px, why = float(p.iloc[j + 1].open) if j + 1 < len(p) else float(b.close), "index"
                break
        if rule == "early" and b.t >= dtime(14, 30):
            px, why = float(b.close), "early"
            break
    if px is None:
        px, why = float(p.iloc[-1].close), "close"
    slip = px * qty * (0.0005 if why == "stop" else 0.0003)
    fric = (cost_in_rupees(e * (qty + (qty if half_done else 0))) / 2
            + cost_in_rupees(px * qty) / 2)
    return (px - e) * qty - fric - slip + pnl_partial, why


def score(pnls, months):
    p = np.asarray(pnls)
    sd = p.std(ddof=1)
    t = p.mean() / (sd / np.sqrt(len(p))) if sd else 0
    per = {m: float(p[months == m].sum()) for m in sorted(set(months))}
    ex5 = float(np.sort(p)[::-1][5:].sum())
    ok = all(v > 0 for v in per.values()) and ex5 > 0
    return dict(n=len(p), net=float(p.sum()), avg=float(p.mean()), t=float(t),
                win=float((p > 0).mean()), ex5=ex5, per=per, ok=ok)


def main():
    data = pickle.load(open(CACHE, "rb"))
    recs = paths(data)
    months = np.array([str(r["date"])[:7] for r in recs])
    print(f"{len(recs)} live-rule signals over {len(set(r['date'] for r in recs))} days, "
          f"Rs{NOTIONAL:,.0f} per trade, cost {VARIABLE_ROUNDTRIP_PCT*100:.4f}% + slippage\n")

    rules = [("hold to close (v3 now)", "hold", None),
             ("trail 1.5xATR", "trail", 1.5),
             ("trail 2.5xATR", "trail", 2.5),
             ("trail 3.5xATR", "trail", 3.5),
             ("exit on close < VWAP", "vwap", None),
             ("exit on RSI < 50", "rsi", None),
             ("exit if NIFTY -0.5%", "index", None),
             ("half at +1R", "half", None),
             ("square off 14:30", "early", None)]

    mo = sorted(set(months))
    print(f"  {'rule':<26}{'n':>5} {'net':>10} {'avg':>8} {'win%':>6} {'t':>6} "
          + " ".join(f"{m[-2:]:>8}" for m in mo) + f" {'ex-top5':>9}  survives")
    base = None
    for label, rule, k in rules:
        res = [run(r, rule, k) for r in recs]
        s = score([x[0] for x in res], months)
        if base is None:
            base = s
        d = s["net"] - base["net"]
        print(f"  {label:<26}{s['n']:>5} {s['net']:>10,.0f} {s['avg']:>+8,.0f} "
              f"{s['win']*100:>5.1f}% {s['t']:>+6.2f} "
              + " ".join(f"{s['per'][m]:>8,.0f}" for m in mo)
              + f" {s['ex5']:>9,.0f}  {'YES' if s['ok'] else '-'}"
              + (f"   ({d:+,.0f} vs hold)" if rule != "hold" else ""))

    print()
    print("=" * 100)
    print("  HOW TO READ THIS")
    print("=" * 100)
    print("  Every rule sees the SAME entries, so a difference is the exit and nothing")
    print("  else. A rule only earns a place in the bot if it beats 'hold' AND is")
    print("  positive in every month AND survives losing its best five trades - the")
    print("  test every exit device in the threshold sweep failed. Same 59 days as")
    print("  everything else, no holdout: a survivor here is a walk-forward candidate,")
    print("  not a rule to switch on.")


if __name__ == "__main__":
    main()
