"""Drone-survey API: create → presign → upload → complete → start → poll.

Uploads go browser → storage directly with presigned PUTs; this API never
proxies image bytes (except the tiny GCP text file and the local-dev
backend's PUT endpoint in main.py). Object keys are UUID-generated
server-side; every nested route checks survey/project ownership.
"""

from __future__ import annotations

import json
import logging
import os
import uuid

from fastapi import APIRouter, HTTPException, UploadFile

from . import config, db
from .jobs import TERMINAL_STATES, QueueUnavailable, enqueue_survey
from .photogrammetry import get_provider
from .preflight import PreflightError, validate_gcp_text
from .schemas import (
    CompleteUploadOut,
    PhotogrammetryHealthOut,
    PresignedUploadOut,
    PresignRequestIn,
    ProviderTaskInfoOut,
    SimpleOk,
    StartOut,
    SurveyCreateIn,
    SurveyListOut,
    SurveyOut,
    SurveyPlanOut,
)
from .storage import get_storage

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects/{pid}/drone-surveys",
                   tags=["drone-surveys"])

STARTABLE_STATES = {"uploaded", "failed", "cancelled"}


def _require_project(pid: str) -> dict:
    proj = db.get_project(pid)
    if proj is None:
        raise HTTPException(404, "Project not found")
    return proj


def _require_survey(pid: str, sid: str) -> dict:
    _require_project(pid)
    survey = db.get_survey(sid)
    if survey is None or survey["project_id"] != pid:
        raise HTTPException(404, "Survey not found in this project")
    return survey


def _presigned_for(survey: dict, keys: set[str] | None) -> list[PresignedUploadOut]:
    storage = get_storage()
    expiry = config.s3_presign_expiry_seconds()
    out = []
    for img in survey["images_json"]:
        if keys is not None and img["key"] not in keys:
            continue
        p = storage.presign_put(img["key"], img.get("type", "image/jpeg"), expiry)
        out.append(PresignedUploadOut(
            key=p.key, url=p.url, headers=p.headers, method=p.method,
            filename=img["filename"], size=img["size"]))
    return out


def _survey_out(survey: dict) -> SurveyOut:
    manifest = None
    if survey.get("manifest_path") and os.path.isfile(survey["manifest_path"]):
        try:
            with open(survey["manifest_path"]) as f:
                manifest = json.load(f)
        except (OSError, json.JSONDecodeError):
            manifest = None
    pt = survey.get("provider_status_json") or None
    return SurveyOut(
        id=survey["id"],
        project_id=survey["project_id"],
        provider=survey["provider"],
        external_task_id=survey.get("external_task_id"),
        state=survey["state"],
        stage=survey.get("stage"),
        progress_percent=float(survey.get("progress_percent") or 0),
        image_count=survey["image_count"],
        uploaded_count=survey["uploaded_count"],
        total_bytes=survey["total_bytes"],
        warnings=survey.get("warnings_json") or [],
        error_message=survey.get("error_message"),
        cancel_requested=bool(survey.get("cancel_requested")),
        preflight=survey.get("preflight_json"),
        provider_task=ProviderTaskInfoOut(**pt) if isinstance(pt, dict) else None,
        gcp_supplied=bool(survey.get("gcp_key")),
        dtm_available=bool(survey.get("dtm_path")
                           and os.path.isfile(survey["dtm_path"])),
        orthophoto_available=bool(survey.get("orthophoto_path")
                                  and os.path.isfile(survey["orthophoto_path"])),
        manifest=manifest,
        created_at=survey["created_at"],
        started_at=survey.get("started_at"),
        completed_at=survey.get("completed_at"),
        updated_at=survey["updated_at"],
    )


# ---------------------------------------------------------------------------


