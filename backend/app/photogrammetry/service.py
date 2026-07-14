"""Provider factory + ODM option policy.

The default task requests a bare-earth DTM and an orthophoto and skips the
full textured 3D model. Options are validated against what the connected
node actually advertises when that information is available; unsupported
options are dropped with a logged notice rather than failing the whole task
(NodeODM versions differ), except explicitly-user-requested unknown options,
which are rejected loudly.
"""

from __future__ import annotations

import logging

from .. import config
from .base import PhotogrammetryProvider, ProviderTaskRejected

log = logging.getLogger(__name__)

# Set by tests (or exotic deployments) to inject a provider instance.
_override: PhotogrammetryProvider | None = None


def set_provider_override(provider: PhotogrammetryProvider | None) -> None:
    global _override
    _override = provider


def get_provider() -> PhotogrammetryProvider:
    if _override is not None:
        return _override
    kind = config.photogrammetry_provider()
    if kind == "nodeodm":
        from .nodeodm import NodeOdmProvider

        return NodeOdmProvider(
            url=config.nodeodm_url(),
            token=config.nodeodm_token(),
            timeout=config.nodeodm_timeout_seconds(),
            max_parallel_uploads=config.odm_max_parallel_uploads(),
        )
    raise ValueError(f"Unknown PHOTOGRAMMETRY_PROVIDER: {kind!r}")


def default_odm_options() -> dict:
    """Conservative defaults: bare-earth DTM + orthophoto, no 3D model."""
    opts: dict = {
        "dtm": True,
        "dsm": False,
        "skip-3dmodel": True,
        "orthophoto-resolution": config.odm_orthophoto_resolution_cm(),
        "dem-resolution": config.odm_dem_resolution_cm(),
        "pc-classify": True,   # ground classification feeds the DTM
    }
    split = config.odm_split_image_count()
    if split > 0:
        opts["split"] = split
        opts["split-overlap"] = config.odm_split_overlap_meters()
    return opts


def build_task_options(user_options: dict | None,
                       supported: set[str] | None) -> dict:
    """Merge defaults with user-requested options and validate them.

    - Defaults not supported by the node are dropped with a log line
      (except `dtm`/`skip-3dmodel`, which are required for this workflow).
    - User-supplied options unknown to the node are rejected clearly.
    """
    merged = default_odm_options()
    user_options = dict(user_options or {})
    merged.update(user_options)

    if supported is None:
        return merged

    required = {"dtm", "skip-3dmodel"}
    missing_required = required - supported
    if missing_required:
        raise ProviderTaskRejected(
            "The connected NodeODM node does not support required options: "
            + ", ".join(sorted(missing_required)))

    out = {}
    for key, value in merged.items():
        if key in supported:
            out[key] = value
        elif key in user_options:
            raise ProviderTaskRejected(
                f"Requested ODM option '{key}' is not supported by the "
                "connected node")
        else:
            log.info("dropping default ODM option unsupported by node: %s", key)
    return out
