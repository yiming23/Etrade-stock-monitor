"""
E*TRADE OAuth 1.0a authentication handler.

Flow:
  1. Get request token from E*TRADE
  2. User authorizes via browser → gets verifier code
  3. Exchange verifier for access token
  4. Token valid until midnight ET — renewed automatically each run.

Daily auth note:
  E*TRADE tokens expire at midnight ET every day. The scheduler will
  prompt for a new verification code once per day (first run of the day).
  After that, both pre-market and post-market runs reuse the cached token.
"""

import json
import webbrowser
from datetime import date
from pathlib import Path

import pyetrade

from src.utils.config import Settings, PROJECT_ROOT
from src.utils.logger import get_logger

logger = get_logger(__name__)

TOKEN_CACHE_FILE = PROJECT_ROOT / ".etrade_token_cache.json"


class ETradeAuth:
    """Manages E*TRADE OAuth authentication lifecycle."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.consumer_key = settings.etrade_consumer_key
        self.consumer_secret = settings.etrade_consumer_secret
        self.dev = settings.etrade_environment == "sandbox"
        self._access_token: str | None = None
        self._access_token_secret: str | None = None

    def authenticate(self) -> tuple[str, str]:
        """
        Returns (access_token, access_token_secret).
        Tries cached token + renewal first; falls back to interactive auth.
        """
        cached = self._load_cached_tokens()
        if cached:
            # Only renew if cached from today (tokens expire at midnight ET)
            cached_date = cached.get("date", "")
            today = date.today().isoformat()
            if cached_date == today:
                logger.info("Loaded cached access tokens. Attempting renewal...")
                try:
                    self._renew_token(cached["access_token"], cached["access_token_secret"])
                    self._access_token = cached["access_token"]
                    self._access_token_secret = cached["access_token_secret"]
                    logger.info("Token renewal successful — no login needed.")
                    return self._access_token, self._access_token_secret
                except Exception as e:
                    logger.warning(f"Token renewal failed ({e}). Re-authorizing...")
            else:
                logger.info(f"Cached token is from {cached_date}, re-authorizing for today...")

        return self._interactive_auth()

    def _interactive_auth(self) -> tuple[str, str]:
        """Run interactive OAuth flow requiring user action in browser."""
        oauth = pyetrade.ETradeOAuth(self.consumer_key, self.consumer_secret)

        try:
            authorize_url = oauth.get_request_token()
        except Exception as e:
            err_str = str(e)
            if "consumer_key_unknown" in err_str or "401" in err_str:
                raise RuntimeError(
                    "E*TRADE rejected the consumer key (consumer_key_unknown).\n\n"
                    "How to fix:\n"
                    "  1. Log in to https://developer.etrade.com/\n"
                    "  2. Go to 'My Applications' → check app status is 'Approved'\n"
                    "  3. Confirm ETRADE_CONSUMER_KEY in .env matches exactly\n"
                    "  4. Confirm ETRADE_ENVIRONMENT matches key type (sandbox/live)\n"
                ) from e
            raise

        print("\n" + "=" * 60)
        print("E*TRADE AUTHORIZATION REQUIRED (once per day)")
        print("=" * 60)
        print(f"\nOpening browser... If it doesn't open, visit:\n{authorize_url}\n")
        print("After authorizing, you'll receive a 5-character verification code.")
        print("=" * 60)

        try:
            webbrowser.open(authorize_url)
        except Exception:
            pass

        verifier_code = input("\nEnter the verification code: ").strip()

        tokens = oauth.get_access_token(verifier_code)
        self._access_token = tokens["oauth_token"]
        self._access_token_secret = tokens["oauth_token_secret"]

        self._save_tokens()
        logger.info("Authentication successful. Token cached for today.")

        return self._access_token, self._access_token_secret

    def _renew_token(self, access_token: str, access_token_secret: str) -> None:
        """Renew an existing access token (valid within same calendar day ET)."""
        # Note: ETradeAccessManager does not accept 'dev' kwarg in current pyetrade
        access_manager = pyetrade.ETradeAccessManager(
            self.consumer_key,
            self.consumer_secret,
            access_token,
            access_token_secret,
        )
        access_manager.renew_access_token()

    def _save_tokens(self) -> None:
        """Save tokens with today's date so we know when they expire."""
        data = {
            "access_token": self._access_token,
            "access_token_secret": self._access_token_secret,
            "date": date.today().isoformat(),
        }
        TOKEN_CACHE_FILE.write_text(json.dumps(data))
        logger.debug("Tokens saved to cache.")

    def _load_cached_tokens(self) -> dict | None:
        if not TOKEN_CACHE_FILE.exists():
            return None
        try:
            data = json.loads(TOKEN_CACHE_FILE.read_text())
            if data.get("access_token") and data.get("access_token_secret"):
                return data
        except (json.JSONDecodeError, KeyError):
            pass
        return None

    @property
    def access_token(self) -> str:
        if not self._access_token:
            raise RuntimeError("Not authenticated. Call authenticate() first.")
        return self._access_token

    @property
    def access_token_secret(self) -> str:
        if not self._access_token_secret:
            raise RuntimeError("Not authenticated. Call authenticate() first.")
        return self._access_token_secret
