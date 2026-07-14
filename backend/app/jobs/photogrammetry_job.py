"""The durable photogrammetry worker job.

Runs in the RQ worker process. Every step re-reads survey state from SQLite
so that cancellation and restarts behave correctly, and the external
provider task id is persisted the moment it exists so retries never create a
duplicate NodeODM task.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time

from .. import config, db
from ..photogrammetry import (
    ProviderError,
    ProviderTaskFailed,
    ProviderTaskNotFound,
    ProviderUnavailable,
    TaskState,
    build_task_options,
    get_provider,
)
from ..preflight import PreflightError, run_preflight
from ..storage import get_storage

log = logging.getLogger(__name__)

ACTIVE_STATES = [
    "queued", "preflight", "submitting", "provider_queued",
    "provider_running", "downloading", "validating", "terrain_queued",
    "terrain_running",
]
TERMINAL_STATES = ["completed", "failed", "cancelled"]

# Honest stage inference from ODM console output. A stage is only reported
# once a matching log line has actually been seen.
_STAGE_KEYWORDS = [
    ("generating orthophoto", ("odm_orthophoto", "orthophoto")),
    ("generating DTM", ("odm_dem", "dem generation", "dtm")),
    ("classifying ground", ("smrf", "classify", "ground classification")),
    ("generating point cloud", ("openmvs", "point cloud", "georeferencing",
                                "odm_filterpoints")),
    ("creating reconstruction", ("opensfm", "reconstruction", "matching",
                                 "features")),
]


class SurveyCancelled(Exception):
    pass


def _survey(sid: str) -> dict:
    s = db.get_survey(sid)
    if s is None:
        raise RuntimeError(f"survey {sid} vanished")
    return s


def _set(sid: str, **fields) -> None:
    db.update_survey(sid, **fields)


def _check_cancel(sid: str) -> None:
    if _survey(sid).get("cancel_requested"):
        raise SurveyCancelled()


def _photogrammetry_dir(project_id: str) -> str:
    data_dir = os.environ.get(
        "KEYLINE_DATA",
        os.path.join(os.path.dirname(__file__), "..", "..", "data"),
    )
    return os.path.join(data_dir, project_id, "photogrammetry")


def _infer_stage(output_lines: list[str], fallback: str) -> str:
    text = "\n".join(output_lines[-40:]).lower()
    for stage, keywords in _STAGE_KEYWORDS:
        if any(k in text for k in keywords):
            return stage
    return fallback


def _materialize_images(survey: dict, workdir: str) -> list[tuple[str, str]]:
    """Download every uploaded object into workdir. Filenames on disk are
    index-prefixed sanitized originals (ODM logs reference them), never raw
    user paths."""
    storage = get_storage()
    pairs = []
    for i, img in enumerate(survey["images_json"]):
        original = os.path.basename(img.get("filename") or f"img{i}.jpg")
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in original)
        local = os.path.join(workdir, f"{i:04d}_{safe}")
        storage.download_to(img["key"], local)
        pairs.append((original, local))
    return pairs


def run_survey(survey_id: str) -> None:
    survey = _survey(survey_id)
    sid = survey_id
    pid = survey["project_id"]
    lg = logging.LoggerAdapter(log, {"project_id": pid, "survey_id": sid,
                                     "external_task_id":
                                     survey.get("external_task_id")})
    workdir = tempfile.mkdtemp(prefix=f"keyline-survey-{sid}-")
    out_dir = _photogrammetry_dir(pid)
    os.makedirs(out_dir, exist_ok=True)
    log_path = os.path.join(out_dir, "provider-output.log")
    started = survey.get("started_at") or time.time()
    _set(sid, started_at=started)

    try:
        provider = get_provider()
        external_id = survey.get("external_task_id")

        if not external_id:
            # ---- preflight -------------------------------------------------
            _check_cancel(sid)
            _set(sid, state="preflight", stage="validating photographs")
            images = _materialize_images(survey, workdir)
            gcp_path = None
            if survey.get("gcp_key"):
                gcp_path = os.path.join(workdir, "gcp_list.txt")
                get_storage().download_to(survey["gcp_key"], gcp_path)
            pre = run_preflight(images, min_images=config.drone_min_images(),
                                has_gcp=bool(gcp_path))
            _set(sid, preflight_json=pre.to_dict(),
                 warnings_json=pre.warnings)

            # ---- submit ----------------------------------------------------
            _check_cancel(sid)
            _set(sid, state="submitting", stage="submitting to processing node")
            health = provider.health()
            if not health.ok:
                raise ProviderUnavailable(
                    f"Processing node unavailable: {health.error}")
            supported = None
            if hasattr(provider, "supported_options"):
                supported = provider.supported_options()
            options = build_task_options(survey.get("options_json") or {},
                                         supported)

            def upload_progress(pct: float) -> None:
                db.update_survey(sid, progress_percent=round(float(pct), 1))

            task = provider.create_task(
                [p for _, p in images],
                task_name=f"keyline-{pid}-{sid}",
                options=options,
                gcp_path=gcp_path,
                progress_callback=upload_progress,
            )
            external_id = task.external_task_id
            # persist immediately: retries must never create a second task
            _set(sid, external_task_id=external_id, options_json=options,
                 state="provider_queued", stage="queued for photogrammetry",
                 progress_percent=0)
            lg.info("created provider task %s", external_id)
        else:
            lg.info("resuming existing provider task %s", external_id)

        # ---- poll ----------------------------------------------------------
        poll = max(config.provider_poll_seconds(), 2)
        while True:
            _check_cancel(sid)
            try:
                status = provider.get_task(external_id)
            except ProviderUnavailable as exc:
                lg.warning("provider poll failed, retrying: %s", exc)
                time.sleep(poll)
                continue

            output = provider.get_output(external_id, tail=200)
            if output:
                with open(log_path, "w") as f:
                    f.write("\n".join(output) + "\n")
            _set(sid,
                 provider_status_json={
                     "state": status.state.value,
                     "progress": status.progress_percent,
                     "processing_time_ms": status.processing_time_ms,
                     "last_error": status.last_error,
                 },
                 progress_percent=status.progress_percent)

            if status.state == TaskState.QUEUED:
                _set(sid, state="provider_queued",
                     stage="queued for photogrammetry")
            elif status.state == TaskState.RUNNING:
                _set(sid, state="provider_running",
                     stage=_infer_stage(output, "processing"))
            elif status.state == TaskState.COMPLETED:
                break
            elif status.state == TaskState.CANCELLED:
                raise SurveyCancelled()
            elif status.state == TaskState.FAILED:
                raise ProviderTaskFailed(
                    status.last_error or "processing failed on the node")
            time.sleep(poll)

        # ---- download + validate ------------------------------------------
        _check_cancel(sid)
        _set(sid, state="downloading", stage="downloading terrain products",
             progress_percent=100)
        assets = provider.download_assets(external_id,
                                          os.path.join(workdir, "assets"))
        final_output = provider.get_output(external_id, tail=500)
        if final_output:
            with open(log_path, "w") as f:
                f.write("\n".join(final_output) + "\n")

        _set(sid, state="validating", stage="validating terrain products")
        from ..assets import normalize_and_validate_assets

        project = db.get_project(pid)
        manifest = normalize_and_validate_assets(
            assets=assets,
            out_dir=out_dir,
            aoi_geojson=project["aoi"],
            survey=_survey(sid),
            provider_health=provider.health(),
        )
        dtm_path = os.path.join(out_dir, "drone_dtm.tif")
        ortho_path = (os.path.join(out_dir, "orthophoto.tif")
                      if manifest["assets"]["orthophoto"] else None)
        manifest_path = os.path.join(out_dir, "manifest.json")
        _set(sid, dtm_path=dtm_path, orthophoto_path=ortho_path,
             manifest_path=manifest_path)

        # ---- terrain analysis ----------------------------------------------
        _check_cancel(sid)
        _set(sid, state="terrain_running", stage="running keyline analysis")
        from .terrain_job import run_terrain_for_survey

        run_terrain_for_survey(sid)

        _set(sid, state="completed", stage="complete",
             completed_at=time.time(), error_message=None)
        lg.info("survey completed")

    except SurveyCancelled:
        lg.info("survey cancelled")
        if _survey(sid).get("external_task_id"):
            try:
                get_provider().cancel_task(_survey(sid)["external_task_id"])
            except ProviderError as exc:
                lg.warning("provider-side cancel failed: %s", exc)
        _set(sid, state="cancelled", stage="cancelled",
             completed_at=time.time())
    except PreflightError as exc:
        lg.warning("preflight failed: %s", exc)
        _set(sid, state="failed", stage="validating photographs",
             error_message=str(exc), completed_at=time.time())
    except ProviderUnavailable as exc:
        lg.error("provider unavailable: %s", exc)
        _set(sid, state="failed", stage="submitting to processing node",
             error_message=str(exc), completed_at=time.time())
    except (ProviderTaskFailed, ProviderTaskNotFound, ProviderError) as exc:
        lg.error("provider error: %s", exc)
        _set(sid, state="failed", error_message=str(exc),
             completed_at=time.time())
    except Exception as exc:  # noqa: BLE001 — surface anything to the record
        lg.exception("survey job crashed")
        _set(sid, state="failed", error_message=f"internal error: {exc}",
             completed_at=time.time())
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def reconcile_stale_surveys(enqueue=None, stale_seconds: float = 120.0) -> list[str]:
    """Called on startup: surveys stranded in active states by a crash are
    resumed (an external task exists → re-enqueue polling) or returned to a
    recoverable state (no external task yet → back to `uploaded`). Never
    marks anything completed without evidence.

    A live worker touches the row every poll, so surveys updated within
    ``stale_seconds`` are still owned by a running job and must not be
    re-enqueued (that would start a duplicate polling job)."""
    if enqueue is None:
        from .queue import QueueUnavailable, enqueue_survey

        def enqueue(sid: str) -> None:  # type: ignore[misc]
            try:
                enqueue_survey(sid)
            except QueueUnavailable as exc:
                log.warning("cannot re-enqueue survey %s: %s", sid, exc)
                db.update_survey(sid, stage="waiting for worker queue")

    touched = []
    for survey in db.surveys_in_states(ACTIVE_STATES):
        sid = survey["id"]
        age = time.time() - (survey.get("updated_at") or 0)
        if age < stale_seconds:
            log.info("reconcile: survey %s updated %.0fs ago — a worker "
                     "still owns it, leaving alone", sid, age)
            continue
        if survey.get("external_task_id"):
            log.info("reconcile: resuming survey %s (task %s)", sid,
                     survey["external_task_id"])
            enqueue(sid)
        else:
            log.info("reconcile: returning survey %s to uploaded", sid)
            db.update_survey(
                sid, state="uploaded", stage=None, progress_percent=0,
                error_message="Processing was interrupted before submission; "
                              "start it again.")
        touched.append(sid)
    return touched
