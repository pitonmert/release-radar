import json

import feedparser
import pytest

from release_radar import database as db
from release_radar import config as settings, feeds, summarizer as summaries, telegram, service
from release_radar.messages import ReleaseSummary


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


def _setup(monkeypatch, tmp_path, feed_entries, telegram_result=True, source_type="github_releases"):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"TestSource": {"rss": "http://example.com/feed", "type": source_type}})
    )
    monkeypatch.setattr(settings, "CONFIG_FILE", str(config_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))

    monkeypatch.setattr(feeds, "fetch_feed_with_retry", lambda url, name: FakeFeed(feed_entries))
    monkeypatch.setattr(summaries, "process_ai", lambda text: ReleaseSummary("AI summary"))

    telegram_calls = []

    def fake_send_telegram(message):
        telegram_calls.append(message)
        return telegram_result

    monkeypatch.setattr(telegram, "send_telegram_message", fake_send_telegram)
    return telegram_calls


def _seen_guids(tmp_path, source="TestSource"):
    conn = db.get_connection(db_path=str(tmp_path / "test.db"))
    guids = db.get_seen_guids(conn, source)
    conn.close()
    return guids


@pytest.mark.parametrize("source_type", ["github_releases", "rss"])
def test_first_run_backfills_without_notifying(monkeypatch, tmp_path, source_type):
    entries = [_entry(f"guid-{i}") for i in range(130)]
    telegram_calls = _setup(monkeypatch, tmp_path, entries, source_type=source_type)

    service.run_scan_cycle()
    service.run_scan_cycle()

    assert telegram_calls == []
    assert _seen_guids(tmp_path) == {f"guid-{i}" for i in range(130)}


@pytest.mark.parametrize("source_type", ["github_releases", "rss"])
def test_new_entry_recorded_only_on_telegram_success(monkeypatch, tmp_path, source_type):
    conn = db.get_connection(db_path=str(tmp_path / "test.db"))
    db.mark_seen(conn, "TestSource", ["old-1"])
    conn.close()

    entries = [_entry("old-1"), _entry("new-1")]
    telegram_calls = _setup(monkeypatch, tmp_path, entries, source_type=source_type)

    service.run_scan_cycle()

    assert len(telegram_calls) == 1
    assert "new-1" in _seen_guids(tmp_path)


@pytest.mark.parametrize("source_type", ["github_releases", "rss"])
def test_new_entry_not_recorded_when_telegram_fails(monkeypatch, tmp_path, source_type):
    conn = db.get_connection(db_path=str(tmp_path / "test.db"))
    db.mark_seen(conn, "TestSource", ["old-1"])
    conn.close()

    entries = [_entry("old-1"), _entry("new-1")]
    telegram_calls = _setup(
        monkeypatch, tmp_path, entries, telegram_result=False, source_type=source_type
    )

    service.run_scan_cycle()

    assert len(telegram_calls) == 1
    assert "new-1" not in _seen_guids(tmp_path)

    monkeypatch.setattr(telegram, "send_telegram_message", lambda message: True)
    service.run_scan_cycle()
    assert "new-1" in _seen_guids(tmp_path)


@pytest.mark.parametrize("source_type", ["github_releases", "rss"])
def test_second_cycle_does_not_renotify_same_entry(monkeypatch, tmp_path, source_type):
    conn = db.get_connection(db_path=str(tmp_path / "test.db"))
    db.mark_seen(conn, "TestSource", ["old-1"])
    conn.close()

    entries = [_entry("old-1"), _entry("new-1")]
    telegram_calls = _setup(monkeypatch, tmp_path, entries, source_type=source_type)

    service.run_scan_cycle()
    assert len(telegram_calls) == 1

    telegram_calls.clear()
    entries[1]["content"] = [{"value": "Updated release notes"}]
    service.run_scan_cycle()

    assert telegram_calls == []


def _seed_history(tmp_path):
    conn = db.get_connection(db_path=str(tmp_path / "test.db"))
    db.mark_seen(conn, "TestSource", ["old-1"])
    conn.close()


