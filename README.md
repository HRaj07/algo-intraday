# ⚡ AlgoTrade India — Intraday Bot

Fully automated intraday paper trading system running on **GitHub Actions** — free, serverless, no laptop needed.

## Live Strategy

**Extreme VWAP Mean Reversion** (`strategies/vwap_mr.py`) is the only strategy currently wired
into `main.py`. A 730-day backtest showed the previous ORB engine was structurally unprofitable
(PF 0.73-0.92 across filters), so it was retired from live trading; `strategies/orb.py` and
`ORB_CONFIG` are kept in the repo for research only.

| Strategy | Status | Win Rate | Profit Factor | Notes |
|---|---|---|---|---|
| Extreme VWAP Mean Reversion (RSI 28/72) | **Live** | 55.4% | 1.41 | 0.55% stop, 1.3x VWAP overshoot target |
| Opening Range Breakout + CPR | Research only | 45-49% | 0.73-0.92 | Not profitable enough over 3yr, disabled in `main.py` |

### 2026-08 tuning pass
A 3-year parameter sweep (Aug 2023 – Aug 2026, hourly bars, 15 NSE large caps, ₹2,000 risk/trade,
₹50/trade friction) compared the strategy's exit rules head-to-head:

| Config | Trades | Win Rate | PF | Net P&L |
|---|---|---|---|---|
| Old: 0.70% stop, exit exactly at VWAP (1.0x) | 540 | 58.3% | 1.31 | +₹90,640 |
| **New: 0.55% stop, 1.3x VWAP overshoot target** | 540 | 55.4% | **1.41** | **+₹1,44,564** |

Letting winners run 30% past the VWAP line (instead of exiting on touch) and tightening the stop
from 0.70% to 0.55% raised net profit ~60% while staying net-positive in every single backtested
year (2023, 2024, 2025, 2026). `max_daily_trades` was also raised from 2 to 3 to capture more of
the diversified signal flow without materially hurting the profit factor. See `config.py` →
`VWAP_MR_CONFIG` / `SYSTEM` for the exact values, and `profit_max_sweep.py` for the sweep harness.

## Schedule
Runs every 15 minutes from **9:15 AM to 3:15 PM IST** on weekdays (`.github/workflows/intraday_scan.yml`).
