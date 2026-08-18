import db


def _connect(tmp_path):
    return db.get_connection(db_path=str(tmp_path / "test.db"))


def test_get_connection_creates_schema(tmp_path):
    conn = _connect(tmp_path)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='seen_entries'"
    ).fetchall()
    assert len(rows) == 1
    conn.close()


def test_mark_seen_and_get_seen_guids_roundtrip(tmp_path):
    conn = _connect(tmp_path)
    db.mark_seen(conn, "src", ["a", "b", "c"])
    assert db.get_seen_guids(conn, "src") == {"a", "b", "c"}
    conn.close()


def test_mark_seen_dedup_via_primary_key(tmp_path):
    conn = _connect(tmp_path)
    db.mark_seen(conn, "src", ["a", "b"])
    db.mark_seen(conn, "src", ["b", "c"])
    count = conn.execute("SELECT COUNT(*) FROM seen_entries WHERE source = 'src'").fetchone()[0]
    assert count == 3
    conn.close()


def test_get_seen_guids_empty_for_unseen_source(tmp_path):
    conn = _connect(tmp_path)
    assert db.get_seen_guids(conn, "unknown") == set()
    conn.close()


def test_mark_seen_empty_list_is_noop(tmp_path):
    conn = _connect(tmp_path)
    db.mark_seen(conn, "src", [])
    assert db.get_seen_guids(conn, "src") == set()
    conn.close()


def test_mark_seen_isolated_per_source(tmp_path):
    conn = _connect(tmp_path)
    db.mark_seen(conn, "src-a", ["a1"])
    db.mark_seen(conn, "src-b", ["b1"])
    assert db.get_seen_guids(conn, "src-a") == {"a1"}
    assert db.get_seen_guids(conn, "src-b") == {"b1"}
    conn.close()
