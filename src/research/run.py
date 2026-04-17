"""
Research pipeline CLI entry point.

Usage:
    # IC analysis on S&P 500 (takes ~30-60 min first time, cached after)
    python -m src.research.run --ic-analysis

    # Walk-forward out-of-sample validation (~2-3 hours)
    python -m src.research.run --walk-forward

    # Screen full S&P 500 for top opportunities
    python -m src.research.run --screen
    python -m src.research.run --screen --top 20
    python -m src.research.run --screen --sector Technology
    python -m src.research.run --screen --direction up

    # Visualized historical backtest (PNG, strictly point-in-time signals)
    python -m src.research.run --backtest-visual
    python -m src.research.run --backtest-visual --period gfc
    python -m src.research.run --backtest-visual --period full_cycle
    python -m src.research.run --backtest-visual --period covid --portfolio ANET,COIN,META,MSFT
    python -m src.research.run --backtest-visual --start-date 2008-01-01 --end-date 2012-12-31

    Named periods available:
      recent       Last 3 years (~5 min)
      post_covid   2020–now, COVID crash + recovery (~10 min)
      covid        2018–2023 full COVID regime (~15 min)
      bull_2010s   2010–2019 post-GFC bull market (~25 min)
      gfc          2005–2012 Global Financial Crisis (~30 min, backfills cache)
      full_cycle   2004–now multi-regime (~60 min first run)
      dot_com      1998–2004 dot-com bust (~60 min first run)

    NOTE: --period overrides --years. First run for long periods fetches
    historical price data for 500 stocks — subsequent runs use cache.

All commands are independent of the daily report pipeline.
"""

from __future__ import annotations

import argparse

from src.universe.sp500 import get_sp500_tickers
from src.utils.logger import get_logger

logger = get_logger(__name__)

_VALID_PERIODS = [
    "recent", "post_covid", "covid", "bull_2010s", "gfc", "full_cycle", "dot_com"
]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Quant research pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # ── Commands ──────────────────────────────────────────────────────────────
    parser.add_argument("--ic-analysis",     action="store_true",
                        help="Run IC analysis on universe")
    parser.add_argument("--walk-forward",    action="store_true",
                        help="Run walk-forward OOS validation")
    parser.add_argument("--screen",          action="store_true",
                        help="Screen S&P 500 for opportunities")
    parser.add_argument("--backtest-visual", action="store_true",
                        help="Generate visualized backtest report (PNG, PIT signals)")

    # ── Shared options ────────────────────────────────────────────────────────
    parser.add_argument("--years",    type=int, default=3,
                        help="Years of history to use (default: 3; overridden by --period)")
    parser.add_argument("--universe", default="sp500",
                        help="Universe: sp500 (default)")

    # ── Screener options ──────────────────────────────────────────────────────
    parser.add_argument("--top",       type=int, default=10,
                        help="Number of top stocks to show (default: 10)")
    parser.add_argument("--sector",    default=None,
                        help="Filter by sector name")
    parser.add_argument("--direction", default=None,
                        help="Filter: 'up' or 'down'")

    # ── Backtest options ──────────────────────────────────────────────────────
    parser.add_argument("--portfolio", default=None,
                        help="Comma-separated symbols to highlight in backtest "
                             "(e.g. ANET,COIN,META,MSFT)")
    parser.add_argument(
        "--period",
        default=None,
        choices=_VALID_PERIODS,
        metavar="PERIOD",
        help=(
            "Named historical period for backtest. "
            f"Choices: {{{', '.join(_VALID_PERIODS)}}}. "
            "Overrides --years and sets start/end dates automatically."
        ),
    )
    parser.add_argument("--start-date", default=None,
                        help="Custom backtest start date (YYYY-MM-DD). "
                             "Overrides --period.")
    parser.add_argument("--end-date", default=None,
                        help="Custom backtest end date (YYYY-MM-DD). "
                             "Overrides --period.")

    args = parser.parse_args()

    if not any([args.ic_analysis, args.walk_forward, args.screen, args.backtest_visual]):
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

    if args.backtest_visual:
        from src.research.backtest_visual import run_backtest_visual

        portfolio_symbols = (
            [s.strip().upper() for s in args.portfolio.split(",")]
            if args.portfolio else None
        )

        # start_date/end_date override period if both are provided
        use_period     = args.period     if not (args.start_date or args.end_date) else None
        use_start_date = args.start_date
        use_end_date   = args.end_date

        out_path = run_backtest_visual(
            tickers,
            years=args.years,
            portfolio_symbols=portfolio_symbols,
            period=use_period,
            start_date=use_start_date,
            end_date=use_end_date,
        )
        print(f"\n✅ Backtest report saved to:\n   {out_path}\n")


if __name__ == "__main__":
    main()
