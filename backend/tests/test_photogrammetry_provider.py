"""Provider interface, option policy, and NodeODM asset discovery — all
offline (the NodeODM provider's network layer is not exercised here)."""

import os

import pytest

from app.photogrammetry import (
    ProviderTaskRejected,
    TaskState,
    build_task_options,
    default_odm_options,
    get_provider,
    set_provider_override,
)
from app.photogrammetry.nodeodm import _find_asset

from fake_provider import FakeProvider


def test_default_options_request_dtm_and_skip_3dmodel():
    opts = default_odm_options()
    assert opts["dtm"] is True
    assert opts["skip-3dmodel"] is True
    assert opts["dsm"] is False
    assert "orthophoto-resolution" in opts and "dem-resolution" in opts


def test_build_options_drops_unsupported_defaults_quietly():
    supported = {"dtm", "skip-3dmodel", "orthophoto-resolution"}
    out = build_task_options(None, supported)
    assert set(out) <= supported
    assert out["dtm"] is True


def test_build_options_rejects_unsupported_user_option():
    supported = {"dtm", "skip-3dmodel"}
    with pytest.raises(ProviderTaskRejected, match="pc-super-magic"):
        build_task_options({"pc-super-magic": 1}, supported)


def test_build_options_rejects_node_without_dtm_support():
    with pytest.raises(ProviderTaskRejected, match="dtm"):
        build_task_options(None, {"orthophoto-resolution"})


def test_build_options_without_node_info_keeps_defaults():
    out = build_task_options({"dem-resolution": 25}, None)
    assert out["dem-resolution"] == 25
    assert out["dtm"] is True


def test_provider_override_is_explicit_and_reversible(monkeypatch):
    fake = FakeProvider()
    set_provider_override(fake)
    try:
        assert get_provider() is fake
    finally:
        set_provider_override(None)
    # without override, config decides (default: nodeodm class)
    monkeypatch.setenv("PHOTOGRAMMETRY_PROVIDER", "nodeodm")
    assert get_provider().name == "nodeodm"


def test_fake_provider_lifecycle(tmp_path):
    fake = FakeProvider()
    task = fake.create_task(["a.jpg", "b.jpg"], "t", {"dtm": True})
    states = [fake.get_task(task.external_task_id).state for _ in range(3)]
    assert states == [TaskState.QUEUED, TaskState.RUNNING, TaskState.COMPLETED]
    assets = fake.download_assets(task.external_task_id, str(tmp_path))
    assert assets.dtm_path and os.path.exists(assets.dtm_path)
    assert assets.orthophoto_path and os.path.exists(assets.orthophoto_path)


def test_find_asset_handles_zip_extract_and_flat_layouts(tmp_path):
    nested = tmp_path / "extracted" / "odm_dem"
    nested.mkdir(parents=True)
    (nested / "dtm.tif").write_bytes(b"x")
    assert _find_asset(str(tmp_path), os.path.join("odm_dem", "dtm.tif"),
                       "dtm.tif").endswith("dtm.tif")

    flat = tmp_path / "flat"
    flat.mkdir()
    (flat / "dtm.tif").write_bytes(b"x")
    assert _find_asset(str(flat), os.path.join("odm_dem", "dtm.tif"),
                       "dtm.tif").endswith("dtm.tif")

    assert _find_asset(str(tmp_path / "empty-nowhere"), "dtm.tif") is None
