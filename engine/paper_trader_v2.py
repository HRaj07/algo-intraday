"""
Paper trading engine v2.

BUGS FIXED FROM v1 (each one cost real money in the log)
--------------------------------------------------------
1. SQUARE-OFF COULD SILENTLY FAIL. v1's check_exits() did
       bar_data = current_data.get(ticker)
       if not bar_data: continue
   so a position whose ticker was missing from that scan's data was simply never
   examined - not for its stop, not for its target, not for square-off. The log
   shows 26 runs that recorded "No today data received", and three positions
   that survived overnight, one for 2.8 days. In live MIS trading that is either
   a broker auto-square-off at market or an overnight gap on leverage.
   v2 keeps a last-known-price cache and squares off on the CLOCK, unconditionally.

2. "BREAKEVEN" TRAIL LOCKED IN A LOSS. v1 moved the stop to entry + 0.10R once
   price reached +1R. Round-trip friction at its 0.55% stop was 0.27R, so that
   rule guaranteed -0.17R. Its single trailing-stop exit duly lost Rs328.
   v2 computes the trail level from costs.py so breakeven means breakeven.

3. NO TIME STOP. 27 of v1's 49 trades (55%) were closed by the square-off clock
   at an average of -Rs137, holding capital hostage all afternoon for nothing.

4. WIN RATE WAS WRONG. total_trades incremented on ENTRY, winning_trades on EXIT,
   so the reported win rate was diluted by every open position.

5. ALL-OR-NOTHING EXITS. v2 scales half out at VWAP - where the mean-reversion
   thesis actually completes - and lets the rest run on a trailing stop.
"""
import json
import logging
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Dict, List, Optional


from config import SYSTEM, STRATEGY, RISK
from costs import one_side_cost, cost_in_rupees, VARIABLE_ROUNDTRIP_PCT, FIXED_ROUNDTRIP
from engine.risk_manager import RiskManager

from tzutil import IST as ist, now_ist, stamp
logger = logging.getLogger(__name__)


