"""
Intraday Algo Trading Bot - Main Production Execution
Runs every 15 minutes via GitHub Actions (9:15 AM - 3:15 PM IST)
Primary Strategy: 30-Minute Institutional ORB with ADX & VWAP Multi-factor Filter
Risk Sizing: Fixed ₹2,000 max risk per trade with 1:2.0 Asymmetric R:R and +1R Breakeven Trailing Stop
"""
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
import urllib.request

# Create directories FIRST before logging setup (GitHub Actions clean checkout)
Path("logs").mkdir(exist_ok=True)
Path("reports").mkdir(exist_ok=True)

# Setup logging
import pytz

ist = pytz.timezone("Asia/Kolkata")

class ISTFormatter(logging.Formatter):
    """Logging formatter that converts timestamps to IST (Asia/Kolkata)."""
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=pytz.utc).astimezone(ist)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat()

formatter = ISTFormatter(
    fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.handlers.clear()

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
stream_handler.setLevel(logging.INFO)
root_logger.addHandler(stream_handler)

file_handler = logging.FileHandler("logs/intraday.log", mode="a")
file_handler.setFormatter(formatter)
file_handler.setLevel(logging.INFO)
root_logger.addHandler(file_handler)

# Silence noisy third-party library logs (also re-enables yfinance multithreading,
# which yfinance disables when it detects DEBUG-level logging)
logging.getLogger("yfinance").setLevel(logging.WARNING)
logging.getLogger("peewee").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


from config import INTRADAY_UNIVERSE, SYSTEM
from data.fetcher import IntradayFetcher
from engine.paper_trader import PaperTrader


def send_discord(message: str):
    """Send notification to Discord."""
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        logger.debug("Discord webhook not set")
        return
    try:
        payload = json.dumps({
            "username": "IntraDay Bot 🇮🇳",
            "content": message,
        }).encode("utf-8")
        req = urllib.request.Request(webhook_url, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "Mozilla/5.0")
        urllib.request.urlopen(req, timeout=10)
        logger.info("Discord notification sent")
    except Exception as e:
        logger.warning(f"Discord alert failed: {e}")


def run_intraday_scan():
    """Main intraday scan - runs every 15 minutes."""
    import pytz
    ist = pytz.timezone("Asia/Kolkata")
    now = datetime.now(ist)
    logger.info("=" * 60)
    logger.info(f"Intraday Scan | {now.strftime('%Y-%m-%d %H:%M IST')}")
    logger.info("=" * 60)

    # Weekend check
    if now.weekday() >= 5:
        logger.info("Weekend - NSE closed")
        return

    market_start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    market_end = now.replace(hour=15, minute=30, second=0, microsecond=0)
    square_off = now.replace(hour=15, minute=15, second=0, microsecond=0)

    if not (market_start <= now <= market_end):
        logger.info("Outside NSE trading hours")
        return

    # Fetch live 15-min data
    fetcher = IntradayFetcher()
    logger.info(f"Fetching 15m data for {len(INTRADAY_UNIVERSE)} stocks...")
    all_data = fetcher.fetch_intraday(INTRADAY_UNIVERSE, days_back=5)
    
    today = now.date()
    today_data = {t: df[df.index.date == today] for t, df in all_data.items() if not df[df.index.date == today].empty}
    logger.info(f"Received valid intraday data for {len(today_data)} tickers")

    if not today_data:
        logger.warning("No today data received")
        return

    # Build price map for positions check
    current_bars = {}
    for t, df in today_data.items():
        last = df.iloc[-1]
        current_bars[t] = {
            "close": last["close"],
            "high": last["high"],
            "low": last["low"]
        }

    trader = PaperTrader()

    # 1. Manage open positions & check exits
    exits = trader.check_exits(current_bars)
    for ex in exits:
        icon = "✅" if ex['pnl'] > 0 else "❌"
        msg = (
            f"{icon} **EXIT** `{ex['ticker']}` | {ex['direction']}\n"
            f"Entry: ₹{ex['entry_price']} → Exit: ₹{ex['exit_price']}\n"
            f"P&L: **₹{ex['pnl']:+.0f}** ({ex['pnl_pct']:+.2f}%) | Reason: `{ex['reason']}`\n"
            f"Strategy: `{ex['strategy']}`"
        )
        send_discord(msg)

    # 2. If square-off time reached, force-close all open positions then save EOD
    if now >= square_off:
        logger.info("Square-off time reached (3:15 PM) - Force closing all open positions")
        # Force-close any remaining open positions at current market price
        square_off_exits = trader.check_exits(current_bars)
        for ex in square_off_exits:
            icon = "✅" if ex['pnl'] > 0 else "❌"
            msg = (
                f"{icon} **SQUARE-OFF** `{ex['ticker']}` | {ex['direction']}\n"
                f"Entry: ₹{ex['entry_price']} → Exit: ₹{ex['exit_price']}\n"
                f"P&L: **₹{ex['pnl']:+.0f}** ({ex['pnl_pct']:+.2f}%) | Reason: `square_off`\n"
                f"Strategy: `{ex['strategy']}`"
            )
            send_discord(msg)
        _save_daily_summary(trader)
        return

    # 3. Scan for VWAP Mean Reversion signals ONLY
    # ORB was removed — 730-day backtest showed ORB PF=0.73 (consistently unprofitable)
    # VWAP MR long-only (RSI 28, 0.55% stop, 1.3x VWAP target, max 5/day) = 58.2% WR, PF 1.70, +₹1.40L / 3yr
    from strategies.vwap_mr import ExtremeVWAPMeanReversionStrategy
    mr_strat = ExtremeVWAPMeanReversionStrategy()
    signals = mr_strat.compute_signals(all_data)
    signals = sorted(signals, key=lambda x: x.get("score", 0), reverse=True)[:SYSTEM["max_daily_trades"]]
    logger.info(f"VWAP MR Signals Found: {len(signals)}")

    # 4. Enter trades with strict risk management
    new_entries = []
    for sig in signals:
        pos = trader.enter_trade(sig)
        if pos:
            new_entries.append((sig, pos))

    for sig, pos in new_entries:
        icon = "🟢" if sig['signal'] == 'BUY' else "🔴"
        msg = (
            f"{icon} **ENTRY** `{sig['ticker']}` | {sig['signal']}\n"
            f"Price: ₹{sig['price']} | Stop Loss: ₹{sig['stop_loss']} | Target: ₹{sig['target']}\n"
            f"Qty: {pos['qty']} (Risk: ₹{SYSTEM['risk_per_trade']:,}) | ADX: {sig.get('adx', 'N/A')}\n"
            f"Strategy: `{sig['strategy']}` | Time: {sig.get('time', 'N/A')[-8:-3]}"
        )
        send_discord(msg)

    summary = trader.get_summary()
    logger.info(
        f"Portfolio Summary: Cash=₹{summary['cash']:,.0f} | Open={summary['open_positions']} | "
        f"Total P&L=₹{summary['total_pnl']:+,.0f} | Win Rate={summary['win_rate_pct']}%"
    )

    # Log entry
    log_entry = {
        "time": str(now),
        "signals": signals,
        "entries": len(new_entries),
        "exits": len(exits),
        "portfolio": summary,
    }
    with open("logs/signals_intraday.json", "a") as f:
        f.write(json.dumps(log_entry) + "\n")


def _save_daily_summary(trader: PaperTrader):
    import pytz
    ist = pytz.timezone("Asia/Kolkata")
    summary = trader.get_summary()
    today = datetime.now(ist).date()
    history = trader.state.get("trade_history", [])
    today_trades = [t for t in history if str(today) in t.get("exit_time", "")]
    today_pnl = sum(t["pnl"] for t in today_trades)

    day_summary = {
        "date": str(today),
        "today_trades": len(today_trades),
        "today_pnl": round(today_pnl, 2),
        "today_winners": sum(1 for t in today_trades if t["pnl"] > 0),
        "total_pnl": summary["total_pnl"],
        "win_rate_pct": summary["win_rate_pct"],
        "return_pct": summary["return_pct"],
    }

    with open("logs/daily_summary.json", "a") as f:
        f.write(json.dumps(day_summary) + "\n")

    if today_trades:
        sign = "+" if today_pnl >= 0 else ""
        msg = (
            f"📊 **IntraDay Bot — EOD Summary** ({today})\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Today Trades : {len(today_trades)} | Winners: {day_summary['today_winners']}\n"
            f"Today P&L    : **₹{sign}{today_pnl:,.0f}**\n"
            f"Total Return : **{sign}{summary['return_pct']}%** (since inception)\n"
            f"Win Rate     : {summary['win_rate_pct']}%\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        send_discord(msg)

    logger.info(f"EOD Summary: {day_summary}")


if __name__ == "__main__":
    run_intraday_scan()
