from __future__ import annotations

import os

from .. import config
from .base import PresignedUpload, StorageBackend, StorageError  # noqa: F401
from .local import LocalStorage, safe_key_path  # noqa: F401
from .s3 import S3Storage  # noqa: F401

_override: StorageBackend | None = None


def set_storage_override(storage: StorageBackend | None) -> None:
    global _override
    _override = storage


def local_storage_root() -> str:
    data_dir = os.environ.get(
        "KEYLINE_DATA",
        os.path.join(os.path.dirname(__file__), "..", "..", "data"),
    )
    return os.path.join(data_dir, "uploads")


def get_storage() -> StorageBackend:
    if _override is not None:
        return _override
    backend = config.storage_backend()
    if backend == "s3":
        return S3Storage(
            bucket=config.s3_bucket(),
            endpoint_url=config.s3_endpoint_url(),
            region=config.s3_region(),
            access_key=config.s3_access_key_id(),
            secret_key=config.s3_secret_access_key(),
            secure=config.s3_secure(),
            public_endpoint_url=config.s3_public_endpoint_url(),
        )
    if backend == "local":
        return LocalStorage(local_storage_root())
    raise ValueError(f"Unknown STORAGE_BACKEND: {backend!r}")
