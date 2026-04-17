#!/usr/bin/env bash
# Run all three historical backtests sequentially.
# Each generates a separate PNG in data/backtest_reports/.
#
# Usage:
#   ./run_backtests.sh                        # all three
#   ./run_backtests.sh --portfolio ANET,COIN  # highlight holdings in all three
#
# First run for gfc / full_cycle will trigger backfill of historical
# price data for 500 stocks — this takes 30-90 min depending on cache state.
# Subsequent runs use the cache and are much faster.

set -euo pipefail

PORTFOLIO_ARG=""
if [[ $# -gt 0 && "$1" == "--portfolio" && -n "${2:-}" ]]; then
    PORTFOLIO_ARG="--portfolio $2"
    echo "Portfolio overlay: $2"
fi

PYTHON="${PYTHON:-python3}"
RUN="$PYTHON -m src.research.run --backtest-visual $PORTFOLIO_ARG"

total_start=$(date +%s)

run_period() {
    local period="$1"
    local label="$2"
    local t0
    t0=$(date +%s)
    echo ""
    echo "============================================================"
    echo "  Running: $label  (--period $period)"
    echo "  Started: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "============================================================"
    $RUN --period "$period"
    local elapsed=$(( $(date +%s) - t0 ))
    echo "  ✅ Done in $(( elapsed / 60 ))m $(( elapsed % 60 ))s"
}

run_period "recent"      "Recent 3 Years"
run_period "gfc"         "Global Financial Crisis (2005–2012)"
run_period "full_cycle"  "Full Multi-Regime History (2004–now)"

total_elapsed=$(( $(date +%s) - total_start ))
echo ""
echo "============================================================"
echo "  All backtests complete"
echo "  Total time: $(( total_elapsed / 60 ))m $(( total_elapsed % 60 ))s"
echo "  Reports saved to: data/backtest_reports/"
echo "============================================================"
ls -lh data/backtest_reports/*.png 2>/dev/null || true
