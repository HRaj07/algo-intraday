# AlgoTrade India — Intraday Bot

Automated intraday **paper** trading on NSE equities, running on GitHub Actions.
No server, no laptop needed. Scans every 15 minutes during market hours.

> **Status: v3. Direction reversed after measurement. Expected to make roughly nothing.**
>
> v1 lost ₹11,317 over 49 paper trades because costs ate its winners. v2 rebuilt
> the cost model and the engine around the same mean-reversion idea. In September
> 2026 that idea was finally measured directly — **it was negative before costs**,
> so v2 was not a cost problem and could not be rescued by tuning.
>
> v3 trades the opposite signal, which is the only one on this market measured with
> the right sign. Its honestly expected return is **approximately zero**. It is
> running so that forward paper trades can test it on data no one has seen. Do not
> point this at a broker. The go-live bar is at the bottom of this file and nothing
> in v3 clears it.

---

## The short version

| | v1 | v2 | v3 |
|---|---|---|---|
| Idea | buy stretched **below** VWAP | same, with real costs | buy stretched **above** VWAP |
| RSI | < 28 | < 40 | **≥ 75** |
| Volume | ignored | ceiling at 2× | **floor at 4×** |
| Entry | close of a forming bar | stop-limit above the high | market, next bar's open |
| Stop | 0.55% flat | 1.2×ATR, floor 0.8% | 2.0×ATR, floor **1.2%** |
| Exits | target + trail | scale at VWAP, trail, time stop | **stop or square-off, nothing else** |
| Result | −₹11,317 live | negative before costs | ~breakeven, unproven |

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

## Why v2 was replaced

Every diagnosis up to this point said the same thing: the signal picks winners and
cannot afford them. That is a cost problem, and cost problems are fixable. It was
never tested directly, because the fix always seemed to be one layer further down.

On 2026-09-24 it was tested directly. `study_signal_edge.py` takes 185,847 real
15-minute bars across 210 NSE names and measures the forward return from the entry
condition with **no stops, no sizing, no targets and no costs** — just what price
does next.

| Held for | v2's setup (below VWAP, RSI < 40) |
|---|---|
| 15 minutes | +0.008% |
| 1 hour | −0.028% |
| 2 hours | −0.085% |
| to the close | **−0.137%** (t = −7.97, n = 4,546) |

It does not revert. It keeps falling, and further the longer it is held. Two
corollaries finished the idea off:

- **More oversold was worse.** −0.095% at 0.8–1.2% below VWAP against −0.004% at
  0.2–0.4%. The carefully derived deviation floor was steering toward the worst
  available bucket.
- **RSI did nothing.** Every bucket from 0–20 through 40–50 was negative and
  roughly equal, so no threshold in that family was ever going to work.

The confirmation trigger — v2's headline improvement — measured **−0.153%** against
−0.137% without it, and fired on 26% of setups. It made things worse.

### What the same data said to do instead

The mirror condition was the only positive result anywhere, and it was positive
under four unrelated features that all graded monotonically the same way. All
figures net of the 0.0955% round-trip cost, entered at a tradeable price:

| | weakest | → | → | strongest |
|---|---|---|---|---|
| RSI | 60–65 −0.126% | 65–70 −0.026% | 70–75 +0.017% | **75+ +0.147%** |
| relative volume | 1.2–2 −0.050% | 2–4 +0.014% | | **4+ +0.128%** |
| distance above VWAP | 0.8–1.2 0.000% | 1.2–1.8 +0.082% | | **1.8–3 +0.117%** |
| ATR | 0.4–0.6 −0.017% | 0.6–0.9 +0.062% | | **0.9+ +0.318%** |

One good bucket is luck. Four unrelated features each grading monotonically in the
same direction is a relationship. RSI ≥ 75, rvol ≥ 4 and dev ≥ 1.2% each cleared
cost in July, August **and** September independently.

### Why v3 is still expected to make nothing

Run as an account — 3 concurrent, 5 entries a day, real costs both sides —
`study_portfolio.py` gives:

```
165 trades · win rate 43.6% · PF 1.04 · net +₹12,787 (+1.28%) · t = +0.20
gross before costs  ₹93,712        friction paid  ₹80,925
top 5 trades        ₹88,676        without them   −₹75,889
```

**Friction is 86% of the gross edge**, and five trades out of 165 are the entire
result. `study_sweep.py` then ran 100 threshold combinations as full books:

```
0 of 100 cells positive in all three months AND still positive without their top 5
best t = +1.63 · median t = −1.47 · 0 cells above t = 2 · 49% made money at all
```

Zero survivors. The best t is the maximum of 100 tries on one period, which is worth
nothing. The breakeven trail lost money in **every** cell it appeared in — which is
why v3 has no trail, no target and no time stop: the edge is the right tail, and
each of those is a device for cutting the right tail off.

