"""
End-to-end pipeline test with synthetic bars - no network, no yfinance.

This exists because v1 was never tested. Its square-off bug, its loss-locking
breakeven trail and its fragment sizing were all reachable in ten lines of test
code, and none of them was ever exercised before real (paper) money met them.

Run:  python tests/test_pipeline.py
"""
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Send the harness's own logs somewhere disposable. Previously this wrote a
# realistic-looking logs/intraday_v2.log full of synthetic trades straight into
# the repo, which reads exactly like a live run until you notice the dates.
_LOGTMP = tempfile.mkdtemp(prefix="algo_test_logs_")
os.environ["ALGO_LOG_DIR"] = _LOGTMP

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


def rally(base, step=0.0035, n=25):
    """
    A session that trends up and away from its own VWAP on rising RSI - the
    shape momentum v3 is built to trade, and the exact shape v2 was built to
    stand aside from. Cumulative VWAP lags a trending price, so a steady climb
    puts the close further above VWAP with every bar.
    """
    p = [base]
    for _ in range(n - 1):
        p.append(p[-1] * (1 + step))
    return p[:n]


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
# v1 entered with a market order at the bar close, so symmetric 5bps slippage
# was right FOR v1. Pricing its history with v2's limit-order cost would be
# revisionist - it would credit v1 with an efficiency it never had.
_V1_COST = 0.001355
_v1_fric = 2000 / 0.0055 * _V1_COST + 47
_v1_be = (2000 + _v1_fric) / ((1.4 * 2000 - _v1_fric) + (2000 + _v1_fric))
check("v1's stop needed >50% win rate to break even",
      _v1_be > 0.50, f"{_v1_be * 100:.1f}% vs its actual 32.7%")
check("v2's stop band needs materially less",
      breakeven_win_rate(2000, 0.015, 1.5) < 0.47,
      f"{breakeven_win_rate(2000, 0.015, 1.5) * 100:.1f}%")

print("\n" + "=" * 70)
print("1b. DERIVED CONFIG - settings that must agree, cannot drift apart")
print("=" * 70)
# Three numbers are now computed from capital and the cost hurdle rather than
# typed in. Each one was a bug waiting to happen as a literal: a fixed Rs2cr
# turnover floor was already too low at Rs5L capital, and a fixed Rs60,000
# notional floor silently retires the moment capital doubles.
import config as _cfgmod
from config import MOMENTUM, ACTIVE_STRATEGY
from learning import MIN_RISK_PCT
_floor = MOMENTUM["stop_pct_floor"] if ACTIVE_STRATEGY == "momentum_v3" else STRATEGY["stop_pct_floor"]
_target = SYSTEM["initial_capital"] * RISK["risk_pct_per_trade"] / _floor

# The turnover floor asks whether a name can absorb a full-size position at the
# ACTIVE strategy's floor stop. It is about slippage honesty, so it uses full
# base risk, not the tapered minimum.
_min_pos = _target * RISK["min_notional_fraction_of_target"]
_cap = FILTERS["min_median_15m_turnover"] * RISK["max_pct_of_bar_volume"]
check("the smallest full-size position fits inside the bar-volume cap",
      _min_pos <= _cap + 1, f"min position Rs{_min_pos:,.0f} vs cap Rs{_cap:,.0f}")
check("turnover floor is derived from the ACTIVE strategy's stop, not v2's",
      abs(FILTERS["min_median_15m_turnover"]
          - _min_pos / RISK["max_pct_of_bar_volume"]) < 1,
      f"Rs{FILTERS['min_median_15m_turnover'] / 1e7:.2f}cr per 15-min bar")

# THE BUG THE FULL REPLAY FOUND. The notional floor was derived from v2's 0.6%
# stop; v3 stops at 1.2%+ so its positions are half the size, and adaptive
# sizing tapers risk toward 0.20% in a drawdown. Against the old floor, 12 of
# 29 v3 orders were refused as "fragments". The floor must sit below the
# smallest LEGITIMATE v3 position - a tapered trade at the widest normal stop.
_smallest_legit = SYSTEM["initial_capital"] * MIN_RISK_PCT / MOMENTUM["stop_pct_cap"]
check("a drawdown-tapered v3 trade at the widest stop still clears the notional floor",
      _smallest_legit >= RISK["min_notional_per_trade"],
      f"smallest legitimate Rs{_smallest_legit:,.0f} vs floor Rs{RISK['min_notional_per_trade']:,.0f}")
check("a true fragment (qty=1 of a Rs500 stock) is still refused",
      500 < RISK["min_notional_per_trade"])
check("minimum notional is derived from the tapered minimum risk, not a literal",
      abs(RISK["min_notional_per_trade"]
          - SYSTEM["initial_capital"] * MIN_RISK_PCT / _floor
            * RISK["min_notional_fraction_of_target"]) < 1,
      f"Rs{RISK['min_notional_per_trade']:,.0f}")
# Must read the live cost, not a copy of it. A hardcoded 0.001355 here would
# have silently passed while the real cost moved underneath it.
from costs import VARIABLE_ROUNDTRIP_PCT as _C
_lhs = STRATEGY["min_vwap_deviation"] * (1 + STRATEGY["t2_vwap_overshoot"] / 2) - _C
_rhs = STRATEGY["min_reward_risk_after_cost"] * STRATEGY["stop_pct_floor"]
check("deviation floor still clears the cost hurdle",
      _lhs >= _rhs - 1e-5,   # the floor is rounded to 5dp, so allow that much
      f"{STRATEGY['min_vwap_deviation'] * 100:.3f}% -> {_lhs:.6f} vs {_rhs:.6f}")

print("\n" + "=" * 70)
print("2. RISK MANAGER - the gates v1 did not have")
print("=" * 70)

