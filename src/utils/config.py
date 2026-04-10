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

    @property
    def etrade_base_url(self) -> str:
        if self.etrade_environment == "sandbox":
            return "https://apisb.etrade.com"
        return "https://api.etrade.com"


def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
