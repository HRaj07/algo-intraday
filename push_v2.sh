#!/usr/bin/env bash
#
# Push the v2 rebuild to GitHub.
#
# Run this from inside your local clone of HRaj07/algo-intraday, after copying
# the v2 files in. It refuses to push if the test suite fails.
#
#   chmod +x push_v2.sh
#   ./push_v2.sh
#
set -euo pipefail

BRANCH="v2-cost-aware-rebuild"
REMOTE="${REMOTE:-origin}"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die()  { printf '\n\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- sanity
[ -d .git ] || die "not a git repository - cd into your algo-intraday clone first"

REPO_URL="$(git remote get-url "$REMOTE" 2>/dev/null || true)"
case "$REPO_URL" in
  *algo-intraday*) ;;
  "") die "no remote named '$REMOTE'" ;;
  *) die "remote '$REMOTE' is $REPO_URL, which does not look like algo-intraday" ;;
esac

for f in costs.py tzutil.py config.py main.py backtest_honest.py \
         engine/risk_manager.py engine/paper_trader_v2.py \
         strategies/vwap_mr_v2.py tests/test_pipeline.py; do
  [ -f "$f" ] || die "missing $f - copy the v2 files into this clone first"
done

# ---------------------------------------------------------------- gate 1: tests
say "Running the test suite (44 assertions, no network needed)"
python3 tests/test_pipeline.py > /tmp/algo_v2_tests.log 2>&1 || {
  tail -25 /tmp/algo_v2_tests.log
  die "tests failed - not pushing. Full log: /tmp/algo_v2_tests.log"
}
tail -3 /tmp/algo_v2_tests.log

# ---------------------------------------------------------------- gate 2: cadence
say "Confirming the 15-minute scan cadence is untouched"
grep -q "cron: '7,22,37,52 3-10 \* \* 1-5'" .github/workflows/intraday_scan.yml \
  || die "the workflow cron changed - v2 must not alter the scan schedule"
grep -q "run: python main.py" .github/workflows/intraday_scan.yml \
  || die "the workflow entry point changed - it must stay 'python main.py'"
echo "  cron unchanged: every 15 min (:07 :22 :37 :52), UTC 3-10, Mon-Fri"
echo "  entry point unchanged: python main.py"

# ---------------------------------------------------------------- branch
say "Creating branch $BRANCH"
git fetch "$REMOTE" --quiet || true
if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
  git checkout "$BRANCH"
else
  git checkout -b "$BRANCH"
fi

# ---------------------------------------------------------------- commit
say "Staging the v2 rebuild"
git add -A \
  costs.py tzutil.py config.py config_v1.py main.py main_v1.py \
  backtest_honest.py README_V2.md push_v2.sh \
  engine/risk_manager.py engine/paper_trader_v2.py \
  strategies/vwap_mr_v2.py data/fetcher.py tests/

if git diff --staged --quiet; then
  echo "  nothing to commit - already up to date"
else
  git commit -F - <<'MSG'
Rebuild around transaction costs: fix the sizing, exits and validation

The strategy was gross-profitable and net-negative. Across 49 closed paper
trades: gross +Rs5,821, friction -Rs17,138, net -Rs11,317.

Root cause: profit_max_sweep.py - the file that selected every live parameter -
charges a flat Rs50 per round trip. The real median is Rs421. Re-pricing that
sweep's own 540 trades honestly turns its advertised +Rs1,44,564 into roughly
-Rs56,000. The edge was the cost error.

Cost and sizing
- costs.py is now the single source of truth, imported by the live engine and
  the backtester, so they can never diverge again. Adds the missing stamp duty
  and applies GST to the full base.
- Stop widened from a flat 0.55% to 1.2xATR (floor 0.8%, cap 2.2%). Notional is
  risk/stop%, so a tighter stop is a LARGER position paying MORE cost for the
  same rupee risk. The old config called 0.55% "max capital efficiency"; it was
  the most expensive stop width available. Cuts friction ~58%.
- Risk is now 0.4% of equity, not a fixed Rs2,000 that keeps betting the same
  size all the way down.
- Hard caps added: 2 concurrent positions, 3 entries/day, 1 per sector, 2.0x
  gross exposure, 1% of bar volume, Rs60,000 minimum notional.
- A trade that cannot be funded at full size is now SKIPPED. The old fallback
  (shrink to leftover cash) produced 15 fragment trades that won 6.7% of the
  time; at qty=1 the fixed brokerage alone swamps any move.

Engine bugs
- check_exits() returned early when a ticker was missing from that scan's data,
  so the position was never checked for stop, target or square-off. 26 runs
  logged "No today data received" and three positions survived overnight, one
  for 2.8 days. Square-off is now clock-driven and unconditional, backed by a
  last-known-price cache.
- The breakeven trail moved the stop to entry+0.1R while friction was 0.27R,
  guaranteeing -0.17R. It is now computed from the cost model at runtime.
- Added a time stop. 27 of 49 trades (55%) died at the closing bell for an
  average of -Rs137, pinning capital in trades whose thesis had already failed.
- Win rate counted entries, not closed trades, so open positions diluted it.

Strategy
- Entry is now a stop-limit trigger above the signal bar's high. The old code
  bought the close of a still-forming bar - a price you cannot transact at -
  while price was still falling.
- Added a NIFTY regime gate; it fails closed when index data is missing. The old
  code had no index awareness and opened 5 losing positions on 2026-09-15.
- Added RVOL, gap and turnover filters. A breakout strategy wants high relative
  volume; a mean-reversion strategy must avoid it, because high RVOL means
  information is arriving and you do not fade information.
- Added a cost hurdle: reject any signal below 1.5:1 reward-to-risk after costs.
  The old geometry was 0.91:1, needing a 52.5% win rate to break even while
  running 32.7%.
- min_vwap_deviation is now DERIVED from the cost hurdle (1.22%) rather than
  hand-tuned, so the two filters cannot drift apart. Expect far fewer trades.
- Universe cut 40 -> 25 on liquidity. Four of the five worst P&L contributors
  were event-driven names that a reversion strategy should not be fading.

Validation
- backtest_honest.py: real costs, a real cash ledger, next-bar trigger fills,
  train/validate/test split, and an explicit selection-bias haircut. The old
  sweep quoted a best-of-N profit factor as an estimate of future performance.
- tests/test_pipeline.py: 44 assertions on synthetic data, no network. Every bug
  above has a test that fails if it returns.

Unchanged: the 15-minute scan cadence and the `python main.py` entry point.
v1 files are kept (config_v1.py, main_v1.py) and still import.

NOT YET VALIDATED: no v2 parameter has been backtested. Run the walk-forward
before this goes anywhere near money. Go-live bar: positive on the validation
window after real costs, t > 2, >= 200 trades, max drawdown < 8%.
MSG
  echo "  committed"
fi

# ---------------------------------------------------------------- push
say "Pushing to $REMOTE/$BRANCH"
git push -u "$REMOTE" "$BRANCH"

SLUG="$(echo "$REPO_URL" | sed -E 's#.*github.com[:/]##; s#\.git$##')"
say "Done"
cat <<EOF
  Open a PR:  https://github.com/$SLUG/compare/$BRANCH?expand=1
  Or with gh: gh pr create --fill --base main --head $BRANCH

  NOTE: the scanner workflow auto-commits to main every 15 minutes, so merge
  with a rebase or a squash-merge rather than a long-lived branch.
EOF
