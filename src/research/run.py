"""
Research pipeline CLI entry point.

Usage:
    # IC analysis on S&P 500 (takes ~30-60 min first time, cached after)
    python -m src.research.run --ic-analysis

    # Walk-forward out-of-sample validation
    python -m src.research.run --walk-forward

    # Screen full S&P 500 for top opportunities
    python -m src.research.run --screen
    python -m src.research.run --screen --top 20
    python -m src.research.run --screen --sector Technology
    python -m src.research.run --screen --direction up

All commands are independent of the daily report pipeline.
"""

from __future__ import annotations

import argparse

from src.universe.sp500 import get_sp500_tickers
from src.utils.logger import get_logger

logger = get_logger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Quant research pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument("--ic-analysis",  action="store_true", help="Run IC analysis on universe")
    parser.add_argument("--walk-forward", action="store_true", help="Run walk-forward OOS validation")
    parser.add_argument("--screen",       action="store_true", help="Screen S&P 500 for opportunities")

    # IC analysis options
    parser.add_argument("--years",   type=int, default=2,    help="Years of history to use (default: 2)")
    parser.add_argument("--universe", default="sp500",        help="Universe: sp500 (default)")

    # Screener options
    parser.add_argument("--top",       type=int, default=10,  help="Number of top stocks to show (default: 10)")
    parser.add_argument("--sector",    default=None,           help="Filter by sector name")
    parser.add_argument("--direction", default=None,           help="Filter: 'up' or 'down'")

    args = parser.parse_args()

    if not any([args.ic_analysis, args.walk_forward, args.screen]):
        parser.print_help()
        return

    tickers = get_sp500_tickers()
    logger.info(f"Universe: {len(tickers)} S&P 500 tickers")

    if args.ic_analysis:
        from src.research.ic_analysis import run_ic_analysis
        run_ic_analysis(tickers, years=args.years)

    if args.walk_forward:
        from src.research.ic_analysis import walk_forward_validation
        walk_forward_validation(tickers, years=args.years)

    if args.screen:
        from src.research.screener import run_screen
        run_screen(
            top_n=args.top,
            sector_filter=args.sector,
            direction_filter=args.direction,
        )


if __name__ == "__main__":
    main()
