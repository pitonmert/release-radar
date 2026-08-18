import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "data", "release_radar.db")
DB_PATH = os.environ.get("DB_PATH", DEFAULT_DB_PATH)

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_entries (
    source  TEXT NOT NULL,
    guid    TEXT NOT NULL,
    seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source, guid)
);
"""


def get_connection(db_path=None):
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


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
