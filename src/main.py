"""
E*TRADE Stock Monitor - Main Orchestrator

Schedules pre-market and post-market reports.
Can also be run once for testing with --once flag.
"""

import argparse
import sys
import signal
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from src.etrade.auth import ETradeAuth
from src.etrade.portfolio import PortfolioReader
from src.news.scraper import NewsScraper
from src.analysis.analyzer import StockAnalyzer
from src.email.sender import EmailSender
from src.utils.config import get_settings
from src.utils.logger import get_logger

logger = get_logger(__name__)


class StockMonitor:
    """Main orchestrator that ties all modules together."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._validate_settings()
        self.news_scraper = NewsScraper(self.settings)
        self.analyzer = StockAnalyzer(self.settings)
        self.email_sender = EmailSender(self.settings)
        self._auth: ETradeAuth | None = None
        self._portfolio_reader: PortfolioReader | None = None

    def _validate_settings(self) -> None:
        """Validate that required settings are present."""
        errors = []
        if not self.settings.etrade_consumer_key:
            errors.append("ETRADE_CONSUMER_KEY not set")
        if not self.settings.etrade_consumer_secret:
            errors.append("ETRADE_CONSUMER_SECRET not set")

        # LLM backend validation
        backend = self.settings.llm_backend.lower()
        if backend == "gemini" and not self.settings.gemini_api_key:
            errors.append("GEMINI_API_KEY not set (required for LLM_BACKEND=gemini)")
        elif backend == "claude" and not self.settings.anthropic_api_key:
            errors.append("ANTHROPIC_API_KEY not set (required for LLM_BACKEND=claude)")

        # Email backend validation
        if not self.settings.gmail_address:
            errors.append("GMAIL_ADDRESS not set")
        if not self.settings.recipient_email:
            errors.append("RECIPIENT_EMAIL not set")
        if self.settings.email_backend.lower() == "smtp" and not self.settings.gmail_app_password:
            errors.append("GMAIL_APP_PASSWORD not set (required for EMAIL_BACKEND=smtp)")

        if errors:
            for e in errors:
                logger.warning(f"⚠️  Missing config: {e}")
            logger.warning(
                "Some features may not work. Edit .env and fill in the missing values."
            )

    def _ensure_authenticated(self) -> None:
        """Ensure E*TRADE authentication is active."""
        if not self._auth:
            self._auth = ETradeAuth(self.settings)

        token, secret = self._auth.authenticate()
        self._portfolio_reader = PortfolioReader(self.settings, token, secret)

    def run_report(self, report_type: str = "pre_market") -> None:
        """
        Execute a single report cycle:
          1. Authenticate with E*TRADE
          2. Fetch portfolio positions
          3. Scrape news for each holding
          4. Analyze news with Claude AI
          5. Send email report
        """
        tz = ZoneInfo(self.settings.timezone)
        now = datetime.now(tz)
        logger.info(f"{'='*60}")
        logger.info(f"Running {report_type} report at {now.strftime('%Y-%m-%d %H:%M %Z')}")
        logger.info(f"{'='*60}")

        # Step 1: Authenticate
        logger.info("[1/5] Authenticating with E*TRADE...")
        try:
            self._ensure_authenticated()
        except Exception as e:
            logger.error(f"Authentication failed: {e}")
            return

        # Step 2: Fetch portfolio
        logger.info("[2/5] Fetching portfolio positions...")
        try:
            portfolio = self._portfolio_reader.get_portfolio()
        except Exception as e:
            logger.error(f"Failed to fetch portfolio: {e}")
            return

        if not portfolio.positions:
            logger.warning("No positions found in portfolio. Skipping report.")
            return

        # Deduplicate symbols (same stock can appear in multiple lots)
        unique_symbols = list(dict.fromkeys(portfolio.symbols))
        logger.info(
            f"Found {len(portfolio.positions)} positions "
            f"({len(unique_symbols)} unique): {', '.join(unique_symbols)}"
        )

        # Step 3: Fetch news for all holdings
        logger.info("[3/5] Fetching news for all holdings...")
        news_by_symbol = self.news_scraper.fetch_all_news(unique_symbols)
        total_news = sum(len(v) for v in news_by_symbol.values())
        logger.info(f"Fetched {total_news} total articles across all holdings")

        # Step 4: Select top 5 cross-portfolio + analyze with Claude (single call)
        top_n = self.settings.top_news_count
        logger.info(f"[4/5] Selecting top {top_n} news across portfolio and analyzing...")
        top_articles = self.news_scraper.select_top_portfolio_news(
            news_by_symbol, portfolio.positions, top_n=top_n
        )
        portfolio_analysis = self.analyzer.analyze_top_news(top_articles, portfolio.positions)

        # Step 5: Send email
        logger.info("[5/5] Sending email report...")
        success = self.email_sender.send_report(
            portfolio, portfolio_analysis, report_type
        )

        if success:
            logger.info("✅ Report sent successfully!")
        else:
            logger.error("❌ Failed to send report.")

    def start_scheduler(self) -> None:
        """Start the APScheduler to run reports at configured times."""
        tz = ZoneInfo(self.settings.timezone)
        scheduler = BlockingScheduler(timezone=tz)

        # Pre-market job
        scheduler.add_job(
            self.run_report,
            CronTrigger(
                hour=self.settings.pre_market_hour,
                minute=self.settings.pre_market_minute,
                day_of_week="mon-fri",
                timezone=tz,
            ),
            args=["pre_market"],
            id="pre_market_report",
            name="Pre-Market Report",
            misfire_grace_time=600,
        )

        # Post-market job
        scheduler.add_job(
            self.run_report,
            CronTrigger(
                hour=self.settings.post_market_hour,
                minute=self.settings.post_market_minute,
                day_of_week="mon-fri",
                timezone=tz,
            ),
            args=["post_market"],
            id="post_market_report",
            name="Post-Market Report",
            misfire_grace_time=600,
        )

        # Graceful shutdown
        def shutdown(signum, frame):
            logger.info("Shutting down scheduler...")
            scheduler.shutdown(wait=False)
            sys.exit(0)

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        logger.info(f"📅 Scheduler started ({self.settings.timezone})")
        logger.info(
            f"   Pre-market:  {self.settings.pre_market_hour:02d}:"
            f"{self.settings.pre_market_minute:02d} Mon-Fri"
        )
        logger.info(
            f"   Post-market: {self.settings.post_market_hour:02d}:"
            f"{self.settings.post_market_minute:02d} Mon-Fri"
        )
        logger.info("Press Ctrl+C to stop.\n")

        scheduler.start()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="E*TRADE Stock Monitor - AI-powered portfolio news & analysis"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single report immediately and exit",
    )
    parser.add_argument(
        "--type",
        choices=["pre_market", "post_market"],
        default="pre_market",
        help="Report type when using --once (default: pre_market)",
    )
    args = parser.parse_args()

    monitor = StockMonitor()

    if args.once:
        monitor.run_report(args.type)
    else:
        monitor.start_scheduler()


if __name__ == "__main__":
    main()
