"""
VWAP Mean Reversion v2 - cost-aware, regime-gated, trigger-confirmed.

WHAT CHANGED AND WHY
--------------------
v1 bought the close of the currently-forming 15-minute bar whenever RSI < 28 and
price sat 0.6% under VWAP. Three things were wrong with that, in order of size:

1. THE TRADE COULD NOT PAY FOR ITSELF. Target was 1.3 x 0.6% = 0.78% of notional;
   round-trip friction is 0.134% of notional. After costs the setup was 0.91:1
   reward-to-risk and needed a 52.5% win rate to break even. It ran 32.7%.

2. THERE WAS NO TRIGGER. "RSI is low and price is below VWAP" describes a stock
   that is falling. v1 bought it mid-fall, at a price it could not actually
   transact at (the close of a bar that has not closed yet). v2 requires price
   to turn back up through the signal bar's high before it will buy. If the
   stock keeps falling, no fill, no loss.

3. THERE WAS NO WAY TO TELL NOISE FROM NEWS. Mean reversion works on
   liquidity-driven dips and fails on information-driven ones. v2 adds a
   relative-volume ceiling, a gap filter and an index regime gate. Note the
   direction of the RVOL filter: a breakout strategy wants "stocks in play",
   a reversion strategy must avoid them.

The stop also moved from a flat 0.55% to 1.2 x ATR. That is partly about
normalising risk across names (0.55% is ~3 ATRs on ITC and ~0.3 ATR on
TATASTEEL) and partly about cost: notional = risk / stop%, so widening the stop
from 0.55% to ~1.5% cuts position size, and therefore friction, by about 60%
without changing the rupee risk at all.
"""
import logging
from datetime import datetime, time as dtime
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from config import STRATEGY, FILTERS, SYSTEM, INDEX_TICKER
from costs import VARIABLE_ROUNDTRIP_PCT, FIXED_ROUNDTRIP
from data.fetcher import TechnicalIndicators

from tzutil import IST as ist, now_ist, stamp
logger = logging.getLogger(__name__)

TICK = 0.05  # NSE equity tick size