# Use the CONFIGURED capital. A fixture pinned to Rs5L while config said Rs10L
# made min_notional (derived from config) unreachable, and the test failed for
# a reason that had nothing to do with the code under test.
_CAP = SYSTEM["initial_capital"]
state = {"cash": _CAP, "initial_capital": _CAP, "equity_peak": _CAP,
         "positions": {}, "trade_history": [], "daily_entry_count": {},
         "pending_orders": {}, "last_known_price": {}, "total_pnl": 0.0}
rm = RiskManager(state)

qty, why = rm.size_position(1000.0, 985.0, bar_volume=1_000_000)
check("normal trade sizes", qty is not None, why)
notional = qty * 1000.0 if qty else 0
check("notional stays under the 2.0x gross cap",
      notional <= _CAP * RISK["max_gross_notional_mult"], f"Rs{notional:,.0f}")

# the fragment v1 would have taken
qty_frag, why_frag = rm.size_position(12160.0, 12100.0, bar_volume=3)
check("fragment trade is REFUSED, not shrunk", qty_frag is None, why_frag)

# the stop so tight that friction swamps the risk
# A 0.1% stop forces so much notional that the caps shrink the position until
# friction exceeds the risk left in it. The guard must measure friction against
# what is ACTUALLY at risk, not against the budget it started from.
qty_tight, why_tight = rm.size_position(1000.0, 999.0, bar_volume=10_000_000)
check("absurdly tight stop is refused on cost grounds", qty_tight is None, why_tight)

now = datetime(2026, 9, 21, 10, 30, tzinfo=IST)
state["positions"] = {"HDFCBANK.NS": {"entry_price": 1700, "qty": 100}}
ok, why = rm.can_open("ICICIBANK.NS", now)
check("sector cap blocks a second bank long", not ok, why)
ok, why = rm.can_open("TCS.NS", now)
check("a different sector is allowed", ok)

state["positions"] = {}
# Read the limit from config rather than hard-coding it - this test failed once
# when the cap moved from -2R to -3R, which is the test drifting from the system
# rather than catching a bug.
R = _CAP * RISK["risk_pct_per_trade"]
limit = RISK["daily_loss_limit_R"]
just_under = -(limit - 0.5) * R
state["trade_history"] = [
    {"pnl": just_under, "exit_time": "2026-09-21 11:00:00 IST"}]
halted, why = rm.trading_halted(now)
check(f"a {abs(just_under)/R:.1f}R day does NOT halt (limit is -{limit}R)", not halted)
state["trade_history"].append(
    {"pnl": -1.0 * R, "exit_time": "2026-09-21 13:00:00 IST"})
halted, why = rm.trading_halted(now)
check(f"daily loss limit halts trading past -{limit}R", halted, why)

state["trade_history"] = [{"pnl": -300, "exit_time": "2026-08-01 11:00:00 IST"}] * 30
import engine.risk_manager as _rmmod
_mode0 = _rmmod.SYSTEM.get("mode")
_rmmod.SYSTEM["mode"] = "live"
halted, why = rm.trading_halted(now)
check("rolling-PF kill switch HALTS in live mode", halted, why)
_rmmod.SYSTEM["mode"] = "paper"
state.pop("killswitch_trips", None)
halted, why = rm.trading_halted(now)
check("in PAPER mode the kill switch records the trip and keeps measuring",
      not halted and state.get("killswitch_trips"),
      f"trips={state.get('killswitch_trips')}")
halted2, _ = rm.trading_halted(now)
check("the trip is recorded once per day, not once per scan",
      len(state.get("killswitch_trips", [])) == 1)
_rmmod.SYSTEM["mode"] = _mode0

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
print("3a. REACHABILITY - can the strategy fire on a realistic dislocation?")
print("=" * 70)
# THE TEST THAT WAS MISSING. For three days live the suite passed 82 checks
# while the strategy was structurally incapable of trading: the derived
# deviation floor (1.218%) sat above the largest dislocation the market
# produced (1.20%). Every filter was individually defensible and the product
# of them was zero.
#
# A threshold that no realistic market move can clear is a bug, and nothing
# here would have said so. These assertions fail if that recurs.
_ATR_PCT = 0.0035          # ~0.35% per 15-min bar, typical NSE large cap
_realistic_dip = 0.010     # a 1.0% intraday dislocation from VWAP - common
check("the deviation floor is reachable by a realistic 1.0% dislocation",
      STRATEGY["min_vwap_deviation"] <= _realistic_dip,
      f"floor {STRATEGY['min_vwap_deviation']*100:.3f}% vs a 1.0% move")
check("the stop floor is within a normal bar's ATR range",
      STRATEGY["stop_pct_floor"] <= 3 * _ATR_PCT,
      f"floor {STRATEGY['stop_pct_floor']*100:.2f}% vs 3xATR {3*_ATR_PCT*100:.2f}%")

# And end to end: a synthetic dip of realistic depth MUST produce a signal.
np.random.seed(21)
_h, _d = history(2000.0, 12, vol=800_000)
_td = _d
while _td.weekday() >= 5:
    _td += timedelta(days=1)
_mod_now = datetime.combine(_td, datetime.min.time()).replace(hour=12, tzinfo=IST)
import strategies.vwap_mr_v2 as _m
_m.now_ist = lambda: _mod_now
_stock = pd.concat([_h, make_day(_td, base=2000.0,
                                 path=dip_then_reverse(2000.0, slide=0.0028),
                                 vol=800_000).iloc[:12]])
_hn, _ = history(24000.0, 12, vol=0)
_nif = pd.concat([_hn, make_day(_td, base=24000.0,
                                path=[24000 + 5 * i for i in range(25)],
                                vol=0).iloc[:12]])
_sigs = VWAPMeanReversionV2().compute_signals({"TCS.NS": _stock}, _nif, 14.0)
check("a realistic dip end-to-end produces a tradeable signal",
      len(_sigs) == 1,
      f"{len(_sigs)} signal(s) - if 0, the filter stack is again unreachable")

