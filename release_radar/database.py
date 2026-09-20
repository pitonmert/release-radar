import os
import sqlite3
import json
import uuid

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "data", "release_radar.db")
DB_PATH = os.environ.get("DB_PATH", DEFAULT_DB_PATH)

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_entries (
    source  TEXT NOT NULL,
    guid    TEXT NOT NULL,
    seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source, guid)
);
CREATE TABLE IF NOT EXISTS releases (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    guid TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    source_text TEXT NOT NULL,
    published_date TEXT,
    discovered_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    model TEXT,
    summary_json TEXT,
    pages_json TEXT,
    notified INTEGER NOT NULL DEFAULT 0,
    UNIQUE(source, guid)
);
CREATE TABLE IF NOT EXISTS bot_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bot_messages (
    chat_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    release_id TEXT,
    PRIMARY KEY(chat_id, message_id)
);
"""


def get_connection(db_path=None):
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def get_release(conn, release_id):
    cursor = conn.execute("SELECT * FROM releases WHERE id = ?", (release_id,))
    row = cursor.fetchone()
    return dict(zip((column[0] for column in cursor.description), row)) if row else None


def save_release(conn, source, update):
    conn.execute(
        "INSERT OR IGNORE INTO releases "
        "(id, source, guid, title, url, source_text, published_date) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (uuid.uuid4().hex, source, update["guid"], update["title"], update["url"],
         update["text"], update.get("published_date")),
    )
    conn.commit()
    release_id = conn.execute(
        "SELECT id FROM releases WHERE source = ? AND guid = ?", (source, update["guid"]),
    ).fetchone()[0]
    return get_release(conn, release_id)


def save_summary(conn, release_id, model, summary, pages):
    conn.execute(
        "UPDATE releases SET model=?, summary_json=?, pages_json=? WHERE id=?",
        (model, json.dumps(summary, ensure_ascii=False), json.dumps(pages, ensure_ascii=False),
         release_id),
    )
    conn.commit()


def mark_notified(conn, release, chat_id, message_id):
    with conn:
        conn.execute("INSERT OR IGNORE INTO bot_messages VALUES (?, ?, ?)",
                     (str(chat_id), message_id, release["id"]))
        conn.execute("UPDATE releases SET notified=1 WHERE id=?", (release["id"],))
        conn.execute("INSERT OR IGNORE INTO seen_entries (source, guid) VALUES (?, ?)",
                     (release["source"], release["guid"]))


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM bot_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn, key, value):
    conn.execute("INSERT INTO bot_meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, str(value)))
    conn.commit()


def namespace(conn):
    conn.execute("INSERT OR IGNORE INTO bot_meta VALUES ('namespace', ?)", (uuid.uuid4().hex[:8],))
    conn.commit()
    return get_meta(conn, "namespace")


def get_seen_guids(conn, source) -> set:
    rows = conn.execute("SELECT guid FROM seen_entries WHERE source = ?", (source,))
    return {row[0] for row in rows}


def mark_seen(conn, source, guids) -> None:
    if not guids:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO seen_entries (source, guid) VALUES (?, ?)",
        [(source, g) for g in guids],
    )
    conn.commit()
