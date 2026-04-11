"""
Telegram bot helper for auth PIN delivery, error alerts, and command handling.

Supported commands (send from Telegram anytime):
  /auth    — trigger E*TRADE re-authorization immediately
  /status  — show current portfolio cache info
  /help    — list available commands

Setup (one-time):
  1. Message @BotFather on Telegram → /newbot → follow prompts → copy token
  2. Start a chat with your new bot (send /start)
  3. Visit https://api.telegram.org/bot{TOKEN}/getUpdates → find "chat":{"id":...}
  4. Add to .env:
       TELEGRAM_ENABLED=true
       TELEGRAM_BOT_TOKEN=your_token_here
       TELEGRAM_CHAT_ID=your_chat_id_here
"""

from __future__ import annotations

import re
import threading
import time
from typing import Callable

import requests

from src.utils.logger import get_logger

logger = get_logger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


class TelegramNotifier:
    """
    Telegram Bot API wrapper.

    Architecture:
      - A background daemon thread (_command_loop) always polls for messages.
      - When auth is in progress, the loop pauses and the auth flow polls directly.
      - Commands (/auth, /status, /help) are dispatched to a registered callback.
    """

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.token = bot_token
        self.chat_id = str(chat_id)
        self._last_update_id: int = 0
        self._update_lock = threading.Lock()         # guards _last_update_id
        self._auth_in_progress = threading.Event()  # pauses command loop during auth
        self._command_callback: Callable[[str], None] | None = None
        self._listener_thread: threading.Thread | None = None

    # -------------------------------------------------------------------------
    # Command listener (runs as daemon thread inside the scheduler process)
    # -------------------------------------------------------------------------

    def start_command_listener(self, callback: Callable[[str], None]) -> None:
        """
        Start a background thread that polls for Telegram commands.
        callback(command_text) is called on the listener thread for each command.
        """
        self._command_callback = callback
        self._listener_thread = threading.Thread(
            target=self._command_loop, daemon=True, name="telegram-listener"
        )
        self._listener_thread.start()
        logger.info("Telegram command listener started (/auth, /status, /help)")

    def _command_loop(self) -> None:
        """Daemon thread: poll for commands, pause while auth is in progress."""
        self._sync_update_offset()   # skip messages sent before startup
        while True:
            try:
                # Yield to auth flow when it's waiting for a PIN
                if self._auth_in_progress.is_set():
                    time.sleep(1)
                    continue

                updates = self._get_updates()
                for update in updates:
                    text = self._extract_text(update)
                    if not text:
                        continue
                    if text.startswith("/") and self._command_callback:
                        logger.info(f"Telegram command received: {text!r}")
                        threading.Thread(
                            target=self._safe_dispatch,
                            args=(text.strip(),),
                            daemon=True,
                        ).start()
            except Exception as e:
                logger.debug(f"Telegram command loop error: {e}")
            time.sleep(3)

    def _safe_dispatch(self, command: str) -> None:
        try:
            self._command_callback(command)
        except Exception as e:
            logger.error(f"Telegram command handler error: {e}")
            self.send_error("Command handler error", str(e))

    # -------------------------------------------------------------------------
    # Public interface
    # -------------------------------------------------------------------------

    def send_message(self, text: str, silent: bool = False) -> bool:
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
        Send the E*TRADE auth URL as a tappable button and wait for PIN reply.
        Pauses the command listener loop while waiting to avoid update conflicts.
        Returns the PIN string, or None if timed out.
        """
        from src.utils.config import get_settings
        settings = get_settings()
        timeout = settings.telegram_auth_timeout_secs
        reminder_after = settings.telegram_auth_reminder_secs

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
            logger.warning(f"Inline button failed ({e}), sending plain URL...")
            self.send_message(
                f"🔐 <b>E*TRADE Authorization Needed</b>\n\n"
                f"Open this link, log in, then reply with the PIN:\n"
                f"{authorize_url}"
            )

        logger.info("Telegram: auth request sent. Waiting for PIN reply...")

        # Pause the command listener so it doesn't consume the PIN message
        self._auth_in_progress.set()
        self._sync_update_offset()   # skip anything that arrived before auth prompt

        try:
            pin = self._poll_for_pin(
                timeout=timeout,
                reminder_after=reminder_after,
                reminder_text=(
                    "⏰ <b>Reminder:</b> E*TRADE PIN still needed.\n"
                    "Please reply with your verification code.\n\n"
                    "If you missed the button, send /auth to restart."
                ),
            )
        finally:
            self._auth_in_progress.clear()   # always resume command listener

        if pin:
            self.send_message("✅ PIN received. Authenticating...")
        else:
            self.send_message(
                "❌ No PIN received within the timeout window.\n\n"
                "Send <b>/auth</b> whenever you're ready to try again."
            )

        return pin

    def send_error(self, title: str, detail: str = "") -> None:
        body = f"⚠️ <b>{title}</b>"
        if detail:
            body += f"\n\n<code>{detail[:800]}</code>"
        self.send_message(body)

    def send_info(self, text: str) -> None:
        self.send_message(text, silent=True)

    # -------------------------------------------------------------------------
    # Internal polling helpers
    # -------------------------------------------------------------------------

    def _poll_for_pin(self, timeout: int, reminder_after: int,
                      reminder_text: str) -> str | None:
        deadline = time.time() + timeout
        reminder_sent = False

        while time.time() < deadline:
            remaining = deadline - time.time()
            if not reminder_sent and remaining < (timeout - reminder_after):
                self.send_message(reminder_text)
                reminder_sent = True

            updates = self._get_updates()
            for update in updates:
                text = self._extract_text(update)
                if text and self._looks_like_pin(text):
                    logger.info("Telegram: PIN received")
                    return text.strip()

            time.sleep(3)

        return None

    def _sync_update_offset(self) -> None:
        """Skip all pending messages so we only react to new ones."""
        updates = self._get_updates(limit=100)
        if updates:
            with self._update_lock:
                self._last_update_id = updates[-1]["update_id"] + 1

    def _get_updates(self, limit: int = 10) -> list[dict]:
        try:
            with self._update_lock:
                offset = self._last_update_id
            resp = self._post("getUpdates", {
                "offset": offset,
                "limit": limit,
                "timeout": 2,
                "allowed_updates": ["message"],
            }, timeout=6)
            updates = resp.get("result", [])
            if updates:
                with self._update_lock:
                    self._last_update_id = updates[-1]["update_id"] + 1
            return updates
        except Exception as e:
            logger.debug(f"Telegram getUpdates error: {e}")
            return []

    def _extract_text(self, update: dict) -> str | None:
        msg = update.get("message", {})
        if str(msg.get("chat", {}).get("id", "")) != self.chat_id:
            return None
        return msg.get("text", "").strip()

    @staticmethod
    def _looks_like_pin(text: str) -> bool:
        """E*TRADE PINs: 3–10 uppercase alphanumeric chars, not a slash command."""
        cleaned = text.strip().upper()
        return (not cleaned.startswith("/") and
                bool(re.fullmatch(r"[A-Z0-9]{3,10}", cleaned)))

    def _post(self, method: str, data: dict, timeout: int = 10) -> dict:
        url = TELEGRAM_API.format(token=self.token, method=method)
        resp = requests.post(url, json=data, timeout=timeout)
        resp.raise_for_status()
        result = resp.json()
        if not result.get("ok"):
            raise RuntimeError(f"Telegram API error: {result}")
        return result


def make_notifier(settings) -> TelegramNotifier | None:
    if not settings.telegram_enabled:
        return None
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.warning(
            "TELEGRAM_ENABLED=true but TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set."
        )
        return None
    return TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
