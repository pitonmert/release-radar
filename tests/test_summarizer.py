from bs4 import BeautifulSoup
import json
from types import SimpleNamespace

import pytest

from release_radar import summarizer as summaries
from release_radar.messages import build_messages


def _fake_ai(monkeypatch, response_text):
    calls = []

    def generate_content(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(text=response_text)

    monkeypatch.setattr(
        summaries, "ai_client", SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    )
    return calls


def test_client_is_created_from_loaded_configuration_and_reused(monkeypatch):
    from release_radar import config

    monkeypatch.setattr(config, "GEMINI_KEY", "test-key")
    monkeypatch.setattr(summaries, "ai_client", None)
    constructed = []
    client = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **kw: SimpleNamespace(
        text=json.dumps({"overview": "Özet", "critical": [], "features": [],
                         "fixes": [], "changes": []}),
    )))
    monkeypatch.setattr(summaries.genai, "Client",
                        lambda **kw: constructed.append(kw) or client)
    assert summaries.process_ai("Source") is not None
    assert summaries.process_ai("Source") is not None
    assert constructed == [{"api_key": "test-key"}]


def test_process_ai_uses_schema_and_preserves_code(monkeypatch):
    code = '  codex exec "<prompt>"\n'
    response = json.dumps({
        "overview": "Türkçe özet", "critical": [], "features": ["Yeni özellik"],
        "fixes": [], "changes": [], "code_examples": [code],
    })
    calls = _fake_ai(monkeypatch, response)
    result = summaries.process_ai("Kaynak:\n" + code)
    assert result.overview == "Türkçe özet"
    assert result.code_examples == (code,)
    assert calls[0]["model"] == "gemini-3.5-flash-lite"
    assert calls[0]["config"].response_mime_type == "application/json"
    assert calls[0]["config"].response_json_schema == summaries.SUMMARY_SCHEMA


def test_process_ai_model_override_does_not_change_default(monkeypatch):
    response = json.dumps({
        "overview": "Özet", "critical": [], "features": [], "fixes": [], "changes": [],
    })
    calls = _fake_ai(monkeypatch, response)
    assert summaries.process_ai("Source", model="gemini-3.8-flash") is not None
    assert summaries.process_ai("Source") is not None
    assert [call["model"] for call in calls] == ["gemini-3.8-flash", "gemini-3.5-flash-lite"]


def test_environment_model_can_be_overridden_for_one_call(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "configured-model")
    response = json.dumps({"overview": "Özet", "critical": [], "features": [], "fixes": [], "changes": []})
    calls = _fake_ai(monkeypatch, response)
    summaries.process_ai("Source")
    summaries.process_ai("Source", model="test-model")
    summaries.process_ai("Source")
    assert [call["model"] for call in calls] == ["configured-model", "test-model", "configured-model"]


def test_short_claude_release_keeps_context_through_delivery_rendering(monkeypatch):
    # Curated output: checks the contract and renderer, not live model comprehension.
    source = (
        "Changed auto mode for Claude API and Enterprise users, and on Bedrock, Vertex, "
        "Foundry and gateways, to default to the server-side classifier, which does not "
        "charge for classifier overhead (`CLAUDE_CODE_AUTO_MODE_SERVER=0` opts out on "
        "Bedrock, Vertex, Foundry and gateways); warns on billed fallback.\n"
        "Added an `Auto mode server` row to `/status` showing whether this session's "
        "auto mode classifier runs on the server."
    )
    feature = (
        "/status komutuna, oturumun otomatik mod sınıflandırıcısının sunucuda çalışıp "
        "çalışmadığını gösteren Auto mode server satırı eklendi."
    )
    change = (
        "Claude API ve Enterprise kullanıcıları ile Bedrock, Vertex, Foundry ve ağ "
        "geçitlerinde otomatik mod varsayılan olarak sunucu tarafı sınıflandırıcıyı "
        "kullanır; sınıflandırıcı ek yükü için ücret alınmaz. Bedrock, Vertex, Foundry "
        "ve ağ geçitlerinde CLAUDE_CODE_AUTO_MODE_SERVER=0 ortam değişkeniyle bu "
        "davranış devre dışı bırakılabilir. Ücretli yedek yönteme geçilirse uyarı verilir."
    )
    calls = _fake_ai(monkeypatch, json.dumps({
        "critical": [], "features": [feature], "fixes": [], "changes": [change],
    }))
    summary = summaries.process_ai(source)
    assert summary.overview == ""
    assert summary.code_examples == ()
    message = build_messages("Claude Code", "v2.1.278", "", summary)[0]
    visible = BeautifulSoup(message.text, "html.parser").get_text()
    assert feature in visible
    assert change in visible
    assert "<pre>" not in message.text
    assert "Komut / kod örneği" not in message.text
    prompt = calls[0]["contents"]
    for rule in (
        "tüm anlamını çevir", "ücretlendirmeyi, uyarıları", "kodu açıklamasından ayırma",
        "Kısa duyurularda alanı atla", "her kaynak maddesini çıktıyla karşılaştır",
    ):
        assert rule in prompt
    assert source in prompt


@pytest.mark.parametrize("in_source", [True, False])
def test_summary_links_must_come_from_source(monkeypatch, in_source):
    url = "https://example.com/billing"
    _fake_ai(monkeypatch, json.dumps({
        "critical": [], "features": [], "fixes": [],
        "changes": [f"`SETTING=0` ile kapatılır. [Ücretlendirme ayrıntıları]({url})"],
    }))
    summary = summaries.process_ai("SETTING=0 details " + (url if in_source else ""))
    assert (summary is not None) is in_source


@pytest.mark.parametrize("source", [
    "Added a keyboard shortcut for opening the search panel.",
    "\n".join(f"Fixed independent issue {index} in the editor." for index in range(20)),
])
def test_prompt_is_release_neutral_and_preserves_causal_relationships(monkeypatch, source):
    # Inspect instructions independently of the input. Mock output does not assess translation.
    calls = _fake_ai(monkeypatch, json.dumps({
        "critical": [], "features": ["Örnek çıktı"], "fixes": [], "changes": [],
    }))
    assert summaries.process_ai(source) is not None
    instructions, supplied_source = calls[0]["contents"].split("Metin:\n", 1)
    assert supplied_source == source
    for specific in (
        "/status", "SETTING=0", "Sınıflandırıcı ücretlendirme ayrıntıları",
        "CLAUDE_CODE_AUTO_MODE_SERVER", "Claude", "Bedrock", "v2.1.278",
    ):
        assert specific not in instructions
    for general in (
        "Her eylemin öznesini", "kaynakta olmayan neden-sonuç ilişkisi kurma",
        "kaynakta açıklanan konudan üret", "doğal bir Türkçe cümlenin",
    ):
        assert general in instructions


@pytest.mark.parametrize("response", [
    None, "", "Not JSON", "{}", '{"overview": "Missing lists"}',
    json.dumps({"overview": " ", "critical": [], "features": [], "fixes": [], "changes": []}),
    json.dumps({"overview": "Özet", "critical": [], "features": "Wrong type", "fixes": [], "changes": []}),
    json.dumps({"overview": "Özet", "critical": [], "features": [], "fixes": [], "changes": [],
                "code_examples": ["invented command"]}),
])
def test_process_ai_rejects_invalid_or_invented_content(monkeypatch, response):
    _fake_ai(monkeypatch, response)
    assert summaries.process_ai("Original source") is None
