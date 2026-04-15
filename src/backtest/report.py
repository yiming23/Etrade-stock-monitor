"""
Backtest report file generator.

Saves the backtest results to a Markdown file so you can open it anytime
without rerunning the analysis.

Files written:
  data/backtest_report.md           ← always overwritten with the latest run
  data/backtest_report_YYYY-MM-DD.md ← dated snapshot
"""

from __future__ import annotations

import statistics
from datetime import date, datetime
from pathlib import Path

from src.utils.config import PROJECT_ROOT
from src.utils.logger import get_logger

logger = get_logger(__name__)

REPORT_DIR = PROJECT_ROOT / "data"


def save_report(
    rows: list[dict],
    summary: dict,
    report_type: str = "pre_market",
) -> Path:
    """
    Write a Markdown backtest report and return the file path.
    Always writes data/backtest_report.md (latest) and a dated copy.
    """
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    content = _build_markdown(rows, summary, report_type)

    latest_path = REPORT_DIR / "backtest_report.md"
    dated_path  = REPORT_DIR / f"backtest_report_{date.today().isoformat()}.md"

    for path in (latest_path, dated_path):
        path.write_text(content, encoding="utf-8")

    logger.info(f"Backtest report saved → {latest_path}")
    return latest_path


def _build_markdown(rows: list[dict], summary: dict, report_type: str) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    dates = sorted({r["date"] for r in rows}) if rows else []
    period = f"{dates[0]} → {dates[-1]}" if dates else "no data"

    total    = summary.get("total_predictions", 0)
    correct  = summary.get("direction_correct", 0)
    acc      = summary.get("direction_accuracy", 0)
    mag_err  = summary.get("mean_magnitude_error")
    med_err  = summary.get("median_magnitude_error")

    lines: list[str] = []
    a = lines.append  # shorthand

    a(f"# Backtest Report — {report_type.replace('_', ' ').title()}")
    a(f"*Generated: {now}  |  Period: {period}  |  Trading days: {len(dates)}*\n")

    # ── Overall ──────────────────────────────────────────────────────────────
    a("## Overall Accuracy\n")
    a(f"| Metric | Value |")
    a(f"|--------|-------|")
    a(f"| Direction accuracy | **{acc:.1%}** ({correct}/{total}) |")
    if mag_err is not None:
        a(f"| Mean magnitude error | ±{mag_err:.2f}% |")
        a(f"| Median magnitude error | ±{med_err:.2f}% |")
    a(f"| Baseline (random) | 50.0% |")
    a(f"| Baseline (always-up) | ~53% |")
    a("")

    acc_bar = _md_bar(acc)
    a(f"**Direction:** `{acc_bar}` {acc:.1%}\n")

    # ── Per-symbol ────────────────────────────────────────────────────────────
    a("## Per-Symbol Breakdown\n")
    a("| Symbol | Calls | Correct | Accuracy | Mag Error | Grade |")
    a("|--------|------:|-------:|--------:|--------:|-------|")
    for sym, v in sorted(
        summary.get("by_symbol", {}).items(),
        key=lambda x: x[1]["accuracy"], reverse=True
    ):
        mag = f"±{v['mean_mag_error']:.1f}%" if v.get("mean_mag_error") is not None else "N/A"
        grade = _grade(v["accuracy"], v["total"])
        a(f"| **{sym}** | {v['total']} | {v['correct']} | {v['accuracy']:.1%} | {mag} | {grade} |")
    a("")

    # ── Per-recommendation ────────────────────────────────────────────────────
    a("## By Recommendation\n")
    a("| Recommendation | Calls | Correct | Accuracy |")
    a("|---------------|------:|-------:|--------:|")
    for rec, v in sorted(
        summary.get("by_recommendation", {}).items(),
        key=lambda x: x[1]["total"], reverse=True
    ):
        a(f"| {rec} | {v['total']} | {v['correct']} | {v['accuracy']:.1%} |")
    a("")

    # ── Recent detail ─────────────────────────────────────────────────────────
    a("## Recent Predictions (latest 30)\n")
    a("| Date | Symbol | Predicted | Pred% | Actual% | Actual Dir | Result |")
    a("|------|--------|-----------|------:|--------:|-----------|--------|")
    for r in (rows or [])[:30]:
        pred_dir = (r.get("predicted_direction") or "?").upper()
        act_dir  = (r.get("actual_direction")    or "?").upper()
        pred_mag = r.get("predicted_magnitude_pct") or 0
        act_chg  = r.get("actual_day_change_pct")
        act_str  = f"{act_chg:+.2f}%" if act_chg is not None else "N/A"
        ok       = "✅" if r.get("direction_correct") else "❌"
        a(f"| {r['date']} | {r['symbol']} | {pred_dir} | {pred_mag:+.1f}% "
          f"| {act_str} | {act_dir} | {ok} |")
    a("")

    # ── Insights ──────────────────────────────────────────────────────────────
    a("## Key Insights\n")

    if total >= 10:
        best  = max(summary.get("by_symbol", {}).items(),
                    key=lambda x: x[1]["accuracy"], default=None)
        worst = min(summary.get("by_symbol", {}).items(),
                    key=lambda x: x[1]["accuracy"], default=None)
        if best  and best[1]["total"]  >= 3:
            a(f"- 🏆 **Most accurate**: {best[0]} at {best[1]['accuracy']:.1%}")
        if worst and worst[1]["total"] >= 3:
            a(f"- 📉 **Least accurate**: {worst[0]} at {worst[1]['accuracy']:.1%}")

        if acc >= 0.60:
            a(f"- ✅ Overall direction accuracy ({acc:.1%}) beats random — signal detected.")
        elif acc >= 0.52:
            a(f"- ⚠️  Accuracy ({acc:.1%}) slightly above random — weak signal, watch for more data.")
        else:
            a(f"- ❌ Accuracy ({acc:.1%}) near/below random — LLM news signal not reliable yet.")
    else:
        a(f"- ℹ️  Only {total} predictions so far — need 20+ for statistically meaningful results.")

    a("")
    a("---")
    a(f"*Report generated by E\\*TRADE Stock Monitor backtest engine*")

    return "\n".join(lines)


def _md_bar(ratio: float, width: int = 20) -> str:
    filled = round(ratio * width)
    return "█" * filled + "░" * (width - filled)


def _grade(accuracy: float, n: int) -> str:
    if n < 5:
        return "—"
    if accuracy >= 0.65:
        return "⭐⭐⭐"
    if accuracy >= 0.55:
        return "⭐⭐"
    if accuracy >= 0.45:
        return "⭐"
    return "📉"
