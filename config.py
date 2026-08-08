"""
Intraday Algo Trading System - Configuration
"""

# Top 20 most liquid NSE F&O stocks for intraday
INTRADAY_UNIVERSE = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS",
    "SBIN.NS", "AXISBANK.NS", "KOTAKBANK.NS", "LT.NS", "BHARTIARTL.NS",
    "BAJFINANCE.NS", "MARUTI.NS", "TATAMOTORS.NS", "WIPRO.NS", "HCLTECH.NS",
    "ADANIPORTS.NS", "TITAN.NS", "SUNPHARMA.NS", "NTPC.NS", "POWERGRID.NS",
]

# Use NSE Nifty 50 ETF as index proxy
NIFTY_ETF = "NIFTYBEES.NS"

SYSTEM = {
    "mode": "paper",
    "initial_capital": 500_000,  # 5 Lakhs paper capital
    "currency": "INR",
    "timezone": "Asia/Kolkata",
    "market_open": "09:15",
    "market_close": "15:30",
    "square_off_time": "15:15",  # Square off all positions by 3:15 PM
}

# Transaction costs (realistic NSE intraday costs)
COSTS = {
    "brokerage_per_order": 20,      # Zerodha flat fee
    "stt_pct": 0.00025,             # 0.025% on sell side only
    "exchange_charges_pct": 0.0000345,
    "sebi_charges_pct": 0.000001,
    "gst_on_brokerage": 0.18,       # 18% GST on brokerage
    "slippage_pct": 0.001,          # 0.1% slippage estimate
}

# Strategy 1: Opening Range Breakout
ORB = {
    "range_minutes": 15,            # First 15-min candle = opening range
    "volume_multiplier": 1.5,       # Volume must be 1.5x 5-day avg first candle volume
    "vwap_filter": True,            # Only long above VWAP, short below VWAP
    "risk_reward": 2.0,             # Target = 2x stop loss
    "max_range_pct": 0.03,          # Skip if opening range > 3% (too volatile)
    "min_range_pct": 0.002,         # Skip if opening range < 0.2% (too tight)
    "max_positions": 3,
    "position_size_pct": 0.15,      # 15% of capital per trade
}

# Strategy 2: VWAP Pullback + Supertrend
VWAP_PULLBACK = {
    "supertrend_period": 10,
    "supertrend_mult": 3.0,
    "vwap_touch_pct": 0.002,        # Price within 0.2% of VWAP = "at VWAP"
    "min_trend_bars": 4,            # Trend must have been established for 4+ bars
    "stop_atr_mult": 1.5,           # Stop = 1.5x ATR from VWAP
    "target_atr_mult": 2.5,
    "max_positions": 2,
    "position_size_pct": 0.15,
    "start_after_minutes": 45,      # Only after 10:00 AM (45 min after open)
}

# Strategy 3: EMA 9/21 Crossover
EMA_CROSS = {
    "fast_ema": 9,
    "slow_ema": 21,
    "trend_ema": 200,               # Only longs above 200 EMA, shorts below
    "volume_multiplier": 1.5,       # Volume confirmation
    "stop_pct": 0.008,              # 0.8% stop loss
    "target_pct": 0.015,            # 1.5% target
    "max_positions": 3,
    "position_size_pct": 0.12,
    "start_after_minutes": 30,      # Only after 9:45 AM
}

REPORTING = {
    "report_dir": "reports",
    "log_dir": "logs",
}
