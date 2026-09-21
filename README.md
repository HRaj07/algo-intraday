# AlgoTrade India — Intraday Bot

Automated intraday **paper** trading on NSE equities, running on GitHub Actions.
No server, no laptop needed. Scans every 15 minutes during market hours.

> **Status: rebuilt, not yet validated.**
> v1 ran live on paper for 49 trades and lost ₹11,317. The cause was diagnosed
> and fixed (see below), but **no v2 parameter has been backtested**. Treat this
> as a system under test, not a working edge. The validation gate is at the
> bottom of this file, and nothing here should touch a live broker until it clears.

---

## Why v1 lost money

This is the whole story, and it is worth understanding before changing anything.

Across 49 closed paper trades, v1 produced **+₹5,821 of gross profit** and paid
**₹17,138 in friction**, netting **−₹11,317**. The strategy was picking winners.
It just could not afford to trade.

The reason nobody noticed: the research code and the live code used different
cost models. `profit_max_sweep.py` — the file whose output chose every parameter
in `config.py` — charged a flat **₹50** per round trip. The live engine's actual
median was **₹421**. Every parameter was therefore selected in a world where
trading was seven times cheaper than reality. Re-pricing that sweep's own headline
result at real costs turns its advertised **+₹1,44,564 into roughly −₹56,000**.

The edge was the error.

### The mechanism that made it worse

Position size is not a free choice. Fix your rupee risk and your stop, and notional
follows:

```
notional = risk_in_rupees / stop_%
```

At ₹2,000 risk and v1's 0.55% stop, that is **₹3.64 lakh of stock per trade** — on
a ₹5 lakh account. And since most costs scale with notional, a *tighter* stop means
a *bigger* position paying *more* cost for identical risk:

| Stop | Notional | Friction | As % of ₹2,000 risk | Breakeven win rate |
|---|---|---|---|---|
| 0.55% (v1) | ₹3,63,636 | ₹535 | 26.7% | 52.8% |
| 1.00% | ₹2,00,000 | ₹315 | 15.8% | 48.2% |
| 1.50% | ₹1,33,333 | ₹226 | 11.3% | 46.4% |
| 2.50% | ₹80,000 | ₹155 | 7.7% | 44.9% |

v1's config described its 0.55% stop as *"Ultra Tight Stop, Max Capital Efficiency"*.
Of every available stop width, it was the one that maximised cost per unit of risk.
After costs its trades were **0.91 : 1** reward-to-risk, needing a **52.5% win rate**
to return zero. It ran 32.7%.

---

## What changed in v2

### Cost and sizing

| | v1 | v2 | Why |
|---|---|---|---|
| Cost model | two, disagreeing 7× | one, `costs.py`, shared | The backtest and the bot must price trades identically |
| Stop | flat 0.55% | 1.2×ATR, floor 0.8%, cap 2.2% | Cuts friction ~58%; normalises risk across ITC and TATASTEEL |
| Risk/trade | fixed ₹2,000 | 0.4% of equity | Fixed rupees keeps betting the same size all the way down |
| Missing charges | no stamp duty, GST on brokerage only | full statutory set | v1 understated its own costs |

### Position and portfolio limits

v1 had none of these. One trade consumed 73% of the account; the next four were
silently downsized to whatever cash remained, some to **qty = 1**. Those fragments
won 6.7% of the time — at qty 1 the ₹47 fixed brokerage swamps any move.

| | v1 | v2 |
|---|---|---|
| Concurrent positions | 5 | 2 |
| Entries per day | 5 (only ~1.5 fundable) | 3 |
| Per sector | unlimited | 1 |
| Gross exposure | 3.6× equity | 2.0× equity |
| Minimum notional | none | ₹60,000 — **below this, skip the trade** |
| Share of bar volume | none | 1% |

### Circuit breakers

v1 had zero. Its worst day was −₹4,178 and it drew down 4.0% in 21 trading days
with nothing that would ever have stopped it.

- Daily loss limit: −2R
- Weekly loss limit: −6R
- Drawdown halt: −6% equity
- Kill switch: rolling 30-trade profit factor below 0.85 → stop, manual restart

### Engine bugs fixed

1. **Square-off could fail silently.** `check_exits()` skipped any position whose
   ticker was missing from that scan's data — not checking its stop, target *or*
   square-off. 26 runs logged *"No today data received"* and **three positions
   survived overnight**, one for 2.8 days. Square-off is now clock-driven and
   unconditional, backed by a last-known-price cache.
