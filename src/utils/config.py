"""
Configuration management using pydantic-settings.
Loads from .env file and environment variables.
"""

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    """Application settings loaded from environment variables / .env file."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # E*TRADE
    etrade_consumer_key: str = ""
    etrade_consumer_secret: str = ""
    etrade_environment: str = "sandbox"  # "sandbox" or "live"

    # LLM Backend: "gemini" (free), "claude" (paid), or "fallback" (no API)
    llm_backend: str = "gemini"

    # Google Gemini (FREE - recommended)
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"

    # Anthropic Claude (PAID - with daily spend cap)
    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-4-20250514"
    daily_spend_limit_usd: float = 0.50  # Max $/day on Claude API

    # Email: "gmail_api" (OAuth2, no App Password) or "smtp" (needs App Password)
    email_backend: str = "gmail_api"
    gmail_address: str = ""
    gmail_app_password: str = ""  # Only needed if email_backend=smtp
    recipient_email: str = ""

    # Schedule
    pre_market_hour: int = 8
    pre_market_minute: int = 30
    post_market_hour: int = 16
    post_market_minute: int = 30
    timezone: str = "US/Eastern"

    # News
    max_news_per_stock: int = 20
    top_news_count: int = 5

    # Email display
    # Set to True to hide the total dollar amount — shows % performance instead.
    # Per-stock prices and P&L percentages are always shown regardless.
    hide_account_value: bool = True

    # Schedule — midday report
    mid_market_hour: int = 12
    mid_market_minute: int = 0

    # Portfolio cache + weekly re-auth schedule
    # Day of week for the weekly E*TRADE re-auth (sun/mon/tue/..)
    portfolio_refresh_day: str = "sun"
    # Hour and minute for the weekly refresh (local ET time, 24h)
    portfolio_refresh_hour: int = 12
    portfolio_refresh_minute: int = 0
    # Fallback: also re-auth if cache is older than this many days
    # (catches missed Sundays, e.g. Mac was off). Set 0 to disable.
    portfolio_cache_days: int = 8

    # Telegram bot — for auth PIN delivery and error alerts.
    # Set in .env — never hardcode here.
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    # Seconds to wait for the user to reply with PIN before sending a reminder.
    telegram_auth_reminder_secs: int = 120
    # Total seconds to wait before giving up on Telegram auth.
    telegram_auth_timeout_secs: int = 300

    @property
    def etrade_base_url(self) -> str:
        if self.etrade_environment == "sandbox":
            return "https://apisb.etrade.com"
        return "https://api.etrade.com"


def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
