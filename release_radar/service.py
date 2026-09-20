"""Coordinate scans, publishing, and the Telegram listener."""

from functools import partial
import logging
import os
import signal
import sys
import threading

from . import config as settings, database as db, feeds, summarizer as summaries, telegram
from .messages import build_messages
from .pagination import Pager
from .publisher import Publisher
from .telegram import TelegramClient

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


def run_scan_cycle(
    *, config=None, db_path=None, feed_loader=None, initialize_empty=True, summarizer=None,
    publisher=None,
):
    """Scan once; explicit inputs also support isolated, deterministic replay tests."""
    logging.info("release-radar scan process started.")

    if config is None:
        config = settings.load_json_file(settings.CONFIG_FILE, {})
    if feed_loader is None:
        feed_loader = feeds.fetch_feed_with_retry
    if summarizer is None:
        summarizer = summaries.process_ai
    if not config:
        logging.error("config.json is empty or unreadable; skipping this scan cycle.")
        return

    if publisher is not None:
        publisher.retry_pending()

    conn = db.get_connection(db_path=db_path)
    try:
        for source_name, source_config in config.items():
            rss_url = source_config.get("rss")
            source_type = source_config.get("type")

            if not rss_url or not source_type:
                logging.error(f"{source_name}: 'rss' or 'type' missing in config, skipping.")
                continue

            try:
                feed = feed_loader(rss_url, source_name)
                if not feed or not feed.entries:
                    logging.warning(f"{source_name}: Feed empty or unreachable, skipping.")
                    continue

                seen_guids = db.get_seen_guids(conn, source_name)

                if not seen_guids and initialize_empty:
                    logging.info(f"Initial setup for {source_name}, syncing data...")
                    initial_guids = []
                    for entry in feed.entries:
                        guid = entry.get("id") or entry.get("guid") or entry.get("link")
                        if not guid:
                            continue
                        entry_link = entry.get("link", "")
                        if source_type == "vscode_github" and "/updates/" not in entry_link:
                            continue
                        initial_guids.append(guid)
                    db.mark_seen(conn, source_name, initial_guids)
                    continue

                current_source_updates, skipped_guids = feeds.collect_updates(
                    feed, source_name, source_type, seen_guids,
                )

                if not current_source_updates and not skipped_guids:
                    logging.info(f"{source_name}: No new updates.")
                    continue

                if current_source_updates:
                    logging.info(
                        f"Found {len(current_source_updates)} new updates in {source_name}."
                    )

                successful_guids = []

                for update in current_source_updates:
                    logging.info(f"Processing: {update['title']} -> {update['url']}")

                    if publisher is not None:
                        publisher(source_name, update)
                        continue

                    ai_summary = summarizer(update["text"])

                    if not ai_summary:
                        logging.error(
                            f"Failed to generate AI summary for {source_name} - '{update['title']}'."
                        )
                        continue

                    try:
                        messages = build_messages(
                            source_name,
                            update["title"],
                            update["url"],
                            ai_summary,
                            update["published_date"],
                        )
                    except ValueError as e:
                        logging.error("Failed to render '%s': %s", update["title"], e)
                        continue
                    if telegram.send_telegram_message(messages):
                        successful_guids.append(update["guid"])

                if successful_guids or skipped_guids:
                    db.mark_seen(conn, source_name, [*successful_guids, *skipped_guids])

            except Exception as e:
                logging.error(f"Unexpected error while processing {source_name}: {e}")
    finally:
        conn.close()

    logging.info("release-radar scan process completed.")


_stop_event = threading.Event()


def _handle_shutdown_signal(signum, frame):
    logging.info(f"Received signal {signum}; stopping after the current cycle.")
    _stop_event.set()


def main():
    settings.load_config()
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    client = TelegramClient(settings.TELEGRAM_TOKEN, settings.TELEGRAM_CHAT_ID, _stop_event)
    client.preflight()
    config = settings.load_json_file(settings.CONFIG_FILE, {})
    model = os.getenv("GEMINI_MODEL", settings.DEFAULT_MODEL)
    pager = Pager(client, db.DB_PATH, config)
    publisher = Publisher(pager, partial(summaries.process_ai, model=model), model)

    def scan_loop():
        while not _stop_event.is_set():
            try:
                run_scan_cycle(publisher=publisher)
            except Exception:
                logging.exception("Unhandled scan failure; will retry next interval.")
            _stop_event.wait(settings.SCAN_INTERVAL_SECONDS)

    with client.poll_lock():
        # A conflict is fatal; never delete webhooks or drop pending updates.
        pager.poll_once(timeout=1)
        scanner = threading.Thread(target=scan_loop, name="release-scanner", daemon=True)
        scanner.start()
        logging.info("release-radar interactive service started (model: %s).", model)
        try:
            pager.listen(_stop_event)
        finally:
            _stop_event.set()
            scanner.join(timeout=60)
    logging.info("release-radar service stopped.")


if __name__ == "__main__":
    main()