2. **The "breakeven" trail locked in a loss.** It moved the stop to entry + 0.1R
   while friction was 0.27R, guaranteeing −0.17R. Now computed from the cost model.
3. **No time stop.** 27 of 49 trades (55%) died at the closing bell averaging −₹137,
   pinning capital in trades whose thesis had already failed.
4. **Win rate counted entries, not closed trades**, so open positions diluted it.
5. **Equity accounting** (found post-merge): `equity()` added full notional to cash
   that had only lost the 20% margin, inflating equity by ₹2.4L on a ₹3L position.
   `equity_peak` latched onto the fake high and the drawdown halt fired permanently
   on the first close. Entry costs were also charged twice, and partial exits
   stranded margin.

### Strategy logic

- **Entry is now a trigger, not a prediction.** v1 bought the close of a
  still-forming bar while price was falling — a price you cannot actually transact
  at. v2 rests a stop-limit buy above the signal bar's high: price must turn up and
  take you in. If it keeps falling, no fill, no loss.
- **Market regime gate.** No dip-buying while NIFTY sits below its own VWAP. v1 had
  no index awareness and opened five positions on 2026-09-15, losing all five. The
  gate **fails closed** — no index data means no trading.
- **Don't fade news.** Relative volume ceiling, opening-gap filter, turnover floor.
  A breakout strategy *wants* high RVOL; a mean-reversion strategy must avoid it,
  because high RVOL means information is arriving.
- **Cost hurdle.** Reject any signal below 1.5 : 1 reward-to-risk *after* costs.
- **Scale out at VWAP**, then trail the remainder by 1.2×ATR.
- **Universe cut 40 → 25** on liquidity. Four of v1's five worst P&L contributors
  were event-driven names a reversion strategy should never have been fading.

### Expect far fewer trades

`STRATEGY["min_vwap_deviation"]` is **derived, not chosen**:

```
D >= (min_rr × stop_floor + cost_pct) / (1 + t2_overshoot/2)     →  1.22%
```

v1 used 0.6%, loosened from 0.8% "to fire on 15min bars" — producing signals that
could never pay for themselves, which is precisely how it lost money. Change
`min_reward_risk_after_cost` or `stop_pct_floor` and the threshold follows
automatically; they cannot drift apart.

If NSE large caps rarely dislocate 1.22% from VWAP, this strategy rarely has an
opportunity worth taking. **That is a finding about the market, not a knob to turn.**

---

## How it runs

GitHub Actions fires `python main.py` every 15 minutes, `'7,22,37,52 3-10 * * 1-5'`
UTC — 32 runs a day, Mon–Fri. Each in-market run fetches data, caches prices, checks
exits and squares off. Only runs inside **09:45–13:15 IST** can open a position.

| Window | What happens |
|---|---|
| Before 09:45 | Scan and manage only — opening auction noise |
| 09:45–13:15 | Full scan, entries allowed |
| 13:15–15:05 | Manage open positions, no new entries |
| 15:05 | Unconditional square-off |

Square-off is 15:05, not 15:15, because Zerodha's own MIS auto-square-off runs at
15:12 (CAS) / 15:25 and fills at market regardless of your price.

### Local commands

```bash
python tests/test_pipeline.py    # 51 assertions, synthetic data, no network
python check_data.py             # is ^NSEI actually fetching?
python main.py                   # one scan by hand
```

`check_data.py` matters more than it sounds. The regime gate fails closed, so a
broken index feed and a genuinely quiet market produce **identical logs**. If the
bot goes silent for days, run it before assuming the filters are just being strict.

---

## Files

| File | Role |
|---|---|
| `main.py` | Entry point. Prices → exits → fills → scan, in that order |
| `config.py` | Every parameter, each with its justification in a comment |
| `costs.py` | **Single source of truth for costs.** Imported by engine and backtester |
| `strategies/vwap_mr_v2.py` | Regime gate, filters, trigger, cost hurdle |
| `engine/risk_manager.py` | Sizing, exposure caps, circuit breakers |
| `engine/paper_trader_v2.py` | Orders, fills, exits, ledger |
| `data/fetcher.py` | yfinance wrapper and indicators |
| `backtest_honest.py` | Real costs, real cash ledger, walk-forward split |
| `tests/test_pipeline.py` | Every bug above has a test that fails if it returns |
| `check_data.py` | Data health check |
| `tzutil.py` | One definition of "now" (v1 logged a month in UTC by accident) |

Two ledgers, deliberately separate: `logs/paper_state.json` is v1's frozen history
(49 trades, −₹11,317); `logs/paper_state_v2.json` is live, starting fresh at ₹5L.

