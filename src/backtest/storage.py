"""
Prediction tracking database (SQLite).

Two tables:
  predictions — LLM pre-market direction + magnitude calls, one row per
                (date, symbol, report_type).
  actuals     — EOD open/close prices fetched automatically from yfinance,
                one row per (date, symbol).

Usage:
  from src.tracking.db import PredictionDB
  db = PredictionDB()
  db.save_predictions(date, stock_calls, report_type)
  db.save_actuals(date, symbols)          # fetches via yfinance
  rows = db.get_backtest_rows()           # joined predictions + actuals
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Generator

from src.utils.config import PROJECT_ROOT
from src.utils.logger import get_logger

logger = get_logger(__name__)

DB_PATH = PROJECT_ROOT / "data" / "predictions.db"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_magnitude(estimated_move: str) -> tuple[str, float]:
    """
    Parse the LLM's free-text 'estimated_move' string into:
      (direction, midpoint_pct)

    Examples:
      "+1.5% to +3%"   → ("up",   2.25)
      "-2% to -4%"     → ("down", 3.0)
      "flat to -0.5%"  → ("down", 0.25)
      "+0.5%"          → ("up",   0.5)
      "N/A"            → ("flat", 0.0)
    """
    if not estimated_move or estimated_move.strip().upper() in ("N/A", "FLAT", ""):
        return "flat", 0.0

    # Extract all numeric values (with optional sign)
    nums = re.findall(r"[+-]?\d+\.?\d*", estimated_move)
    if not nums:
        return "flat", 0.0

    values = [float(n) for n in nums]
    midpoint = sum(values) / len(values)

    if midpoint > 0.1:
        direction = "up"
    elif midpoint < -0.1:
        direction = "down"
    else:
        direction = "flat"

    return direction, abs(midpoint)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

class PredictionDB:
    """Thin wrapper around SQLite for prediction tracking."""

    def __init__(self, db_path: Path = DB_PATH) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init_schema()

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS predictions (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    date            TEXT NOT NULL,
                    symbol          TEXT NOT NULL,
                    report_type     TEXT NOT NULL,
                    direction       TEXT NOT NULL,
                    magnitude_pct   REAL,
                    recommendation  TEXT,
                    estimated_move  TEXT,
                    created_at      TEXT DEFAULT (datetime('now')),
                    UNIQUE(date, symbol, report_type)
                );

                CREATE TABLE IF NOT EXISTS actuals (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    date            TEXT NOT NULL,
                    symbol          TEXT NOT NULL,
                    open_price      REAL,
                    close_price     REAL,
                    prev_close      REAL,
                    day_change_pct  REAL,
                    fetched_at      TEXT DEFAULT (datetime('now')),
                    UNIQUE(date, symbol)
                );

                CREATE INDEX IF NOT EXISTS idx_pred_date  ON predictions(date);
                CREATE INDEX IF NOT EXISTS idx_pred_sym   ON predictions(symbol);
                CREATE INDEX IF NOT EXISTS idx_act_date   ON actuals(date);
            """)

    # -----------------------------------------------------------------------
    # Write
    # -----------------------------------------------------------------------

    def save_predictions(
        self,
        trade_date: date,
        stock_calls: list,          # list[StockCall] from analyzer
        report_type: str = "pre_market",
    ) -> int:
        """
        Persist LLM stock calls for trade_date.
        Returns number of rows inserted/replaced.
        """
        date_str = trade_date.isoformat()
        rows = 0
        with self._conn() as conn:
            for call in stock_calls:
                # Use structured fields if LLM provided them, else parse string
                direction = getattr(call, "predicted_direction", None)
                magnitude = getattr(call, "predicted_magnitude_pct", None)
                if not direction:
                    direction, magnitude = _parse_magnitude(
                        getattr(call, "estimated_move", "")
                    )
                if magnitude is None:
                    _, magnitude = _parse_magnitude(
                        getattr(call, "estimated_move", "")
                    )

                conn.execute(
                    """
                    INSERT INTO predictions
                        (date, symbol, report_type, direction, magnitude_pct,
                         recommendation, estimated_move)
                    VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(date, symbol, report_type) DO UPDATE SET
                        direction       = excluded.direction,
                        magnitude_pct   = excluded.magnitude_pct,
                        recommendation  = excluded.recommendation,
                        estimated_move  = excluded.estimated_move,
                        created_at      = datetime('now')
                    """,
                    (
                        date_str,
                        call.symbol,
                        report_type,
                        direction,
                        magnitude,
                        getattr(call, "recommendation", None),
                        getattr(call, "estimated_move", None),
                    ),
                )
                rows += 1
        logger.info(f"Saved {rows} {report_type} predictions for {date_str}")
        return rows

    def save_actuals(self, trade_date: date, symbols: list[str]) -> int:
        """
        Fetch EOD OHLC from yfinance and store in actuals table.
        Safe to call multiple times — uses INSERT OR REPLACE.
        Returns number of symbols stored.
        """
        import yfinance as yf

        date_str = trade_date.isoformat()
        # Fetch a 5-day window so we reliably get the target date even if
        # the market was closed on adjacent days.
        start = (trade_date - timedelta(days=5)).isoformat()
        end   = (trade_date + timedelta(days=1)).isoformat()

        stored = 0
        with self._conn() as conn:
            for sym in symbols:
                try:
                    hist = yf.Ticker(sym).history(start=start, end=end)
                    if hist.empty:
                        logger.warning(f"yfinance: no data for {sym} on {date_str}")
                        continue

                    # Filter to the target date
                    hist.index = hist.index.tz_localize(None)
                    target_rows = hist[hist.index.date == trade_date]
                    if target_rows.empty:
                        # Market closed on that day (holiday/weekend)
                        continue

                    row = target_rows.iloc[0]
                    open_p  = float(row["Open"])
                    close_p = float(row["Close"])

                    # Previous close = day before target in the history
                    idx = hist.index.get_loc(target_rows.index[0])
                    prev_close = float(hist.iloc[idx - 1]["Close"]) if idx > 0 else None
                    day_chg = (
                        (close_p - prev_close) / prev_close * 100
                        if prev_close else None
                    )

                    conn.execute(
                        """
                        INSERT INTO actuals
                            (date, symbol, open_price, close_price, prev_close, day_change_pct)
                        VALUES (?,?,?,?,?,?)
                        ON CONFLICT(date, symbol) DO UPDATE SET
                            open_price      = excluded.open_price,
                            close_price     = excluded.close_price,
                            prev_close      = excluded.prev_close,
                            day_change_pct  = excluded.day_change_pct,
                            fetched_at      = datetime('now')
                        """,
                        (date_str, sym, open_p, close_p, prev_close, day_chg),
                    )
                    stored += 1
                    logger.debug(
                        f"{sym} {date_str}: open={open_p:.2f} close={close_p:.2f} "
                        f"chg={day_chg:+.2f}%" if day_chg is not None else
                        f"{sym} {date_str}: open={open_p:.2f} close={close_p:.2f}"
                    )
                except Exception as e:
                    logger.warning(f"Failed to fetch actuals for {sym}: {e}")

        logger.info(f"Saved actuals for {stored}/{len(symbols)} symbols on {date_str}")
        return stored

    # -----------------------------------------------------------------------
    # Read
    # -----------------------------------------------------------------------

    def get_backtest_rows(
        self,
        report_type: str = "pre_market",
        symbol: str | None = None,
        since: date | None = None,
    ) -> list[dict]:
        """
        Return joined predictions + actuals rows where both exist.
        Each row is a plain dict.
        """
        sql = """
            SELECT
                p.date,
                p.symbol,
                p.report_type,
                p.direction          AS predicted_direction,
                p.magnitude_pct      AS predicted_magnitude_pct,
                p.recommendation,
                p.estimated_move,
                a.open_price,
                a.close_price,
                a.prev_close,
                a.day_change_pct     AS actual_day_change_pct,
                CASE
                    WHEN a.day_change_pct > 0.1  THEN 'up'
                    WHEN a.day_change_pct < -0.1 THEN 'down'
                    ELSE 'flat'
                END AS actual_direction,
                CASE
                    WHEN (p.direction = 'up'   AND a.day_change_pct >  0.1) THEN 1
                    WHEN (p.direction = 'down' AND a.day_change_pct < -0.1) THEN 1
                    WHEN (p.direction = 'flat' AND abs(a.day_change_pct) <= 0.1) THEN 1
                    ELSE 0
                END AS direction_correct
            FROM predictions p
            JOIN actuals a ON p.date = a.date AND p.symbol = a.symbol
            WHERE p.report_type = ?
        """
        params: list = [report_type]

        if symbol:
            sql += " AND p.symbol = ?"
            params.append(symbol)
        if since:
            sql += " AND p.date >= ?"
            params.append(since.isoformat())

        sql += " ORDER BY p.date DESC, p.symbol"

        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_prediction_dates(self, report_type: str = "pre_market") -> list[str]:
        """Dates that have predictions but no actuals yet."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT p.date
                FROM predictions p
                LEFT JOIN actuals a ON p.date = a.date AND p.symbol = a.symbol
                WHERE p.report_type = ? AND a.date IS NULL
                ORDER BY p.date
                """,
                (report_type,),
            ).fetchall()
        return [r["date"] for r in rows]

    def get_symbols_for_date(self, trade_date: date) -> list[str]:
        """Return distinct symbols that have predictions for a given date."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM predictions WHERE date = ?",
                (trade_date.isoformat(),),
            ).fetchall()
        return [r["symbol"] for r in rows]

    def get_accuracy_summary(
        self,
        symbols: list[str],
        report_type: str = "pre_market",
        min_samples: int = 3,
    ) -> dict[str, dict]:
        """
        Return per-symbol accuracy for use in the email report.

        Returns:
          { "AAPL": {"correct": 7, "total": 12, "accuracy": 0.583}, ... }
          Only symbols with >= min_samples matched rows are included.
        """
        if not symbols:
            return {}

        placeholders = ",".join("?" * len(symbols))
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    p.symbol,
                    COUNT(*) AS total,
                    SUM(
                        CASE
                            WHEN (p.direction = 'up'   AND a.day_change_pct >  0.1) THEN 1
                            WHEN (p.direction = 'down' AND a.day_change_pct < -0.1) THEN 1
                            WHEN (p.direction = 'flat' AND abs(a.day_change_pct) <= 0.1) THEN 1
                            ELSE 0
                        END
                    ) AS correct
                FROM predictions p
                JOIN actuals a ON p.date = a.date AND p.symbol = a.symbol
                WHERE p.report_type = ? AND p.symbol IN ({placeholders})
                GROUP BY p.symbol
                """,
                [report_type, *symbols],
            ).fetchall()

        result = {}
        for r in rows:
            if r["total"] >= min_samples:
                result[r["symbol"]] = {
                    "correct":  r["correct"],
                    "total":    r["total"],
                    "accuracy": r["correct"] / r["total"],
                }
        return result
