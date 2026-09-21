"""
End-to-end pipeline test with synthetic bars - no network, no yfinance.

This exists because v1 was never tested. Its square-off bug, its loss-locking
breakeven trail and its fragment sizing were all reachable in ten lines of test
code, and none of them was ever exercised before real (paper) money met them.

Run:  python tests/test_pipeline.py
"""
import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tzutil import IST
from costs import cost_in_rupees, breakeven_win_rate, cost_as_fraction_of_risk
from config import SYSTEM, RISK, STRATEGY, FILTERS
from engine.paper_trader_v2 import PaperTraderV2
from engine.risk_manager import RiskManager
from strategies.vwap_mr_v2 import VWAPMeanReversionV2

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


# ----------------------------------------------------------------- synthetic
def session_index(day, n=25):
    """15-minute bar stamps for one NSE session, 09:15 to 15:15."""
    start = datetime.combine(day, datetime.min.time()).replace(hour=9, minute=15)
    return pd.DatetimeIndex([start + timedelta(minutes=15 * i) for i in range(n)])


def make_day(day, base=1000.0, path=None, vol=400_000, n=25):
    """One session of OHLCV. `path` is a list of closes; a flat drift if absent."""
    idx = session_index(day, n)
    if path is None:
        path = base + np.cumsum(np.random.normal(0, base * 0.0012, n))
    path = np.asarray(path, dtype=float)[:n]
    o = np.concatenate([[base], path[:-1]])
    h = np.maximum(o, path) * 1.0008
    l = np.minimum(o, path) * 0.9992
    return pd.DataFrame(
        {"open": o, "high": h, "low": l, "close": path,
         "volume": np.full(n, float(vol))}, index=idx)


def history(ticker_base, days, **kw):
    """Several quiet sessions, so RVOL and ATR have a baseline to compare against."""
    frames = []
    d = datetime(2026, 9, 1).date()
    for i in range(days):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        frames.append(make_day(d, base=ticker_base, **kw))
        d += timedelta(days=1)
    return pd.concat(frames), d


def dip_then_reverse(base, slide=0.0045, n=25):
    """
    A session that slides below its own VWAP on falling RSI, then turns up.
    This is the setup v2 is built to trade, and the shape v1 would have bought
    two bars too early, mid-fall. `slide` controls the depth per bar.
    """
    p = [base] * 3
    for i in range(9):                    # the slide
        p.append(p[-1] * (1 - slide))
    for i in range(13):                   # the reversion
        p.append(p[-1] * (1 + slide * 0.7))
    return p[:n]


# ======================================================================
print("\n" + "=" * 70)
print("1. COST MODEL - the arithmetic the whole rebuild rests on")
print("=" * 70)

c_tight = cost_in_rupees(2000 / 0.0055)
c_wide = cost_in_rupees(2000 / 0.015)
check("tighter stop costs more for identical rupee risk",
      c_tight > c_wide, f"0.55% -> Rs{c_tight:.0f} vs 1.5% -> Rs{c_wide:.0f}")
check("widening 0.55% -> 1.5% cuts friction by >50%",
      (c_tight - c_wide) / c_tight > 0.5, f"{(c_tight - c_wide) / c_tight * 100:.0f}% saved")
check("v1's stop needed >50% win rate to break even",
      breakeven_win_rate(2000, 0.0055, 1.4) > 0.50,
      f"{breakeven_win_rate(2000, 0.0055, 1.4) * 100:.1f}% vs its actual 32.7%")
check("v2's stop band needs materially less",
      breakeven_win_rate(2000, 0.015, 1.5) < 0.47,
      f"{breakeven_win_rate(2000, 0.015, 1.5) * 100:.1f}%")

print("\n" + "=" * 70)
print("2. RISK MANAGER - the gates v1 did not have")
print("=" * 70)

state = {"cash": 500_000, "initial_capital": 500_000, "equity_peak": 500_000,
         "positions": {}, "trade_history": [], "daily_entry_count": {},
         "pending_orders": {}, "last_known_price": {}, "total_pnl": 0.0}
