"""
Telegram bot helper for auth PIN delivery and error alerts.

Setup (one-time):
  1. Message @BotFather on Telegram → /newbot → follow prompts → copy token
  2. Start a chat with your new bot (just send /start)
  3. Visit https://api.telegram.org/bot{TOKEN}/getUpdates → find "chat":{"id":...}
  4. Add to .env:
       TELEGRAM_ENABLED=true
       TELEGRAM_BOT_TOKEN=your_token_here
       TELEGRAM_CHAT_ID=your_chat_id_here
"""

from __future__ import annotations

import re
import time

import requests

from src.utils.logger import get_logger

logger = get_logger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


class TelegramNotifier:
    """
    Thin wrapper around Telegram Bot API.

    Used for:
      - Sending the E*TRADE auth URL and waiting for PIN reply
      - Sending error/alert messages
      - Sending info messages (auth success, report sent, etc.)
    """

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.token = bot_token
        self.chat_id = str(chat_id)
        self._last_update_id: int = 0

    # -------------------------------------------------------------------------
    # Public interface
    # -------------------------------------------------------------------------

    def send_message(self, text: str, silent: bool = False) -> bool:
        """Send a plain-text message. Returns True on success."""
        try:
            self._post("sendMessage", {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_notification": silent,
            })
            return True
        except Exception as e:
            logger.error(f"Telegram sendMessage failed: {e}")
            return False

    def send_auth_request(self, authorize_url: str) -> str | None:
        """
        Send the E*TRADE auth URL and wait for the user to reply with a PIN.

        Returns the PIN string, or None if timed out.
        Sends a reminder halfway through the timeout window.
        """
        from src.utils.config import get_settings
        settings = get_settings()
        timeout = settings.telegram_auth_timeout_secs
        reminder_after = settings.telegram_auth_reminder_secs

        # Send message with an inline button — buttons are always tappable
        try:
            self._post("sendMessage", {
                "chat_id": self.chat_id,
                "text": (
                    "🔐 <b>E*TRADE Authorization Needed</b>\n\n"
                    "1. Tap the button below to open E*TRADE login\n"
                    "2. Log in and complete verification\n"
                    "3. Copy the 5-character PIN shown on screen\n"
                    "4. Reply to this message with the PIN"
                ),
                "parse_mode": "HTML",
                "reply_markup": {
                    "inline_keyboard": [[
                        {"text": "🔓 Authorize E*TRADE", "url": authorize_url}
                    ]]
                },
            })
        except Exception as e:
            # Fallback: send URL as plain text (auto-linked by Telegram)
            logger.warning(f"Inline button failed ({e}), sending plain URL...")
            self.send_message(
                f"🔐 <b>E*TRADE Authorization Needed</b>\n\n"
                f"Open this link, log in, then reply with the PIN:\n"
                f"{authorize_url}"
            )
        logger.info("Telegram: auth request sent. Waiting for PIN reply...")

        pin = self._poll_for_pin(timeout=timeout, reminder_after=reminder_after,
                                  reminder_text=(
                                      "⏰ <b>Reminder:</b> E*TRADE PIN still needed.\n"
                                      "Please reply with your verification code."
                                  ))
        if pin:
            self.send_message(f"✅ PIN received. Authenticating...")
        else:
            self.send_message(
                "❌ No PIN received within the timeout window.\n"
                "The scheduled report will be skipped.\n"
                "Re-run manually to try again."
            )

        return pin

    def send_error(self, title: str, detail: str = "") -> None:
        """Send an error alert."""
        body = f"⚠️ <b>{title}</b>"
        if detail:
            body += f"\n\n<code>{detail[:800]}</code>"
        self.send_message(body)

    def send_info(self, text: str) -> None:
        """Send a silent info notification (no phone buzz)."""
        self.send_message(text, silent=True)

    # -------------------------------------------------------------------------
    # Internal polling
    # -------------------------------------------------------------------------

    def _poll_for_pin(
        self,
        timeout: int,
        reminder_after: int,
        reminder_text: str,
    ) -> str | None:
        """
        Poll getUpdates every 3 seconds for a reply containing a PIN-like code.
        A valid PIN is 3–10 alphanumeric characters (E*TRADE uses 5 chars).
        """
        self._sync_update_offset()  # skip any old pending messages

        deadline = time.time() + timeout
        reminder_sent = False
        poll_interval = 3

        while time.time() < deadline:
            remaining = deadline - time.time()

            # Send reminder once
            if not reminder_sent and remaining < (timeout - reminder_after):
                self.send_message(reminder_text)
                reminder_sent = True

            updates = self._get_updates()
            for update in updates:
                text = self._extract_text(update)
                if text and self._looks_like_pin(text):
                    logger.info(f"Telegram: received PIN reply")
                    return text.strip()

            time.sleep(poll_interval)

        return None

    def _sync_update_offset(self) -> None:
        """Fast-forward the offset so we ignore any messages sent before now."""
        updates = self._get_updates(limit=100)
        if updates:
            self._last_update_id = updates[-1]["update_id"] + 1

    def _get_updates(self, limit: int = 10) -> list[dict]:
        """Fetch new updates from Telegram, advancing the offset."""
        try:
            resp = self._post("getUpdates", {
                "offset": self._last_update_id,
                "limit": limit,
                "timeout": 2,      # long-poll for 2s on Telegram's side
                "allowed_updates": ["message"],
            }, timeout=6)
            updates = resp.get("result", [])
            if updates:
                self._last_update_id = updates[-1]["update_id"] + 1
            return updates
        except Exception as e:
            logger.debug(f"Telegram getUpdates error: {e}")
            return []

    def _extract_text(self, update: dict) -> str | None:
        """Pull the message text from an update object."""
        msg = update.get("message", {})
        # Only accept messages from our own chat
        if str(msg.get("chat", {}).get("id", "")) != self.chat_id:
            return None
        return msg.get("text", "").strip()

    @staticmethod
    def _looks_like_pin(text: str) -> bool:
        """Heuristic: E*TRADE PINs are 3–10 uppercase alphanumeric chars."""
        cleaned = text.strip().upper()
        return bool(re.fullmatch(r"[A-Z0-9]{3,10}", cleaned))

    def _post(self, method: str, data: dict, timeout: int = 10) -> dict:
        url = TELEGRAM_API.format(token=self.token, method=method)
        resp = requests.post(url, json=data, timeout=timeout)
        resp.raise_for_status()
        result = resp.json()
        if not result.get("ok"):
            raise RuntimeError(f"Telegram API error: {result}")
        return result


def make_notifier(settings) -> TelegramNotifier | None:
    """Return a TelegramNotifier if enabled, else None."""
    if not settings.telegram_enabled:
        return None
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.warning(
            "TELEGRAM_ENABLED=true but TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set. "
            "Disabling Telegram."
        )
        return None
    return TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
