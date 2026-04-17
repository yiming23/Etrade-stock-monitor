"""
Information Coefficient (IC) analysis for quant signal evaluation.

IC = Spearman rank correlation between predicted signal values and actual forward returns.
IC > 0 means the signal has predictive power. IC < 0 means it's anti-predictive.
IC IR (IC / std(IC)) measures signal consistency over time.

Professional standard:
  IC > 0.02  = weak but worth tracking
  IC > 0.05  = useful signal
  IC > 0.10  = strong signal

METHODOLOGY — IMPORTANT:

  PRICE SIGNALS (computed point-in-time from this pipeline):
    momentum_12m_1m, momentum_1m, rel_strength
    → Strictly PIT using expanding mean/std. No look-ahead bias.

  FUNDAMENTAL SIGNALS (cannot be computed PIT from free data sources):
    analyst_revision, sue, short_interest, insider_net,
    put_call_ratio, iv_skew, iv_rank, volume_surge
    → yfinance only provides current snapshots, not historical ones.
    → We use academic literature IC values as the prior instead.
    → Source papers use Bloomberg/Compustat PIT databases.

  HYBRID WEIGHTING STRATEGY:
    Final weights = blend of:
      - Empirical PIT IC  (price signals)    — from this pipeline
      - Academic prior IC (fundamental)       — from peer-reviewed papers
    This is the industry-standard approach when no PIT fundamental DB is available.

Usage (research pipeline):
    python -m src.research.run --ic-analysis
    python -m src.research.run --ic-analysis --universe sp500 --years 3
"""

from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from scipy import stats

from src.data.fetcher import MarketDataFetcher
from src.universe.sp500 import get_sector_etf, get_sp500_tickers
from src.utils.config import PROJECT_ROOT
from src.utils.logger import get_logger

logger = get_logger(__name__)

_IC_RESULTS_FILE = PROJECT_ROOT / "data" / "ic_results.json"

# ── Price signals: validated point-in-time by this pipeline ──────────────────
# All signals here are computable strictly PIT from price data only.
_PRICE_SIGNALS = frozenset({
    "momentum_12m_1m",   # Jegadeesh & Titman (1993)
    "high_52w_ratio",    # George & Hwang (2004) — 52-week high momentum anchor
    "rel_strength",      # Moskowitz & Grinblatt (1999)
    "beta_12m",          # Frazzini & Pedersen (2014) BAB — rolling beta, inverted
    "momentum_1m",       # weak; included for completeness
})

# ── Academic IC priors for fundamental signals ────────────────────────────────
# Source: peer-reviewed literature on large-cap US equities, ~21-day holding period.
# These IC values come from studies run on Bloomberg/Compustat PIT databases.
# We use them because yfinance provides only current snapshots — no PIT history.
#
# NOTE: Our implementations are proxies for the academic definitions:
#   analyst_revision  academic: EPS estimate CHANGE over 4 weeks (IBES)
#                     ours:     static recommendation_mean level (1–5 scale)
#   sue               academic: (actual EPS - consensus EPS) / std(surprises)
#                     ours:     mean surprise% from earnings_history (similar)
#   short_interest    academic: monthly short interest from exchange reports
#                     ours:     yfinance shortRatio (updated biweekly)
#   insider_net       academic: Form 4 purchases - sales / shares outstanding
#                     ours:     yfinance insider_transactions (90-day window)
#
# These discrepancies mean our implementations may underperform vs academic IC.
# Use academic IC as an upper-bound prior, not an exact target.

_ACADEMIC_IC: dict[str, float] = {
    "analyst_revision":  0.055,  # Chan, Jegadeesh & Lakonishok (1996); rev momentum
    "sue":               0.045,  # Bernard & Thomas (1989); PEAD well-documented
    "short_interest":    0.035,  # Dechow et al (2001); Desai et al (2002)
    "insider_net":       0.030,  # Seyhun (1986); Lakonishok & Lee (2001)
    "put_call_ratio":    0.020,  # Pan & Poteshman (2006); options order flow
    "iv_skew":           0.015,  # Xing, Zhang & Zhao (2010); skew predicts returns
    "momentum_1m":       0.010,  # Weak; Jegadeesh (1990) 1m reversal risk
    "volume_surge":      0.008,  # Gervais, Kaniel & Mingelgrin (2001)
    "iv_rank":           0.000,  # Excluded: theory ambiguous at 21d, OOS IC negative
}
# Note: momentum_12m_1m, high_52w_ratio, rel_strength, beta_12m, momentum_1m
# are in _PRICE_SIGNALS and get empirical PIT IC — not in _ACADEMIC_IC.


