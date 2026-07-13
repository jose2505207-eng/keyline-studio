"""KML/KMZ/GeoJSON boundary import + styled KML export round-trip."""

import io
import json
import zipfile

import pytest

from app.kml_io import BoundaryError, parse_boundary, results_to_kml

# As exported by Google Earth (Document > Placemark > Polygon, altitude 0,
# gx namespace present).
GE_KML = b"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2" xmlns:kml="http://www.opengis.net/kml/2.2" xmlns:atom="http://www.w3.org/2005/Atom">
<Document>
  <name>rancho.kml</name>
  <Placemark>
    <name>Rancho El Encino</name>
    <styleUrl>#m_ylw-pushpin</styleUrl>
    <Polygon>
      <tessellate>1</tessellate>
      <outerBoundaryIs><LinearRing><coordinates>
        -101.9901,21.4198,0 -101.9552,21.4201,0 -101.9548,21.4405,0 -101.9899,21.4402,0 -101.9901,21.4198,0
      </coordinates></LinearRing></outerBoundaryIs>
    </Polygon>
  </Placemark>
</Document>
</kml>"""


def test_google_earth_kml_polygon_imports():
    poly = parse_boundary("rancho.kml", GE_KML)
    assert poly["type"] == "Polygon"
    ring = poly["coordinates"][0]
    assert len(ring) == 5
    assert ring[0] == pytest.approx([-101.9901, 21.4198])


def test_kmz_imports():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("doc.kml", GE_KML)
    poly = parse_boundary("rancho.kmz", buf.getvalue())
    assert poly["type"] == "Polygon"


def test_geojson_feature_imports():
    fc = {"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {},
        "geometry": {"type": "Polygon",
                     "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
    }]}
    poly = parse_boundary("area.geojson", json.dumps(fc).encode())
    assert poly["coordinates"][0][1] == [1, 0]


def test_kml_without_polygon_rejected():
    point_kml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <kml xmlns="http://www.opengis.net/kml/2.2"><Document>
    <Placemark><Point><coordinates>-101.9,21.4,0</coordinates></Point></Placemark>
    </Document></kml>"""
    with pytest.raises(BoundaryError, match="No polygon"):
        parse_boundary("point.kml", point_kml)


def test_export_kml_roundtrips_through_import():
    aoi = {"type": "Polygon", "coordinates": [[
        [-101.99, 21.42], [-101.955, 21.42], [-101.955, 21.44],
        [-101.99, 21.44], [-101.99, 21.42]]]}
    fc = {"type": "FeatureCollection",
          "properties": {"warning": None, "relief_m": 42.0},
          "features": [
              {"type": "Feature",
               "geometry": {"type": "LineString",
                            "coordinates": [[-101.98, 21.43], [-101.97, 21.431]]},
               "properties": {"kind": "keyline", "keypoint_id": "k0", "id": "l0"}},
              {"type": "Feature",
               "geometry": {"type": "Point", "coordinates": [-101.98, 21.43]},
               "properties": {"kind": "keypoint", "id": "k0", "elevation": 1912.5,
                              "confidence": 0.71, "source": "satellite"}},
              {"type": "Feature",
               "geometry": {"type": "LineString",
                            "coordinates": [[-101.985, 21.425], [-101.96, 21.437]]},
               "properties": {"kind": "valley", "id": "v0"}},
              {"type": "Feature",
               "geometry": {"type": "LineString",
                            "coordinates": [[-101.988, 21.421], [-101.957, 21.423]]},
               "properties": {"kind": "ridge", "id": "r0"}},
          ]}

    kml_text = results_to_kml(fc, aoi, "test export")
    assert "<Folder><name>Keylines</name>" in kml_text
    assert "confidence: 0.71" in kml_text
    assert "source: satellite" in kml_text

    # the exported document re-imports through the same path (AOI polygon)
    poly = parse_boundary("export.kml", kml_text.encode())
    assert poly["type"] == "Polygon"
    assert poly["coordinates"][0][0] == pytest.approx([-101.99, 21.42])
