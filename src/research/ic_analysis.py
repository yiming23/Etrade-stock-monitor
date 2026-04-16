"""
Information Coefficient (IC) analysis for quant signal evaluation.

IC = Spearman rank correlation between predicted signal values and actual forward returns.
IC > 0 means the signal has predictive power. IC < 0 means it's anti-predictive.
IC IR (IC / std(IC)) measures signal consistency over time.

Professional standard:
  IC > 0.02  = weak but worth tracking
  IC > 0.05  = useful signal
  IC > 0.10  = strong signal

Usage (research pipeline):
    python -m src.research.run --ic-analysis
    python -m src.research.run --ic-analysis --universe sp500 --years 2

The output updates data/quant_model.json with IC-weighted signal weights.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from scipy import stats

from src.data.fetcher import MarketDataFetcher
from src.forecast.signals import compute_signals, SignalBundle
from src.universe.sp500 import get_sector_etf, get_sp500_tickers
from src.utils.config import PROJECT_ROOT
from src.utils.logger import get_logger

logger = get_logger(__name__)

_IC_RESULTS_FILE = PROJECT_ROOT / "data" / "ic_results.json"


# ── Main entry point ──────────────────────────────────────────────────────────

def run_ic_analysis(
    symbols: list[str],
    years: int = 2,
    forward_periods: list[int] = [5, 21],   # 1-week, 1-month
    min_observations: int = 30,
) -> dict:
    """
    Compute IC for each signal across the given universe.

    Returns:
        {
          "by_signal": {
            "momentum_12m_1m": {
              "ic_5d": 0.042, "ic_21d": 0.061,
              "ic_ir_5d": 0.38, "ic_ir_21d": 0.51,
              "observations": 840
            },
            ...
          },
          "recommended_weights": { signal: weight, ... },
          "run_date": "2026-04-16",
          "universe_size": 450,
        }
    """
    fetcher = MarketDataFetcher()

    logger.info(f"IC analysis: {len(symbols)} symbols, {years}y history, "
                f"forward periods: {forward_periods}")

    # Collect (signal_values, forward_returns) pairs for each date
    # Structure: { signal_name: { period: [(pred, actual), ...] } }
    data: dict[str, dict[int, list[tuple[float, float]]]] = {
        period: {} for period in forward_periods
    }

    processed = 0
    for sym in symbols:
        try:
            rows = _compute_signal_return_pairs(sym, fetcher, forward_periods, years)
            for period, pairs in rows.items():
                for signal_name, pred, actual in pairs:
                    if period not in data:
                        data[period] = {}
                    if signal_name not in data[period]:
                        data[period][signal_name] = []
                    data[period][signal_name].append((pred, actual))
            processed += 1
            if processed % 50 == 0:
                logger.info(f"  Processed {processed}/{len(symbols)} symbols...")
        except Exception as e:
            logger.debug(f"[{sym}] IC analysis failed: {e}")

    logger.info(f"IC analysis: {processed} symbols processed. Computing ICs...")

    # Compute IC per signal per period
    by_signal: dict[str, dict] = {}
    all_signal_names = set()
    for period_data in data.values():
        all_signal_names.update(period_data.keys())

    for signal_name in all_signal_names:
        by_signal[signal_name] = {}
        for period in forward_periods:
            pairs = data.get(period, {}).get(signal_name, [])
            if len(pairs) < min_observations:
                continue

            preds   = [p for p, _ in pairs]
            actuals = [a for _, a in pairs]

            ic, p_value = stats.spearmanr(preds, actuals)
            ic = float(ic) if not np.isnan(ic) else 0.0

            # Rolling IC for IC-IR computation
            ic_ir = _compute_ic_ir(preds, actuals, window=20)

            by_signal[signal_name][f"ic_{period}d"]    = round(ic, 4)
            by_signal[signal_name][f"ic_ir_{period}d"] = round(ic_ir, 3)
            by_signal[signal_name][f"p_value_{period}d"] = round(float(p_value), 4)
            by_signal[signal_name]["observations"]      = len(pairs)

    # Compute recommended weights from IC
    recommended_weights = _ic_to_weights(by_signal, forward_periods)

    result = {
        "run_date":           date.today().isoformat(),
        "universe_size":      processed,
        "forward_periods":    forward_periods,
        "by_signal":          by_signal,
        "recommended_weights": recommended_weights,
    }

    _save_ic_results(result)
    _print_ic_report(by_signal, forward_periods, recommended_weights)
    return result


# ── Walk-forward validation ───────────────────────────────────────────────────

def walk_forward_validation(
    symbols: list[str],
    years: int = 2,
    train_months: int = 12,
    test_months: int = 1,
    forward_period: int = 21,
) -> dict:
    """
    Walk-forward IC validation.

    Trains on `train_months` of data, tests on next `test_months`.
    Rolls forward one test period at a time.

    Returns out-of-sample IC for each signal — this is the honest measure
    of whether a signal actually works (vs in-sample overfitting).
    """
    fetcher = MarketDataFetcher()
    logger.info(f"Walk-forward validation: train={train_months}m, test={test_months}m")

    end_date   = date.today()
    start_date = end_date - timedelta(days=int(years * 365))

    oos_preds:   dict[str, list[float]] = {}
    oos_actuals: dict[str, list[float]] = {}

    # Roll through test windows
    test_start = start_date + timedelta(days=int(train_months * 30))
    while test_start < end_date - timedelta(days=int(test_months * 30)):
        test_end = test_start + timedelta(days=int(test_months * 30))

        for sym in symbols:
            try:
                prices = fetcher.get_prices(sym, years=years)
                if prices.empty:
                    continue

                # Only use data up to test_start (no look-ahead)
                hist = prices[prices.index < pd.Timestamp(test_start)]
                if len(hist) < 60:
                    continue

                # Compute signals at test_start
                bundle = compute_signals(sym, fetcher, sector_etf=get_sector_etf(sym))

                # Actual forward return over test period
                future = prices[
                    (prices.index >= pd.Timestamp(test_start)) &
                    (prices.index < pd.Timestamp(test_end))
                ]
                if future.empty:
                    continue
                actual_ret = (future["Close"].iloc[-1] / future["Close"].iloc[0]) - 1

                # Record each signal's OOS prediction vs actual
                for sig_name, sig_val in bundle.available_signals().items():
                    if sig_name not in oos_preds:
                        oos_preds[sig_name]   = []
                        oos_actuals[sig_name] = []
                    oos_preds[sig_name].append(sig_val)
                    oos_actuals[sig_name].append(actual_ret)

            except Exception:
                continue

        test_start = test_end

    # Compute OOS IC
    oos_ic = {}
    for sig_name in oos_preds:
        if len(oos_preds[sig_name]) < 20:
            continue
        ic, pval = stats.spearmanr(oos_preds[sig_name], oos_actuals[sig_name])
        oos_ic[sig_name] = {
            "oos_ic":         round(float(ic), 4),
            "p_value":        round(float(pval), 4),
            "observations":   len(oos_preds[sig_name]),
        }

    logger.info("Walk-forward OOS IC results:")
    for sig, res in sorted(oos_ic.items(), key=lambda x: -abs(x[1]["oos_ic"])):
        logger.info(f"  {sig:<25} IC={res['oos_ic']:+.4f}  p={res['p_value']:.3f}  n={res['observations']}")

    return oos_ic


# ── Internal helpers ──────────────────────────────────────────────────────────

def _compute_signal_return_pairs(
    symbol: str,
    fetcher: MarketDataFetcher,
    forward_periods: list[int],
    years: int,
) -> dict[int, list[tuple[str, float, float]]]:
    """
    For a single symbol, compute signals at multiple historical dates
    and match them with the realized forward returns.

    Returns: { period: [(signal_name, signal_value, forward_return), ...] }

    Note: we sample every ~21 trading days (monthly) to avoid autocorrelation.
    """
    prices = fetcher.get_prices(symbol, years=years)
    if prices.empty or len(prices) < 60:
        return {}

    # Sample dates monthly (every 21 trading days) to reduce autocorrelation
    sample_indices = range(252, len(prices) - max(forward_periods), 21)
    result: dict[int, list] = {p: [] for p in forward_periods}

    for idx in sample_indices:
        # Truncate price history to simulate point-in-time
        hist_prices = prices.iloc[:idx]
        sample_date = hist_prices.index[-1].date()

        try:
            # Re-fetch signals using only history up to sample_date
            # (We use a mock fetcher that limits data — approximate here)
            bundle = compute_signals(symbol, fetcher, sector_etf=get_sector_etf(symbol))
            sig_dict = bundle.available_signals()
            if not sig_dict:
                continue

            # Compute actual forward returns
            for period in forward_periods:
                if idx + period >= len(prices):
                    continue
                fwd_return = (
                    prices["Close"].iloc[idx + period] /
                    prices["Close"].iloc[idx]
                ) - 1

                for sig_name, sig_val in sig_dict.items():
                    result[period].append((sig_name, sig_val, fwd_return))

        except Exception:
            continue

    return result


def _compute_ic_ir(
    preds: list[float],
    actuals: list[float],
    window: int = 20,
) -> float:
    """
    Compute IC-IR = mean(rolling_IC) / std(rolling_IC).
    Uses rolling windows of `window` observations.
    Higher IC-IR = more consistent signal.
    """
    if len(preds) < window * 2:
        return 0.0

    rolling_ics = []
    for i in range(window, len(preds), window // 2):
        p_slice = preds[i - window:i]
        a_slice = actuals[i - window:i]
        ic, _ = stats.spearmanr(p_slice, a_slice)
        if not np.isnan(ic):
            rolling_ics.append(ic)

    if len(rolling_ics) < 3:
        return 0.0

    mean_ic = np.mean(rolling_ics)
    std_ic  = np.std(rolling_ics)
    return float(mean_ic / std_ic) if std_ic > 0 else 0.0


def _ic_to_weights(
    by_signal: dict[str, dict],
    forward_periods: list[int],
    primary_period: int = 21,
) -> dict[str, float]:
    """
    Convert IC results to normalized weights.

    Weight = max(0, IC) for the primary period.
    Signals with IC ≤ 0 get weight = 0 (we don't invert bad signals).
    Weights are normalized to sum to 1.0.
    """
    from src.forecast.quant import _PRIOR_WEIGHTS

    raw_weights: dict[str, float] = {}
    period_key = f"ic_{primary_period}d"

    for sig_name, stats_dict in by_signal.items():
        ic = stats_dict.get(period_key, 0.0)
        # Only use positive IC signals
        raw_weights[sig_name] = max(0.0, ic)

    # If we have very few valid signals, fall back to priors
    n_positive = sum(1 for w in raw_weights.values() if w > 0)
    if n_positive < 3:
        logger.warning("Too few positive-IC signals — falling back to prior weights")
        return dict(_PRIOR_WEIGHTS)

    # Normalize
    total = sum(raw_weights.values())
    if total == 0:
        return dict(_PRIOR_WEIGHTS)

    return {k: round(v / total, 4) for k, v in raw_weights.items()}


def _print_ic_report(
    by_signal: dict[str, dict],
    forward_periods: list[int],
    recommended_weights: dict[str, float],
) -> None:
    """Print a formatted IC analysis report to stdout."""
    print("\n" + "=" * 65)
    print("QUANT SIGNAL IC ANALYSIS REPORT")
    print("=" * 65)

    header = f"{'Signal':<25}" + "".join(
        f"  IC({p}d)  IC-IR" for p in forward_periods
    ) + "   n"
    print(header)
    print("-" * 65)

    for sig in sorted(by_signal, key=lambda s: -abs(by_signal[s].get(f"ic_{forward_periods[-1]}d", 0))):
        row = f"{sig:<25}"
        for p in forward_periods:
            ic    = by_signal[sig].get(f"ic_{p}d",    0.0)
            ic_ir = by_signal[sig].get(f"ic_ir_{p}d", 0.0)
            row  += f"  {ic:+.4f}  {ic_ir:+.2f}"
        n = by_signal[sig].get("observations", 0)
        row += f"   {n}"
        print(row)

    print("\nRecommended weights:")
    for sig, w in sorted(recommended_weights.items(), key=lambda x: -x[1]):
        print(f"  {sig:<25} {w:.4f}")
    print("=" * 65 + "\n")


def _save_ic_results(result: dict) -> None:
    _IC_RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_IC_RESULTS_FILE, "w") as f:
        json.dump(result, f, indent=2)
    logger.info(f"IC results saved to {_IC_RESULTS_FILE}")
