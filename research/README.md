# research/

Machine-written. Nothing in this folder is edited by hand.

| File | Written by | When | What it is |
|---|---|---|---|
| `walkforward.jsonl` | `walkforward.py` | Sunday 07:30 IST | One line per weekly decision: the parameters proposed, what they scored on the fit window, what they scored on the two held-out weeks, what the *incumbent* would have scored on the same weeks, and whether anything was adopted. Written **before** the week it applies to trades. |
| `replay_recent.txt` | `backtest_recent.py` | Saturday 08:30 IST | The live strategy replayed on the last 60 days of real bars |
| `signal_edge.txt` | `study_signal_edge.py` | Saturday | Forward returns from the raw entry condition, no machinery |
| `momentum.txt` | `study_momentum.py` | Saturday | The mirror finding at tradeable prices, split by month and by name |
| `selection.txt` | `study_selection.py` | Saturday | RSI / volume / deviation / ATR graded against forward return |
| `candidate.txt` | `study_candidate.py` | Saturday | The month-robust cuts, alone and combined, with stops |
| `portfolio.txt` | `study_portfolio.py` | Saturday | The candidate as a real book under the portfolio caps |
| `exits.txt` | `study_exits.py` | Saturday | Nine exit rules on identical entries |
| `sweep.txt` | `study_sweep.py` | Saturday | 100 threshold cells, each a full book |
| `swing_daily.txt` | `study_swing_daily.py` | Saturday | Daily bars, five years, **real train/test split** |
| `LAST_RUN.txt` | the workflow | Saturday | Timestamp, commit, active strategy, any overrides in force |

## How to read it

Every study prints its own "what this does not tell you" block at the end.
Read that before the numbers. The recurring caveat is the same one throughout
this repo: 60 days of 15-minute bars is the whole window yfinance serves, so
none of the intraday studies has a holdout. `swing_daily.txt` is the exception
and is the only file here that can validate rather than suggest.

`walkforward.jsonl` is the file that matters most over time. After roughly
eight weekly entries it answers whether refitting the thresholds has beaten
leaving them alone — and if it has not, the honest move is to disable the
Sunday job.
