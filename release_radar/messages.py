"""Validated summaries and deterministic, size-safe Telegram HTML messages."""

from dataclasses import dataclass
from html import escape
import json
import re
from urllib.parse import urlsplit


TELEGRAM_MAX_CHARS = 4096
INLINE_PATTERN = re.compile(
    r"`(?P<code>[^`\n]+)`"
    r"|\[(?P<label>[^\]\n]+)\]\((?P<url>https?://[^\s<>\)]+)\)"
    r"|(?P<auto>(?<![\w/<])(?:/[A-Za-z][\w-]*|[A-Z][A-Z0-9_]*=[^\s,;()<>]+))"
)


def is_web_url(url):
    try:
        parsed = urlsplit(url)
        return parsed.scheme in ("http", "https") and bool(parsed.hostname)
    except ValueError:
        return False


def inline_link_urls(text):
    return [match["url"] for match in INLINE_PATTERN.finditer(text) if match["url"]]


def render_inline(text):
    """Render only code and HTTP(S) links; escape all model-supplied HTML."""
    parts = []
    offset = 0
    for match in INLINE_PATTERN.finditer(text):
        parts.append(escape(text[offset:match.start()]))
        if match["code"] or match["auto"]:
            parts.append(f'<code>{escape(match["code"] or match["auto"])}</code>')
        elif is_web_url(match["url"]):
            parts.append(f'<a href="{escape(match["url"], quote=True)}">'
                         f'{escape(match["label"])}</a>')
        else:
            parts.append(escape(match[0]))
        offset = match.end()
    parts.append(escape(text[offset:]))
    return "".join(parts)
