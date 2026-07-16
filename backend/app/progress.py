"""Structured, honest terrain-analysis progress.

The single source of truth for "what stage is this analysis run in, and is the
worker still alive?" is the ``analysis_runs`` row. This module gives the
pipeline a :class:`ProgressReporter` that records real stage transitions and a
background heartbeat, and gives the API a :func:`classify_health` helper that
turns the persisted timestamps (plus optional RQ job status) into an honest
health verdict.

Design rules (see the mission brief):

* Progress is computed from *completed* stages in the active plan — never from
  a fabricated fine-grained percentage. It never decreases.
* A heartbeat proves the worker is alive; it does **not** advance progress.
* The stage plan is built dynamically for the selected DEM mode, so a
  drone-only run never shows "fetching satellite DEM" as pending.
* Every stage transition is committed to the database immediately.
* The heartbeat thread is always stopped in a ``finally`` block and is bound to
  one run id, so it can never leak or touch another run's row.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable

from . import db

log = logging.getLogger(__name__)

ANALYSIS_VERSION = "2"


# ---------------------------------------------------------------------------
# Canonical stages + human labels

QUEUED = "queued"
LOADING_PROJECT = "loading_project"
RESOLVING_DTM = "resolving_dtm"
VALIDATING_DTM = "validating_dtm"
TERRAIN_QUALITY_CHECKS = "terrain_quality_checks"
SELECTING_DEM_MODE = "selecting_dem_mode"
FETCHING_SATELLITE_DEM = "fetching_satellite_dem"
REPROJECTING_SATELLITE_DEM = "reprojecting_satellite_dem"
PREPARING_DRONE_DEM = "preparing_drone_dem"
COMPUTING_DRONE_COVERAGE = "computing_drone_coverage"
FUSING_DEM = "fusing_dem"
CLIPPING_DEM = "clipping_dem"
CONDITIONING_DEM = "conditioning_dem"
CALCULATING_FLOW_DIRECTION = "calculating_flow_direction"
CALCULATING_FLOW_ACCUMULATION = "calculating_flow_accumulation"
EXTRACTING_VALLEYS = "extracting_valleys"
EXTRACTING_RIDGES = "extracting_ridges"
DETECTING_KEYPOINTS = "detecting_keypoints"
GENERATING_KEYLINES = "generating_keylines"
VALIDATING_SPATIAL_RESULTS = "validating_spatial_results"
GENERATING_HILLSHADE = "generating_hillshade"
GENERATING_EXPORTS = "generating_exports"
SAVING_RESULTS = "saving_results"
COMPLETED = "completed"

STAGE_LABELS: dict[str, str] = {
    QUEUED: "Queued",
    LOADING_PROJECT: "Loading project",
    RESOLVING_DTM: "Resolving DTM",
    VALIDATING_DTM: "Validating DTM",
    TERRAIN_QUALITY_CHECKS: "Running terrain quality checks",
    SELECTING_DEM_MODE: "Selecting DEM mode",
    FETCHING_SATELLITE_DEM: "Fetching satellite DEM",
    REPROJECTING_SATELLITE_DEM: "Reprojecting satellite DEM",
    PREPARING_DRONE_DEM: "Preparing drone DEM",
    COMPUTING_DRONE_COVERAGE: "Computing drone coverage",
    FUSING_DEM: "Fusing DEM",
    CLIPPING_DEM: "Clipping DEM to AOI",
    CONDITIONING_DEM: "Conditioning terrain",
    CALCULATING_FLOW_DIRECTION: "Calculating flow direction",
    CALCULATING_FLOW_ACCUMULATION: "Calculating flow accumulation",
    EXTRACTING_VALLEYS: "Extracting valleys",
    EXTRACTING_RIDGES: "Extracting ridges",
    DETECTING_KEYPOINTS: "Detecting keypoints",
    GENERATING_KEYLINES: "Generating keylines",
    VALIDATING_SPATIAL_RESULTS: "Validating spatial results",
    GENERATING_HILLSHADE: "Generating hillshade",
    GENERATING_EXPORTS: "Creating downloads",
    SAVING_RESULTS: "Saving results",
    COMPLETED: "Complete",
}

TERRAIN_SOURCE_LABELS = {
    "satellite_only": "Satellite",
    "drone_only": "Drone DTM",
    "fused": "Fused (drone + satellite)",
    "existing_dtm": "Existing DTM",
}

# Terminal run states
TERMINAL_STATES = {"completed", "completed_with_warnings", "failed", "cancelled"}


def stage_label(stage: str | None) -> str:
    if not stage:
        return ""
    return STAGE_LABELS.get(stage, stage.replace("_", " ").capitalize())


def build_stage_plan(dem_mode: str) -> list[str]:
    """The ordered stage plan for a resolved DEM mode.

    ``auto`` is treated as a provisional superset until the pipeline resolves
    the real mode and calls :meth:`ProgressReporter.set_mode`.
    """
    common_tail = [
        CONDITIONING_DEM,
        CALCULATING_FLOW_ACCUMULATION,
        EXTRACTING_VALLEYS,
        EXTRACTING_RIDGES,
        DETECTING_KEYPOINTS,
        GENERATING_KEYLINES,
        VALIDATING_SPATIAL_RESULTS,
        GENERATING_HILLSHADE,
        GENERATING_EXPORTS,
        SAVING_RESULTS,
        COMPLETED,
    ]
    if dem_mode == "satellite_only":
        head = [
            LOADING_PROJECT, SELECTING_DEM_MODE, FETCHING_SATELLITE_DEM,
            REPROJECTING_SATELLITE_DEM, TERRAIN_QUALITY_CHECKS,
        ]
    elif dem_mode == "drone_only":
        head = [
            LOADING_PROJECT, RESOLVING_DTM, VALIDATING_DTM, SELECTING_DEM_MODE,
            COMPUTING_DRONE_COVERAGE, PREPARING_DRONE_DEM,
            TERRAIN_QUALITY_CHECKS,
        ]
    elif dem_mode == "fused":
        head = [
            LOADING_PROJECT, RESOLVING_DTM, VALIDATING_DTM, SELECTING_DEM_MODE,
            FETCHING_SATELLITE_DEM, REPROJECTING_SATELLITE_DEM,
            COMPUTING_DRONE_COVERAGE, FUSING_DEM, TERRAIN_QUALITY_CHECKS,
        ]
    else:  # auto / unknown — provisional superset
        head = [
            LOADING_PROJECT, RESOLVING_DTM, VALIDATING_DTM, SELECTING_DEM_MODE,
            COMPUTING_DRONE_COVERAGE, PREPARING_DRONE_DEM,
            TERRAIN_QUALITY_CHECKS,
        ]
    return head + common_tail


# ---------------------------------------------------------------------------
# Reporter


class AnalysisCancelled(RuntimeError):
    """Raised at a stage boundary when a cooperative cancel was requested."""


class ProgressReporter:
    """Drives one analysis run's structured progress + heartbeat.

    Bound to a single ``run_id``; every write targets that row only. Not a
    module global — construct one per run and pass it explicitly.
    """

    def __init__(self, run_id: str, dem_mode: str = "auto",
                 rq_job_id: str | None = None,
                 extra_progress: Callable[[str], None] | None = None):
        self.run_id = run_id
        self._plan = build_stage_plan(dem_mode)
        self._completed: list[str] = []
        self._current: str | None = None
        self._last_percent = 0.0
        self._extra = extra_progress
        self._lock = threading.Lock()
        self._hb_stop = threading.Event()
        self._hb_thread: threading.Thread | None = None
        self._interval = _heartbeat_interval()
        worker = rq_job_id or os.environ.get("HOSTNAME") or "inline"
        db.update_analysis_run(
            run_id, state="running", stage=QUEUED,
            stage_label=stage_label(QUEUED), started_at=time.time(),
            heartbeat_at=time.time(), stage_count=len(self._plan),
            stage_index=0, progress_percent=0.0,
            stage_plan_json=self._plan, rq_job_id=rq_job_id,
            worker_name=worker, terrain_source=None,
            analysis_version=ANALYSIS_VERSION, error_code=None,
            error_message=None)

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> "ProgressReporter":
        """Begin the background heartbeat. Idempotent."""
        if self._hb_thread is None:
            self._hb_thread = threading.Thread(
                target=self._heartbeat_loop, name=f"hb-{self.run_id}",
                daemon=True)
            self._hb_thread.start()
        return self

    def close(self) -> None:
        """Stop the heartbeat cleanly. Always call from a ``finally`` block."""
        self._hb_stop.set()
        t = self._hb_thread
        if t is not None:
            t.join(timeout=2.0)
            self._hb_thread = None

    def __enter__(self) -> "ProgressReporter":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- mode / plan -------------------------------------------------------
    def set_mode(self, dem_mode: str, terrain_source: str | None = None) -> None:
        """Swap the stage plan to the resolved DEM mode. Completed stages are
        preserved; progress stays monotonic."""
        with self._lock:
            self._plan = build_stage_plan(dem_mode)
            self._completed = [s for s in self._completed if s in self._plan]
            src = terrain_source or dem_mode
            db.update_analysis_run(
                self.run_id, dem_mode=dem_mode, terrain_source=src,
                stage_plan_json=self._plan, stage_count=len(self._plan))
            self._recompute_locked(self._current)

    # -- stage transitions -------------------------------------------------
    def start_stage(self, stage: str, message: str | None = None) -> None:
        self._check_cancel()
        with self._lock:
            # entering a new stage implicitly completes the previous one, so
            # the pipeline only needs to announce stage starts
            if (self._current and self._current != stage
                    and self._current not in self._completed):
                self._completed.append(self._current)
            self._current = stage
            msg = message or stage_label(stage)
            self._recompute_locked(stage, message=msg)
        db.append_run_log(self.run_id, message or stage_label(stage),
                          stage=stage)
        if self._extra:
            self._extra(message or stage_label(stage))

    def complete_stage(self, stage: str, message: str | None = None) -> None:
        with self._lock:
            if stage not in self._completed:
                self._completed.append(stage)
            self._recompute_locked(self._current, message=message)

    def heartbeat(self, message: str | None = None) -> None:
        """Prove liveness without advancing progress."""
        fields = {"heartbeat_at": time.time()}
        if message:
            fields["current_message"] = message
        db.update_analysis_run(self.run_id, **fields)
        if message:
            db.append_run_log(self.run_id, message, level="debug",
                              stage=self._current)
        if self._extra and message:
            self._extra(message)

    def warning(self, code: str, message: str) -> None:
        with self._lock:
            run = db.get_analysis_run(self.run_id) or {}
            warnings = run.get("warnings_json") or []
            warnings.append({"code": code, "message": message,
                             "t": time.time()})
            db.update_analysis_run(self.run_id, warnings_json=warnings)
        db.append_run_log(self.run_id, f"{code}: {message}", level="warning",
                          stage=self._current)

    def fail(self, code: str, message: str) -> None:
        db.update_analysis_run(
            self.run_id, state="failed", error_code=code,
            error_message=message, completed_at=time.time(),
            current_message=message)
        db.append_run_log(self.run_id, f"{code}: {message}", level="error",
                          stage=self._current)

    def complete(self, state: str = "completed",
                 message: str | None = None) -> None:
        with self._lock:
            if COMPLETED not in self._completed:
                self._completed.append(COMPLETED)
            self._current = COMPLETED
            pct = 100.0
            self._last_percent = pct
            db.update_analysis_run(
                self.run_id, state=state, stage=COMPLETED,
                stage_label=stage_label(COMPLETED),
                stage_index=len(self._plan), progress_percent=pct,
                current_message=message or stage_label(COMPLETED),
                completed_at=time.time(), heartbeat_at=time.time())
        db.append_run_log(self.run_id, message or "Analysis complete",
                          stage=COMPLETED)

    # -- cancellation ------------------------------------------------------
    def _check_cancel(self) -> None:
        if db.run_cancel_requested(self.run_id):
            raise AnalysisCancelled("Analysis was cancelled by request.")

    # -- internals ---------------------------------------------------------
    def _recompute_locked(self, current: str | None,
                          message: str | None = None) -> None:
        n = len(self._plan) or 1
        done = len([s for s in self._completed if s != COMPLETED])
        pct = min(100.0, round(100.0 * done / n, 1))
        pct = max(pct, self._last_percent)  # never decrease
        self._last_percent = pct
        try:
            idx = self._plan.index(current) + 1 if current in self._plan \
                else done
        except ValueError:
            idx = done
        db.update_analysis_run(
            self.run_id, stage=current, stage_label=stage_label(current),
            stage_index=idx, stage_count=len(self._plan),
            progress_percent=pct, current_message=message or stage_label(current),
            heartbeat_at=time.time())

    def _heartbeat_loop(self) -> None:
        while not self._hb_stop.wait(self._interval):
            try:
                run = db.get_analysis_run(self.run_id)
                if run is None or run.get("state") in TERMINAL_STATES:
                    return
                started = run.get("started_at") or time.time()
                elapsed = int(time.time() - started)
                db.update_analysis_run(self.run_id, heartbeat_at=time.time())
                log.debug("heartbeat run=%s stage=%s elapsed=%ss",
                          self.run_id, self._current, elapsed)
            except Exception:  # noqa: BLE001 — a heartbeat must never crash a run
                log.debug("heartbeat tick failed for run %s", self.run_id,
                          exc_info=True)


# ---------------------------------------------------------------------------
# Config thresholds


def _heartbeat_interval() -> int:
    from . import config
    return config._int("ANALYSIS_HEARTBEAT_INTERVAL_SECONDS", 15)


def stale_warning_seconds() -> int:
    from . import config
    return config._int("ANALYSIS_STALE_WARNING_SECONDS", 90)


def stalled_seconds() -> int:
    from . import config
    return config._int("ANALYSIS_STALLED_SECONDS", 300)


# ---------------------------------------------------------------------------
# Health classification


def classify_health(run: dict, worker_status: str | None = None,
                    now: float | None = None) -> str:
    """Honest health verdict from persisted heartbeat age + RQ/worker status.

    Returns one of: ``complete``, ``failed``, ``active``, ``slow``,
    ``possibly_stalled``, ``worker_missing``.

    A heavy Whitebox/GDAL operation that produces no fine-grained output is
    never itself a failure — it shows ``slow`` / ``possibly_stalled`` while the
    worker still exists, and only ``worker_missing`` when the RQ job/worker has
    actually disappeared.
    """
    now = now or time.time()
    state = run.get("state")
    if state in ("completed", "completed_with_warnings"):
        return "complete"
    if state == "failed":
        return "failed"
    if state == "cancelled":
        return "failed"

    hb = run.get("heartbeat_at") or run.get("started_at")
    age = (now - hb) if hb else None

    # If the queue backend says the job is gone while we still think we're
    # running, the worker died — this is distinguishable from slow processing.
    if worker_status in ("missing", "failed", "canceled", "stopped"):
        return "worker_missing"

    if age is None:
        return "active"
    if age <= stale_warning_seconds():
        return "active"
    if age <= stalled_seconds():
        return "slow"
    # No heartbeat for a long time. If we can confirm the RQ job is still
    # started/queued, it is a slow operation, not a dead worker.
    if worker_status in ("started", "queued", "deferred", "scheduled"):
        return "possibly_stalled"
    return "possibly_stalled" if worker_status is None else "worker_missing"


def health_message(health: str, seconds_since_heartbeat: int | None) -> str | None:
    if health == "slow":
        return ("The current terrain operation is taking a while but the "
                "worker is still reporting in.")
    if health == "possibly_stalled":
        mins = (seconds_since_heartbeat or 0) // 60
        return (f"No heartbeat for {mins} minute(s). The worker job still "
                "exists and may be processing a slow terrain operation.")
    if health == "worker_missing":
        return ("The analysis worker is no longer running. Retry the "
                "analysis.")
    return None
