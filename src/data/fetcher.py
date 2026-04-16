"""
Historical market data fetcher with local Parquet cache.

Downloads OHLCV price history from yfinance and stores it as Parquet files
so repeated research runs don't hammer the API.

Cache layout:
    data/market_cache/
        prices/AAPL.parquet    — daily OHLCV for each ticker
        info/AAPL.json         — fundamentals snapshot (ttm)

Usage:
    from src.data.fetcher import MarketDataFetcher
    fetcher = MarketDataFetcher()
    df = fetcher.get_prices("AAPL", years=2)       # DataFrame[OHLCV]
    info = fetcher.get_info("AAPL")                # dict
    fetcher.prefetch(tickers, years=2)             # batch download
"""

from __future__ import annotations

import json
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import yfinance as yf

from src.utils.config import PROJECT_ROOT
from src.utils.logger import get_logger

logger = get_logger(__name__)

_CACHE_DIR   = PROJECT_ROOT / "data" / "market_cache"
_PRICES_DIR  = _CACHE_DIR / "prices"
_INFO_DIR    = _CACHE_DIR / "info"

# Refresh price cache if last data point is older than this
_PRICE_STALE_DAYS = 1
# Refresh fundamentals snapshot weekly
_INFO_STALE_DAYS  = 7


class MarketDataFetcher:
    """
    Thin wrapper around yfinance with Parquet caching.

    - Price history is appended incrementally (only fetches missing days)
    - Info (fundamentals) is refreshed weekly
    - All methods handle missing data gracefully (return None / empty df)
    """

    def __init__(self) -> None:
        _PRICES_DIR.mkdir(parents=True, exist_ok=True)
        _INFO_DIR.mkdir(parents=True, exist_ok=True)

    # ── Prices ───────────────────────────────────────────────────────────────

    def get_prices(
        self,
        symbol: str,
        years: int = 2,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        """
        Return daily OHLCV DataFrame for symbol going back `years` years.
        Columns: Open, High, Low, Close, Volume
        Index: DatetimeIndex (tz-naive, UTC dates)
        Returns empty DataFrame if data unavailable.
        """
        cache_file = _PRICES_DIR / f"{symbol}.parquet"
        start_date = date.today() - timedelta(days=int(years * 365))

        cached = self._load_prices_cache(cache_file)

        if force_refresh or cached is None or self._prices_are_stale(cached):
            # Only fetch what we're missing
            fetch_from = (
                (cached.index[-1].date() + timedelta(days=1)).isoformat()
                if cached is not None and not force_refresh
                else start_date.isoformat()
            )
            fresh = self._fetch_prices(symbol, fetch_from)
            if fresh is not None and not fresh.empty:
                cached = (
                    pd.concat([cached, fresh]).drop_duplicates()
                    if cached is not None
                    else fresh
                )
                cached.to_parquet(cache_file)

        if cached is None or cached.empty:
            return pd.DataFrame()

        # Return only the requested window
        cutoff = pd.Timestamp(start_date)
        return cached[cached.index >= cutoff]

    def get_info(self, symbol: str, force_refresh: bool = False) -> dict:
        """
        Return yfinance .info dict for symbol.
        Cached weekly. Returns {} on failure.
        """
        cache_file = _INFO_DIR / f"{symbol}.json"

        if not force_refresh and cache_file.exists():
            mtime = date.fromtimestamp(cache_file.stat().st_mtime)
            if (date.today() - mtime).days < _INFO_STALE_DAYS:
                with open(cache_file) as f:
                    return json.load(f)

        try:
            info = yf.Ticker(symbol).info or {}
            with open(cache_file, "w") as f:
                json.dump(info, f)
            return info
        except Exception as e:
            logger.debug(f"[{symbol}] info fetch failed: {e}")
            return {}

    # ── Batch prefetch ───────────────────────────────────────────────────────

    def prefetch(
        self,
        symbols: list[str],
        years: int = 2,
        delay: float = 0.3,
    ) -> None:
        """
        Download and cache price history for a list of tickers.
        Skips tickers that already have fresh cache.
        Adds a small delay between requests to be polite to yfinance.
        """
        stale = [
            s for s in symbols
            if self._prices_are_stale(self._load_prices_cache(_PRICES_DIR / f"{s}.parquet"))
        ]
        logger.info(f"Prefetching {len(stale)}/{len(symbols)} tickers (others cached)...")

        for i, sym in enumerate(stale, 1):
            try:
                self.get_prices(sym, years=years)
                if i % 50 == 0:
                    logger.info(f"  Progress: {i}/{len(stale)}")
                time.sleep(delay)
            except Exception as e:
                logger.debug(f"[{sym}] prefetch failed: {e}")

        logger.info("Prefetch complete.")

    def load_all_prices(
        self,
        symbols: list[str],
        years: int = 2,
    ) -> dict[str, pd.DataFrame]:
        """
        Load cached price DataFrames for all symbols.
        Returns dict[symbol → DataFrame]. Missing symbols are omitted.
        """
        result = {}
        for sym in symbols:
            df = self.get_prices(sym, years=years)
            if not df.empty:
                result[sym] = df
        return result

    # ── Options (no cache — real-time) ───────────────────────────────────────

    def get_options_snapshot(self, symbol: str) -> dict:
        """
        Return a snapshot of near-term options chain metrics.
        {
          "put_call_volume_ratio": float,
          "atm_iv": float,          # ATM implied volatility
          "iv_skew": float,         # 10% OTM put IV - 10% OTM call IV
        }
        Returns {} on failure.
        """
        try:
            ticker = yf.Ticker(symbol)
            exps = ticker.options
            if not exps:
                return {}

            # Use nearest expiration that is ≥ 7 days out
            exp = next(
                (e for e in exps if (pd.Timestamp(e) - pd.Timestamp.now()).days >= 7),
                exps[0],
            )
            chain  = ticker.option_chain(exp)
            calls  = chain.calls
            puts   = chain.puts

            last_price = ticker.fast_info.get("lastPrice", None)
            if not last_price:
                return {}

            # Put/call volume ratio
            call_vol = max(calls["volume"].sum(), 1)
            put_vol  = puts["volume"].sum()
            pc_ratio = put_vol / call_vol

            # ATM IV (closest strike)
            atm_call = calls.iloc[(calls["strike"] - last_price).abs().argsort()[:1]]
            atm_iv   = float(atm_call["impliedVolatility"].values[0]) if not atm_call.empty else 0.0

            # IV skew: 10% OTM put vs 10% OTM call
            otm_put_strike  = last_price * 0.90
            otm_call_strike = last_price * 1.10
            put_row  = puts.iloc[(puts["strike"] - otm_put_strike).abs().argsort()[:1]]
            call_row = calls.iloc[(calls["strike"] - otm_call_strike).abs().argsort()[:1]]
            put_iv   = float(put_row["impliedVolatility"].values[0])  if not put_row.empty  else atm_iv
            call_iv  = float(call_row["impliedVolatility"].values[0]) if not call_row.empty else atm_iv
            skew     = put_iv - call_iv

            return {
                "put_call_volume_ratio": round(pc_ratio, 3),
                "atm_iv":  round(atm_iv, 4),
                "iv_skew": round(skew, 4),
            }
        except Exception as e:
            logger.debug(f"[{symbol}] options snapshot failed: {e}")
            return {}

    # ── Analyst / fundamentals ───────────────────────────────────────────────

    def get_analyst_data(self, symbol: str) -> dict:
        """
        Return analyst-related data:
        {
          "eps_estimate_current_qtr": float,
          "eps_estimate_next_qtr": float,
          "recommendation_mean": float,   # 1=Strong Buy ... 5=Sell
          "num_analyst_opinions": int,
          "earnings_history": list[dict]  # last 4 quarters actual vs estimate
        }
        Returns {} on failure.
        """
        try:
            t = yf.Ticker(symbol)
            info = self.get_info(symbol)

            earnings_hist = []
            try:
                eh = t.earnings_history
                if eh is not None and not eh.empty:
                    for _, row in eh.tail(8).iterrows():
                        earnings_hist.append({
                            "date":     str(row.name.date()) if hasattr(row.name, "date") else str(row.name),
                            "actual":   float(row.get("epsActual",   0) or 0),
                            "estimate": float(row.get("epsEstimate", 0) or 0),
                            "surprise": float(row.get("surprisePercent", 0) or 0),
                        })
            except Exception:
                pass

            return {
                "eps_estimate_current_qtr": info.get("epsCurrentYear"),
                "eps_estimate_next_qtr":    info.get("epsForward"),
                "recommendation_mean":      info.get("recommendationMean"),
                "num_analyst_opinions":     info.get("numberOfAnalystOpinions"),
                "earnings_history":         earnings_hist,
            }
        except Exception as e:
            logger.debug(f"[{symbol}] analyst data failed: {e}")
            return {}

    def get_insider_data(self, symbol: str) -> dict:
        """
        Return insider transaction summary for last 90 days.
        {
          "net_shares_90d": int,     # positive = net buying
          "transaction_count": int,
        }
        """
        try:
            t = yf.Ticker(symbol)
            ins = t.insider_transactions
            if ins is None or ins.empty:
                return {"net_shares_90d": 0, "transaction_count": 0}

            cutoff = pd.Timestamp.now() - pd.Timedelta(days=90)
            ins.index = pd.to_datetime(ins.index)
            recent = ins[ins.index >= cutoff]

            net = int(recent["Shares"].sum()) if "Shares" in recent.columns else 0
            return {
                "net_shares_90d":   net,
                "transaction_count": len(recent),
            }
        except Exception as e:
            logger.debug(f"[{symbol}] insider data failed: {e}")
            return {"net_shares_90d": 0, "transaction_count": 0}

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _fetch_prices(self, symbol: str, start: str) -> Optional[pd.DataFrame]:
        try:
            df = yf.Ticker(symbol).history(start=start, auto_adjust=True)
            if df.empty:
                return None
            df.index = df.index.tz_localize(None)
            return df[["Open", "High", "Low", "Close", "Volume"]]
        except Exception as e:
            logger.debug(f"[{symbol}] price fetch failed: {e}")
            return None

    def _load_prices_cache(self, path: Path) -> Optional[pd.DataFrame]:
        if not path.exists():
            return None
        try:
            return pd.read_parquet(path)
        except Exception:
            return None

    def _prices_are_stale(self, df: Optional[pd.DataFrame]) -> bool:
        if df is None or df.empty:
            return True
        last_date = df.index[-1].date()
        # Allow 1 day lag (market may not have closed yet today)
        return (date.today() - last_date).days > _PRICE_STALE_DAYS