LIST_FIELDS = ("critical", "features", "fixes", "changes", "code_examples")
SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "overview": {"type": "string"},
        **{name: {"type": "array", "items": {"type": "string"}} for name in LIST_FIELDS},
    },
    "required": ["critical", "features", "fixes", "changes"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class ReleaseSummary:
    overview: str = ""
    critical: tuple[str, ...] = ()
    features: tuple[str, ...] = ()
    fixes: tuple[str, ...] = ()
    changes: tuple[str, ...] = ()
    code_examples: tuple[str, ...] = ()

    @classmethod
    def from_json(cls, raw: str):
        data = json.loads(raw)
        required = set(SUMMARY_SCHEMA["required"])
        allowed = set(SUMMARY_SCHEMA["properties"])
        if not isinstance(data, dict) or not required <= data.keys() or data.keys() - allowed:
            raise ValueError("Summary fields do not match the schema")
        overview = data.get("overview", "")
        if not isinstance(overview, str):
            raise ValueError("Summary overview must be text when provided")
        values = {"overview": overview.strip()}
        for name in LIST_FIELDS:
            items = data.get(name, [])
            if not isinstance(items, list) or any(
                not isinstance(item, str) or not item.strip() for item in items
            ):
                raise ValueError(f"Summary {name} must contain non-empty strings")
            # Preserve every character of source code, including indentation.
            values[name] = tuple(dict.fromkeys(
                items if name == "code_examples" else (item.strip() for item in items)
            ))
        critical = set(values["critical"])
        for name in ("features", "fixes", "changes"):
            values[name] = tuple(item for item in values[name] if item not in critical)
        if not values["overview"] and not any(values[name] for name in LIST_FIELDS[:-1]):
            raise ValueError("Summary must contain explanatory text, not just code")
        return cls(**values)


@dataclass(frozen=True)
class TelegramMessage:
    text: str
    reply_markup: dict | None


def text_length(text: str) -> int:
    """Use UTF-16 units conservatively so astral emoji cannot overflow a message."""
    return len(text.encode("utf-16-le")) // 2


@dataclass(frozen=True)
class _Block:
    text: str
    heading: str = ""
    kind: str = "plain"

    @property
    def length(self):
        return text_length(self.text) + (text_length(self.heading) + 1 if self.heading else 0)

    def render(self):
        body = escape(self.text) if self.kind == "code" else render_inline(self.text)
        if self.kind == "code":
            body = f"<pre><code>{body}</code></pre>"
        heading = f"<b>{escape(self.heading)}</b>\n" if self.heading else ""
        return heading + body


def _take_text(text: str, budget: int, *, protect_inline=False) -> tuple[str, str]:
    used = 0
    end = 0
    for char in text:
        size = 2 if ord(char) > 0xFFFF else 1
        if used + size > budget:
            break
        used += size
        end += 1
    if not end:
        raise ValueError("Message header leaves no room for content")
    if end < len(text):
        # Keep list items and code lines together where possible; never drop whitespace.
        boundary = text.rfind("\n", 0, end)
        if boundary < 0:
            boundary = text.rfind(" ", 0, end)
        if boundary >= 0:
            end = boundary + 1
        if protect_inline:
            for match in INLINE_PATTERN.finditer(text):
                if match.start() < end < match.end():
                    if match.start() > 0:
                        end = match.start()
                    elif text_length(match[0]) <= budget:
                        end = match.end()
                    # An oversized token is split as literal text, never as raw HTML.
                    break
    return text[:end], text[end:]


def _pack(blocks: list[_Block], budget: int) -> list[list[_Block]]:
    pages = []
    page = []
    used = 0
    for block in blocks:
        if block.length > budget:
            if page:
                pages.append(page)
                page, used = [], 0
            overhead = block.length - text_length(block.text)
            remaining = block.text
            while remaining:
                part, remaining = _take_text(
                    remaining, budget - overhead, protect_inline=block.kind != "code",
                )
                fragment = _Block(part, block.heading, block.kind)
                if remaining:
                    pages.append([fragment])
                else:
                    page, used = [fragment], fragment.length
        else:
            separator = 2 if page else 0
            if used + separator + block.length > budget:
                pages.append(page)
                page, used, separator = [], 0, 0
            page.append(block)
            used += separator + block.length
    if page:
        pages.append(page)
    return pages


def build_messages(
    source: str, title: str, url: str, summary: ReleaseSummary,
    published_date: str | None = None,
) -> list[TelegramMessage]:
    """Build every part before sending, so rendering failures never cause partial delivery."""
    header = f"<b>{escape(source)}</b>"
    header_length = text_length(source)
    if title:
        separator = " " if re.fullmatch(r"v?\d+\.\d+(?:\.\d+)?(?:[-+][\w.-]+)?", title) else "\n"
        header += f"{separator}<b>{escape(title)}</b>"
        header_length += text_length(separator) + text_length(title)
    date_suffix = ""
    if published_date:
        date_line = f"Yayın tarihi: {published_date}"
        date_suffix = f"\n{escape(date_line)}"
        header_length += 1 + text_length(date_line)

    blocks = [_Block(summary.overview)] if summary.overview.strip() else []
    for field, heading in (
        ("critical", "Kritik değişiklikler"),
        ("features", "Yeni özellikler"),
        ("fixes", "Önemli hata düzeltmeleri"),
        ("changes", "Diğer değişiklikler"),
    ):
        items = getattr(summary, field)
        if items:
            body = "\n".join(f"• {item}" for item in items)
            blocks.append(_Block(body, heading))
    blocks.extend(_Block(code, "Komut / kod örneği", "code") for code in summary.code_examples)
    if not blocks:
        raise ValueError("Cannot render an empty summary")

    # Reserve numbering before packing; grow the reservation when crossing 9/99/... parts.
    digits = 1
    while True:
        budget = TELEGRAM_MAX_CHARS - header_length - (2 * digits + 4) - 2
        if budget < 64:
            raise ValueError("Source/title is too long for a Telegram message")
        pages = _pack(blocks, budget)
        required_digits = len(str(len(pages)))
        if required_digits <= digits:
            break
        digits = required_digits

    # Feeds occasionally omit links. Never invent a destination or pass an invalid button.
    markup = None
    if is_web_url(url):
        markup = {"inline_keyboard": [[{"text": "Resmî sürüm notlarını aç", "url": url}]]}
    return [
        TelegramMessage(
            text=header + (f" · {i}/{len(pages)}" if len(pages) > 1 else "") + date_suffix
            + "\n\n" + "\n\n".join(block.render() for block in page),
            reply_markup=markup,
        )
        for i, page in enumerate(pages, 1)
    ]
