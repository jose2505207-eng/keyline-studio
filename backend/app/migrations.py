"""Idempotent, additive SQLite migrations.

A `schema_version` table records the highest applied migration. Migrations
only ever add tables/columns — never drop or rewrite — so existing
installations keep their projects and jobs. `migrate()` is safe to call on
every startup and from multiple processes (WAL + IMMEDIATE transaction).
"""

from __future__ import annotations

import sqlite3

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS drone_surveys (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            provider TEXT NOT NULL DEFAULT 'nodeodm',
            external_task_id TEXT,
            state TEXT NOT NULL DEFAULT 'created',
            stage TEXT,
            progress_percent REAL NOT NULL DEFAULT 0,
            image_count INTEGER NOT NULL DEFAULT 0,
            uploaded_count INTEGER NOT NULL DEFAULT 0,
            total_bytes INTEGER NOT NULL DEFAULT 0,
            images_json TEXT NOT NULL DEFAULT '[]',
            gcp_key TEXT,
            options_json TEXT NOT NULL DEFAULT '{}',
            provider_status_json TEXT,
            preflight_json TEXT,
            warnings_json TEXT NOT NULL DEFAULT '[]',
            error_message TEXT,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            dtm_path TEXT,
            orthophoto_path TEXT,
            manifest_path TEXT,
            created_at REAL NOT NULL,
            started_at REAL,
            completed_at REAL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_drone_surveys_project
            ON drone_surveys(project_id);
        """,
    ),
]


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations; returns the resulting schema version."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER NOT NULL)"
    )
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] or 0
    for version, sql in MIGRATIONS:
        if version <= current:
            continue
        conn.executescript(sql)
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        current = version
    conn.commit()
    return current