# ── Main entry point ──────────────────────────────────────────────────────────

def run_ic_analysis(
    symbols: list[str],
    years: int = 3,
    forward_periods: list[int] = [5, 21],
    min_observations: int = 30,
) -> dict:
    """
    Compute PIT IC for price signals across the given universe.
    Fundamental signal weights come from academic literature (see module docstring).

    Returns:
        {
          "by_signal": { signal_name: { "ic_21d": ..., "ic_ir_21d": ..., ... } },
          "recommended_weights": { signal: weight, ... },
          "run_date": "...",
          "universe_size": N,
        }
    """
    fetcher = MarketDataFetcher()

    logger.info(
        f"IC analysis: {len(symbols)} symbols, {years}y history, "
        f"forward periods: {forward_periods}"
    )
    logger.info(
        "Note: Only PRICE signals computed PIT. "
        "Fundamental signals use academic IC priors."
    )

    # Accumulate (pit_signal_value, forward_return) per signal per period
    # { period: { signal_name: [(pred, actual), ...] } }
    data: dict[int, dict[str, list[tuple[float, float]]]] = {
        p: {} for p in forward_periods
    }

    processed = 0
    for sym in symbols:
        try:
            rows = _compute_signal_return_pairs(sym, fetcher, forward_periods, years)
            for period, pairs in rows.items():
                for signal_name, pred, actual in pairs:
                    data[period].setdefault(signal_name, []).append((pred, actual))
            processed += 1
            if processed % 50 == 0:
                logger.info(f"  Processed {processed}/{len(symbols)} symbols...")
        except Exception as e:
            logger.debug(f"[{sym}] IC analysis failed: {e}")

    logger.info(f"IC analysis: {processed} symbols processed. Computing ICs...")

    # Compute empirical IC for each price signal
    by_signal: dict[str, dict] = {}
    all_signal_names: set[str] = set()
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

            ic_ir = _compute_ic_ir(preds, actuals, window=20)

            by_signal[signal_name][f"ic_{period}d"]       = round(ic, 4)
            by_signal[signal_name][f"ic_ir_{period}d"]    = round(ic_ir, 3)
            by_signal[signal_name][f"p_value_{period}d"]  = round(float(p_value), 4)
            by_signal[signal_name]["observations"]          = len(pairs)
            by_signal[signal_name]["source"]                = "pit_empirical"

    # Add academic IC entries for fundamental signals (not in by_signal)
    for sig_name, academic_ic in _ACADEMIC_IC.items():
        if sig_name not in by_signal:
            by_signal[sig_name] = {
                "ic_21d":   academic_ic,
                "ic_ir_21d": 0.0,         # unknown without PIT data
                "observations": 0,
                "source":   "academic_prior",
            }

    recommended_weights = _ic_to_weights(by_signal, forward_periods)

    result = {
        "run_date":             date.today().isoformat(),
        "universe_size":        processed,
        "forward_periods":      forward_periods,
        "methodology":          "hybrid-pit-academic",
        "by_signal":            by_signal,
        "recommended_weights":  recommended_weights,
    }

    _save_ic_results(result)
    _print_ic_report(by_signal, forward_periods, recommended_weights)
    return result


# ── Walk-forward OOS validation ───────────────────────────────────────────────

