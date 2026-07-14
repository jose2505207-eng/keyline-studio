"""Typed models shared by all photogrammetry providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TaskState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass
class ProviderHealth:
    ok: bool
    provider: str
    version: str = ""
    engine: str = ""
    engine_version: str = ""
    queue_count: int | None = None
    max_images: int | None = None
    error: str | None = None


@dataclass
class ProviderTask:
    external_task_id: str
    name: str = ""


@dataclass
class ProviderTaskStatus:
    external_task_id: str
    state: TaskState
    progress_percent: float = 0.0
    processing_time_ms: int | None = None
    last_error: str | None = None
    images_count: int | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class PhotogrammetryAssets:
    """Normalized local paths after a successful download."""
    dtm_path: str | None
    orthophoto_path: str | None
    assets_dir: str
    extra: dict = field(default_factory=dict)
