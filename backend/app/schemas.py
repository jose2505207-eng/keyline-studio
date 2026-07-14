"""Typed request/response models for the drone-survey API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ImageMetaIn(BaseModel):
    filename: str
    type: str = "image/jpeg"
    size: int = Field(gt=0)
    lastModified: int | None = None


class SurveyCreateIn(BaseModel):
    images: list[ImageMetaIn]
    options: dict = Field(default_factory=dict)


class PresignedUploadOut(BaseModel):
    key: str
    url: str
    headers: dict[str, str]
    method: str = "PUT"
    filename: str
    size: int


class SurveyPlanOut(BaseModel):
    survey_id: str
    uploads: list[PresignedUploadOut]
    min_images: int
    max_images: int
    max_file_bytes: int
    max_total_bytes: int
    upload_concurrency: int


class PresignRequestIn(BaseModel):
    keys: list[str] | None = None  # None = every image key


class CompleteUploadOut(BaseModel):
    ok: bool
    uploaded_count: int
    missing: list[str] = Field(default_factory=list)
    size_mismatch: list[str] = Field(default_factory=list)


class ProviderTaskInfoOut(BaseModel):
    state: str | None = None
    progress: float | None = None
    processing_time_ms: int | None = None
    last_error: str | None = None


class SurveyOut(BaseModel):
    id: str
    project_id: str
    provider: str
    external_task_id: str | None
    state: str
    stage: str | None
    progress_percent: float
    image_count: int
    uploaded_count: int
    total_bytes: int
    warnings: list[str] = Field(default_factory=list)
    error_message: str | None
    cancel_requested: bool
    preflight: dict | None = None
    provider_task: ProviderTaskInfoOut | None = None
    gcp_supplied: bool
    dtm_available: bool
    orthophoto_available: bool
    manifest: dict | None = None
    created_at: float
    started_at: float | None
    completed_at: float | None
    updated_at: float


class SurveyListOut(BaseModel):
    surveys: list[SurveyOut]


class StartOut(BaseModel):
    ok: bool
    state: str
    queue_job_id: str | None = None


class SimpleOk(BaseModel):
    ok: bool
    detail: str | None = None


class PhotogrammetryHealthOut(BaseModel):
    provider: str
    configured_url: str
    reachable: bool
    version: str = ""
    engine: str = ""
    engine_version: str = ""
    queue_count: int | None = None
    max_images: int | None = None
    error: str | None = None
