"""Image preflight: corrupt JPEGs, duplicates, GPS warnings, GCP format."""

import pytest

from app.preflight import PreflightError, run_preflight, validate_gcp_text
from survey_helpers import VALID_GCP, make_jpeg


def _images(tmp_path, n=5, gps=True):
    return [(f"img{i}.jpg", make_jpeg(str(tmp_path / f"img{i}.jpg"),
                                      gps=gps, seed=i)) for i in range(n)]


def test_healthy_dataset_passes_with_gps(tmp_path):
    res = run_preflight(_images(tmp_path), min_images=3, has_gcp=False)
    assert res.valid == 5 and res.gps_count == 5
    assert not res.warnings
    assert res.cameras.get("DJI Mavic 3E") == 5


def test_corrupt_jpeg_detected_and_counted(tmp_path):
    imgs = _images(tmp_path, 4)
    bad = tmp_path / "broken.jpg"
    bad.write_bytes(b"\xff\xd8\xff garbage not a jpeg")
    imgs.append(("broken.jpg", str(bad)))
    res = run_preflight(imgs, min_images=3, has_gcp=False)
    assert res.corrupt == ["broken.jpg"] and res.valid == 4


def test_zero_byte_file_detected(tmp_path):
    imgs = _images(tmp_path, 3)
    empty = tmp_path / "empty.jpg"
    empty.write_bytes(b"")
    imgs.append(("empty.jpg", str(empty)))
    res = run_preflight(imgs, min_images=3, has_gcp=False)
    assert res.zero_byte == ["empty.jpg"]


def test_duplicate_hashes_skipped_with_warning(tmp_path):
    imgs = _images(tmp_path, 3)
    dup = tmp_path / "copy.jpg"
    dup.write_bytes(open(imgs[0][1], "rb").read())
    imgs.append(("copy.jpg", str(dup)))
    res = run_preflight(imgs, min_images=3, has_gcp=False)
    assert res.valid == 3 and len(res.duplicate_hashes) == 1
    assert any("byte-identical" in w for w in res.warnings)


def test_missing_gps_warns_without_gcp_but_not_with(tmp_path):
    imgs = _images(tmp_path, 5, gps=False)
    res = run_preflight(imgs, min_images=3, has_gcp=False)
    assert res.gps_count == 0
    assert any("GPS" in w for w in res.warnings)
    res2 = run_preflight(imgs, min_images=3, has_gcp=True)
    assert not any("GPS" in w for w in res2.warnings)


def test_too_few_valid_images_is_actionable_error(tmp_path):
    imgs = _images(tmp_path, 2)
    with pytest.raises(PreflightError, match="minimum 3"):
        run_preflight(imgs, min_images=3, has_gcp=False)


def test_gcp_validation():
    validate_gcp_text(VALID_GCP)
    with pytest.raises(PreflightError, match="CRS"):
        validate_gcp_text(b"1 2 3 4 5 a.jpg\n" * 4)
    with pytest.raises(PreflightError, match="at least 3"):
        validate_gcp_text(b"EPSG:32613\n1 2 3 4 5 a.jpg\n")
    with pytest.raises(PreflightError, match="fields"):
        validate_gcp_text(b"EPSG:32613\n1 2 3\n1 2 3\n1 2 3\n")
