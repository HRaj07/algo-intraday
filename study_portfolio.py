"""
Turn the per-signal average into an account P&L.

    python3 study_portfolio.py

WHY THIS IS A DIFFERENT NUMBER
------------------------------
study_candidate.py says the candidate rule is worth +0.255% per signal, net of
cost, over 963 signals. That is an average over EVERY occurrence, and it is not
a number you can spend, for three reasons:

  1. There are 16 signals a day and the book holds 3 positions. You take a
     fifth of them, and which fifth depends on arrival order and on what you
     are already holding - not on which turned out best.
  2. Concurrency is capital. Three positions held to the close means the fourth
     good signal of the day is declined, and the rule's fat right tail may well
     be in the ones declined.
  3. A per-signal mean weights a Rs50,000 position the same as a Rs5,00,000 one.
     The account does not.

So this file runs the rule as a book: first-come-first-served within the
portfolio caps from config.py, sized off live equity, stopped at a real stop,
squared off at the session close, charged the real cost model on both sides.

WHAT IT STILL FLATTERS
----------------------
Entry at the next bar's open assumes a fill at that open on a stock that is
running hard on four times its normal volume; real slippage there is worse than
the 1bp the cost model assigns to a limit entry. Stops are assumed to fill at
the stop price, not through it. And the thresholds were chosen after seeing
these 59 days. Read the result as an upper bound on a hypothesis.
"""
import pickle
from collections import defaultdict
from datetime import time as dtime

import numpy as np
import pandas as pd

from config import STRATEGY, SYSTEM, RISK, SECTOR, INDEX_TICKER, VIX_TICKER
from costs import cost_in_rupees, VARIABLE_ROUNDTRIP_PCT
from data.fetcher import TechnicalIndicators

CACHE = "cache_recent_15m.pkl"
TI = TechnicalIndicators()

# the candidate rule, stated once
RSI_MIN = 75
RVOL_MIN = 4.0
DEV_MIN = 0.008
STOP_PCT = 0.012


def prepare(data):
    first = dtime(*map(int, SYSTEM["first_entry_time"].split(":")))
    last = dtime(*map(int, SYSTEM["last_entry_time"].split(":")))
    frames = {}
    for tkr, df in data.items():
        if tkr in (INDEX_TICKER, VIX_TICKER) or len(df) < 100:
            continue
        df = df.sort_index().copy()
        df["rsi"] = TI.rsi(df, STRATEGY["rsi_period"])
        df["vwap"] = TI.vwap(df)
        df["rvol"] = df["volume"] / df["volume"].rolling(50, min_periods=20).median()
        df["turnover"] = (df["close"] * df["volume"]).rolling(50, min_periods=20).median()
        d = pd.Series(df.index.date, index=df.index)
        t = pd.Series(df.index.time, index=df.index)
        df["dev_above"] = (df["close"] - df["vwap"]) / df["vwap"]
        df["is_signal"] = (
            (df.dev_above >= DEV_MIN) & (df.rsi >= RSI_MIN) & (df.rvol >= RVOL_MIN)
            & (df.turnover >= RISK["min_median_15m_turnover"])
            & (t >= first) & (t <= last)
        )
        df["_date"] = d
        frames[tkr] = df
    return frames


def run(frames, capital):
    all_days = sorted({d for f in frames.values() for d in f["_date"].unique()})
    equity = capital
    trades = []
    daily = []

    for day in all_days:
        day_frames = {t: f[f._date == day] for t, f in frames.items()}
        day_frames = {t: f for t, f in day_frames.items() if len(f) >= 8}
        if not day_frames:
            continue
        nbars = max(len(f) for f in day_frames.values())
        open_pos = {}
        entries_today = 0
        day_pnl = 0.0

        for i in range(nbars):
            # ---- manage what is open, on this bar ----
            for tkr in list(open_pos):
                f = day_frames[tkr]
                if i >= len(f):
                    continue
                bar = f.iloc[i]
                p = open_pos[tkr]
                exit_px = reason = None
                if bar["low"] <= p["stop"]:
                    exit_px, reason = p["stop"], "stop"
                elif i == len(f) - 1:
                    exit_px, reason = bar["close"], "square_off"
                if exit_px is None:
                    continue
                notional_out = exit_px * p["qty"]
                fric = cost_in_rupees(p["notional"]) / 2 + cost_in_rupees(notional_out) / 2
                slip = notional_out * 0.0005 if reason == "stop" else notional_out * 0.0003
                pnl = (exit_px - p["entry"]) * p["qty"] - fric - slip
                equity += pnl
                day_pnl += pnl
                trades.append({"date": day, "ticker": tkr, "reason": reason,
                               "pnl": pnl, "R": pnl / p["risk_rs"],
                               "notional": p["notional"], "friction": fric + slip})
                del open_pos[tkr]

            # ---- look for entries, filled at the NEXT bar's open ----
            if entries_today >= RISK["max_entries_per_day"]:
                continue
            cands = []
            for tkr, f in day_frames.items():
                if i >= len(f) - 1 or tkr in open_pos:
                    continue
                if not f.iloc[i]["is_signal"]:
                    continue
                cands.append((f.iloc[i]["rvol"], tkr, f.iloc[i + 1]["open"]))
            # rank by participation - the feature that graded most steeply
            cands.sort(reverse=True)
            for _, tkr, entry_px in cands:
                if len(open_pos) >= RISK["max_concurrent_positions"]:
                    break
                if entries_today >= RISK["max_entries_per_day"]:
                    break
                sec = SECTOR.get(tkr, "OTHER")
                if sum(1 for o in open_pos if SECTOR.get(o, "OTHER") == sec) >= \
                        RISK["max_positions_per_sector"]:
                    continue
                if not np.isfinite(entry_px) or entry_px <= 0:
                    continue
                risk_rs = equity * RISK["risk_pct_per_trade"]
                stop = entry_px * (1 - STOP_PCT)
                qty = int(risk_rs / (entry_px - stop))
                notional = qty * entry_px
                if qty < 1 or notional < RISK["min_notional_per_trade"]:
                    continue
                cap_n = equity * RISK["max_notional_per_trade_pct"]
                if notional > cap_n:
                    qty = int(cap_n / entry_px)
                    notional = qty * entry_px
                if qty < 1:
                    continue
                open_pos[tkr] = {"entry": entry_px, "stop": stop, "qty": qty,
                                 "notional": notional, "risk_rs": (entry_px - stop) * qty}
                entries_today += 1
        daily.append({"date": day, "pnl": day_pnl, "equity": equity})

    return pd.DataFrame(trades), pd.DataFrame(daily), equity


