from contextlib import closing
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from release_radar import database as db
from release_radar import service
from release_radar.pagination import Pager
from release_radar.publisher import Publisher
from release_radar.telegram import TelegramClient, TelegramError
from release_radar.messages import ReleaseSummary


SOURCES = ["Claude Code", "VS Code", "ChatGPT & Codex"]


class FakeTelegram:
    chat_id = "123"

    def __init__(self):
        self.sent = []
        self.edited = []
        self.calls = []
        self.updates = []
        self.fail_send = False

    def send(self, text, keyboard):
        if self.fail_send:
            raise TelegramError(503, "Unavailable")
        self.sent.append((text, keyboard))
        return len(self.sent)

    def edit(self, message_id, text, keyboard):
        self.edited.append((message_id, text, keyboard))

    def call(self, method, **payload):
        self.calls.append((method, payload))
        return self.updates if method == "getUpdates" else True


@pytest.fixture
def system(tmp_path):
    client = FakeTelegram()
    menu = Pager(client, str(tmp_path / "test.db"), SOURCES)
    calls = []

    def summarize(text):
        calls.append(text)
        return ReleaseSummary(features=("Özellik " * 1800,))

    return SimpleNamespace(client=client, menu=menu, calls=calls,
                           publisher=Publisher(menu, summarize, "test-model"))


def update(guid="one"):
    return {"guid": guid, "title": "v2.1.278", "url": "https://example.com/release",
            "text": "Source notes", "published_date": "19.09.2026"}


def callback(system, action, target="", page=0, message_id=1, chat_id=123):
    return {"update_id": 7, "callback_query": {
        "id": "click", "data": system.menu.button("test", action, target, page)["callback_data"],
        "message": {"chat": {"id": chat_id}, "message_id": message_id},
    }}


def release(system):
    with closing(db.get_connection(system.menu.db_path)) as conn:
        release_id = conn.execute("SELECT id FROM releases ORDER BY rowid LIMIT 1").fetchone()[0]
        return db.get_release(conn, release_id)


@pytest.mark.parametrize("source", SOURCES)
def test_auto_summary_is_sent_directly_and_navigation_never_generates(system, source):
    assert system.publisher(source, update())
    saved = release(system)
    assert saved["model"] == "test-model"
    assert saved["source_text"] == "Source notes"
    assert saved["notified"] == 1
    assert len(json.loads(saved["pages_json"])) > 1
    assert "Yeni güncelleme hazır" not in system.client.sent[0][0]
    assert "Özellik" in system.client.sent[0][0]
    for action, page in [("open", 0), ("open", 1), ("open", 1), ("open", 0)]:
        system.menu.handle(callback(system, action, saved["id"], page))
    assert len(system.client.sent) == 1
    assert len(system.client.edited) == 4
    assert system.calls == ["Source notes"]
    assert " · 2/" in system.client.edited[1][1]


def test_failed_delivery_retries_cached_summary_even_when_feed_disappears(system):
    system.client.fail_send = True
    assert not system.publisher(SOURCES[0], update())
    assert release(system)["notified"] == 0
    assert release(system)["pages_json"]
    system.client.fail_send = False
    service.run_scan_cycle(config={SOURCES[0]: {"rss": "x", "type": "rss"}},
                        db_path=system.menu.db_path, publisher=system.publisher,
                        feed_loader=lambda *_: None)
    assert release(system)["notified"] == 1
    assert len(system.calls) == 1


def test_failed_summary_is_not_notified_or_regenerated_twice_in_one_cycle(system):
    system.publisher.summarizer = lambda _: None
    assert not system.publisher(SOURCES[0], update())
    assert not system.publisher(SOURCES[0], update())
    assert not system.client.sent
    assert release(system)["pages_json"] is None
    system.publisher.summarizer = lambda _: ReleaseSummary("OK")
    system.publisher.retry_pending()
    assert release(system)["notified"] == 1


def test_restart_preserves_callbacks_cache_and_update_offset(system):
    system.publisher(SOURCES[0], update())
    saved = release(system)
    system.client.updates = [callback(system, "open", saved["id"])]
    system.menu.poll_once()
    restored = Pager(system.client, system.menu.db_path, SOURCES)
    assert restored.namespace == system.menu.namespace
    system.client.updates = []
    restored.poll_once()
    assert system.client.calls[-1][1]["offset"] == 8
    restored.handle(callback(system, "open", saved["id"]))
    assert len(system.calls) == 1


