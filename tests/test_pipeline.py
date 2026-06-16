"""Phase 2: canvas_io colour splitting + roi_pipeline + /compute route."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image

from stroi_studio import canvas_io
from stroi_studio.app import create_app
from stroi_studio.roi_pipeline import compute_roi
from stroi_studio.state import Store


def _flatten(H, W):
    img = np.zeros((H, W, 3), np.uint8)
    # cyan rectangular loop (stroke width 4); interior is rows 64-136, x 64-176
    img[60:64, 60:180] = (0, 255, 255)
    img[136:140, 60:180] = (0, 255, 255)
    img[60:140, 60:64] = (0, 255, 255)
    img[60:140, 176:180] = (0, 255, 255)
    img[150:170, 50:120] = (0, 255, 0)     # green add-back (below the loop)
    img[95:115, 90:130] = (255, 0, 0)      # red exclude (inside the loop)
    return img


def test_split_layers_clean_separation():
    layers = canvas_io.split_layers(_flatten(200, 240))
    assert int(layers["loop"].sum()) > 100
    # green/red here are SOLID blocks, so region-fill is a no-op on them.
    assert int(layers["add_back"].sum()) == 1400      # 20*70
    assert int(layers["exclude"].sum()) == 800        # 20*40
    # No cross-talk between colour layers.
    assert int((layers["loop"] & layers["add_back"]).sum()) == 0
    assert int((layers["loop"] & layers["exclude"]).sum()) == 0
    assert int((layers["add_back"] & layers["exclude"]).sum()) == 0


def test_green_loop_fills_interior():
    """A thin GREEN loop adds back its enclosed area, not just the stroke."""
    img = np.zeros((200, 240, 3), np.uint8)
    img[60:64, 60:180] = (0, 255, 0)
    img[136:140, 60:180] = (0, 255, 0)
    img[60:140, 60:64] = (0, 255, 0)
    img[60:140, 176:180] = (0, 255, 0)
    stroke = int((np.asarray(img)[:, :, 1] == 255).sum())   # painted pixels
    layers = canvas_io.split_layers(img)
    assert int(layers["add_back"].sum()) > stroke           # interior included
    assert layers["add_back"][100, 120]                      # centre is filled


def test_solid_blob_unchanged_by_fill():
    """A solid brush blob is returned as-is (no spurious growth)."""
    img = np.zeros((120, 120, 3), np.uint8)
    img[30:90, 30:90] = (0, 255, 0)
    layers = canvas_io.split_layers(img)
    assert int(layers["add_back"].sum()) == 60 * 60


def test_load_annotation_resizes_nearest(tmp_path):
    img = _flatten(200, 240)
    p = tmp_path / "a.png"
    Image.fromarray(img).save(p)
    out = canvas_io.load_annotation(str(p), size=(120, 100))  # (W, H)
    assert out.shape == (100, 120, 3)
    # Pure colours survive nearest-neighbour resize.
    uniq = {tuple(c) for c in out.reshape(-1, 3)}
    assert (0, 255, 255) in uniq


def _seed_slide(results_dir, tmp_path, annotation=None):
    store = Store(tmp_path / "s.sqlite")
    from stroi_studio.ingest import ingest
    ingest(store, results_dir=results_dir)
    sid = store.list_slides()[0]["slide_id"]
    if annotation is not None:
        ann_path = tmp_path / "ann.png"
        Image.fromarray(annotation).save(ann_path)
        store.update_slide(sid, annotation_png=str(ann_path))
    return store, sid


def test_compute_roi_writes_outputs(results_dir, tmp_path):
    store, sid = _seed_slide(results_dir, tmp_path, annotation=_flatten(200, 240))
    slide = store.get_slide(sid)
    out_dir = tmp_path / "out"
    summary = compute_roi(slide, out_dir)
    # A cyan loop is drawn, so the ROI is restricted to it (mode "loop").
    assert summary["mode"] == "loop"
    assert summary["roi_px"] > 100
    # The red exclude block inside the loop must be carved out of the ROI.
    roi = np.asarray(Image.open(summary["roi_png"]).convert("L")) > 127
    assert not roi[100:110, 100:120].any()
    assert "exclude=" in summary["detail"]
    assert "tissue=" in summary["detail"]
    for key in ("roi_png", "roi_json", "overlay"):
        assert Path(summary[key]).exists()
    # Sidecar carries the back-mapping geometry.
    sc = json.loads(Path(summary["roi_json"]).read_text())
    assert sc["level0_w"] == 3427 and sc["level0_h"] == 2851
    assert abs(sc["downsample_x"] - 3427 / 240) < 1e-6
    assert sc["downsample_x"] != sc["downsample_y"]
    assert sc["slide_file"] == "H&E_Demo-1.svs"


def test_tissue_is_the_base_no_annotation(results_dir, tmp_path):
    """With nothing drawn, the ROI is exactly the HistoQC tissue mask."""
    store, sid = _seed_slide(results_dir, tmp_path, annotation=None)
    summary = compute_roi(store.get_slide(sid), tmp_path / "out")
    assert summary["mode"] == "tissue_only"
    roi = np.asarray(Image.open(summary["roi_png"]).convert("L")) > 127
    # tissue fixture is rows 40-160, cols 40-200
    assert roi[100, 120] and not roi[10, 10]


def test_green_adds_back_to_tissue(results_dir, tmp_path):
    """Green extends the tissue base into a region QC didn't include."""
    img = np.zeros((200, 240, 3), np.uint8)
    img[170:190, 60:160] = (0, 255, 0)          # below the tissue block (row<160)
    store, sid = _seed_slide(results_dir, tmp_path, annotation=img)
    summary = compute_roi(store.get_slide(sid), tmp_path / "out")
    assert summary["mode"] == "tissue_edited"
    roi = np.asarray(Image.open(summary["roi_png"]).convert("L")) > 127
    assert roi[100, 120]      # original tissue still there
    assert roi[180, 100]      # added-back region now included
    assert "addback=" in summary["detail"]


