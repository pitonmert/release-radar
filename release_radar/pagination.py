"""Persistent navigation of cached release pages."""

from contextlib import closing
import json
import logging
import time

from . import database as db
from .messages import is_web_url
from .telegram import TelegramError


class Pager:
    def __init__(self, client, db_path, sources):
        self.client = client
        self.db_path = db_path
        self.sources = set(sources)
        with closing(db.get_connection(db_path)) as conn:
            self.namespace = db.namespace(conn)

    def button(self, label, action, target="", page=0):
        value = f"rr:{self.namespace}:{action}:{target}:{page}"
        if len(value.encode()) > 64:
            raise ValueError("Callback exceeds Telegram limit")
        return {"text": label, "callback_data": value}

    def official(self, release):
        return ([{"text": "Resmî notları aç", "url": release["url"]}]
                if is_web_url(release["url"]) else [])

    def release_view(self, release_id, page):
        with closing(db.get_connection(self.db_path)) as conn:
            release = db.get_release(conn, release_id)
        if not release or not release["pages_json"]:
            raise ValueError("Bu özet artık mevcut değil.")
        pages = json.loads(release["pages_json"])
        if page < 0 or page >= len(pages):
            raise ValueError("Geçersiz sayfa.")
        navigation = []
        if page:
            navigation.append(self.button("← Önceki", "open", release_id, page - 1))
        if page + 1 < len(pages):
            navigation.append(self.button("Sonraki →", "open", release_id, page + 1))
        rows = [navigation] if navigation else []
        if official := self.official(release):
            rows.append(official)
        return pages[page], {"inline_keyboard": rows}

    def handle(self, update):
        callback = update.get("callback_query")
        if not callback:
            return
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        if str(chat.get("id")) != self.client.chat_id:
            self.client.call("answerCallbackQuery", callback_query_id=callback["id"],
                             text="Bu mesaja erişiminiz yok.")
            return
        parts = callback.get("data", "").split(":")
        with closing(db.get_connection(self.db_path)) as conn:
            owned = conn.execute(
                "SELECT release_id FROM bot_messages WHERE chat_id=? AND message_id=?",
                (self.client.chat_id, message.get("message_id")),
            ).fetchone()
        if (not owned or len(parts) != 5 or parts[:2] != ["rr", self.namespace]
                or parts[2] != "open" or owned[0] != parts[3]):
            self.client.call("answerCallbackQuery", callback_query_id=callback["id"],
                             text="Bu sayfa bağlantısı artık etkin değil.")
            return
        try:
            page = int(parts[4])
            if not 0 <= page <= 1_000_000:
                raise ValueError("Geçersiz sayfa.")
            view = self.release_view(parts[3], page)
        except (ValueError, TypeError):
            self.client.call("answerCallbackQuery", callback_query_id=callback["id"],
                             text="Bu sayfa artık mevcut değil.")
            return
        self.client.call("answerCallbackQuery", callback_query_id=callback["id"])
        self.client.edit(message["message_id"], *view)
        logging.info("Release page displayed: %s", page + 1)

    def poll_once(self, timeout=20, not_before=None):
        with closing(db.get_connection(self.db_path)) as conn:
            offset = int(db.get_meta(conn, "offset", "0"))
        updates = self.client.call("getUpdates", offset=offset, timeout=timeout,
                                   allowed_updates=["callback_query"])
        for update in updates:
            try:
                if not_before is None or update.get("message", {}).get("date", not_before) >= not_before:
                    self.handle(update)
            except TelegramError as error:
                if error.code not in (400, 403):
                    raise
                logging.warning("Unavailable Telegram message skipped (%s).", error.code)
            with closing(db.get_connection(self.db_path)) as conn:
                db.set_meta(conn, "offset", update["update_id"] + 1)

    def listen(self, stop, seconds=None, not_before=None):
        deadline = time.monotonic() + seconds if seconds is not None else None
        while not stop.is_set() and (deadline is None or time.monotonic() < deadline):
            timeout = 20 if deadline is None else max(1, min(20, int(deadline - time.monotonic())))
            try:
                self.poll_once(timeout, not_before)
            except TelegramError as error:
                if error.code in (401, 409):
                    stop.set()
                    raise RuntimeError("Telegram listener authentication/conflict failure; stopped.") from error
                logging.warning("Telegram listener temporarily unavailable (%s).", error.code)
                stop.wait(5)

    def finish_test(self):
        with closing(db.get_connection(self.db_path)) as conn:
            messages = conn.execute("SELECT message_id, release_id FROM bot_messages WHERE chat_id=?",
                                    (self.client.chat_id,)).fetchall()
            releases = {rid: db.get_release(conn, rid) for _, rid in messages if rid}
        for message_id, release_id in messages:
            release = releases.get(release_id)
            rows = [self.official(release)] if release and self.official(release) else []
            try:
                self.client.edit(message_id, "İnteraktif test sona erdi.", {"inline_keyboard": rows})
            except TelegramError as error:
                logging.error("Test message cleanup failed (%s); buttons may remain visible.", error.code)
