"""
E*TRADE OAuth 1.0a authentication handler.

Auth modes:
  1. Terminal (default) — opens browser, prompts for PIN in terminal
  2. Telegram — sends auth URL via bot, waits for PIN reply on phone

Token caching:
  - Tokens expire at midnight ET every day
  - Within same day: renew (no PIN needed)
  - New day: full interactive auth (PIN required)
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

    def __init__(self, settings: Settings, notifier=None) -> None:
        """
        Args:
            settings: app configuration
            notifier: optional TelegramNotifier — if set, uses Telegram for PIN delivery
        """
        self.settings = settings
        self.consumer_key = settings.etrade_consumer_key
        self.consumer_secret = settings.etrade_consumer_secret
        self.dev = settings.etrade_environment == "sandbox"
        self.notifier = notifier
        self._access_token: str | None = None
        self._access_token_secret: str | None = None

    def authenticate(self) -> tuple[str, str]:
        """
        Returns (access_token, access_token_secret).
        Priority:
          1. Cached token from today → try renewal (silent)
          2. Full interactive auth (PIN via Telegram or terminal)
        """
        cached = self._load_cached_tokens()
        if cached:
            cached_date = cached.get("date", "")
            today = date.today().isoformat()
            if cached_date == today:
                logger.info("Loaded cached E*TRADE tokens. Attempting renewal...")
                try:
                    self._renew_token(
                        cached["access_token"], cached["access_token_secret"]
                    )
                    self._access_token = cached["access_token"]
                    self._access_token_secret = cached["access_token_secret"]
                    logger.info("Token renewal successful — no login needed.")
                    return self._access_token, self._access_token_secret
                except Exception as e:
                    logger.warning(f"Token renewal failed ({e}). Re-authorizing...")
            else:
                logger.info(
                    f"Cached token from {cached_date} — re-authorizing for today..."
                )

        if self.notifier:
            return self._telegram_auth()
        return self._terminal_auth()

    # -------------------------------------------------------------------------
    # Auth flows
    # -------------------------------------------------------------------------

    def _terminal_auth(self) -> tuple[str, str]:
        """Classic terminal flow — opens browser, reads PIN from stdin."""
        oauth = pyetrade.ETradeOAuth(self.consumer_key, self.consumer_secret)
        authorize_url = self._get_request_token(oauth)

        print("\n" + "=" * 60)
        print("E*TRADE AUTHORIZATION REQUIRED")
        print("=" * 60)
        print(f"\nOpening browser... If it doesn't open, visit:\n{authorize_url}\n")
        print("After authorizing, enter the 5-character verification code.")
        print("=" * 60)

        try:
            webbrowser.open(authorize_url)
        except Exception:
            pass

        verifier_code = input("\nEnter the verification code: ").strip()
        return self._exchange_token(oauth, verifier_code)

    def _telegram_auth(self) -> tuple[str, str]:
        """Telegram flow — sends URL to phone, waits for PIN reply."""
        oauth = pyetrade.ETradeOAuth(self.consumer_key, self.consumer_secret)
        authorize_url = self._get_request_token(oauth)

        logger.info("Requesting E*TRADE PIN via Telegram...")
        pin = self.notifier.send_auth_request(authorize_url)

        if not pin:
            # Fallback to terminal if Telegram timed out and we're in a TTY
            import sys
            if sys.stdin.isatty():
                logger.warning("Telegram auth timed out. Falling back to terminal...")
                self.notifier.send_error(
                    "Auth timeout",
                    "Falling back to terminal input. Check your server.",
                )
                pin = input(
                    "\nTelegram timed out. Enter verification code in terminal: "
                ).strip()
            else:
                raise RuntimeError(
                    "E*TRADE auth failed: no PIN received via Telegram and no TTY available."
                )

        return self._exchange_token(oauth, pin)

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _get_request_token(self, oauth) -> str:
        try:
            return oauth.get_request_token()
        except Exception as e:
            err_str = str(e)
            if "consumer_key_unknown" in err_str or "401" in err_str:
                raise RuntimeError(
                    "E*TRADE rejected the consumer key.\n\n"
                    "Checklist:\n"
                    "  1. Log in at https://developer.etrade.com/ → My Applications\n"
                    "  2. App status must be 'Approved'\n"
                    "  3. ETRADE_CONSUMER_KEY in .env must match exactly (no spaces)\n"
                    "  4. ETRADE_ENVIRONMENT must match the key type (sandbox/live)\n"
                ) from e
            raise

    def _exchange_token(self, oauth, verifier_code: str) -> tuple[str, str]:
        tokens = oauth.get_access_token(verifier_code)
        self._access_token = tokens["oauth_token"]
        self._access_token_secret = tokens["oauth_token_secret"]
        self._save_tokens()

        if self.notifier:
            self.notifier.send_info("✅ E*TRADE authenticated. Token cached for today.")
        logger.info("E*TRADE authentication successful. Token cached.")

        return self._access_token, self._access_token_secret

    def _renew_token(self, access_token: str, access_token_secret: str) -> None:
        access_manager = pyetrade.ETradeAccessManager(
            self.consumer_key,
            self.consumer_secret,
            access_token,
            access_token_secret,
        )
        access_manager.renew_access_token()

    def _save_tokens(self) -> None:
        data = {
            "access_token": self._access_token,
            "access_token_secret": self._access_token_secret,
            "date": date.today().isoformat(),
        }
        TOKEN_CACHE_FILE.write_text(json.dumps(data))
        logger.debug("E*TRADE tokens saved to cache.")

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