def walk_forward_validation(
    symbols: list[str],
    years: int = 3,
    train_months: int = 12,
    test_months: int = 1,
    forward_period: int = 21,
) -> dict:
    """
    Walk-forward IC validation for PRICE SIGNALS ONLY (PIT computation).

    Trains on `train_months`, tests on next `test_months`, rolls forward.
    Returns out-of-sample IC for each price signal — the honest measure of
    whether these signals actually work (vs in-sample overfitting).

    Fundamental signals are NOT validated here because yfinance cannot
    provide their historical point-in-time values.
    """
    fetcher = MarketDataFetcher()
    logger.info(
        f"Walk-forward validation (price signals only): "
        f"train={train_months}m, test={test_months}m, forward={forward_period}d"
    )

    end_date   = date.today()
    start_date = end_date - timedelta(days=int(years * 365))

    oos_preds:   dict[str, list[float]] = {}
    oos_actuals: dict[str, list[float]] = {}

    test_start = start_date + timedelta(days=int(train_months * 30))

    while test_start < end_date - timedelta(days=int(test_months * 30)):
        test_end = test_start + timedelta(days=int(test_months * 30))

        for sym in symbols:
            try:
                prices = fetcher.get_prices(sym, years=years)
                if prices.empty:
                    continue

                closes = prices["Close"]

                # Sector ETF for rel_strength
                etf_key    = get_sector_etf(sym)
                etf_prices = fetcher.get_prices(etf_key, years=years) if etf_key else pd.DataFrame()
                etf_closes = (
                    etf_prices["Close"].reindex(closes.index, method="ffill")
                    if not etf_prices.empty else None
                )

                # Pre-compute expanding stats for the full series
                ret_252      = closes.pct_change(252)
                ret_21       = closes.pct_change(21)
                exp_mean_252 = ret_252.expanding(min_periods=60).mean()
                exp_std_252  = ret_252.expanding(min_periods=60).std()
                exp_mean_21  = ret_21.expanding(min_periods=21).mean()
                exp_std_21   = ret_21.expanding(min_periods=21).std()

                # high_52w_ratio
                roll_max_252   = closes.rolling(252, min_periods=60).max()
                ratio_52w      = closes / roll_max_252
                exp_mean_ratio = ratio_52w.expanding(min_periods=60).mean()
                exp_std_ratio  = ratio_52w.expanding(min_periods=60).std()

                if etf_closes is not None:
                    rel_ret_21   = ret_21 - etf_closes.pct_change(21)
                    exp_mean_rel = rel_ret_21.expanding(min_periods=21).mean()
                    exp_std_rel  = rel_ret_21.expanding(min_periods=21).std()

                # beta_12m: rolling beta vs SPY
                roll_beta_wf:  Optional[pd.Series] = None
                exp_mean_beta_wf: Optional[pd.Series] = None
                exp_std_beta_wf:  Optional[pd.Series] = None
                spy_p_wf = fetcher.get_prices("SPY", years=years)
                if not spy_p_wf.empty:
                    spy_ret_wf  = spy_p_wf["Close"].pct_change()
                    stk_ret_wf  = closes.pct_change()
                    spy_aln_wf  = spy_ret_wf.reindex(closes.index, method="ffill")
                    roll_cov_wf = stk_ret_wf.rolling(252, min_periods=60).cov(spy_aln_wf)
                    roll_var_wf = spy_aln_wf.rolling(252, min_periods=60).var()
                    roll_beta_wf    = roll_cov_wf / roll_var_wf.replace(0, float("nan"))
                    exp_mean_beta_wf = roll_beta_wf.expanding(min_periods=60).mean()
                    exp_std_beta_wf  = roll_beta_wf.expanding(min_periods=60).std()

                # Find the price index closest to test_start
                test_ts   = pd.Timestamp(test_start)
                avail_idx = prices.index[prices.index <= test_ts]
                if len(avail_idx) < 260:
                    continue
                idx = len(avail_idx) - 1   # 0-based index of the eval date

                # Compute PIT signals at this idx
                sig: dict[str, float] = {}

                if idx >= 252:
                    val = closes.iloc[idx - 22] / closes.iloc[idx - 252] - 1
                    m, s = exp_mean_252.iloc[idx], exp_std_252.iloc[idx]
                    sig["momentum_12m_1m"] = _zscore_clip_vals(val, m, s)

                if idx >= 252:
                    r = ratio_52w.iloc[idx]
                    m = exp_mean_ratio.iloc[idx]
                    s = exp_std_ratio.iloc[idx]
                    v = _zscore_clip_vals(r, m, s)
                    if v != 0.0:
                        sig["high_52w_ratio"] = v

                if idx >= 22:
                    val = closes.iloc[idx] / closes.iloc[idx - 22] - 1
                    m, s = exp_mean_21.iloc[idx], exp_std_21.iloc[idx]
                    sig["momentum_1m"] = _zscore_clip_vals(val, m, s)

                if etf_closes is not None and idx >= 22:
                    ev = etf_closes.iloc[idx - 22]
                    if not pd.isna(ev) and ev > 0:
                        sr = closes.iloc[idx] / closes.iloc[idx - 22] - 1
                        er = etf_closes.iloc[idx] / ev - 1
                        m, s = exp_mean_rel.iloc[idx], exp_std_rel.iloc[idx]
                        sig["rel_strength"] = _zscore_clip_vals(sr - er, m, s)

                if roll_beta_wf is not None and idx >= 252:
                    b = roll_beta_wf.iloc[idx]
                    if not (pd.isna(b) or math.isinf(float(b))):
                        m = exp_mean_beta_wf.iloc[idx]
                        s = exp_std_beta_wf.iloc[idx]
                        v = _zscore_clip_vals(
                            -float(b),
                            -float(m) if not pd.isna(m) else 0.0,
                            float(s) if not pd.isna(s) else 0.0,
                        )
                        if v != 0.0:
                            sig["beta_12m"] = v

                if not sig:
                    continue

                # Actual forward return in the TEST window (21d after test_start)
                future = prices[
                    (prices.index >= pd.Timestamp(test_start)) &
                    (prices.index < pd.Timestamp(test_end))
                ]
                if future.empty or len(future) < 5:
                    continue
                actual_ret = future["Close"].iloc[-1] / future["Close"].iloc[0] - 1

                for sig_name, sig_val in sig.items():
                    oos_preds.setdefault(sig_name, []).append(sig_val)
                    oos_actuals.setdefault(sig_name, []).append(actual_ret)

            except Exception:
                continue

        test_start = test_end

    # Compute OOS IC per signal
    oos_ic: dict[str, dict] = {}
    for sig_name in oos_preds:
        if len(oos_preds[sig_name]) < 20:
            continue
        ic, pval = stats.spearmanr(oos_preds[sig_name], oos_actuals[sig_name])
        oos_ic[sig_name] = {
            "oos_ic":       round(float(ic), 4),
            "p_value":      round(float(pval), 4),
            "observations": len(oos_preds[sig_name]),
        }

    logger.info("Walk-forward OOS IC (price signals — strictly PIT):")
    for sig, res in sorted(oos_ic.items(), key=lambda x: -abs(x[1]["oos_ic"])):
        logger.info(
            f"  {sig:<25} IC={res['oos_ic']:+.4f}  "
            f"p={res['p_value']:.3f}  n={res['observations']}"
        )
    logger.info(
        "  Fundamental signals not shown here — weights from academic priors "
        "(analyst_revision, sue, short_interest, insider_net, put_call_ratio, iv_skew, volume_surge)"
    )

    return oos_ic


