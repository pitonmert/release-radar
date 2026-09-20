

from release_radar import config as settings


def test_load_config_reads_root_env_without_overriding_host_values(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=file-token\nTELEGRAM_CHAT_ID=123\n"
        "GEMINI_API_KEY=file-key\nSCAN_INTERVAL_SECONDS=90\n"
    )
    monkeypatch.setattr(settings, "BASE_DIR", str(tmp_path))
    # Restore both module state and environment after exercising the loader.
    for name in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "GEMINI_KEY", "SCAN_INTERVAL_SECONDS"):
        monkeypatch.setattr(settings, name, getattr(settings, name))
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GEMINI_API_KEY", "SCAN_INTERVAL_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "host-token")
    settings.load_config()
    assert settings.TELEGRAM_TOKEN == "host-token"
    assert settings.TELEGRAM_CHAT_ID == "123"
    assert settings.GEMINI_KEY == "file-key"
    assert settings.SCAN_INTERVAL_SECONDS == 90


def test_load_json_file_missing_file_returns_default(tmp_path):
    result = settings.load_json_file(str(tmp_path / "missing.json"), {"default": True})
    assert result == {"default": True}


def test_load_json_file_invalid_json_returns_default(tmp_path):
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("{not valid json")
    result = settings.load_json_file(str(bad_file), {"default": True})
    assert result == {"default": True}
