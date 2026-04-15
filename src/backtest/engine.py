"""
Backtest engine — compare LLM predictions against actual EOD prices.

Usage:
  python -m src.main --backtest                  # all history
  python -m src.main --backtest --symbol AAPL    # one stock
  python -m src.main --backtest --since 2026-01-01
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date

from src.backtest.storage import PredictionDB
from src.utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_backtest(
    report_type: str = "pre_market",
    symbol: str | None = None,
    since: date | None = None,
    verbose: bool = True,
) -> dict:
    """
    Run backtest and print a report.

    Returns a summary dict with the key metrics.
    """
    db = PredictionDB()
    rows = db.get_backtest_rows(report_type=report_type, symbol=symbol, since=since)

    if not rows:
        print("\n⚠️  No matched prediction+actual rows found.")
        print("   Run --once --type pre_market first to generate predictions,")
        print("   then --fetch-actuals to pull EOD prices.\n")
        return {}

    summary = _compute_summary(rows)

    if verbose:
        _print_report(rows, summary, report_type)

    return summary


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------

def _compute_summary(rows: list[dict]) -> dict:
    """Aggregate accuracy metrics across all rows."""
    total = len(rows)
    correct_dir = sum(r["direction_correct"] for r in rows)

    # Magnitude error: |predicted_magnitude - abs(actual_change)|
    mag_errors = []
    for r in rows:
        pred_mag = r.get("predicted_magnitude_pct") or 0.0
        actual_chg = r.get("actual_day_change_pct")
        if actual_chg is not None:
            mag_errors.append(abs(pred_mag - abs(actual_chg)))

    # Per-symbol breakdown
    by_symbol: dict[str, dict] = defaultdict(lambda: {"total": 0, "correct": 0, "mag_errors": []})
    for r in rows:
        sym = r["symbol"]
        by_symbol[sym]["total"] += 1
        by_symbol[sym]["correct"] += r["direction_correct"]
        pred_mag = r.get("predicted_magnitude_pct") or 0.0
        actual_chg = r.get("actual_day_change_pct")
        if actual_chg is not None:
            by_symbol[sym]["mag_errors"].append(abs(pred_mag - abs(actual_chg)))

    # Per-recommendation breakdown
    by_rec: dict[str, dict] = defaultdict(lambda: {"total": 0, "correct": 0})
    for r in rows:
        rec = r.get("recommendation") or "N/A"
        by_rec[rec]["total"] += 1
        by_rec[rec]["correct"] += r["direction_correct"]

    return {
        "total_predictions":    total,
        "direction_correct":    correct_dir,
        "direction_accuracy":   correct_dir / total if total else 0,
        "mean_magnitude_error": statistics.mean(mag_errors) if mag_errors else None,
        "median_magnitude_error": statistics.median(mag_errors) if mag_errors else None,
        "by_symbol": {
            sym: {
                "total":    v["total"],
                "correct":  v["correct"],
                "accuracy": v["correct"] / v["total"] if v["total"] else 0,
                "mean_mag_error": (
                    statistics.mean(v["mag_errors"]) if v["mag_errors"] else None
                ),
            }
            for sym, v in by_symbol.items()
        },
        "by_recommendation": {
            rec: {
                "total":    v["total"],
                "correct":  v["correct"],
                "accuracy": v["correct"] / v["total"] if v["total"] else 0,
            }
            for rec, v in by_rec.items()
        },
    }


# ---------------------------------------------------------------------------
# Pretty-print
# ---------------------------------------------------------------------------

def _print_report(rows: list[dict], summary: dict, report_type: str) -> None:
    LINE = "=" * 68

    print(f"\n{LINE}")
    print(f"  BACKTEST REPORT  —  {report_type.replace('_', ' ').upper()}")
    if rows:
        dates = sorted({r["date"] for r in rows})
        print(f"  Period : {dates[0]}  →  {dates[-1]}  ({len(dates)} trading days)")
    print(LINE)

    # Overall
    total = summary["total_predictions"]
    correct = summary["direction_correct"]
    acc = summary["direction_accuracy"]
    bar = _bar(acc)
    print(f"\n  Direction accuracy : {correct}/{total}  {acc:.1%}  {bar}")

    mag_err = summary.get("mean_magnitude_error")
    if mag_err is not None:
        print(f"  Avg magnitude error: ±{mag_err:.2f}%  "
              f"(median ±{summary['median_magnitude_error']:.2f}%)")

    # Baseline reference
    print(f"\n  Reference  →  random guess = 50%  |  always-up = ~53% (S&P long-run)")

    # Per-symbol table
    print(f"\n  {'SYMBOL':<8}  {'CALLS':>5}  {'CORRECT':>7}  {'ACCURACY':>9}  "
          f"{'MAG ERR':>8}  GRADE")
    print(f"  {'-'*8}  {'-'*5}  {'-'*7}  {'-'*9}  {'-'*8}  {'-'*5}")
    for sym, v in sorted(summary["by_symbol"].items(),
                         key=lambda x: x[1]["accuracy"], reverse=True):
        mag = f"±{v['mean_mag_error']:.1f}%" if v["mean_mag_error"] is not None else "  N/A "
        grade = _grade(v["accuracy"], v["total"])
        print(f"  {sym:<8}  {v['total']:>5}  {v['correct']:>7}  "
              f"{v['accuracy']:>8.1%}  {mag:>8}  {grade}")

    # Per-recommendation table
    print(f"\n  {'RECO':<6}  {'CALLS':>5}  {'CORRECT':>7}  {'ACCURACY':>9}")
    print(f"  {'-'*6}  {'-'*5}  {'-'*7}  {'-'*9}")
    for rec, v in sorted(summary["by_recommendation"].items(),
                         key=lambda x: x[1]["total"], reverse=True):
        print(f"  {rec:<6}  {v['total']:>5}  {v['correct']:>7}  {v['accuracy']:>8.1%}")

    # Recent 10 predictions detail
    if rows:
        print(f"\n  {'DATE':<11} {'SYM':<6} {'PRED':>5} {'PRED%':>6} "
              f"{'ACTUAL%':>8} {'ACT':>5}  RESULT")
        print(f"  {'-'*11} {'-'*6} {'-'*5} {'-'*6} {'-'*8} {'-'*5}  {'-'*6}")
        for r in rows[:20]:
            pred_dir  = r["predicted_direction"][:4].upper()
            act_dir   = (r.get("actual_direction") or "?")[:4].upper()
            pred_mag  = r.get("predicted_magnitude_pct") or 0
            act_chg   = r.get("actual_day_change_pct")
            act_str   = f"{act_chg:+.2f}%" if act_chg is not None else "  N/A"
            ok        = "✅" if r["direction_correct"] else "❌"
            print(f"  {r['date']:<11} {r['symbol']:<6} {pred_dir:>5} "
                  f"{pred_mag:>+5.1f}% {act_str:>8}  {act_dir:>5}  {ok}")

    print(f"\n{LINE}\n")


def _bar(ratio: float, width: int = 20) -> str:
    filled = round(ratio * width)
    return "[" + "█" * filled + "░" * (width - filled) + "]"


def _grade(accuracy: float, n: int) -> str:
    if n < 5:
        return "—"     # too few samples
    if accuracy >= 0.65:
        return "⭐⭐⭐"
    if accuracy >= 0.55:
        return "⭐⭐"
    if accuracy >= 0.45:
        return "⭐"
    return "📉"