def test_legacy_migration_preserves_seen_without_backfilling(tmp_path):
    path = str(tmp_path / "old.db")
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE seen_entries (source TEXT, guid TEXT, seen_at TEXT, PRIMARY KEY(source,guid))")
        conn.execute("INSERT INTO seen_entries VALUES ('Claude Code', 'old', '2026-01-01')")
        conn.commit()
    with closing(db.get_connection(path)) as conn:
        assert db.get_seen_guids(conn, "Claude Code") == {"old"}
        assert conn.execute("SELECT count(*) FROM releases").fetchone()[0] == 0


def test_first_scan_silently_baselines_all_sources(system):
    service.run_scan_cycle(
        config={name: {"rss": "x", "type": "rss"} for name in SOURCES},
        db_path=system.menu.db_path, publisher=system.publisher,
        feed_loader=lambda *_: SimpleNamespace(entries=[{"id": "old"}]),
    )
    assert not system.calls and not system.client.sent
    with closing(db.get_connection(system.menu.db_path)) as conn:
        assert conn.execute("SELECT count(*) FROM seen_entries").fetchone()[0] == 3
        assert conn.execute("SELECT count(*) FROM releases").fetchone()[0] == 0


def test_new_scan_uses_publisher_and_does_not_send_legacy_parts(system):
    config = {SOURCES[0]: {"rss": "x", "type": "github_releases"}}
    with closing(db.get_connection(system.menu.db_path)) as conn:
        db.mark_seen(conn, SOURCES[0], ["old"])
    feed = SimpleNamespace(entries=[{"id": "new", "title": "v1.0", "link": "https://example.com",
                                     "content": [{"value": "New release"}]}])
    for _ in range(2):
        service.run_scan_cycle(config=config, db_path=system.menu.db_path,
                            publisher=system.publisher, feed_loader=lambda *_: feed)
    assert len(system.client.sent) == 1 and len(system.calls) == 1


@pytest.mark.parametrize("command", ["/start", "/menu"])
def test_menu_commands_do_not_send_messages(system, command):
    system.menu.handle({"message": {"chat": {"id": 123}, "text": command}})
    assert not system.client.sent


def test_single_page_only_has_official_link(system):
    system.publisher.summarizer = lambda _: ReleaseSummary("Kısa özet.")
    system.publisher(SOURCES[0], update())
    text, keyboard = system.client.sent[0]
    assert "Kısa özet." in text
    assert " · 1/" not in text
    assert keyboard == {"inline_keyboard": [[{
        "text": "Resmî notları aç", "url": "https://example.com/release",
    }]]}


@pytest.mark.parametrize("invalid", ["chat", "message", "namespace", "data", "page", "target"])
def test_invalid_callbacks_cannot_edit_messages(system, invalid):
    system.publisher(SOURCES[0], update())
    event = callback(system, "open", release(system)["id"])
    query = event["callback_query"]
    if invalid == "chat":
        query["message"]["chat"]["id"] = 999
    elif invalid == "message":
        query["message"]["message_id"] = 999
    elif invalid == "namespace":
        query["data"] = query["data"].replace(system.menu.namespace, "expired")
    elif invalid == "data":
        query["data"] = "not-a-callback"
    elif invalid == "page":
        query["data"] = system.menu.button("x", "open", release(system)["id"], 999)["callback_data"]
    else:
        query["data"] = system.menu.button("x", "open", "missing")["callback_data"]
    system.menu.handle(event)
    assert not system.client.edited
    assert system.client.calls[-1][0] == "answerCallbackQuery"


def test_cleanup_removes_test_buttons_but_keeps_source_link(system):
    system.publisher(SOURCES[0], update())
    system.menu.handle({"message": {"chat": {"id": 123}, "text": "/menu"}})
    system.menu.finish_test()
    assert len(system.client.edited) == 1
    for _, text, keyboard in system.client.edited:
        assert "test sona erdi" in text
        assert all("callback_data" not in button for row in keyboard["inline_keyboard"] for button in row)


@pytest.mark.parametrize("action", ["home", "card", "list"])
def test_old_menu_actions_are_rejected(system, action):
    system.publisher(SOURCES[0], update())
    system.menu.handle(callback(system, action, release(system)["id"]))
    assert not system.client.edited


def test_message_cannot_navigate_to_another_release(system):
    system.publisher(SOURCES[0], update("first"))
    system.publisher(SOURCES[0], update("second"))
    system.menu.handle(callback(system, "open", release(system)["id"], message_id=2))
    assert not system.client.edited


