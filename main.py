"""
Intraday Algo Trading Bot - Main Entry Point
Runs every 15 minutes via GitHub Actions (9:15 AM - 3:15 PM IST)
Strategies: ORB, VWAP Pullback, EMA 9/21 Crossover
Mode: Paper Trading (no real money)
"""
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
import urllib.request

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/intraday.log", mode="a"),
    ],
)
logger = logging.getLogger(__name__)

Path("logs").mkdir(exist_ok=True)
Path("reports").mkdir(exist_ok=True)

from config import INTRADAY_UNIVERSE, SYSTEM
from data.fetcher import IntradayFetcher, TechnicalIndicators
from strategies.orb import ORBStrategy
from strategies.vwap_pullback import VWAPPullbackStrategy
from strategies.ema_cross import EMACrossStrategy
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
        logger.warning(f"Discord failed: {e}")


def run_intraday_scan():
    """Main intraday scan - runs every 15 minutes."""
    now = datetime.now()
    logger.info(f"=" * 60)
    logger.info(f"Intraday Scan | {now.strftime('%Y-%m-%d %H:%M IST')}")
    logger.info(f"=" * 60)

    # Check if market is open
    if now.weekday() >= 5:  # Weekend
        logger.info("Weekend - Market closed")
        return

    market_start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    market_end = now.replace(hour=15, minute=30, second=0, microsecond=0)
    square_off = now.replace(hour=15, minute=15, second=0, microsecond=0)

    if not (market_start <= now <= market_end):
        logger.info("Outside market hours")
        return

    # Fetch live 15-min data
    fetcher = IntradayFetcher()
    logger.info(f"Fetching data for {len(INTRADAY_UNIVERSE)} stocks...")
    data_dict = fetcher.get_today_data(INTRADAY_UNIVERSE)
    logger.info(f"Got data for {len(data_dict)} stocks")

    if not data_dict:
        logger.warning("No data received")
        return

    # Get latest prices for exit checks
    current_prices = {t: df['close'].iloc[-1] for t, df in data_dict.items()}

    # Initialize paper trader
    trader = PaperTrader()

    # Check exits first
    exits = trader.check_exits(current_prices)
    for ex in exits:
        icon = "✅" if ex['pnl'] > 0 else "❌"
        msg = (
            f"{icon} **EXIT** `{ex['ticker']}` | {ex['direction']}\n"
            f"Entry: ₹{ex['entry_price']} → Exit: ₹{ex['exit_price']}\n"
            f"P&L: **₹{ex['pnl']:+.0f}** ({ex['pnl_pct']:+.2f}%) | Reason: {ex['reason']}\n"
            f"Strategy: `{ex['strategy']}`"
        )
        send_discord(msg)

    # Square off time? Stop looking for new entries
    if now >= square_off:
        logger.info("Square-off time reached - no new entries")
        _save_daily_summary(trader)
        return

    # Run all 3 strategies to find entries
    all_signals = []
    all_signals += ORBStrategy().compute_signals(data_dict)
    all_signals += VWAPPullbackStrategy().compute_signals(data_dict)
    all_signals += EMACrossStrategy().compute_signals(data_dict)

    logger.info(f"Signals found: {len(all_signals)}")

    # Paper trade entries
    new_entries = []
    for signal in all_signals:
        position = trader.enter_trade(signal)
        if position:
            new_entries.append((signal, position))

    # Send Discord alerts for new entries
    for signal, pos in new_entries:
        icon = "🟢" if signal['signal'] == 'BUY' else "🔴"
        msg = (
            f"{icon} **ENTRY** `{signal['ticker']}` | {signal['signal']}\n"
            f"Price: ₹{signal['price']} | Stop: ₹{signal['stop_loss']} | Target: ₹{signal['target']}\n"
            f"Strategy: `{signal['strategy']}` | Time: {signal.get('time', 'N/A')[-8:-3]}"
        )
        send_discord(msg)

    # Summary
    summary = trader.get_summary()
    logger.info(f"Portfolio: Cash=₹{summary['cash']:,.0f} | Open Pos={summary['open_positions']} | Total P&L=₹{summary['total_pnl']:+,.0f} | Win Rate={summary['win_rate_pct']}%")

    # Log signal data
    log_entry = {
        "time": str(now),
        "signals": all_signals,
        "entries": len(new_entries),
        "exits": len(exits),
        "portfolio": summary,
    }
    with open("logs/signals_intraday.json", "a") as f:
        f.write(json.dumps(log_entry) + "\n")


def _save_daily_summary(trader: PaperTrader):
    """Save end-of-day summary."""
    summary = trader.get_summary()
    today = datetime.now().date()
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
        webhook = os.environ.get("DISCORD_WEBHOOK_URL")
        if webhook:
            send_discord(msg)

    logger.info(f"EOD Summary: {day_summary}")


if __name__ == "__main__":
    run_intraday_scan()
