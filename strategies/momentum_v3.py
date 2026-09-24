"""
Momentum v3 - long strength, not weakness.

WHY THIS EXISTS
---------------
v1 and v2 both bought stocks stretched BELOW their intraday VWAP on low RSI,
on the theory that stretched prices revert. On 2026-09-24 that theory was
measured directly, with no trading machinery in the way: 185,847 real
15-minute bars across 210 NSE names, forward returns from the entry condition,
no stops, no sizing, no costs.

    below VWAP, RSI < 40, held:   +15m +0.008%   +1h -0.028%
                                  +2h  -0.085%   to close -0.137%  (t = -7.97)

It does not revert. It keeps going down, and further the longer it is held.
Being more oversold made it worse. Every RSI bucket from 0-20 to 40-50 was
negative, so no threshold anywhere in that family was going to work - which is
why v2 is replaced rather than retuned.

The mirror condition was the only positive result in the study, and it was
positive under four unrelated features that all graded monotonically the same
way: the stronger the extension, the heavier the volume, the more volatile the
name, the better the forward return. See config.MOMENTUM for the table.

WHAT IS DELIBERATELY ABSENT
---------------------------
No reversal trigger. v2's stop-limit above the signal bar's high was measured
at -0.153% to the close against -0.137% without it, and it fired on 26% of
setups. For a momentum entry it means paying up to confirm a move that has
already confirmed itself.

No target, no partial exit, no breakeven trail, no time stop. The breakeven
trail lost money in every one of the 50 sweep cells it appeared in. Removing
the best five trades turns every cell of that sweep negative. The edge is the
right tail; each of those devices is a way of cutting the right tail off.

WHAT THIS IS EXPECTED TO EARN
-----------------------------
Roughly nothing, on the evidence. As an account this rule made +1.28% over the
59 measured days at t = +0.20, and no cell of a 100-cell threshold sweep
survived both the month-by-month check and the removal of its best five trades.
It is deployed because it is the only signal on this market measured with the
right sign, and forward paper trading is the only honest test left. Treat every
trade from here as out-of-sample data, not as income.
"""
import logging
from datetime import datetime, time as dtime
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from config import MOMENTUM, FILTERS, SYSTEM, INDEX_TICKER
from costs import VARIABLE_ROUNDTRIP_PCT
from data.fetcher import TechnicalIndicators

from tzutil import now_ist

logger = logging.getLogger(__name__)