def main():
    data = pickle.load(open(CACHE, "rb"))
    frames = prepare(data)
    cap = SYSTEM["initial_capital"]
    print(f"rule: dev>={DEV_MIN*100:.1f}% above VWAP, RSI>={RSI_MIN}, rvol>={RVOL_MIN}, "
          f"{STOP_PCT*100:.1f}% stop, hold to close")
    print(f"caps: {RISK['max_concurrent_positions']} concurrent, "
          f"{RISK['max_entries_per_day']}/day, {RISK['max_positions_per_sector']}/sector, "
          f"risk {RISK['risk_pct_per_trade']*100:.2f}% of Rs{cap:,.0f}\n")

    tr, daily, final = run(frames, cap)
    if tr.empty:
        print("no trades")
        return

    pnl = tr.pnl.values
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    pf = wins.sum() / abs(losses.sum()) if len(losses) else 99
    eq = cap + np.cumsum(pnl)
    dd = (np.maximum.accumulate(np.r_[cap, eq]) - np.r_[cap, eq]).max()
    sd = pnl.std(ddof=1)
    t = pnl.mean() / (sd / np.sqrt(len(pnl))) if sd else 0

    print("=" * 76)
    print("  ACCOUNT RESULT — the candidate rule run as a book")
    print("=" * 76)
    print(f"  trading days          {daily.date.nunique()}")
    print(f"  trades                {len(tr)}  ({len(tr)/daily.date.nunique():.1f}/day)")
    print(f"  win rate              {(pnl>0).mean()*100:.1f}%")
    print(f"  profit factor         {pf:.2f}")
    print(f"  net P&L               Rs{pnl.sum():,.0f}   ({pnl.sum()/cap*100:+.2f}%)")
    print(f"  friction paid         Rs{tr.friction.sum():,.0f}")
    print(f"  gross before costs    Rs{pnl.sum()+tr.friction.sum():,.0f}")
    print(f"  avg trade             Rs{pnl.mean():+,.0f}   ({tr.R.mean():+.3f}R)")
    print(f"  t-stat                {t:+.2f}   (>2 is the go-live bar)")
    print(f"  max drawdown          Rs{dd:,.0f}  ({dd/cap*100:.2f}%)")
    print(f"  median notional       Rs{tr.notional.median():,.0f}")

    print("\n  by exit:")
    for r, g in tr.groupby("reason"):
        print(f"    {r:<14}{len(g):>4} trades  Rs{g.pnl.sum():>10,.0f}  "
              f"avg Rs{g.pnl.mean():>+8,.0f}")

    print("\n  by month:")
    tr["mo"] = pd.to_datetime(tr.date).dt.strftime("%Y-%m")
    for mo, g in tr.groupby("mo"):
        print(f"    {mo}   {len(g):>4} trades  Rs{g.pnl.sum():>10,.0f}  "
              f"({g.pnl.sum()/cap*100:+.2f}%)  avg {g.R.mean():+.3f}R")

    print("\n  concentration — how much of the P&L is the best few trades:")
    s = np.sort(pnl)[::-1]
    for k in (1, 3, 5, 10):
        if len(s) > k:
            print(f"    top {k:<3} trades = Rs{s[:k].sum():>9,.0f} "
                  f"of Rs{pnl.sum():,.0f} total")
    print(f"    without the top 5: Rs{s[5:].sum():,.0f} "
          f"({s[5:].sum()/cap*100:+.2f}%)")

    print("\n" + "=" * 76)
    print("  HOW TO READ THIS")
    print("=" * 76)
    print("  The thresholds were picked after looking at these 59 days, entries")
    print("  assume a fill at the open of a bar in a fast-moving stock, and the")
    print("  concentration block above says how much of the result rests on a")
    print("  handful of trades. If that line says most of it, the rule needs")
    print("  those tails to survive - and tails are the first thing a live fill")
    print("  takes away.")


if __name__ == "__main__":
    main()