class VWAPMeanReversionV2:
    name = "VWAP-MR-v2"

    def __init__(self):
        self.p = STRATEGY
        self.f = FILTERS
        self.ti = TechnicalIndicators()

    # =====================================================================
    # REGIME GATE - evaluated once per scan, applies to every candidate
    # =====================================================================
    def regime_ok(self, index_df: Optional[pd.DataFrame], vix: Optional[float],
                  today) -> tuple[bool, str]:
        """
        v1 had no index awareness whatsoever. On 2026-09-15 it opened five
        positions and all five lost; on 09-09, 09-16 and 08-13 every trade of
        the day lost. Individual-stock mean reversion does not survive a
        market-wide slide - you are not fading a stretched rubber band, you are
        standing in front of index flow with 2-3x leverage.
        """
        if index_df is None or index_df.empty:
            # Fail CLOSED. v1's habit was to carry on when data was missing;
            # that is how three of its positions ended up held overnight.
            return False, "no index data - standing down"

        idx_today = index_df[index_df.index.date == today]
        if len(idx_today) < 2:
            return False, "insufficient index bars"

        idx_vwap = self.ti.vwap(idx_today).iloc[-1]
        idx_close = idx_today["close"].iloc[-1]
        idx_open = idx_today["open"].iloc[0]

        if self.f["require_index_above_vwap"]:
            dev = (idx_vwap - idx_close) / idx_vwap
            if dev > self.f["index_vwap_tolerance"]:
                return False, (
                    f"NIFTY {dev * 100:.2f}% below its VWAP "
                    f"(limit {self.f['index_vwap_tolerance'] * 100:.1f}%)"
                )

        day_move = (idx_close - idx_open) / idx_open
        if day_move < -self.f["max_index_daily_drop"]:
            return False, f"NIFTY down {day_move * 100:.2f}% on the day"

        if vix is not None:
            if vix > self.f["max_india_vix"]:
                return False, f"India VIX {vix:.1f} above {self.f['max_india_vix']}"
            if vix < self.f["min_india_vix"]:
                return False, f"India VIX {vix:.1f} too low to pay costs"

        return True, "regime ok"

    # =====================================================================
    # SESSION WINDOW
    # =====================================================================
    def in_entry_window(self, bar_time) -> tuple[bool, str]:
        t = bar_time.time() if hasattr(bar_time, "time") else bar_time
        first = dtime(*map(int, SYSTEM["first_entry_time"].split(":")))
        last = dtime(*map(int, SYSTEM["last_entry_time"].split(":")))
        if t < first:
            return False, "before first entry time (opening auction noise)"
        if t > last:
            # 55% of v1's exits were the 15:15 clock rather than the thesis,
            # and its median hold was 105 minutes. A trade entered at 14:30
            # has 45 minutes to work. That is not a trade, it is a coin flip
            # with costs attached.
            return False, "after last entry time - not enough session left to revert"
        return True, "in window"

    # =====================================================================
    # PER-NAME FILTERS
    # =====================================================================
    def name_filters_ok(self, ticker: str, df: pd.DataFrame,
                        today_df: pd.DataFrame) -> tuple[bool, str]:
        # --- opening gap: an event, not an overextension ---
        prev = df[df.index.date < today_df.index[0].date()]
        if not prev.empty:
            prev_close = prev["close"].iloc[-1]
            gap = abs(today_df["open"].iloc[0] - prev_close) / prev_close
            if gap > self.f["max_opening_gap_pct"]:
                return False, f"opening gap {gap * 100:.2f}% - treat as news, not stretch"

        # --- relative volume: the news detector ---
        # Compare today's cumulative volume at this point in the session with the
        # same point on prior days. High RVOL means information is arriving, and
        # information is exactly what you must not fade.
        n = len(today_df)
        hist_days = sorted(set(d for d in df.index.date if d < today_df.index[0].date()))[-14:]
        if len(hist_days) >= 5:
            sames = []
            for d in hist_days:
                dd = df[df.index.date == d]
                if len(dd) >= n:
                    sames.append(dd["volume"].iloc[:n].sum())
            if sames:
                baseline = float(np.median(sames))
                rvol = today_df["volume"].sum() / baseline if baseline > 0 else 1.0
                if rvol > self.f["rvol_max"]:
                    return False, f"RVOL {rvol:.2f} - news flow, do not fade it"
                if rvol < self.f["rvol_min"]:
                    return False, f"RVOL {rvol:.2f} - too quiet for reversion flow"

        # --- liquidity: keeps the 5 bps slippage assumption honest ---
        turnover = (df["close"] * df["volume"]).tail(50).median()
        if turnover < self.f["min_median_15m_turnover"]:
            return False, f"median 15m turnover Rs{turnover / 1e7:.1f}cr below floor"

        return True, "filters ok"

    # =====================================================================
    # SIGNAL
    # =====================================================================
    def compute_signals(self, data: Dict[str, pd.DataFrame],
                        index_df: Optional[pd.DataFrame] = None,
                        vix: Optional[float] = None) -> List[Dict]:
        today = now_ist().date()

        ok, why = self.regime_ok(index_df, vix, today)
        if not ok:
            logger.info(f"REGIME GATE CLOSED: {why} - no entries this scan")
            return []

        out = []
        for ticker, df in data.items():
            try:
                sig = self._signal(ticker, df, today)
                if sig:
                    out.append(sig)
            except Exception as e:
                logger.debug(f"{ticker}: {e}")

        # Rank by how much the setup clears its cost hurdle, not by raw
        # extension. v1 ranked on "most oversold", which preferentially picked
        # the names in genuine trouble.
        out.sort(key=lambda s: s["edge_after_cost"], reverse=True)
        return out

    def _signal(self, ticker: str, df: pd.DataFrame, today) -> Optional[Dict]:
        today_df = df[df.index.date == today]
        if len(today_df) < 4:
            return None

        bar_time = today_df.index[-1]
        ok, why = self.in_entry_window(bar_time)
        if not ok:
            return None

        ok, why = self.name_filters_ok(ticker, df, today_df)
        if not ok:
            logger.debug(f"{ticker}: {why}")
            return None

        vwap = self.ti.vwap(today_df).iloc[-1]
        rsi = self.ti.rsi(df, self.p["rsi_period"]).iloc[-1]
        atr = self.ti.atr(df, self.p["atr_period"]).iloc[-1]

        bar = today_df.iloc[-1]
        close, high, low = bar["close"], bar["high"], bar["low"]
        if vwap <= 0 or close <= 0 or not np.isfinite(atr) or atr <= 0:
            return None

        # ---- condition 1: oversold and stretched below VWAP ----
        deviation = (vwap - close) / close
        if rsi > self.p["rsi_oversold"]:
            return None
        if deviation < self.p["min_vwap_deviation"]:
            return None

        # ---- the entry TRIGGER (not the entry price) ----
        # A stop-limit buy one tick above this bar's high. Price must turn up
        # and take us in. v1 assumed a fill at the falling bar's close, which is
        # both unfillable and the worst moment to buy.
        trigger = high + self.p["trigger_offset_ticks"] * TICK

        # ---- stop: volatility-scaled, structure-aware, cost-floored ----
        atr_stop = trigger - self.p["stop_atr_mult"] * atr
        struct_stop = low - TICK
        stop = min(atr_stop, struct_stop) if self.p["stop_below_signal_low"] else atr_stop

        stop_pct = (trigger - stop) / trigger
        if stop_pct < self.p["stop_pct_floor"]:
            # Widen to the floor. A stop tighter than this forces so much
            # notional that friction alone swamps the trade.
            stop = trigger * (1 - self.p["stop_pct_floor"])
            stop_pct = self.p["stop_pct_floor"]
        if stop_pct > self.p["stop_pct_cap"]:
            return None  # too wild to size sensibly

        risk_per_share = trigger - stop

        # ---- targets ----
        t1 = vwap                                                   # thesis complete
        t2 = vwap + self.p["t2_vwap_overshoot"] * (vwap - trigger)   # let it run

        # ---- THE COST HURDLE ----
        # Convert round-trip friction into a per-share price and demand that the
        # trade clears its stop by a real multiple AFTER paying it. This single
        # test is what v1 never applied, and it is the test its own geometry
        # would have failed.
        cost_per_share = trigger * VARIABLE_ROUNDTRIP_PCT
        blended_target = 0.5 * t1 + 0.5 * t2
        net_reward = (blended_target - trigger) - cost_per_share
        rr_after_cost = net_reward / risk_per_share if risk_per_share > 0 else 0.0

        if rr_after_cost < self.p["min_reward_risk_after_cost"]:
            logger.debug(
                f"{ticker}: R:R after cost {rr_after_cost:.2f} < "
                f"{self.p['min_reward_risk_after_cost']} - skipping"
            )
            return None

        return {
            "ticker": ticker,
            "signal": "BUY",
            "order_type": "STOP_LIMIT",
            "trigger_price": round(trigger, 2),
            "limit_price": round(trigger * 1.002, 2),   # cap the chase
            "valid_bars": self.p["trigger_valid_bars"],
            "stop_loss": round(stop, 2),
            "t1": round(t1, 2),
            "t2": round(t2, 2),
            "t1_fraction": self.p["t1_fraction"],
            "vwap": round(vwap, 2),
            "rsi": round(float(rsi), 1),
            "atr": round(float(atr), 2),
            "stop_pct": round(stop_pct * 100, 2),
            "risk_per_share": round(risk_per_share, 2),
            "rr_after_cost": round(rr_after_cost, 2),
            "edge_after_cost": round(rr_after_cost, 3),
            "bar_volume": float(bar["volume"]),
            "strategy": self.name,
            "signal_bar_time": str(bar_time),
        }
