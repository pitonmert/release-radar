"""Runtime configuration and source-list loading."""

import json
import logging
import os
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
DEFAULT_MODEL = "gemini-3.5-flash-lite"
DEFAULT_SCAN_INTERVAL_SECONDS = 6 * 60 * 60
SCAN_INTERVAL_SECONDS = DEFAULT_SCAN_INTERVAL_SECONDS
TELEGRAM_TOKEN = TELEGRAM_CHAT_ID = GEMINI_KEY = None


def load_config():
    global TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, GEMINI_KEY, SCAN_INTERVAL_SECONDS
    load_dotenv(os.path.join(BASE_DIR, ".env"))
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    GEMINI_KEY = os.getenv("GEMINI_API_KEY")
    if not all([TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, GEMINI_KEY]):
        logging.error("Missing .env configuration! Script stopped.")
        exit(1)
    SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", DEFAULT_SCAN_INTERVAL_SECONDS))


def load_json_file(filename, default_value):
    if not os.path.exists(filename):
        return default_value
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"Error reading {filename}: {e}")
        return default_value
