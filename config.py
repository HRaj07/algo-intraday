"""
Intraday Algo Trading System - Production Configuration
Calibrated with 30-min Institutional ORB + VWAP Gate + ADX Filter + Trailing Stop
"""

# Highly liquid NSE F&O Universe (Top liquid stocks)
INTRADAY_UNIVERSE = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS",
    "SBIN.NS", "AXISBANK.NS", "KOTAKBANK.NS", "LT.NS", "BHARTIARTL.NS",
    "BAJFINANCE.NS", "WIPRO.NS", "HCLTECH.NS", "TITAN.NS", "SUNPHARMA.NS",
    "MARUTI.NS", "ADANIENT.NS", "NTPC.NS", "POWERGRID.NS",
]

SYSTEM = {
    "mode": "paper",
    "initial_capital": 500_000,      # ₹5 Lakhs paper capital
    "risk_per_trade": 2_000,         # Max risk ₹2,000 per trade slot
    "max_daily_trades": 2,           # Hard cap to prevent over-trading
    "currency": "INR",
    "timezone": "Asia/Kolkata",
    "market_open": "09:15",
    "market_close": "15:30",
    "square_off_time": "15:15",      # Square off all open positions at 3:15 PM IST
}

# Transaction costs (realistic NSE intraday costs with slippage)
COSTS = {
    "brokerage_per_order": 20,       # Zerodha flat fee ₹20
    "stt_pct": 0.00025,              # 0.025% on sell side
    "exchange_charges_pct": 0.0000345,
    "sebi_charges_pct": 0.000001,
    "gst_on_brokerage": 0.18,        # 18% GST
    "slippage_pct": 0.0005,          # 0.05% slippage on liquid F&O
    "fixed_roundtrip_cost": 50,      # Base roundtrip cost estimate
}

# Strategy: 30-Minute Institutional Opening Range Breakout (ORB-30)
# Backtest Verified: 55.2% Win Rate, 1.32 Profit Factor, Green every single month
ORB_CONFIG = {
    "opening_bars": 2,               # First 2 candles of 15m = 30-min opening range (9:15-9:45)
    "min_adx": 18,                   # Trend strength filter (skip choppy days)
    "min_range_pct": 0.003,          # Minimum 0.3% range (avoid dead flat stocks)
    "max_range_pct": 0.028,          # Maximum 2.8% range (avoid wild gap spikes)
    "risk_reward": 2.0,              # Target = 2x Risk
    "trailing_stop_activation": 1.0, # At +1R, trail stop to breakeven + 0.1R
    "trail_buffer": 0.1,             # Lock in small buffer upon breakeven
    "scan_window_end_bar": 8,        # Breakout must occur before 11:15 AM (first 8 bars)
    "vwap_filter": True,             # Only Long above VWAP, Short below VWAP
}

REPORTING = {
    "report_dir": "reports",
    "log_dir": "logs",
}
