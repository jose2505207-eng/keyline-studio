"""DTM/orthophoto validation and the orthophoto web preview."""

import numpy as np
import pytest
import rasterio

from app.assets import (
    AssetValidationError,
    dtm_aoi_coverage,
    ensure_orthophoto_preview,
    validate_dtm,
    validate_orthophoto,
)
from fake_provider import write_synthetic_dtm, write_synthetic_orthophoto
from survey_helpers import aoi_inside_fake_dtm


def test_validate_dtm_happy_path(tmp_path):
    dtm = str(tmp_path / "dtm.tif")
    write_synthetic_dtm(dtm)
    meta = validate_dtm(dtm, aoi_inside_fake_dtm())
    assert meta["crs"] == "EPSG:32613"
    assert meta["resolution_m"] == [1.0, 1.0]
    assert meta["width"] == 120 and meta["height"] == 120
    assert 1890 < meta["elevation_range_m"][0] < 1920
    assert meta["aoi_coverage"] > 0.9
    assert meta["footprint_wgs84"]["type"] == "Polygon"


def test_validate_dtm_missing_file():
    with pytest.raises(AssetValidationError, match="did not produce a DTM"):
        validate_dtm("/nowhere/dtm.tif", aoi_inside_fake_dtm())


def test_validate_dtm_no_aoi_overlap(tmp_path):
    dtm = str(tmp_path / "far.tif")
    write_synthetic_dtm(dtm, origin=(500000.0, 2000000.0))  # 400 km away
    with pytest.raises(AssetValidationError, match="does not meaningfully overlap"):
        validate_dtm(dtm, aoi_inside_fake_dtm())


def test_validate_dtm_all_nodata(tmp_path):
    from rasterio.transform import from_origin

    path = str(tmp_path / "empty.tif")
    with rasterio.open(path, "w", driver="GTiff", height=10, width=10,
                       count=1, dtype="float32", crs="EPSG:32613",
                       nodata=-9999.0,
                       transform=from_origin(597000, 2374000, 1, 1)) as dst:
        dst.write(np.full((10, 10), -9999.0, dtype="float32"), 1)
    with pytest.raises(AssetValidationError, match="only nodata"):
        validate_dtm(path, aoi_inside_fake_dtm())


def test_validate_dtm_rejects_multiband(tmp_path):
    from rasterio.transform import from_origin

    path = str(tmp_path / "rgb.tif")
    with rasterio.open(path, "w", driver="GTiff", height=10, width=10,
                       count=3, dtype="float32", crs="EPSG:32613",
                       transform=from_origin(597000, 2374000, 1, 1)) as dst:
        for i in range(3):
            dst.write(np.ones((10, 10), dtype="float32"), i + 1)
    with pytest.raises(AssetValidationError, match="bands"):
        validate_dtm(path, aoi_inside_fake_dtm())


def test_coverage_partial(tmp_path):
    dtm = str(tmp_path / "half.tif")
    write_synthetic_dtm(dtm, size=(120, 55), nodata_corner=False)
    cov = dtm_aoi_coverage(dtm, aoi_inside_fake_dtm())
    assert 0.1 < cov < 0.9


def test_validate_orthophoto_and_preview(tmp_path):
    tif = str(tmp_path / "ortho.tif")
    write_synthetic_orthophoto(tif)
    meta = validate_orthophoto(tif)
    assert meta["bands"] == 4 and meta["crs"] == "EPSG:32613"

    preview, bounds = ensure_orthophoto_preview(tif, str(tmp_path))
    from PIL import Image

    img = Image.open(preview)
    assert img.mode == "RGBA"
    # the nodata corner keeps alpha 0
    assert img.getpixel((1, 1))[3] == 0
    assert img.getpixel((60, 60))[3] == 255
    assert len(bounds["coordinates"]) == 4
    # cached on second call
    p2, _ = ensure_orthophoto_preview(tif, str(tmp_path))
    assert p2 == preview


def test_orthophoto_without_crs_rejected(tmp_path):
    path = str(tmp_path / "nocrs.tif")
    with rasterio.open(path, "w", driver="GTiff", height=8, width=8,
                       count=3, dtype="uint8") as dst:
        for i in range(3):
            dst.write(np.zeros((8, 8), dtype="uint8"), i + 1)
    with pytest.raises(AssetValidationError, match="CRS"):
        validate_orthophoto(path)
