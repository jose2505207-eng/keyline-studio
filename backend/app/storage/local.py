"""Local-disk storage backend for development and tests.

"Presigned" URLs point at the API's own PUT endpoint
(/api/local-uploads/{key}); the API writes the body under
DATA_DIR/uploads/<key>. Keys are validated against traversal before any
filesystem access.
"""

from __future__ import annotations

import os
import shutil

from .base import PresignedUpload, StorageError


def safe_key_path(root: str, key: str) -> str:
    """Resolve key inside root, rejecting any traversal attempt."""
    if key.startswith(("/", "\\")) or ".." in key.split("/"):
        raise StorageError(f"Illegal object key: {key!r}")
    path = os.path.realpath(os.path.join(root, key))
    if not path.startswith(os.path.realpath(root) + os.sep):
        raise StorageError(f"Object key escapes storage root: {key!r}")
    return path


class LocalStorage:
    name = "local"

    def __init__(self, root: str, public_base: str = ""):
        self.root = root
        self.public_base = public_base.rstrip("/")
        os.makedirs(root, exist_ok=True)

    def _path(self, key: str) -> str:
        return safe_key_path(self.root, key)

    def presign_put(self, key: str, content_type: str,
                    expiry_seconds: int) -> PresignedUpload:
        self._path(key)  # validate early
        return PresignedUpload(
            key=key,
            url=f"{self.public_base}/api/local-uploads/{key}",
            headers={"Content-Type": content_type},
        )

    def exists(self, key: str) -> bool:
        return os.path.isfile(self._path(key))

    def size(self, key: str) -> int:
        path = self._path(key)
        if not os.path.isfile(path):
            raise StorageError(f"Object not found: {key}")
        return os.path.getsize(path)

    def download_to(self, key: str, dest_path: str) -> None:
        path = self._path(key)
        if not os.path.isfile(path):
            raise StorageError(f"Object not found: {key}")
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        shutil.copyfile(path, dest_path)

    def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)

    def delete_prefix(self, prefix: str) -> int:
        base = self._path(prefix)
        if not os.path.isdir(base):
            return 0
        count = sum(len(files) for _, _, files in os.walk(base))
        shutil.rmtree(base, ignore_errors=True)
        return count
