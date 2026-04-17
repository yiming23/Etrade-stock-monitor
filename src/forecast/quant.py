"""
QuantForecaster — implements the Forecaster interface using quant signals.

Architecture:
  1. Compute SignalBundle for each position via signals.py
  2. Apply IC-weighted combination → composite score ∈ [-1, +1]
  3. Map score → direction + magnitude_pct + position recommendation
  4. Persist signal values and predictions for IC tracking

Model weights are stored in data/quant_model.json and updated by the
research pipeline (src/research/ic_analysis.py) as IC data accumulates.
On first run, academic prior weights are used.

The model is intentionally simple now (weighted sum).
Phase 2 (after 3-6 months of data): replace with Ridge/Logistic regression.

Usage:
    from src.forecast.quant import QuantForecaster
    forecaster = QuantForecaster()
    results = forecaster.forecast(positions, context={}, report_type="pre_market")
"""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path
from typing import Optional

import numpy as np

from src.data.fetcher import MarketDataFetcher
from src.forecast.base import ForecastResult, Forecaster
from src.forecast.signals import SignalBundle, compute_signals
from src.universe.sp500 import get_sector_etf
from src.utils.config import PROJECT_ROOT
from src.utils.logger import get_logger

logger = get_logger(__name__)

_MODEL_FILE = PROJECT_ROOT / "data" / "quant_model.json"

# ── Hybrid academic prior weights ────────────────────────────────────────────
# These are the FALLBACK weights used on first run before any IC analysis.
# They are normalized from academic literature IC values (21d holding period).
#
# METHODOLOGY — two-tier approach:
#   PRICE SIGNALS (momentum_12m_1m, momentum_1m, rel_strength):
#     After running `--ic-analysis`, these are replaced by empirical PIT IC values.
#     The prior below is from academic papers as a starting point.
#
#   FUNDAMENTAL SIGNALS (analyst_revision, sue, short_interest, etc.):
#     These CANNOT be backtested point-in-time with free data (yfinance only
#     provides current snapshots, not historical point-in-time values).
#     Weights come from peer-reviewed papers on Bloomberg/Compustat databases:
#       analyst_revision  Chan, Jegadeesh & Lakonishok (1996) — IC ≈ 0.055
#       sue               Bernard & Thomas (1989)             — IC ≈ 0.045
#       short_interest    Dechow et al (2001)                 — IC ≈ 0.035
#       insider_net       Seyhun (1986)                       — IC ≈ 0.030
#       put_call_ratio    Pan & Poteshman (2006)              — IC ≈ 0.020
#       iv_skew           Xing, Zhang & Zhao (2010)           — IC ≈ 0.015
#       momentum_1m       Jegadeesh & Titman (1993)           — IC ≈ 0.010
#       volume_surge      Gervais et al (2001)                — IC ≈ 0.008
#       iv_rank           Excluded (OOS IC negative)          — IC = 0.000
#       momentum_12m_1m   Jegadeesh & Titman (1993)           — IC ≈ 0.060 (prior)
#       rel_strength      Moskowitz & Grinblatt (1999)        — IC ≈ 0.040 (prior)
#
# Total raw IC ≈ 0.318 → normalized to sum to 1.0 below.

_PRIOR_WEIGHTS: dict[str, float] = {
    # price signals — prior from literature (replaced by PIT IC after --ic-analysis)
    "momentum_12m_1m":  0.1887,  # 0.060 / 0.318
    "analyst_revision": 0.1730,  # 0.055 / 0.318
    "sue":              0.1415,  # 0.045 / 0.318
    "short_interest":   0.1101,  # 0.035 / 0.318
    "insider_net":      0.0943,  # 0.030 / 0.318
    "rel_strength":     0.1258,  # 0.040 / 0.318
    "put_call_ratio":   0.0629,  # 0.020 / 0.318
    "iv_skew":          0.0472,  # 0.015 / 0.318
    "momentum_1m":      0.0314,  # 0.010 / 0.318
    "volume_surge":     0.0252,  # 0.008 / 0.318
    "iv_rank":          0.0000,  # excluded: OOS IC was negative
}