@router.post("", response_model=SurveyPlanOut)
def create_survey(pid: str, body: SurveyCreateIn):
    _require_project(pid)
    n = len(body.images)
    if n == 0:
        raise HTTPException(422, "No images in the upload plan")
    if n > config.drone_max_images():
        raise HTTPException(
            422, f"Too many images: {n} (maximum {config.drone_max_images()})")
    allowed = config.drone_allowed_extensions()
    total = 0
    images = []
    for meta in body.images:
        base = os.path.basename(meta.filename)  # strip any path components
        ext = os.path.splitext(base)[1].lower()
        if ext not in allowed:
            raise HTTPException(
                422, f"'{base}': only {', '.join(allowed)} files are accepted")
        if meta.type not in ("image/jpeg", "image/jpg"):
            raise HTTPException(422, f"'{base}': MIME type must be image/jpeg")
        if meta.size > config.drone_max_file_bytes():
            raise HTTPException(
                422, f"'{base}' is {meta.size} bytes; per-file limit is "
                     f"{config.drone_max_file_bytes()}")
        total += meta.size
        images.append({
            # UUID key — the user filename is metadata only, never a path
            "key": f"uploads/{pid}/__SID__/{uuid.uuid4().hex}{ext}",
            "filename": base,
            "type": "image/jpeg",
            "size": meta.size,
            "uploaded": False,
        })
    if total > config.drone_max_total_bytes():
        raise HTTPException(
            422, f"Total upload is {total} bytes; limit is "
                 f"{config.drone_max_total_bytes()}")

    sid = db.create_survey(pid, images, body.options, total_bytes=total)
    for img in images:
        img["key"] = img["key"].replace("__SID__", sid)
    db.update_survey(sid, images_json=images, state="uploading")
    survey = db.get_survey(sid)
    return SurveyPlanOut(
        survey_id=sid,
        uploads=_presigned_for(survey, None),
        min_images=config.drone_min_images(),
        max_images=config.drone_max_images(),
        max_file_bytes=config.drone_max_file_bytes(),
        max_total_bytes=config.drone_max_total_bytes(),
        upload_concurrency=config.drone_upload_concurrency(),
    )


@router.post("/{sid}/presign", response_model=list[PresignedUploadOut])
def refresh_presigned(pid: str, sid: str, body: PresignRequestIn):
    survey = _require_survey(pid, sid)
    keys = set(body.keys) if body.keys else None
    if keys is not None:
        known = {img["key"] for img in survey["images_json"]}
        foreign = keys - known
        if foreign:
            raise HTTPException(
                422, "Unknown object keys for this survey: "
                     + ", ".join(sorted(foreign)[:5]))
    return _presigned_for(survey, keys)


@router.post("/{sid}/complete-upload", response_model=CompleteUploadOut)
def complete_upload(pid: str, sid: str):
    survey = _require_survey(pid, sid)
    if survey["state"] not in ("uploading", "created", "uploaded"):
        raise HTTPException(409, f"Survey is {survey['state']}; cannot "
                                 "finalize uploads now")
    storage = get_storage()
    missing, mismatch = [], []
    uploaded = 0
    images = survey["images_json"]
    for img in images:
        if not storage.exists(img["key"]):
            missing.append(img["filename"])
            img["uploaded"] = False
            continue
        actual = storage.size(img["key"])
        if actual != img["size"]:
            mismatch.append(f"{img['filename']} ({actual} != {img['size']})")
            img["uploaded"] = False
            continue
        img["uploaded"] = True
        uploaded += 1
    ok = not missing and not mismatch
    db.update_survey(sid, images_json=images, uploaded_count=uploaded,
                     state="uploaded" if ok else "uploading",
                     progress_percent=100.0 * uploaded / max(len(images), 1))
    return CompleteUploadOut(ok=ok, uploaded_count=uploaded,
                             missing=missing[:25], size_mismatch=mismatch[:25])


@router.post("/{sid}/gcp", response_model=SimpleOk)
async def upload_gcp(pid: str, sid: str, file: UploadFile):
    survey = _require_survey(pid, sid)
    data = await file.read()
    if len(data) > 1 << 20:
        raise HTTPException(422, "GCP file larger than 1 MB")
    try:
        validate_gcp_text(data)
    except PreflightError as exc:
        raise HTTPException(422, f"Invalid GCP file: {exc}")
    key = f"uploads/{pid}/{sid}/gcp_list.txt"
    get_storage().put_bytes(key, data, "text/plain")
    db.update_survey(sid, gcp_key=key)
    return SimpleOk(ok=True, detail="GCP file registered")


