"""Phase 3: thumbnail ROI mask -> level-0 GeoJSON back-mapping."""
from __future__ import annotations

import numpy as np
from PIL import Image

from stroi_studio import mapping
from stroi_studio.app import create_app


def test_rectangle_to_four_vertices():
    m = np.zeros((20, 30), bool)
    m[5:15, 8:25] = True
    rings = mapping.mask_to_rings(m)
    assert len(rings) == 1
    ext = rings[0]["exterior"]
    assert len(ext) == 4               # collinear points merged
    xs = [p[0] for p in ext]
    ys = [p[1] for p in ext]
    assert (min(xs), max(xs)) == (8, 25)
    assert (min(ys), max(ys)) == (5, 15)
    assert mapping._signed_area(ext) > 0   # exterior is CCW


def test_anisotropic_non_integer_scaling():
    m = np.zeros((20, 30), bool)
    m[5:15, 8:25] = True
    gj = mapping.roi_to_geojson(m, downsample_x=14.27, downsample_y=15.99)
    coords = gj["features"][0]["geometry"]["coordinates"][0]
    xs = [p[0] for p in coords]
    ys = [p[1] for p in coords]
    assert abs(max(xs) - 25 * 14.27) < 1e-6
    assert abs(max(ys) - 15 * 15.99) < 1e-6
    # rings are explicitly closed
    assert coords[0] == coords[-1]


def test_hole_becomes_polygon_hole():
    m = np.zeros((40, 40), bool)
    m[5:35, 5:35] = True
    m[15:25, 15:25] = False                 # excluded island
    gj = mapping.roi_to_geojson(m, downsample_x=1.0, downsample_y=1.0)
    poly = gj["features"][0]["geometry"]["coordinates"]
    assert len(poly) == 2                   # exterior + 1 hole
    ext, holes = poly[0], poly[1:]
    assert not mapping.point_in_rings(20, 20, ext, holes)   # in the hole
    assert mapping.point_in_rings(8, 8, ext, holes)          # in the ROI


def test_two_components_two_features():
    m = np.zeros((20, 40), bool)
    m[3:8, 3:8] = True
    m[12:18, 25:35] = True
    gj = mapping.roi_to_geojson(m, downsample_x=2.0, downsample_y=2.0)
    assert len(gj["features"]) == 2


def test_roundtrip_membership_at_scale():
    """A level-0 point inside the scaled polygon maps back into the mask."""
    m = np.zeros((30, 30), bool)
    m[10:20, 6:24] = True
    sx, sy = 12.5, 13.3
    gj = mapping.roi_to_geojson(m, downsample_x=sx, downsample_y=sy)
    ext = gj["features"][0]["geometry"]["coordinates"][0]
    # centre of the rect in level-0 coords
    cx, cy = 15 * sx, 15 * sy
    assert mapping.point_in_rings(cx, cy, ext, [])
    # a point clearly outside
    assert not mapping.point_in_rings(2 * sx, 2 * sy, ext, [])


def test_empty_mask_no_features():
    gj = mapping.roi_to_geojson(np.zeros((10, 10), bool),
                                downsample_x=1.0, downsample_y=1.0)
    assert gj["features"] == []


def test_geojson_route(results_dir, tmp_path, cyan_annotation_png):
    app = create_app(results_dir=str(results_dir),
                     studio_out=str(tmp_path / "out"))
    c = app.test_client()
    sid = app.config["STORE"].list_slides()[0]["slide_id"]
    with open(cyan_annotation_png, "rb") as fh:
        c.post(f"/slide/{sid}/annotation", data={"annotation": (fh, "a.png")},
               content_type="multipart/form-data")
    r = c.post(f"/slide/{sid}/compute").get_json()
    assert r["n_polygons"] >= 1
    assert "geojson_url" in r
    resp = c.get(f"/slide/{sid}/roi.geojson")
    assert resp.status_code == 200
    gj = resp.get_json()
    assert gj["type"] == "FeatureCollection"
    # coordinates are in level-0 space: larger than thumbnail extent
    coords = gj["features"][0]["geometry"]["coordinates"][0]
    assert max(p[0] for p in coords) > 240        # thumb is 240 wide
    # roi.json sidecar is also served
    assert c.get(f"/slide/{sid}/roi.json").status_code == 200