class PaperTraderV2:
    def __init__(self, state_file: str = "logs/paper_state_v2.json"):
        self.state_file = Path(state_file)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state = self._load()
        self.risk = RiskManager(self.state)

    def _load(self) -> Dict:
        if self.state_file.exists():
            try:
                return json.load(open(self.state_file))
            except Exception:
                logger.error("state file unreadable - refusing to start with a blank book")
                raise
        return {
            "cash": SYSTEM["initial_capital"],
            "initial_capital": SYSTEM["initial_capital"],
            "equity_peak": SYSTEM["initial_capital"],
            "positions": {},
            "pending_orders": {},
            "last_known_price": {},
            "trade_history": [],
            "daily_entry_count": {},
            "total_pnl": 0.0,
        }

    def _save(self):
        json.dump(self.state, open(self.state_file, "w"), indent=2, default=str)

    # ==================================================================
    # PRICE CACHE - the fix for the silent square-off failure
    # ==================================================================
    def update_prices(self, bars: Dict[str, Dict[str, float]], now: datetime):
        for t, b in bars.items():
            self.state["last_known_price"][t] = {
                "close": b["close"], "high": b["high"], "low": b["low"],
                "as_of": now.isoformat(),
            }

    def _price_for(self, ticker: str) -> Optional[Dict]:
        return self.state["last_known_price"].get(ticker)

    # ==================================================================
    # PENDING STOP-LIMIT ORDERS
    # ==================================================================
    def place_order(self, sig: Dict, now: datetime) -> bool:
        """v2 places a resting trigger rather than assuming a fill at the close
        of a bar that has not closed yet."""
        ok, why = self.risk.can_open(sig["ticker"], now)
        if not ok:
            logger.info(f"ORDER REJECTED {sig['ticker']}: {why}")
            return False
        self.state["pending_orders"][sig["ticker"]] = {
            **sig, "placed_at": now.isoformat(), "bars_alive": 0,
        }
        logger.info(
            f"ORDER {sig['ticker']} stop-limit buy @ Rs{sig['trigger_price']} "
            f"(SL Rs{sig['stop_loss']}, {sig['stop_pct']}%, R:R after cost {sig['rr_after_cost']})"
        )
        self._save()
        return True

    def process_orders(self, bars: Dict[str, Dict[str, float]], now: datetime) -> List[Dict]:
        """Fill pending triggers; expire stale ones."""
        filled = []
        for ticker, o in list(self.state["pending_orders"].items()):
            bar = bars.get(ticker)
            o["bars_alive"] += 1

            if o["bars_alive"] > o.get("valid_bars", 2):
                logger.info(f"ORDER EXPIRED {ticker} - price never reclaimed the trigger")
                del self.state["pending_orders"][ticker]
                continue

            if not bar:
                continue

            # Triggered when the bar trades through the trigger price.
            if bar["high"] >= o["trigger_price"]:
                fill = min(o["trigger_price"], o["limit_price"])
                pos = self._open(o, fill, now)
                if pos:
                    filled.append(pos)
                del self.state["pending_orders"][ticker]

        self._save()
        return filled

    def _open(self, o: Dict, fill_price: float, now: datetime) -> Optional[Dict]:
        qty, why = self.risk.size_position(fill_price, o["stop_loss"], o.get("bar_volume"))
        if qty is None:
            logger.info(f"SIZING SKIP {o['ticker']}: {why}")
            return None

        entry_cost = one_side_cost(fill_price, qty, "buy")
        margin = fill_price * qty * 0.20  # MIS ~5x on NSE equity

        # Only the MARGIN leaves cash here. The entry cost is carried on the
        # position and settled at close, where _close() subtracts it inside pnl.
        # The first cut deducted it in both places, double-charging every trade.
        self.state["cash"] -= margin

        pos = {
            "ticker": o["ticker"], "direction": "LONG",
            "entry_price": fill_price, "qty": qty, "qty_open": qty,
            "stop_loss": o["stop_loss"], "initial_stop": o["stop_loss"],
            "t1": o["t1"], "t2": o["t2"], "t1_fraction": o["t1_fraction"],
            "t1_done": False, "trailed": False,
            "risk_per_share": round(fill_price - o["stop_loss"], 2),
            "atr": o["atr"], "margin": margin, "entry_cost": entry_cost,
            "bars_held": 0, "realised_pnl": 0.0,
            "strategy": o["strategy"],
            "entry_time": stamp(now),

            # The entry snapshot, carried from the signal so it reaches the
            # trade record. rr_after_cost in particular is the system's own
            # PREDICTION for this trade; keeping it is what makes a calibration
            # check possible later. v1 discarded every such number, which is why
            # its 49 trades could not say whether any filter was earning its place.
            "predicted_rr": o.get("rr_after_cost"),
            "entry_rsi": o.get("entry_rsi"),
            "entry_deviation_pct": o.get("entry_deviation_pct"),
            "entry_atr_pct": o.get("entry_atr_pct"),
            "entry_rvol": o.get("entry_rvol"),
            "entry_gap_pct": o.get("entry_gap_pct"),
            "entry_turnover_cr": o.get("entry_turnover_cr"),
            "entry_stop_pct": o.get("stop_pct"),
        }
        self.state["positions"][o["ticker"]] = pos
        today = str(now.date())
        self.state["daily_entry_count"][today] = self.state["daily_entry_count"].get(today, 0) + 1
        logger.info(f"FILLED {o['ticker']} @ Rs{fill_price} | {why}")
        return pos

    # ==================================================================
    # EXITS
    # ==================================================================
    def check_exits(self, bars: Dict[str, Dict[str, float]], now: datetime) -> List[Dict]:
        exits = []
        sq_h, sq_m = map(int, SYSTEM["square_off_time"].split(":"))
        force = now.time() >= dtime(sq_h, sq_m)

        for ticker, pos in list(self.state["positions"].items()):
            bar = bars.get(ticker)

            if not bar:
                # THE v1 BUG. Do not `continue` here - a position with no fresh
                # data is MORE dangerous, not less. Fall back to last known
                # price and shout about it.
                cached = self._price_for(ticker)
                if cached:
                    logger.warning(
                        f"STALE DATA {ticker} - using last known price from "
                        f"{cached['as_of']}. Position is NOT unmanaged."
                    )
                    bar = cached
                elif force:
                    logger.error(
                        f"CANNOT PRICE {ticker} AT SQUARE-OFF. In live trading this "
                        f"position must be closed manually NOW."
                    )
                    continue
                else:
                    logger.warning(f"NO DATA {ticker} - holding, will force-close at square-off")
                    continue

            pos["bars_held"] += 1
            price, high, low = bar["close"], bar["high"], bar["low"]
            entry, R = pos["entry_price"], pos["risk_per_share"]

            # ---- 1. clock beats everything ----
            if force:
                exits.append(self._close(ticker, pos, price, "square_off", now, pos["qty_open"]))
                continue

            # ---- 2. stop ----
            if low <= pos["stop_loss"]:
                reason = "trailing_stop" if pos["trailed"] else "stop_loss"
                exits.append(self._close(ticker, pos, pos["stop_loss"], reason, now, pos["qty_open"]))
                continue

            # ---- 3. T1: scale half out at VWAP, where the thesis completes ----
            if not pos["t1_done"] and high >= pos["t1"]:
                part = max(1, int(pos["qty_open"] * pos["t1_fraction"]))
                exits.append(self._close(ticker, pos, pos["t1"], "t1_vwap", now, part, partial=True))
                pos["t1_done"] = True
                # Cost-aware breakeven: clear friction FIRST, then add a buffer.
                pos["stop_loss"] = round(entry + self._friction_R(pos) * R + 0.15 * R, 2)
                pos["trailed"] = True
                logger.info(
                    f"T1 {ticker} - half out at VWAP, stop to Rs{pos['stop_loss']} "
                    f"(a real breakeven, not v1's entry+0.1R which locked in -0.17R)"
                )
                if pos["qty_open"] <= 0:
                    continue

            # ---- 4. T2 ----
            if pos["t1_done"] and high >= pos["t2"]:
                exits.append(self._close(ticker, pos, pos["t2"], "t2_target", now, pos["qty_open"]))
                continue

            # ---- 5. ATR trail once T1 is banked ----
            if pos["trailed"]:
                trail = price - STRATEGY["trail_atr_mult_after_t1"] * pos["atr"]
                if trail > pos["stop_loss"]:
                    pos["stop_loss"] = round(trail, 2)

            # ---- 6. time stop ----
            # If a reversion trade has not started reverting in an hour, it is
            # not stretched - it is trending, and you are on the wrong side.
            if pos["bars_held"] >= STRATEGY["time_stop_bars"] and not pos["t1_done"]:
                r_now = (price - entry) / R if R > 0 else 0
                if r_now < STRATEGY["time_stop_min_R"]:
                    exits.append(self._close(
                        ticker, pos, price,
                        f"time_stop_{r_now:+.2f}R", now, pos["qty_open"]))
                    continue

        self.risk.update_peak()
        self._save()
        return exits

    def _friction_R(self, pos: Dict) -> float:
        """Round-trip friction expressed in R, for this specific position."""
        notional = pos["entry_price"] * pos["qty"]
        risk_rupees = pos["risk_per_share"] * pos["qty"]
        return cost_in_rupees(notional) / risk_rupees if risk_rupees > 0 else 0.0

    def _close(self, ticker: str, pos: Dict, exit_price: float, reason: str,
               now: datetime, qty: int, partial: bool = False) -> Dict:
        qty = min(qty, pos["qty_open"])
        exit_cost = one_side_cost(exit_price, qty, "sell")
        entry_cost_share = pos["entry_cost"] * (qty / pos["qty"])
        pnl = (exit_price - pos["entry_price"]) * qty - entry_cost_share - exit_cost

        # Release this slice's margin and DECREMENT what the position still holds,
        # or a partial exit leaves the full margin on the books and equity()
        # over-counts for the rest of the position's life.
        #
        # The fraction is against qty_open (what is still live), NOT the original
        # qty. Using the original strands margin on every scale-out: after T1 takes
        # half, margin is already halved, so a T2 exit of the remaining half would
        # release half of the half and quietly orphan the rest. On a Rs1.26L
        # position that was Rs6,300 per trade, which then read as a drawdown and
        # tripped the halt - the same failure wearing a different hat.
        qty_open_before = pos["qty_open"]
        released = pos["margin"] * (qty / qty_open_before) if qty_open_before else pos["margin"]
        pos["margin"] -= released
        self.state["cash"] += released + pnl
        self.state["total_pnl"] += pnl
        pos["qty_open"] -= qty
        pos["realised_pnl"] += pnl

        rec = {
            "ticker": ticker, "direction": "LONG",
            "entry_price": pos["entry_price"], "exit_price": round(exit_price, 2),
            "qty": qty, "pnl": round(pnl, 2),
            "R_multiple": round(pnl / (pos["risk_per_share"] * pos["qty"]), 2)
                          if pos["risk_per_share"] > 0 else 0,
            "reason": reason, "partial": partial,
            "friction": round(entry_cost_share + exit_cost, 2),
            "strategy": pos["strategy"],
            "entry_time": pos["entry_time"],
            "exit_time": stamp(now),
        }
        # Copy the entry snapshot onto every exit record, including partials, so
        # each realised outcome sits next to the conditions that produced it.
        for k in ("predicted_rr", "entry_rsi", "entry_deviation_pct",
                  "entry_atr_pct", "entry_rvol", "entry_gap_pct",
                  "entry_turnover_cr", "entry_stop_pct"):
            if pos.get(k) is not None:
                rec[k] = pos[k]
        self.state["trade_history"].append(rec)

        if pos["qty_open"] <= 0:
            del self.state["positions"][ticker]

        logger.info(
            f"EXIT {ticker} {qty}@Rs{exit_price} | P&L Rs{pnl:+,.0f} "
            f"({rec['R_multiple']:+.2f}R) | friction Rs{rec['friction']:,.0f} | {reason}"
        )
        return rec

    # ==================================================================
    def summary(self) -> Dict:
        h = self.state["trade_history"]
        closed = len(h)
        wins = sum(1 for t in h if t["pnl"] > 0)
        gp = sum(t["pnl"] for t in h if t["pnl"] > 0)
        gl = abs(sum(t["pnl"] for t in h if t["pnl"] <= 0))
        return {
            "equity": round(self.risk.equity(), 2),
            "cash": round(self.state["cash"], 2),
            "open_positions": len(self.state["positions"]),
            "pending_orders": len(self.state["pending_orders"]),
            "closed_trades": closed,                        # v1 counted entries here
            "win_rate_pct": round(wins / closed * 100, 1) if closed else 0.0,
            "profit_factor": round(gp / gl, 2) if gl > 0 else None,
            "total_friction": round(sum(t.get("friction", 0) for t in h), 2),
            "total_pnl": round(self.state["total_pnl"], 2),
            "return_pct": round(
                self.state["total_pnl"] / self.state["initial_capital"] * 100, 2),
        }
