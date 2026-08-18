#!/usr/bin/env python3
"""One-time migration: seed the SQLite DB from the legacy state.json.

Run once from the repo root, BEFORE the first `docker compose up`:
    python scripts/migrate_state_to_sqlite.py

Idempotent (safe to re-run) -- inserts use INSERT OR IGNORE under the hood.
"""
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
import db  # noqa: E402

STATE_FILE = os.path.join(REPO_ROOT, "state.json")


def main():
    if not os.path.exists(STATE_FILE):
        print(f"No {STATE_FILE} found -- nothing to migrate.")
        return
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    conn = db.get_connection()
    total = 0
    for source_name, guids in state.items():
        db.mark_seen(conn, source_name, guids)
        print(f"  {source_name}: migrated {len(guids)} GUIDs")
        total += len(guids)
    conn.close()
    print(f"Done. Migrated {total} GUIDs across {len(state)} source(s) into {db.DB_PATH}")


if __name__ == "__main__":
    main()