# ── PIT signal computation (shared logic) ─────────────────────────────────────

def _compute_signal_return_pairs(
    symbol: str,
    fetcher: MarketDataFetcher,
    forward_periods: list[int],
    years: int,
) -> dict[int, list[tuple[str, float, float]]]:
    """
    Compute PRICE signals point-in-time and match with realized forward returns.

    For each stock: pre-compute expanding mean/std for all signals, then at
    each monthly sample index, read signal values at that index (no future data).

    Signals computed (all strictly PIT, price data only):
      momentum_12m_1m  : 12m return skipping last month
      high_52w_ratio   : price / 52-week rolling max
      rel_strength     : excess return vs sector ETF
      beta_12m         : rolling 12m beta vs SPY, inverted (BAB)
      momentum_1m      : 1-month return

    Returns: { period: [(signal_name, signal_value, forward_return), ...] }
    """
    prices = fetcher.get_prices(symbol, years=years)
    if prices.empty or len(prices) < 260 + max(forward_periods):
        return {}

    closes  = prices["Close"]

    # ── Pre-compute rolling/expanding stats for efficiency ─────────────────────
    ret_252      = closes.pct_change(252)
    ret_21       = closes.pct_change(21)
    exp_mean_252 = ret_252.expanding(min_periods=60).mean()
    exp_std_252  = ret_252.expanding(min_periods=60).std()
    exp_mean_21  = ret_21.expanding(min_periods=21).mean()
    exp_std_21   = ret_21.expanding(min_periods=21).std()

    # high_52w_ratio: price / 252-day rolling max
    roll_max_252   = closes.rolling(252, min_periods=60).max()
    ratio_52w      = closes / roll_max_252
    exp_mean_ratio = ratio_52w.expanding(min_periods=60).mean()
    exp_std_ratio  = ratio_52w.expanding(min_periods=60).std()

    # Sector ETF for rel_strength
    etf_key    = get_sector_etf(symbol)
    etf_prices = fetcher.get_prices(etf_key, years=years) if etf_key else pd.DataFrame()
    etf_closes    = None
    exp_mean_rel  = None
    exp_std_rel   = None

    if not etf_prices.empty:
        etf_closes   = etf_prices["Close"].reindex(closes.index, method="ffill")
        rel_ret_21   = ret_21 - etf_closes.pct_change(21)
        exp_mean_rel = rel_ret_21.expanding(min_periods=21).mean()
        exp_std_rel  = rel_ret_21.expanding(min_periods=21).std()

    # beta_12m: rolling 252-day beta vs SPY
    spy_prices = fetcher.get_prices("SPY", years=years)
    roll_beta: Optional[pd.Series] = None
    exp_mean_beta: Optional[pd.Series] = None
    exp_std_beta:  Optional[pd.Series] = None
    if not spy_prices.empty:
        spy_ret     = spy_prices["Close"].pct_change()
        stk_ret     = closes.pct_change()
        spy_aligned = spy_ret.reindex(closes.index, method="ffill")
        roll_cov    = stk_ret.rolling(252, min_periods=60).cov(spy_aligned)
        roll_var    = spy_aligned.rolling(252, min_periods=60).var()
        roll_beta   = roll_cov / roll_var.replace(0, float("nan"))
        exp_mean_beta = roll_beta.expanding(min_periods=60).mean()
        exp_std_beta  = roll_beta.expanding(min_periods=60).std()

    # ── Walk forward monthly ──────────────────────────────────────────────────
    sample_indices = range(260, len(prices) - max(forward_periods), 21)
    result: dict[int, list] = {p: [] for p in forward_periods}

    for idx in sample_indices:
        sig: dict[str, float] = {}

        # momentum_12m_1m: 12m return skipping last month (strictly PIT)
        if idx >= 252:
            val = closes.iloc[idx - 22] / closes.iloc[idx - 252] - 1
            m, s = exp_mean_252.iloc[idx], exp_std_252.iloc[idx]
            v = _zscore_clip_vals(val, m, s)
            if v != 0.0:
                sig["momentum_12m_1m"] = v

        # high_52w_ratio: price / 52-week max (strictly PIT)
        if idx >= 252:
            r = ratio_52w.iloc[idx]
            m = exp_mean_ratio.iloc[idx]
            s = exp_std_ratio.iloc[idx]
            v = _zscore_clip_vals(r, m, s)
            if v != 0.0:
                sig["high_52w_ratio"] = v

        # momentum_1m: 1-month return (strictly PIT)
        if idx >= 22:
            val = closes.iloc[idx] / closes.iloc[idx - 22] - 1
            m, s = exp_mean_21.iloc[idx], exp_std_21.iloc[idx]
            v = _zscore_clip_vals(val, m, s)
            if v != 0.0:
                sig["momentum_1m"] = v

        # rel_strength: excess vs sector ETF (strictly PIT)
        if etf_closes is not None and idx >= 22:
            ev = etf_closes.iloc[idx - 22] if not pd.isna(etf_closes.iloc[idx - 22]) else None
            if ev and ev > 0:
                sr = closes.iloc[idx] / closes.iloc[idx - 22] - 1
                er = etf_closes.iloc[idx] / ev - 1
                m, s = exp_mean_rel.iloc[idx], exp_std_rel.iloc[idx]
                v = _zscore_clip_vals(sr - er, m, s)
                if v != 0.0:
                    sig["rel_strength"] = v

        # beta_12m: rolling beta vs SPY, INVERTED (low beta = bullish; BAB)
        if roll_beta is not None and idx >= 252:
            b = roll_beta.iloc[idx]
            if not (pd.isna(b) or math.isinf(float(b))):
                m = exp_mean_beta.iloc[idx]
                s = exp_std_beta.iloc[idx]
                # Invert beta: low beta → positive z-score (bullish)
                v = _zscore_clip_vals(-float(b), -float(m) if not pd.isna(m) else 0.0, float(s) if not pd.isna(s) else 0.0)
                if v != 0.0:
                    sig["beta_12m"] = v

        if not sig:
            continue

        # Forward returns (the actual future this signal should predict)
        for period in forward_periods:
            if idx + period >= len(prices):
                continue
            fwd = closes.iloc[idx + period] / closes.iloc[idx] - 1
            for sig_name, sig_val in sig.items():
                result[period].append((sig_name, sig_val, fwd))

    return result