print("\n" + "=" * 70)
print("3b. ZERO-VOLUME INDEX - the bug that made the regime gate a no-op")
print("=" * 70)
# Yahoo returns volume=0 for ^NSEI. The naive VWAP divided by zero, produced
# NaN, and every gate comparison silently evaluated False - so the gate passed
# everything instead of standing down. Caught by check_data.py in production,
# not by this suite, because every synthetic index here had fake volume.
from data.fetcher import TechnicalIndicators as _TI
_ti = _TI()

idx_zero = make_day(trade_day, base=24000.0,
                    path=[24000 + 8 * i for i in range(25)], vol=0)
vw_zero = _ti.vwap(idx_zero)
check("VWAP is finite even with zero volume throughout",
      bool(np.isfinite(vw_zero.iloc[-1])), f"vwap={vw_zero.iloc[-1]:,.2f}")
check("zero-volume VWAP equals the unweighted mean of typical price",
      abs(vw_zero.iloc[-1] -
          ((idx_zero['high'] + idx_zero['low'] + idx_zero['close']) / 3).mean()) < 0.01)

# and it must still match real VWAP when volume IS present
idx_vol = make_day(trade_day, base=24000.0,
                   path=[24000 + 8 * i for i in range(25)], vol=1_000_000)
vw_flat = _ti.vwap(idx_vol)
check("flat volume gives the same answer as no volume",
      abs(vw_flat.iloc[-1] - vw_zero.iloc[-1]) < 0.01)

# the gate itself, on a genuinely zero-volume index
hist_zero, _ = history(24000.0, 12, vol=0)
nifty_zero = pd.concat([hist_zero, idx_zero.iloc[:sig_bar]])
ok_z, why_z = strat.regime_ok(nifty_zero, 14.0, trade_day)
check("regime gate evaluates on a zero-volume index", ok_z, why_z)

# a corrupt index must FAIL CLOSED, never pass by NaN comparison
bad = idx_zero.copy()
bad.loc[:, ["open", "high", "low", "close"]] = np.nan
nifty_bad = pd.concat([hist_zero, bad.iloc[:sig_bar]])
ok_b, why_b = strat.regime_ok(nifty_bad, 14.0, trade_day)
check("non-finite index values FAIL CLOSED, not open", not ok_b, why_b)

print("\n" + "=" * 70)
print("3c. MOMENTUM v3 - the strategy that replaced reversion")
print("=" * 70)
# v2 shipped with a filter stack that could not fire, and 82 passing checks
# said nothing about it. Every assertion here exists so that cannot happen
# again to v3: the thresholds must be REACHABLE, the direction must be the one
# the measurement supports, and the exits that lost money in every sweep cell
# must stay off.
from config import MOMENTUM
from strategies.momentum_v3 import MomentumV3
import strategies.momentum_v3 as _m3

# --- the direction itself. This is the whole point of v3 existing. ---
check("v3 buys ABOVE vwap where v2 bought below",
      MOMENTUM["min_dev_above_vwap"] > 0 and STRATEGY["min_vwap_deviation"] > 0,
      f"v3 needs +{MOMENTUM['min_dev_above_vwap']*100:.1f}% above, "
      f"v2 needed {STRATEGY['min_vwap_deviation']*100:.2f}% below")
check("v3 requires HIGH rsi, v2 required low",
      MOMENTUM["min_rsi"] > STRATEGY["rsi_oversold"],
      f"{MOMENTUM['min_rsi']} vs {STRATEGY['rsi_oversold']}")
check("v3 requires HIGH volume, v2 capped it",
      MOMENTUM["min_rvol"] > FILTERS["rvol_max"],
      f"v3 floor {MOMENTUM['min_rvol']} vs v2 ceiling {FILTERS['rvol_max']}")

# --- the exits that lost money in every sweep cell they appeared in ---
check("v3 has NO breakeven trail (lost in all 50 sweep cells with it on)",
      MOMENTUM["use_breakeven_trail"] is False)
check("v3 has NO time stop (the edge is the right tail)",
      MOMENTUM["use_time_stop"] is False)
check("v3 has NO profit target",
      MOMENTUM["use_target"] is False)

# --- reachability: a threshold no real move clears is a bug ---
# Median ATR on the measured sample was 0.619%, median adverse excursion
# before the close -0.76%. A stop floor inside that noise band is stopped out
# by nothing in particular; one far outside it cannot be sized.
check("v3 stop floor sits outside ordinary noise but is still sizeable",
      0.008 <= MOMENTUM["stop_pct_floor"] <= 0.020,
      f"{MOMENTUM['stop_pct_floor']*100:.1f}%")
check("v3 stop floor is wider than v2's (smaller position, less friction)",
      MOMENTUM["stop_pct_floor"] > STRATEGY["stop_pct_floor"],
      f"{MOMENTUM['stop_pct_floor']*100:.1f}% vs {STRATEGY['stop_pct_floor']*100:.1f}%")

# --- end to end: a trending name on a volume spike MUST produce a signal ---
np.random.seed(33)


def momentum_day(day, base, bars=10, step=0.005, vol=500_000, spike=8.0,
                 bar_range=0.006):
    """
    A realistic trending session for v3.

    make_day() draws bars only ~0.16% wide, which is a tenth of what NSE
    15-minute bars actually do - the measured median ATR on the real sample was
    0.619%. A synthetic day that narrow drags ATR under v3's volatility floor
    and the strategy correctly refuses to trade it, which would make this test
    fail for a reason that has nothing to do with the strategy. `bar_range`
    sets a realistic high-low span; `spike` lifts volume on the signal bar.
    """
    idx = session_index(day, bars)
    p = np.array([base * (1 + step) ** i for i in range(bars)])
    o = np.concatenate([[base], p[:-1]])
    h = np.maximum(o, p) * (1 + bar_range / 2)
    l = np.minimum(o, p) * (1 - bar_range / 2)
    v = np.full(bars, float(vol))
    v[-1] = vol * spike                       # the participation spike
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": p,
                         "volume": v}, index=idx)


