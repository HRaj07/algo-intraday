# algo-intraday v2

v2 is now the live path. `main.py` is the v2 runner, so the GitHub Actions
workflow is **unchanged** — same `python main.py` entry point, same
`'7,22,37,52 3-10 * * 1-5'` cron, still scanning every 15 minutes.

Full reasoning is in the review document. This is the operational summary.

## The diagnosis in one line

The strategy is gross-profitable (**+₹5,821** over 49 paper trades) and loses to
friction (**−₹17,138**). The backtest that selected every live parameter charged
**₹50** per round trip when the real figure is **₹421**.

## What changed

| File | Status | What it does |
|---|---|---|
| `costs.py` | new | Single source of truth for NSE costs. Imported by the live engine *and* the backtester so they can never disagree again. |
| `tzutil.py` | new | One definition of "now", on stdlib `zoneinfo`. v1 logged August trades in UTC and September in IST. |
| `config.py` | replaced | Cost-aware parameters, exposure caps, circuit breakers. Old version kept as `config_v1.py`. |
| `main.py` | replaced | Prices cached before exits; exits before entries; triggers before scanning. Old version kept as `main_v1.py`. |
| `strategies/vwap_mr_v2.py` | new | Regime gate, RVOL/gap/liquidity filters, reversal trigger, ATR stop, cost hurdle. |
| `engine/risk_manager.py` | new | Real capital model, sector limits, daily/weekly loss limits, drawdown halt, PF kill switch. |
| `engine/paper_trader_v2.py` | new | Fixes the silent square-off failure, the loss-locking breakeven trail, the missing time stop, the wrong win rate. Adds partial exits. |
| `backtest_honest.py` | new | Real costs, real cash ledger, next-bar trigger fills, walk-forward split, selection-bias haircut. |
| `tests/test_pipeline.py` | new | 44 assertions, synthetic data, no network. |
| `data/fetcher.py` | patched | `yfinance` imported lazily so indicators work offline. |

v1 files are left in place and still import. State lives in
`logs/paper_state_v2.json`, so v1's history stays intact for comparison.

## Run the tests

```bash
python tests/test_pipeline.py     # 44 passed, 0 failed — no network needed
```

Each v1 bug has a test that fails if it comes back. The square-off test calls
`check_exits({})` with **no market data at all** and asserts the position still
closes — that is the bug that left three positions open overnight.

## The five changes that matter most

1. **Widen the stop.** 0.55% → 1.2×ATR (floor 0.8%, cap 2.2%). Notional is
   `risk ÷ stop%`, so a tighter stop is a *larger* position paying *more* cost
   for identical rupee risk. Cuts friction 58%, changes your risk by nothing.
2. **Never trade a fragment.** Minimum ₹60,000 notional, or skip. v1's sub-₹2L
   trades won 6.7% of the time.
3. **Add a regime gate.** No dip-buying while NIFTY is below its own VWAP. v1
   had no index awareness and opened 5 losing positions on 15 Sep.
4. **Require a reversal trigger.** Rest a stop-limit buy above the signal bar's
   high instead of buying a falling bar's close — a price you cannot get.
5. **Fix square-off.** A missing price must never mean an unmanaged position.

## Expect far fewer trades

`STRATEGY["min_vwap_deviation"]` is **derived**, not chosen. It falls out of the
cost hurdle:

```
D >= (min_rr × stop_floor + cost_pct) / (1 + t2_overshoot/2)
```

which currently gives **1.22%**. v1 used 0.6%, loosened from 0.8% "to fire on
15min bars" — generating signals that could never pay for themselves. Change
`min_reward_risk_after_cost` or `stop_pct_floor` and the deviation threshold
follows automatically; they can no longer drift apart.

If NSE large caps rarely dislocate 1.2% from VWAP, this strategy rarely has an
opportunity worth taking. That is a finding about the market, not a knob to turn.

## Before risking money

```bash
python backtest_honest.py --data cache_15m.pkl --index nifty_15m.pkl
python backtest_honest.py --data cache_15m.pkl --index nifty_15m.pkl --sweep
```

Sanity check first: confirm the **old** config comes out negative on the training
window under the real cost model. If it doesn't, the cost analysis is wrong and
everything downstream needs revisiting.

**Go-live bar:** positive on the validation window after real costs, t > 2,
≥ 200 trades, max drawdown < 8%.

## Caveats

- **No v2 parameter has been backtested.** The session that produced this had no
  market-data access. Parameters are reasoned from mechanism and literature.
  `backtest_honest.py` exists so you can do the validation yourself.
- The test suite proves the *plumbing* works, not that the strategy is
  profitable. Its synthetic data is engineered to revert; the P&L it prints is
  not a performance claim.
- The 13:15 entry cutoff has p = 0.078 and the threshold was chosen after seeing
  the data. Adopted for its mechanism — a trade needs time to work before
  square-off — not as a proven edge.
- The RVOL ceiling of 2.0 is a starting point, untestable from the current log
  because entry volume was never recorded.