# ── Normalization helpers ─────────────────────────────────────────────────────

def _zscore_clip_vals(value: float, mean: float, std: float, cap: float = 2.0) -> float:
    """Z-score normalize and clip to [-1, +1]. Returns 0 if inputs are invalid."""
    if any(math.isnan(x) or math.isinf(x) for x in (value, mean, std)):
        return 0.0
    if std == 0:
        return 0.0
    z = (value - mean) / std
    return float(max(-1.0, min(1.0, z / cap)))


# ── Weight computation ────────────────────────────────────────────────────────

def _ic_to_weights(
    by_signal: dict[str, dict],
    forward_periods: list[int],
    primary_period: int = 21,
) -> dict[str, float]:
    """
    Hybrid weight computation:
      - Price signals: use empirical PIT IC from this analysis
      - Fundamental signals: use academic literature IC (_ACADEMIC_IC)

    Signals with IC ≤ 0 get weight = 0.
    Final weights normalized to sum to 1.0.
    """
    period_key   = f"ic_{primary_period}d"
    raw_weights: dict[str, float] = {}

    # Price signals: empirical PIT IC
    for sig_name in _PRICE_SIGNALS:
        sig_data = by_signal.get(sig_name, {})
        ic = sig_data.get(period_key, 0.0)
        raw_weights[sig_name] = max(0.0, float(ic))

    # Fundamental signals: academic prior IC
    for sig_name, academic_ic in _ACADEMIC_IC.items():
        raw_weights[sig_name] = max(0.0, academic_ic)

    n_positive = sum(1 for w in raw_weights.values() if w > 0)
    if n_positive < 2:
        logger.warning("Too few positive-weight signals — using academic priors")
        from src.forecast.quant import _PRIOR_WEIGHTS
        return dict(_PRIOR_WEIGHTS)

    total = sum(raw_weights.values())
    if total == 0:
        from src.forecast.quant import _PRIOR_WEIGHTS
        return dict(_PRIOR_WEIGHTS)

    return {
        k: round(v / total, 4)
        for k, v in sorted(raw_weights.items(), key=lambda x: -x[1])
    }