_h3, _d3 = history(1800.0, 12, vol=500_000)
_td3 = _d3
while _td3.weekday() >= 5:
    _td3 += timedelta(days=1)
_now3 = datetime.combine(_td3, datetime.min.time()).replace(hour=11, tzinfo=IST)
_m3.now_ist = lambda: _now3
_stock3 = pd.concat([_h3, momentum_day(_td3, 1800.0)])
_hn3, _ = history(24000.0, 12, vol=0)
_nif3 = pd.concat([_hn3, make_day(_td3, base=24000.0,
                                  path=[24000 + 6 * i for i in range(25)],
                                  vol=0).iloc[:9]])
_s3 = MomentumV3().compute_signals({"TCS.NS": _stock3}, _nif3, 14.0)
check("a trending name on a volume spike produces a signal",
      len(_s3) == 1,
      f"{len(_s3)} signal(s) - if 0, v3's filter stack is unreachable")
if _s3:
    _sig3 = _s3[0]
    check("the signal is a MARKET order, not v2's stop-limit trigger",
          _sig3["order_type"] == "MARKET", _sig3["order_type"])
    check("the signal carries its own exit rules",
          _sig3["exit_cfg"]["use_breakeven_trail"] is False)
    check("the entry snapshot records what the rule looked at",
          all(k in _sig3 for k in
              ("entry_rsi", "entry_rvol", "entry_deviation_pct", "entry_atr_pct")))

# --- the same name WITHOUT the volume spike must be rejected ---
_quiet = pd.concat([_h3, momentum_day(_td3, 1800.0, spike=1.0)])
_sq = MomentumV3().compute_signals({"TCS.NS": _quiet}, _nif3, 14.0)
check("the same rally on ORDINARY volume is rejected", len(_sq) == 0,
      f"{len(_sq)} signal(s); rvol is meant to be binding")

# --- the regime gate must fail closed, same as v2's ---
_okr, _whyr = MomentumV3().regime_ok(None, 14.0, _td3)
check("v3 regime gate fails closed with no index data", not _okr, _whyr)
_crash = pd.concat([_hn3, make_day(_td3, base=24000.0,
                                   path=[24000 * (1 - 0.0035 * i) for i in range(25)],
                                   vol=0).iloc[:9]])
_okc, _whyc = MomentumV3().regime_ok(_crash, 14.0, _td3)
check("v3 stands down when the index is falling hard", not _okc, _whyc)

# --- a MARKET order fills at the NEXT bar's open, never at its close ---
_tr3 = PaperTraderV2(state_file=str(tmp / "mkt.json"))
_tr3.state["cash"] = _tr3.state["initial_capital"] = 1_000_000
_mkt_sig = {"ticker": "ZZZ.NS", "order_type": "MARKET", "reference_price": 1000.0,
            "stop_pct": 1.2, "stop_loss": 988.0, "valid_bars": 1, "atr": 6.0,
            "strategy": "MOM-v3", "bar_volume": 5_000_000,
            "exit_cfg": {"use_target": False, "use_breakeven_trail": False,
                         "use_time_stop": False, "thesis_exits": True}}
_clock3 = datetime.combine(_td3, datetime.min.time()).replace(hour=11, tzinfo=IST)
pt.now_ist = lambda c=_clock3: c
_tr3.place_order(_mkt_sig, _clock3)
_filled = _tr3.process_orders(
    {"ZZZ.NS": {"open": 1010.0, "close": 1040.0, "high": 1045.0,
                "low": 1005.0, "volume": 5_000_000}}, _clock3)
check("a MARKET order fills at the bar's OPEN, not its close",
      _filled and abs(_filled[0]["entry_price"] - 1010.0) < 0.01,
      f"filled at {_filled[0]['entry_price'] if _filled else 'nothing'}")
if _filled:
    _p3 = _tr3.state["positions"]["ZZZ.NS"]
    check("the stop is re-derived from the FILL, not the stale reference",
          abs(_p3["stop_loss"] - 1010.0 * 0.988) < 0.05,
          f"stop Rs{_p3['stop_loss']} on a Rs1010 fill (signal said Rs988)")
    check("risk per share reflects the real fill",
          abs(_p3["risk_per_share"] - 1010.0 * 0.012) < 0.1,
          f"Rs{_p3['risk_per_share']}")

print("\n" + "=" * 70)
print("3e. IN-TRADE EXITS - the one rule that measured positive, and the guards")
print("=" * 70)
# study_exits.py: nine exit rules on the same 261 entries. Trailing stops lost
# money at every multiple (-258k at 1.5xATR). Exit-on-VWAP-reclaim was the
# only rule that raised both the net and the tail-independent total. These
# assertions pin that outcome into the code so it cannot drift back.
from engine.exit_manager import ExitManager

check("no trailing-stop switch exists in MOMENTUM to be flipped on",
      not any("trail" in k for k in MOMENTUM if MOMENTUM[k]),
      f"{[k for k in MOMENTUM if 'trail' in k]}")
check("VWAP-reclaim exit is on", MOMENTUM["exit_on_vwap_reclaim"] is True)
check("RSI and index exits are off (measured as noise)",
      not MOMENTUM["exit_on_rsi_below"] and not MOMENTUM["exit_on_index_drop"])

_em = ExitManager()
_v3pos = {"ticker": "AAA.NS", "entry_price": 1000.0, "risk_per_share": 12.0,
          "stop_loss": 988.0, "bars_held": 3, "strategy": "MOM-v3",
          "exit_cfg": {"thesis_exits": True}}
_below = {"open": 1005, "high": 1008, "low": 996, "close": 997, "volume": 1e6}
_above = {"open": 1005, "high": 1012, "low": 1003, "close": 1010, "volume": 1e6}

