import os
import re
import json
import time
import logging
import requests
import feedparser
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "releaseradar.log")
STATE_FILE = os.path.join(BASE_DIR, "state.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

MAX_TELEGRAM_RETRIES = 3
MAX_FETCH_RETRIES = 3
RETRY_BACKOFF_BASE = 2
MAX_INPUT_CHARS = 15000
TELEGRAM_MAX_CHARS = 4096

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

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


def save_json_atomic(filename, data):
    tmp_file = f"{filename}.tmp"
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        os.replace(tmp_file, filename)
        return True
    except Exception as e:
        logging.error(f"Failed to save {filename}: {e}")
        if os.path.exists(tmp_file):
            os.remove(tmp_file)
        return False


def sort_guids(guids, source_type):
    if source_type == "vscode_github":
        return sorted(set(guids), key=vscode_version_sort_key)
    return unique_guids(guids)


def merge_guids(existing_guids, processed_guids, source_type):
    merged_guids = unique_guids([*existing_guids, *processed_guids])
    if source_type == "vscode_github":
        return sort_guids(merged_guids, source_type)[-50:]
    return merged_guids[-50:]


def unique_guids(guids):
    seen = set()
    unique = []
    for guid in guids:
        if guid in seen:
            continue
        seen.add(guid)
        unique.append(guid)
    return unique


def vscode_version_sort_key(guid):
    match = re.search(r"/updates/v(\d+)_(\d+)", guid)
    if not match:
        return (float("inf"), float("inf"), guid)
    return (int(match.group(1)), int(match.group(2)), guid)


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


def main():
    logging.info("release-radar scan process started.")

    config = load_json_file(CONFIG_FILE, {})
    if not config:
        logging.error("config.json is empty or unreadable. Script stopped.")
        exit(1)

    state = load_json_file(STATE_FILE, {})

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

            seen_guid_list = state.get(source_name, [])
            seen_guids = set(seen_guid_list)

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
                if source_type == "github_releases":
                    initial_guids.reverse()
                state[source_name] = sort_guids(initial_guids, source_type)
                save_json_atomic(STATE_FILE, state)
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
                updated_guids = merge_guids(
                    seen_guid_list,
                    [*successful_guids, *skipped_guids],
                    source_type,
                )
                state[source_name] = updated_guids
                save_json_atomic(STATE_FILE, state)

        except Exception as e:
            logging.error(f"Unexpected error while processing {source_name}: {e}")

    logging.info("release-radar scan process completed.")


if __name__ == "__main__":
    main()