rm = RiskManager(state)

qty, why = rm.size_position(1000.0, 985.0, bar_volume=1_000_000)
check("normal trade sizes", qty is not None, why)
notional = qty * 1000.0 if qty else 0
check("notional stays under the 2.0x gross cap",
      notional <= 500_000 * RISK["max_gross_notional_mult"], f"Rs{notional:,.0f}")

# the fragment v1 would have taken
qty_frag, why_frag = rm.size_position(12160.0, 12100.0, bar_volume=3)
check("fragment trade is REFUSED, not shrunk", qty_frag is None, why_frag)

# the stop so tight that friction swamps the risk
qty_tight, why_tight = rm.size_position(1000.0, 999.0, bar_volume=10_000_000)
check("absurdly tight stop is refused on cost grounds", qty_tight is None, why_tight)

now = datetime(2026, 9, 21, 10, 30, tzinfo=IST)
state["positions"] = {"HDFCBANK.NS": {"entry_price": 1700, "qty": 100}}
ok, why = rm.can_open("ICICIBANK.NS", now)
check("sector cap blocks a second bank long", not ok, why)
ok, why = rm.can_open("TCS.NS", now)
check("a different sector is allowed", ok)

state["positions"] = {}
# risk budget is 0.4% of Rs5L = Rs2,000, so the -2R daily limit is -Rs4,000
state["trade_history"] = [
    {"pnl": -1500, "exit_time": "2026-09-21 11:00:00 IST"},
    {"pnl": -1500, "exit_time": "2026-09-21 12:00:00 IST"},
]
halted, why = rm.trading_halted(now)
check("-1.5R day does NOT halt (limit is -2R)", not halted)
state["trade_history"].append({"pnl": -1500, "exit_time": "2026-09-21 13:00:00 IST"})
halted, why = rm.trading_halted(now)
check("daily loss limit halts trading past -2R", halted, why)

state["trade_history"] = [{"pnl": -300, "exit_time": "2026-08-01 11:00:00 IST"}] * 30
halted, why = rm.trading_halted(now)
check("rolling-PF kill switch fires", halted, why)

print("\n" + "=" * 70)
print("3. STRATEGY - regime gate, filters, trigger, cost hurdle")
print("=" * 70)

np.random.seed(11)
strat = VWAPMeanReversionV2()

hist_tcs, next_day = history(3200.0, 12, vol=500_000)
trade_day = next_day
while trade_day.weekday() >= 5:
    trade_day += timedelta(days=1)

# the signal bar is bar 11, deep in the slide and before 13:15
sig_bar = 12
tcs_today = make_day(trade_day, base=3200.0, path=dip_then_reverse(3200.0), vol=500_000)
tcs = pd.concat([hist_tcs, tcs_today.iloc[:sig_bar]])

hist_nifty, _ = history(24000.0, 12, vol=1_000_000)
nifty_up = pd.concat([hist_nifty,
                      make_day(trade_day, base=24000.0,
                               path=[24000 + 8 * i for i in range(25)],
                               vol=1_000_000).iloc[:sig_bar]])
nifty_down = pd.concat([hist_nifty,
                        make_day(trade_day, base=24000.0,
                                 path=[24000 * (1 - 0.0009 * i) for i in range(25)],
                                 vol=1_000_000).iloc[:sig_bar]])

import strategies.vwap_mr_v2 as mod
mod.now_ist = lambda: datetime.combine(trade_day, datetime.min.time()).replace(
    hour=12, minute=0, tzinfo=IST)

ok, why = strat.regime_ok(nifty_up, 14.0, trade_day)
check("regime gate OPEN when NIFTY holds its VWAP", ok, why)
ok, why = strat.regime_ok(nifty_down, 14.0, trade_day)
check("regime gate CLOSED when NIFTY slides", not ok, why)
ok, why = strat.regime_ok(nifty_up, 31.0, trade_day)
check("regime gate CLOSED when VIX is elevated", not ok, why)
ok, why = strat.regime_ok(None, 14.0, trade_day)
check("regime gate FAILS CLOSED with no index data (v1 carried on)", not ok, why)

