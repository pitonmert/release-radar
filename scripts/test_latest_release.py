#!/usr/bin/env python3
"""Send the latest Claude Code release once using disposable state, never production state."""

import argparse
from contextlib import closing
from contextlib import nullcontext
from functools import partial
import logging
from pathlib import Path
import sys
import threading
import time
from tempfile import TemporaryDirectory
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from release_radar import database as db  # noqa: E402
from release_radar import config as settings, feeds, summarizer as summaries, service  # noqa: E402
from release_radar.pagination import Pager  # noqa: E402
from release_radar.publisher import Publisher  # noqa: E402
from release_radar.telegram import TelegramClient  # noqa: E402

SOURCE = "Claude Code"


def entry_guid(entry):
    return entry.get("id") or entry.get("guid") or entry.get("link")


def latest_entry(entries):
    candidates = [
        entry for entry in entries
        if entry_guid(entry) and "insiders" not in entry.get("title", "").lower()
    ]
    if not candidates:
        raise ValueError("Claude Code akışında uygun sürüm bulunamadı.")
    # Prefer publication time; GitHub Atom usually provides only an update timestamp.
    if all(entry.get("published_parsed") or entry.get("updated_parsed") for entry in candidates):
        return max(
            candidates,
            key=lambda entry: entry.get("published_parsed") or entry["updated_parsed"],
        )
    logging.warning("Eksik tarihler var; GitHub akışının en başındaki uygun sürüm seçiliyor.")
    return candidates[0]


def run_test(model=settings.DEFAULT_MODEL, version=None, interactive=False, listen_seconds=600):
    config = settings.load_json_file(settings.CONFIG_FILE, {})
    source = config.get(SOURCE)
    if not source or source.get("type") != "github_releases" or not source.get("rss"):
        raise ValueError("config.json içinde geçerli bir Claude Code GitHub kaynağı gerekli.")

    settings.load_config()
    logging.warning("Gerçek Telegram gönderimi yapılacak ve Gemini kotası kullanılacak.")
    logging.info("İstenen model: %s", model)
    feed = feeds.fetch_feed_with_retry(source["rss"], SOURCE)
    if not feed or not feed.entries:
        raise ValueError("Claude Code akışı boş veya erişilemiyor.")
    if version:
        matches = [e for e in feed.entries if e.get("title", "").strip() == version]
        if len(matches) != 1 or not entry_guid(matches[0]) or "insiders" in version.lower():
            raise ValueError("İstenen sürüm akışta benzersiz ve uygun olarak bulunamadı.")
        selected = matches[0]
    else:
        selected = latest_entry(feed.entries)
    guid = entry_guid(selected)
    logging.info("Test sürümü: %s (%s)", selected.get("title", ""), guid)

    # Freeze the feed: announcements arriving during the test must not get sent too.
    snapshot = SimpleNamespace(entries=list(feed.entries))
    client = TelegramClient(settings.TELEGRAM_TOKEN, settings.TELEGRAM_CHAT_ID) if interactive else None
    if client:
        client.preflight()
    with (client.poll_lock() if client else nullcontext()), TemporaryDirectory(
        prefix="release-radar-test-",
    ) as temporary_dir:
        db_path = str(Path(temporary_dir) / "state.db")
        pager = Pager(client, db_path, config) if client else None
        started_at = int(time.time())
        try:
            if pager:
                pager.poll_once(timeout=1, not_before=started_at)
            service.run_scan_cycle(
                config={SOURCE: source}, db_path=db_path, feed_loader=lambda *_: snapshot,
            )
            with closing(db.get_connection(db_path=db_path)) as conn:
                if guid not in db.get_seen_guids(conn, SOURCE):
                    raise RuntimeError("İlk tarama seçilen sürümü kaydedemedi.")
                conn.execute("DELETE FROM seen_entries WHERE source = ? AND guid = ?", (SOURCE, guid))
                conn.commit()

            # Disable initial sync here, including when the feed contained only one release.
            publisher = Publisher(pager, partial(summaries.process_ai, model=model), model) if pager else None
            service.run_scan_cycle(
                config={SOURCE: source}, db_path=db_path,
                feed_loader=lambda *_: SimpleNamespace(entries=[selected]), initialize_empty=False,
                summarizer=partial(summaries.process_ai, model=model), publisher=publisher,
            )
            with closing(db.get_connection(db_path=db_path)) as conn:
                if guid not in db.get_seen_guids(conn, SOURCE):
                    raise RuntimeError("Özetleme veya Telegram gönderimi tamamlanamadı; logları inceleyin.")
            if pager:
                logging.info("INTERACTIVE_TEST_READY: %s seconds. Önceki/Sonraki butonlarını deneyin.",
                             listen_seconds)
                pager.listen(threading.Event(), seconds=listen_seconds, not_before=started_at)
        finally:
            if pager:
                pager.finish_test()

    logging.info("Gönderim başarılı. Geçici veritabanı temizlendi.")


def main(argv=()):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model", default=settings.DEFAULT_MODEL,
        help=f"Gemini model ID for this test only (default: {settings.DEFAULT_MODEL})",
    )
    parser.add_argument("--version", help="Exact release title, for example v2.1.278")
    parser.add_argument("--interactive", action="store_true", help="Send the first summary page and listen for pagination")
    parser.add_argument("--listen-seconds", type=int, default=600, help="Interactive test duration")
    args = parser.parse_args(argv)
    if not args.model.strip():
        parser.error("--model must not be empty")
    if args.listen_seconds < 1:
        parser.error("--listen-seconds must be positive")
    try:
        run_test(model=args.model.strip(), version=args.version, interactive=args.interactive,
                 listen_seconds=args.listen_seconds)
    except KeyboardInterrupt:
        logging.error("Test iptal edildi; varsa geçici veritabanı temizlendi.")
        return 130
    except Exception as error:
        logging.error("Test başarısız: %s. Varsa geçici veritabanı temizlendi.", error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