_v = _em.evaluate(_v3pos, _below, {"vwap": 1002.0, "rsi": 60}, None)
check("a v3 position that closes below VWAP is exited",
      _v is not None and _v[0] == "vwap_reclaim", f"{_v}")
_v = _em.evaluate(_v3pos, _above, {"vwap": 1002.0, "rsi": 60}, None)
check("a v3 position still above VWAP is left alone", _v is None, f"{_v}")

_fresh = dict(_v3pos, bars_held=0)
_v = _em.evaluate(_fresh, _below, {"vwap": 1002.0}, None)
check("the entry bar itself never triggers the VWAP exit", _v is None,
      "a market fill can land a tick under a moving VWAP; give it one bar")

_v2pos = dict(_v3pos, strategy="VWAP-MR-v2", exit_cfg={})
_v = _em.evaluate(_v2pos, _below, {"vwap": 1002.0}, None)
check("a v2 position (below VWAP by design) is never touched by thesis exits",
      _v is None, f"{_v}")

_v = _em.evaluate(_v3pos, _below, None, None)
check("no indicators -> no thesis exit (fail quiet, stop still applies)",
      _v is None)

# the trader honours it end to end, and a caller without context is unchanged
_tr = PaperTraderV2(state_file=str(tmp / "thesis.json"))
_tr.state["cash"] = _tr.state["initial_capital"] = 1_000_000
_ck = datetime.combine(_td3, datetime.min.time()).replace(hour=11, tzinfo=IST)
pt.now_ist = lambda c=_ck: c
_tr.place_order(_mkt_sig, _ck)
_tr.process_orders({"ZZZ.NS": {"open": 1000.0, "close": 1004.0, "high": 1006.0,
                               "low": 999.0, "volume": 5e6}}, _ck)
_ex0 = _tr.check_exits({"ZZZ.NS": {"open": 1004, "close": 990, "high": 1005,
                                   "low": 989.5, "volume": 5e6}}, _ck)
check("WITHOUT context a below-VWAP bar does not exit (old callers unchanged)",
      not _ex0 and "ZZZ.NS" in _tr.state["positions"])
_ex1 = _tr.check_exits({"ZZZ.NS": {"open": 1004, "close": 990, "high": 1005,
                                   "low": 989.5, "volume": 5e6}}, _ck,
                       context={"indicators": {"ZZZ.NS": {"vwap": 995.0, "rsi": 55}},
                                "index": {"nifty": 24000.0}})
check("WITH context the same bar exits on vwap_reclaim",
      _ex1 and _ex1[0]["reason"] == "vwap_reclaim",
      f"{[e['reason'] for e in _ex1]}")
check("the per-bar snapshot was recorded for the position",
      len(_tr.last_snapshots) == 1 and _tr.last_snapshots[0]["ticker"] == "ZZZ.NS")
if _tr.last_snapshots:
    _s = _tr.last_snapshots[0]
    check("snapshot carries R-now, MFE, MAE and the VWAP verdict",
          all(k in _s for k in ("r_now", "r_mfe", "r_mae", "above_vwap"))
          and _s["above_vwap"] is False, f"{_s}")

print("\n" + "=" * 70)
print("3d. WALK-FORWARD OVERRIDES - the weekly job must not be able to hurt you")
print("=" * 70)
# walkforward.py runs unattended on a schedule and writes a file that config.py
# reads. That is a remote-control surface on a live trading bot, so the clamp
# around it is the safety property, not the feature. These assertions describe
# what a malicious, buggy or half-written params_live.json must NOT be able to
# do.
import importlib
import config as _cfg

_pfile = tmp / "params_live.json"


def _reload_with(payload):
    if payload is None:
        _pfile.unlink(missing_ok=True)
    else:
        json.dump(payload, open(_pfile, "w"))
    os.environ["ALGO_LIVE_PARAMS"] = str(_pfile)
    return importlib.reload(_cfg)


_base_rsi = _cfg.MOMENTUM["min_rsi"]

# in-band values ARE applied - the job has to be able to do its job
_c = _reload_with({"params": {"min_rsi": 78, "min_rvol": 5.0}})
check("an in-band override is applied", _c.MOMENTUM["min_rsi"] == 78,
      f"rsi {_c.MOMENTUM['min_rsi']}")
check("the override is reported, not silent", "min_rsi" in _c.LIVE_PARAM_OVERRIDES)

# out-of-band values are REFUSED, silently and safely
_c = _reload_with({"params": {"min_rsi": 5, "min_rvol": 500.0,
                              "stop_pct_floor": 0.9}})
check("an out-of-band RSI is refused", _c.MOMENTUM["min_rsi"] == _base_rsi,
      f"rsi {_c.MOMENTUM['min_rsi']}")
check("an absurd stop width is refused",
      _c.MOMENTUM["stop_pct_floor"] <= 0.020,
      f"{_c.MOMENTUM['stop_pct_floor']}")

# the things it must never reach, whatever the file says
_c = _reload_with({"params": {"use_breakeven_trail": True, "use_time_stop": True,
                              "min_dev_above_vwap": -0.05,
                              "risk_pct_per_trade": 0.5}})
check("the file cannot switch the breakeven trail back on",
      _c.MOMENTUM["use_breakeven_trail"] is False)
check("the file cannot switch the time stop back on",
      _c.MOMENTUM["use_time_stop"] is False)
check("the file cannot flip the direction to negative deviation",
      _c.MOMENTUM["min_dev_above_vwap"] > 0,
      f"{_c.MOMENTUM['min_dev_above_vwap']}")
check("the file cannot touch risk per trade",
      _c.RISK["risk_pct_per_trade"] < 0.05,
      f"{_c.RISK['risk_pct_per_trade']}")

# corrupt / missing files must be inert, not fatal
_pfile.write_text("{not json at all")
os.environ["ALGO_LIVE_PARAMS"] = str(_pfile)
try:
    _c = importlib.reload(_cfg)
    _survived = True
except Exception:
    _survived = False