def test_telegram_rate_limit_and_not_modified(monkeypatch):
    class Stop:
        def __init__(self):
            self.waits = []

        def wait(self, delay):
            self.waits.append(delay)
            return False

    stop = Stop()
    client = TelegramClient("secret", 123, stop)
    replies = iter([
        {"ok": False, "error_code": 429, "parameters": {"retry_after": 7}},
        {"ok": True, "result": {"message_id": 1}},
        {"ok": False, "error_code": 400, "description": "Bad Request: message is not modified"},
    ])
    monkeypatch.setattr("release_radar.telegram.requests.post", lambda *a, **k: SimpleNamespace(
        status_code=400, json=lambda: next(replies)))
    assert client.send("test", {"inline_keyboard": []}) == 1
    assert stop.waits == [7]
    assert client.edit(1, "test", {"inline_keyboard": []}) is True


def test_poll_conflict_stops_without_changing_settings(system):
    def conflict(*args, **kwargs):
        raise TelegramError(409, "Another consumer")
    system.client.call = conflict
    stop = threading.Event()
    with pytest.raises(RuntimeError, match="conflict"):
        system.menu.listen(stop, seconds=1)
    assert stop.is_set()


def test_webhook_preflight_does_not_delete_it(monkeypatch):
    client = TelegramClient("secret", 123)
    calls = []
    monkeypatch.setattr(client, "call", lambda method, **kw: calls.append(method) or {"url": "active"})
    with pytest.raises(RuntimeError, match="Webhook"):
        client.preflight()
    assert calls == ["getWebhookInfo"]


def test_oversized_page_number_is_rejected_before_sql(system):
    system.publisher(SOURCES[0], update())
    event = callback(system, "open", release(system)["id"], page=10**12)
    system.menu.handle(event)
    assert not system.client.edited


def test_removed_source_pending_summary_is_not_generated(system):
    with closing(db.get_connection(system.menu.db_path)) as conn:
        db.save_release(conn, "Removed source", update())
    system.publisher.retry_pending()
    assert not system.calls and not system.client.sent


def test_broken_summary_does_not_block_other_pending_records(system):
    with closing(db.get_connection(system.menu.db_path)) as conn:
        db.save_release(conn, SOURCES[0], update("broken"))
        other = update("working")
        other["text"] = "Working notes"
        db.save_release(conn, SOURCES[0], other)

    def summarize(text):
        if text == "Source notes":
            raise RuntimeError("Unexpected failure")
        return ReleaseSummary("OK")

    system.publisher.summarizer = summarize
    system.publisher.retry_pending()
    assert len(system.client.sent) == 1
    with closing(db.get_connection(system.menu.db_path)) as conn:
        assert conn.execute("SELECT notified FROM releases WHERE guid='broken'").fetchone()[0] == 0
        assert conn.execute("SELECT notified FROM releases WHERE guid='working'").fetchone()[0] == 1


def test_local_poll_lock_rejects_second_consumer():
    first = TelegramClient("unit-test-lock-token", 123)
    second = TelegramClient("unit-test-lock-token", 123)
    with first.poll_lock():
        with pytest.raises(RuntimeError, match="başka bir local"):
            with second.poll_lock():
                pytest.fail("Second consumer acquired the lock")
    with second.poll_lock():
        pass


def test_pagination_remains_responsive_during_summary_generation(system):
    system.publisher(SOURCES[0], update())
    saved = release(system)
    entered = threading.Event()
    finish = threading.Event()

    def slow_summary(_):
        entered.set()
        assert finish.wait(5)
        return ReleaseSummary("OK")

    system.publisher.summarizer = slow_summary
    scanner = threading.Thread(target=system.publisher, args=(SOURCES[0], update("second")))
    scanner.start()
    try:
        assert entered.wait(5)
        system.menu.handle(callback(system, "open", saved["id"], 1))
        assert len(system.client.sent) == 1
        assert len(system.client.edited) == 1
        assert " · 2/" in system.client.edited[0][1]
    finally:
        finish.set()
        scanner.join(timeout=5)
    assert not scanner.is_alive()
    assert release(system)["notified"] == 1


def test_unknown_chat_commands_are_ignored(system):
    system.menu.handle({"message": {"chat": {"id": 999}, "text": "/menu"}})
    assert not system.client.sent


def test_cleanup_reports_failure_without_leaving_database_open(system, caplog):
    system.publisher(SOURCES[0], update())

    def fail(*args):
        raise TelegramError(503, "Unavailable")

    system.client.edit = fail
    system.menu.finish_test()
    assert "cleanup failed" in caplog.text
    with closing(db.get_connection(system.menu.db_path)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
