"""
S&P 500 universe definition.

Fetches the current S&P 500 constituent list from Wikipedia and caches it
locally. Also provides sector → ETF mapping for relative-strength signals.

Usage:
    from src.universe.sp500 import get_sp500_tickers, SECTOR_ETF_MAP
    tickers = get_sp500_tickers()          # list[str]
    sector  = get_ticker_sector("AAPL")    # "Technology"
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from src.utils.config import PROJECT_ROOT
from src.utils.logger import get_logger

logger = get_logger(__name__)

_CACHE_FILE = PROJECT_ROOT / "data" / "sp500_universe.json"
_CACHE_TTL_DAYS = 7          # refresh universe weekly

# Sector ETF proxies for relative-strength computation
SECTOR_ETF_MAP: dict[str, str] = {
    "Technology":             "XLK",
    "Health Care":            "XLV",
    "Financials":             "XLF",
    "Consumer Discretionary": "XLY",
    "Industrials":            "XLI",
    "Energy":                 "XLE",
    "Materials":              "XLB",
    "Utilities":              "XLU",
    "Real Estate":            "XLRE",
    "Communication Services": "XLC",
    "Consumer Staples":       "XLP",
}


def get_sp500_tickers(force_refresh: bool = False) -> list[str]:
    """
    Return current S&P 500 ticker list.
    Uses local cache (refreshed weekly) to avoid repeated Wikipedia fetches.
    """
    if not force_refresh and _cache_is_fresh():
        return _load_cache()["tickers"]

    logger.info("Fetching S&P 500 universe from Wikipedia...")
    try:
        df = pd.read_html(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            attrs={"id": "constituents"},
        )[0]
        # Wikipedia uses "." in BRK.B etc — yfinance wants "-"
        tickers = df["Symbol"].str.replace(".", "-", regex=False).tolist()
        sectors = dict(zip(
            df["Symbol"].str.replace(".", "-", regex=False),
            df["GICS Sector"],
        ))
        _save_cache(tickers, sectors)
        logger.info(f"S&P 500 universe loaded: {len(tickers)} tickers")
        return tickers
    except Exception as e:
        logger.warning(f"Failed to fetch S&P 500 list: {e}. Using cache if available.")
        if _CACHE_FILE.exists():
            return _load_cache()["tickers"]
        raise


def get_ticker_sector(symbol: str) -> str | None:
    """Return the GICS sector for a ticker, or None if not found."""
    if _CACHE_FILE.exists():
        data = _load_cache()
        return data.get("sectors", {}).get(symbol)
    return None


def get_sector_etf(symbol: str) -> str:
    """Return the sector ETF proxy for a ticker (defaults to SPY)."""
    sector = get_ticker_sector(symbol)
    return SECTOR_ETF_MAP.get(sector, "SPY")


# ── Cache helpers ────────────────────────────────────────────────────────────

def _cache_is_fresh() -> bool:
    if not _CACHE_FILE.exists():
        return False
    data = _load_cache()
    cached_date = date.fromisoformat(data.get("fetched_at", "2000-01-01"))
    return (date.today() - cached_date).days < _CACHE_TTL_DAYS


def _load_cache() -> dict:
    with open(_CACHE_FILE) as f:
        return json.load(f)


def _save_cache(tickers: list[str], sectors: dict[str, str]) -> None:
    _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_CACHE_FILE, "w") as f:
        json.dump({
            "fetched_at": date.today().isoformat(),
            "tickers": tickers,
            "sectors": sectors,
        }, f, indent=2)