def _rss_entries(body):
    return feedparser.parse(
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        f"<channel><title>Test feed</title>{body}</channel></rss>"
    ).entries


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "<description>Short summary</description>"
            "<content:encoded><![CDATA[<h2>Changes</h2><p>Full details</p>]]></content:encoded>",
            "Changes\nFull details",
        ),
        (
            "<description>Short summary</description>"
            "<content:encoded><![CDATA[# Announcement\n\nUse `codex app`.]]></content:encoded>",
            "# Announcement\n\nUse `codex app`.",
        ),
        ("<description><![CDATA[<p>Description only</p>]]></description>", "Description only"),
        (
            "<content:encoded></content:encoded><description>Fallback</description>",
            "Fallback",
        ),
    ],
)
def test_rss_extracts_full_content_or_description(monkeypatch, tmp_path, body, expected):
    entries = _rss_entries(
        "<item><guid>new-1</guid><title>Announcement</title>"
        f"<link>https://example.com/announcement</link>{body}</item>"
    )
    telegram_calls = _setup(monkeypatch, tmp_path, entries, source_type="rss")
    _seed_history(tmp_path)
    inputs = []
    monkeypatch.setattr(summaries, "process_ai", lambda text: inputs.append(text) or ReleaseSummary("Summary"))

    service.run_scan_cycle()

    assert inputs == [f"Title: Announcement\n\n{expected}"]
    assert len(telegram_calls) == 1
    assert telegram_calls[0][0].reply_markup["inline_keyboard"][0][0]["url"] == (
        "https://example.com/announcement"
    )


@pytest.mark.parametrize("content_type", ["text/plain", "text/markdown"])
def test_rss_preserves_plain_text_and_markdown(monkeypatch, tmp_path, content_type):
    entry = _entry("new-1", content="Use `codex <prompt>` and List<T>.")
    entry["content"][0]["type"] = content_type
    _setup(monkeypatch, tmp_path, [entry], source_type="rss")
    _seed_history(tmp_path)
    inputs = []
    monkeypatch.setattr(summaries, "process_ai", lambda text: inputs.append(text) or ReleaseSummary("Summary"))

    service.run_scan_cycle()

    assert "Use `codex <prompt>` and List<T>." in inputs[0]


@pytest.mark.parametrize("missing_date", [False, True])
def test_rss_orders_by_date_or_reverse_feed_order(monkeypatch, tmp_path, missing_date):
    items = []
    for day in (18, 16, 17):
        date = f"<pubDate>{day} Sep 2026 00:00:00 GMT</pubDate>"
        if missing_date and day == 16:
            date = ""
        items.append(
            f"<item><guid>day-{day}</guid><title>Day {day}</title>"
            f"{date}<description>Details</description></item>"
        )
    entries = _rss_entries("".join(items))
    _setup(monkeypatch, tmp_path, entries, source_type="rss")
    _seed_history(tmp_path)
    inputs = []
    monkeypatch.setattr(summaries, "process_ai", lambda text: inputs.append(text) or ReleaseSummary("Summary"))

    service.run_scan_cycle()

    expected_days = [17, 16, 18] if missing_date else [16, 17, 18]
    assert [text.splitlines()[0] for text in inputs] == [
        f"Title: Day {day}" for day in expected_days
    ]


def test_rss_does_not_filter_titles_or_categories(monkeypatch, tmp_path):
    entries = _rss_entries("".join(
        f"<item><guid>{category}</guid><title>{title}</title>"
        f"<category>{category}</category><description>Details</description></item>"
        for category, title in (
            ("general", "Insiders announcement"),
            ("codex-app", "App beta"),
            ("codex-mobile", "Mobile alpha"),
            ("codex-cli", "CLI release candidate rc.1"),
        )
    ))
    telegram_calls = _setup(monkeypatch, tmp_path, entries, source_type="rss")
    _seed_history(tmp_path)

    service.run_scan_cycle()

    assert len(telegram_calls) == 4


def test_github_still_skips_insiders(monkeypatch, tmp_path):
    entries = [_entry("new-1", title="Insiders release")]
    telegram_calls = _setup(monkeypatch, tmp_path, entries)
    _seed_history(tmp_path)

    service.run_scan_cycle()

    assert telegram_calls == []
    assert "new-1" in _seen_guids(tmp_path)


