"""Object-storage abstraction for drone-photo uploads.

The browser uploads with presigned PUT URLs; the backend only verifies and
later materializes objects for the worker. Keys are always generated
server-side (UUID-based) — user filenames are metadata only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class PresignedUpload:
    key: str
    url: str
    headers: dict[str, str]
    method: str = "PUT"


class StorageError(RuntimeError):
    pass


class StorageBackend(Protocol):
    name: str

    def presign_put(self, key: str, content_type: str,
                    expiry_seconds: int) -> PresignedUpload: ...

    def exists(self, key: str) -> bool: ...

    def size(self, key: str) -> int:
        """Byte size of an existing object (raises StorageError if absent)."""
        ...

    def download_to(self, key: str, dest_path: str) -> None: ...

    def put_bytes(self, key: str, data: bytes, content_type: str) -> None: ...

    def delete_prefix(self, prefix: str) -> int:
        """Delete every object under prefix; returns number removed."""
        ...
