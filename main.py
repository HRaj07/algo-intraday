"""
Intraday bot v2 - main loop.

Order of operations per scan, which differs from v1 in ways that matter:
  1. refresh the price cache FIRST, so exits never run blind
  2. manage exits BEFORE looking for new entries
  3. fill any pending triggers from the previous bar
  4. only then scan for new signals, behind the regime gate
"""
import json
import logging
import os
import sys
import urllib.request
from datetime import datetime, time as dtime
from pathlib import Path

from tzutil import IST as ist, UTC, now_ist

Path("logs").mkdir(exist_ok=True)
Path("reports").mkdir(exist_ok=True)


class ISTFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=UTC).astimezone(ist)
        return dt.strftime(datefmt) if datefmt else dt.isoformat()


_h = logging.StreamHandler()
_h.setFormatter(ISTFormatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
                             "%Y-%m-%d %H:%M:%S"))
_f = logging.FileHandler("logs/intraday_v2.log", mode="a")
_f.setFormatter(_h.formatter)
logging.basicConfig(level=logging.INFO, handlers=[_h, _f])
for noisy in ("yfinance", "peewee", "urllib3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

from config import INTRADAY_UNIVERSE, SYSTEM, INDEX_TICKER, VIX_TICKER
from data.fetcher import IntradayFetcher
from engine.paper_trader_v2 import PaperTraderV2
from strategies.vwap_mr_v2 import VWAPMeanReversionV2


def notify(msg: str):
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        return
    try:
        req = urllib.request.Request(
            url, data=json.dumps({"username": "IntraDay v2", "content": msg}).encode(),
            method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "Mozilla/5.0")
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        logger.warning(f"discord failed: {e}")


def main():
    now = now_ist()
    logger.info("=" * 64)
    logger.info(f"Scan | {now:%Y-%m-%d %H:%M IST}")

    if now.weekday() >= 5:
        logger.info("weekend - NSE closed")
        return
    if not (dtime(9, 15) <= now.time() <= dtime(15, 30)):
        logger.info("outside NSE hours")
        return

    fetcher = IntradayFetcher()
    symbols = INTRADAY_UNIVERSE + [INDEX_TICKER, VIX_TICKER]
    data = fetcher.fetch_intraday(symbols, days_back=20)   # 20d: enough for RVOL baseline

    index_df = data.pop(INDEX_TICKER, None)
    vix_df = data.pop(VIX_TICKER, None)
    vix = float(vix_df["close"].iloc[-1]) if vix_df is not None and not vix_df.empty else None

    today = now.date()
    bars = {}
    for t, df in data.items():
        d = df[df.index.date == today]
        if d.empty:
            continue
        last = d.iloc[-1]
        bars[t] = {"close": float(last["close"]), "high": float(last["high"]),
                   "low": float(last["low"]), "volume": float(last["volume"])}

    trader = PaperTraderV2()

    # 1. cache prices BEFORE anything else - this is what makes square-off safe
    trader.update_prices(bars, now)

    # 2. exits, unconditionally, even if today's fetch came back thin
    for ex in trader.check_exits(bars, now):
        icon = "OK" if ex["pnl"] > 0 else "X"
        notify(f"[{icon}] EXIT {ex['ticker']} {ex['qty']}@Rs{ex['exit_price']} | "
               f"Rs{ex['pnl']:+,.0f} ({ex['R_multiple']:+.2f}R) | {ex['reason']}")

    sq_h, sq_m = map(int, SYSTEM["square_off_time"].split(":"))
    if now.time() >= dtime(sq_h, sq_m):
        s = trader.summary()
        logger.info(f"EOD: {s}")
        notify(f"EOD | equity Rs{s['equity']:,.0f} | P&L Rs{s['total_pnl']:+,.0f} | "
               f"WR {s['win_rate_pct']}% | PF {s['profit_factor']} | "
               f"friction paid Rs{s['total_friction']:,.0f}")
        return

    # 3. fill triggers resting from the previous bar
    for pos in trader.process_orders(bars, now):
        notify(f"FILLED {pos['ticker']} @Rs{pos['entry_price']} qty {pos['qty']} | "
               f"SL Rs{pos['stop_loss']} T1 Rs{pos['t1']}")

    # 4. new signals, behind the regime gate
    halted, why = trader.risk.trading_halted(now)
    if halted:
        logger.warning(f"NOT TRADING: {why}")
        notify(f"HALTED: {why}")
        return

    strat = VWAPMeanReversionV2()
    for sig in strat.compute_signals(data, index_df, vix):
        if not trader.place_order(sig, now):
            continue

    s = trader.summary()
    logger.info(f"equity Rs{s['equity']:,.0f} | open {s['open_positions']} | "
                f"pending {s['pending_orders']} | closed {s['closed_trades']} | "
                f"WR {s['win_rate_pct']}% | PF {s['profit_factor']} | "
                f"friction Rs{s['total_friction']:,.0f}")

    with open("logs/scan_v2.jsonl", "a") as f:
        f.write(json.dumps({"time": str(now), "summary": s}) + "\n")


if __name__ == "__main__":
    main()