check("a corrupt params file does not crash the scan", _survived)
check("a corrupt params file changes nothing",
      _survived and _c.MOMENTUM["min_rsi"] == _base_rsi)

os.environ.pop("ALGO_LIVE_PARAMS", None)
_cfg = importlib.reload(_cfg)

print("\n" + "=" * 70)
print("4b. EQUITY ACCOUNTING - the bug that bricked the first v2 cut")
print("=" * 70)
# The original equity() was cash + sum(entry_price * qty), but opening a position
# only deducts the 20% margin from cash - so it added the full notional on top of
# cash that still held 80% of it. equity_peak latched onto the inflated number and
# the drawdown halt fired permanently the first time a position closed.
# None of the 44 assertions above noticed. These do.

t = fresh_trader()
pt.now_ist = lambda: morning
eq_before = t.risk.equity()
t.place_order(order, morning)
bar = {"TCS.NS": {"close": 3152.0, "high": 3160.0, "low": 3145.0}}
t.update_prices(bar, morning)
t.process_orders(bar, morning)
eq_open = t.risk.equity()
pos = t.state["positions"]["TCS.NS"]
notional = pos["entry_price"] * pos["qty"]

check("opening a position does not inflate equity",
      abs(eq_open - eq_before) < 0.02 * eq_before,
      f"Rs{eq_before:,.0f} -> Rs{eq_open:,.0f} on Rs{notional:,.0f} notional")
check("equity is not cash + full notional (the old bug)",
      abs(eq_open - (t.state['cash'] + notional)) > 1000,
      f"buggy formula would give Rs{t.state['cash'] + notional:,.0f}")

t.risk.update_peak()
peak_open = t.state["equity_peak"]
ex = t.check_exits({"TCS.NS": {"close": 3095.0, "high": 3140.0, "low": 3090.0}}, morning)
eq_closed = t.risk.equity()
halted, why = t.risk.trading_halted(morning)

check("equity_peak does not latch onto an inflated figure",
      peak_open < eq_before * 1.05, f"peak Rs{peak_open:,.0f}")
check("a normal losing trade does NOT trip the drawdown halt",
      not halted, why or "not halted")
check("equity after close = equity before + realised P&L",
      abs(eq_closed - (eq_before + sum(e['pnl'] for e in ex))) < 1.0,
      f"Rs{eq_closed:,.2f} vs Rs{eq_before + sum(e['pnl'] for e in ex):,.2f}")

# cash must reconcile exactly - the entry cost was previously charged twice,
# once on open and again inside pnl at close
t2_ = fresh_trader()
pt.now_ist = lambda: morning
start_cash = t2_.state["cash"]
t2_.place_order(order, morning)
t2_.update_prices(bar, morning)
t2_.process_orders(bar, morning)
ex2 = t2_.check_exits({"TCS.NS": {"close": 3225.0, "high": 3400.0, "low": 3210.0}}, morning)
ex2 += t2_.check_exits({}, datetime(2026, 9, 21, 15, 5, tzinfo=IST))
realised = sum(e["pnl"] for e in ex2)
check("cash reconciles to start + realised P&L (no double-charged costs)",
      abs(t2_.state["cash"] - (start_cash + realised)) < 1.0,
      f"cash Rs{t2_.state['cash']:,.2f} vs expected Rs{start_cash + realised:,.2f}")

# partial exits must release their share of margin
t3_ = fresh_trader()
pt.now_ist = lambda: morning
t3_.place_order(order, morning)
t3_.update_prices(bar, morning)
t3_.process_orders(bar, morning)
m_full = t3_.state["positions"]["TCS.NS"]["margin"]
t3_.check_exits({"TCS.NS": {"close": 3225.0, "high": 3230.0, "low": 3210.0}}, morning)
p3 = t3_.state["positions"].get("TCS.NS")
if p3:
    check("partial exit releases its share of margin",
          p3["margin"] < m_full * 0.75,
          f"Rs{m_full:,.0f} -> Rs{p3['margin']:,.0f} after taking half off")

print("\n" + "=" * 70)
print("4c. ADAPTIVE SIZING - learns, but at the rate the evidence allows")
print("=" * 70)
from learning import (adaptive_risk_pct, performance_scalar, drawdown_scalar,
                      posterior_expectancy, MIN_RISK_PCT, MAX_RISK_PCT,
                      BASE_RISK_PCT, PRIOR_STRENGTH)

def trades(rs):
    return [{"R_multiple": r, "pnl": r * 1000} for r in rs]

# --- the central property: small samples must NOT move it much ---
five_losses = trades([-1.0] * 5)
p5, _ = performance_scalar(five_losses)
check("5 straight losses barely move sizing (streaks are normal)",
      p5 > 0.95, f"scalar {p5:.3f}")

three_wins = trades([1.5, 1.7, 1.6])
p3, _ = performance_scalar(three_wins)
check("3 straight wins do NOT size up (luck is not edge)",
      p3 < 1.05, f"scalar {p3:.3f}")

# --- but it DOES learn, given real evidence ---
many_bad = trades([-1.0] * 40 + [1.5] * 10)     # 20% win rate over 50
p_bad, why_bad = performance_scalar(many_bad)
check("50 genuinely bad trades DO cut size", p_bad < 0.9, why_bad)

many_good = trades([1.5] * 35 + [-1.0] * 15)    # 70% win rate over 50
p_good, why_good = performance_scalar(many_good)
check("50 genuinely good trades DO raise size", p_good > 1.05, why_good)

# --- learning rate scales with n, as designed ---
_, _, n_small = posterior_expectancy(trades([-1.0] * 10))
post_10, prior, _ = posterior_expectancy(trades([-1.0] * 10))
post_200, _, _ = posterior_expectancy(trades([-1.0] * 200))
check("200 bad trades move belief much further than 10",
      abs(post_200 - prior) > 3 * abs(post_10 - prior),
      f"10 -> {post_10:+.3f}R, 200 -> {post_200:+.3f}R, prior {prior:+.3f}R")

