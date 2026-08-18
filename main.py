import os
import re
import json
import time
import logging
import signal
import sys
import requests
import feedparser
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from dotenv import load_dotenv

import db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

MAX_TELEGRAM_RETRIES = 3
MAX_FETCH_RETRIES = 3
RETRY_BACKOFF_BASE = 2
MAX_INPUT_CHARS = 15000
TELEGRAM_MAX_CHARS = 4096

DEFAULT_SCAN_INTERVAL_SECONDS = 6 * 60 * 60
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", DEFAULT_SCAN_INTERVAL_SECONDS))
SHUTDOWN_POLL_SECONDS = 5

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

TELEGRAM_TOKEN = TELEGRAM_CHAT_ID = GEMINI_KEY = None
ai_client = None


def load_config():
    global TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, GEMINI_KEY, ai_client
    load_dotenv()
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    GEMINI_KEY = os.getenv("GEMINI_API_KEY")
    if not all([TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, GEMINI_KEY]):
        logging.error("Missing .env configuration! Script stopped.")
        exit(1)
    ai_client = genai.Client(api_key=GEMINI_KEY)


def load_json_file(filename, default_value):
    if not os.path.exists(filename):
        return default_value
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"Error reading {filename}: {e}")
        return default_value


def fetch_feed_with_retry(url, source_name):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 release-radar/1.0"
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
                logging.warning(
                    f"{source_name} bozo feed (entries exist): {feed.bozo_exception}"
                )
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
    url = f"https://raw.githubusercontent.com/microsoft/vscode-docs/main/release-notes/v{version}.md"

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()

        text = response.text
        if len(text) > MAX_INPUT_CHARS:
            logging.info(
                f"VS Code markdown truncated from {len(text)} to "
                f"{MAX_INPUT_CHARS} characters."
            )
            text = text[:MAX_INPUT_CHARS] + "\n\n[...text truncated...]"
        return text, url
    except Exception as e:
        logging.error(f"Failed to fetch VS Code markdown ({url}): {e}")
        return None


def process_ai(text_to_summarize):
    try:
        prompt = (
            "Aşağıdaki yazılım sürüm notlarını Türkçeye çevir ve özetle. "
            "Teknik terimleri, komut adlarını ve kod ifadelerini değiştirme. "
            "Markdown karakteri (*, `, #) kullanma, sadece düz metin kullan. "
            "İlk satıra sürüm adını/numarasını yaz (Örn: Sürüm: v2.1.146).\n\n"
            "Çıktıyı şu üç bölümde düzenle:\n"
            "1. Yeni Özellikler: Tüm yeni özellikleri kısa ve öz şekilde listele. Her maddeyi '- ' ile başlat.\n"
            "2. Kritik Hata Düzeltmeleri: Yalnızca kritik veya önemli hata düzeltmelerini özetle. Her maddeyi '- ' ile başlat.\n"
            "3. Önemli Değişiklikler: Davranış değişiklikleri, kaldırılan özellikler veya breaking change niteliğindeki güncellemeleri özetle. Her maddeyi '- ' ile başlat.\n\n"
            "Eğer bir bölümde ilgili içerik yoksa o bölümü atla.\n\n"
            f"Metin:\n{text_to_summarize}"
        )
        response = ai_client.models.generate_content(
            model="gemini-flash-lite-latest",
            contents=prompt,
            config=types.GenerateContentConfig(max_output_tokens=8192),
        )
        return response.text
    except Exception as e:
        logging.error(f"Gemini API error: {e}")
        return None


def _send_single_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}

    for attempt in range(1, MAX_TELEGRAM_RETRIES + 1):
        try:
            response = requests.post(url, json=payload, timeout=10)
            if response.status_code == 200:
                return True
            logging.warning(
                f"Telegram attempt {attempt}/{MAX_TELEGRAM_RETRIES} "
                f"failed (HTTP {response.status_code}): {response.text}"
            )
        except Exception as e:
            logging.warning(
                f"Telegram attempt {attempt}/{MAX_TELEGRAM_RETRIES} connection error: {e}"
            )

        if attempt < MAX_TELEGRAM_RETRIES:
            time.sleep(RETRY_BACKOFF_BASE**attempt)

    logging.error("All Telegram send attempts exhausted.")
    return False


def send_telegram_message(message):
    if len(message) <= TELEGRAM_MAX_CHARS:
        success = _send_single_telegram(message)
        if success:
            logging.info("Telegram notification sent successfully.")
        return success

    chunks = []
    current = ""
    for line in message.split("\n"):
        if len(current) + len(line) + 1 > TELEGRAM_MAX_CHARS:
            if current:
                chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)

    logging.info(
        f"Message split into {len(chunks)} chunks ({len(message)} characters)."
    )

    for i, chunk in enumerate(chunks, 1):
        if not _send_single_telegram(chunk):
            logging.error(f"Failed to send Telegram chunk {i}/{len(chunks)}.")
            return False
        if i < len(chunks):
            time.sleep(1)

    logging.info("Telegram notification sent successfully (multi-chunk).")
    return True


