"""Drone DTM upload hardening: a synthetic EPSG:32613 (UTM 13N) GeoTIFF —
like a Mavic 3E DTM processed by a local surveyor — must validate, report its
CRS/resolution/bounds, and return a WGS84 footprint. Bad rasters are rejected
with clear messages. No network required."""

import numpy as np
import pytest
import rasterio
from fastapi.testclient import TestClient
from rasterio.transform import from_origin

AOI = {"type": "Polygon", "coordinates": [[
    [-104.06, 21.44], [-104.02, 21.44], [-104.02, 21.47],
    [-104.06, 21.47], [-104.06, 21.44]]]}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import app.db as db
    import app.main as main

    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "keyline.sqlite"))
    monkeypatch.setattr(main, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(main, "MAPS_DIR", str(tmp_path / "maps"))
    with TestClient(main.app) as c:
        yield c


def _project(client) -> str:
    r = client.post("/api/projects", json={"name": "utm13n", "aoi": AOI})
    assert r.status_code == 200
    return r.json()["project_id"]


def _write_tif(path, data, crs="EPSG:32613", res=2.0, nodata=None,
               origin=(597000.0, 2374000.0), count=1):
    h, w = data.shape[-2], data.shape[-1]
    with rasterio.open(
        path, "w", driver="GTiff", height=h, width=w, count=count,
        dtype="float32", crs=crs, nodata=nodata,
        transform=from_origin(origin[0], origin[1], res, res),
    ) as dst:
        if count == 1:
            dst.write(data.astype("float32"), 1)
        else:
            for i in range(count):
                dst.write(data.astype("float32"), i + 1)


def test_utm13n_dtm_uploads_and_reports_metadata(client, tmp_path):
    pid = _project(client)
    y, x = np.mgrid[0:80, 0:80]
    dem = (1900.0 + 0.1 * x + 0.05 * y).astype("float32")
    dem[:5, :5] = -9999.0  # nodata corner
    tif = tmp_path / "mavic_dtm.tif"
    _write_tif(tif, dem, nodata=-9999.0)

    with open(tif, "rb") as f:
        r = client.post(f"/api/projects/{pid}/drone-dem",
                        files={"file": ("mavic_dtm.tif", f, "image/tiff")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["crs"] == "EPSG:32613"
    assert body["resolution_m"] == [2.0, 2.0]
    assert body["footprint"]["type"] == "Polygon"
    # UTM 13N easting 597 km / northing 2374 km is ~104°W, ~21.5°N
    lon, lat = body["footprint"]["coordinates"][0][0]
    assert -105.0 < lon < -103.0 and 21.0 < lat < 22.0
    assert 1890.0 <= body["elevation_range_m"][0] <= 1910.0


def test_multiband_raster_rejected(client, tmp_path):
    pid = _project(client)
    data = np.ones((10, 10), dtype="float32") * 100
    tif = tmp_path / "rgb.tif"
    _write_tif(tif, data, count=3)
    with open(tif, "rb") as f:
        r = client.post(f"/api/projects/{pid}/drone-dem",
                        files={"file": ("rgb.tif", f, "image/tiff")})
    assert r.status_code == 422
    assert "single-band" in r.json()["detail"]


def test_all_nodata_rejected(client, tmp_path):
    pid = _project(client)
    data = np.full((10, 10), -9999.0, dtype="float32")
    tif = tmp_path / "empty.tif"
    _write_tif(tif, data, nodata=-9999.0)
    with open(tif, "rb") as f:
        r = client.post(f"/api/projects/{pid}/drone-dem",
                        files={"file": ("empty.tif", f, "image/tiff")})
    assert r.status_code == 422
    assert "nodata" in r.json()["detail"]


def test_implausible_elevations_rejected(client, tmp_path):
    pid = _project(client)
    data = np.full((10, 10), 65535.0, dtype="float32")  # unscaled DN, not meters
    tif = tmp_path / "dn.tif"
    _write_tif(tif, data)
    with open(tif, "rb") as f:
        r = client.post(f"/api/projects/{pid}/drone-dem",
                        files={"file": ("dn.tif", f, "image/tiff")})
    assert r.status_code == 422
    assert "-500..9000" in r.json()["detail"]
