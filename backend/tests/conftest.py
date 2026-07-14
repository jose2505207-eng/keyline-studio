import os
import sys

# Tests must run offline: force the pure-Python hydrology engine so no
# WhiteboxTools binary download is attempted.
os.environ.setdefault("KEYLINE_HYDRO_ENGINE", "pysheds")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


import pytest  # noqa: E402


@pytest.fixture()
def drone_env(tmp_path, monkeypatch):
    """Isolated app environment for drone-survey tests: tmp data dir + DB,
    local storage override, FakePhotogrammetryProvider override, and small
    upload limits so tiny datasets are valid."""
    import app.db as db
    import app.main as main
    from app.photogrammetry import set_provider_override
    from app.storage import LocalStorage, set_storage_override
    from fake_provider import FakeProvider
    from fastapi.testclient import TestClient

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("KEYLINE_DATA", str(data_dir))
    monkeypatch.setenv("DRONE_MIN_IMAGES", "3")
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "keyline.sqlite"))
    monkeypatch.setattr(main, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(main, "MAPS_DIR", str(data_dir / "maps"))

    storage = LocalStorage(str(data_dir / "uploads"))
    provider = FakeProvider()
    set_storage_override(storage)
    set_provider_override(provider)

    class Env:
        pass

    env = Env()
    env.tmp = tmp_path
    env.data_dir = data_dir
    env.storage = storage
    env.provider = provider
    env.db = db

    with TestClient(main.app) as client:
        env.client = client
        yield env

    set_storage_override(None)
    set_provider_override(None)