So v3 is not deployed because it is expected to be profitable. It is deployed
because it is the only signal here with the right sign, the 59 measured days are now
spent as in-sample, and forward paper trading is the only honest test left.

### The one structural lever not yet pulled

Friction is charged per trade regardless of holding period. A signal capturing 3%
over six days pays the same toll as one capturing 0.2% over four hours — a tenfold
change in the only ratio that matters. `study_swing_daily.py` tests that on five
years of daily bars with a **real train/test split**, which 60 days of 15-minute
data can never support. That test has not been run yet and it is the highest-value
thing left to do.

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
python tests/test_pipeline.py      # 123 assertions, synthetic data, no network
python check_data.py               # is ^NSEI actually fetching?
python main.py                     # one scan by hand
python backtest_recent.py          # replay the REAL strategy on real bars
python study_signal_edge.py        # measure the premise itself, no machinery
python study_swing_daily.py        # the untested question: does holding days work?
python walkforward.py --dry-run    # what would Sunday's job decide?
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
| `strategies/momentum_v3.py` | **The live strategy.** Long strength, market entry, stop-or-square-off |
| `strategies/vwap_mr_v2.py` | The control case. Kept runnable so the studies that condemned it reproduce |
| `walkforward.py` | Weekly refit on held-out data. Adopts nothing that fails the holdout |
| `study_signal_edge.py` | Forward returns from the raw condition. This is what killed v2 |
| `study_momentum.py` | The mirror finding, re-tested at tradeable prices |
| `study_selection.py` | Grades RSI / volume / deviation / ATR for a tradeable subset |
| `study_candidate.py` | The month-robust cuts, alone and combined, with stops |
| `study_portfolio.py` | The candidate as a real book — where +0.255%/signal became t = +0.20 |
| `study_sweep.py` | 100 threshold cells, each a full book. Zero survivors |
| `study_swing_daily.py` | Daily bars, 5 years, real train/test split. **Not yet run** |
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

- **v3 does not clear that bar and is not close.** Its measured account-level
  t-statistic is +0.20 on 165 trades. It is running to generate out-of-sample
  evidence, not because it passed anything.
- **Every v3 threshold was chosen after looking at the 59 days it was scored on.**
  There is no holdout at 15-minute resolution because yfinance serves 60 days and
  no more. The forward paper trades are the first genuine out-of-sample data.
- **The edge, such as it is, lives in the right tail.** Removing the best five
  trades turned every one of 100 sweep cells negative. A run of ordinary results
  followed by nothing is the expected shape, not a malfunction.
- **No v2 parameter was ever backtested either**, and v2's premise turned out to be
  negative before costs. Reasoning from mechanism is not the same as measuring.
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

### What adapts weekly, and why it cannot cheat

`walkforward.py` runs every Sunday at 07:30 IST — market shut, no positions open,
any change landing before Monday's first scan. It may move **four thresholds**
(distance above VWAP, RSI floor, volume floor, stop width), each inside a
hard-coded band, and **nothing else**. Direction, exits, risk per trade, the caps,
the universe and the cost model are not reachable from it.

The difference between this and an optimiser is the ordering:

1. Fit on everything **except** the last two weeks.
2. A candidate only qualifies if it was positive in every month of the fit window
   **and** still positive with its best five trades removed.
3. Score the survivor on the held-out two weeks it has never seen.
4. Adopt only if it cleared cost there **and** beat what is already running.
5. Write the decision down **before** the period it applies to trades.

Step 5 is the point. Every record in `research/walkforward.jsonl` also carries what
the frozen original parameters would have scored on the same holdout, so after a
couple of months the file answers the question almost no self-tuning bot can:
**did the retuning help?** If refitting never beats leaving the parameters alone,
the job says so in its own output and the honest move is to switch it off.

Run against real data today, it adopted nothing — no cell survived the
tail-independence check. That is the filter working, and most weeks should end
there.

### What still does NOT adapt

The direction of the trade, every exit rule, risk per trade, the position caps and
the cost model. Those change when a human reads the evidence and decides.

That line exists because crossing it is what killed v1. Every v1 parameter was a
reaction to observed results — RSI 25→28, deviation 0.8%→0.6%, stop 0.70%→0.55%,
shorts off, max trades 3→5 — each justified with a profit-factor number, all
fitted to a cost model that was 7× too low. Net outcome: −₹11,317.

It nearly happened again on 2026-09-24: stacking filters until the number looked
good produced **+0.353% net at t = +5.72** that was **−0.236% in September**. The
holdout in `walkforward.py` exists specifically to catch that, because the mistake
is not hypothetical — it was made here, in the analysis that produced v3.

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