def test_red_excludes_from_tissue(results_dir, tmp_path):
    """Red removes part of the tissue base."""
    img = np.zeros((200, 240, 3), np.uint8)
    img[50:90, 60:160] = (255, 0, 0)            # inside the tissue block
    store, sid = _seed_slide(results_dir, tmp_path, annotation=img)
    summary = compute_roi(store.get_slide(sid), tmp_path / "out")
    assert summary["mode"] == "tissue_edited"
    roi = np.asarray(Image.open(summary["roi_png"]).convert("L")) > 127
    assert not roi[70, 100]   # excluded region removed
    assert roi[140, 100]      # rest of tissue kept
    assert "exclude=" in summary["detail"]


def test_cyan_loop_restricts_to_tissue_inside(results_dir, tmp_path):
    """A cyan loop limits the ROI to (tissue ∩ loop): only tissue inside it."""
    img = np.zeros((200, 240, 3), np.uint8)      # loop well inside the tissue
    img[60:64, 60:180] = (0, 255, 255)
    img[136:140, 60:180] = (0, 255, 255)
    img[60:140, 60:64] = (0, 255, 255)
    img[60:140, 176:180] = (0, 255, 255)
    store, sid = _seed_slide(results_dir, tmp_path, annotation=img)
    summary = compute_roi(store.get_slide(sid), tmp_path / "out")
    assert summary["mode"] == "loop"
    roi = np.asarray(Image.open(summary["roi_png"]).convert("L")) > 127
    assert roi[100, 120]      # tissue inside the loop kept
    assert not roi[50, 50]    # tissue OUTSIDE the loop dropped (was in base)


def test_compute_route_end_to_end(results_dir, tmp_path, cyan_annotation_png):
    app = create_app(results_dir=str(results_dir),
                     studio_out=str(tmp_path / "out"))
    c = app.test_client()
    sid = app.config["STORE"].list_slides()[0]["slide_id"]
    with open(cyan_annotation_png, "rb") as fh:
        c.post(f"/slide/{sid}/annotation",
               data={"annotation": (fh, "a.png")},
               content_type="multipart/form-data")
    r = c.post(f"/slide/{sid}/compute")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] and body["mode"] == "loop"
    assert "overlay_url" in body
    slide = app.config["STORE"].get_slide(sid)
    assert slide["roi_mode"] == "loop" and slide["roi_png"]
    assert c.get(f"/slide/{sid}/overlay.png").status_code == 200


def test_export_tiles_guard_requires_slide(results_dir, tmp_path):
    """The synthetic slide has no original WSI on disk -> export refused (409)."""
    app = create_app(results_dir=str(results_dir),
                     studio_out=str(tmp_path / "out"))
    c = app.test_client()
    sid = app.config["STORE"].list_slides()[0]["slide_id"]
    r = c.post(f"/slide/{sid}/export-tiles", json={})
    assert r.status_code == 409
    assert "not found on disk" in r.get_json()["error"]
