"""Cache generated summaries and deliver their first page."""

from contextlib import closing
from dataclasses import asdict
import logging

from . import database as db
from .messages import build_messages
from .telegram import TelegramError


class Publisher:
    def __init__(self, pager, summarizer, model):
        self.pager = pager
        self.summarizer = summarizer
        self.model = model
        self.attempted = set()

    def retry_pending(self):
        self.attempted.clear()
        with closing(db.get_connection(self.pager.db_path)) as conn:
            ids = conn.execute("SELECT id FROM releases WHERE notified=0 ORDER BY discovered_at, rowid").fetchall()
        for (release_id,) in ids:
            with closing(db.get_connection(self.pager.db_path)) as conn:
                release = db.get_release(conn, release_id)
            if release["source"] in self.pager.sources:
                self.publish(release_id)

    def __call__(self, source, update):
        with closing(db.get_connection(self.pager.db_path)) as conn:
            release = db.save_release(conn, source, update)
        return self.publish(release["id"])

    def publish(self, release_id):
        if release_id in self.attempted:
            return False
        self.attempted.add(release_id)
        try:
            with closing(db.get_connection(self.pager.db_path)) as conn:
                release = db.get_release(conn, release_id)
            if release["notified"]:
                return True
            if not release["pages_json"]:
                summary = self.summarizer(release["source_text"])
                if summary is None:
                    logging.error("Summary unavailable; queued for the next scan.")
                    return False
                pages = build_messages(release["source"], release["title"], release["url"], summary,
                                       release["published_date"])
                with closing(db.get_connection(self.pager.db_path)) as conn:
                    db.save_summary(conn, release_id, self.model, asdict(summary), [p.text for p in pages])
                    release = db.get_release(conn, release_id)
            message_id = self.pager.client.send(*self.pager.release_view(release_id, 0))
            with closing(db.get_connection(self.pager.db_path)) as conn:
                db.mark_notified(conn, release, self.pager.client.chat_id, message_id)
            logging.info("Interactive notification sent; summary cached (%s).", release["title"])
            return True
        except (TelegramError, ValueError) as error:
            logging.error("Release deferred until next scan: %s", error)
            return False
        except Exception as error:
            logging.error("Release deferred after unexpected %s.", type(error).__name__)
            return False