Superseded v1 research scripts live in `_local_archive/` (gitignored) and in git
history. To retrieve one: `git checkout <sha> -- profit_max_sweep.py`.

---

## Before this touches real money

```bash
python backtest_honest.py --data cache_15m.pkl --index nifty_15m.pkl
python backtest_honest.py --data cache_15m.pkl --index nifty_15m.pkl --sweep
```

**Sanity check first:** confirm the *old* config comes out **negative** on the
training window under the real cost model. If it doesn't, the cost analysis above
is wrong and everything downstream needs revisiting.

Then walk forward: tune on 2023–24, validate on 2025, leave 2026 untouched until
the config is frozen. Re-running the test window turns it into training data.

**Go-live bar**, written down in advance so it cannot be negotiated later:

- net positive on the validation window **after real costs**
- t-statistic > 2
- at least 200 trades
- maximum drawdown < 8%

Anything short of that is a story, not an edge.

### Known caveats

- **No v2 parameter has been backtested.** They are reasoned from mechanism and
  from published research, not fitted.
- The 13:15 entry cutoff has permutation **p = 0.078** on n=49, and the threshold
  was chosen *after* looking at the data. Adopted for its mechanism — a trade needs
  time to work before square-off — not as a validated edge.
- The RVOL ceiling of 2.0 is a starting point; v1's logs never recorded entry
  volume, so it could not be tested.
- Slippage at 5 bps/side is an **assumption, not a measurement**. It is the largest
  single cost and the only negotiable one. Log intended price against actual fill
  and check.
- Context: SEBI found **71% of individual intraday equity traders lost money** in
  FY23. Fixing the arithmetic makes this system fair rather than rigged against
  you. Whether a real edge remains underneath is the open question.

### Rollback

v1's `main.py` and `config.py` are in `_local_archive/` and in git history:

```bash
git log --oneline --all       # find the pre-rebuild commit
git checkout <sha> -- main.py config.py
```

---

## Does it learn?

Yes, but only about **size** — and at a rate the evidence supports.

### What adapts: position size

`learning.py` scales risk per trade by two factors, hard-bounded to
**0.20%–0.60%** of equity:

- **Performance.** Expectancy in R, shrunk toward a prior by sample size:
  `posterior = (k·prior + n·observed) / (k + n)` with k = 50. So ten trades
  barely move it, two hundred move it a lot.
- **Drawdown.** Full size above the high-water mark, tapering to half size at a
  5% drawdown (the halt fires at 6%).

Every sizing decision is logged with both scalars and the reasoning, so any
position size can be reconstructed afterwards.

### What does NOT adapt: the rules

Entry criteria, exit criteria, filters and thresholds never self-adjust. They
change when a human reads the review and decides.

That line exists because crossing it is what killed v1. Every v1 parameter was a
reaction to observed results — RSI 25→28, deviation 0.8%→0.6%, stop 0.70%→0.55%,
shorts off, max trades 3→5 — each justified with a profit-factor number, all
fitted to a cost model that was 7× too low. Net outcome: −₹11,317. Automating
that loop runs the same mistake faster, without a human ever pausing to ask
whether the cost model was right.

Size is recoverable. A loosened filter that admits unprofitable trades is not.

### Why the shrinkage matters

With a 45% win rate, five losses in a row happen about **5% of the time** — you
should expect several per hundred trades from a perfectly good strategy. A system
that reacts to each one is learning superstition. And reacting to *wins* is worse:
it sizes up exactly when luck, not edge, produced them.

The sample sizes are unforgiving:

| To detect | Trades needed |
|---|---|
| 45% → 55% win rate (large) | 392 |
| 45% → 50% (modest) | 1,565 |
| 45% → 47% (realistic) | 9,738 |

At v2's expected rate of roughly 0.5 trades/day, 392 trades is about three years.
Anything adapting faster than that is fitting to noise, by construction.

### The review

```bash
python analyse_trades.py          # or --json
```

Breaks the trade record down by exit reason, entry hour, ticker, sector and
weekday — and prints a **verdict column** next to every row: `anecdote`,
`too few to act`, `not significant`, or `SIGNIFICANT`. A slice under 30 trades
is never actionable no matter how good its p-value looks.

It also runs the test that matters most: **gross-profitable but net-negative?**
That single question is what v1 failed, and it is checked first every time.

A weekly GitHub Action (`weekly_review.yml`) runs this on Saturday mornings and
posts the summary to Discord.