sigs = strat.compute_signals({"TCS.NS": tcs}, nifty_up, 14.0)
check("a qualifying dip produces a signal", len(sigs) == 1,
      f"{len(sigs)} signal(s)")

# A shallow dip is oversold and below VWAP but cannot pay for itself. v1 would
# have taken it at 0.6% deviation; v2's cost hurdle must refuse it.
tcs_shallow = pd.concat([hist_tcs,
                         make_day(trade_day, base=3200.0,
                                  path=dip_then_reverse(3200.0, slide=0.0015),
                                  vol=500_000).iloc[:sig_bar]])
check("shallow dip REJECTED - cannot clear its own costs",
      len(strat.compute_signals({"TCS.NS": tcs_shallow}, nifty_up, 14.0)) == 0,
      f"deviation floor is {STRATEGY['min_vwap_deviation'] * 100:.2f}%, derived from the hurdle")

if sigs:
    s = sigs[0]
    last_bar = tcs.iloc[-1]
    check("entry is a TRIGGER above the bar high, not the falling close",
          s["trigger_price"] > last_bar["high"] >= last_bar["close"],
          f"trigger Rs{s['trigger_price']} vs bar close Rs{last_bar['close']:.2f}")
    check("stop is ATR-scaled inside the configured band",
          STRATEGY["stop_pct_floor"] * 100 <= s["stop_pct"] <= STRATEGY["stop_pct_cap"] * 100,
          f"{s['stop_pct']}%")
    check("stop is wider than v1's flat 0.55%", s["stop_pct"] > 0.55, f"{s['stop_pct']}%")
    check("cost hurdle cleared", s["rr_after_cost"] >= STRATEGY["min_reward_risk_after_cost"],
          f"R:R after cost {s['rr_after_cost']}")

# the same setup, but blocked because the index is falling
check("no signals while the regime gate is shut",
      len(strat.compute_signals({"TCS.NS": tcs}, nifty_down, 14.0)) == 0)

# news-driven dip: same shape, 4x the volume
tcs_news = pd.concat([hist_tcs,
                      make_day(trade_day, base=3200.0, path=dip_then_reverse(3200.0),
                               vol=2_000_000).iloc[:sig_bar]])
check("high-RVOL dip is NOT faded (do not trade against news)",
      len(strat.compute_signals({"TCS.NS": tcs_news}, nifty_up, 14.0)) == 0)

# after the entry window
mod.now_ist = lambda: datetime.combine(trade_day, datetime.min.time()).replace(
    hour=14, minute=30, tzinfo=IST)
tcs_late = pd.concat([hist_tcs, tcs_today.iloc[:22]])
check("no entries after the 13:15 cutoff",
      len(strat.compute_signals({"TCS.NS": tcs_late}, nifty_up, 14.0)) == 0)

print("\n" + "=" * 70)
print("4. PAPER TRADER - the four v1 bugs, each tested directly")
print("=" * 70)

tmp = Path(tempfile.mkdtemp())
import engine.paper_trader_v2 as pt


def fresh_trader():
    f = tmp / f"state_{np.random.randint(1e9)}.json"
    return PaperTraderV2(state_file=str(f))


# ---- BUG 1: square-off must not depend on data being present ----
t = fresh_trader()
morning = datetime(2026, 9, 21, 11, 0, tzinfo=IST)
pt.now_ist = lambda: morning
order = {"ticker": "TCS.NS", "trigger_price": 3150.0, "limit_price": 3156.0,
         "stop_loss": 3100.0, "t1": 3220.0, "t2": 3255.0, "t1_fraction": 0.5,
         "atr": 25.0, "bar_volume": 900_000, "valid_bars": 2,
         "strategy": "test", "stop_pct": 1.59, "rr_after_cost": 1.8}
