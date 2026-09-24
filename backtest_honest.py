"""
Cost-honest, capital-constrained, walk-forward backtester.

WHY THE OLD BACKTESTS WERE WRONG
--------------------------------
profit_max_sweep.py - the file whose output every v1 parameter came from - made
four errors, each of which flatters the result:

  1. COST = Rs50 FLAT. Real round-trip at the notional it was implicitly taking
     is ~Rs421. Re-pricing its own 540 trades at the real rate turns the
     advertised +Rs1,44,564 into roughly -Rs56,000. The edge WAS the error.

  2. NO CAPITAL CONSTRAINT. `qty = max(1, int(RISK / risk))` with no cash check,
     so it took 2-5 full-size trades a day at Rs3.6L each on a Rs5L account.
     Live, trade #1 ate 73% of the book and the rest became qty=1 fragments.

  3. FILL AT THE SIGNAL BAR'S CLOSE. You cannot transact at a price you only
     learn when the bar closes. v2 fills on a trigger in the NEXT bar.

  4. TUNED ON HOURLY BARS, DEPLOYED ON 15-MINUTE BARS, 15 names vs 40 live.
     RSI 25->28 and deviation 0.8%->0.6% were loosened for the faster timeframe
     without re-running the sweep at all.

THE DISCIPLINE THIS FILE ENFORCES
---------------------------------
  - one cost model, imported from costs.py, shared with the live engine
  - the same bar interval the live system runs on
  - a real cash/margin ledger, so a trade that cannot be funded is not taken
  - train / validate / test split, with the test window touched ONCE
  - an explicit count of configurations tried, and a deflated significance test

Run:  python backtest_honest.py --data cache_15m.pkl
"""
import argparse
import itertools
import json
import math
import pickle
from collections import defaultdict
from datetime import time as dtime

import numpy as np
import pandas as pd

from costs import one_side_cost, cost_in_rupees, VARIABLE_ROUNDTRIP_PCT
from config import VALIDATION, RISK, SECTOR

TICK = 0.05


# ---------------------------------------------------------------- indicators
def vwap(df):
    tp = (df["high"] + df["low"] + df["close"]) / 3
    g = df.groupby(df.index.date)
    return (tp * df["volume"]).groupby(df.index.date).cumsum() / g["volume"].cumsum()


def rsi(close, n=14):
    d = close.diff()
    g = d.clip(lower=0).ewm(com=n - 1, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(com=n - 1, adjust=False).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))