@router.post("/{sid}/start", response_model=StartOut)
def start_survey(pid: str, sid: str):
    survey = _require_survey(pid, sid)
    if survey["state"] not in STARTABLE_STATES:
        raise HTTPException(
            409, f"Survey is {survey['state']}; finish uploading first"
            if survey["state"] in ("created", "uploading")
            else f"Survey is already {survey['state']}")
    if survey["uploaded_count"] < len(survey["images_json"]):
        raise HTTPException(409, "Not every image upload has been verified")
    try:
        job_id = enqueue_survey(sid)
    except QueueUnavailable as exc:
        raise HTTPException(503, str(exc))
    db.update_survey(sid, state="queued", stage="waiting for worker",
                     error_message=None, cancel_requested=0,
                     completed_at=None)
    return StartOut(ok=True, state="queued", queue_job_id=job_id)


@router.get("", response_model=SurveyListOut)
def list_surveys(pid: str):
    _require_project(pid)
    return SurveyListOut(surveys=[_survey_out(s) for s in db.list_surveys(pid)])


@router.get("/{sid}", response_model=SurveyOut)
def get_survey(pid: str, sid: str):
    return _survey_out(_require_survey(pid, sid))


@router.post("/{sid}/cancel", response_model=SimpleOk)
def cancel_survey(pid: str, sid: str):
    survey = _require_survey(pid, sid)
    if survey["state"] in TERMINAL_STATES:
        raise HTTPException(409, f"Survey already {survey['state']}")
    db.update_survey(sid, cancel_requested=1)
    detail = "Cancellation requested; the worker will stop at the next step"
    if survey.get("external_task_id"):
        try:
            if get_provider().cancel_task(survey["external_task_id"]):
                detail = "Cancellation requested on the processing node"
        except Exception as exc:  # noqa: BLE001 — cancel is best-effort here
            log.warning("provider cancel failed for %s: %s", sid, exc)
    return SimpleOk(ok=True, detail=detail)


@router.post("/{sid}/retry", response_model=StartOut)
def retry_survey(pid: str, sid: str):
    survey = _require_survey(pid, sid)
    if survey["state"] not in ("failed", "cancelled"):
        raise HTTPException(409, f"Survey is {survey['state']}; retry only "
                                 "applies to failed or cancelled surveys")
    # Idempotency: keep external_task_id — the worker resumes the existing
    # provider task instead of creating a duplicate. Clear it only when the
    # provider no longer knows the task.
    ext = survey.get("external_task_id")
    if ext:
        from .photogrammetry import ProviderError

        try:
            get_provider().get_task(ext)
        except ProviderError:
            db.update_survey(sid, external_task_id=None)
    try:
        job_id = enqueue_survey(sid)
    except QueueUnavailable as exc:
        raise HTTPException(503, str(exc))
    db.update_survey(sid, state="queued", stage="waiting for worker",
                     error_message=None, cancel_requested=0,
                     completed_at=None)
    return StartOut(ok=True, state="queued", queue_job_id=job_id)


# --------------------------------------------------------------------------
# Provider health (mounted without the project prefix)

health_router = APIRouter(tags=["photogrammetry"])


@health_router.get("/api/photogrammetry/health",
                   response_model=PhotogrammetryHealthOut)
def photogrammetry_health():
    provider = get_provider()
    h = provider.health()
    url = getattr(provider, "url", "")
    return PhotogrammetryHealthOut(
        provider=h.provider,
        configured_url=url,  # never includes the token
        reachable=h.ok,
        version=h.version,
        engine=h.engine,
        engine_version=h.engine_version,
        queue_count=h.queue_count,
        max_images=h.max_images,
        error=h.error,
    )
