"""Versioned terrain-analysis runs.

Terrain analysis is cheap and iterated often; photogrammetry is expensive
and never rerun from here. Every execution — whether triggered by a
completed survey, the legacy analyze endpoint, or POST /reanalyze — creates
an analysis_runs record and writes into its own directory
(data/<project_id>/analysis/<run_id>/), so runs never overwrite each other
and previous runs remain comparable.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable

from .. import config, db
from ..pipeline import (
    InsufficientCoverageError,
    params_from_dict,
    run_pipeline,
)

log = logging.getLogger(__name__)


def data_dir() -> str:
    return os.environ.get(
        "KEYLINE_DATA",
        os.path.join(os.path.dirname(__file__), "..", "..", "data"),
    )


def run_output_dir(project_id: str, run_id: str) -> str:
    return os.path.join(data_dir(), project_id, "analysis", run_id)


def _current_rq_job_id() -> str | None:
    """The RQ job id when executing inside an rq worker, else None (the
    in-process BackgroundTasks path used by /analyze)."""
    try:
        from rq import get_current_job

        job = get_current_job()
        return job.id if job is not None else None
    except Exception:  # noqa: BLE001 — rq not installed / not in a worker
        return None


def execute_analysis_run(run_id: str,
                         extra_progress: Callable[[str], None] | None = None) -> dict:
    """Run the pipeline for one analysis_runs record. Reuses the stored DTM;
    never talks to the photogrammetry provider.

    Drives structured, persisted progress via a ProgressReporter and, after a
    successful terrain analysis, generates the standing exports in the
    ``generating_exports`` stage. An export failure downgrades the run to
    ``completed_with_warnings`` — never ``failed``."""
    from .. import progress as prog

    run = db.get_analysis_run(run_id)
    if run is None:
        raise RuntimeError(f"analysis run {run_id} not found")
    project = db.get_project(run["project_id"])
    if project is None:
        raise RuntimeError(f"project {run['project_id']} not found")

    lg = logging.LoggerAdapter(log, {"project_id": project["id"],
                                     "survey_id": run.get("survey_id"),
                                     "analysis_run_id": run_id})

    params = run.get("params_json") or {}
    out_dir = run_output_dir(project["id"], run_id)
    project_dir = os.path.join(data_dir(), project["id"])
    dem_path = run.get("dem_path")
    rq_job_id = _current_rq_job_id()

    # Duplicate-worker guard: only one worker may execute a given run. A stale
    # claim (dead worker, no recent heartbeat) can be taken over.
    import socket

    worker_token = rq_job_id or f"{socket.gethostname()}:{os.getpid()}"
    if not db.claim_analysis_run(run_id, worker_token):
        lg.warning("analysis run %s already claimed by another worker — "
                   "skipping duplicate execution", run_id)
        return db.get_analysis_run(run_id) or {}

    reporter = prog.ProgressReporter(
        run_id, dem_mode=params.get("dem_mode", "auto"),
        rq_job_id=rq_job_id, extra_progress=extra_progress,
        terrain_source=params.get("terrain_source"))
    db.update_analysis_run(run_id, result_dir=out_dir,
                           fill_missing_with_satellite=int(bool(
                               params.get("fill_missing_areas_with_satellite"))))
    reporter.start()
    try:
        reporter.start_stage(prog.LOADING_PROJECT, "loading project + AOI")
        if dem_path:
            reporter.start_stage(prog.RESOLVING_DTM, "resolving source DTM")
            reporter.start_stage(prog.VALIDATING_DTM, "validating source DTM")
            if not os.path.isfile(dem_path):
                reporter.fail("DTM_FILE_MISSING",
                              f"The source DTM file is missing: "
                              f"{os.path.basename(dem_path)}")
                raise RuntimeError(f"DTM for run {run_id} missing: {dem_path}")

        survey = db.get_survey(run["survey_id"]) if run.get("survey_id") else None
        gcp_supplied = bool(survey and survey.get("gcp_key"))

        fc = run_pipeline(
            project_dir, project["aoi"],
            drone_path=dem_path,
            params=params_from_dict(params.get("terrain")),
            dem_mode=params.get("dem_mode", "auto"),
            out_dir=out_dir,
            survey_id=run.get("survey_id"),
            analysis_run_id=run_id,
            gcp_supplied=gcp_supplied,
            satellite_qa=config._bool("QA_SATELLITE_CROSSCHECK", True),
            fill_missing_areas_with_satellite=bool(
                params.get("fill_missing_areas_with_satellite")),
            reporter=reporter,
        )

        props = fc.get("properties", {})
        # persist terrain results before exports so a partial export can never
        # lose the completed analysis
        db.update_analysis_run(
            run_id, dem_mode=props.get("dem_mode"),
            counts_json=props.get("counts") or {},
            notices_json=props.get("notices") or [],
            qa_json=props.get("qa"), error_message=None, error_code=None)

        # --- exports (optional; failure => completed_with_warnings) ----------
        from .. import exports as exports_mod

        reporter.start_stage(prog.GENERATING_EXPORTS, "creating downloads")
        try:
            avail = exports_mod.generate_run_exports(
                out_dir, fc, aoi_wgs84=project["aoi"])
        except Exception as exc:  # noqa: BLE001 — must not fail the analysis
            lg.warning("export generation failed: %s", exc)
            avail = {"errors": {"exports": str(exc)}}
        db.update_analysis_run(run_id, exports_json=avail)

        reporter.start_stage(prog.SAVING_RESULTS, "saving results")

        # The terrain analysis itself succeeded. Per the error-behaviour spec,
        # `completed_with_warnings` means an *optional export* could not be
        # produced — QA/terrain warnings are surfaced via counts/notices/qa and
        # the result's own status, not by demoting the run.
        state = "completed_with_warnings" if avail.get("errors") else "completed"
        reporter.complete(state=state,
                          message="Analysis complete" if state == "completed"
                          else "Analysis complete (an optional export could "
                          "not be generated)")
        lg.info("analysis run %s: %s", state, props.get("counts"))
        return fc
    except prog.AnalysisCancelled:
        db.update_analysis_run(run_id, state="cancelled",
                               error_code="CANCELLED",
                               error_message="Analysis was cancelled.",
                               completed_at=time.time())
        _cleanup_temp_files(out_dir)
        lg.info("analysis run cancelled")
        raise
    except InsufficientCoverageError as exc:
        # Honest, actionable failure — never a silent satellite fallback.
        lg.info("analysis run stopped: DTM coverage %.1f%% insufficient",
                exc.coverage * 100)
        cur = db.get_analysis_run(run_id) or {}
        if cur.get("state") not in prog.TERMINAL_STATES:
            reporter.fail(exc.code, str(exc))
            db.update_analysis_run(run_id, completed_at=time.time())
        _cleanup_temp_files(out_dir)
        raise
    except Exception as exc:
        lg.exception("analysis run failed")
        cur = db.get_analysis_run(run_id) or {}
        if cur.get("state") not in prog.TERMINAL_STATES:
            reporter.fail("ANALYSIS_FAILED", str(exc))
            db.update_analysis_run(run_id, completed_at=time.time())
        _cleanup_temp_files(out_dir)
        raise
    finally:
        reporter.close()


def _cleanup_temp_files(out_dir: str) -> None:
    """Remove partial/temporary artifacts from an aborted run (atomic writers
    leave *.tmp behind only if killed mid-write)."""
    import glob

    try:
        for pattern in ("*.tmp", "*.zip.tmp", "**/*.tmp"):
            for p in glob.glob(os.path.join(out_dir, pattern), recursive=True):
                try:
                    os.remove(p)
                except OSError:
                    pass
    except Exception:  # noqa: BLE001 — cleanup is best-effort
        pass


def run_analysis_job(run_id: str) -> None:
    """RQ entry point for POST /reanalyze."""
    execute_analysis_run(run_id)


def run_terrain_for_survey(survey_id: str) -> dict:
    """Terrain stage after photogrammetry: registers the validated DTM on
    the project and executes a fresh analysis run against it."""
    survey = db.get_survey(survey_id)
    if survey is None:
        raise RuntimeError(f"survey {survey_id} not found")
    project = db.get_project(survey["project_id"])
    if project is None:
        raise RuntimeError(f"project {survey['project_id']} not found")
    dtm = survey.get("dtm_path")
    if not dtm or not os.path.isfile(dtm):
        raise RuntimeError("survey has no validated DTM to analyze")

    # register the DTM on the project so re-analysis from the UI reuses it
    db.set_drone_path(project["id"], dtm)
    # A drone survey may opt in to satellite gap-filling via its options; the
    # provenance is the drone DTM regardless of the resolved engine mode.
    options = survey.get("options_json") or {}
    run_id = db.create_analysis_run(
        project["id"], survey_id, dtm,
        {"trigger": "survey", "dem_mode": "auto",
         "terrain_source": "drone_only",
         "fill_missing_areas_with_satellite":
             bool(options.get("fill_missing_areas_with_satellite"))})

    def survey_stage(step: str) -> None:
        db.update_survey(survey_id, stage=f"keyline analysis: {step}")

    return execute_analysis_run(run_id, extra_progress=survey_stage)