t.place_order(order, morning)
t.process_orders({"TCS.NS": {"close": 3152.0, "high": 3160.0, "low": 3145.0}}, morning)
check("stop-limit order fills when price reclaims the trigger",
      "TCS.NS" in t.state["positions"])
t.update_prices({"TCS.NS": {"close": 3152.0, "high": 3160.0, "low": 3145.0}}, morning)

close_time = datetime(2026, 9, 21, 15, 5, tzinfo=IST)
pt.now_ist = lambda: close_time
exits = t.check_exits({}, close_time)          # <-- EMPTY data, the v1 killer
check("square-off fires with NO market data at all (v1 held overnight)",
      len(exits) == 1 and exits[0]["reason"] == "square_off",
      f"{[e['reason'] for e in exits]}")
check("no position survives the close", len(t.state["positions"]) == 0)

# ---- BUG 2: the breakeven trail must clear friction ----
t = fresh_trader()
pt.now_ist = lambda: morning
t.place_order(order, morning)
t.process_orders({"TCS.NS": {"close": 3152.0, "high": 3160.0, "low": 3145.0}}, morning)
pos = t.state["positions"]["TCS.NS"]
entry, R = pos["entry_price"], pos["risk_per_share"]
t.check_exits({"TCS.NS": {"close": 3225.0, "high": 3230.0, "low": 3210.0}}, morning)
pos = t.state["positions"].get("TCS.NS")
if pos:
    fric_R = t._friction_R(pos)
    lifted = (pos["stop_loss"] - entry) / R
    check("T1 banks half at VWAP", pos["qty_open"] < pos["qty"],
          f"{pos['qty_open']}/{pos['qty']} left")
    check("trail clears friction (v1's entry+0.1R locked in a loss)",
          lifted > fric_R, f"stop at +{lifted:.2f}R vs friction {fric_R:.2f}R")

# ---- BUG 3: the time stop ----
t = fresh_trader()
pt.now_ist = lambda: morning
t.place_order(order, morning)
t.process_orders({"TCS.NS": {"close": 3152.0, "high": 3160.0, "low": 3145.0}}, morning)
flat = {"TCS.NS": {"close": 3151.0, "high": 3154.0, "low": 3148.0}}
reasons = []
for _ in range(5):
    reasons += [e["reason"] for e in t.check_exits(flat, morning)]
check("time stop closes a trade that never reverts",
      any(r.startswith("time_stop") for r in reasons), f"{reasons}")

# ---- BUG 4: win rate counts CLOSED trades ----
t = fresh_trader()
pt.now_ist = lambda: morning
t.place_order(order, morning)
t.process_orders({"TCS.NS": {"close": 3152.0, "high": 3160.0, "low": 3145.0}}, morning)
s = t.summary()
check("an open position does not dilute the win rate",
      s["closed_trades"] == 0 and s["open_positions"] == 1,
      f"closed={s['closed_trades']} open={s['open_positions']}")

# ---- stop loss fills and is charged costs ----
t = fresh_trader()
pt.now_ist = lambda: morning
t.place_order(order, morning)
t.process_orders({"TCS.NS": {"close": 3152.0, "high": 3160.0, "low": 3145.0}}, morning)
ex = t.check_exits({"TCS.NS": {"close": 3095.0, "high": 3140.0, "low": 3090.0}}, morning)
check("stop loss triggers", len(ex) == 1 and ex[0]["reason"] == "stop_loss")
if ex:
    check("friction is recorded on every trade", ex[0]["friction"] > 0,
          f"Rs{ex[0]['friction']:,.0f} on a Rs{abs(ex[0]['pnl']):,.0f} loss")
    check("loss is capped near 1R", -1.6 < ex[0]["R_multiple"] < -0.8,
          f"{ex[0]['R_multiple']}R")

# ---- order expiry ----
t = fresh_trader()
pt.now_ist = lambda: morning
t.place_order(order, morning)
for _ in range(4):
    t.process_orders({"TCS.NS": {"close": 3100.0, "high": 3110.0, "low": 3090.0}}, morning)