# --- bounds hold under absurd input ---
pct_awful, _ = adaptive_risk_pct(trades([-1.0] * 500), 400_000, 500_000)
pct_amazing, _ = adaptive_risk_pct(trades([3.0] * 500), 900_000, 900_000)
check("sizing never falls below the floor", pct_awful >= MIN_RISK_PCT,
      f"{pct_awful*100:.3f}%")
check("sizing never exceeds the ceiling", pct_amazing <= MAX_RISK_PCT,
      f"{pct_amazing*100:.3f}%")
check("a catastrophic record still de-risks hard", pct_awful < BASE_RISK_PCT * 0.7,
      f"{pct_awful*100:.3f}% vs base {BASE_RISK_PCT*100:.2f}%")

# --- drawdown scalar ---
d0, _ = drawdown_scalar(500_000, 500_000)
d2, _ = drawdown_scalar(490_000, 500_000)   # 2%
d5, _ = drawdown_scalar(475_000, 500_000)   # 5%
check("no de-risking at the high-water mark", d0 == 1.0)
check("no de-risking inside normal drawdown", d2 == 1.0, f"{d2}")
check("half size at 5% drawdown", abs(d5 - 0.5) < 0.01, f"{d5:.2f}")
check("drawdown de-risking is monotonic", d0 >= d2 >= d5)

# --- it must never touch entry/exit rules ---
import learning, inspect
src = inspect.getsource(learning)
forbidden = ["STRATEGY[", "FILTERS[", "rsi_oversold", "min_vwap_deviation",
             "stop_atr_mult", "min_reward_risk"]
leaks = [w for w in forbidden if w in src and "never" not in src.split(w)[0][-200:].lower()]
check("learning.py cannot modify entry/exit rules",
      not any(f"{w}] =" in src or f'{w}"] =' in src for w in forbidden),
      "size only, by construction")

print("\n" + "=" * 70)
print("4d. CAPITAL RECONCILIATION - raising capital must actually take effect")
print("=" * 70)
# The state file pins capital at creation. Raising SYSTEM["initial_capital"]
# does nothing on its own, because _load() reads cash from disk. Without a
# reconcile, config.py would claim Rs10L while the book traded Rs5L - the same
# stale-input-producing-a-confident-answer shape as the NaN regime gate.
import json as _json

def _write_state(path, cap, cash, trades):
    _json.dump({"cash": cash, "initial_capital": cap, "equity_peak": cash,
                "positions": {}, "pending_orders": {}, "last_known_price": {},
                "trade_history": trades, "daily_entry_count": {},
                "total_pnl": cash - cap}, open(path, "w"))

_f = tmp / "cap_empty.json"
_write_state(_f, SYSTEM["initial_capital"] / 2, SYSTEM["initial_capital"] / 2, [])
_t = PaperTraderV2(state_file=str(_f))
check("an EMPTY book migrates to the configured capital",
      abs(_t.state["initial_capital"] - SYSTEM["initial_capital"]) < 1,
      f"Rs{_t.state['initial_capital']:,.0f}")
check("migration credits the cash difference too",
      abs(_t.state["cash"] - SYSTEM["initial_capital"]) < 1,
      f"cash Rs{_t.state['cash']:,.0f}")

_f2 = tmp / "cap_history.json"
_old = SYSTEM["initial_capital"] / 2
_write_state(_f2, _old, _old + 7000,
             [{"pnl": 7000, "R_multiple": 1.7, "exit_time": "2026-09-22 11:00:00 IST"}])
_t2 = PaperTraderV2(state_file=str(_f2))
check("a book WITH history is NOT silently rebased",
      abs(_t2.state["initial_capital"] - _old) < 1,
      f"stayed at Rs{_t2.state['initial_capital']:,.0f} - restating mid-book "
      f"would make past and future returns incomparable")
check("and its cash is untouched",
      abs(_t2.state["cash"] - (_old + 7000)) < 1)

print("\n" + "=" * 70)
print("4e. WHAT GETS RECORDED FOR LATER")
print("=" * 70)
# v1's signals_intraday.json is the only reason its 188 candidates could be
# replayed through v2's filters. Without an equivalent, "what did we reject,
# and would it have worked?" is unanswerable - trade_history holds only what
# was taken.
mod.now_ist = lambda: datetime.combine(trade_day, datetime.min.time()).replace(
    hour=12, minute=0, tzinfo=IST)
_ = strat.compute_signals({"TCS.NS": tcs, "INFY.NS": tcs_shallow}, nifty_up, 14.0)
_scan = getattr(strat, "last_scan", {})
check("every scan records why candidates were rejected",
      isinstance(_scan.get("rejects"), dict) and len(_scan["rejects"]) > 0,
      f"{_scan.get('rejects')}")
check("scan records how many names were examined",
      _scan.get("scanned") == 2, f"scanned={_scan.get('scanned')}")

_ = strat.compute_signals({"TCS.NS": tcs}, nifty_down, 14.0)
check("a closed regime gate is recorded with its reason",
      "NIFTY" in str(getattr(strat, "last_scan", {}).get("regime", "")),
      str(strat.last_scan.get("regime"))[:60])

# price cache must cover only what we could need to exit
_t3 = fresh_trader()
pt.now_ist = lambda: morning
_many = {f"X{i}.NS": {"close": 100.0, "high": 101.0, "low": 99.0} for i in range(50)}
_t3.update_prices(_many, morning)
check("price cache stays empty with no positions or orders",
      len(_t3.state["last_known_price"]) == 0,
      f"{len(_t3.state['last_known_price'])} entries for 50 tickers offered")
_t3.place_order(order, morning)
_t3.update_prices({**_many, "TCS.NS": {"close": 3152.0, "high": 3160.0, "low": 3145.0}}, morning)
check("price cache covers a resting order",
      list(_t3.state["last_known_price"]) == ["TCS.NS"],
      f"{list(_t3.state['last_known_price'])}")

