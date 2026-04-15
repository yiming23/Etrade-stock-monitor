"""
E*TRADE Stock Monitor — Main Orchestrator

Schedule (Mon–Fri, ET):
  08:30  Pre-market   — overnight news, opening trade plan
  12:00  Mid-day      — morning recap, updated position calls
  16:30  Post-market  — day summary, upcoming events & earnings outlook

Portfolio auth:
  - Full E*TRADE re-auth only when cache is older than PORTFOLIO_CACHE_DAYS (default 7)
  - Daily token renewal is silent (no PIN needed) while within the same calendar day
  - Telegram bot delivers auth PIN to your phone when re-auth is required

CLI flags:
  --once                 Run one report immediately and exit
  --type pre_market|mid_market|post_market
  --refresh-portfolio    Force re-auth with E*TRADE and update the portfolio cache
  --schedule             Run the scheduler (default when no flags given)
"""

import argparse
import logging
import signal
import sys
import threading
from datetime import datetime, timedelta
try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    from backports.zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.events import (
    EVENT_JOB_EXECUTED, EVENT_JOB_ERROR, EVENT_JOB_MISSED,
)

from src.etrade.auth import ETradeAuth
from src.etrade.portfolio import (
    PortfolioReader,
    PortfolioSummary,
    fetch_earnings_calendar,
    load_portfolio_from_cache,
)
from src.news.scraper import NewsScraper
from src.analysis.analyzer import StockAnalyzer
from src.email.sender import EmailSender
from src.utils.config import get_settings
from src.utils.logger import get_logger
from src.utils.telegram_bot import make_notifier
from src.backtest.storage import PredictionDB
from src.backtest.engine import run_backtest
from src.backtest.report import save_report as save_backtest_report

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Bridge Python stdlib logging → loguru so APScheduler errors appear in logs
# ---------------------------------------------------------------------------
class _LoguruHandler(logging.Handler):
    """Redirect stdlib logging records into loguru."""
    _level_map = {
        logging.DEBUG:    "DEBUG",
        logging.INFO:     "INFO",
        logging.WARNING:  "WARNING",
        logging.ERROR:    "ERROR",
        logging.CRITICAL: "CRITICAL",
    }
    def emit(self, record: logging.LogRecord) -> None:
        lvl = self._level_map.get(record.levelno, "DEBUG")
        from loguru import logger as _loguru
        _loguru.opt(depth=6, exception=record.exc_info).log(
            lvl, record.getMessage()
        )

_handler = _LoguruHandler()
logging.getLogger("apscheduler").addHandler(_handler)
logging.getLogger("apscheduler").setLevel(logging.DEBUG)


