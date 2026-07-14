"""DEM mode selection and the drone-only pipeline path (offline)."""

import pytest

from app.pipeline import select_dem_mode
from fake_provider import write_synthetic_dtm
from survey_helpers import aoi_inside_fake_dtm


def test_satellite_only_without_drone():
    mode, cov = select_dem_mode(None, aoi_inside_fake_dtm(), "auto")
    assert mode == "satellite_only" and cov is None


def test_drone_only_selected_at_full_coverage(tmp_path):
    dtm = str(tmp_path / "full.tif")
    write_synthetic_dtm(dtm, nodata_corner=False)
    mode, cov = select_dem_mode(dtm, aoi_inside_fake_dtm(), "auto")
    assert mode == "drone_only"
    assert cov > 0.98


def test_fused_selected_at_partial_coverage(tmp_path):
    # DTM covering only the western half of the AOI footprint
    dtm = str(tmp_path / "half.tif")
    write_synthetic_dtm(dtm, size=(120, 55), nodata_corner=False)
    mode, cov = select_dem_mode(dtm, aoi_inside_fake_dtm(), "auto")
    assert mode == "fused"
    assert 0.1 < cov < 0.9


def test_explicit_mode_requires_consistency(tmp_path):
    with pytest.raises(ValueError, match="requires a drone DTM"):
        select_dem_mode(None, aoi_inside_fake_dtm(), "drone_only")
    with pytest.raises(ValueError, match="Unknown dem_mode"):
        select_dem_mode(None, aoi_inside_fake_dtm(), "banana")


def test_drone_only_pipeline_runs_offline(tmp_path, monkeypatch):
    """A complete drone DTM must be analyzed without any Copernicus fetch,
    without satellite smoothing, and without satellite reliability warnings."""
    from app import dem_source, pipeline

    def _boom(*a, **k):
        raise AssertionError("satellite fetch must not happen in drone_only")

    monkeypatch.setattr(dem_source, "fetch_glo30", _boom)
    dtm = str(tmp_path / "full.tif")
    write_synthetic_dtm(dtm, nodata_corner=False)

    project_dir = str(tmp_path / "proj")
    fc = pipeline.run_pipeline(project_dir, aoi_inside_fake_dtm(),
                               drone_path=dtm)
    props = fc["properties"]
    assert props["dem_mode"] == "drone_only"
    assert props["drone_coverage"] >= 0.98
    assert props["warning"] is None
    assert props["keylines_suppressed"] is False
    assert props["dem_resolution_m"] <= 2.0  # native drone resolution kept
    # every keypoint (if any) must be drone-sourced
    for f in fc["features"]:
        if f["properties"].get("kind") == "keypoint":
            assert f["properties"]["source"] == "drone"
