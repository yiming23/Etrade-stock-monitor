"""
Quant Factor Model — Historical Backtest with Visualizations
=============================================================

METHODOLOGY — READ BEFORE INTERPRETING RESULTS:

  SIGNALS INCLUDED IN THIS BACKTEST (price-only, strictly point-in-time):
    - momentum_12m_1m   12-month return skipping last month
    - momentum_1m       1-month price return
    - rel_strength      Excess return vs sector ETF

  SIGNALS EXCLUDED (require point-in-time fundamental database):
    - analyst_revision  yfinance only provides today's consensus, not historical
    - short_interest    yfinance only provides today's shortRatio
    - sue               Earnings history may include unreported quarters
    - insider_net       90-day window is relative to today, not sample date

  The live model uses all signals. This backtest is a rigorous price-only test.
  Fundamental signals cannot be cleanly backtested with free data sources.

  SURVIVORSHIP BIAS WARNING:
    Universe = current S&P 500 constituents. Stocks that were removed
    (bankruptcy, acquisition, etc.) are not included. This biases long-horizon
    results upward — especially for GFC / dot-com periods.

  WEIGHTS (renormalized from v1.1 OOS weights, price signals only):
    momentum_12m_1m : 0.66   (0.35 / 0.53)
    momentum_1m     : 0.23   (0.12 / 0.53)
    rel_strength    : 0.11   (0.06 / 0.53)

Industry-standard methodology:
  - Universe  : S&P 500 (current constituents)
  - Rebalance : Monthly (every 21 trading days)
  - Forward   : 21-day holding period return
  - Baseline  : S&P 500 (SPY) buy-and-hold
  - Quintiles : Cross-sectional per rebalancing date

Named historical periods (select with --period):
  recent       : Last 3 years (default)
  post_covid   : 2020–now (crash + recovery + bull)
  covid        : 2018–2023 (full COVID regime)
  bull_2010s   : 2010–2019 (post-GFC decade bull)
  gfc          : 2005–2012 (GFC crash + recovery)
  full_cycle   : 2004–now  (multi-regime history)
  dot_com      : 1998–2004 (dot-com bust)

Usage:
    python -m src.research.run --backtest-visual
    python -m src.research.run --backtest-visual --period gfc
    python -m src.research.run --backtest-visual --period full_cycle
    python -m src.research.run --backtest-visual --start-date 2008-01-01 --end-date 2012-12-31
    python -m src.research.run --backtest-visual --portfolio ANET,COIN,META,MSFT --period covid
"""

from __future__ import annotations

import math
import warnings
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from scipy import stats

try:
    import matplotlib.dates as mdates
except ImportError:
    mdates = None  # type: ignore

from src.data.fetcher import MarketDataFetcher
from src.universe.sp500 import get_ticker_sector, get_sector_etf
from src.utils.config import PROJECT_ROOT
from src.utils.logger import get_logger

logger = get_logger(__name__)

_OUTPUT_DIR    = PROJECT_ROOT / "data" / "backtest_reports"
_N_QUINTILES   = 5
_FORWARD_DAYS  = 21   # 1-month holding period
_RESAMPLE_DAYS = 21   # monthly rebalancing cadence
_WARMUP_DAYS   = 260  # ~1yr + buffer; need enough history before first signal

# ── Point-in-time weights ─────────────────────────────────────────────────────
# Only price signals are included — we can compute these truly point-in-time.
# Weights are renormalized from v1.1 OOS weights (sum = 1.0).
_PIT_WEIGHTS: dict[str, float] = {
    "momentum_12m_1m": 0.66,
    "momentum_1m":     0.23,
    "rel_strength":    0.11,
}
_PIT_SIGNAL_NAMES = list(_PIT_WEIGHTS.keys())

# ── Named historical periods ──────────────────────────────────────────────────
NAMED_PERIODS: dict[str, dict] = {
    "recent": {
        "label":      "Recent 3 Years",
        "start_date": None,   # computed from years
        "end_date":   None,
        "years":      3,
    },
    "post_covid": {
        "label":      "COVID & Recovery (2020–now)",
        "start_date": "2020-01-01",
        "end_date":   None,
        "years":      7,
    },
    "covid": {
        "label":      "COVID Era (2018–2023)",
        "start_date": "2018-01-01",
        "end_date":   "2023-12-31",
        "years":      9,
    },
    "bull_2010s": {
        "label":      "Post-GFC Bull Market (2010–2019)",
        "start_date": "2010-01-01",
        "end_date":   "2019-12-31",
        "years":      17,
    },
    "gfc": {
        "label":      "Global Financial Crisis (2005–2012)",
        "start_date": "2005-01-01",
        "end_date":   "2012-12-31",
        "years":      22,
    },
    "full_cycle": {
        "label":      "Full Multi-Regime History (2004–now)",
        "start_date": "2004-01-01",
        "end_date":   None,
        "years":      23,
    },
    "dot_com": {
        "label":      "Dot-com Bust (1998–2004)",
        "start_date": "1998-01-01",
        "end_date":   "2004-12-31",
        "years":      29,
    },
}


