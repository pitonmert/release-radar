import json

import db
import main


class FakeFeed:
    def __init__(self, entries):
        self.entries = entries


def _entry(guid, title="Test Release", content="<p>Details</p>"):
    return {
        "id": guid,
        "link": f"https://example.com/{guid}",
        "title": title,
        "content": [{"value": content}],
    }


def _setup(monkeypatch, tmp_path, feed_entries, telegram_result=True):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"TestSource": {"rss": "http://example.com/feed", "type": "github_releases"}})
    )
    monkeypatch.setattr(main, "CONFIG_FILE", str(config_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))

    monkeypatch.setattr(main, "fetch_feed_with_retry", lambda url, name: FakeFeed(feed_entries))
    monkeypatch.setattr(main, "process_ai", lambda text: "AI summary")

    telegram_calls = []

    def fake_send_telegram(message):
        telegram_calls.append(message)
        return telegram_result

    monkeypatch.setattr(main, "send_telegram_message", fake_send_telegram)
    return telegram_calls


def _seen_guids(tmp_path, source="TestSource"):
    conn = db.get_connection(db_path=str(tmp_path / "test.db"))
    guids = db.get_seen_guids(conn, source)
    conn.close()
    return guids


def test_first_run_backfills_without_notifying(monkeypatch, tmp_path):
    entries = [_entry("guid-1"), _entry("guid-2"), _entry("guid-3")]
    telegram_calls = _setup(monkeypatch, tmp_path, entries)

    main.run_scan_cycle()

    assert telegram_calls == []
    assert _seen_guids(tmp_path) == {"guid-1", "guid-2", "guid-3"}


def test_new_entry_recorded_only_on_telegram_success(monkeypatch, tmp_path):
    conn = db.get_connection(db_path=str(tmp_path / "test.db"))
    db.mark_seen(conn, "TestSource", ["old-1"])
    conn.close()

    entries = [_entry("old-1"), _entry("new-1")]
    telegram_calls = _setup(monkeypatch, tmp_path, entries, telegram_result=True)

    main.run_scan_cycle()

    assert len(telegram_calls) == 1
    assert "new-1" in _seen_guids(tmp_path)


def test_new_entry_not_recorded_when_telegram_fails(monkeypatch, tmp_path):
    conn = db.get_connection(db_path=str(tmp_path / "test.db"))
    db.mark_seen(conn, "TestSource", ["old-1"])
    conn.close()

    entries = [_entry("old-1"), _entry("new-1")]
    telegram_calls = _setup(monkeypatch, tmp_path, entries, telegram_result=False)

    main.run_scan_cycle()

    assert len(telegram_calls) == 1
    assert "new-1" not in _seen_guids(tmp_path)


def test_second_cycle_does_not_renotify_same_entry(monkeypatch, tmp_path):
    conn = db.get_connection(db_path=str(tmp_path / "test.db"))
    db.mark_seen(conn, "TestSource", ["old-1"])
    conn.close()

    entries = [_entry("old-1"), _entry("new-1")]
    telegram_calls = _setup(monkeypatch, tmp_path, entries, telegram_result=True)

    main.run_scan_cycle()
    assert len(telegram_calls) == 1

    telegram_calls.clear()
    main.run_scan_cycle()

    assert telegram_calls == []
