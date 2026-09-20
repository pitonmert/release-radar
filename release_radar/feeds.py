"""RSS/Atom fetching and source-specific release extraction."""

from datetime import date
import logging
import re
import time
import feedparser
import requests
from bs4 import BeautifulSoup

MAX_FETCH_RETRIES = 3
RETRY_BACKOFF_BASE = 2
MAX_INPUT_CHARS = 15000


def fetch_feed_with_retry(url, source_name):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 release-radar/1.0"
        )
    }

    for attempt in range(1, MAX_FETCH_RETRIES + 1):
        try:
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()

            feed = feedparser.parse(response.content)
            if feed.bozo and feed.bozo_exception:
                if not feed.entries:
                    raise ValueError(f"Feed parse error: {feed.bozo_exception}")
                logging.warning(f"{source_name} bozo feed (entries exist): {feed.bozo_exception}")
            return feed
        except Exception as e:
            logging.warning(
                f"{source_name} fetch attempt {attempt}/{MAX_FETCH_RETRIES} failed: {e}"
            )
            if attempt < MAX_FETCH_RETRIES:
                time.sleep(RETRY_BACKOFF_BASE**attempt)
    logging.error(f"All fetch attempts exhausted for {source_name}.")
    return None


def fetch_vscode_markdown(entry_link):
    match = re.search(r"v(\d+_\d+)", entry_link)
    if not match:
        logging.error(f"Could not parse version from VS Code entry link: {entry_link}")
        return None

    version = match.group(1)
    url = (
        f"https://raw.githubusercontent.com/microsoft/vscode-docs/main/release-notes/v{version}.md"
    )

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()

        text = response.text
        if len(text) > MAX_INPUT_CHARS:
            logging.info(
                f"VS Code markdown truncated from {len(text)} to {MAX_INPUT_CHARS} characters."
            )
            text = text[:MAX_INPUT_CHARS] + "\n\n[...text truncated...]"
        return text, url
    except Exception as e:
        logging.error(f"Failed to fetch VS Code markdown ({url}): {e}")
        return None


def entry_publication_date(entry):
    published = entry.get("published_parsed")
    if not published:
        return None
    try:
        return date(*published[:3]).strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return None


def collect_updates(feed, source_name, source_type, seen_guids):
    """Return new parsed releases and intentionally skipped IDs."""
    current_source_updates = []
    skipped_guids = []

    entries = feed.entries
    if source_type == "rss":
        entries = list(reversed(entries))
        # Sort dated feeds chronologically; otherwise retain reverse feed order.
        if all(e.get("published_parsed") or e.get("updated_parsed") for e in entries):
            entries.sort(key=lambda e: e.get("published_parsed") or e["updated_parsed"])

    for entry in entries:
        guid = entry.get("id") or entry.get("guid") or entry.get("link")
        if not guid:
            logging.warning(
                f"{source_name}: Skipped entry with missing GUID: "
                f"{entry.get('title', '?')}"
            )
            continue

        if guid in seen_guids:
            continue

        entry_link = entry.get("link", "")
        if source_type == "vscode_github" and "/updates/" not in entry_link:
            continue

        title = entry.get("title", "")
        if source_type != "rss" and "insiders" in title.lower():
            logging.info(f"{source_name}: Skipped Insiders entry: {title}")
            if source_type != "vscode_github":
                skipped_guids.append(guid)
            continue

        if source_type == "vscode_github":
            result = fetch_vscode_markdown(entry_link)
            if not result:
                logging.warning(
                    f"{source_name} - '{title}': "
                    "Full text could not be fetched, skipping."
                )
                continue

            full_text, _source_url = result

            if "ProductEdition: Insiders" in full_text[:1000]:
                logging.info(
                    f"{source_name}: Skipped Insiders entry (Markdown Check): {title}"
                )
                continue
            current_source_updates.append(
                {
                    "guid": guid,
                    "title": title,
                    "text": f"Title: {title}\n\n{full_text}",
                    "url": entry_link,
                    "published_date": entry_publication_date(entry),
                }
            )

        elif source_type in ("github_releases", "rss"):
            contents = entry.get("content") or [{}]
            raw_content = contents[0].get("value") or entry.get("summary") or ""
            if not raw_content:
                logging.warning(
                    f"{source_name} - '{title}': Content is empty, skipping."
                )
                continue

            content_type = (
                contents[0].get("type")
                if contents[0].get("value")
                else entry.get("summary_detail", {}).get("type")
            )
            if source_type == "rss" and content_type in ("text/plain", "text/markdown"):
                content = raw_content.strip()
            else:
                content = (
                    BeautifulSoup(raw_content, "html.parser")
                    .get_text(separator="\n")
                    .strip()
                )

            if not content:
                logging.warning(
                    f"{source_name} - '{title}': Content is empty, skipping."
                )
                continue

            if len(content) > MAX_INPUT_CHARS:
                content = content[:MAX_INPUT_CHARS] + "\n\n[...text truncated...]"

            current_source_updates.append(
                {
                    "guid": guid,
                    "title": title,
                    "text": f"Title: {title}\n\n{content}",
                    "url": entry_link,
                    "published_date": entry_publication_date(entry),
                }
            )

        else:
            logging.warning(
                f"{source_name}: Unknown source type '{source_type}', skipping."
            )
            continue

    if source_type == "github_releases":
        current_source_updates.reverse()
    return current_source_updates, skipped_guids
