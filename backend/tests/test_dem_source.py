"""Tile-name math for all four hemisphere quadrants — pure string logic, no network."""

from app.dem_source import tile_name, tiles_for_bbox, tile_url


def test_ne_quadrant():
    assert tile_name(44.5, 6.5) == "Copernicus_DSM_COG_10_N44_00_E006_00_DEM"


def test_sw_quadrant():
    # (-20.5, -46.5) lies in the cell whose SW corner is S21 W047
    assert tile_name(-20.5, -46.5) == "Copernicus_DSM_COG_10_S21_00_W047_00_DEM"


def test_nw_quadrant():
    assert tile_name(10.5, -83.5) == "Copernicus_DSM_COG_10_N10_00_W084_00_DEM"


def test_se_quadrant():
    assert tile_name(-32.5, 151.5) == "Copernicus_DSM_COG_10_S33_00_E151_00_DEM"


def test_negative_fractional_floors_south_west():
    # -21.3 is inside the S22 cell (SW corner naming), not S21
    assert tile_name(-21.3, -47.2) == "Copernicus_DSM_COG_10_S22_00_W048_00_DEM"


def test_integer_corner_belongs_to_own_cell():
    assert tile_name(44.0, 6.0) == "Copernicus_DSM_COG_10_N44_00_E006_00_DEM"


def test_equator_and_meridian():
    assert tile_name(0.5, 0.5) == "Copernicus_DSM_COG_10_N00_00_E000_00_DEM"
    assert tile_name(-0.5, -0.5) == "Copernicus_DSM_COG_10_S01_00_W001_00_DEM"


def test_bbox_spanning_tiles():
    names = tiles_for_bbox(5.9, 44.9, 6.1, 45.1)
    assert set(names) == {
        "Copernicus_DSM_COG_10_N44_00_E005_00_DEM",
        "Copernicus_DSM_COG_10_N44_00_E006_00_DEM",
        "Copernicus_DSM_COG_10_N45_00_E005_00_DEM",
        "Copernicus_DSM_COG_10_N45_00_E006_00_DEM",
    }


def test_bbox_single_tile():
    assert tiles_for_bbox(6.1, 44.1, 6.2, 44.2) == [
        "Copernicus_DSM_COG_10_N44_00_E006_00_DEM"
    ]


def test_tile_url():
    name = "Copernicus_DSM_COG_10_N44_00_E006_00_DEM"
    assert tile_url(name) == (
        "/vsis3/copernicus-dem-30m/"
        "Copernicus_DSM_COG_10_N44_00_E006_00_DEM/"
        "Copernicus_DSM_COG_10_N44_00_E006_00_DEM.tif"
    )