def test_rss_retries_failed_summary(monkeypatch, tmp_path):
    telegram_calls = _setup(monkeypatch, tmp_path, [_entry("new-1")], source_type="rss")
    _seed_history(tmp_path)
    outcomes = iter([None, ReleaseSummary("Summary")])
    monkeypatch.setattr(summaries, "process_ai", lambda text: next(outcomes))

    service.run_scan_cycle()
    assert telegram_calls == []
    assert "new-1" not in _seen_guids(tmp_path)

    service.run_scan_cycle()
    assert len(telegram_calls) == 1
    assert "new-1" in _seen_guids(tmp_path)


@pytest.mark.parametrize("content", ["", "<p> </p>"])
def test_rss_empty_content_is_not_marked_seen(monkeypatch, tmp_path, content):
    telegram_calls = _setup(
        monkeypatch, tmp_path, [_entry("new-1", content=content)], source_type="rss"
    )
    _seed_history(tmp_path)

    service.run_scan_cycle()

    assert telegram_calls == []
    assert "new-1" not in _seen_guids(tmp_path)


def test_rss_limits_content_after_html_cleanup(monkeypatch, tmp_path):
    entry = _entry("new-1", content="<p>" + "x" * (feeds.MAX_INPUT_CHARS + 10) + "</p>")
    _setup(monkeypatch, tmp_path, [entry], source_type="rss")
    _seed_history(tmp_path)
    inputs = []
    monkeypatch.setattr(summaries, "process_ai", lambda text: inputs.append(text) or ReleaseSummary("Summary"))

    service.run_scan_cycle()

    assert inputs == [
        "Title: Test Release\n\n" + "x" * feeds.MAX_INPUT_CHARS + "\n\n[...text truncated...]"
    ]


def test_vscode_uses_markdown_for_summary_and_public_page_for_button(monkeypatch, tmp_path):
    entry = _entry("new-1", title="September 2026")
    entry["link"] = "https://code.visualstudio.com/updates/v1_138"
    entry["published_parsed"] = (2026, 9, 19, 0, 0, 0, 0, 0, 0)
    calls = _setup(monkeypatch, tmp_path, [entry], source_type="vscode_github")
    _seed_history(tmp_path)
    fetched = []
    inputs = []

    def fetch(link):
        fetched.append(link)
        return "# Full markdown notes", "https://raw.githubusercontent.com/test/v1_138.md"

    monkeypatch.setattr(feeds, "fetch_vscode_markdown", fetch)
    monkeypatch.setattr(summaries, "process_ai", lambda text: inputs.append(text) or ReleaseSummary("Özet"))
    service.run_scan_cycle()

    assert fetched == [entry["link"]]
    assert inputs == ["Title: September 2026\n\n# Full markdown notes"]
    assert calls[0][0].reply_markup["inline_keyboard"][0][0]["url"] == entry["link"]
    assert "19.09.2026" in calls[0][0].text
    assert "new-1" in _seen_guids(tmp_path)


def test_partial_delivery_keeps_entry_unseen(monkeypatch, tmp_path):
    sender = telegram.send_telegram_message
    _setup(monkeypatch, tmp_path, [_entry("new-1")], source_type="rss")
    _seed_history(tmp_path)
    monkeypatch.setattr(summaries, "process_ai", lambda text: ReleaseSummary("x" * 12000))
    # Restore the real multi-part sender while keeping every network call mocked.
    monkeypatch.setattr(telegram, "send_telegram_message", sender)
    outcomes = iter([True, False])
    sent = []
    monkeypatch.setattr(
        telegram, "_send_single_telegram", lambda message: sent.append(message) or next(outcomes)
    )
    monkeypatch.setattr(telegram.time, "sleep", lambda _: None)
    service.run_scan_cycle()
    assert len(sent) == 2
    assert "new-1" not in _seen_guids(tmp_path)


def test_render_failure_does_not_block_other_entries(monkeypatch, tmp_path):
    entries = [_entry("new-1", title="x" * 4096), _entry("new-2")]
    calls = _setup(monkeypatch, tmp_path, entries, source_type="rss")
    _seed_history(tmp_path)
    service.run_scan_cycle()
    assert len(calls) == 1
    assert "new-1" not in _seen_guids(tmp_path)
    assert "new-2" in _seen_guids(tmp_path)
