"""Local storage backend: presign plan, object verification, traversal
rejection, prefix cleanup."""

import pytest

from app.storage import LocalStorage, StorageError
from app.storage.local import safe_key_path


def test_local_roundtrip(tmp_path):
    s = LocalStorage(str(tmp_path))
    key = "uploads/p/s/abc.jpg"
    p = s.presign_put(key, "image/jpeg", 600)
    assert p.url.endswith(f"/api/local-uploads/{key}")
    assert p.headers["Content-Type"] == "image/jpeg"
    assert not s.exists(key)
    s.put_bytes(key, b"hello", "image/jpeg")
    assert s.exists(key) and s.size(key) == 5
    dest = tmp_path / "out.bin"
    s.download_to(key, str(dest))
    assert dest.read_bytes() == b"hello"
    assert s.delete_prefix("uploads/p/s") == 1
    assert not s.exists(key)


@pytest.mark.parametrize("bad", [
    "../etc/passwd",
    "uploads/../../etc/passwd",
    "/absolute/path",
    "a/../../b",
])
def test_traversal_keys_rejected(tmp_path, bad):
    s = LocalStorage(str(tmp_path))
    with pytest.raises(StorageError):
        safe_key_path(str(tmp_path), bad)
    with pytest.raises(StorageError):
        s.put_bytes(bad, b"x", "image/jpeg")


def test_missing_object_size_raises(tmp_path):
    s = LocalStorage(str(tmp_path))
    with pytest.raises(StorageError):
        s.size("uploads/nope.jpg")
