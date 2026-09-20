"""Telegram HTTP client and direct message delivery."""

from contextlib import contextmanager
import hashlib
import logging
import os
import tempfile
import threading
import time
import requests

from . import config
from .messages import TelegramMessage

MAX_TELEGRAM_RETRIES = 3
RETRY_BACKOFF_BASE = 2


class TelegramError(RuntimeError):
    def __init__(self, code, description):
        self.code = code
        super().__init__(f"Telegram {code}: {description}")


class TelegramClient:
    def __init__(self, token, chat_id, stop=None):
        self.token = token
        self.chat_id = str(chat_id)
        self.stop = stop or threading.Event()

    def call(self, method, **payload):
        for attempt in range(3):
            try:
                response = requests.post(
                    f"https://api.telegram.org/bot{self.token}/{method}", json=payload,
                    timeout=30 if method == "getUpdates" else 10,
                )
                data = response.json()
            except (requests.RequestException, ValueError):
                # Never include request URLs (which contain the bot token) in logs.
                if attempt == 2:
                    raise TelegramError(0, "Network or invalid response") from None
                if self.stop.wait(2 ** attempt):
                    raise TelegramError(0, "Stopped") from None
                continue
            if data.get("ok"):
                return data.get("result")
            code = data.get("error_code", response.status_code)
            description = str(data.get("description", "Request failed")).replace(self.token, "[redacted]")
            if method == "editMessageText" and "message is not modified" in description.lower():
                return True
            if code == 429 and attempt < 2:
                delay = max(1, int(data.get("parameters", {}).get("retry_after", 1)))
                if not self.stop.wait(delay):
                    continue
            raise TelegramError(code, description)

    def send(self, text, keyboard):
        result = self.call("sendMessage", chat_id=self.chat_id, text=text, parse_mode="HTML",
                           link_preview_options={"is_disabled": True}, reply_markup=keyboard)
        return result["message_id"]

    def edit(self, message_id, text, keyboard):
        return self.call("editMessageText", chat_id=self.chat_id, message_id=message_id,
                         text=text, parse_mode="HTML", link_preview_options={"is_disabled": True},
                         reply_markup=keyboard)

    def preflight(self):
        if self.call("getWebhookInfo").get("url"):
            raise RuntimeError("Webhook etkin; ayarlar değiştirilmedi. Ayrı test botu kullanın.")
        chat = self.call("getChat", chat_id=self.chat_id)
        if chat.get("type") != "private":
            raise RuntimeError("Sayfalı mesajlar yalnızca yapılandırılmış özel sohbeti destekler.")

    @contextmanager
    def poll_lock(self):
        # Prevent a second local consumer. Cross-host conflicts also fail on Telegram 409.
        import fcntl
        digest = hashlib.sha256(self.token.encode()).hexdigest()[:20]
        path = os.path.join(tempfile.gettempdir(), f"release-radar-poll-{digest}.lock")
        with open(path, "a", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Bu bot için başka bir local dinleyici zaten çalışıyor.") from None
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def _send_single_telegram(message: TelegramMessage):
    url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": message.text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
    if message.reply_markup:
        payload["reply_markup"] = message.reply_markup

    for attempt in range(1, MAX_TELEGRAM_RETRIES + 1):
        try:
            response = requests.post(url, json=payload, timeout=10)
            if response.status_code == 200 and response.json().get("ok") is True:
                return True
            logging.warning(
                f"Telegram attempt {attempt}/{MAX_TELEGRAM_RETRIES} "
                f"failed (HTTP {response.status_code}): {response.text}"
            )
        except Exception as e:
            logging.warning(
                f"Telegram attempt {attempt}/{MAX_TELEGRAM_RETRIES} connection error: {e}"
            )

        if attempt < MAX_TELEGRAM_RETRIES:
            time.sleep(RETRY_BACKOFF_BASE**attempt)

    logging.error("All Telegram send attempts exhausted.")
    return False


def send_telegram_message(messages: list[TelegramMessage]):
    if not messages:
        return False
    for i, message in enumerate(messages, 1):
        if not _send_single_telegram(message):
            logging.error(f"Failed to send Telegram chunk {i}/{len(messages)}.")
            return False
        if i < len(messages):
            time.sleep(1)

    logging.info("Telegram notification sent successfully (%s parts).", len(messages))
    return True
