"""Batch export of approved ROIs from the dashboard."""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
from PIL import Image

from stroi_studio.app import create_app


def _seed_computed(app, sid, annotation):
    c = app.test_client()
    import io
    buf = io.BytesIO()
    Image.fromarray(annotation).save(buf, "PNG")
    buf.seek(0)
    c.post(f"/slide/{sid}/annotation", data={"annotation": (buf, "a.png")},
           content_type="multipart/form-data")
    c.post(f"/slide/{sid}/compute")
    return c


def _cyan(H, W):
    img = np.zeros((H, W, 3), np.uint8)
    img[40:44, 40:160] = (0, 255, 255)
    img[120:124, 40:160] = (0, 255, 255)
    img[40:124, 40:44] = (0, 255, 255)
    img[40:124, 156:160] = (0, 255, 255)
    return img


def _wait_export(app, timeout=10):
    store = app.config["STORE"]
    runner = app.config["EXPORT_RUNNER"]
    t0 = time.time()
    while runner.is_running() and time.time() - t0 < timeout:
        time.sleep(0.05)
    return store.latest_export_run()


def test_export_requires_a_product(results_dir, tmp_path):
    app = create_app(results_dir=str(results_dir), studio_out=str(tmp_path / "o"))
    sid = app.config["STORE"].list_slides()[0]["slide_id"]
    _seed_computed(app, sid, _cyan(200, 240))
    app.test_client().post(f"/slide/{sid}/status", json={"review_status": "approved"})
    c = app.test_client()
    r = c.post("/export/run", json={"products": []})
    assert r.status_code == 400


def test_export_only_approved_with_roi(results_dir, tmp_path):
    """Nothing approved yet -> export refuses."""
    app = create_app(results_dir=str(results_dir), studio_out=str(tmp_path / "o"))
    sid = app.config["STORE"].list_slides()[0]["slide_id"]
    _seed_computed(app, sid, _cyan(200, 240))   # computed but NOT approved
    c = app.test_client()
    r = c.post("/export/run", json={"products": ["geojson"]})
    assert r.status_code == 400
    assert "approved" in r.get_json()["error"]


def test_export_geojson_for_approved(results_dir, tmp_path):
    app = create_app(results_dir=str(results_dir), studio_out=str(tmp_path / "o"))
    sid = app.config["STORE"].list_slides()[0]["slide_id"]
    c = _seed_computed(app, sid, _cyan(200, 240))
    c.post(f"/slide/{sid}/status", json={"review_status": "approved"})
    r = c.post("/export/run", json={"products": ["geojson"]})
    assert r.status_code == 200
    run = _wait_export(app)
    assert run["status"] == "done"
    assert run["n_done"] == 1
    # the geojson landed in the slide's studio output dir
    slide = app.config["STORE"].get_slide(sid)
    out = Path(slide["roi_png"]).parent
    assert list(out.glob("*_roi.geojson"))


def test_export_explicit_slide_ids(results_dir, tmp_path):
    app = create_app(results_dir=str(results_dir), studio_out=str(tmp_path / "o"))
    sid = app.config["STORE"].list_slides()[0]["slide_id"]
    c = _seed_computed(app, sid, _cyan(200, 240))   # not approved
    # explicit ids bypass the approved filter
    r = c.post("/export/run", json={"products": ["geojson"], "slide_ids": [sid]})
    assert r.status_code == 200
    run = _wait_export(app)
    assert run["status"] == "done" and run["n_done"] == 1


def test_export_status_endpoint(results_dir, tmp_path):
    app = create_app(results_dir=str(results_dir), studio_out=str(tmp_path / "o"))
    r = app.test_client().get("/export/status")
    assert r.status_code == 200
    body = r.get_json()
    assert "running" in body