# ── Main entry point ──────────────────────────────────────────────────────────

def run_backtest_visual(
    tickers: list[str],
    years: int = 3,
    portfolio_symbols: list[str] | None = None,
    period: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> Path:
    """
    Run point-in-time walk-forward backtest and generate visualization.

    Args:
        tickers:           Universe (typically full S&P 500)
        years:             Years of price history (overridden by period)
        portfolio_symbols: Holdings to highlight in sector chart
        period:            Named period key from NAMED_PERIODS
        start_date:        Custom start date YYYY-MM-DD (overrides period)
        end_date:          Custom end date YYYY-MM-DD (overrides period)

    Returns:
        Path to saved PNG report
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.gridspec as gridspec

    # ── Resolve period ────────────────────────────────────────────────────────
    period_label = "Custom"
    _start: Optional[date] = None
    _end:   Optional[date] = None

    if period and period in NAMED_PERIODS:
        p = NAMED_PERIODS[period]
        period_label = p["label"]
        years        = p["years"]
        if p["start_date"]:
            _start = date.fromisoformat(p["start_date"])
        if p["end_date"]:
            _end = date.fromisoformat(p["end_date"])
    elif start_date or end_date:
        if start_date:
            _start = date.fromisoformat(start_date)
            # Ensure years covers the full requested window + warmup
            years_needed = (date.today() - _start).days // 365 + 2
            years = max(years, years_needed)
        if end_date:
            _end = date.fromisoformat(end_date)
        period_label = f"{start_date or 'start'} → {end_date or 'now'}"

    logger.info(f"Backtest period  : {period_label}")
    logger.info(f"Data history     : {years}y")
    logger.info(f"Universe         : {len(tickers)} tickers")
    logger.info(f"Signals          : {', '.join(_PIT_SIGNAL_NAMES)} (price-only, PIT)")

    if years > 5:
        logger.info(
            f"Note: fetching {years}y of data for {len(tickers)} stocks. "
            f"First run may take 30–90 minutes for long periods."
        )

    # ── Prefetch data ─────────────────────────────────────────────────────────
    fetcher = MarketDataFetcher()

    # SPY for market baseline
    logger.info("Fetching SPY (market baseline)...")
    spy_prices = fetcher.get_prices("SPY", years=years)

    # Unique sector ETFs (needed for rel_strength PIT computation)
    unique_etfs: set[str] = set()
    for sym in tickers:
        etf = get_sector_etf(sym)
        if etf:
            unique_etfs.add(etf)
    logger.info(f"Fetching {len(unique_etfs)} sector ETFs...")
    sector_etf_prices: dict[str, pd.DataFrame] = {}
    for etf in unique_etfs:
        df = fetcher.get_prices(etf, years=years)
        if not df.empty:
            sector_etf_prices[etf] = df

    # All tickers
    logger.info("Prefetching ticker prices (may trigger backfill for long periods)...")
    fetcher.prefetch(tickers, years=years)

    # ── Collect backtest records ──────────────────────────────────────────────
    records = _collect_records(
        tickers, fetcher, years,
        sector_etf_prices=sector_etf_prices,
        start_date=_start,
        end_date=_end,
    )

    if not records:
        raise RuntimeError(
            "No backtest records collected. "
            "Check that tickers have sufficient price history for the requested period."
        )

    df = pd.DataFrame(records)
    df["date"] = pd.to_datetime(df["date"])

    n_obs     = len(df)
    n_dates   = df["date"].nunique()
    n_stocks  = df["symbol"].nunique()
    logger.info(
        f"Collected {n_obs:,} stock-month observations | "
        f"{n_dates} rebalancing dates | {n_stocks} stocks"
    )

    # ── Cross-sectional quintile assignment per rebalancing date ──────────────
    df["quintile"] = (
        df.groupby("date")["composite_score"]
          .transform(
              lambda x: pd.qcut(x, _N_QUINTILES, labels=False, duplicates="drop") + 1
          )
    )

    # ── Compute SPY monthly returns aligned to backtest dates ─────────────────
    spy_monthly = _compute_spy_monthly(spy_prices, df["date"])

    # ── Figure layout ─────────────────────────────────────────────────────────
    fig = plt.figure(figsize=(18, 22))
    fig.patch.set_facecolor("#f8fafc")
    gs = gridspec.GridSpec(
        3, 2,
        figure=fig,
        hspace=0.50,
        wspace=0.35,
        top=0.91, bottom=0.05,
        left=0.07, right=0.97,
    )

    today_str = date.today().strftime("%B %d, %Y")
    bias_note = "⚠ Survivorship bias present (current S&P 500 constituents only)"
    sig_note  = "Signals: momentum_12m_1m + momentum_1m + rel_strength  [price-only, strictly point-in-time]"
    fig.suptitle(
        f"Quant Factor Model — Backtest  |  {today_str}\n"
        f"Universe: S&P 500 ({len(tickers)} stocks)  ·  Period: {period_label}  ·  "
        f"Monthly rebalancing  ·  21-day forward return\n"
        f"{sig_note}\n{bias_note}",
        fontsize=10, fontweight="bold", color="#1e293b", y=0.975,
    )

    ax1 = fig.add_subplot(gs[0, 0])
    ax2 = fig.add_subplot(gs[0, 1])
    ax3 = fig.add_subplot(gs[1, :])   # full-width IC time series
    ax4 = fig.add_subplot(gs[2, 0])
    ax5 = fig.add_subplot(gs[2, 1])

    _plot_quintile_spread(ax1, df, spy_monthly)
    _plot_ls_cumulative(ax2, df, spy_prices, spy_monthly)
    _plot_ic_timeseries(ax3, df)
    _plot_sector_attribution(ax4, df, portfolio_symbols or [])
    _plot_signal_ic_summary(ax5, df)

    # ── Save ──────────────────────────────────────────────────────────────────
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    period_slug = period or (start_date.replace("-", "") if start_date else "custom")
    out_path    = _OUTPUT_DIR / f"backtest_{date.today()}_{period_slug}.png"
    fig.savefig(out_path, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)

    logger.info(f"Report saved → {out_path}")
    _print_summary(df, spy_monthly, period_label)
    return out_path


# ── Data collection (point-in-time) ──────────────────────────────────────────

def _collect_records(
    tickers: list[str],
    fetcher: MarketDataFetcher,
    years: int,
    sector_etf_prices: dict[str, pd.DataFrame],
    start_date: Optional[date] = None,
    end_date:   Optional[date] = None,
) -> list[dict]:
    """
    Walk forward monthly. For each (stock, date) compute:
      - Composite score using STRICTLY point-in-time price signals
      - 21-day forward return (the thing we're trying to predict)
      - Per-signal values for IC analysis

    Point-in-time means: at sample_date, only data up to and including
    sample_date is used. No data from after sample_date touches the signal.

    Uses pre-computed expanding statistics for speed O(1) per sample.
    """
    records  = []
    processed = 0

    for sym in tickers:
        try:
            prices = fetcher.get_prices(sym, years=years)
            if prices.empty or len(prices) < _WARMUP_DAYS + _FORWARD_DAYS:
                continue

            sector    = get_ticker_sector(sym) or "Unknown"
            etf_key   = get_sector_etf(sym)
            etf_df    = sector_etf_prices.get(etf_key, pd.DataFrame())

            closes  = prices["Close"]
            volumes = prices["Volume"]

            # ── Pre-compute rolling series once per stock ─────────────────────
            # These are computed over the FULL price history. At each sample
            # index `idx`, we use `.iloc[idx]` to get the value AS IF we only
            # had data up to idx. This is mathematically equivalent to
            # recomputing on truncated data but O(N) instead of O(N²).

            ret_252  = closes.pct_change(252)    # 12-month rolling return
            ret_21   = closes.pct_change(21)     # 1-month rolling return

            # Expanding mean/std (PIT normalization at each bar)
            exp_mean_252 = ret_252.expanding(min_periods=60).mean()
            exp_std_252  = ret_252.expanding(min_periods=60).std()
            exp_mean_21  = ret_21.expanding(min_periods=21).mean()
            exp_std_21   = ret_21.expanding(min_periods=21).std()

            # Sector ETF aligned to stock calendar
            has_etf = not etf_df.empty
            if has_etf:
                etf_closes   = etf_df["Close"].reindex(closes.index, method="ffill")
                etf_ret_21   = etf_closes.pct_change(21)
                rel_ret_21   = ret_21 - etf_ret_21
                exp_mean_rel = rel_ret_21.expanding(min_periods=21).mean()
                exp_std_rel  = rel_ret_21.expanding(min_periods=21).std()

            # ── Walk forward ──────────────────────────────────────────────────
            sample_indices = range(
                _WARMUP_DAYS,
                len(prices) - _FORWARD_DAYS,
                _RESAMPLE_DAYS,
            )

            for idx in sample_indices:
                sample_date = prices.index[idx].date()

                if start_date and sample_date < start_date:
                    continue
                if end_date and sample_date > end_date:
                    continue

                # ── Point-in-time signal values ───────────────────────────────
                # All reads use iloc[idx] or iloc[idx - N] — never data past idx.

                sig: dict[str, float] = {}

                # momentum_12m_1m: return from 252 bars ago to 22 bars ago
                if idx >= 252:
                    val = closes.iloc[idx - 22] / closes.iloc[idx - 252] - 1
                    m   = exp_mean_252.iloc[idx]
                    s   = exp_std_252.iloc[idx]
                    sig["momentum_12m_1m"] = _zscore_clip(val, m, s)

                # momentum_1m: return over last 22 bars
                if idx >= 22:
                    val = closes.iloc[idx] / closes.iloc[idx - 22] - 1
                    m   = exp_mean_21.iloc[idx]
                    s   = exp_std_21.iloc[idx]
                    sig["momentum_1m"] = _zscore_clip(val, m, s)

                # rel_strength: excess vs sector ETF over last 22 bars
                if has_etf and idx >= 22:
                    stock_ret  = closes.iloc[idx] / closes.iloc[idx - 22] - 1
                    if not pd.isna(etf_closes.iloc[idx - 22]) and etf_closes.iloc[idx - 22] > 0:
                        sector_ret = etf_closes.iloc[idx] / etf_closes.iloc[idx - 22] - 1
                        rel = stock_ret - sector_ret
                        m   = exp_mean_rel.iloc[idx]
                        s   = exp_std_rel.iloc[idx]
                        sig["rel_strength"] = _zscore_clip(rel, m, s)

                if not sig:
                    continue

                # Composite score
                score = _pit_composite_score(sig)

                # Forward return (21 trading days after sample_date)
                fwd_return = (
                    closes.iloc[idx + _FORWARD_DAYS] / closes.iloc[idx]
                ) - 1

                records.append({
                    "date":            sample_date,
                    "symbol":          sym,
                    "sector":          sector,
                    "composite_score": score,
                    "forward_return":  fwd_return,
                    **{k: sig.get(k, 0.0) for k in _PIT_SIGNAL_NAMES},
                })

            processed += 1
            if processed % 50 == 0:
                logger.info(f"  {processed}/{len(tickers)} stocks processed...")

        except Exception as e:
            logger.debug(f"[{sym}] backtest failed: {e}")

    return records


# ── Signal computation helpers ────────────────────────────────────────────────

def _zscore_clip(value: float, mean: float, std: float, cap: float = 2.0) -> float:
    """
    Z-score normalize and clip to [-1, +1].
    cap=2 means ±2 sigma maps to ±1.
    Returns 0 if std is missing/zero.
    """
    if pd.isna(std) or std == 0 or pd.isna(mean) or pd.isna(value):
        return 0.0
    z = (value - mean) / std
    return float(max(-1.0, min(1.0, z / cap)))


def _pit_composite_score(signals: dict[str, float]) -> float:
    """
    Weighted sum of PIT signals, normalized for missing signals.
    Returns value in [-1, +1].
    """
    score    = 0.0
    total_w  = 0.0
    for sig, w in _PIT_WEIGHTS.items():
        v = signals.get(sig, 0.0)
        if v != 0.0 and not math.isnan(v):
            score   += v * w
            total_w += w
    if total_w == 0:
        return 0.0
    return score / total_w   # normalize so missing signals don't dilute


# ── SPY baseline ──────────────────────────────────────────────────────────────

def _compute_spy_monthly(
    spy_prices: pd.DataFrame,
    backtest_dates: pd.Series,
) -> pd.Series:
    """
    For each rebalancing date in the backtest, compute SPY's 21-trading-day
    forward return — same holding period as the factor backtest.
    """
    if spy_prices.empty:
        return pd.Series(dtype=float)

    spy_closes = spy_prices["Close"].sort_index()
    dates      = sorted(backtest_dates.unique())
    result: dict = {}

    for dt in dates:
        ts = pd.Timestamp(dt)

        # Price at (or just before) this date
        avail = spy_closes[spy_closes.index <= ts]
        if avail.empty:
            continue
        p0_idx = avail.index.get_loc(avail.index[-1])

        # Price 21 trading days later
        future_idx = p0_idx + _FORWARD_DAYS
        if future_idx >= len(spy_closes):
            continue

        p0 = spy_closes.iloc[p0_idx]
        p1 = spy_closes.iloc[future_idx]
        result[dt] = p1 / p0 - 1

    return pd.Series(result)


# ── Visual style ──────────────────────────────────────────────────────────────

_STYLE = {
    "bg":       "#f8fafc",
    "panel":    "#ffffff",
    "grid":     "#e2e8f0",
    "text":     "#1e293b",
    "subtext":  "#64748b",
    "green":    "#16a34a",
    "red":      "#dc2626",
    "blue":     "#2563eb",
    "amber":    "#d97706",
    "purple":   "#7c3aed",
    "q_colors": ["#dc2626", "#f87171", "#94a3b8", "#4ade80", "#16a34a"],
}


def _style_ax(ax, title: str, xlabel: str = "", ylabel: str = "") -> None:
    ax.set_facecolor(_STYLE["panel"])
    ax.set_title(title, fontsize=10, fontweight="bold",
                 color=_STYLE["text"], pad=8)
    ax.set_xlabel(xlabel, fontsize=9, color=_STYLE["subtext"])
    ax.set_ylabel(ylabel, fontsize=9, color=_STYLE["subtext"])
    ax.tick_params(colors=_STYLE["subtext"], labelsize=8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color(_STYLE["grid"])
    ax.spines["bottom"].set_color(_STYLE["grid"])
    ax.yaxis.grid(True, color=_STYLE["grid"], linewidth=0.7, zorder=0)
    ax.set_axisbelow(True)


# ── Plot functions ────────────────────────────────────────────────────────────

def _plot_quintile_spread(
    ax,
    df: pd.DataFrame,
    spy_monthly: pd.Series,
) -> None:
    """
    Bar chart: average 21d return per quintile Q1 (weakest) → Q5 (strongest).
    If factor works: monotonically increasing Q1 → Q5.
    SPY average monthly return shown as a dashed baseline.
    """
    valid = df.dropna(subset=["quintile", "forward_return"])
    if valid.empty:
        _style_ax(ax, "Quintile Return Spread")
        return

    q_means  = valid.groupby("quintile")["forward_return"].mean() * 100
    q_sems   = valid.groupby("quintile")["forward_return"].sem()  * 100
    q_counts = valid.groupby("quintile")["forward_return"].count()

    qs   = q_means.index.astype(int)
    bars = ax.bar(
        qs, q_means.values,
        color=[_STYLE["q_colors"][i - 1] for i in qs],
        width=0.6, zorder=3, alpha=0.9,
    )
    ax.errorbar(
        qs, q_means.values, yerr=q_sems.values,
        fmt="none", color=_STYLE["text"], capsize=4, linewidth=1.2, zorder=4,
    )
    ax.axhline(0, color=_STYLE["text"], linewidth=0.8, zorder=2)

    for bar, q in zip(bars, qs):
        h     = bar.get_height()
        label = f"{h:+.2f}%\nn={q_counts[q]}"
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            h + (0.05 if h >= 0 else -0.15),
            label, ha="center",
            va="bottom" if h >= 0 else "top",
            fontsize=7.5, color=_STYLE["text"],
        )

    # SPY average monthly return as baseline
    if not spy_monthly.empty:
        spy_avg = spy_monthly.mean() * 100
        ax.axhline(
            spy_avg, color=_STYLE["amber"], linewidth=1.5,
            linestyle="--", zorder=4, alpha=0.9,
            label=f"SPY avg: {spy_avg:+.2f}%/mo",
        )
        ax.legend(fontsize=8, loc="upper left", framealpha=0.85)

    spread = (
        q_means.iloc[-1] - q_means.iloc[0]
        if len(q_means) == _N_QUINTILES else float("nan")
    )
    title = f"Quintile Return Spread  (Q5–Q1 = {spread:+.2f}%/month)"
    _style_ax(ax, title,
              xlabel="Factor Score Quintile (1=Weakest, 5=Strongest)",
              ylabel="Avg 21-day Forward Return (%)")
    ax.set_xticks(list(qs))
    ax.set_xticklabels([f"Q{q}" for q in qs])


def _plot_ls_cumulative(
    ax,
    df: pd.DataFrame,
    spy_prices: pd.DataFrame,
    spy_monthly: pd.Series,
) -> None:
    """
    Three lines:
      ① Long Q5 / Short Q1  (pure factor alpha)
      ② Long Q5 only         (long-only factor portfolio)
      ③ SPY buy-and-hold     (market benchmark, same period)

    Comparing ① vs ③ shows factor alpha net of market exposure.
    Comparing ② vs ③ shows how much the long leg adds vs passive.
    """
    valid = df.dropna(subset=["quintile", "forward_return"])
    if valid.empty or valid["quintile"].nunique() < _N_QUINTILES:
        _style_ax(ax, "Cumulative Return vs Benchmark")
        return

    q_by_date = (
        valid.groupby(["date", "quintile"])["forward_return"]
             .mean()
             .unstack("quintile")
    )

    if 5 not in q_by_date.columns or 1 not in q_by_date.columns:
        _style_ax(ax, "Cumulative Return vs Benchmark")
        return

    ls_returns   = q_by_date[5] - q_by_date[1]    # L/S
    long_returns = q_by_date[5]                    # Long Q5
    cum_ls   = (1 + ls_returns).cumprod()   - 1
    cum_long = (1 + long_returns).cumprod() - 1

    dates = pd.to_datetime(q_by_date.index)

    # SPY cumulative (aligned to same rebalancing dates)
    cum_spy = None
    if not spy_monthly.empty:
        spy_aligned = spy_monthly.reindex(q_by_date.index, fill_value=0.0)
        cum_spy = (1 + spy_aligned).cumprod() - 1

    # ── Plot ──────────────────────────────────────────────────────────────────
    ax.plot(dates, cum_ls   * 100, color=_STYLE["blue"],   linewidth=2.0,
            zorder=4, label="L/S (Long Q5 / Short Q1)")
    ax.plot(dates, cum_long * 100, color=_STYLE["green"],  linewidth=1.5,
            zorder=3, linestyle="--", label="Long Q5 only", alpha=0.85)
    if cum_spy is not None:
        ax.plot(dates, cum_spy * 100, color=_STYLE["amber"], linewidth=1.5,
                zorder=3, linestyle=":",  label="S&P 500 (SPY)", alpha=0.95)

    ax.fill_between(dates, 0, cum_ls * 100,
                    where=(cum_ls >= 0), alpha=0.08,
                    color=_STYLE["green"], zorder=2)
    ax.fill_between(dates, 0, cum_ls * 100,
                    where=(cum_ls < 0), alpha=0.08,
                    color=_STYLE["red"], zorder=2)
    ax.axhline(0, color=_STYLE["text"], linewidth=0.8, zorder=1)

    # Stats annotation
    sharpe   = _compute_sharpe(ls_returns)
    final_ls = cum_ls.iloc[-1] * 100
    final_spy = cum_spy.iloc[-1] * 100 if cum_spy is not None else float("nan")
    title = (
        f"Cumulative Return vs Benchmark\n"
        f"L/S: {final_ls:+.1f}%  ·  "
        f"SPY: {final_spy:+.1f}%  ·  "
        f"L/S Sharpe: {sharpe:.2f}"
    )

    ax.legend(loc="upper left", fontsize=8, framealpha=0.85)
    _style_ax(ax, title, ylabel="Cumulative Return (%)")
    if mdates:
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
        for lbl in ax.xaxis.get_majorticklabels():
            lbl.set_rotation(30)
            lbl.set_ha("right")


def _plot_ic_timeseries(ax, df: pd.DataFrame) -> None:
    """
    Rolling monthly IC per signal (3-month smoothed).
    Shows signal consistency and whether predictive power is decaying.
    """
    valid = df.dropna(subset=["forward_return"])
    if valid.empty:
        _style_ax(ax, "Rolling Monthly IC by Signal (3-month smoothed)")
        return

    import matplotlib.cm as cm

    signal_ics: dict[str, pd.Series] = {}
    for sig in _PIT_SIGNAL_NAMES:
        if sig not in valid.columns:
            continue
        ic_by_date: dict = {}
        for dt, grp in valid.groupby("date"):
            vals = grp[sig].values
            rets = grp["forward_return"].values
            mask = vals != 0
            if mask.sum() < 10:
                continue
            ic, _ = stats.spearmanr(vals[mask], rets[mask])
            if not np.isnan(ic):
                ic_by_date[dt] = ic
        if ic_by_date:
            signal_ics[sig] = pd.Series(ic_by_date).sort_index()

    if not signal_ics:
        _style_ax(ax, "Rolling Monthly IC by Signal (3-month smoothed)")
        return

    colors  = [_STYLE["blue"], _STYLE["green"], _STYLE["purple"]]
    plotted = 0
    for (sig, ic_series), color in zip(signal_ics.items(), colors):
        smoothed = ic_series.rolling(3, min_periods=1).mean()
        ax.plot(
            pd.to_datetime(ic_series.index), smoothed,
            label=sig.replace("_", " "),
            color=color, linewidth=1.6, alpha=0.85,
        )
        plotted += 1

    ax.axhline(0,    color=_STYLE["text"],  linewidth=1.0, zorder=1)
    ax.axhline(0.05, color=_STYLE["green"], linewidth=0.8,
               linestyle="--", alpha=0.6, zorder=1)
    ax.axhline(-0.05, color=_STYLE["red"],  linewidth=0.8,
               linestyle="--", alpha=0.6, zorder=1)

    if valid["date"].nunique() > 0:
        earliest = pd.to_datetime(valid["date"].min())
        ax.text(earliest, 0.052, "IC=0.05 threshold",
                fontsize=7, color=_STYLE["green"], alpha=0.7)

    _style_ax(
        ax, "Rolling Monthly IC by Signal  (3-month smoothed)",
        ylabel="IC (Spearman ρ with 21d forward return)",
    )
    ax.legend(loc="upper right", fontsize=8.5, ncol=3,
              framealpha=0.85, edgecolor=_STYLE["grid"])
    if mdates:
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
        for lbl in ax.xaxis.get_majorticklabels():
            lbl.set_rotation(30)
            lbl.set_ha("right")


def _plot_sector_attribution(
    ax,
    df: pd.DataFrame,
    portfolio_symbols: list[str],
) -> None:
    """
    Q5–Q1 spread per GICS sector.
    Highlights which sectors the model works best / worst in.
    Portfolio sectors highlighted in blue.
    """
    valid = df.dropna(subset=["quintile", "forward_return", "sector"])
    if valid.empty:
        _style_ax(ax, "Sector Attribution (Q5–Q1 Spread)")
        return

    sector_spread: dict[str, float] = {}
    for sector, grp in valid.groupby("sector"):
        q5 = grp[grp["quintile"] == 5]["forward_return"].mean()
        q1 = grp[grp["quintile"] == 1]["forward_return"].mean()
        n  = len(grp)
        if not np.isnan(q5) and not np.isnan(q1) and n >= 20:
            sector_spread[sector] = (q5 - q1) * 100

    if not sector_spread:
        _style_ax(ax, "Sector Attribution (Q5–Q1 Spread)")
        return

    sectors = sorted(sector_spread, key=sector_spread.get)
    spreads = [sector_spread[s] for s in sectors]
    colors  = [_STYLE["green"] if s >= 0 else _STYLE["red"] for s in spreads]

    bars = ax.barh(sectors, spreads, color=colors, alpha=0.85, zorder=3)
    ax.axvline(0, color=_STYLE["text"], linewidth=0.8, zorder=2)

    for bar, val in zip(bars, spreads):
        ax.text(
            val + (0.02 if val >= 0 else -0.02),
            bar.get_y() + bar.get_height() / 2,
            f"{val:+.2f}%", va="center",
            ha="left" if val >= 0 else "right",
            fontsize=8, color=_STYLE["text"],
        )

    # Highlight portfolio sectors
    if portfolio_symbols:
        port_sectors = {
            get_ticker_sector(s)
            for s in portfolio_symbols
            if get_ticker_sector(s)
        }
        for i, sector in enumerate(sectors):
            if sector in port_sectors:
                try:
                    ax.get_yticklabels()[i].set_color(_STYLE["blue"])
                    ax.get_yticklabels()[i].set_fontweight("bold")
                except IndexError:
                    pass

    _style_ax(
        ax,
        "Sector Attribution  (Q5–Q1 spread, blue = your holdings)",
        xlabel="Avg 21d Return Spread (%)",
    )
    ax.tick_params(axis="y", labelsize=8)


def _plot_signal_ic_summary(ax, df: pd.DataFrame) -> None:
    """
    Mean IC per signal with bootstrap standard-deviation error bars.
    Green bar = IC ≥ 0.05 (useful); amber = 0 ≤ IC < 0.05; red = negative.
    """
    valid = df.dropna(subset=["forward_return"])
    if valid.empty:
        _style_ax(ax, "Per-Signal IC Summary")
        return

    ic_means, ic_stds, labels = [], [], []
    for sig in _PIT_SIGNAL_NAMES:
        if sig not in valid.columns:
            continue
        mask = valid[sig] != 0
        if mask.sum() < 20:
            continue
        ic, _ = stats.spearmanr(valid.loc[mask, sig], valid.loc[mask, "forward_return"])
        if np.isnan(ic):
            continue

        # Bootstrap std (IC across monthly periods)
        ics_by_date = []
        for _, grp in valid[mask].groupby("date"):
            v = grp[sig].values
            r = grp["forward_return"].values
            if len(v) >= 5:
                ic_d, _ = stats.spearmanr(v, r)
                if not np.isnan(ic_d):
                    ics_by_date.append(ic_d)

        ic_means.append(ic)
        ic_stds.append(np.std(ics_by_date) if len(ics_by_date) > 1 else 0.0)
        labels.append(sig.replace("_", "\n"))

    if not ic_means:
        _style_ax(ax, "Per-Signal IC Summary")
        return

    xs     = np.arange(len(labels))
    colors = [
        _STYLE["green"] if ic >= 0.05
        else _STYLE["amber"] if ic >= 0
        else _STYLE["red"]
        for ic in ic_means
    ]

    ax.bar(xs, ic_means, color=colors, alpha=0.85, zorder=3, width=0.55)
    ax.errorbar(xs, ic_means, yerr=ic_stds,
                fmt="none", color=_STYLE["text"],
                capsize=4, linewidth=1.2, zorder=4)
    ax.axhline(0,    color=_STYLE["text"],  linewidth=0.8, zorder=2)
    ax.axhline(0.05, color=_STYLE["green"], linewidth=0.8,
               linestyle="--", alpha=0.6, zorder=1)
    ax.axhline(-0.05, color=_STYLE["red"],  linewidth=0.8,
               linestyle="--", alpha=0.6, zorder=1)

    for x, ic in zip(xs, ic_means):
        ax.text(x, ic + (0.004 if ic >= 0 else -0.008),
                f"{ic:+.3f}", ha="center",
                va="bottom" if ic >= 0 else "top",
                fontsize=8, color=_STYLE["text"])

    _style_ax(
        ax,
        "Per-Signal IC Summary  (green ≥ 0.05 threshold)",
        ylabel="IC (Spearman ρ, 21d forward return)",
    )
    ax.set_xticks(xs)
    ax.set_xticklabels(labels, fontsize=8)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _compute_sharpe(returns: pd.Series, periods_per_year: int = 12) -> float:
    """Annualized Sharpe ratio assuming monthly returns, zero risk-free rate."""
    if len(returns) < 4 or returns.std() == 0:
        return 0.0
    return float(returns.mean() / returns.std() * math.sqrt(periods_per_year))


def _print_summary(
    df: pd.DataFrame,
    spy_monthly: pd.Series,
    period_label: str,
) -> None:
    valid    = df.dropna(subset=["quintile", "forward_return"])
    if valid.empty:
        return

    q_means  = valid.groupby("quintile")["forward_return"].mean() * 100
    spread   = q_means.get(5, 0.0) - q_means.get(1, 0.0)
    hit_rate = (valid["forward_return"] * np.sign(valid["composite_score"]) > 0).mean()

    q_by_date = (valid.groupby(["date", "quintile"])["forward_return"]
                       .mean().unstack("quintile"))
    ls_ret = (
        q_by_date.get(5, pd.Series(dtype=float)) -
        q_by_date.get(1, pd.Series(dtype=float))
    ).dropna()
    sharpe = _compute_sharpe(ls_ret) if len(ls_ret) > 3 else float("nan")

    spy_cum = float("nan")
    if not spy_monthly.empty:
        spy_aligned = spy_monthly.reindex(q_by_date.index, fill_value=0.0)
        spy_cum     = ((1 + spy_aligned).cumprod().iloc[-1] - 1) * 100

    ls_cum = ((1 + ls_ret).cumprod().iloc[-1] - 1) * 100 if len(ls_ret) > 0 else float("nan")

    print("\n" + "=" * 60)
    print(f"BACKTEST SUMMARY  —  {period_label}")
    print("=" * 60)
    print(f"  Observations      : {len(valid):,} stock-months")
    print(f"  Rebalancing dates : {valid['date'].nunique()}")
    print(f"  Stocks covered    : {valid['symbol'].nunique()}")
    print(f"  Q5 avg return     : {q_means.get(5, float('nan')):+.2f}%/month")
    print(f"  Q1 avg return     : {q_means.get(1, float('nan')):+.2f}%/month")
    print(f"  Q5–Q1 spread      : {spread:+.2f}%/month")
    print(f"  L/S cumulative    : {ls_cum:+.1f}%")
    print(f"  SPY cumulative    : {spy_cum:+.1f}%")
    print(f"  L/S Sharpe ratio  : {sharpe:.2f}")
    print(f"  Hit rate (dir.)   : {hit_rate:.1%}")
    print("  ─────────────────────────────────────────────────────")
    print("  Signals            : momentum_12m_1m + momentum_1m + rel_strength")
    print("  Look-ahead bias    : None (strictly point-in-time price data)")
    print("  Survivorship bias  : Present (current S&P 500 constituents)")
    print("=" * 60 + "\n")
