import json
from types import SimpleNamespace

import pytest

import main
from telegram_messages import ReleaseSummary, TelegramMessage, build_messages


def test_load_json_file_missing_file_returns_default(tmp_path):
    result = main.load_json_file(str(tmp_path / "missing.json"), {"default": True})
    assert result == {"default": True}


def test_load_json_file_invalid_json_returns_default(tmp_path):
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("{not valid json")
    result = main.load_json_file(str(bad_file), {"default": True})
    assert result == {"default": True}


def test_send_telegram_message_sends_prepared_parts_in_order(monkeypatch):
    sent_chunks = []

    def fake_send_single(text):
        sent_chunks.append(text)
        return True

    monkeypatch.setattr(main, "_send_single_telegram", fake_send_single)
    monkeypatch.setattr(main.time, "sleep", lambda _: None)

    messages = build_messages(
        "Claude Code", "v1.2.3", "https://example.com/release",
        ReleaseSummary("Özet", features=tuple(f"Özellik {i}" for i in range(2000))),
    )
    result = main.send_telegram_message(messages)

    assert result is True
    assert len(sent_chunks) > 1
    assert sent_chunks == messages


def test_send_stops_on_failed_part(monkeypatch):
    sent = []
    outcomes = iter([True, False])
    monkeypatch.setattr(
        main, "_send_single_telegram", lambda message: sent.append(message) or next(outcomes)
    )
    monkeypatch.setattr(main.time, "sleep", lambda _: None)
    messages = [TelegramMessage(str(i), None) for i in range(3)]
    assert main.send_telegram_message(messages) is False
    assert sent == messages[:2]
    assert main.send_telegram_message([]) is False


def test_telegram_request_contains_html_and_button(monkeypatch):
    requests = []
    monkeypatch.setattr(main, "TELEGRAM_TOKEN", "test-token")
    monkeypatch.setattr(main, "TELEGRAM_CHAT_ID", "test-chat")

    def post(url, **kwargs):
        requests.append((url, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: {"ok": True})

    monkeypatch.setattr(main.requests, "post", post)
    message = build_messages("VS Code", "v1", "https://example.com/v1", ReleaseSummary("Özet"))[0]
    assert main._send_single_telegram(message) is True
    url, kwargs = requests[0]
    assert url.endswith("/sendMessage")
    assert kwargs["json"] == {
        "chat_id": "test-chat", "text": message.text, "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True}, "reply_markup": message.reply_markup,
    }
    assert kwargs["timeout"] == 10


@pytest.mark.parametrize("failure", ["http", "api", "connection"])
def test_telegram_retries_failures(monkeypatch, failure):
    calls = []
    sleeps = []

    def post(*args, **kwargs):
        calls.append(kwargs)
        if failure == "connection":
            raise main.requests.ConnectionError("Test failure")
        return SimpleNamespace(
            status_code=503 if failure == "http" else 200,
            text="Test failure", json=lambda: {"ok": False},
        )

    monkeypatch.setattr(main.requests, "post", post)
    monkeypatch.setattr(main.time, "sleep", sleeps.append)
    assert main._send_single_telegram(TelegramMessage("Özet", None)) is False
    assert len(calls) == main.MAX_TELEGRAM_RETRIES
    assert sleeps == [2, 4]
    assert "reply_markup" not in calls[0]["json"]


def _fake_ai(monkeypatch, response_text):
    calls = []

    def generate_content(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(text=response_text)

    monkeypatch.setattr(
        main, "ai_client", SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    )
    return calls


def test_process_ai_uses_schema_and_preserves_code(monkeypatch):
    code = '  codex exec "<prompt>"\n'
    response = json.dumps({
        "overview": "Türkçe özet", "critical": [], "features": ["Yeni özellik"],
        "fixes": [], "changes": [], "code_examples": [code],
    })
    calls = _fake_ai(monkeypatch, response)
    result = main.process_ai("Kaynak:\n" + code)
    assert result.overview == "Türkçe özet"
    assert result.code_examples == (code,)
    assert calls[0]["model"] == "gemini-flash-latest"
    assert calls[0]["config"].response_mime_type == "application/json"
    assert calls[0]["config"].response_json_schema == main.SUMMARY_SCHEMA


@pytest.mark.parametrize("response", [
    None, "", "Not JSON", "{}", '{"overview": "Missing lists"}',
    json.dumps({"overview": " ", "critical": [], "features": [], "fixes": [], "changes": []}),
    json.dumps({"overview": "Özet", "critical": [], "features": "Wrong type", "fixes": [], "changes": []}),
    json.dumps({"overview": "Özet", "critical": [], "features": [], "fixes": [], "changes": [],
                "code_examples": ["invented command"]}),
])
def test_process_ai_rejects_invalid_or_invented_content(monkeypatch, response):
    _fake_ai(monkeypatch, response)
    assert main.process_ai("Original source") is None


@pytest.mark.parametrize(("entry", "expected"), [
    ({"published_parsed": (2026, 9, 19, 0, 0, 0, 0, 0, 0)}, "19.09.2026"),
    ({}, None),
    ({"updated_parsed": (2026, 9, 19, 0, 0, 0, 0, 0, 0)}, None),
    ({"published_parsed": (2026, 13, 40)}, None),
])
def test_publication_date_does_not_invent_a_date(entry, expected):
    assert main.entry_publication_date(entry) == expected
