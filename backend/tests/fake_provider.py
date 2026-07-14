"""FakePhotogrammetryProvider for tests only.

Never used in production: app.photogrammetry.service only returns it when a
test explicitly calls set_provider_override(). It simulates the NodeODM
lifecycle from a scripted sequence of statuses and materializes synthetic
georeferenced fixture assets on download.
"""

from __future__ import annotations

import os
import uuid

import numpy as np

from app.photogrammetry.base import ProviderTaskNotFound, ProviderUnavailable
from app.photogrammetry.models import (
    PhotogrammetryAssets,
    ProviderHealth,
    ProviderTask,
    ProviderTaskStatus,
    TaskState,
)


def write_synthetic_dtm(path: str, *, crs: str = "EPSG:32613",
                        origin=(597000.0, 2374000.0), size=(120, 120),
                        res: float = 1.0, base: float = 1900.0,
                        nodata_corner: bool = True) -> None:
    import rasterio
    from rasterio.transform import from_origin

    h, w = size
    y, x = np.mgrid[0:h, 0:w]
    dem = (base + 0.08 * x + 0.05 * y +
           3.0 * np.sin(x / 15.0)).astype("float32")
    if nodata_corner:
        dem[:5, :5] = -9999.0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with rasterio.open(
        path, "w", driver="GTiff", height=h, width=w, count=1,
        dtype="float32", crs=crs, nodata=-9999.0,
        transform=from_origin(origin[0], origin[1], res, res),
    ) as dst:
        dst.write(dem, 1)


def write_synthetic_orthophoto(path: str, *, crs: str = "EPSG:32613",
                               origin=(597000.0, 2374000.0), size=(120, 120),
                               res: float = 1.0) -> None:
    import rasterio
    from rasterio.transform import from_origin

    h, w = size
    rng = np.random.default_rng(3)
    bands = rng.integers(40, 200, (3, h, w)).astype("uint8")
    alpha = np.full((h, w), 255, dtype="uint8")
    alpha[:5, :5] = 0  # nodata corner
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with rasterio.open(
        path, "w", driver="GTiff", height=h, width=w, count=4,
        dtype="uint8", crs=crs,
        transform=from_origin(origin[0], origin[1], res, res),
    ) as dst:
        for i in range(3):
            dst.write(bands[i], i + 1)
        dst.write(alpha, 4)


class FakeProvider:
    name = "fake"

    def __init__(self, statuses: list[TaskState] | None = None,
                 healthy: bool = True, fail_error: str = "simulated failure",
                 dtm_kwargs: dict | None = None, make_ortho: bool = True):
        self.healthy = healthy
        self.statuses = statuses or [TaskState.QUEUED, TaskState.RUNNING,
                                     TaskState.COMPLETED]
        self.fail_error = fail_error
        self.dtm_kwargs = dtm_kwargs or {}
        self.make_ortho = make_ortho
        self.tasks: dict[str, dict] = {}
        self.create_calls = 0
        self.cancelled: list[str] = []

    def health(self) -> ProviderHealth:
        if not self.healthy:
            return ProviderHealth(ok=False, provider=self.name,
                                  error="fake node down")
        return ProviderHealth(ok=True, provider=self.name, version="2.5.0",
                              engine="odm", engine_version="3.5.5",
                              queue_count=0, max_images=1000)

    def supported_options(self):
        return {"dtm", "dsm", "skip-3dmodel", "orthophoto-resolution",
                "dem-resolution", "pc-classify", "split", "split-overlap"}

    def create_task(self, image_paths, task_name, options, gcp_path=None,
                    progress_callback=None):
        if not self.healthy:
            raise ProviderUnavailable("fake node down")
        self.create_calls += 1
        tid = uuid.uuid4().hex
        self.tasks[tid] = {"step": 0, "images": len(image_paths),
                           "options": options, "gcp": bool(gcp_path)}
        if progress_callback:
            progress_callback(100.0)
        return ProviderTask(external_task_id=tid, name=task_name)

    def _advance(self, tid: str) -> TaskState:
        t = self.tasks.get(tid)
        if t is None:
            raise ProviderTaskNotFound(f"unknown fake task {tid}")
        state = self.statuses[min(t["step"], len(self.statuses) - 1)]
        t["step"] += 1
        return state

    def get_task(self, external_task_id: str) -> ProviderTaskStatus:
        state = self._advance(external_task_id)
        t = self.tasks[external_task_id]
        frac = min(t["step"] / max(len(self.statuses), 1), 1.0)
        return ProviderTaskStatus(
            external_task_id=external_task_id,
            state=state,
            progress_percent=100.0 * frac if state != TaskState.FAILED else 0.0,
            last_error=self.fail_error if state == TaskState.FAILED else None,
            images_count=t["images"],
        )

    def get_output(self, external_task_id: str, tail: int = 100):
        return ["[INFO] opensfm reconstruction",
                "[INFO] generating dtm",
                "[INFO] orthophoto done"][-tail:]

    def download_assets(self, external_task_id: str,
                        destination: str) -> PhotogrammetryAssets:
        if external_task_id not in self.tasks:
            raise ProviderTaskNotFound(f"unknown fake task {external_task_id}")
        dtm = os.path.join(destination, "odm_dem", "dtm.tif")
        write_synthetic_dtm(dtm, **self.dtm_kwargs)
        ortho = None
        if self.make_ortho:
            ortho = os.path.join(destination, "odm_orthophoto",
                                 "odm_orthophoto.tif")
            write_synthetic_orthophoto(
                ortho,
                crs=self.dtm_kwargs.get("crs", "EPSG:32613"),
                origin=self.dtm_kwargs.get("origin", (597000.0, 2374000.0)),
                size=self.dtm_kwargs.get("size", (120, 120)),
                res=self.dtm_kwargs.get("res", 1.0),
            )
        return PhotogrammetryAssets(dtm_path=dtm, orthophoto_path=ortho,
                                    assets_dir=destination)

    def cancel_task(self, external_task_id: str) -> bool:
        self.cancelled.append(external_task_id)
        return True
