"""PhotogrammetryProvider protocol and provider error taxonomy.

API routes, database code, and UI never talk to PyODM directly — they use
this interface, so other engines (or the tests' fake provider) plug in
without touching call sites.
"""

from __future__ import annotations

from typing import Callable, Protocol

from .models import (
    PhotogrammetryAssets,
    ProviderHealth,
    ProviderTask,
    ProviderTaskStatus,
)


class ProviderError(RuntimeError):
    """Base class for provider failures (safe message for users)."""


class ProviderUnavailable(ProviderError):
    """The processing node cannot be reached or is unhealthy."""


class ProviderTaskRejected(ProviderError):
    """The node refused to create the task (bad options, over limits...)."""


class ProviderTaskFailed(ProviderError):
    """The node accepted the task but processing failed."""


class ProviderTaskNotFound(ProviderError):
    """The external task id is unknown to the node (restarted/purged)."""


class PhotogrammetryProvider(Protocol):
    name: str

    def health(self) -> ProviderHealth: ...

    def create_task(
        self,
        image_paths: list[str],
        task_name: str,
        options: dict,
        gcp_path: str | None = None,
        progress_callback: Callable[[float], None] | None = None,
    ) -> ProviderTask: ...

    def get_task(self, external_task_id: str) -> ProviderTaskStatus: ...

    def get_output(self, external_task_id: str, tail: int = 100) -> list[str]: ...

    def download_assets(
        self,
        external_task_id: str,
        destination: str,
    ) -> PhotogrammetryAssets: ...

    def cancel_task(self, external_task_id: str) -> bool: ...
