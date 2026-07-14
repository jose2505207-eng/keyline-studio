"""Migrations must be additive and idempotent: an existing pre-migration
database keeps its projects/jobs and gains the drone_surveys table."""

import sqlite3

from app.migrations import migrate


def _legacy_db(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL,
            aoi TEXT NOT NULL, drone_path TEXT, created_at REAL NOT NULL);
        CREATE TABLE jobs (id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
            state TEXT NOT NULL, log TEXT NOT NULL DEFAULT '[]',
            updated_at REAL NOT NULL);
        INSERT INTO projects VALUES ('p1', 'legacy', '{}', NULL, 1.0);
        INSERT INTO jobs VALUES ('j1', 'p1', 'done', '[]', 1.0);
    """)
    conn.commit()
    return conn


def test_migrate_preserves_existing_rows(tmp_path):
    conn = _legacy_db(tmp_path / "legacy.sqlite")
    version = migrate(conn)
    assert version >= 1
    assert conn.execute("SELECT name FROM projects WHERE id='p1'").fetchone()[0] == "legacy"
    assert conn.execute("SELECT state FROM jobs WHERE id='j1'").fetchone()[0] == "done"
    # new table exists and is usable
    conn.execute(
        "INSERT INTO drone_surveys (id, project_id, created_at, updated_at) "
        "VALUES ('s1', 'p1', 1.0, 1.0)")
    assert conn.execute("SELECT state FROM drone_surveys").fetchone()[0] == "created"


def test_migrate_is_idempotent(tmp_path):
    conn = _legacy_db(tmp_path / "again.sqlite")
    v1 = migrate(conn)
    v2 = migrate(conn)
    assert v1 == v2
    count = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    assert count == v1  # one row per applied migration, none duplicated


def test_survey_crud_roundtrip(tmp_path, monkeypatch):
    import app.db as db

    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "crud.sqlite"))
    db.init_db()
    pid = db.create_project("t", {"type": "Polygon", "coordinates": []})
    sid = db.create_survey(pid, [{"key": "k1", "filename": "a.jpg", "size": 5}],
                           {"dtm": True}, total_bytes=5)
    s = db.get_survey(sid)
    assert s["state"] == "created" and s["image_count"] == 1
    assert s["images_json"][0]["filename"] == "a.jpg"
    db.update_survey(sid, state="uploaded", uploaded_count=1,
                     warnings_json=["low gps"])
    s = db.get_survey(sid)
    assert s["state"] == "uploaded" and s["warnings_json"] == ["low gps"]
    assert db.list_surveys(pid)[0]["id"] == sid
    assert db.surveys_in_states(["uploaded"])[0]["id"] == sid
