import main


def test_load_json_file_missing_file_returns_default(tmp_path):
    result = main.load_json_file(str(tmp_path / "missing.json"), {"default": True})
    assert result == {"default": True}


def test_load_json_file_invalid_json_returns_default(tmp_path):
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("{not valid json")
    result = main.load_json_file(str(bad_file), {"default": True})
    assert result == {"default": True}


def test_send_telegram_message_chunks_long_text(monkeypatch):
    sent_chunks = []

    def fake_send_single(text):
        sent_chunks.append(text)
        return True

    monkeypatch.setattr(main, "_send_single_telegram", fake_send_single)
    monkeypatch.setattr(main.time, "sleep", lambda _: None)

    long_message = "\n".join(f"line {i}" for i in range(2000))
    assert len(long_message) > main.TELEGRAM_MAX_CHARS

    result = main.send_telegram_message(long_message)

    assert result is True
    assert len(sent_chunks) > 1
    assert all(len(chunk) <= main.TELEGRAM_MAX_CHARS for chunk in sent_chunks)