print("\n" + "=" * 70)
print("5. FULL SESSION - main.py wiring, 25 bars, frozen clock")
print("=" * 70)

import main as bot

# BOTH strategies are driven through main.py, on the market shape each one is
# built for. Testing only the active one would leave the other's wiring to rot
# unnoticed - and v2 is the control case the research still has to reproduce.
# It also catches the thing a single-strategy test cannot: two different signal
# shapes and two different exit rule sets sharing one paper trader.


def run_session(strategy_name, day_path_for):
    np.random.seed(5)
    universe = {}
    for tk, base in [("TCS.NS", 3200.0), ("INFY.NS", 1500.0), ("RELIANCE.NS", 2800.0)]:
        h, _ = history(base, 12, vol=600_000)
        universe[tk] = pd.concat([h, day_path_for(base)])
    nifty_full = pd.concat([hist_nifty, make_day(trade_day, base=24000.0,
                                                 path=[24000 + 8 * i for i in range(25)],
                                                 vol=1_000_000)])
    vix_full = pd.concat([history(14.0, 12, vol=1000)[0],
                          make_day(trade_day, base=14.0, path=[14.0] * 25, vol=1000)])

    state_file = tmp / f"session_{strategy_name}.json"
    bars_seen, events = 0, []
    for bar_i in range(4, 25):
        ts = session_index(trade_day)[bar_i]
        clock = ts.to_pydatetime().replace(tzinfo=IST)

        sliced = {tk: df[df.index <= ts] for tk, df in universe.items()}
        sliced["^NSEI"] = nifty_full[nifty_full.index <= ts]
        sliced["^INDIAVIX"] = vix_full[vix_full.index <= ts]

        class StubFetcher:
            def fetch_intraday(self, tickers, days_back=5):
                return {t: sliced[t].copy() for t in tickers if t in sliced}

        bot.IntradayFetcher = StubFetcher
        bot.ACTIVE_STRATEGY = strategy_name
        bot.now_ist = lambda c=clock: c
        bot.INTRADAY_UNIVERSE = ["TCS.NS", "INFY.NS", "RELIANCE.NS"]
        pt.now_ist = lambda c=clock: c
        mod.now_ist = lambda c=clock: c
        _m3.now_ist = lambda c=clock: c
        bot.PaperTraderV2 = lambda: PaperTraderV2(state_file=str(state_file))
        try:
            bot.main()
            bars_seen += 1
        except Exception as e:
            events.append(f"bar {bar_i} ({ts:%H:%M}) raised {type(e).__name__}: {e}")
    return state_file, bars_seen, events


def assert_session(label, state_file, bars_seen, events):
    check(f"[{label}] all 21 scans ran without raising", not events, "; ".join(events[:2]))
    check(f"[{label}] every scan completed", bars_seen == 21, f"{bars_seen}/21")
    final = json.load(open(state_file))
    check(f"[{label}] state file is written and readable", "cash" in final)
    check(f"[{label}] no position left open after 15:05",
          len(final["positions"]) == 0, f"{list(final['positions'])}")
    check(f"[{label}] no orders left resting", len(final["pending_orders"]) == 0)

    h = final["trade_history"]
    print(f"\n  {label}: {len(h)} fill(s), net Rs{sum(t['pnl'] for t in h):+,.0f}, "
          f"friction Rs{sum(t.get('friction', 0) for t in h):,.0f}")
    for t_ in h:
        print(f"    {t_['ticker']:<13} {t_['qty']:>4} @ Rs{t_['exit_price']:>8.2f}  "
              f"Rs{t_['pnl']:>8,.0f} ({t_['R_multiple']:+.2f}R)  {t_['reason']}")
    if h:
        noms = [t_["entry_price"] * t_["qty"] for t_ in h]
        check(f"[{label}] no fragment trades taken",
              all(n >= RISK["min_notional_per_trade"] * 0.4 for n in noms),
              f"min notional Rs{min(noms):,.0f}")
        check(f"[{label}] entry timestamps are IST-labelled (v1 logged UTC for a month)",
              all("IST" in t_["entry_time"] for t_ in h))
    return h


# --- v2 on the dislocation it was built for ---
_sf, _bs, _ev = run_session(
    "vwap_mr_v2",
    lambda base: make_day(trade_day, base=base, path=dip_then_reverse(base), vol=600_000))
_h2 = assert_session("v2", _sf, _bs, _ev)

# --- v3 on a trending session with a participation spike ---
def _mom_full_day(base):
    # step is steeper than it looks it should need: VWAP is volume-weighted on
    # the TYPICAL price (h+l+c)/3, so a wide bar drags VWAP up toward the close
    # and a gentle climb never opens the 1.2% gap the rule wants.
    d = momentum_day(trade_day, base, bars=25, step=0.0045, vol=600_000, spike=1.0)
    # the spike arrives mid-session, inside the entry window, not at the bell
    d.loc[d.index[8:16], "volume"] = 600_000 * 9
    return d


_sf3, _bs3, _ev3 = run_session("momentum_v3", _mom_full_day)
_h3f = assert_session("v3", _sf3, _bs3, _ev3)

check("v3 actually traded end-to-end through main.py", len(_h3f) > 0,
      f"{len(_h3f)} fill(s) - 0 means the production path is unreachable")
if _h3f:
    check("v3 trades are recorded under their own strategy name",
          all(t_.get("strategy") == "MOM-v3" for t_ in _h3f))
    check("v3 exits are square-off or stop, never a target or time stop",
          all(t_["reason"] in ("square_off", "stop_loss") for t_ in _h3f),
          f"{sorted({t_['reason'] for t_ in _h3f})}")

shutil.rmtree(tmp, ignore_errors=True)

print("\n" + "=" * 70)
print(f"{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print(f"  FAILED: {f}")
print("=" * 70)
sys.exit(1 if FAIL else 0)
