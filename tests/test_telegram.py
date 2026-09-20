from types import SimpleNamespace

import pytest

from release_radar import config as settings, telegram
from release_radar.messages import ReleaseSummary, TelegramMessage, build_messages


def test_send_telegram_message_sends_prepared_parts_in_order(monkeypatch):
    sent_chunks = []

    def fake_send_single(text):
        sent_chunks.append(text)
        return True

    monkeypatch.setattr(telegram, "_send_single_telegram", fake_send_single)
    monkeypatch.setattr(telegram.time, "sleep", lambda _: None)

    messages = build_messages(
        "Claude Code", "v1.2.3", "https://example.com/release",
        ReleaseSummary("Özet", features=tuple(f"Özellik {i}" for i in range(2000))),
    )
    result = telegram.send_telegram_message(messages)

    assert result is True
    assert len(sent_chunks) > 1
    assert sent_chunks == messages


def test_send_stops_on_failed_part(monkeypatch):
    sent = []
    outcomes = iter([True, False])
    monkeypatch.setattr(
        telegram, "_send_single_telegram", lambda message: sent.append(message) or next(outcomes)
    )
    monkeypatch.setattr(telegram.time, "sleep", lambda _: None)
    messages = [TelegramMessage(str(i), None) for i in range(3)]
    assert telegram.send_telegram_message(messages) is False
    assert sent == messages[:2]
    assert telegram.send_telegram_message([]) is False


def test_telegram_request_contains_html_and_button(monkeypatch):
    requests = []
    monkeypatch.setattr(settings, "TELEGRAM_TOKEN", "test-token")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "test-chat")

    def post(url, **kwargs):
        requests.append((url, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: {"ok": True})

    monkeypatch.setattr(telegram.requests, "post", post)
    message = build_messages("VS Code", "v1", "https://example.com/v1", ReleaseSummary("Özet"))[0]
    assert telegram._send_single_telegram(message) is True
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
            raise telegram.requests.ConnectionError("Test failure")
        return SimpleNamespace(
            status_code=503 if failure == "http" else 200,
            text="Test failure", json=lambda: {"ok": False},
        )

    monkeypatch.setattr(telegram.requests, "post", post)
    monkeypatch.setattr(telegram.time, "sleep", sleeps.append)
    assert telegram._send_single_telegram(TelegramMessage("Özet", None)) is False
    assert len(calls) == telegram.MAX_TELEGRAM_RETRIES
    assert sleeps == [2, 4]
    assert "reply_markup" not in calls[0]["json"]
