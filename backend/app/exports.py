"""Result exports beyond the full GeoJSON/KML: keylines-only files, a
GeoPackage of all terrain layers, and DXF for CAD/field-layout workflows.

Keyline-specific exports refuse with an explanatory error when the result
contains no keylines — an empty file would just move the confusion into
QGIS/Google Earth/CAD.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import zipfile

from .kml_io import results_to_kml


class ExportUnavailable(ValueError):
    """No exportable content of the requested kind; message says why."""


def _features(fc: dict, kinds: set[str]) -> list[dict]:
    return [f for f in fc.get("features", [])
            if (f.get("properties") or {}).get("kind") in kinds]


def _keyline_reason(fc: dict) -> str:
    props = fc.get("properties") or {}
    reasons = props.get("keypoint_reasons") or []
    base = "This analysis produced no keylines"
    if reasons:
        return f"{base}: {' '.join(reasons)}"
    return base + "."


def keylines_geojson(fc: dict) -> dict:
    feats = _features(fc, {"keyline", "keypoint"})
    keylines = [f for f in feats
                if f["properties"]["kind"] == "keyline"]
    if not keylines:
        raise ExportUnavailable(_keyline_reason(fc))
    props = fc.get("properties") or {}
    return {
        "type": "FeatureCollection",
        "features": feats,
        "properties": {
            "layer": "keylines",
            "project_id": props.get("project_id"),
            "analysis_run_id": props.get("analysis_run_id"),
            "survey_id": props.get("survey_id"),
        },
    }


def keylines_kml(fc: dict, doc_name: str) -> str:
    sub = keylines_geojson(fc)  # raises when empty
    return results_to_kml(sub, aoi=None, doc_name=doc_name)


def terrain_gpkg(fc: dict, out_path: str) -> str:
    """All terrain layers as a multi-layer GeoPackage via geopandas."""
    import geopandas as gpd
    from shapely.geometry import shape

    wrote_any = False
    for kind, layer in (("valley", "valleys"), ("ridge", "ridges"),
                        ("keypoint", "keypoints"), ("keyline", "keylines"),
                        ("contour", "contours")):
        feats = _features(fc, {kind})
        if not feats:
            continue
        gdf = gpd.GeoDataFrame(
            [{k: v for k, v in (f.get("properties") or {}).items()
              if not isinstance(v, (dict, list))} for f in feats],
            geometry=[shape(f["geometry"]) for f in feats],
            crs="EPSG:4326",
        )
        gdf.to_file(out_path, layer=layer, driver="GPKG",
                    mode="a" if wrote_any else "w")
        wrote_any = True
    if not wrote_any:
        raise ExportUnavailable(
            "This analysis produced no vector features to package.")
    return out_path


def keylines_dxf(fc: dict, out_path: str) -> str:
    """Keylines + keypoints as DXF polylines/points (WGS84 lon/lat coords;
    CAD users typically re-project on import)."""
    import ezdxf

    keylines = _features(fc, {"keyline"})
    if not keylines:
        raise ExportUnavailable(_keyline_reason(fc))

    doc = ezdxf.new(dxfversion="R2010")
    doc.layers.add("KEYLINES", color=3)   # green
    doc.layers.add("KEYPOINTS", color=1)  # red
    msp = doc.modelspace()
    for f in keylines:
        coords = f["geometry"]["coordinates"]
        p = f.get("properties") or {}
        pl = msp.add_lwpolyline([(c[0], c[1]) for c in coords],
                                dxfattribs={"layer": "KEYLINES"})
        pl.dxf.thickness = 0
        # attach useful attributes as XDATA-ish extended tags via a comment
        # layerless MTEXT label at the line start with the key facts
        label = (f"{p.get('id', '')} kp={p.get('keypoint_id', '')} "
                 f"elev={p.get('elevation', '')}m len={p.get('length_m', '')}m")
        msp.add_mtext(label, dxfattribs={"layer": "KEYLINES",
                                         "char_height": 0.00003,
                                         "insert": tuple(coords[0][:2])})
    for f in _features(fc, {"keypoint"}):
        x, y = f["geometry"]["coordinates"][:2]
        msp.add_point((x, y), dxfattribs={"layer": "KEYPOINTS"})
    doc.saveas(out_path)
    return out_path


def export_availability(fc: dict) -> dict:
    """What the frontend may offer, with reasons for what it may not."""
    counts = (fc.get("properties") or {}).get("counts") or {}
    has_keylines = bool(counts.get("keylines"))
    has_any = any(counts.get(k) for k in
                  ("valleys", "ridges", "keypoints", "keylines", "contours"))
    return {
        "geojson": True,
        "kml": True,
        "keylines_geojson": has_keylines,
        "keylines_kml": has_keylines,
        "keylines_dxf": has_keylines,
        "gpkg": has_any,
        "unavailable_reason": None if has_keylines else _keyline_reason(fc),
    }


# ---------------------------------------------------------------------------
# Design products (original DTM, terrain layers, summary, ZIP package)


def safe_filename(name: str, default: str = "keyline") -> str:
    """A filesystem-safe base name (no path separators / traversal)."""
    base = os.path.basename(name or "")
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-._")
    return base or default


def terrain_layers_geojson(fc: dict) -> dict:
    """All terrain vectors (valleys, ridges, keypoints, contours) — the
    context layers, separate from the keyline design."""
    feats = _features(fc, {"valley", "ridge", "keypoint", "contour"})
    props = fc.get("properties") or {}
    return {
        "type": "FeatureCollection",
        "features": feats,
        "properties": {
            "layer": "terrain",
            "project_id": props.get("project_id"),
            "analysis_run_id": props.get("analysis_run_id"),
            "counts": props.get("counts"),
        },
    }


def analysis_summary(fc: dict, run: dict | None = None,
                     project: dict | None = None) -> dict:
    """Human/machine summary of the analysis run for the design package."""
    props = fc.get("properties") or {}
    return {
        "project_id": props.get("project_id"),
        "project_name": (project or {}).get("name"),
        "analysis_run_id": props.get("analysis_run_id"),
        "survey_id": props.get("survey_id"),
        "analysis_version": (run or {}).get("analysis_version"),
        "dem_mode": props.get("dem_mode"),
        "terrain_source": (run or {}).get("terrain_source") or props.get("dem_mode"),
        "dem_resolution_m": props.get("dem_resolution_m"),
        "drone_coverage": props.get("drone_coverage"),
        "analysis_crs": props.get("analysis_crs"),
        "dem_bounds_wgs84": props.get("dem_bounds_wgs84"),
        "relief_m": props.get("relief_m"),
        "status": props.get("status"),
        "counts": props.get("counts"),
        "notices": props.get("notices"),
        "keypoint_reasons": props.get("keypoint_reasons"),
        "warning": props.get("warning"),
        "generated_at": time.time(),
    }


def resolve_original_dtm(run: dict, out_dir: str) -> tuple[str, str] | None:
    """(path, download_filename) of the untouched elevation raster used by the
    analysis. For an existing/drone DTM this is the exact source file (bytes
    unchanged); for a satellite-only run it is the reprojected analysis DEM,
    which is the elevation raster the analysis actually used."""
    dem_path = run.get("dem_path")
    if dem_path and os.path.isfile(dem_path):
        return dem_path, "original-dtm.tif"
    fallback = os.path.join(out_dir, "dem_utm.tif")
    if os.path.isfile(fallback):
        return fallback, "original-dtm.tif"
    return None


def _readme_text(fc: dict, run: dict | None, project: dict | None) -> str:
    props = fc.get("properties") or {}
    counts = props.get("counts") or {}
    name = (project or {}).get("name") or props.get("project_id") or "project"
    lines = [
        f"Keyline Studio — design package for {name}",
        "=" * 60,
        "",
        "CANDIDATE DESIGN ONLY. These keylines and keypoints are computational",
        "suggestions. Field verification by a qualified practitioner is",
        "required before any earthworks.",
        "",
        f"Analysis run:      {props.get('analysis_run_id')}",
        f"Analysis version:  {(run or {}).get('analysis_version')}",
        f"Terrain source:    {props.get('dem_mode')}",
        f"DEM resolution:    {props.get('dem_resolution_m')} m/px",
        f"Analysis CRS:      {props.get('analysis_crs')}",
        f"AOI bounds (WGS84):{props.get('dem_bounds_wgs84')}",
        f"Terrain relief:    {props.get('relief_m')} m",
        f"Status:            {props.get('status')}",
        "",
        "Feature counts:",
        f"  Valleys:   {counts.get('valleys', 0)}",
        f"  Ridges:    {counts.get('ridges', 0)}",
        f"  Keypoints: {counts.get('keypoints', 0)}",
        f"  Keylines:  {counts.get('keylines', 0)}",
        f"  Contours:  {counts.get('contours', 0)}",
        "",
    ]
    if not counts.get("keylines"):
        lines += ["NO KEYLINE FOUND for this analysis.",
                  _keyline_reason(fc), ""]
    warn = props.get("warning")
    if warn:
        lines += ["QA / reliability warning:", f"  {warn}", ""]
    lines += [
        "Files:",
        "  original-dtm.tif      Untouched elevation raster used for analysis.",
        "  keyline-design-map.tif Visual georeferenced map (NOT elevation).",
        "  keylines.geojson      Candidate keylines + keypoints (EPSG:4326).",
        "  keylines.kml          Candidate keylines for Google Earth.",
        "  terrain-layers.geojson Valleys, ridges, contours (context).",
        "  analysis-summary.json Machine-readable run summary.",
        "  terrain-qa.json       Terrain quality-assurance report.",
        "  orthophoto.tif        Orthophoto (when available).",
        "  manifest.json         Package contents + checksums.",
        "",
        "Elevation data may include Copernicus GLO-30 (c) DLR/Airbus, ESA/EU.",
    ]
    return "\n".join(lines)


def build_design_package(zip_path: str, *, out_dir: str, fc: dict,
                         run: dict | None = None, project: dict | None = None,
                         original_dtm: tuple[str, str] | None = None,
                         orthophoto_path: str | None = None) -> str:
    """Assemble the complete design ZIP atomically.

    All archive names are fixed literals under a sanitised project folder, so
    the package can never contain a traversal path. Written to a temp file and
    renamed into place; the caller streams the finished file.
    """
    props = fc.get("properties") or {}
    folder = safe_filename(
        (project or {}).get("name") or props.get("project_id") or "keyline",
        "keyline-design")
    manifest: dict = {"folder": folder, "files": [],
                      "analysis_run_id": props.get("analysis_run_id"),
                      "generated_at": time.time()}

    def _arc(name: str) -> str:
        return f"{folder}/{safe_filename(name)}"

    os.makedirs(os.path.dirname(os.path.abspath(zip_path)), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(zip_path)),
                               suffix=".zip.tmp")
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            def write_bytes(name: str, data: bytes) -> None:
                arc = _arc(name)
                zf.writestr(arc, data)
                manifest["files"].append({"name": arc, "bytes": len(data)})

            def write_file(name: str, path: str) -> None:
                if not path or not os.path.isfile(path):
                    return
                arc = _arc(name)
                zf.write(path, arcname=arc)
                manifest["files"].append(
                    {"name": arc, "bytes": os.path.getsize(path)})

            write_bytes("README.txt", _readme_text(fc, run, project).encode())
            if original_dtm:
                write_file("original-dtm.tif", original_dtm[0])
            write_file("keyline-design-map.tif",
                       os.path.join(out_dir, "keyline-design-map.tif"))
            # keyline design (only when present)
            try:
                write_bytes("keylines.geojson",
                            json.dumps(keylines_geojson(fc)).encode())
                write_bytes("keylines.kml",
                            keylines_kml(fc, f"Keylines — {folder}").encode())
            except ExportUnavailable:
                pass
            write_bytes("terrain-layers.geojson",
                        json.dumps(terrain_layers_geojson(fc)).encode())
            write_bytes("analysis-summary.json",
                        json.dumps(analysis_summary(fc, run, project),
                                   indent=2).encode())
            write_bytes("terrain-qa.json",
                        json.dumps((run or {}).get("qa_json")
                                   or props.get("qa") or {}, indent=2).encode())
            if orthophoto_path:
                write_file("orthophoto.tif", orthophoto_path)
            write_bytes("manifest.json", json.dumps(manifest, indent=2).encode())
        os.replace(tmp, zip_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return zip_path


def _write_atomic_bytes(path: str, data: bytes) -> None:
    from .spatial import atomic_write_bytes

    atomic_write_bytes(path, data)


def generate_run_exports(out_dir: str, fc: dict,
                         aoi_wgs84: dict | None = None) -> dict:
    """Produce the standing export files for a completed run under
    ``<out_dir>/exports/`` and report availability.

    Never reruns hydrology; only reads the run's existing rasters/vectors. An
    export failure is reported (not raised) so a completed terrain analysis is
    never demoted to failed by an optional download.
    """
    counts = (fc.get("properties") or {}).get("counts") or {}
    has_keylines = bool(counts.get("keylines"))
    has_vectors = any(counts.get(k) for k in
                      ("valleys", "ridges", "keypoints", "keylines", "contours"))
    avail = {
        "original_dtm": True,  # resolved at download time
        "keylines_geojson": has_keylines,
        "keylines_kml": has_keylines,
        "terrain_layers_geojson": has_vectors,
        "terrain_gpkg": False,
        "visual_geotiff": False,
        "design_bundle": True,
        "errors": {},
    }
    exports_dir = os.path.join(out_dir, "exports")
    os.makedirs(exports_dir, exist_ok=True)

    # Keyline vector files: materialized so they can be registered as
    # verifiable artifacts (checksums, sizes) instead of built per request.
    if has_keylines:
        try:
            sub = keylines_geojson(fc)
            _write_atomic_bytes(os.path.join(exports_dir, "keylines.geojson"),
                                json.dumps(sub).encode())
            doc_name = (fc.get("properties") or {}).get("project_id") or "keylines"
            _write_atomic_bytes(
                os.path.join(exports_dir, "keylines.kml"),
                keylines_kml(fc, f"Keylines — {doc_name}").encode())
        except (ExportUnavailable, OSError) as exc:
            avail["errors"]["keylines_files"] = str(exc)

    if has_vectors:
        try:
            terrain_gpkg(fc, os.path.join(exports_dir, "terrain.gpkg"))
            avail["terrain_gpkg"] = True
        except Exception as exc:  # noqa: BLE001 — optional export
            avail["errors"]["terrain_gpkg"] = str(exc)

    # Visual GeoTIFF: a diagnostic terrain map is still useful with no keyline,
    # so build it whenever there is any raster to render.
    dest = os.path.join(out_dir, "keyline-design-map.tif")
    try:
        from .visual_export import build_visual_geotiff

        build_visual_geotiff(out_dir, dest, aoi_wgs84=aoi_wgs84)
        avail["visual_geotiff"] = True
        avail["visual_is_diagnostic"] = not has_keylines
    except Exception as exc:  # noqa: BLE001 — optional export, report don't raise
        avail["errors"]["visual_geotiff"] = str(exc)
    return avail
