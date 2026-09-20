import json
from html.parser import HTMLParser

import pytest

from release_radar.messages import ReleaseSummary, build_messages


class TelegramHTML(HTMLParser):
    """Check nesting and inspect rendered text independently of the renderer."""

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.text = []
        self.details = []
        self.codes = []
        self.inline_codes = []
        self.links = []
        self.headings = []
        self.feed(html)
        self.close()
        assert not self.stack

    def handle_starttag(self, tag, attrs):
        assert tag in {"b", "pre", "code", "a"}
        if tag == "blockquote":
            assert attrs == [("expandable", None)]
            assert not self.stack
            self.details.append([])
        elif tag == "code":
            if self.stack == ["pre"]:
                self.codes.append([])
            else:
                assert self.stack in ([], ["blockquote"])
                self.inline_codes.append([])
        elif tag == "a":
            assert self.stack in ([], ["blockquote"])
            assert len(attrs) == 1 and attrs[0][0] == "href"
            assert attrs[0][1].startswith(("https://", "http://"))
            self.links.append(attrs[0][1])
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
            if "pre" in self.stack:
                self.codes[-1].append(data)
            else:
                self.inline_codes[-1].append(data)
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
    assert not parsed.details
    visible = "".join(parsed.text)
    assert "• Yeni özellik." in visible
    assert "• Önemli düzeltme." in visible
    assert "• Davranış değişikliği." in visible
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
    reconstructed = "".join("".join(page.text).split("Yeni özellikler\n", 1)[1]
                            for page in pages if "Yeni özellikler\n" in "".join(page.text))
    assert reconstructed == "• " + content
    for i, (message, page) in enumerate(zip(messages, pages), 1):
        assert f"Ürün\nDuyuru · {i}/{len(pages)}\n" in "".join(page.text)
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
    details = ["".join(page.text).split("Yeni özellikler\n", 1)[1]
               for page in pages if "Yeni özellikler\n" in "".join(page.text)]
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


@pytest.mark.parametrize("overview", [None, "", "   "])
def test_short_announcement_omits_optional_overview(overview):
    data = {"critical": [], "features": ["/status komutuna yeni satır eklendi."],
            "fixes": [], "changes": []}
    if overview is not None:
        data["overview"] = overview
    summary = ReleaseSummary.from_json(json.dumps(data))
    assert summary.overview == ""
    message = build_messages("Claude Code", "v2.1.278", "", summary)[0]
    parsed = parse_messages([message])[0]
    assert "".join(parsed.text) == (
        "Claude Code v2.1.278\n\nYeni özellikler\n• /status komutuna yeni satır eklendi."
    )
    assert not parsed.codes
    assert ["".join(code) for code in parsed.inline_codes] == ["/status"]


@pytest.mark.parametrize("overview", [None, 42, [], {}])
def test_overview_wrong_type_is_rejected(overview):
    with pytest.raises(ValueError, match="overview"):
        ReleaseSummary.from_json(json.dumps({
            "overview": overview, "critical": [], "features": ["Feature"],
            "fixes": [], "changes": [],
        }))


@pytest.mark.parametrize("code_examples", [[], ["SETTING=0"]])
def test_optional_overview_does_not_allow_empty_or_code_only_summary(code_examples):
    with pytest.raises(ValueError, match="explanatory text"):
        ReleaseSummary.from_json(json.dumps({
            "critical": [], "features": [], "fixes": [], "changes": [],
            "code_examples": code_examples,
        }))


def test_renderer_rejects_empty_direct_summary():
    with pytest.raises(ValueError, match="empty summary"):
        build_messages("Claude Code", "v1", "", ReleaseSummary())


def test_long_message_without_overview_still_preserves_items():
    content = "Türkçe 🚀 " * 1500
    pages = parse_messages(build_messages("Ürün", "v1", "", ReleaseSummary(
        changes=(content,),
    )))
    assert len(pages) > 1
    assert "".join("".join(page.text).split("Diğer değişiklikler\n", 1)[1]
                   for page in pages) == "• " + content


@pytest.mark.parametrize(("title", "separator"), [
    ("v2.1.278", " "), ("3.8", " "), ("v1.2.3-beta.1", " "),
    ("New mobile experience", "\n"),
])
def test_version_shares_source_line_but_announcement_keeps_title(title, separator):
    page = parse_messages(build_messages("Ürün", title, "", ReleaseSummary("Özet")))[0]
    assert "".join(page.text) == f"Ürün{separator}{title}\n\nÖzet"


@pytest.mark.parametrize("items", [
    ("x" * 698,), ("x" * 699,), ("a", "b", "c"), ("a", "b", "c", "d"),
])
def test_all_sections_remain_visible(items):
    page = parse_messages(build_messages("Ürün", "v1.0", "", ReleaseSummary(
        critical=("Kritik " * 200,), changes=items,
    )))[0]
    assert not page.details
    visible = "".join(page.text)
    assert "Kritik" in visible
    assert all(item in visible for item in items)


@pytest.mark.parametrize("published_date", [None, "19.09.2026"])
def test_counter_is_beside_version_and_before_date(published_date):
    pages = parse_messages(build_messages(
        "Claude Code", "v2.1.277", "", ReleaseSummary(features=("🚀" * 23000,)),
        published_date,
    ))
    assert len(pages) >= 10
    for index, page in enumerate(pages, 1):
        lines = "".join(page.text).splitlines()
        assert lines[0] == f"Claude Code v2.1.277 · {index}/{len(pages)}"
        if published_date:
            assert lines[1] == "Yayın tarihi: 19.09.2026"


def test_single_message_omits_counter():
    page = parse_messages(build_messages(
        "Claude Code", "v2.1.278", "", ReleaseSummary("Kısa duyuru."),
    ))[0]
    assert "".join(page.text).splitlines()[0] == "Claude Code v2.1.278"


def test_inline_code_links_and_html_escaping():
    url = 'https://example.com/docs?a=1&b="quoted"'
    item = f'`/status` ve `SETTING=0`: [Ayrıntılar <script>]({url}). <b>Literal</b>'
    message = build_messages("Ürün", "v1.0", "", ReleaseSummary(changes=(item,)))[0]
    page = parse_messages([message])[0]
    assert page.links == [url]
    assert ["".join(code) for code in page.inline_codes] == ["/status", "SETTING=0"]
    assert "Ayrıntılar <script>" in "".join(page.text)
    assert "<script>" not in message.text
    assert "&lt;b&gt;Literal&lt;/b&gt;" in message.text


@pytest.mark.parametrize("token", [
    "`" + "command " * 45 + "`",
    "[Ayrıntılar](https://example.com/" + "x" * 300 + ")",
])
def test_inline_tokens_survive_page_boundaries(token):
    pages = parse_messages(build_messages("Ürün", "v1.0", "", ReleaseSummary(
        changes=("x" * 3800 + " " + token + " son",),
    )))
    assert len(pages) > 1
    if token.startswith("`"):
        assert ["".join(code) for page in pages for code in page.inline_codes] == [token[1:-1]]
    else:
        assert [url for page in pages for url in page.links] == [token.split("](")[1][:-1]]


def test_invalid_link_stays_literal_and_oversized_token_does_not_overflow():
    content = "[Tehlikeli](javascript:alert(1)) [Bozuk](https://[broken) " + "`" + "x" * 9000 + "`"
    pages = parse_messages(build_messages("Ürün", "v1.0", "", ReleaseSummary(changes=(content,))))
    assert not any(page.links for page in pages)
    assert "javascript:alert(1)" in "".join(pages[0].text)


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
