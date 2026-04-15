"""
Abstract Forecaster interface (Open/Closed Principle).

Any prediction strategy — LLM-based, ML-based, or rule-based —
implements this contract.  The backtest engine, email sender, and
scheduler all depend on this interface, NOT on the concrete implementation.

When you add a quantitative ML model later:
  1. Create  src/forecast/ml.py   with  class MLForecaster(Forecaster)
  2. Wire it in  src/main.py      where  StockAnalyzer  is instantiated
  3. Everything else (backtest, email, scheduler) stays unchanged.

That's the Open/Closed Principle in practice.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ForecastResult:
    """
    Canonical output of any Forecaster — independent of how it was produced.

    Kept minimal and serialisable so the backtest storage layer can persist
    it without knowing about LLMs, news articles, or any other detail.
    """
    symbol:            str
    direction:         str    # "up" | "down" | "flat"
    magnitude_pct:     float  # predicted absolute % move (always positive)
    recommendation:    str    # "BUY" | "SELL" | "HOLD" | "TRIM" | "ADD"
    confidence:        str = "medium"   # "high" | "medium" | "low"
    estimated_move:    str = ""         # human-readable, e.g. "+1.5% to +3%"
    narrative:         str = ""         # one-paragraph rationale


class Forecaster(ABC):
    """
    Interface every forecaster must implement.

    Inputs  : positions (what we hold), context (news / indicators / …)
    Output  : list[ForecastResult]  — one per symbol, always
    """

    @abstractmethod
    def forecast(
        self,
        positions: list,           # list[Position] from etrade/portfolio.py
        context: dict,             # flexible: {"articles": [...], "earnings": {...}}
        report_type: str = "pre_market",
    ) -> list[ForecastResult]:
        """Return one ForecastResult per symbol in positions."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier shown in logs and report headers."""
        ...
