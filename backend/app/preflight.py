"""Image preflight checks run by the worker before submitting to the
photogrammetry engine.

Deliberately honest about its limits: EXIF alone cannot establish flight
overlap, so none is claimed. Missing GPS on a few frames is a warning, not a
rejection — but a dataset with too few geotagged images and no GCP file gets
a strong warning since the reconstruction will not be georeferenced.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections import Counter
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_EXIF_MAKE, _EXIF_MODEL, _EXIF_GPS = 271, 272, 34853


class PreflightError(ValueError):
    pass


@dataclass
class PreflightResult:
    total: int = 0
    valid: int = 0
    corrupt: list[str] = field(default_factory=list)
    zero_byte: list[str] = field(default_factory=list)
    duplicate_names: list[str] = field(default_factory=list)
    duplicate_hashes: list[str] = field(default_factory=list)
    gps_count: int = 0
    cameras: dict[str, int] = field(default_factory=dict)
    dimension_groups: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "valid": self.valid,
            "corrupt": self.corrupt[:20],
            "zero_byte": self.zero_byte[:20],
            "duplicate_names": self.duplicate_names[:20],
            "duplicate_hashes": self.duplicate_hashes[:20],
            "gps_count": self.gps_count,
            "cameras": self.cameras,
            "dimension_groups": self.dimension_groups,
            "warnings": self.warnings,
        }


def _md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run_preflight(image_paths: list[tuple[str, str]], min_images: int,
                  has_gcp: bool) -> PreflightResult:
    """Check materialized images. ``image_paths`` is (original_name, path).

    Raises PreflightError when too few usable images remain; otherwise
    returns a result whose ``warnings`` list is surfaced to the user.
    """
    from PIL import Image

    res = PreflightResult(total=len(image_paths))
    seen_names: Counter[str] = Counter()
    seen_hashes: dict[str, str] = {}
    usable: list[tuple[str, str]] = []

    for name, path in image_paths:
        seen_names[name] += 1
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            res.zero_byte.append(name)
            continue
        try:
            with Image.open(path) as img:
                img.verify()
            with Image.open(path) as img:
                if img.format != "JPEG":
                    raise ValueError(f"not a JPEG (got {img.format})")
                dims = f"{img.width}x{img.height}"
                exif = img.getexif()
        except Exception as exc:  # noqa: BLE001 — every decode failure = corrupt
            log.info("preflight: corrupt image %s: %s", name, exc)
            res.corrupt.append(name)
            continue

        digest = _md5(path)
        if digest in seen_hashes:
            res.duplicate_hashes.append(f"{name} == {seen_hashes[digest]}")
            continue
        seen_hashes[digest] = name

        res.dimension_groups[dims] = res.dimension_groups.get(dims, 0) + 1
        make = str(exif.get(_EXIF_MAKE, "")).strip()
        model = str(exif.get(_EXIF_MODEL, "")).strip()
        camera = (f"{make} {model}".strip()) or "unknown"
        res.cameras[camera] = res.cameras.get(camera, 0) + 1
        # get_ifd is the safe accessor; a raw int offset means no parsed GPS
        try:
            gps = exif.get_ifd(_EXIF_GPS)
        except Exception:  # noqa: BLE001 — malformed GPS IFDs are common
            gps = None
        if gps:
            res.gps_count += 1
        usable.append((name, path))

    res.duplicate_names = [n for n, c in seen_names.items() if c > 1]
    res.valid = len(usable)

    if res.valid < min_images:
        raise PreflightError(
            f"Only {res.valid} usable photographs out of {res.total} "
            f"(minimum {min_images}). Corrupt: {len(res.corrupt)}, "
            f"zero-byte: {len(res.zero_byte)}, duplicates: "
            f"{len(res.duplicate_hashes)}.")

    if res.duplicate_names:
        res.warnings.append(
            f"{len(res.duplicate_names)} duplicate filenames were uploaded")
    if res.duplicate_hashes:
        res.warnings.append(
            f"{len(res.duplicate_hashes)} images are byte-identical "
            "duplicates and were skipped")
    if len(res.dimension_groups) > 2:
        res.warnings.append(
            "Images have inconsistent dimensions "
            f"({len(res.dimension_groups)} distinct sizes) — mixed cameras "
            "or crops can degrade reconstruction")
    gps_frac = res.gps_count / max(res.valid, 1)
    if gps_frac < 0.8 and not has_gcp:
        res.warnings.append(
            f"Only {res.gps_count} of {res.valid} images carry GPS EXIF and "
            "no ground-control file was supplied — the reconstruction may "
            "not be georeferenced. Add a GCP file or use geotagged photos.")
    return res


def validate_gcp_text(data: bytes) -> None:
    """Validate an OpenDroneMap gcp_list.txt: a CRS header line followed by
    >= 3 lines of `geo_x geo_y geo_z im_x im_y image_name`."""
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise PreflightError("GCP file is not UTF-8 text")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < 4:
        raise PreflightError(
            "GCP file needs a CRS header plus at least 3 control points")
    header = lines[0]
    if not any(tok in header.upper() for tok in ("EPSG", "PROJ", "WGS84", "UTM", "+")):
        raise PreflightError(
            "GCP first line must declare the CRS (e.g. 'EPSG:32613' or a "
            "proj string)")
    for i, ln in enumerate(lines[1:], start=2):
        parts = ln.split()
        if len(parts) < 6:
            raise PreflightError(
                f"GCP line {i} has {len(parts)} fields; expected at least 6 "
                "(geo_x geo_y geo_z im_x im_y image_name)")
        try:
            [float(p) for p in parts[:5]]
        except ValueError:
            raise PreflightError(f"GCP line {i} has non-numeric coordinates")
