"""
In-trade management: what to do with an open position, bar by bar.

WHY THIS IS A SEPARATE MODULE
-----------------------------
The paper trader owns the mechanics of an exit - the fill, the cost, the
ledger. It should not own the OPINION of when to exit, because opinions get
tested and replaced and the mechanics do not. v2's exits were written straight
into check_exits() reading STRATEGY[...] directly, which is why two strategies
could not share one book and why testing an exit rule meant editing the trader.

Everything here is a pure function of (position, this bar, indicators). It
returns either None or (reason, price). The trader does the rest.

WHAT IS IN HERE, AND WHY MOST OF IT IS OFF
------------------------------------------
study_exits.py measured nine rules on the same 261 entries. The table is in
config.MOMENTUM next to the switches. The short version:

  - trailing stops lose money, and tighter trails lose more. The edge in this
    strategy is the right tail of the return distribution; a trail's job is to
    cut that tail off. Not implemented, deliberately, so it cannot be enabled
    by flipping a flag - it would have to be written, and the person writing
    it would have to read this first.
  - exit-on-VWAP-reclaim is the one rule that raised both the net AND the
    total without its best five trades. It is on. Its mechanism is that the
    thesis (extended above VWAP on momentum) is falsified when price closes
    back below VWAP, and it cannot fire on a trade that is working.
  - RSI and index-drop exits measured as noise. They exist here so they can be
    switched on by the walk-forward if a future measurement supports it, and
    they are off.

Every rule reads the bar's CLOSE and acts at the NEXT bar's open. That is not
a modelling convenience, it is what a 15-minute cron can actually do: by the
time the scan runs the bar has closed, and the earliest fill is the next open.

WHAT GETS RECORDED
------------------
snapshot() returns the per-bar state of an open position - where price is
relative to entry, the running best and worst, and what every rule would see.
main.py appends one line per open position per scan to logs/positions_v3.jsonl.
That file is the dataset that lets any of the rules above be re-tested on REAL
trades later, which is the only test that beats the 59-day sample.
"""
from datetime import datetime
from typing import Dict, Optional, Tuple

import numpy as np

from config import MOMENTUM
from data.fetcher import TechnicalIndicators
import logging