def atr(df, n=14):
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - pc).abs(),
                    (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(com=n - 1, adjust=False).mean()


# ---------------------------------------------------------------- the engine
def run(data, index_df, params, start, end, capital=500_000, verbose=False):
    """
    data: {ticker: 15m OHLCV DataFrame, IST-naive index}
    Returns a dict of results. Every trade pays real costs and must be funded.
    """
    pre = {}
    for t, df in data.items():
        pre[t] = {"vwap": vwap(df), "rsi": rsi(df["close"]), "atr": atr(df)}

    idx_vwap = vwap(index_df) if index_df is not None else None

    cash = capital
    equity = capital
    peak = capital
    positions = {}
    trades = []
    rejected = defaultdict(int)
    day_entries = defaultdict(int)
    day_pnl = defaultdict(float)
    halted_days = set()

    days = sorted({d for d in next(iter(data.values())).index.date
                   if pd.Timestamp(start).date() <= d <= pd.Timestamp(end).date()})

    for day in days:
        # ---------- index regime for the day, bar by bar ----------
        idx_today = index_df[index_df.index.date == day] if index_df is not None else None
        if idx_today is None or len(idx_today) < 2:
            rejected["no index data"] += 1
            continue

        bars_today = {t: df[df.index.date == day] for t, df in data.items()}
        n_bars = max((len(v) for v in bars_today.values()), default=0)
        if n_bars < 8:
            continue

        open_pos = {}
        pending = {}

        for i in range(n_bars):
            # ---------------- manage open positions ----------------
            for tk in list(open_pos):
                p = open_pos[tk]
                bt = bars_today[tk]
                if i >= len(bt):
                    continue
                bar = bt.iloc[i]
                p["bars"] += 1
                R = p["risk"]
                exit_px = exit_rsn = None

                # clock first
                if bar.name.time() >= dtime(15, 5) or i == n_bars - 1:
                    exit_px, exit_rsn = bar["close"], "square_off"
                elif bar["low"] <= p["stop"]:
                    exit_px, exit_rsn = p["stop"], "stop"
                elif not p["t1_done"] and bar["high"] >= p["t1"]:
                    # scale half at VWAP, move to a COST-AWARE breakeven
                    half = max(1, p["qty"] // 2)
                    pnl = ((p["t1"] - p["entry"]) * half
                           - one_side_cost(p["entry"], half, "buy")
                           - one_side_cost(p["t1"], half, "sell"))
                    cash += p["margin"] * (half / p["qty0"]) + pnl
                    trades.append({"day": str(day), "ticker": tk, "pnl": pnl,
                                   "R": pnl / (R * p["qty0"]), "reason": "t1_vwap"})
                    day_pnl[day] += pnl
                    p["qty"] -= half
                    p["t1_done"] = True
                    fricR = cost_in_rupees(p["entry"] * p["qty0"]) / (R * p["qty0"])
                    p["stop"] = p["entry"] + (fricR + 0.15) * R
                    if p["qty"] <= 0:
                        del open_pos[tk]
                        continue
                elif p["t1_done"] and bar["high"] >= p["t2"]:
                    exit_px, exit_rsn = p["t2"], "t2"
                elif (p["bars"] >= params["time_stop_bars"] and not p["t1_done"]
                      and (bar["close"] - p["entry"]) / R < params["time_stop_min_R"]):
                    exit_px, exit_rsn = bar["close"], "time_stop"

                if p["t1_done"] and exit_rsn is None:
                    trail = bar["close"] - params["trail_atr"] * p["atr"]
                    p["stop"] = max(p["stop"], trail)

                if exit_rsn:
                    pnl = ((exit_px - p["entry"]) * p["qty"]
                           - one_side_cost(p["entry"], p["qty"], "buy")
                           - one_side_cost(exit_px, p["qty"], "sell"))
                    cash += p["margin"] * (p["qty"] / p["qty0"]) + pnl
                    trades.append({"day": str(day), "ticker": tk, "pnl": pnl,
                                   "R": pnl / (R * p["qty0"]), "reason": exit_rsn})
                    day_pnl[day] += pnl
                    del open_pos[tk]

            # ---------------- circuit breakers ----------------
            equity = cash + sum(p["entry"] * p["qty"] * 0.20 for p in open_pos.values())
            peak = max(peak, equity)
            if (peak - equity) / peak >= RISK["max_drawdown_halt_pct"]:
                halted_days.add(day)
            risk_budget = equity * RISK["risk_pct_per_trade"]
            if day_pnl[day] <= -RISK["daily_loss_limit_R"] * risk_budget:
                halted_days.add(day)
            if day in halted_days:
                continue

            # ---------------- fill pending triggers ----------------
            for tk in list(pending):
                o = pending[tk]
                o["age"] += 1
                bt = bars_today[tk]
                if o["age"] > params["trigger_valid_bars"] or i >= len(bt):
                    del pending[tk]
                    rejected["trigger never hit"] += 1
                    continue
                bar = bt.iloc[i]
                if bar["high"] >= o["trigger"]:
                    entry = o["trigger"]
                    risk_ps = entry - o["stop"]
                    qty = int(risk_budget / risk_ps)
                    qty = min(qty, int(equity * RISK["max_notional_per_trade_pct"] / entry))
                    room = equity * RISK["max_gross_notional_mult"] - sum(
                        p["entry"] * p["qty"] for p in open_pos.values())
                    qty = min(qty, int(max(room, 0) / entry))
                    qty = min(qty, int(bar["volume"] * RISK["max_pct_of_bar_volume"]))
                    margin = entry * qty * 0.20
                    if margin > cash:
                        qty = int((cash / 0.20) / entry)
                        margin = entry * qty * 0.20
                    # THE FRAGMENT KILLER - v1's missing guard
                    if qty < 1 or entry * qty < RISK["min_notional_per_trade"]:
                        rejected["cannot fund full size"] += 1
                        del pending[tk]
                        continue
                    cash -= margin
                    open_pos[tk] = {
                        "entry": entry, "qty": qty, "qty0": qty, "stop": o["stop"],
                        "t1": o["t1"], "t2": o["t2"], "risk": risk_ps, "atr": o["atr"],
                        "margin": margin, "bars": 0, "t1_done": False,
                    }
                    day_entries[day] += 1
                    del pending[tk]

            # ---------------- scan for new signals ----------------
            if i >= len(idx_today):
                continue
            ib = idx_today.iloc[i]
            iv = idx_vwap[idx_today.index[i]]
            if (iv - ib["close"]) / iv > params["index_vwap_tol"]:
                continue
            if (ib["close"] - idx_today["open"].iloc[0]) / idx_today["open"].iloc[0] < -params["max_index_drop"]:
                continue

            bar_t = idx_today.index[i].time()
            if bar_t < dtime(9, 45) or bar_t > dtime(13, 15):
                continue
            if day_entries[day] >= RISK["max_entries_per_day"]:
                continue
            if len(open_pos) + len(pending) >= RISK["max_concurrent_positions"]:
                continue

            cands = []
            for tk, bt in bars_today.items():
                if tk in open_pos or tk in pending or i >= len(bt) or i < 2:
                    continue
                bar = bt.iloc[i]
                ts = bt.index[i]
                vw = pre[tk]["vwap"].get(ts, np.nan)
                rs = pre[tk]["rsi"].get(ts, np.nan)
                at = pre[tk]["atr"].get(ts, np.nan)
                if not all(np.isfinite([vw, rs, at])) or at <= 0:
                    continue
                if rs > params["rsi_os"]:
                    continue
                dev = (vw - bar["close"]) / bar["close"]
                if dev < params["min_dev"]:
                    continue

                # sector cap
                sec = SECTOR.get(tk, "OTHER")
                if sum(1 for h in open_pos if SECTOR.get(h, "OTHER") == sec) >= RISK["max_positions_per_sector"]:
                    continue

                trigger = bar["high"] + TICK
                stop = min(trigger - params["stop_atr"] * at, bar["low"] - TICK)
                sp = (trigger - stop) / trigger
                if sp < params["stop_floor"]:
                    stop = trigger * (1 - params["stop_floor"])
                    sp = params["stop_floor"]
                if sp > params["stop_cap"]:
                    continue

                t1 = vw
                t2 = vw + params["t2_overshoot"] * (vw - trigger)
                blended = 0.5 * t1 + 0.5 * t2
                cps = trigger * VARIABLE_ROUNDTRIP_PCT
                rr = ((blended - trigger) - cps) / (trigger - stop)
                if rr < params["min_rr"]:
                    rejected["failed cost hurdle"] += 1
                    continue
                cands.append({"tk": tk, "trigger": trigger, "stop": stop,
                              "t1": t1, "t2": t2, "atr": at, "rr": rr, "age": 0})

            cands.sort(key=lambda c: c["rr"], reverse=True)
            slots = RISK["max_concurrent_positions"] - len(open_pos) - len(pending)
            for c in cands[:max(0, slots)]:
                pending[c["tk"]] = c

    return _stats(trades, capital, rejected, halted_days)


def _stats(trades, capital, rejected, halted):
    if not trades:
        return {"trades": 0, "note": "no trades"}
    pnl = [t["pnl"] for t in trades]
    w = [x for x in pnl if x > 0]
    l = [x for x in pnl if x <= 0]
    gp, gl = sum(w), abs(sum(l))
    eq, peak, dd = 0, 0, 0
    for x in pnl:
        eq += x
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    mu = np.mean(pnl)
    sd = np.std(pnl, ddof=1) if len(pnl) > 1 else 0
    return {
        "trades": len(trades),
        "win_rate": round(len(w) / len(trades) * 100, 1),
        "net_pnl": round(sum(pnl)),
        "profit_factor": round(gp / gl, 2) if gl else None,
        "avg_R": round(np.mean([t["R"] for t in trades]), 3),
        "expectancy": round(mu),
        "t_stat": round(mu / (sd / math.sqrt(len(pnl))), 2) if sd else None,
        "max_dd": round(dd),
        "max_dd_pct": round(abs(dd) / capital * 100, 2),
        "return_pct": round(sum(pnl) / capital * 100, 2),
        "by_reason": {k: [len(v), round(sum(v))] for k, v in
                      _group(trades).items()},
        "rejected": dict(rejected),
        "halted_days": len(halted),
    }


def _group(trades):
    g = defaultdict(list)
    for t in trades:
        g[t["reason"]].append(t["pnl"])
    return g


# Read from config.py. NEVER write these numbers twice.
#
# This dict used to hold its own copies, and by 2026-09-24 every one had
# drifted: it said rsi 30 / stop floor 0.8% / deviation 0.5% while the live
# system ran 40 / 0.6% / 0.796%. A backtest of a strategy that does not exist
# is worse than no backtest - it is v1's original sin in a different file.
#
# NOTE: this file still re-implements the signal LOGIC, which is its own drift
# risk. backtest_recent.py drives the real strategy class instead and should be
# preferred for anything but the walk-forward structure below.
from config import STRATEGY as _S, FILTERS as _F

BASE = {
    "rsi_os": _S["rsi_oversold"],
    "min_dev": _S["min_vwap_deviation"],
    "stop_atr": _S["stop_atr_mult"],
    "stop_floor": _S["stop_pct_floor"],
    "stop_cap": _S["stop_pct_cap"],
    "t2_overshoot": _S["t2_vwap_overshoot"],
    "min_rr": _S["min_reward_risk_after_cost"],
    "trail_atr": _S["trail_atr_mult_after_t1"],
    "time_stop_bars": _S["time_stop_bars"],
    "time_stop_min_R": _S["time_stop_min_R"],
    "trigger_valid_bars": _S["trigger_valid_bars"],
    "index_vwap_tol": _F["index_vwap_tolerance"],
    "max_index_drop": _F["max_index_daily_drop"],
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="pickle: {ticker: 15m OHLCV df}")
    ap.add_argument("--index", required=True, help="pickle: NIFTY 15m OHLCV df")
    ap.add_argument("--sweep", action="store_true")
    a = ap.parse_args()

    data = pickle.load(open(a.data, "rb"))
    index_df = pickle.load(open(a.index, "rb"))

    tr, va, te = (VALIDATION["train_period"], VALIDATION["validate_period"],
                  VALIDATION["test_period"])

    if not a.sweep:
        print("\n=== TRAIN ===");    print(json.dumps(run(data, index_df, BASE, *tr), indent=2))
        print("\n=== VALIDATE ==="); print(json.dumps(run(data, index_df, BASE, *va), indent=2))
        print("\n" + "=" * 62)
        print("TEST window deliberately NOT run. Touch it once, at the end,")
        print("after the config is frozen. Re-running it turns it into training data.")
        print("=" * 62)
        return

    # ------------------------------------------------------------------
    # Sweep, with the multiple-testing problem stated out loud.
    # v1's config comments quote a single best-of-sweep PF as if it were an
    # estimate of future performance. It is a maximum over N draws, which is
    # biased upward by roughly sd * sqrt(2*ln(N)) even when no edge exists.
    # ------------------------------------------------------------------
    grid = {"rsi_os": [26, 30, 34], "min_dev": [0.005, 0.007],
            "stop_atr": [1.0, 1.2, 1.6], "min_rr": [1.3, 1.5]}
    keys = list(grid)
    combos = list(itertools.product(*grid.values()))
    print(f"Testing {len(combos)} configurations on TRAIN, validating the best on VALIDATE.\n")

    rows = []
    for c in combos:
        p = {**BASE, **dict(zip(keys, c))}
        r = run(data, index_df, p, *tr)
        if r.get("trades", 0) >= 30:
            rows.append((dict(zip(keys, c)), r))

    rows.sort(key=lambda x: x[1]["net_pnl"], reverse=True)
    for cfg, r in rows[:5]:
        print(f"  {cfg} -> {r['trades']}tr PF {r['profit_factor']} net Rs{r['net_pnl']:,}")

    if rows:
        best_cfg, best_train = rows[0]
        med = np.median([r["net_pnl"] for _, r in rows])
        val = run(data, index_df, {**BASE, **best_cfg}, *va)
        print(f"\nBest on TRAIN: {best_cfg}")
        print(f"  train net Rs{best_train['net_pnl']:,} | median config Rs{med:,.0f}")
        print(f"  VALIDATE net Rs{val.get('net_pnl', 0):,} PF {val.get('profit_factor')} "
              f"t={val.get('t_stat')}")
        n = len(combos)
        haircut = math.sqrt(2 * math.log(n)) if n > 1 else 0
        print(f"\n  Selection bias: best-of-{n} inflates the estimate by roughly "
              f"{haircut:.2f} standard errors even with NO real edge.")
        print("  Go-live bar: positive on VALIDATE, after real costs, with t > 2,")
        print("  and at least "
              f"{VALIDATION['min_trades_for_significance']} trades. Anything less is a story.")


if __name__ == "__main__":
    main()
