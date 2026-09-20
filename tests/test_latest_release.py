from contextlib import closing
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from release_radar import database as db
from release_radar import config as settings, feeds, summarizer as summaries, telegram, service
from scripts import test_latest_release as replay
from release_radar.messages import ReleaseSummary


def entry(guid, day=1, title=None):
    return {
        "id": guid, "title": title or guid, "link": f"https://example.com/{guid}",
        "content": [{"value": "<p>Release details</p>"}],
        "published_parsed": (2026, 9, day, 0, 0, 0, 0, 0, 0),
    }


@pytest.fixture
def harness(monkeypatch, tmp_path):
    production_db = tmp_path / "production.db"
    with closing(db.get_connection(str(production_db))) as conn:
        db.mark_seen(conn, replay.SOURCE, ["keep-me"])
    original = production_db.read_bytes()
    monkeypatch.setattr(db, "DB_PATH", str(production_db))
    monkeypatch.setenv("DB_PATH", str(production_db))
    config = {
        replay.SOURCE: {"rss": "https://example.com/feed", "type": "github_releases"},
        "Other": {"rss": "https://example.com/other", "type": "rss"},
    }
    monkeypatch.setattr(settings, "load_config", lambda: None)
    monkeypatch.setattr(settings, "load_json_file", lambda *_: config)
    state = SimpleNamespace(
        entries=[entry("older", 1), entry("newest", 19), entry("middle", 10)],
        fetches=[], summaries=[], messages=[], scans=[], models=[],
        summary_result=ReleaseSummary("Özet"), delivery_result=True,
    )

    def fetch(url, source):
        state.fetches.append((url, source))
        return SimpleNamespace(entries=state.entries)

    def summarize(text, *, model):
        state.summaries.append(text)
        state.models.append(model)
        return state.summary_result

    def send(messages):
        state.messages.append(messages)
        return state.delivery_result

    scan = service.run_scan_cycle

    def record_scan(**kwargs):
        state.scans.append(kwargs)
        scan(**kwargs)
        if len(state.scans) == 1:
            assert not state.summaries
            assert not state.messages
            # A later arrival must never be included in the second pass.
            state.entries.append(entry("arrived-during-test", 20))

    monkeypatch.setattr(feeds, "fetch_feed_with_retry", fetch)
    monkeypatch.setattr(summaries, "process_ai", summarize)
    monkeypatch.setattr(telegram, "send_telegram_message", send)
    monkeypatch.setattr(service, "run_scan_cycle", record_scan)
    yield state
    assert production_db.read_bytes() == original
    for kwargs in state.scans:
        path = Path(kwargs["db_path"])
        assert not path.exists()
        assert not path.parent.exists()


@pytest.mark.parametrize("single_entry", [False, True])
def test_replays_only_latest_and_cleans_up(harness, single_entry):
    if single_entry:
        harness.entries = [entry("newest", 19)]
    assert replay.main() == 0
    assert harness.fetches == [("https://example.com/feed", replay.SOURCE)]
    assert len(harness.scans) == 2
    assert all(list(scan["config"]) == [replay.SOURCE] for scan in harness.scans)
    assert harness.summaries == ["Title: newest\n\nRelease details"]
    assert len(harness.messages) == 1
    assert harness.models == ["gemini-3.5-flash-lite"]


def test_explicit_model_is_scoped_to_test(harness):
    assert replay.main(["--model", "gemini-3.8-flash"]) == 0
    assert harness.models == ["gemini-3.8-flash"]
    assert settings.DEFAULT_MODEL == "gemini-3.5-flash-lite"


@pytest.mark.parametrize("arguments", [["--model"], ["--model", "  "], ["--unknown"]])
def test_invalid_arguments_do_not_fetch(harness, arguments):
    with pytest.raises(SystemExit) as error:
        replay.main(arguments)
    assert error.value.code == 2
    assert not harness.fetches


@pytest.mark.parametrize("failure", ["summary", "delivery", "empty_content", "exception"])
def test_failure_returns_nonzero_and_cleans_up(harness, monkeypatch, failure):
    if failure == "summary":
        harness.summary_result = None
    elif failure == "delivery":
        harness.delivery_result = False
    elif failure == "empty_content":
        harness.entries[1]["content"] = []
    else:
        def explode(_, **kwargs):
            raise RuntimeError("API unavailable")
        monkeypatch.setattr(summaries, "process_ai", explode)
    assert replay.main() == 1
    assert len(harness.scans) == 2


