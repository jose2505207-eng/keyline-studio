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
from ..pipeline import params_from_dict, run_pipeline

log = logging.getLogger(__name__)


def data_dir() -> str:
    return os.environ.get(
        "KEYLINE_DATA",
        os.path.join(os.path.dirname(__file__), "..", "..", "data"),
    )


def run_output_dir(project_id: str, run_id: str) -> str:
    return os.path.join(data_dir(), project_id, "analysis", run_id)


def execute_analysis_run(run_id: str,
                         extra_progress: Callable[[str], None] | None = None) -> dict:
    """Run the pipeline for one analysis_runs record. Reuses the stored DTM;
    never talks to the photogrammetry provider."""
    run = db.get_analysis_run(run_id)
    if run is None:
        raise RuntimeError(f"analysis run {run_id} not found")
    project = db.get_project(run["project_id"])
    if project is None:
        raise RuntimeError(f"project {run['project_id']} not found")

    lg = logging.LoggerAdapter(log, {"project_id": project["id"],
                                     "survey_id": run.get("survey_id"),
                                     "analysis_run_id": run_id})

    def progress(step: str) -> None:
        db.update_analysis_run(run_id, stage=step)
        if extra_progress:
            extra_progress(step)

    dem_path = run.get("dem_path")
    if dem_path and not os.path.isfile(dem_path):
        db.update_analysis_run(run_id, state="failed",
                               error_message=f"DTM missing: {dem_path}",
                               completed_at=time.time())
        raise RuntimeError(f"DTM for run {run_id} missing on disk: {dem_path}")

    survey = db.get_survey(run["survey_id"]) if run.get("survey_id") else None
    gcp_supplied = bool(survey and survey.get("gcp_key"))
    params = run.get("params_json") or {}
    out_dir = run_output_dir(project["id"], run_id)
    project_dir = os.path.join(data_dir(), project["id"])

    db.update_analysis_run(run_id, state="running", stage="starting",
                           result_dir=out_dir)
    try:
        fc = run_pipeline(
            project_dir, project["aoi"],
            drone_path=dem_path,
            progress=progress,
            params=params_from_dict(params.get("terrain")),
            dem_mode=params.get("dem_mode", "auto"),
            out_dir=out_dir,
            survey_id=run.get("survey_id"),
            analysis_run_id=run_id,
            gcp_supplied=gcp_supplied,
            satellite_qa=config._bool("QA_SATELLITE_CROSSCHECK", True),
        )
    except Exception as exc:
        lg.exception("analysis run failed")
        db.update_analysis_run(run_id, state="failed",
                               error_message=str(exc),
                               completed_at=time.time())
        raise

    props = fc.get("properties", {})
    db.update_analysis_run(
        run_id,
        state="completed", stage="complete", completed_at=time.time(),
        dem_mode=props.get("dem_mode"),
        counts_json=props.get("counts") or {},
        notices_json=props.get("notices") or [],
        qa_json=props.get("qa"),
        error_message=None,
    )
    lg.info("analysis run completed: %s", props.get("counts"))
    return fc


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
    run_id = db.create_analysis_run(project["id"], survey_id, dtm,
                                    {"trigger": "survey", "dem_mode": "auto"})

    def survey_stage(step: str) -> None:
        db.update_survey(survey_id, stage=f"keyline analysis: {step}")

    return execute_analysis_run(run_id, extra_progress=survey_stage)