# Direction thresholds
_UP_THRESHOLD   =  0.12   # composite score > 0.12 → bullish
_DOWN_THRESHOLD = -0.12   # composite score < -0.12 → bearish

# Position recommendation thresholds
_ADD_THRESHOLD  =  0.20
_TRIM_THRESHOLD = -0.20


class QuantForecaster(Forecaster):
    """
    Quant signal-based forecaster.
    Implements the Forecaster interface so it can be used anywhere
    an LLMForecaster is used (Open/Closed Principle).
    """

    def __init__(self) -> None:
        self.fetcher = MarketDataFetcher()
        self.weights = self._load_weights()

    @property
    def name(self) -> str:
        return "QuantForecaster-v1"

    def forecast(
        self,
        positions: list,
        context: dict,
        report_type: str = "pre_market",
    ) -> list[ForecastResult]:
        """
        Run quant signals on all positions and return ForecastResult per symbol.
        """
        results = []
        for pos in positions:
            symbol = pos.symbol if hasattr(pos, "symbol") else str(pos)
            try:
                result = self._forecast_one(symbol)
                results.append(result)
            except Exception as e:
                logger.warning(f"[{symbol}] QuantForecaster failed: {e}")
        return results

    def forecast_symbols(self, symbols: list[str]) -> list[ForecastResult]:
        """
        Run quant signals on an arbitrary list of symbols.
        Used by the screener (not tied to portfolio positions).
        """
        results = []
        for sym in symbols:
            try:
                results.append(self._forecast_one(sym))
            except Exception as e:
                logger.debug(f"[{sym}] skipped: {e}")
        return results

    # ── Core prediction logic ────────────────────────────────────────────────

    def _forecast_one(self, symbol: str) -> ForecastResult:
        sector_etf = get_sector_etf(symbol)
        bundle = compute_signals(symbol, self.fetcher, sector_etf=sector_etf)
        score  = self._composite_score(bundle)

        direction, recommendation = self._classify(score)
        magnitude = self._estimate_magnitude(score, symbol)
        confidence = self._confidence_level(bundle.data_quality, abs(score))
        narrative  = self._build_narrative(symbol, bundle, score, direction)

        logger.debug(
            f"[{symbol}] score={score:+.3f} → {direction} "
            f"({magnitude:.1f}%) [{confidence}] quality={bundle.data_quality:.0%}"
        )

        return ForecastResult(
            symbol         = symbol,
            direction      = direction,
            magnitude_pct  = magnitude,
            recommendation = recommendation,
            confidence     = confidence,
            estimated_move = f"{'+' if direction == 'up' else '-' if direction == 'down' else '±'}{magnitude:.1f}%",
            narrative      = narrative,
        )

    def _composite_score(self, bundle: SignalBundle) -> float:
        """
        IC-weighted linear combination of all signals.
        Returns float in approximately [-1, +1].
        """
        signals = bundle.to_dict()
        skip = {"symbol", "data_quality"}

        total_weight = 0.0
        weighted_sum = 0.0

        for signal_name, weight in self.weights.items():
            val = signals.get(signal_name, 0.0)
            if val != 0.0:  # only include signals that were computed
                weighted_sum  += weight * val
                total_weight  += weight

        if total_weight < 0.01:
            return 0.0

        # Re-normalize by actual weight used (some signals may be missing)
        raw_score = weighted_sum / total_weight
        return max(-1.0, min(1.0, raw_score))

    def _classify(self, score: float) -> tuple[str, str]:
        """Map composite score to (direction, recommendation)."""
        if score >= _ADD_THRESHOLD:
            return "up",   "ADD"
        elif score >= _UP_THRESHOLD:
            return "up",   "BUY"
        elif score <= _TRIM_THRESHOLD:
            return "down", "TRIM"
        elif score <= _DOWN_THRESHOLD:
            return "down", "SELL"
        else:
            return "flat", "HOLD"

    def _estimate_magnitude(self, score: float, symbol: str) -> float:
        """
        Predict expected % move scaled by the stock's own historical volatility.
        magnitude = |score| × annualized_vol × sqrt(holding_days/252)

        Holding period proxy: 20 trading days (1 month).
        """
        try:
            prices = self.fetcher.get_prices(symbol, years=1)
            if prices.empty or len(prices) < 20:
                return abs(score) * 3.0   # fallback: 3% per unit score

            daily_vol = prices["Close"].pct_change().tail(60).std()
            annualized_vol = daily_vol * math.sqrt(252)
            # Scale for a ~20-day holding period
            period_vol = annualized_vol * math.sqrt(20 / 252)
            magnitude  = abs(score) * period_vol * 100  # convert to pct
            return round(max(0.5, min(magnitude, 25.0)), 1)  # cap at 25%
        except Exception:
            return abs(score) * 3.0

    def _confidence_level(self, data_quality: float, score_magnitude: float) -> str:
        """
        Confidence based on how many signals were available and how strong the score is.
        """
        if data_quality >= 0.7 and score_magnitude >= 0.20:
            return "high"
        elif data_quality >= 0.4 and score_magnitude >= 0.10:
            return "medium"
        else:
            return "low"

    def _build_narrative(
        self,
        symbol: str,
        bundle: SignalBundle,
        score: float,
        direction: str,
    ) -> str:
        """Build a short human-readable explanation of the top driving signals."""
        available = bundle.available_signals()
        if not available:
            return f"{symbol}: insufficient data for quant analysis."

        # Find top 2 contributing signals (by weight × |value|)
        contributions = {
            k: self.weights.get(k, 0) * abs(v)
            for k, v in available.items()
        }
        top_signals = sorted(contributions, key=contributions.get, reverse=True)[:2]

        signal_descriptions = {
            "analyst_revision":  "analyst recommendation",
            "sue":               "earnings surprise history",
            "momentum_12m_1m":   "12-month price momentum",
            "rel_strength":      "sector relative strength",
            "insider_net":       "insider transaction activity",
            "short_interest":    "short interest positioning",
            "put_call_ratio":    "options sentiment (put/call)",
            "iv_skew":           "options skew",
            "iv_rank":           "implied volatility level",
            "momentum_1m":       "1-month price momentum",
            "volume_surge":      "volume activity",
        }

        driver_text = " and ".join(
            signal_descriptions.get(s, s) for s in top_signals
        )
        direction_word = "bullish" if direction == "up" else "bearish" if direction == "down" else "neutral"
        q_score_pct   = f"{abs(score):.0%}"

        return (
            f"Quant signals are {direction_word} (composite score {score:+.2f}). "
            f"Primary drivers: {driver_text}. "
            f"Signal quality: {bundle.data_quality:.0%} of indicators available."
        )

    # ── Model weights management ─────────────────────────────────────────────

    def _load_weights(self) -> dict[str, float]:
        """Load weights from file, falling back to academic priors."""
        if _MODEL_FILE.exists():
            try:
                with open(_MODEL_FILE) as f:
                    data = json.load(f)
                weights = data.get("weights", {})
                if weights:
                    logger.debug(f"Quant model loaded (version: {data.get('version','?')})")
                    return weights
            except Exception as e:
                logger.warning(f"Failed to load quant model: {e}. Using priors.")

        # First run — save priors to disk
        self._save_weights(_PRIOR_WEIGHTS, version="v1.0-prior")
        return dict(_PRIOR_WEIGHTS)

    def _save_weights(self, weights: dict[str, float], version: str = "v1.0") -> None:
        _MODEL_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_MODEL_FILE, "w") as f:
            json.dump({
                "version":    version,
                "updated_at": date.today().isoformat(),
                "weights":    weights,
            }, f, indent=2)

    def update_weights(self, new_weights: dict[str, float], version: str) -> None:
        """Called by ic_analysis.py to update weights after IC computation."""
        self.weights = new_weights
        self._save_weights(new_weights, version=version)
        logger.info(f"Quant model weights updated → {version}")
