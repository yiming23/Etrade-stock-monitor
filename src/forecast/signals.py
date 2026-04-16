"""
Quant signal computation for any ticker.

Each signal returns a float in [-1, +1] using time-series z-score normalization:
  - Positive (+1) = bullish
  - Negative (-1) = bearish
  - 0 = neutral / data unavailable

Signals implemented (grouped by academic evidence strength):

  TIER 1 — Strong academic evidence:
    analyst_revision    Earnings revision momentum (Chan et al 1996)
    sue                 Standardized Unexpected Earnings / PEAD (Bernard & Thomas 1989)
    momentum_12m_1m     Intermediate momentum, skip-1-month (Jegadeesh & Titman 1993)

  TIER 2 — Moderate evidence:
    rel_strength        Relative strength vs sector ETF
    insider_net         Net insider buying (Seyhun 1986)
    short_interest      Short interest ratio (high = bearish)

  TIER 3 — Useful context, weaker standalone:
    put_call_ratio      Options market sentiment
    iv_rank             IV vs 52-week range (low IV = calm = slightly bullish)
    iv_skew             Put skew (high = market buying downside protection)
    momentum_1m         Short-term momentum (weak, can reverse)
    volume_surge        Unusual volume (confirms moves, weak standalone)

Usage:
    from src.forecast.signals import SignalBundle, compute_signals
    from src.data.fetcher import MarketDataFetcher

    fetcher = MarketDataFetcher()
    bundle = compute_signals("AAPL", fetcher, sector_etf="XLK")
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Optional

import numpy as np
import pandas as pd

from src.data.fetcher import MarketDataFetcher
from src.utils.logger import get_logger

logger = get_logger(__name__)

# Minimum price history required (trading days)
_MIN_DAYS = 60


@dataclass
class SignalBundle:
    """
    All computed signals for one ticker on one date.
    Raw signal values — NOT yet weighted or combined.
    All values in [-1, +1] (0 = unavailable or neutral).
    """
    symbol: str

    # Tier 1
    analyst_revision:  float = 0.0   # EPS estimate revision over 4 weeks
    sue:               float = 0.0   # Standardized Unexpected Earnings
    momentum_12m_1m:   float = 0.0   # 12-month minus last-month momentum

    # Tier 2
    rel_strength:      float = 0.0   # vs sector ETF (1-month)
    insider_net:       float = 0.0   # net insider buying (90 days)
    short_interest:    float = 0.0   # inverted short ratio signal

    # Tier 3
    put_call_ratio:    float = 0.0   # options sentiment
    iv_rank:           float = 0.0   # implied vol percentile (inverted)
    iv_skew:           float = 0.0   # put-call IV skew (inverted)
    momentum_1m:       float = 0.0   # short-term price momentum
    volume_surge:      float = 0.0   # recent volume vs average

    # Metadata
    data_quality: float = 0.0        # fraction of signals successfully computed

    def to_dict(self) -> dict:
        return asdict(self)

    def available_signals(self) -> dict[str, float]:
        """Return only the signals that were actually computed (non-zero)."""
        skip = {"symbol", "data_quality"}
        return {k: v for k, v in asdict(self).items() if k not in skip and v != 0.0}


# ── Public entry point ───────────────────────────────────────────────────────

def compute_signals(
    symbol: str,
    fetcher: MarketDataFetcher,
    sector_etf: str = "SPY",
) -> SignalBundle:
    """
    Compute all quant signals for a given symbol.
    Returns SignalBundle with 0.0 for any signal that couldn't be computed.
    """
    bundle = SignalBundle(symbol=symbol)
    computed = 0
    total = 11  # total signals attempted

    # ── Price history ────────────────────────────────────────────────────────
    prices = fetcher.get_prices(symbol, years=2)
    if prices.empty or len(prices) < _MIN_DAYS:
        logger.debug(f"[{symbol}] insufficient price history ({len(prices)} days)")
        return bundle

    closes = prices["Close"]
    volumes = prices["Volume"]

    # ── Tier 1: Momentum (12M - 1M) ─────────────────────────────────────────
    # Classic Jegadeesh & Titman: buy 12-month winners, skip last month
    # Skipping last month avoids short-term reversal contamination
    if len(closes) >= 252:
        ret_12m = (closes.iloc[-22] / closes.iloc[-252]) - 1   # 12 months ago to 1 month ago
        bundle.momentum_12m_1m = _zscore_clip(ret_12m, closes.pct_change(252).dropna())
        computed += 1

    # ── Tier 3: Momentum (1M) ────────────────────────────────────────────────
    if len(closes) >= 22:
        ret_1m = (closes.iloc[-1] / closes.iloc[-22]) - 1
        bundle.momentum_1m = _zscore_clip(ret_1m, closes.pct_change(21).dropna())
        computed += 1

    # ── Tier 2: Relative strength vs sector ETF ──────────────────────────────
    try:
        sector_prices = fetcher.get_prices(sector_etf, years=1)
        if not sector_prices.empty and len(sector_prices) >= 22:
            stock_ret  = (closes.iloc[-1] / closes.iloc[-22]) - 1
            sector_ret = (sector_prices["Close"].iloc[-1] / sector_prices["Close"].iloc[-22]) - 1
            rel = stock_ret - sector_ret
            # Normalize against the stock's own rolling relative-return distribution
            rolling_rel = closes.pct_change(21) - sector_prices["Close"].reindex(closes.index).pct_change(21)
            bundle.rel_strength = _zscore_clip(rel, rolling_rel.dropna())
            computed += 1
    except Exception as e:
        logger.debug(f"[{symbol}] rel_strength failed: {e}")

    # ── Tier 3: Volume surge ─────────────────────────────────────────────────
    if len(volumes) >= 20:
        vol_recent = volumes.tail(5).mean()
        vol_avg    = volumes.tail(60).mean()
        if vol_avg > 0:
            surge = (vol_recent / vol_avg) - 1
            # Normalize: historical distribution of volume ratios
            rolling_surge = volumes.rolling(5).mean() / volumes.rolling(60).mean() - 1
            bundle.volume_surge = _zscore_clip(surge, rolling_surge.dropna(), cap=2.0)
            computed += 1

    # ── Tier 1: Analyst revision + SUE ───────────────────────────────────────
    try:
        analyst = fetcher.get_analyst_data(symbol)
        info    = fetcher.get_info(symbol)

        # Analyst revision: compare current EPS estimate to prior
        # Proxy: use recommendation_mean change direction (1=strong buy, 5=sell)
        rec_mean = analyst.get("recommendation_mean")
        if rec_mean is not None:
            # Invert and normalize: 1 (strong buy) → +1, 5 (sell) → -1
            bundle.analyst_revision = _clip(-(rec_mean - 3.0) / 2.0)
            computed += 1

        # SUE: Standardized Unexpected Earnings
        earnings_hist = analyst.get("earnings_history", [])
        if len(earnings_hist) >= 2:
            surprises = [e["surprise"] for e in earnings_hist if e["surprise"] is not None]
            if surprises:
                # Mean surprise and trend direction
                mean_surprise = np.mean(surprises[-4:])  # last 4 quarters
                # Normalize to [-1, +1] using tanh-like squashing
                bundle.sue = _clip(math.tanh(mean_surprise / 10.0))
                computed += 1

    except Exception as e:
        logger.debug(f"[{symbol}] analyst/sue failed: {e}")

    # ── Tier 2: Insider net buying ────────────────────────────────────────────
    try:
        insider = fetcher.get_insider_data(symbol)
        net_shares = insider.get("net_shares_90d", 0)
        if net_shares != 0:
            # Normalize by market cap proxy (shares outstanding)
            shares_out = fetcher.get_info(symbol).get("sharesOutstanding", 1) or 1
            net_frac   = net_shares / shares_out
            bundle.insider_net = _clip(math.tanh(net_frac * 500))
            computed += 1
    except Exception as e:
        logger.debug(f"[{symbol}] insider failed: {e}")

    # ── Tier 2: Short interest ────────────────────────────────────────────────
    try:
        info = fetcher.get_info(symbol)
        short_ratio = info.get("shortRatio")   # days to cover
        if short_ratio is not None and short_ratio > 0:
            # High short ratio = bearish (invert)
            # Typical range: 0-20 days. >10 is very high.
            bundle.short_interest = _clip(-math.tanh((short_ratio - 3.0) / 4.0))
            computed += 1
    except Exception as e:
        logger.debug(f"[{symbol}] short_interest failed: {e}")

    # ── Tier 3: Options signals ───────────────────────────────────────────────
    try:
        opts = fetcher.get_options_snapshot(symbol)
        if opts:
            # Put/Call ratio: >1 is bearish (more puts), <1 is bullish
            pc = opts.get("put_call_volume_ratio", 1.0)
            bundle.put_call_ratio = _clip(-(pc - 1.0) / 1.0)
            computed += 1

            # IV skew: high skew (puts > calls) = market nervous = bearish
            skew = opts.get("iv_skew", 0.0)
            bundle.iv_skew = _clip(-math.tanh(skew / 0.1))
            computed += 1

            # IV rank: approximate from ATM IV vs 52-week price range as proxy
            # (True IV rank needs historical options data; use realized vol proxy)
            if len(closes) >= 252:
                atm_iv = opts.get("atm_iv", 0.0)
                hist_vol_252 = closes.pct_change().tail(252).std() * math.sqrt(252)
                iv_vs_hist = atm_iv / max(hist_vol_252, 0.001)
                # IV much > hist vol = market pricing in event = slightly bearish
                bundle.iv_rank = _clip(-(iv_vs_hist - 1.0) / 1.0)
                computed += 1

    except Exception as e:
        logger.debug(f"[{symbol}] options failed: {e}")

    bundle.data_quality = computed / total
    logger.debug(
        f"[{symbol}] signals: {computed}/{total} computed, "
        f"quality={bundle.data_quality:.0%}"
    )
    return bundle


# ── Normalization helpers ────────────────────────────────────────────────────

def _zscore_clip(value: float, series: pd.Series, cap: float = 2.0) -> float:
    """
    Time-series z-score: (value - mean) / std, then clip to [-1, +1].
    Uses the historical distribution of the same metric for this stock.
    """
    if series.empty or series.std() == 0:
        return 0.0
    z = (value - series.mean()) / series.std()
    return _clip(z / cap)   # cap=2 means ±2 sigma → ±1


def _clip(value: float) -> float:
    """Hard-clip to [-1, +1], handling NaN."""
    if math.isnan(value) or math.isinf(value):
        return 0.0
    return max(-1.0, min(1.0, value))