class MomentumV3:
    name = "MOM-v3"

    def __init__(self):
        self.p = MOMENTUM
        self.f = FILTERS
        self.ti = TechnicalIndicators()
        self.last_scan: Dict = {}

    # =====================================================================
    # REGIME - a solvency gate, not an alpha filter
    # =====================================================================
    def regime_ok(self, index_df: Optional[pd.DataFrame], vix: Optional[float],
                  today) -> tuple[bool, str]:
        """
        This is NOT claimed as edge. Gating on the index looked helpful in the
        measurement (+0.067% with NIFTY above its VWAP against -0.024% below)
        and then failed in September, so it earns no place as a signal filter.

        It stays because three leveraged correlated longs in a market that is
        falling hard is the one configuration that can do serious damage before
        any per-trade stop is reached. It fails CLOSED: a NaN must never be
        allowed to answer a safety question, which is the exact bug that made
        v2's version of this gate a silent no-op for weeks.
        """
        if index_df is None or index_df.empty:
            return False, "no index data - standing down"

        idx_today = index_df[index_df.index.date == today]
        if len(idx_today) < 2:
            return False, "insufficient index bars"

        idx_close = float(idx_today["close"].iloc[-1])
        idx_open = float(idx_today["open"].iloc[0])
        if not all(np.isfinite([idx_close, idx_open])) or idx_open <= 0:
            return False, f"index values not finite (open={idx_open}, close={idx_close})"

        day_move = (idx_close - idx_open) / idx_open
        if day_move < -self.p["max_index_daily_drop"]:
            return False, (f"NIFTY down {day_move * 100:.2f}% on the day - "
                           f"standing down at {self.p['max_index_daily_drop'] * 100:.1f}%")

        if vix is not None and np.isfinite(vix) and vix > self.p["max_india_vix"]:
            return False, f"India VIX {vix:.1f} above {self.p['max_india_vix']}"

        return True, "regime ok"

    # =====================================================================
    def in_entry_window(self, bar_time) -> tuple[bool, str]:
        t = bar_time.time() if hasattr(bar_time, "time") else bar_time
        first = dtime(*map(int, SYSTEM["first_entry_time"].split(":")))
        last = dtime(*map(int, SYSTEM["last_entry_time"].split(":")))
        if t < first:
            return False, "before first entry time (opening auction noise)"
        if t > last:
            # Positions are held to square-off, so a late entry has no session
            # left to work in. Bars after 13:15 measured +0.010% net against
            # +0.110% for the 10:15-11:15 band.
            return False, "after last entry time - no session left to run"
        return True, "in window"

    # =====================================================================
    # SIGNAL
    # =====================================================================
    def compute_signals(self, data: Dict[str, pd.DataFrame],
                        index_df: Optional[pd.DataFrame] = None,
                        vix: Optional[float] = None) -> List[Dict]:
        today = now_ist().date()

        ok, why_regime = self.regime_ok(index_df, vix, today)
        if not ok:
            logger.info(f"REGIME GATE CLOSED: {why_regime} - no entries this scan")
            self.last_scan = {"regime": why_regime, "scanned": len(data), "signals": 0,
                              "rejects": {"regime gate closed": len(data)},
                              "near_misses": []}
            return []

        out, rejects, near = [], {}, []
        for ticker, df in data.items():
            try:
                sig, why = self._signal(ticker, df, today)
                if sig:
                    out.append(sig)
                elif why:
                    key = why.split(" -")[0].split("(")[0].strip()
                    rejects[key] = rejects.get(key, 0) + 1
                    if why.startswith("rvol") and len(near) < 10:
                        near.append({"ticker": ticker, "reason": why})
            except Exception as e:
                logger.debug(f"{ticker}: {e}")

        self.last_scan = {"regime": why_regime, "scanned": len(data),
                          "signals": len(out), "rejects": rejects,
                          "near_misses": near[:10]}

        # Rank by relative volume. Of the four graded features it had the
        # steepest jump at its top bucket, and unlike the others it is a direct
        # read on how much money is actually behind the move right now.
        out.sort(key=lambda s: s["entry_rvol"], reverse=True)
        return out

    def _signal(self, ticker: str, df: pd.DataFrame, today):
        today_df = df[df.index.date == today]
        if len(today_df) < 4:
            return None, "too few bars today"

        ok, why = self.in_entry_window(today_df.index[-1])
        if not ok:
            return None, why

        # --- liquidity, so the slippage assumption stays honest ---
        turnover = float((df["close"] * df["volume"]).tail(50).median())
        if not np.isfinite(turnover) or turnover < self.f["min_median_15m_turnover"]:
            return None, f"turnover Rs{turnover / 1e7:.1f}cr below floor"

        vwap = float(self.ti.vwap(today_df).iloc[-1])
        rsi = float(self.ti.rsi(df, self.p["rsi_period"]).iloc[-1])
        atr = float(self.ti.atr(df, self.p["atr_period"]).iloc[-1])
        bar = today_df.iloc[-1]
        close = float(bar["close"])

        if not all(np.isfinite([vwap, rsi, atr, close])) or vwap <= 0 or close <= 0 or atr <= 0:
            return None, "non-finite indicators"

        # --- bar-level relative volume ---
        # Deliberately NOT the cumulative-day RVOL in FILTERS. The measurement
        # behind min_rvol used this bar's volume against the name's own recent
        # median, and the two quantities behave differently: a name can be
        # quiet all session and print one enormous bar, and it is that bar
        # which carries the signal.
        vols = df["volume"].tail(self.p["rvol_lookback_bars"])
        med = float(vols.median())
        if not np.isfinite(med) or med <= 0:
            return None, "no volume baseline"
        rvol = float(bar["volume"]) / med

        # --- the three graded conditions ---
        deviation = (close - vwap) / vwap          # positive = ABOVE vwap
        if deviation < self.p["min_dev_above_vwap"]:
            return None, f"deviation {deviation * 100:.2f}% above VWAP below floor"
        if rsi < self.p["min_rsi"]:
            return None, f"RSI {rsi:.0f} below {self.p['min_rsi']}"
        if rvol < self.p["min_rvol"]:
            return None, f"rvol {rvol:.1f} below {self.p['min_rvol']}"

        atr_pct = atr / close
        if atr_pct < self.p["min_atr_pct"]:
            return None, (f"ATR {atr_pct * 100:.2f}% too quiet to clear "
                          f"{VARIABLE_ROUNDTRIP_PCT * 100:.3f}% of costs")

        # --- stop: wide on purpose ---
        # notional = risk / stop%, so widening the stop SHRINKS the position and
        # with it the friction, at identical rupee risk. Median adverse
        # excursion before the close was -0.76%, so a tighter stop is stopped
        # out by ordinary noise while paying more for the privilege.
        stop_pct = max(self.p["stop_pct_floor"], self.p["stop_atr_mult"] * atr_pct)
        if stop_pct > self.p["stop_pct_cap"]:
            return None, f"stop {stop_pct * 100:.2f}% above cap - too wild to size"

        # Reference prices. The fill is the NEXT bar's open, which is the first
        # price an order placed now can actually get; these are for sizing and
        # for the record, and the trader recomputes the stop from the real fill.
        ref = close
        stop = ref * (1 - stop_pct)

        return {
            "ticker": ticker,
            "signal": "BUY",
            "order_type": self.p["order_type"],          # MARKET
            "reference_price": round(ref, 2),
            "stop_pct": round(stop_pct * 100, 3),
            "stop_loss": round(stop, 2),
            "valid_bars": 1,                             # fill next bar or drop it
            "atr": round(atr, 2),
            "strategy": self.name,
            "bar_volume": float(bar["volume"]),
            "signal_bar_time": str(today_df.index[-1]),

            # Exit rules travel WITH the signal rather than being read from a
            # global. Two strategies with different exit logic have to be able
            # to coexist in one book, and v2's exits reading STRATEGY[...]
            # directly is what made that impossible.
            "exit_cfg": {
                "hold_to_square_off": self.p["hold_to_square_off"],
                "use_target": self.p["use_target"],
                "use_breakeven_trail": self.p["use_breakeven_trail"],
                "use_time_stop": self.p["use_time_stop"],
                # Hands the position to engine/exit_manager.py each bar. Which
                # thesis rules are live is decided there, from config.MOMENTUM,
                # with the measurement behind each switch.
                "thesis_exits": True,
            },

            # The entry snapshot. Every number the rule looked at, stored so
            # that in three months "did rvol actually earn its threshold?" has
            # an answer. None of it is reconstructible after the fact.
            "entry_rsi": round(rsi, 1),
            "entry_deviation_pct": round(deviation * 100, 3),
            "entry_atr_pct": round(atr_pct * 100, 3),
            "entry_rvol": round(rvol, 2),
            "entry_turnover_cr": round(turnover / 1e7, 2),
            "entry_vwap": round(vwap, 2),
        }, None
