"""
Stock screener — runs QuantForecaster across the full S&P 500 universe
and returns the top-ranked stocks by composite signal strength.

Runs independently from the daily portfolio report.
Outputs a ranked list that can be emailed or saved to a report.

Usage:
    python -m src.research.run --screen                 # top 10
    python -m src.research.run --screen --top 20        # top 20
    python -m src.research.run --screen --sector XLK    # tech only
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from src.data.fetcher import MarketDataFetcher
from src.forecast.base import ForecastResult
from src.forecast.quant import QuantForecaster
from src.universe.sp500 import get_sp500_tickers, get_ticker_sector, SECTOR_ETF_MAP
from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ScreenerResult:
    rank:          int
    symbol:        str
    sector:        str
    direction:     str
    magnitude_pct: float
    recommendation: str
    confidence:    str
    composite_score: float   # raw score before thresholding
    narrative:     str


def run_screen(
    top_n: int = 10,
    sector_filter: str | None = None,    # e.g. "Technology"
    direction_filter: str | None = None, # "up" | "down" | None (all)
    min_confidence: str = "medium",
    exclude_symbols: list[str] | None = None,
) -> list[ScreenerResult]:
    """
    Run quant signals across S&P 500 and return top_n ranked stocks.

    Args:
        top_n:            How many stocks to return
        sector_filter:    Only screen stocks in this sector
        direction_filter: Only return "up" or "down" signals (None = both)
        min_confidence:   Minimum confidence level ("low", "medium", "high")
        exclude_symbols:  Symbols to exclude (e.g. already in portfolio)

    Returns:
        List of ScreenerResult sorted by |composite_score| descending
    """
    fetcher    = MarketDataFetcher()
    forecaster = QuantForecaster()

    # Build universe
    all_tickers = get_sp500_tickers()
    if sector_filter:
        all_tickers = [
            t for t in all_tickers
            if get_ticker_sector(t) == sector_filter
        ]
    if exclude_symbols:
        exclude_set = set(exclude_symbols)
        all_tickers = [t for t in all_tickers if t not in exclude_set]

    logger.info(
        f"Screening {len(all_tickers)} stocks"
        + (f" [{sector_filter}]" if sector_filter else "")
        + f" → top {top_n}"
    )

    # Prefetch price data for universe (uses cache, only downloads missing)
    fetcher.prefetch(all_tickers, years=2)

    # Run forecaster on entire universe
    results = forecaster.forecast_symbols(all_tickers)

    # Score each result for ranking
    confidence_rank = {"high": 3, "medium": 2, "low": 1}
    min_conf_rank   = confidence_rank.get(min_confidence, 1)

    screener_results = []
    for r in results:
        # Filter by direction
        if direction_filter and r.direction != direction_filter:
            continue
        # Filter by confidence
        if confidence_rank.get(r.confidence, 0) < min_conf_rank:
            continue
        # Skip flat signals
        if r.direction == "flat":
            continue

        sector = get_ticker_sector(r.symbol) or "Unknown"
        # Extract composite score from narrative (we stored it)
        # Re-compute a proxy rank score: magnitude * confidence_multiplier
        conf_mult = {"high": 1.0, "medium": 0.7, "low": 0.4}.get(r.confidence, 0.5)
        rank_score = r.magnitude_pct * conf_mult

        screener_results.append((rank_score, r, sector))

    # Sort by rank score descending
    screener_results.sort(key=lambda x: -x[0])
    top = screener_results[:top_n]

    output = []
    for rank, (score, r, sector) in enumerate(top, 1):
        output.append(ScreenerResult(
            rank           = rank,
            symbol         = r.symbol,
            sector         = sector,
            direction      = r.direction,
            magnitude_pct  = r.magnitude_pct,
            recommendation = r.recommendation,
            confidence     = r.confidence,
            composite_score = score,
            narrative      = r.narrative,
        ))

    _print_screen_report(output)
    return output


def _print_screen_report(results: list[ScreenerResult]) -> None:
    print("\n" + "=" * 70)
    print(f"QUANT SCREENER RESULTS  [{date.today()}]")
    print("=" * 70)
    print(f"{'Rank':<5} {'Symbol':<8} {'Sector':<28} {'Dir':<6} {'Move%':<7} {'Conf':<8} {'Rec'}")
    print("-" * 70)
    for r in results:
        arrow = "▲" if r.direction == "up" else "▼"
        print(
            f"{r.rank:<5} {r.symbol:<8} {r.sector:<28} "
            f"{arrow} {r.direction:<4} {r.magnitude_pct:<7.1f} "
            f"{r.confidence:<8} {r.recommendation}"
        )
    print("=" * 70)
    print()
    for r in results:
        print(f"[{r.rank}] {r.symbol}: {r.narrative}")
    print()
