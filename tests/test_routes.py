"""Phase 1 route + ingest smoke tests against a synthetic HistoQC fixture."""
from __future__ import annotations

from pathlib import Path

from stroi_studio.app import create_app
from stroi_studio.ingest import ingest
from stroi_studio.state import Store


def _make_app(results_dir: Path, tmp_path: Path):
    return create_app(results_dir=str(results_dir),
                      studio_out=str(tmp_path / "out"))


def test_ingest_parses_results_and_dims(results_dir, tmp_path):
    store = Store(tmp_path / "s.sqlite")
    summary = ingest(store, results_dir=results_dir)
    assert summary["n_total"] == 1
    assert summary["has_results_tsv"]
    slides = store.list_slides()
    assert len(slides) == 1
    s = slides[0]
    assert s["slide_file"] == "H&E_Demo-1.svs"
    assert s["thumb_w"] == 240 and s["thumb_h"] == 200
    assert s["level0_w"] == 3427 and s["level0_h"] == 2851
    # Per-axis downsample is computed, not hard-coded, and differs per axis.
    assert abs(s["downsample_x"] - 3427 / 240) < 1e-6
    assert abs(s["downsample_y"] - 2851 / 200) < 1e-6
    assert s["downsample_x"] != s["downsample_y"]


def test_ingest_is_idempotent(results_dir, tmp_path):
    store = Store(tmp_path / "s.sqlite")
    ingest(store, results_dir=results_dir)
    ingest(store, results_dir=results_dir)
    assert len(store.list_slides()) == 1


def test_healthz(results_dir, tmp_path):
    app = _make_app(results_dir, tmp_path)
    r = app.test_client().get("/healthz")
    assert r.status_code == 200 and r.get_json()["status"] == "ok"


def test_dashboard_and_review_render(results_dir, tmp_path):
    app = _make_app(results_dir, tmp_path)
    c = app.test_client()
    assert c.get("/").status_code == 200
    sid = app.config["STORE"].list_slides()[0]["slide_id"]
    assert c.get(f"/slide/{sid}").status_code == 200
    assert c.get(f"/slide/{sid}/thumb.png").status_code == 200
    assert c.get(f"/slide/{sid}/mask_use.png").status_code == 200
    assert c.get(f"/slide/{sid}/annotation.png").status_code == 404  # none yet


def test_set_status(results_dir, tmp_path):
    app = _make_app(results_dir, tmp_path)
    c = app.test_client()
    sid = app.config["STORE"].list_slides()[0]["slide_id"]
    r = c.post(f"/slide/{sid}/status", json={"review_status": "approved"})
    assert r.status_code == 200
    assert app.config["STORE"].get_slide(sid)["review_status"] == "approved"
    bad = c.post(f"/slide/{sid}/status", json={"review_status": "bogus"})
    assert bad.status_code == 400


def test_save_annotation_roundtrip(results_dir, tmp_path, cyan_annotation_png):
    app = _make_app(results_dir, tmp_path)
    c = app.test_client()
    sid = app.config["STORE"].list_slides()[0]["slide_id"]
    with open(cyan_annotation_png, "rb") as fh:
        data = {"annotation": (fh, "annotation.png")}
        r = c.post(f"/slide/{sid}/annotation", data=data,
                   content_type="multipart/form-data")
    assert r.status_code == 200
    slide = app.config["STORE"].get_slide(sid)
    assert slide["annotation_png"] and Path(slide["annotation_png"]).exists()
    assert slide["review_status"] == "in_progress"
    assert c.get(f"/slide/{sid}/annotation.png").status_code == 200


def test_unknown_slide_404(results_dir, tmp_path):
    app = _make_app(results_dir, tmp_path)
    assert app.test_client().get("/slide/9999").status_code == 404


def test_fast_ingest_skips_probe(results_dir, tmp_path):
    """probe_openslide=False leaves readable unprobed (-1) but keeps TSV dims."""
    store = Store(tmp_path / "s.sqlite")
    ingest(store, results_dir=results_dir, slide_dir=str(results_dir),
           probe_openslide=False)
    s = store.list_slides()[0]
    assert s["readable"] == -1                  # not probed
    # downsample still computed from results.tsv dims
    assert s["level0_w"] == 3427
    assert abs(s["downsample_x"] - 3427 / 240) < 1e-6


def test_reopen_restores_annotation_flag(results_dir, tmp_path, cyan_annotation_png):
    """After saving an annotation, the review page advertises it for re-open."""
    app = _make_app(results_dir, tmp_path)
    c = app.test_client()
    sid = app.config["STORE"].list_slides()[0]["slide_id"]
    with open(cyan_annotation_png, "rb") as fh:
        c.post(f"/slide/{sid}/annotation", data={"annotation": (fh, "a.png")},
               content_type="multipart/form-data")
    html = c.get(f"/slide/{sid}").get_data(as_text=True)
    assert 'data-has-annotation="1"' in html      # canvas will reload strokes


def test_start_from_scratch_without_results_dir(tmp_path):
    """With only a slide dir (no HistoQC output yet): app starts, dashboard
    shows the empty-state guide and offers Run HistoQC."""
    slide_dir = tmp_path / "slides"
    slide_dir.mkdir()
    (slide_dir / "demo.svs").write_bytes(b"")     # presence only; never opened
    app = create_app(studio_out=str(tmp_path / "out"), slide_dir=str(slide_dir))
    # results_dir was auto-created under studio_out, not required from the user.
    proj = app.config["STORE"].get_project()
    assert str(tmp_path / "out") in proj["results_dir"]
    assert app.config["STORE"].list_slides() == []
    html = app.test_client().get("/").get_data(as_text=True)
    assert "No slides yet" in html
    assert "Run HistoQC" in html
