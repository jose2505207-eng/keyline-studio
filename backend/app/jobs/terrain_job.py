"""Terrain analysis stage for a completed photogrammetry survey.

Reuses the existing keyline pipeline; the DEM mode (drone_only vs fused) is
selected inside run_pipeline from actual AOI coverage.
"""

from __future__ import annotations

import logging
import os

from .. import db
from ..pipeline import run_pipeline

log = logging.getLogger(__name__)


def run_terrain_for_survey(survey_id: str) -> dict:
    survey = db.get_survey(survey_id)
    if survey is None:
        raise RuntimeError(f"survey {survey_id} not found")
    project = db.get_project(survey["project_id"])
    if project is None:
        raise RuntimeError(f"project {survey['project_id']} not found")
    dtm = survey.get("dtm_path")
    if not dtm or not os.path.isfile(dtm):
        raise RuntimeError("survey has no validated DTM to analyze")

    data_dir = os.environ.get(
        "KEYLINE_DATA",
        os.path.join(os.path.dirname(__file__), "..", "..", "data"),
    )
    project_dir = os.path.join(data_dir, project["id"])

    def progress(step: str) -> None:
        db.update_survey(survey_id, stage=f"keyline analysis: {step}")

    # register the DTM on the project so re-analysis from the UI reuses it
    db.set_drone_path(project["id"], dtm)
    fc = run_pipeline(project_dir, project["aoi"], drone_path=dtm,
                      progress=progress)
    return fc