def test_ctrl_c_cleans_up(harness, monkeypatch):
    def interrupt(_, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(summaries, "process_ai", interrupt)
    assert replay.main() == 130


def test_baseline_failure_does_not_send(harness, monkeypatch):
    original = service.run_scan_cycle

    def failed_baseline(**kwargs):
        kwargs["feed_loader"] = lambda *_: None
        original(**kwargs)

    monkeypatch.setattr(service, "run_scan_cycle", failed_baseline)
    assert replay.main() == 1
    assert not harness.summaries
    assert not harness.messages


def test_empty_feed_does_not_send(harness):
    harness.entries = []
    assert replay.main() == 1
    assert not harness.scans
    assert not harness.messages


def test_missing_source_does_not_fetch(harness, monkeypatch):
    monkeypatch.setattr(settings, "load_json_file", lambda *_: {})
    assert replay.main() == 1
    assert not harness.fetches


def test_selection_ignores_insiders_and_missing_ids():
    old, latest = entry("old", 1), entry("latest", 19)
    missing_id = {"title": "Missing ID", "published_parsed": (2026, 9, 22)}
    assert replay.latest_entry([
        old, entry("insiders", 20, "Insiders build"), missing_id, latest,
    ]) is latest


def test_selection_prefers_publication_date_to_edit_date():
    old, latest = entry("old", 1), entry("latest", 19)
    old["updated_parsed"] = (2026, 9, 22, 0, 0, 0, 0, 0, 0)
    assert replay.latest_entry([old, latest]) is latest


def test_selection_uses_update_date_for_atom():
    old, latest = entry("old", 1), entry("latest", 19)
    for item in (old, latest):
        item["updated_parsed"] = item.pop("published_parsed")
    assert replay.latest_entry([old, latest]) is latest


def test_missing_dates_use_first_eligible_feed_entry(caplog):
    first, second = entry("first", 1), entry("second", 19)
    del first["published_parsed"]
    assert replay.latest_entry([first, second]) is first
    assert "Eksik tarihler" in caplog.text


def test_no_eligible_entry():
    with pytest.raises(ValueError, match="uygun sürüm"):
        replay.latest_entry([entry("insiders", title="Insiders")])


def test_exact_version_selection(harness):
    assert replay.main(["--version", "older"]) == 0
    assert harness.summaries == ["Title: older\n\nRelease details"]


def test_missing_version_does_not_send(harness):
    assert replay.main(["--version", "missing"]) == 1
    assert not harness.messages and not harness.scans


def test_invalid_interactive_duration(harness):
    with pytest.raises(SystemExit) as error:
        replay.main(["--interactive", "--listen-seconds", "0"])
    assert error.value.code == 2
    assert not harness.fetches


@pytest.mark.parametrize("interrupt", [False, True])
def test_interactive_script_shows_summary_directly_then_cleans_up(harness, monkeypatch, interrupt):
    class Client:
        chat_id = "123"

        def __init__(self):
            self.sent = []
            self.edited = []

        def preflight(self):
            pass

        def poll_lock(self):
            return nullcontext()

        def call(self, method, **kwargs):
            assert method == "getUpdates"
            return []

        def send(self, text, keyboard):
            self.sent.append((text, keyboard))
            return 1

        def edit(self, message_id, text, keyboard):
            self.edited.append((message_id, text, keyboard))

    client = Client()
    monkeypatch.setattr(replay, "TelegramClient", lambda *_: client)
    listened = []

    def listen(menu, stop, seconds, not_before):
        listened.append(seconds)
        assert Path(menu.db_path).exists()
        assert len(client.sent) == 1
        assert "Özet" in client.sent[0][0]
        assert "Yeni güncelleme hazır" not in client.sent[0][0]
        assert len(harness.summaries) == 1
        assert not hasattr(menu, "home")
        if interrupt:
            raise KeyboardInterrupt

    monkeypatch.setattr(replay.Pager, "listen", listen)
    assert replay.main(["--version", "newest", "--interactive", "--listen-seconds", "7"]) == (
        130 if interrupt else 0
    )
    assert listened == [7]
    assert not harness.messages  # legacy multi-message sender was not used
    assert "test sona erdi" in client.edited[0][1]
    assert all("callback_data" not in button
               for row in client.edited[0][2]["inline_keyboard"] for button in row)
