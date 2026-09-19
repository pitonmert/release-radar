"""Validated summaries and deterministic, size-safe Telegram HTML messages."""

from dataclasses import dataclass
from html import escape
import json
from urllib.parse import urlsplit


TELEGRAM_MAX_CHARS = 4096
LIST_FIELDS = ("critical", "features", "fixes", "changes", "code_examples")
SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "overview": {"type": "string"},
        **{name: {"type": "array", "items": {"type": "string"}} for name in LIST_FIELDS},
    },
    "required": ["overview", "critical", "features", "fixes", "changes"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class ReleaseSummary:
    overview: str
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
        if not isinstance(data["overview"], str) or not data["overview"].strip():
            raise ValueError("Summary overview must be non-empty text")
        values = {"overview": data["overview"].strip()}
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
        body = escape(self.text)
        if self.kind == "details":
            body = f"<blockquote expandable>{body}</blockquote>"
        elif self.kind == "code":
            body = f"<pre><code>{body}</code></pre>"
        heading = f"<b>{escape(self.heading)}</b>\n" if self.heading else ""
        return heading + body


def _take_text(text: str, budget: int) -> tuple[str, str]:
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
                part, remaining = _take_text(remaining, budget - overhead)
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
        header += f"\n<b>{escape(title)}</b>"
        header_length += 1 + text_length(title)
    if published_date:
        date_line = f"Yayın tarihi: {published_date}"
        header += f"\n{escape(date_line)}"
        header_length += 1 + text_length(date_line)

    blocks = [_Block(summary.overview)]
    for field, heading, kind in (
        ("critical", "Kritik değişiklikler", "plain"),
        ("features", "Yeni özellikler", "details"),
        ("fixes", "Önemli hata düzeltmeleri", "details"),
        ("changes", "Diğer değişiklikler", "details"),
    ):
        items = getattr(summary, field)
        if items:
            blocks.append(_Block("\n".join(f"• {item}" for item in items), heading, kind))
    blocks.extend(_Block(code, "Komut / kod örneği", "code") for code in summary.code_examples)

    # Reserve numbering before packing; grow the reservation when crossing 9/99/... parts.
    digits = 1
    while True:
        budget = TELEGRAM_MAX_CHARS - header_length - (2 * digits + 2) - 2
        if budget < 64:
            raise ValueError("Source/title is too long for a Telegram message")
        pages = _pack(blocks, budget)
        required_digits = len(str(len(pages)))
        if required_digits <= digits:
            break
        digits = required_digits

    # Feeds occasionally omit links. Never invent a destination or pass an invalid button.
    markup = None
    parsed = urlsplit(url)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        markup = {"inline_keyboard": [[{"text": "Resmî sürüm notlarını aç", "url": url}]]}
    return [
        TelegramMessage(
            text=header + (f"\n{i}/{len(pages)}" if len(pages) > 1 else "")
            + "\n\n" + "\n\n".join(block.render() for block in page),
            reply_markup=markup,
        )
        for i, page in enumerate(pages, 1)
    ]