class ExitManager:
    def __init__(self, cfg: Optional[Dict] = None):
        self.cfg = cfg if cfg is not None else MOMENTUM

    # ------------------------------------------------------------------
    def evaluate(self, pos: Dict, bar: Dict, ind: Optional[Dict],
                 index_ctx: Optional[Dict]) -> Optional[Tuple[str, float]]:
        """
        Decide whether an open position should be closed on THIS bar's
        evidence, at a price reachable on the NEXT bar.

        pos       the position as the trader holds it
        bar       this bar's OHLCV for the ticker
        ind       {"vwap":..., "rsi":...} for the ticker as of this bar, or None
        index_ctx {"nifty": last, "nifty_at_entry": ...} or None

        Returns (reason, price) or None. `price` is the bar's close - the
        trader treats a thesis exit as a market order that will fill around
        the next open, and the close is the best available proxy for that
        when the next bar does not yet exist.
        """
        ecfg = pos.get("exit_cfg") or {}
        if not ecfg.get("thesis_exits", False):
            return None                 # v2 positions, or a strategy that opts out
        if not bar:
            return None
        close = float(bar.get("close", 0) or 0)
        if not np.isfinite(close) or close <= 0:
            return None

        # ---- 1. thesis falsified: price closed back below VWAP ----
        if self.cfg.get("exit_on_vwap_reclaim") and ind:
            vwap = ind.get("vwap")
            if vwap is not None and np.isfinite(vwap) and vwap > 0 and close < vwap:
                # Not on the entry bar itself. A market fill can land a tick
                # under a fast-moving VWAP; give the thesis one full bar.
                if pos.get("bars_held", 0) >= 1:
                    return ("vwap_reclaim", close)

        # ---- 2. momentum gone: RSI collapsed (OFF - measured as noise) ----
        rsi_floor = self.cfg.get("exit_on_rsi_below")
        if rsi_floor and ind:
            rsi = ind.get("rsi")
            if rsi is not None and np.isfinite(rsi) and rsi < rsi_floor:
                return ("rsi_collapse", close)

        # ---- 3. market turned (OFF - measured as noise) ----
        drop = self.cfg.get("exit_on_index_drop")
        if drop and index_ctx:
            n0 = index_ctx.get("nifty_at_entry")
            n1 = index_ctx.get("nifty")
            if n0 and n1 and np.isfinite(n0) and np.isfinite(n1) and n1 < n0 * (1 - drop):
                return ("index_reversal", close)

        return None

    # ------------------------------------------------------------------
    @staticmethod
    def snapshot(pos: Dict, bar: Dict, ind: Optional[Dict],
                 index_ctx: Optional[Dict], now: datetime) -> Dict:
        """
        The per-bar record of an open trade. Cheap to write, impossible to
        reconstruct later, and the only way to test an exit rule against
        trades that actually happened rather than trades a backtest imagined.
        """
        entry = float(pos["entry_price"])
        R = float(pos.get("risk_per_share") or 0) or 1e-9
        close = float(bar.get("close", entry)) if bar else entry
        high = float(bar.get("high", close)) if bar else close
        low = float(bar.get("low", close)) if bar else close
        mfe = max(float(pos.get("mfe_price", entry)), high)
        mae = min(float(pos.get("mae_price", entry)), low)
        pos["mfe_price"], pos["mae_price"] = mfe, mae       # carried on the position
        vwap = (ind or {}).get("vwap")
        rec = {
            "time": str(now),
            "ticker": pos["ticker"],
            "strategy": pos.get("strategy"),
            "bars_held": pos.get("bars_held", 0),
            "entry": entry,
            "close": close,
            "r_now": round((close - entry) / R, 3),
            "r_mfe": round((mfe - entry) / R, 3),
            "r_mae": round((mae - entry) / R, 3),
            "stop": pos.get("stop_loss"),
            "vwap": round(float(vwap), 2) if vwap is not None and np.isfinite(vwap) else None,
            "above_vwap": (close >= vwap) if vwap is not None and np.isfinite(vwap) else None,
            "rsi": (ind or {}).get("rsi"),
            "rvol": (ind or {}).get("rvol"),
            "nifty_move_since_entry": (
                round((index_ctx["nifty"] / index_ctx["nifty_at_entry"] - 1) * 100, 3)
                if index_ctx and index_ctx.get("nifty") and index_ctx.get("nifty_at_entry")
                else None),
        }
        return rec


def build_exit_context(trader, data, index_df, today):
    """
    {"indicators": {ticker: {vwap, rsi, rvol}}, "index": {"nifty": close}}
    for the names that are open or pending. Anything the exit rules or the
    per-bar record might read is computed here, once, from the same data the
    strategy sees - never re-derived inside the trader.
    """
    ti = TechnicalIndicators()
    want = set(trader.state.get("positions", {})) | set(trader.state.get("pending_orders", {}))
    ind = {}
    for t in want:
        df = data.get(t)
        if df is None or df.empty:
            continue
        d = df[df.index.date == today]
        if d.empty:
            continue
        try:
            vols = df["volume"].tail(MOMENTUM["rvol_lookback_bars"])
            med = float(vols.median())
            ind[t] = {
                "vwap": float(ti.vwap(d).iloc[-1]),
                "rsi": round(float(ti.rsi(df, MOMENTUM["rsi_period"]).iloc[-1]), 1),
                "rvol": round(float(d["volume"].iloc[-1]) / med, 2) if med > 0 else None,
            }
        except Exception as e:
            logging.getLogger(__name__).debug(f"{t}: exit context failed: {e}")
    idx = {}
    if index_df is not None and not index_df.empty:
        it = index_df[index_df.index.date == today]
        if not it.empty:
            idx["nifty"] = float(it["close"].iloc[-1])
    return {"indicators": ind, "index": idx}