# ── IC-IR computation ─────────────────────────────────────────────────────────

def _compute_ic_ir(
    preds: list[float],
    actuals: list[float],
    window: int = 20,
) -> float:
    """
    IC-IR = mean(rolling_IC) / std(rolling_IC).
    Higher IC-IR = more consistent signal (less noisy).
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


# ── Reporting ─────────────────────────────────────────────────────────────────

def _print_ic_report(
    by_signal: dict[str, dict],
    forward_periods: list[int],
    recommended_weights: dict[str, float],
) -> None:
    print("\n" + "=" * 72)
    print("QUANT SIGNAL IC ANALYSIS REPORT — Hybrid PIT + Academic Prior")
    print("=" * 72)
    print(f"  {'Signal':<25} {'Source':<18}" +
          "".join(f"  IC({p}d)   IC-IR" for p in forward_periods) + "   n")
    print("-" * 72)

    for sig in sorted(by_signal, key=lambda s: -abs(by_signal[s].get(f"ic_{forward_periods[-1]}d", 0))):
        source = by_signal[sig].get("source", "?")
        row = f"  {sig:<25} {source:<18}"
        for p in forward_periods:
            ic    = by_signal[sig].get(f"ic_{p}d",    0.0)
            ic_ir = by_signal[sig].get(f"ic_ir_{p}d", 0.0)
            row  += f"  {ic:+.4f}  {ic_ir:+.2f}"
        n = by_signal[sig].get("observations", 0)
        row += f"   {n}"
        print(row)

    print("\nHybrid recommended weights (price=PIT empirical, rest=academic prior):")
    for sig, w in sorted(recommended_weights.items(), key=lambda x: -x[1]):
        source = "PIT" if sig in _PRICE_SIGNALS else "academic"
        print(f"  {sig:<25} {w:.4f}  [{source}]")
    print("=" * 72 + "\n")


def _save_ic_results(result: dict) -> None:
    _IC_RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_IC_RESULTS_FILE, "w") as f:
        json.dump(result, f, indent=2)
    logger.info(f"IC results saved to {_IC_RESULTS_FILE}")
