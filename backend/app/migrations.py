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


def _add_stage_progress_columns(conn: sqlite3.Connection) -> None:
    """Migration 5: per-stage timing + a worker claim for stall detection and
    duplicate-worker prevention. Additive and idempotent."""
    existing = {row[1] for row in
                conn.execute("PRAGMA table_info(analysis_runs)").fetchall()}
    columns = [
        ("stage_started_at", "REAL"),
        ("last_progress_at", "REAL"),
        ("current_operation", "TEXT"),
        ("fill_missing_with_satellite", "INTEGER NOT NULL DEFAULT 0"),
        ("claimed_by", "TEXT"),
        ("claimed_at", "REAL"),
    ]
    for name, decl in columns:
        if name not in existing:
            conn.execute(f"ALTER TABLE analysis_runs ADD COLUMN {name} {decl}")


def _add_execution_columns(conn: sqlite3.Connection) -> None:
    """Migration 6: one execution path for every analysis run.

    ``executor`` records how the run was dispatched (rq | inline) so
    operators can tell a queued-worker run from a dev-fallback run;
    ``retry_of``/``retry_count`` link a retried run to the failed run it
    replaces without ever mutating the original. Additive and idempotent."""
    existing = {row[1] for row in
                conn.execute("PRAGMA table_info(analysis_runs)").fetchall()}
    columns = [
        ("executor", "TEXT"),
        ("retry_of", "TEXT"),
        ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
    ]
    for name, decl in columns:
        if name not in existing:
            conn.execute(f"ALTER TABLE analysis_runs ADD COLUMN {name} {decl}")


DEFAULT_ORG_ID = "org_default"
DEFAULT_RANCH_ID = "ranch_default"


def _add_tenancy(conn: sqlite3.Connection) -> None:
    """Migration 8: Organization -> Ranch -> Project hierarchy + users,
    API tokens and an audit log.

    Existing projects and DTMs are backfilled into a default organization/
    ranch so nothing breaks for single-user deployments; auth stays opt-in
    (AUTH_MODE env). Additive and idempotent."""
    import time as _time

    conn.executescript("""
    CREATE TABLE IF NOT EXISTS organizations (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        created_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS ranches (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL REFERENCES organizations(id),
        name TEXT NOT NULL,
        geometry_json TEXT,
        created_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL REFERENCES organizations(id),
        email TEXT UNIQUE,
        name TEXT,
        role TEXT NOT NULL DEFAULT 'owner',
        created_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS api_tokens (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id),
        token_hash TEXT NOT NULL UNIQUE,
        label TEXT,
        created_at REAL NOT NULL,
        last_used_at REAL
    );
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        t REAL NOT NULL,
        user_id TEXT,
        org_id TEXT,
        action TEXT NOT NULL,
        resource TEXT,
        detail TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_ranches_org ON ranches(org_id);
    CREATE INDEX IF NOT EXISTS idx_audit_t ON audit_log(t);
    """)
    now = _time.time()
    conn.execute(
        "INSERT OR IGNORE INTO organizations (id, name, created_at) "
        "VALUES (?, 'Default Organization', ?)", (DEFAULT_ORG_ID, now))
    conn.execute(
        "INSERT OR IGNORE INTO ranches (id, org_id, name, created_at) "
        "VALUES (?, ?, 'Default Ranch', ?)",
        (DEFAULT_RANCH_ID, DEFAULT_ORG_ID, now))
    for table in ("projects", "dtms"):
        existing = {row[1] for row in
                    conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if "org_id" not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN org_id TEXT")
        if table == "projects" and "ranch_id" not in existing:
            conn.execute("ALTER TABLE projects ADD COLUMN ranch_id TEXT")
        conn.execute(f"UPDATE {table} SET org_id=? WHERE org_id IS NULL",
                     (DEFAULT_ORG_ID,))
    conn.execute("UPDATE projects SET ranch_id=? WHERE ranch_id IS NULL",
                 (DEFAULT_RANCH_ID,))


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
    (5, _add_stage_progress_columns),
    (6, _add_execution_columns),
    (
        7,
        """
        CREATE TABLE IF NOT EXISTS artifacts (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            run_id TEXT,
            artifact_type TEXT NOT NULL,
            original_filename TEXT,
            stored_path TEXT NOT NULL,
            storage_provider TEXT NOT NULL DEFAULT 'filesystem',
            size_bytes INTEGER,
            checksum_sha256 TEXT,
            mime_type TEXT,
            crs TEXT,
            bounds_json TEXT,
            resolution_json TEXT,
            width INTEGER,
            height INTEGER,
            band_count INTEGER,
            nodata REAL,
            elevation_min REAL,
            elevation_max REAL,
            algorithm_version TEXT,
            source_artifact_id TEXT,
            created_by TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id);
        CREATE INDEX IF NOT EXISTS idx_artifacts_project
            ON artifacts(project_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_run_type
            ON artifacts(run_id, artifact_type);
        """,
    ),
    (8, _add_tenancy),
]


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations; returns the resulting schema version."""
    versions = [v for v, _ in MIGRATIONS]
    assert versions == sorted(versions), \
        "MIGRATIONS must be listed in ascending version order"
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
