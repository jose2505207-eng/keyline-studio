"""Result exports beyond the full GeoJSON/KML: keylines-only files, a
GeoPackage of all terrain layers, and DXF for CAD/field-layout workflows.

Keyline-specific exports refuse with an explanatory error when the result
contains no keylines — an empty file would just move the confusion into
QGIS/Google Earth/CAD.
"""

from __future__ import annotations

import json
import os
import tempfile

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
