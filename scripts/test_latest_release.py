#!/usr/bin/env python3
"""Send the latest Claude Code release once using disposable state, never production state."""

from contextlib import closing
import logging
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import db  # noqa: E402
import main as app  # noqa: E402

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


def run_test():
    config = app.load_json_file(app.CONFIG_FILE, {})
    source = config.get(SOURCE)
    if not source or source.get("type") != "github_releases" or not source.get("rss"):
        raise ValueError("config.json içinde geçerli bir Claude Code GitHub kaynağı gerekli.")

    app.load_config()
    logging.warning("Gerçek Telegram gönderimi yapılacak ve Gemini kotası kullanılacak.")
    feed = app.fetch_feed_with_retry(source["rss"], SOURCE)
    if not feed or not feed.entries:
        raise ValueError("Claude Code akışı boş veya erişilemiyor.")
    selected = latest_entry(feed.entries)
    guid = entry_guid(selected)
    logging.info("Test sürümü: %s (%s)", selected.get("title", ""), guid)

    # Freeze the feed: announcements arriving during the test must not get sent too.
    snapshot = SimpleNamespace(entries=list(feed.entries))
    with TemporaryDirectory(prefix="release-radar-test-") as temporary_dir:
        db_path = str(Path(temporary_dir) / "state.db")
        app.run_scan_cycle(
            config={SOURCE: source}, db_path=db_path, feed_loader=lambda *_: snapshot,
        )
        with closing(db.get_connection(db_path=db_path)) as conn:
            if guid not in db.get_seen_guids(conn, SOURCE):
                raise RuntimeError("İlk tarama seçilen sürümü kaydedemedi.")
            conn.execute("DELETE FROM seen_entries WHERE source = ? AND guid = ?", (SOURCE, guid))
            conn.commit()

        # Disable initial sync here, including when the feed contained only one release.
        app.run_scan_cycle(
            config={SOURCE: source}, db_path=db_path,
            feed_loader=lambda *_: SimpleNamespace(entries=[selected]), initialize_empty=False,
        )
        with closing(db.get_connection(db_path=db_path)) as conn:
            if guid not in db.get_seen_guids(conn, SOURCE):
                raise RuntimeError("Özetleme veya Telegram gönderimi tamamlanamadı; logları inceleyin.")

    logging.info("Gönderim başarılı. Geçici veritabanı temizlendi.")


def main():
    try:
        run_test()
    except KeyboardInterrupt:
        logging.error("Test iptal edildi; varsa geçici veritabanı temizlendi.")
        return 130
    except Exception as error:
        logging.error("Test başarısız: %s. Varsa geçici veritabanı temizlendi.", error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