check("unfilled trigger expires instead of chasing (v1 always 'filled')",
      len(t.state["pending_orders"]) == 0 and len(t.state["positions"]) == 0)

print("\n" + "=" * 70)
print("5. FULL SESSION - main.py wiring, 25 bars, frozen clock")
print("=" * 70)

import main as bot

np.random.seed(5)
universe_data = {}
for tk, base in [("TCS.NS", 3200.0), ("INFY.NS", 1500.0), ("RELIANCE.NS", 2800.0)]:
    h, _ = history(base, 12, vol=600_000)
    full = pd.concat([h, make_day(trade_day, base=base,
                                  path=dip_then_reverse(base), vol=600_000)])
    universe_data[tk] = full
nifty_full = pd.concat([hist_nifty, make_day(trade_day, base=24000.0,
                                             path=[24000 + 8 * i for i in range(25)],
                                             vol=1_000_000)])
vix_full = pd.concat([history(14.0, 12, vol=1000)[0],
                      make_day(trade_day, base=14.0, path=[14.0] * 25, vol=1000)])

state_file = tmp / "session.json"
bars_seen, events = 0, []
for bar_i in range(4, 25):
    ts = session_index(trade_day)[bar_i]
    clock = ts.to_pydatetime().replace(tzinfo=IST)

    sliced = {tk: df[df.index <= ts] for tk, df in universe_data.items()}
    sliced["^NSEI"] = nifty_full[nifty_full.index <= ts]
    sliced["^INDIAVIX"] = vix_full[vix_full.index <= ts]

    class StubFetcher:
        def fetch_intraday(self, tickers, days_back=5):
            return {t: sliced[t].copy() for t in tickers if t in sliced}

    bot.IntradayFetcher = StubFetcher
    bot.now_ist = lambda c=clock: c
    bot.INTRADAY_UNIVERSE = ["TCS.NS", "INFY.NS", "RELIANCE.NS"]
    pt.now_ist = lambda c=clock: c
    mod.now_ist = lambda c=clock: c
    bot.PaperTraderV2 = lambda: PaperTraderV2(state_file=str(state_file))
    try:
        bot.main()
        bars_seen += 1
    except Exception as e:
        events.append(f"bar {bar_i} ({ts:%H:%M}) raised {type(e).__name__}: {e}")

check("all 21 scans ran without raising", not events, "; ".join(events[:2]))
check("every scan completed", bars_seen == 21, f"{bars_seen}/21")

final = json.load(open(state_file))
check("state file is written and readable", "cash" in final)
check("no position left open after 15:05", len(final["positions"]) == 0,
      f"{list(final['positions'])}")
check("no orders left resting", len(final["pending_orders"]) == 0)

h = final["trade_history"]
print(f"\n  Session result: {len(h)} fill(s), net Rs{sum(t['pnl'] for t in h):+,.0f}, "
      f"friction Rs{sum(t.get('friction', 0) for t in h):,.0f}")
for t_ in h:
    print(f"    {t_['ticker']:<13} {t_['qty']:>4} @ Rs{t_['exit_price']:>8.2f}  "
          f"Rs{t_['pnl']:>8,.0f} ({t_['R_multiple']:+.2f}R)  {t_['reason']}")
if h:
    noms = [t_["entry_price"] * t_["qty"] for t_ in h]
    check("no fragment trades taken",
          all(n >= RISK["min_notional_per_trade"] * 0.4 for n in noms),
          f"min notional Rs{min(noms):,.0f}")
    check("entry timestamps are IST-labelled (v1 logged UTC for a month)",
          all("IST" in t_["entry_time"] for t_ in h))

shutil.rmtree(tmp, ignore_errors=True)

print("\n" + "=" * 70)
print(f"{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print(f"  FAILED: {f}")
print("=" * 70)
sys.exit(1 if FAIL else 0)
