import json
from html.parser import HTMLParser

import pytest

from telegram_messages import ReleaseSummary, build_messages


class TelegramHTML(HTMLParser):
    """Check nesting and inspect rendered text independently of the renderer."""

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.text = []
        self.details = []
        self.codes = []
        self.headings = []
        self.feed(html)
        self.close()
        assert not self.stack

    def handle_starttag(self, tag, attrs):
        assert tag in {"b", "blockquote", "pre", "code"}
        if tag == "blockquote":
            assert attrs == [("expandable", None)]
            assert not self.stack
            self.details.append([])
        elif tag == "code":
            assert self.stack == ["pre"]
            self.codes.append([])
        elif tag == "b":
            self.headings.append([])
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack.pop() == tag

    def handle_data(self, data):
        self.text.append(data)
        if "blockquote" in self.stack:
            self.details[-1].append(data)
        if "code" in self.stack:
            self.codes[-1].append(data)
        if "b" in self.stack:
            self.headings[-1].append(data)


def parse_messages(messages):
    parsed = [TelegramHTML(message.text) for message in messages]
    for page in parsed:
        visible = "".join(page.text)
        assert 0 < len(visible.encode("utf-16-le")) // 2 <= 4096
    return parsed


@pytest.mark.parametrize(("source", "title"), [
    ("VS Code", "September 2026 (version 1.138)"),
    ("Claude Code", "v2.1.278"),
    ("ChatGPT & Codex", "New mobile experience"),
])
def test_layout_and_source_metadata(source, title):
    summary = ReleaseSummary(
        "Kısa özet.", critical=("Ayarı değiştirin.",), features=("Yeni özellik.",),
        fixes=("Önemli düzeltme.",), changes=("Davranış değişikliği.",),
        code_examples=('codex exec "örnek"',),
    )
    messages = build_messages(source, title, "https://example.com/release", summary, "19.09.2026")
    assert len(messages) == 1
    parsed = parse_messages(messages)[0]
    assert ["".join(x) for x in parsed.headings][:2] == [source, title]
    assert "Yayın tarihi: 19.09.2026" in "".join(parsed.text)
    assert "Ayarı değiştirin." not in "".join(sum(parsed.details, []))
    assert ["".join(x) for x in parsed.details] == [
        "• Yeni özellik.", "• Önemli düzeltme.", "• Davranış değişikliği.",
    ]
    assert "".join(sum(parsed.codes, [])) == 'codex exec "örnek"'
    assert messages[0].reply_markup == {"inline_keyboard": [[{
        "text": "Resmî sürüm notlarını aç", "url": "https://example.com/release",
    }]]}


def test_empty_sections_missing_title_date_and_link_are_omitted():
    message = build_messages("ChatGPT & Codex", "", "", ReleaseSummary("Yeni duyuru."))[0]
    parsed = parse_messages([message])[0]
    assert "".join(parsed.text) == "ChatGPT & Codex\n\nYeni duyuru."
    assert not parsed.details
    assert not parsed.codes
    assert message.reply_markup is None


def test_untrusted_markup_and_entities_are_displayed_literally():
    content = '<b>Türkçe & "alıntı"</b> &lt; 🚀 <script>bad</script>'
    message = build_messages(content, content, "https://example.com/?a=1&b=2", ReleaseSummary(
        content, features=(content,), code_examples=(content,),
    ))[0]
    parsed = parse_messages([message])[0]
    assert "".join(parsed.text).count(content) == 5
    assert "<script>" not in message.text
    assert "&amp;lt;" in message.text


@pytest.mark.parametrize("content", [
    "a" * 17000,
    "🚀" * 9000,
    "<>&" * 4500,
    "\n".join(f"Madde {i}: Türkçe özellik açıklaması" for i in range(1800)),
])
def test_long_details_are_lossless_and_numbered(content):
    messages = build_messages("Ürün", "Duyuru", "https://example.com/release", ReleaseSummary(
        "Kısa özet", features=(content,),
    ))
    pages = parse_messages(messages)
    assert len(pages) > 1
    reconstructed = "".join("".join(group) for page in pages for group in page.details)
    assert reconstructed == "• " + content
    for i, (message, page) in enumerate(zip(messages, pages), 1):
        assert f"Ürün\nDuyuru\n{i}/{len(pages)}\n" in "".join(page.text)
        assert message.reply_markup == messages[0].reply_markup


def test_code_preserves_indentation_blank_lines_and_long_lines():
    code = '  print("<>& 🚀")\n\n' + "x" * 9000 + '\n    return "Türkçe"\n'
    messages = build_messages("Ürün", "Sürüm", "https://example.com", ReleaseSummary(
        "Özet", code_examples=(code,),
    ))
    pages = parse_messages(messages)
    assert "".join("".join(code) for page in pages for code in page.codes) == code


def test_numbering_reserves_space_when_part_count_grows():
    content = "🚀" * 23000
    messages = build_messages("Ürün " + "x" * 250, "v1", "https://example.com", ReleaseSummary(content))
    pages = parse_messages(messages)
    assert len(pages) >= 10
    restored = "".join("".join(page.text).split("\n\n", 1)[1] for page in pages)
    assert restored == content


def test_whole_items_move_to_next_page_without_splitting():
    items = ("a" * 2100, "b" * 2100)
    messages = build_messages("Ürün", "v1", "https://example.com", ReleaseSummary(
        "Özet", features=items,
    ))
    pages = parse_messages(messages)
    details = ["".join(group) for page in pages for group in page.details]
    assert len(details) == 2
    assert details[0] == "• " + items[0] + "\n"
    assert details[1] == "• " + items[1]


def test_oversized_header_fails_before_delivery():
    with pytest.raises(ValueError, match="too long"):
        build_messages("Ürün", "x" * 4096, "https://example.com", ReleaseSummary("Özet"))


@pytest.mark.parametrize("url", ["", "javascript:alert(1)", "https://", "/relative"])
def test_invalid_links_do_not_become_buttons(url):
    message = build_messages("Ürün", "v1", url, ReleaseSummary("Özet"))[0]
    assert message.reply_markup is None


def test_summary_validation_removes_exact_critical_duplicates():
    result = ReleaseSummary.from_json(json.dumps({
        "overview": " Özet ", "critical": ["Kritik", "Kritik"],
        "features": ["Kritik", "Özellik"], "fixes": ["Kritik"], "changes": [],
    }))
    assert result == ReleaseSummary("Özet", critical=("Kritik",), features=("Özellik",))


@pytest.mark.parametrize("field", ["critical", "features", "fixes", "changes", "code_examples"])
@pytest.mark.parametrize("bad", [None, "text", {}, [None], [4], [" "]])
def test_summary_rejects_invalid_list_members(field, bad):
    data = {"overview": "Özet", "critical": [], "features": [], "fixes": [], "changes": []}
    data[field] = bad
    with pytest.raises(ValueError):
        ReleaseSummary.from_json(json.dumps(data))


@pytest.mark.parametrize("data", [[], None, "text", {"overview": "Özet"}, {
    "overview": "Özet", "critical": [], "features": [], "fixes": [], "changes": [], "extra": "x",
}])
def test_summary_rejects_missing_or_unknown_fields(data):
    with pytest.raises(ValueError):
        ReleaseSummary.from_json(json.dumps(data))