class StockMonitor:
    """Main orchestrator."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._validate_settings()
        self.notifier = make_notifier(self.settings)
        self.news_scraper = NewsScraper(self.settings)
        self.analyzer = StockAnalyzer(self.settings)
        self.email_sender = EmailSender(self.settings)
        self._auth: ETradeAuth | None = None

    # -------------------------------------------------------------------------
    # Validation
    # -------------------------------------------------------------------------

    def _validate_settings(self) -> None:
        errors = []
        if not self.settings.etrade_consumer_key:
            errors.append("ETRADE_CONSUMER_KEY not set")
        if not self.settings.etrade_consumer_secret:
            errors.append("ETRADE_CONSUMER_SECRET not set")

        backend = self.settings.llm_backend.lower()
        if backend == "gemini" and not self.settings.gemini_api_key:
            errors.append("GEMINI_API_KEY not set (required for LLM_BACKEND=gemini)")
        elif backend == "claude" and not self.settings.anthropic_api_key:
            errors.append("ANTHROPIC_API_KEY not set (required for LLM_BACKEND=claude)")

        if not self.settings.gmail_address:
            errors.append("GMAIL_ADDRESS not set")
        if not self.settings.recipient_email:
            errors.append("RECIPIENT_EMAIL not set")
        if (self.settings.email_backend.lower() == "smtp"
                and not self.settings.gmail_app_password):
            errors.append("GMAIL_APP_PASSWORD not set (required for EMAIL_BACKEND=smtp)")

        for e in errors:
            logger.warning(f"⚠️  Missing config: {e}")

    # -------------------------------------------------------------------------
    # Portfolio: cache-first, E*TRADE on expiry
    # -------------------------------------------------------------------------

    def _get_portfolio(self, force_refresh: bool = False) -> PortfolioSummary | None:
        """
        Return portfolio — from cache + live yfinance prices if fresh,
        or from E*TRADE if cache is expired or force_refresh=True.
        """
        if not force_refresh and self.settings.portfolio_cache_days > 0:
            cached = load_portfolio_from_cache(self.settings)
            if cached:
                logger.info(
                    f"Using cached portfolio ({len(cached.positions)} positions, "
                    f"prices live via yfinance)."
                )
                return cached

        # Need E*TRADE auth
        logger.info("Portfolio cache expired or force-refresh — authenticating with E*TRADE...")
        try:
            if not self._auth:
                self._auth = ETradeAuth(self.settings, notifier=self.notifier)
            token, secret = self._auth.authenticate()
            reader = PortfolioReader(self.settings, token, secret)
            portfolio = reader.get_portfolio()
            return portfolio
        except Exception as e:
            logger.error(f"E*TRADE auth/portfolio fetch failed: {e}")
            if self.notifier:
                self.notifier.send_error("E*TRADE auth failed", str(e))
            # Last resort: try stale cache rather than sending nothing
            stale = load_portfolio_from_cache(self.settings)
            if stale:
                logger.warning("Using stale portfolio cache as fallback.")
                return stale
            return None

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _last_trading_day(from_date) -> "date":
        """Return the most recent weekday before from_date (skips weekends)."""
        from datetime import date as _date, timedelta
        d = from_date - timedelta(days=1)
        while d.weekday() >= 5:   # 5=Saturday, 6=Sunday
            d -= timedelta(days=1)
        return d

    # -------------------------------------------------------------------------
    # Report runner
    # -------------------------------------------------------------------------

    def run_report(
        self,
        report_type: str = "pre_market",
        force_portfolio_refresh: bool = False,
        dry_run: bool = False,
    ) -> None:
        """
        Run a full report pipeline.

        dry_run=True: runs every step and sends the email, but does NOT write
        predictions or actuals to the backtest database.  Use this when
        testing so you don't pollute historical accuracy data.
        """
        tz = ZoneInfo(self.settings.timezone)
        now = datetime.now(tz)
        label_map = {
            "pre_market": "Pre-Market",
            "mid_market": "Mid-Day",
            "post_market": "Post-Market",
        }
        label = label_map.get(report_type, report_type)
        dry_tag = "  [DRY RUN — DB not written]" if dry_run else ""

        logger.info("=" * 60)
        logger.info(f"Running {label} report — {now.strftime('%Y-%m-%d %H:%M %Z')}{dry_tag}")
        logger.info("=" * 60)

        # Step 1: Portfolio
        logger.info("[1/6] Loading portfolio...")
        portfolio = self._get_portfolio(force_refresh=force_portfolio_refresh)
        if not portfolio or not portfolio.positions:
            msg = "No portfolio positions available. Skipping report."
            logger.warning(msg)
            if self.notifier:
                self.notifier.send_error(f"{label} report skipped", msg)
            return

        unique_symbols = list(dict.fromkeys(portfolio.symbols))
        logger.info(
            f"  {len(portfolio.positions)} positions "
            f"({'cached+yfinance' if portfolio.from_cache else 'live E*TRADE'}) "
            f"— {', '.join(unique_symbols)}"
        )

        # Step 2: Fetch news
        logger.info("[2/6] Fetching news...")
        news_by_symbol = self.news_scraper.fetch_all_news(unique_symbols)
        total_news = sum(len(v) for v in news_by_symbol.values())
        logger.info(f"  {total_news} articles fetched")

        # Step 3: Rank news
        top_n = self.settings.top_news_count
        logger.info(f"[3/6] Selecting top {top_n} cross-portfolio articles...")
        top_articles = self.news_scraper.select_top_portfolio_news(
            news_by_symbol, portfolio.positions, top_n=top_n
        )

        # Step 4: Fetch earnings calendar for post-market
        earnings_data = {}
        if report_type == "post_market":
            logger.info("[4/6] Fetching earnings calendar...")
            earnings_data = fetch_earnings_calendar(unique_symbols)
            if earnings_data:
                upcoming = ", ".join(
                    f"{s} ({d['earnings_date']})"
                    for s, d in earnings_data.items()
                )
                logger.info(f"  Upcoming earnings: {upcoming}")
        else:
            logger.info("[4/6] Skipping earnings calendar (pre/mid-market)")

        # Step 5: AI analysis
        logger.info(f"[5/6] Running {label} AI analysis...")
        analysis = self.analyzer.analyze_top_news(
            top_articles,
            portfolio.positions,
            report_type=report_type,
            earnings_data=earnings_data,
        )

        # Step 6: Send email — include per-stock accuracy summary from backtest DB
        logger.info("[6/6] Sending email...")
        accuracy_map = {}
        if not dry_run:
            try:
                db = PredictionDB()
                accuracy_map = db.get_accuracy_summary(unique_symbols)
            except Exception as e:
                logger.debug(f"Could not load accuracy summary: {e}")

        success = self.email_sender.send_report(
            portfolio, analysis, report_type,
            accuracy_map=accuracy_map,
        )

        if success:
            logger.info(f"✅ {label} report sent.{dry_tag}")
            if self.notifier:
                self.notifier.send_info(
                    f"📧 {label} report sent at {now.strftime('%H:%M ET')}{dry_tag}"
                )
        else:
            logger.error(f"❌ Failed to send {label} report.")
            if self.notifier:
                self.notifier.send_error(f"{label} email failed", "Check logs.")

        # ── Backtest tracking (skipped in dry-run) ─────────────────────────
        if dry_run:
            logger.info("DRY RUN: skipping backtest DB writes.")
            return

        db = PredictionDB()
        trade_date = now.date()

        # Save LLM predictions for today (all report types, pre-market is primary)
        if analysis.stock_calls:
            db.save_predictions(trade_date, analysis.stock_calls, report_type)

        # Pre-market: fetch YESTERDAY's actuals (previous trading day).
        # This is done at pre-market because it's the most reliably-run job,
        # and yesterday's market has definitely closed by 8:30 AM.
        if report_type == "pre_market" and unique_symbols:
            prev_date = self._last_trading_day(trade_date)
            logger.info(f"Fetching actuals for previous trading day ({prev_date})...")
            db.save_actuals(prev_date, unique_symbols)

    # -------------------------------------------------------------------------
    # Scheduler
    # -------------------------------------------------------------------------

    # -------------------------------------------------------------------------
    # Telegram command handler
    # -------------------------------------------------------------------------

    def _handle_telegram_command(self, command: str) -> None:
        """Called on a background thread for each /command received via Telegram."""
        cmd = command.lower().split()[0]

        if cmd == "/auth":
            self.notifier.send_message(
                "🔄 <b>Starting E*TRADE re-authorization...</b>\n"
                "The new token will be cached until next Sunday."
            )
            self.refresh_portfolio_cache()

        elif cmd == "/status":
            from src.etrade.portfolio import load_portfolio_from_cache, PORTFOLIO_CACHE_FILE
            import json
            if PORTFOLIO_CACHE_FILE.exists():
                try:
                    data = json.loads(PORTFOLIO_CACHE_FILE.read_text())
                    cached_at = data.get("cached_at", "unknown")[:19].replace("T", " ")
                    positions = data.get("positions", [])
                    symbols = ", ".join(p["symbol"] for p in positions)
                    self.notifier.send_message(
                        f"📊 <b>Portfolio Cache Status</b>\n\n"
                        f"Last updated: {cached_at} UTC\n"
                        f"Positions ({len(positions)}): {symbols}\n\n"
                        f"Send /auth to force a refresh."
                    )
                except Exception as e:
                    self.notifier.send_error("Could not read portfolio cache", str(e))
            else:
                self.notifier.send_message(
                    "⚠️ No portfolio cache found.\nSend /auth to authenticate with E*TRADE."
                )

        elif cmd == "/help":
            self.notifier.send_message(
                "📋 <b>Available commands</b>\n\n"
                "/auth — Re-authorize E*TRADE immediately\n"
                "          (use if Sunday auth timed out)\n\n"
                "/status — Show portfolio cache info\n"
                "          (last refresh time + holdings)\n\n"
                "/help — Show this message"
            )

        else:
            self.notifier.send_message(
                f"Unknown command: <code>{command}</code>\n"
                "Send /help for available commands."
            )

    def refresh_portfolio_cache(self) -> None:
        """
        Weekly job: re-auth with E*TRADE, update the portfolio cache.
        Does NOT send an email — purely a background cache refresh.
        Runs Sunday noon so weekday reports always have a fresh cache.
        """
        logger.info("=" * 60)
        logger.info("Weekly portfolio cache refresh (E*TRADE re-auth)")
        logger.info("=" * 60)
        if self.notifier:
            self.notifier.send_message(
                "🔄 <b>Weekly portfolio refresh</b>\n"
                "Connecting to E*TRADE to update your holdings cache..."
            )
        try:
            if not self._auth:
                self._auth = ETradeAuth(self.settings, notifier=self.notifier)
            token, secret = self._auth.authenticate()
            reader = PortfolioReader(self.settings, token, secret)
            portfolio = reader.get_portfolio()
            logger.info(
                f"✅ Cache refreshed: {len(portfolio.positions)} positions, "
                f"${portfolio.total_market_value:,.2f}"
            )
            if self.notifier:
                symbols = ", ".join(dict.fromkeys(portfolio.symbols))
                self.notifier.send_info(
                    f"✅ Portfolio cache updated\n"
                    f"{len(portfolio.positions)} positions: {symbols}"
                )
        except Exception as e:
            logger.error(f"Weekly portfolio refresh failed: {e}")
            if self.notifier:
                self.notifier.send_error("Weekly portfolio refresh failed", str(e))

    def _run_catchup_if_missed(self) -> None:
        """
        On startup, check if the most recent scheduled report was missed
        (e.g. Mac was asleep at 8:30 AM). If so, run it now — but only if
        we're still within a 2-hour catch-up window after the scheduled time.
        """
        tz = ZoneInfo(self.settings.timezone)
        s = self.settings
        now = datetime.now(tz)

        # Only attempt on weekdays
        if now.weekday() >= 5:  # Sat=5, Sun=6
            return

        schedule_slots = [
            (s.pre_market_hour,  s.pre_market_minute,  "pre_market"),
            (s.mid_market_hour,  s.mid_market_minute,  "mid_market"),
            (s.post_market_hour, s.post_market_minute, "post_market"),
        ]

        CATCHUP_WINDOW = timedelta(hours=2)

        for hour, minute, rtype in schedule_slots:
            scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            delta = now - scheduled
            if timedelta(0) < delta < CATCHUP_WINDOW:
                logger.warning(
                    f"⏰ Catch-up: {rtype} was scheduled {int(delta.total_seconds() / 60)}m ago "
                    f"(Mac likely slept). Running now..."
                )
                if self.notifier:
                    self.notifier.send_info(
                        f"⏰ Catch-up: {rtype.replace('_', '-')} report missed "
                        f"(+{int(delta.total_seconds() / 60)}m). Sending now..."
                    )
                try:
                    self.run_report(rtype)
                except Exception as e:
                    logger.error(f"Catch-up report failed: {e}")
                break  # only catch up the most recent missed slot

    def start_scheduler(self) -> None:
        tz = ZoneInfo(self.settings.timezone)
        scheduler = BlockingScheduler(timezone=tz)
        s = self.settings

        # ── APScheduler event listener (logs missed/error/executed via loguru) ──
        def _on_job_event(event):
            if event.exception:
                logger.error(
                    f"❌ Job '{event.job_id}' raised an exception: {event.exception}"
                )
            elif hasattr(event, "scheduled_run_time"):
                # missed event
                logger.warning(
                    f"⚠️  Job '{event.job_id}' MISSED — scheduled for "
                    f"{event.scheduled_run_time.strftime('%Y-%m-%d %H:%M %Z')}. "
                    f"Mac was likely asleep past the grace window."
                )
            else:
                logger.debug(f"✅ Job '{event.job_id}' executed successfully.")

        scheduler.add_listener(
            _on_job_event,
            EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED,
        )

        # ── Weekday report jobs ──────────────────────────────────────────────
        # misfire_grace_time=7200 (2 hours): if Mac was asleep at 8:30 but wakes
        # by 10:30, the report still fires.  Coalesce=True means only ONE run
        # even if multiple slots were missed while asleep.
        report_jobs = [
            (s.pre_market_hour,  s.pre_market_minute,  "pre_market",  "Pre-Market"),
            (s.mid_market_hour,  s.mid_market_minute,  "mid_market",  "Mid-Day"),
            (s.post_market_hour, s.post_market_minute, "post_market", "Post-Market"),
        ]
        for hour, minute, rtype, name in report_jobs:
            scheduler.add_job(
                self.run_report,
                CronTrigger(
                    hour=hour, minute=minute,
                    day_of_week="mon-fri", timezone=tz,
                ),
                args=[rtype],
                id=f"{rtype}_report",
                name=f"{name} Report",
                misfire_grace_time=7200,   # 2-hour grace (was 600s / 10 min)
                coalesce=True,
            )

        # ── Weekly portfolio refresh (Sunday noon by default) ────────────────
        scheduler.add_job(
            self.refresh_portfolio_cache,
            CronTrigger(
                hour=s.portfolio_refresh_hour,
                minute=s.portfolio_refresh_minute,
                day_of_week=s.portfolio_refresh_day,
                timezone=tz,
            ),
            id="weekly_portfolio_refresh",
            name="Weekly Portfolio Refresh",
            misfire_grace_time=14400,   # 4-hour grace for weekly refresh
            coalesce=True,
        )

        def shutdown(signum, frame):
            logger.info("Shutting down scheduler...")
            scheduler.shutdown(wait=False)
            sys.exit(0)

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        logger.info(f"📅 Scheduler running ({s.timezone})")
        logger.info(f"   Pre-Market:  {s.pre_market_hour:02d}:{s.pre_market_minute:02d} Mon-Fri")
        logger.info(f"   Mid-Day:     {s.mid_market_hour:02d}:{s.mid_market_minute:02d} Mon-Fri")
        logger.info(f"   Post-Market: {s.post_market_hour:02d}:{s.post_market_minute:02d} Mon-Fri")
        logger.info(
            f"   Portfolio refresh: {s.portfolio_refresh_day.capitalize()} "
            f"{s.portfolio_refresh_hour:02d}:{s.portfolio_refresh_minute:02d} ET"
        )
        logger.info(
            f"   Auth via: {'Telegram' if s.telegram_enabled else 'terminal'}"
        )
        logger.info(f"   Misfire grace: 2h (reports), 4h (portfolio refresh)")
        logger.info("Press Ctrl+C to stop.\n")

        # Start Telegram command listener (runs on a daemon thread)
        if self.notifier:
            self.notifier.start_command_listener(self._handle_telegram_command)

        # Run catch-up for any report missed while Mac was asleep
        self._run_catchup_if_missed()

        if self.notifier:
            self.notifier.send_info(
                f"🚀 Stock Monitor started\n"
                f"Pre: {s.pre_market_hour:02d}:{s.pre_market_minute:02d} | "
                f"Mid: {s.mid_market_hour:02d}:{s.mid_market_minute:02d} | "
                f"Post: {s.post_market_hour:02d}:{s.post_market_minute:02d} ET\n"
                f"Portfolio refresh: {s.portfolio_refresh_day.capitalize()} "
                f"{s.portfolio_refresh_hour:02d}:{s.portfolio_refresh_minute:02d}"
            )

        scheduler.start()


# =============================================================================
# Entry point
# =============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="E*TRADE Stock Monitor — AI-powered portfolio digest"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one report immediately and exit (default: pre_market)",
    )
    parser.add_argument(
        "--type",
        choices=["pre_market", "mid_market", "post_market"],
        default="pre_market",
        help="Report type for --once (default: pre_market)",
    )
    parser.add_argument(
        "--refresh-portfolio",
        action="store_true",
        help="Force re-auth with E*TRADE and refresh the portfolio cache, then run report",
    )
    parser.add_argument(
        "--schedule",
        action="store_true",
        help="Start the scheduler (default when no other flags given)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run full pipeline and send email but do NOT write to the backtest DB",
    )
    parser.add_argument(
        "--backtest",
        action="store_true",
        help="Run backtest report comparing LLM predictions vs actual prices",
    )
    parser.add_argument(
        "--backtest-symbol",
        metavar="TICKER",
        default=None,
        help="Filter backtest to a single symbol (e.g. --backtest-symbol AAPL)",
    )
    parser.add_argument(
        "--backtest-since",
        metavar="YYYY-MM-DD",
        default=None,
        help="Only include predictions since this date",
    )
    parser.add_argument(
        "--fetch-actuals",
        metavar="YYYY-MM-DD",
        default=None,
        help="Manually fetch EOD actuals for a given date (e.g. --fetch-actuals 2026-04-11)",
    )
    args = parser.parse_args()

    monitor = StockMonitor()

    if args.backtest:
        from datetime import date as _date
        since = _date.fromisoformat(args.backtest_since) if args.backtest_since else None
        db = PredictionDB()
        rows = db.get_backtest_rows(
            report_type=args.type,
            symbol=args.backtest_symbol,
            since=since,
        )
        from src.backtest.engine import _compute_summary
        summary = _compute_summary(rows) if rows else {}
        # Print to terminal
        run_backtest(report_type=args.type, symbol=args.backtest_symbol, since=since)
        # Also save to file
        if rows:
            path = save_backtest_report(rows, summary, args.type)
            print(f"\n📄 Report saved → {path}\n")
    elif args.fetch_actuals:
        from datetime import date as _date
        target = _date.fromisoformat(args.fetch_actuals)
        db = PredictionDB()
        symbols = db.get_symbols_for_date(target)
        if not symbols:
            print(f"No predictions found for {args.fetch_actuals}. Run a report first.")
        else:
            print(f"Fetching actuals for {len(symbols)} symbols on {args.fetch_actuals}...")
            db.save_actuals(target, symbols)
            print("Done.")
    elif args.refresh_portfolio:
        report_type = args.type if args.once else "pre_market"
        logger.info("Force-refreshing portfolio from E*TRADE...")
        monitor.run_report(report_type, force_portfolio_refresh=True)
    elif args.once:
        # Start Telegram listener even in --once mode so commands work during testing
        if monitor.notifier:
            monitor.notifier.start_command_listener(monitor._handle_telegram_command)
        monitor.run_report(args.type, dry_run=args.dry_run)
    else:
        monitor.start_scheduler()


if __name__ == "__main__":
    main()
