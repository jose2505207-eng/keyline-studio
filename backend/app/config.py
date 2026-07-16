"""Central environment configuration for the drone-photogrammetry stack.

Everything is read lazily via functions so tests can monkeypatch os.environ
without import-order headaches. Defaults are safe for local development.
"""

from __future__ import annotations

import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# --- photogrammetry provider -------------------------------------------------

def photogrammetry_provider() -> str:
    return os.environ.get("PHOTOGRAMMETRY_PROVIDER", "nodeodm")


def data_dir() -> str:
    return os.environ.get(
        "KEYLINE_DATA",
        os.path.join(os.path.dirname(__file__), "..", "data"),
    )


def provider_url_override_path() -> str:
    return os.path.join(data_dir(), "provider_url.json")


def nodeodm_url() -> str:
    """Provider URL: a runtime override (set through the admin endpoint by
    the tunnel-sync script) wins over the NODEODM_URL env var. Quick-tunnel
    URLs change on every restart; this keeps the hosted backend pointed at
    the live tunnel without a redeploy."""
    try:
        with open(provider_url_override_path()) as f:
            import json

            url = json.load(f).get("url", "")
            if url:
                return url
    except (OSError, ValueError):
        pass
    return os.environ.get("NODEODM_URL", "http://localhost:3000")


def nodeodm_token() -> str:
    return os.environ.get("NODEODM_TOKEN", "")


def nodeodm_timeout_seconds() -> int:
    return _int("NODEODM_TIMEOUT_SECONDS", 60)


def odm_orthophoto_resolution_cm() -> float:
    return _float("ODM_ORTHOPHOTO_RESOLUTION_CM", 5.0)


def odm_dem_resolution_cm() -> float:
    return _float("ODM_DEM_RESOLUTION_CM", 10.0)


def odm_max_parallel_uploads() -> int:
    return _int("ODM_MAX_PARALLEL_UPLOADS", 4)


def odm_split_image_count() -> int:
    return _int("ODM_SPLIT_IMAGE_COUNT", 0)


def odm_split_overlap_meters() -> int:
    return _int("ODM_SPLIT_OVERLAP_METERS", 150)


def provider_poll_seconds() -> int:
    return _int("PHOTOGRAMMETRY_POLL_SECONDS", 10)


# --- storage ------------------------------------------------------------------

def storage_backend() -> str:
    return os.environ.get("STORAGE_BACKEND", "local")


def s3_endpoint_url() -> str:
    return os.environ.get("S3_ENDPOINT_URL", "")


def s3_public_endpoint_url() -> str:
    """Endpoint embedded in presigned URLs handed to the browser. Defaults to
    S3_ENDPOINT_URL; set separately when the internal endpoint (e.g.
    http://minio:9000 inside docker) is not reachable from the browser."""
    return os.environ.get("S3_PUBLIC_ENDPOINT_URL", "")


def s3_region() -> str:
    return os.environ.get("S3_REGION", "us-east-1")


def s3_bucket() -> str:
    return os.environ.get("S3_BUCKET", "keyline-uploads")


def s3_access_key_id() -> str:
    return os.environ.get("S3_ACCESS_KEY_ID", "")


def s3_secret_access_key() -> str:
    return os.environ.get("S3_SECRET_ACCESS_KEY", "")


def s3_secure() -> bool:
    return _bool("S3_SECURE", True)


def s3_presign_expiry_seconds() -> int:
    return _int("S3_PRESIGN_EXPIRY_SECONDS", 3600)


# --- upload limits -------------------------------------------------------------

def drone_min_images() -> int:
    return _int("DRONE_MIN_IMAGES", 20)


def drone_max_images() -> int:
    return _int("DRONE_MAX_IMAGES", 500)


def drone_max_file_bytes() -> int:
    return _int("DRONE_MAX_FILE_BYTES", 60 * 1024 * 1024)  # 60 MB per photo


def drone_max_total_bytes() -> int:
    return _int("DRONE_MAX_TOTAL_BYTES", 20 * 1024 * 1024 * 1024)  # 20 GB


def drone_allowed_extensions() -> tuple[str, ...]:
    raw = os.environ.get("DRONE_ALLOWED_EXTENSIONS", ".jpg,.jpeg")
    return tuple(e.strip().lower() for e in raw.split(",") if e.strip())


def drone_upload_concurrency() -> int:
    return _int("DRONE_UPLOAD_CONCURRENCY", 4)


# --- terrain -------------------------------------------------------------------

def drone_only_min_aoi_coverage() -> float:
    return _float("DRONE_ONLY_MIN_AOI_COVERAGE", 0.98)


def analysis_stage_timeout_seconds() -> int:
    """Hard watchdog: a stage that reports no progress for this long is marked
    stalled (failed) instead of running forever. Generous so a legitimately
    heavy hydrology/fusion step is not killed prematurely."""
    return _int("ANALYSIS_STAGE_TIMEOUT_SECONDS", 600)


# --- jobs ----------------------------------------------------------------------

def redis_url() -> str:
    return os.environ.get("REDIS_URL", "redis://localhost:6379/0")


# --- terrain QA ------------------------------------------------------------------

def terrain_qa_mode() -> str:
    """warn: run diagnostics and watermark; strict: block keyline generation
    on severe spatial/terrain quality errors."""
    v = os.environ.get("TERRAIN_QA_MODE", "warn").strip().lower()
    return v if v in ("warn", "strict") else "warn"


def qa_tilt_threshold_pct() -> float:
    return _float("QA_TILT_THRESHOLD_PCT", 25.0)


def qa_relief_footprint_ratio() -> float:
    return _float("QA_RELIEF_FOOTPRINT_RATIO", 0.6)


def result_bounds_buffer_m() -> float:
    return _float("RESULT_BOUNDS_BUFFER_M", 50.0)


# --- DTM library --------------------------------------------------------------

def dtm_storage_dir() -> str:
    """Managed DTM library directory. Compose sets /data/dtm (shared bind
    mount); bare dev defaults to <data dir>/dtm."""
    return os.environ.get("DTM_STORAGE_DIR",
                          os.path.join(data_dir(), "dtm"))


def dtm_allowed_external_roots() -> list[str]:
    """Roots a custom server filepath may live under. The data dir and the
    DTM library itself are always allowed so bare-dev setups work without
    extra configuration."""
    raw = os.environ.get("DTM_ALLOWED_EXTERNAL_ROOTS", "/data,/app/data")
    roots = [r.strip() for r in raw.split(",") if r.strip()]
    roots.extend([data_dir(), dtm_storage_dir()])
    return roots


def dtm_max_upload_mb() -> int:
    return _int("DTM_MAX_UPLOAD_MB", 1024)
