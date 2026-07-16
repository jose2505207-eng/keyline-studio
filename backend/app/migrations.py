"""Idempotent, additive SQLite migrations.

A `schema_version` table records the highest applied migration. Migrations
only ever add tables/columns — never drop or rewrite — so existing
installations keep their projects and jobs. `migrate()` is safe to call on
every startup and from multiple processes (WAL + IMMEDIATE transaction).
"""

from __future__ import annotations

import sqlite3
from typing import Callable, Union


def _add_analysis_progress_columns(conn: sqlite3.Connection) -> None:
    """Migration 4: structured, persisted analysis-run progress.

    Additive and idempotent — each column is added only if missing, so a
    database left half-migrated by an interrupted upgrade converges cleanly.
    No existing analysis run is dropped or rewritten.
    """
    existing = {row[1] for row in
                conn.execute("PRAGMA table_info(analysis_runs)").fetchall()}
    columns = [
        ("stage_label", "TEXT"),
        ("stage_index", "INTEGER NOT NULL DEFAULT 0"),
        ("stage_count", "INTEGER NOT NULL DEFAULT 0"),
        ("stage_plan_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("progress_percent", "REAL NOT NULL DEFAULT 0"),
        ("current_message", "TEXT"),
        ("heartbeat_at", "REAL"),
        ("started_at", "REAL"),
        ("error_code", "TEXT"),
        ("rq_job_id", "TEXT"),
        ("worker_name", "TEXT"),
        ("terrain_source", "TEXT"),
        ("analysis_version", "TEXT"),
        ("cancel_requested", "INTEGER NOT NULL DEFAULT 0"),
        ("log_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("warnings_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("exports_json", "TEXT NOT NULL DEFAULT '{}'"),
    ]
    for name, decl in columns:
        if name not in existing:
            conn.execute(f"ALTER TABLE analysis_runs ADD COLUMN {name} {decl}")


MIGRATIONS: list[tuple[int, Union[str, Callable[[sqlite3.Connection], None]]]] = [
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
    (
        2,
        """
        CREATE TABLE IF NOT EXISTS analysis_runs (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            survey_id TEXT,
            state TEXT NOT NULL DEFAULT 'queued',
            stage TEXT,
            dem_mode TEXT,
            dem_path TEXT,
            params_json TEXT NOT NULL DEFAULT '{}',
            qa_json TEXT,
            counts_json TEXT,
            notices_json TEXT NOT NULL DEFAULT '[]',
            error_message TEXT,
            result_dir TEXT,
            created_at REAL NOT NULL,
            completed_at REAL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_analysis_runs_project
            ON analysis_runs(project_id);
        """,
    ),
    (
        3,
        """
        CREATE TABLE IF NOT EXISTS dtms (
            id TEXT PRIMARY KEY,
            storage_path TEXT NOT NULL,
            display_name TEXT NOT NULL,
            original_filename TEXT,
            source_type TEXT NOT NULL DEFAULT 'upload',
            status TEXT NOT NULL DEFAULT 'ready',
            size_bytes INTEGER,
            checksum TEXT,
            crs TEXT,
            width INTEGER,
            height INTEGER,
            nodata REAL,
            survey_id TEXT,
            project_id TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_dtms_survey ON dtms(survey_id);
        """,
    ),
    (4, _add_analysis_progress_columns),
]


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations; returns the resulting schema version."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER NOT NULL)"
    )
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] or 0
    for version, step in MIGRATIONS:
        if version <= current:
            continue
        if callable(step):
            step(conn)
        else:
            conn.executescript(step)
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        current = version
    conn.commit()
    return current
