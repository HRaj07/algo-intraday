import json

summary = {
    "title": "3-Year Intraday Algo Trading Backtest & Deep Quantitative Analysis",
    "period": "August 2023 – August 2026 (730 Calendar Days / 724 Trading Days)",
    "universe": "Nifty 50 Large-Cap Equities (NSE)",
    "resolution": "Hourly Intraday Candles + Multi-Timeframe Daily Regimes",
    "friction_costs": "₹50 per trade (Brokerage + STT + Turnover + Stamp Duty + Slippage)",
    "risk_per_trade": "₹2,000 fixed slot risk with 1R Breakeven Stop",
    "outcomes": {
        "pure_orb": {
            "strategy": "Unfiltered Institutional ORB (15-min to 1-hour breakout)",
            "trades": 1440,
            "win_rate": 45.9,
            "net_pnl": -126028,
            "pf": 0.80,
            "annual_pnl": {"2023": -14698, "2024": -43022, "2025": -30423, "2026": -37886}
        },
        "filtered_orb": {
            "strategy": "Trend & Regime-Filtered ORB (ADX > 20, 200/20 SMA Filter, Max 1 Trade/Day)",
            "trades": 333,
            "win_rate": 49.2,
            "net_pnl": -12138,
            "pf": 0.92,
            "annual_pnl": {"2024": -12506, "2025": -644, "2026": 1012}
        },
        "short_term_60d": {
            "strategy": "15-Minute Institutional ORB with Dynamic Trailing Stop (Last 60 Days)",
            "trades": 58,
            "win_rate": 53.4,
            "net_pnl": 17261,
            "pf": 1.25,
            "period": "May 2026 – Aug 2026"
        }
    }
}

with open("/Users/harshit/Documents/StreamFab/algo-intraday/backtest_3yr_summary.json", "w") as f:
    json.dump(summary, f, indent=2)

print("Saved backtest_3yr_summary.json")
