"""NodeODM provider backed by the official ``pyodm`` package.

Live pyodm objects are never persisted anywhere — only the external task
UUID is stored; the Node client is reconstructed for every call so the
worker survives restarts.
"""

from __future__ import annotations

import glob
import logging
import os
from typing import Callable

from .base import (
    ProviderError,
    ProviderTaskFailed,
    ProviderTaskNotFound,
    ProviderTaskRejected,
    ProviderUnavailable,
)
from .models import (
    PhotogrammetryAssets,
    ProviderHealth,
    ProviderTask,
    ProviderTaskStatus,
    TaskState,
)

log = logging.getLogger(__name__)

_STATE_MAP = {
    "QUEUED": TaskState.QUEUED,
    "RUNNING": TaskState.RUNNING,
    "COMPLETED": TaskState.COMPLETED,
    "FAILED": TaskState.FAILED,
    "CANCELED": TaskState.CANCELLED,
}


def _find_asset(root: str, *patterns: str) -> str | None:
    """Locate an asset whether extracted from all.zip or already on disk."""
    for pattern in patterns:
        hits = glob.glob(os.path.join(root, "**", pattern), recursive=True)
        if hits:
            return sorted(hits, key=len)[0]  # shallowest match
    return None


class NodeOdmProvider:
    name = "nodeodm"

    def __init__(self, url: str, token: str = "", timeout: int = 60,
                 max_parallel_uploads: int = 4):
        self.url = url
        self.token = token
        self.timeout = timeout
        self.max_parallel_uploads = max_parallel_uploads

    # -- internals ----------------------------------------------------------
    def _node(self):
        from pyodm import Node

        url = self.url
        if self.token:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}token={self.token}"
        return Node.from_url(url, timeout=self.timeout)

    def _task(self, external_task_id: str):
        from pyodm.exceptions import NodeConnectionError, NodeResponseError

        try:
            return self._node().get_task(external_task_id)
        except NodeConnectionError as exc:
            raise ProviderUnavailable(f"NodeODM unreachable: {exc}") from exc
        except NodeResponseError as exc:
            raise ProviderTaskNotFound(
                f"NodeODM does not know task {external_task_id}: {exc}") from exc

    # -- interface ----------------------------------------------------------
    def health(self) -> ProviderHealth:
        from pyodm.exceptions import OdmError

        try:
            info = self._node().info()
            return ProviderHealth(
                ok=True,
                provider=self.name,
                version=str(getattr(info, "version", "")),
                engine=str(getattr(info, "engine", "")),
                engine_version=str(getattr(info, "engine_version", "")),
                queue_count=getattr(info, "task_queue_count", None),
                max_images=getattr(info, "max_images", None),
            )
        except (OdmError, OSError) as exc:
            return ProviderHealth(ok=False, provider=self.name, error=str(exc))

    def supported_options(self) -> set[str] | None:
        """Option names the connected node advertises; None if unavailable."""
        from pyodm.exceptions import OdmError

        try:
            return {opt.name for opt in self._node().options()}
        except (OdmError, OSError):
            return None

    def create_task(
        self,
        image_paths: list[str],
        task_name: str,
        options: dict,
        gcp_path: str | None = None,
        progress_callback: Callable[[float], None] | None = None,
    ) -> ProviderTask:
        from pyodm.exceptions import (
            NodeConnectionError,
            NodeResponseError,
            NodeServerError,
            OdmError,
        )

        health = self.health()
        if not health.ok:
            raise ProviderUnavailable(
                f"NodeODM at {self.url} is unavailable: {health.error}")
        if health.max_images and len(image_paths) > health.max_images:
            raise ProviderTaskRejected(
                f"Node accepts at most {health.max_images} images; "
                f"survey has {len(image_paths)}")

        files = list(image_paths)
        if gcp_path:
            files.append(gcp_path)
        try:
            task = self._node().create_task(
                files,
                options=options,
                name=task_name,
                progress_callback=progress_callback,
                parallel_uploads=self.max_parallel_uploads,
            )
        except NodeConnectionError as exc:
            raise ProviderUnavailable(f"NodeODM unreachable mid-upload: {exc}") from exc
        except (NodeResponseError, NodeServerError) as exc:
            raise ProviderTaskRejected(f"NodeODM rejected the task: {exc}") from exc
        except OdmError as exc:
            raise ProviderError(f"NodeODM task creation failed: {exc}") from exc
        return ProviderTask(external_task_id=str(task.uuid), name=task_name)

    def get_task(self, external_task_id: str) -> ProviderTaskStatus:
        info = self._task(external_task_id).info()
        status_name = getattr(getattr(info, "status", None), "name", "UNKNOWN")
        return ProviderTaskStatus(
            external_task_id=external_task_id,
            state=_STATE_MAP.get(status_name, TaskState.UNKNOWN),
            progress_percent=float(getattr(info, "progress", 0) or 0),
            processing_time_ms=getattr(info, "processing_time", None),
            last_error=getattr(info, "last_error", None) or None,
            images_count=getattr(info, "images_count", None),
            raw={"status": status_name},
        )

    def get_output(self, external_task_id: str, tail: int = 100) -> list[str]:
        from pyodm.exceptions import OdmError

        try:
            lines = self._task(external_task_id).output()
            return [str(line) for line in lines[-tail:]]
        except (ProviderError, OdmError, OSError) as exc:
            log.warning("could not fetch NodeODM output for %s: %s",
                        external_task_id, exc)
            return []

    def download_assets(self, external_task_id: str,
                        destination: str) -> PhotogrammetryAssets:
        from pyodm.exceptions import OdmError, TaskFailedError

        os.makedirs(destination, exist_ok=True)
        try:
            assets_dir = self._task(external_task_id).download_assets(destination)
        except TaskFailedError as exc:
            raise ProviderTaskFailed(f"NodeODM task failed: {exc}") from exc
        except OdmError as exc:
            raise ProviderError(f"Asset download failed: {exc}") from exc

        root = str(assets_dir or destination)
        dtm = _find_asset(root, os.path.join("odm_dem", "dtm.tif"), "dtm.tif")
        ortho = _find_asset(
            root,
            os.path.join("odm_orthophoto", "odm_orthophoto.tif"),
            "odm_orthophoto.tif",
        )
        return PhotogrammetryAssets(dtm_path=dtm, orthophoto_path=ortho,
                                    assets_dir=root)

    def cancel_task(self, external_task_id: str) -> bool:
        from pyodm.exceptions import OdmError

        try:
            return bool(self._task(external_task_id).cancel())
        except (ProviderError, OdmError, OSError) as exc:
            log.warning("cancel of NodeODM task %s failed: %s",
                        external_task_id, exc)
            return False