def run_scan_cycle():
    logging.info("release-radar scan process started.")

    config = load_json_file(CONFIG_FILE, {})
    if not config:
        logging.error("config.json is empty or unreadable; skipping this scan cycle.")
        return

    conn = db.get_connection()
    try:
        for source_name, source_config in config.items():
            rss_url = source_config.get("rss")
            source_type = source_config.get("type")

            if not rss_url or not source_type:
                logging.error(
                    f"{source_name}: 'rss' or 'type' missing in config, skipping."
                )
                continue

            try:
                feed = fetch_feed_with_retry(rss_url, source_name)
                if not feed or not feed.entries:
                    logging.warning(f"{source_name}: Feed empty or unreachable, skipping.")
                    continue

                seen_guids = db.get_seen_guids(conn, source_name)

                if not seen_guids:
                    logging.info(f"Initial setup for {source_name}, syncing data...")
                    initial_guids = []
                    for entry in feed.entries[:50]:
                        guid = entry.get("id") or entry.get("guid") or entry.get("link")
                        if not guid:
                            continue
                        entry_link = entry.get("link", "")
                        if source_type == "vscode_github" and "/updates/" not in entry_link:
                            continue
                        initial_guids.append(guid)
                    db.mark_seen(conn, source_name, initial_guids)
                    continue

                current_source_updates = []
                skipped_guids = []

                for entry in feed.entries:
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
                    if "insiders" in title.lower():
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

                        full_text, source_url = result

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
                                "url": source_url,
                            }
                        )

                    elif source_type == "github_releases":
                        contents = entry.get("content") or [{}]
                        raw_content = contents[0].get("value") or entry.get("summary") or ""
                        if not raw_content:
                            logging.warning(
                                f"{source_name} - '{title}': Content is empty, skipping."
                            )
                            continue

                        content = (
                            BeautifulSoup(raw_content, "html.parser")
                            .get_text(separator="\n")
                            .strip()
                        )

                        if len(content) > MAX_INPUT_CHARS:
                            content = (
                                content[:MAX_INPUT_CHARS] + "\n\n[...text truncated...]"
                            )

                        current_source_updates.append(
                            {
                                "guid": guid,
                                "title": title,
                                "text": f"Title: {title}\n\n{content}",
                                "url": entry_link,
                            }
                        )

                    else:
                        logging.warning(
                            f"{source_name}: Unknown source type '{source_type}', skipping."
                        )
                        continue

                if not current_source_updates and not skipped_guids:
                    logging.info(f"{source_name}: No new updates.")
                    continue

                if current_source_updates:
                    if source_type == "github_releases":
                        current_source_updates.reverse()
                    logging.info(
                        f"Found {len(current_source_updates)} new updates in {source_name}."
                    )

                successful_guids = []

                for update in current_source_updates:
                    logging.info(f"Processing: {update['title']} -> {update['url']}")

                    ai_summary = process_ai(update["text"])

                    if not ai_summary:
                        logging.error(
                            f"Failed to generate AI summary for {source_name} - '{update['title']}'."
                        )
                        continue

                    message = (
                        f"release-radar: {source_name.upper()} Update\n\n"
                        f"{ai_summary}\n\n"
                        f"Source: {update['url']}"
                    )
                    if send_telegram_message(message):
                        successful_guids.append(update["guid"])

                if successful_guids or skipped_guids:
                    db.mark_seen(conn, source_name, [*successful_guids, *skipped_guids])

            except Exception as e:
                logging.error(f"Unexpected error while processing {source_name}: {e}")
    finally:
        conn.close()

    logging.info("release-radar scan process completed.")


_shutdown_requested = False


def _handle_shutdown_signal(signum, frame):
    global _shutdown_requested
    logging.info(f"Received signal {signum}; stopping after the current cycle.")
    _shutdown_requested = True


def _sleep_interruptibly(total_seconds, poll_seconds=SHUTDOWN_POLL_SECONDS):
    elapsed = 0
    while elapsed < total_seconds and not _shutdown_requested:
        time.sleep(min(poll_seconds, total_seconds - elapsed))
        elapsed += poll_seconds


def main():
    load_config()
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    logging.info(f"release-radar service starting (interval: {SCAN_INTERVAL_SECONDS}s).")
    while not _shutdown_requested:
        try:
            run_scan_cycle()
        except Exception:
            logging.exception("Unhandled error during scan cycle; will retry next interval.")
        if _shutdown_requested:
            break
        _sleep_interruptibly(SCAN_INTERVAL_SECONDS)
    logging.info("release-radar service stopped.")


if __name__ == "__main__":
    main()
