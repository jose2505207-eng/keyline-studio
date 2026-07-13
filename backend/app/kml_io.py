"""KML/KMZ/GeoJSON boundary import and styled KML results export.

Import accepts a Google Earth-exported polygon (KML or KMZ) or a GeoJSON
file and returns the first polygon found. Export builds a KML document with
valleys/ridges/keypoints/keylines as separate styled folders plus the AOI
boundary, openable directly in Google Earth — and re-importable through the
import path (round-trip covered by tests).
"""

from __future__ import annotations

import io
import json
import zipfile
from xml.sax.saxutils import escape

from fastkml import kml as fastkml_kml


class BoundaryError(ValueError):
    pass


def _polygon_from_geojson(obj: dict) -> dict | None:
    if obj.get("type") == "Polygon":
        return obj
    if obj.get("type") == "Feature":
        return _polygon_from_geojson(obj.get("geometry") or {})
    if obj.get("type") == "FeatureCollection":
        for feat in obj.get("features", []):
            found = _polygon_from_geojson(feat)
            if found:
                return found
    if obj.get("type") == "MultiPolygon" and obj.get("coordinates"):
        return {"type": "Polygon", "coordinates": obj["coordinates"][0]}
    return None


def _walk_kml_features(node):
    for feat in getattr(node, "features", None) or []:
        geom = getattr(feat, "geometry", None)
        if geom is not None:
            yield geom
        yield from _walk_kml_features(feat)


def _polygon_from_kml_bytes(data: bytes) -> dict | None:
    k = fastkml_kml.KML.from_string(data)
    for geom in _walk_kml_features(k):
        gi = getattr(geom, "__geo_interface__", None)
        if not gi:
            continue
        found = _polygon_from_geojson(dict(gi))
        if found:
            # strip altitude if present: [[x, y, z], ...] -> [[x, y], ...]
            found["coordinates"] = [
                [[float(c[0]), float(c[1])] for c in ring]
                for ring in found["coordinates"]
            ]
            return found
    return None


def parse_boundary(filename: str, data: bytes) -> dict:
    """Extract the first polygon (WGS84 GeoJSON) from a KML/KMZ/GeoJSON file."""
    name = (filename or "").lower()
    if name.endswith(".kmz"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                kml_names = [n for n in zf.namelist() if n.lower().endswith(".kml")]
                if not kml_names:
                    raise BoundaryError("KMZ archive contains no .kml document")
                # Google Earth puts the main document at doc.kml
                kml_names.sort(key=lambda n: (n.lower() != "doc.kml", n))
                data = zf.read(kml_names[0])
        except zipfile.BadZipFile:
            raise BoundaryError("Not a valid KMZ (zip) file")
        name = "doc.kml"

    if name.endswith(".kml"):
        try:
            poly = _polygon_from_kml_bytes(data)
        except Exception as exc:
            raise BoundaryError(f"Could not parse KML: {exc}")
    elif name.endswith(".geojson") or name.endswith(".json"):
        try:
            poly = _polygon_from_geojson(json.loads(data))
        except json.JSONDecodeError as exc:
            raise BoundaryError(f"Not valid GeoJSON: {exc}")
    else:
        raise BoundaryError("Unsupported file type — use .kml, .kmz or .geojson")

    if poly is None:
        raise BoundaryError(
            "No polygon found in the file — export your boundary as a polygon "
            "(not a path/line or point placemark) and try again.")
    return poly


# ---------------------------------------------------------------------------
# Export

# KML colors are aabbggrr
_STYLES = {
    "valley": ("ff f6 82 3b", 2.5),
    "ridge": ("ff 3d 71 b4", 2.5),
    "keyline": ("ff 1f 84 12", 4.0),
    "aoi": ("ff 00 d0 ff", 3.0),
}


def _coords_str(coords) -> str:
    return " ".join(f"{x:.7f},{y:.7f},0" for x, y in coords)


def _line_placemark(geom: dict, name: str, style: str) -> str:
    return (f"<Placemark><name>{escape(name)}</name>"
            f"<styleUrl>#{style}</styleUrl><LineString><tessellate>1</tessellate>"
            f"<coordinates>{_coords_str(geom['coordinates'])}</coordinates>"
            f"</LineString></Placemark>")


def results_to_kml(fc: dict, aoi: dict | None, doc_name: str = "Keyline Studio") -> str:
    styles = []
    for sid, (color, width) in _STYLES.items():
        styles.append(
            f'<Style id="{sid}"><LineStyle><color>{color.replace(" ", "")}</color>'
            f"<width>{width}</width></LineStyle>"
            '<PolyStyle><fill>0</fill><outline>1</outline></PolyStyle></Style>')
    styles.append(
        '<Style id="keypoint"><IconStyle><color>ff1f8412</color><scale>1.2</scale>'
        "<Icon><href>http://maps.google.com/mapfiles/kml/shapes/placemark_circle.png"
        "</href></Icon></IconStyle></Style>")

    folders: dict[str, list[str]] = {"Keylines": [], "Keypoints": [],
                                     "Valleys": [], "Ridges": []}
    for feat in fc.get("features", []):
        props = feat.get("properties", {}) or {}
        kind = props.get("kind")
        geom = feat.get("geometry", {})
        fid = props.get("id", "")
        if kind == "valley" and geom.get("type") == "LineString":
            folders["Valleys"].append(_line_placemark(geom, f"Valley {fid}", "valley"))
        elif kind == "ridge" and geom.get("type") == "LineString":
            folders["Ridges"].append(_line_placemark(geom, f"Ridge {fid}", "ridge"))
        elif kind == "keyline" and geom.get("type") == "LineString":
            folders["Keylines"].append(
                _line_placemark(geom, f"Keyline ({props.get('keypoint_id', '')})",
                                "keyline"))
        elif kind == "keypoint" and geom.get("type") == "Point":
            lon, lat = geom["coordinates"][:2]
            desc = (f"Elevation: {props.get('elevation')} m; "
                    f"confidence: {props.get('confidence')}; "
                    f"source: {props.get('source')}. Computational suggestion — "
                    "field verification required before any earthworks.")
            folders["Keypoints"].append(
                f"<Placemark><name>Keypoint {escape(str(fid))}</name>"
                f"<description>{escape(desc)}</description>"
                "<styleUrl>#keypoint</styleUrl>"
                f"<Point><coordinates>{lon:.7f},{lat:.7f},0</coordinates></Point>"
                "</Placemark>")

    folder_xml = []
    if aoi is not None:
        ring = _coords_str(aoi["coordinates"][0])
        folder_xml.append(
            "<Folder><name>AOI boundary</name>"
            '<Placemark><name>AOI</name><styleUrl>#aoi</styleUrl>'
            "<Polygon><tessellate>1</tessellate><outerBoundaryIs><LinearRing>"
            f"<coordinates>{ring}</coordinates>"
            "</LinearRing></outerBoundaryIs></Polygon></Placemark></Folder>")
    for fname, items in folders.items():
        if items:
            folder_xml.append(f"<Folder><name>{fname}</name>{''.join(items)}</Folder>")

    warning = (fc.get("properties") or {}).get("warning")
    desc = escape(warning) if warning else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        f"<name>{escape(doc_name)}</name>"
        + (f"<description>{desc}</description>" if desc else "")
        + "".join(styles) + "".join(folder_xml)
        + "</Document></kml>"
    )
