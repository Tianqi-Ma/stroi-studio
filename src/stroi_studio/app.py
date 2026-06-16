"""Flask application: dashboard, per-slide review, and image serving.

Run on the (often headless) server and reach it over an SSH tunnel:

    stroi-studio --results-dir /path/to/histoqc_output \\
                 --slide-dir  /path/to/slides \\
                 --studio-out /path/to/studio_output --port 5005

Then ``ssh -L 5005:localhost:5005 <server>`` and open http://localhost:5005 .

This module never imports HistoQC and never touches the HistoQC env's openslide
build — HistoQC is reached only via subprocess (:mod:`stroi_studio.histoqc_runner`).
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Optional

from flask import (Flask, abort, jsonify, render_template, request,
                   send_file, url_for)

from . import config
from .export_runner import ExportRunner
from .histoqc_runner import QCRunner
from .ingest import ingest
from .roi_pipeline import compute_roi
from .state import Store


def create_app(*, studio_out: str,
               results_dir: Optional[str] = None,
               slide_dir: Optional[str] = None,
               batch: Optional[str] = None,
               loop_color: str = "cyan") -> Flask:
    """Build the Flask app for one project.

    Two ways to start:

    * **You already have HistoQC output** — pass ``results_dir`` (its
      ``results.tsv`` + per-slide subdirs); slides are ingested immediately.
    * **You only have raw slides** — pass ``slide_dir`` and omit ``results_dir``;
      a results dir is created under ``studio_out`` and stays empty until you run
      HistoQC from the dashboard, which writes its output there and then ingests.
    """
    app = Flask(__name__)

    batch = batch or Path(results_dir).name if results_dir else (
        Path(slide_dir).name if slide_dir else "project")
    out_root = Path(studio_out) / batch
    # Default the HistoQC results dir into the project folder when not supplied,
    # so a from-scratch run (only raw slides) needs no made-up path.
    if not results_dir:
        results_dir = str(out_root / "histoqc_results")
    out_root.mkdir(parents=True, exist_ok=True)
    store = Store(out_root / "studio.sqlite")
    store.set_project(batch=batch, results_dir=results_dir,
                      studio_out=str(studio_out), slide_dir=slide_dir,
                      loop_color=loop_color)

    app.config["STORE"] = store
    app.config["OUT_ROOT"] = out_root
    app.config["PROJECT"] = {"batch": batch, "results_dir": results_dir,
                             "slide_dir": slide_dir, "loop_color": loop_color}
    app.config["QC_RUNNER"] = QCRunner(store)
    app.config["EXPORT_RUNNER"] = ExportRunner(store, out_root)

    # First-run ingest so the dashboard has slides immediately (when results
    # already exist; a fresh project may have none until QC has run). Skip the
    # per-slide openslide probe here so the first page paints fast on a large
    # cohort — readability is resolved lazily at tile-export time.
    if not store.list_slides() and Path(results_dir).exists():
        ingest(store, results_dir=results_dir, slide_dir=slide_dir,
               probe_openslide=False)

    _register_routes(app)
    return app


def _slide_out_dir(app: Flask, slide_file: str) -> Path:
    d = app.config["OUT_ROOT"] / slide_file
    d.mkdir(parents=True, exist_ok=True)
    return d


def _lazy_probe(store: Store, slide: dict) -> dict:
    """Probe a slide's original WSI with openslide and persist the result.

    Used when first-run ingest skipped the (slow) probe. Updates readability,
    open error, and — on success — the exact level-0 dims and per-axis
    downsample. Returns the refreshed slide row.
    """
    from .ingest import _probe_openslide
    ok, err, dims = _probe_openslide(Path(slide["slide_path"]))
    fields: dict = {"readable": 1 if ok else 0, "open_error": err}
    if dims is not None:
        l0w, l0h = float(dims[0]), float(dims[1])
        fields["level0_w"], fields["level0_h"] = int(l0w), int(l0h)
        if slide.get("thumb_w") and slide.get("thumb_h"):
            fields["downsample_x"] = l0w / slide["thumb_w"]
            fields["downsample_y"] = l0h / slide["thumb_h"]
    store.update_slide(slide["slide_id"], **fields)
    return store.get_slide(slide["slide_id"])


def _serve_png_path(path: Optional[str]):
    if not path or not Path(path).exists():
        abort(404)
    return send_file(path, mimetype="image/png", conditional=True)


def _register_routes(app: Flask) -> None:
    store: Store = app.config["STORE"]

    @app.get("/healthz")
    def healthz():
        return jsonify(status="ok", version=_version())

    @app.get("/")
    def index():
        slides = store.list_slides()
        project = store.get_project() or {}
        qc = store.latest_qc_run()
        return render_template("index.html", slides=slides, project=project,
                               qc=qc, states=config.REVIEW_STATES)

    @app.get("/slides")
    def slides_json():
        return jsonify(slides=store.list_slides())

    @app.post("/ingest")
    def reingest():
        project = store.get_project() or {}
        summary = ingest(store, results_dir=project["results_dir"],
                         slide_dir=project.get("slide_dir"))
        return jsonify(summary=summary, slides=store.list_slides())

    # --- HistoQC orchestration -------------------------------------------

    @app.post("/qc/run")
    def qc_run():
        runner: QCRunner = app.config["QC_RUNNER"]
        project = store.get_project() or {}
        data = request.get_json(silent=True) or {}
        slide_dir = data.get("slide_dir") or project.get("slide_dir")
        if not slide_dir or not Path(slide_dir).exists():
            return jsonify(error="no slide_dir set; provide a folder of slides"), 400
        pattern = data.get("pattern", "*.svs")
        slides = sorted(str(p) for p in Path(slide_dir).glob(pattern)
                        if p.is_file())
        if not slides:
            return jsonify(error=f"no slides matched {pattern} in {slide_dir}"), 400
        # HistoQC writes into the project's results dir (its own output tree).
        out_dir = project["results_dir"]
        res = runner.start(
            slides=slides, out_dir=out_dir,
            config_name=data.get("config", config.HISTOQC_CONFIG),
            nprocesses=int(data.get("nprocesses", 4)))
        code = 409 if "error" in res and "in progress" in res["error"] else 200
        return jsonify(**res), code

    @app.get("/qc/status")
    def qc_status():
        runner: QCRunner = app.config["QC_RUNNER"]
        run = store.latest_qc_run()
        return jsonify(running=runner.is_running(), run=run)

    @app.get("/qc/stream")
    def qc_stream():
        """Server-sent events: emit qc_run status until the run finishes."""
        runner: QCRunner = app.config["QC_RUNNER"]

        def gen():
            import json as _json
            last = None
            # Stream while running, then one final event.
            while True:
                run = store.latest_qc_run() or {}
                payload = {"running": runner.is_running(),
                           "status": run.get("status"),
                           "n_done": run.get("n_done"),
                           "n_slides": run.get("n_slides"),
                           "last_line": run.get("last_line")}
                blob = _json.dumps(payload)
                if blob != last:
                    yield f"data: {blob}\n\n"
                    last = blob
                if not runner.is_running() and run.get("status") in (
                        "done", "failed", "cancelled"):
                    break
                time.sleep(1.0)

        return app.response_class(gen(), mimetype="text/event-stream")

    @app.post("/qc/cancel")
    def qc_cancel():
        runner: QCRunner = app.config["QC_RUNNER"]
        res = runner.cancel()
        code = 409 if "error" in res else 200
        return jsonify(**res), code

    @app.post("/qc/ingest")
    def qc_ingest():
        """Ingest after a QC run completes (re-parse the results dir)."""
        project = store.get_project() or {}
        summary = ingest(store, results_dir=project["results_dir"],
                         slide_dir=project.get("slide_dir"))
        return jsonify(summary=summary, n_slides=len(store.list_slides()))

    @app.get("/slide/<int:slide_id>")
    def slide_page(slide_id: int):
        slide = store.get_slide(slide_id)
        if not slide:
            abort(404)
        # Neighbour ids for prev/next navigation through the review queue.
        ids = [s["slide_id"] for s in store.list_slides()]
        pos = ids.index(slide_id) if slide_id in ids else 0
        nav = {
            "pos": pos + 1, "total": len(ids),
            "prev": ids[pos - 1] if pos > 0 else None,
            "next": ids[pos + 1] if pos < len(ids) - 1 else None,
        }
        return render_template("review.html", slide=slide,
                               states=config.REVIEW_STATES, nav=nav)

    @app.get("/slide/<int:slide_id>/thumb.png")
    def slide_thumb(slide_id: int):
        slide = store.get_slide(slide_id) or abort(404)
        return _serve_png_path(slide["thumb_path"])

    @app.get("/slide/<int:slide_id>/mask_use.png")
    def slide_mask(slide_id: int):
        slide = store.get_slide(slide_id) or abort(404)
        return _serve_png_path(slide["mask_use_path"])

    @app.get("/slide/<int:slide_id>/annotation.png")
    def slide_annotation(slide_id: int):
        slide = store.get_slide(slide_id) or abort(404)
        return _serve_png_path(slide["annotation_png"])

    @app.get("/slide/<int:slide_id>/overlay.png")
    def slide_overlay(slide_id: int):
        slide = store.get_slide(slide_id) or abort(404)
        return _serve_png_path(slide["overlay"])

    @app.get("/slide/<int:slide_id>/roi.png")
    def slide_roi_png(slide_id: int):
        """The binary ROI mask (thumbnail resolution) — used by the preview."""
        slide = store.get_slide(slide_id) or abort(404)
        return _serve_png_path(slide.get("roi_png"))

    @app.get("/slide/<int:slide_id>/roi.geojson")
    def slide_geojson(slide_id: int):
        slide = store.get_slide(slide_id) or abort(404)
        if not slide["geojson"] or not Path(slide["geojson"]).exists():
            abort(404)
        return send_file(slide["geojson"], mimetype="application/geo+json",
                         conditional=True)

    @app.get("/slide/<int:slide_id>/roi.json")
    def slide_roi_json(slide_id: int):
        slide = store.get_slide(slide_id) or abort(404)
        if not slide["roi_json"] or not Path(slide["roi_json"]).exists():
            abort(404)
        return send_file(slide["roi_json"], mimetype="application/json",
                         conditional=True)

    @app.post("/slide/<int:slide_id>/status")
    def set_status(slide_id: int):
        slide = store.get_slide(slide_id) or abort(404)
        data = request.get_json(silent=True) or request.form
        status = data.get("review_status")
        if status not in config.REVIEW_STATES:
            return jsonify(error="invalid review_status"), 400
        store.update_slide(slide_id, review_status=status,
                           reviewer_note=data.get("reviewer_note"))
        return jsonify(ok=True, review_status=status)

    @app.post("/slide/<int:slide_id>/annotation")
    def save_annotation(slide_id: int):
        """Persist the flattened canvas PNG (autosave). Phase 2 consumes it."""
        slide = store.get_slide(slide_id) or abort(404)
        png_bytes = _annotation_png_from_request(request)
        if png_bytes is None:
            return jsonify(error="no annotation image"), 400
        out = _slide_out_dir(app, slide["slide_file"]) / (
            slide["slide_file"] + config.ANNOTATION_BASENAME)
        out.write_bytes(png_bytes)
        store.update_slide(slide_id, annotation_png=str(out),
                           review_status="in_progress")
        return jsonify(ok=True, annotation_url=url_for(
            "slide_annotation", slide_id=slide_id))

    @app.post("/slide/<int:slide_id>/compute")
    def compute(slide_id: int):
        """Build the ROI from the saved annotation and write the outputs."""
        slide = store.get_slide(slide_id) or abort(404)
        project = store.get_project() or {}
        out_dir = _slide_out_dir(app, slide["slide_file"])
        summary = compute_roi(slide, out_dir,
                              loop_color=project.get("loop_color", "cyan"))
        store.update_slide(slide_id, roi_mode=summary["mode"],
                           roi_px=summary["roi_px"],
                           tissue_px=summary["tissue_px"],
                           roi_png=summary["roi_png"],
                           roi_json=summary["roi_json"],
                           geojson=summary.get("geojson"),
                           overlay=summary["overlay"])
        summary["overlay_url"] = url_for("slide_overlay", slide_id=slide_id)
        summary["roi_url"] = url_for("slide_roi_png", slide_id=slide_id)
        if summary.get("geojson"):
            summary["geojson_url"] = url_for("slide_geojson", slide_id=slide_id)
        summary.pop("sidecar", None)   # keep the response small
        return jsonify(ok=True, **summary)

    @app.post("/slide/<int:slide_id>/export-tiles")
    def export_tiles_route(slide_id: int):
        """Cut high-res tiles inside the ROI from the original slide (opt-in)."""
        slide = store.get_slide(slide_id) or abort(404)
        if not slide.get("slide_path"):
            return jsonify(error="original slide not found on disk; set "
                           "--slide-dir"), 409
        # Resolve readability lazily (first-run ingest skips the probe).
        if slide.get("readable") not in (0, 1):
            slide = _lazy_probe(store, slide)
        if slide.get("readable") != 1:
            return jsonify(error="original slide not openable; cannot export "
                           "high-res tiles", open_error=slide.get("open_error")), 409
        if not slide.get("roi_png") or not Path(slide["roi_png"]).exists():
            return jsonify(error="compute the ROI first"), 409

        data = request.get_json(silent=True) or {}
        tile_size = int(data.get("tile_size", 256))
        level = int(data.get("level", 0))
        min_frac = float(data.get("min_roi_frac", 0.5))
        limit = data.get("limit")
        limit = int(limit) if limit not in (None, "") else None

        import numpy as np
        from PIL import Image
        from .mapping import export_tiles

        roi_mask = np.asarray(Image.open(slide["roi_png"]).convert("L")) > 127
        out_dir = _slide_out_dir(app, slide["slide_file"]) / config.TILES_DIRNAME
        summary = export_tiles(
            roi_mask, slide["slide_path"], str(out_dir),
            tile_size=tile_size, level=level, min_roi_frac=min_frac,
            downsample_x=slide.get("downsample_x"),
            downsample_y=slide.get("downsample_y"), limit=limit)
        return jsonify(ok=True, **summary)

    @app.post("/slide/<int:slide_id>/export-mask")
    def export_mask_route(slide_id: int):
        """Materialise a level-0 binary ROI mask PNG (opt-in; large)."""
        slide = store.get_slide(slide_id) or abort(404)
        if not slide.get("roi_png") or not Path(slide["roi_png"]).exists():
            return jsonify(error="compute the ROI first"), 409
        l0w, l0h = slide.get("level0_w"), slide.get("level0_h")
        if not l0w or not l0h:
            return jsonify(error="level-0 dimensions unknown for this slide"), 409

        import numpy as np
        from PIL import Image
        from .mapping import export_full_mask

        roi_mask = np.asarray(Image.open(slide["roi_png"]).convert("L")) > 127
        out_path = _slide_out_dir(app, slide["slide_file"]) / (
            slide["slide_file"] + "_roi_level0.png")
        summary = export_full_mask(roi_mask, str(out_path),
                                   level0_w=int(l0w), level0_h=int(l0h))
        return jsonify(ok=True, **summary)

    # --- batch export (dashboard) ----------------------------------------

    @app.post("/export/run")
    def export_run():
        """Batch-export deliverables for a set of slides (background thread).

        Body: ``{slide_ids?: [int], products: ["geojson","tiles","mask"],
        tile_size?: int}``. If ``slide_ids`` is omitted, all *approved* slides
        are used. A chosen slide that has no computed ROI yet is computed on the
        fly during export with the default (its ROI is the HistoQC tissue), so a
        reviewer who simply approves an unedited slide need not click Compute.
        """
        runner: ExportRunner = app.config["EXPORT_RUNNER"]
        data = request.get_json(silent=True) or {}
        # Missing key -> default to geojson; explicit empty list -> error.
        products = data["products"] if "products" in data else ["geojson"]
        valid = [p for p in products if p in ("geojson", "tiles", "mask")]
        if not valid:
            return jsonify(error="no products selected"), 400
        products = valid
        ids = data.get("slide_ids")
        all_slides = store.list_slides()
        if ids:
            idset = set(int(i) for i in ids)
            chosen = [s for s in all_slides if s["slide_id"] in idset]
        else:
            chosen = [s for s in all_slides if s["review_status"] == "approved"]
        # A slide is exportable if it already has an ROI OR has a tissue/thumb to
        # compute one from on the fly (so approving an unedited slide suffices).
        chosen = [s for s in chosen
                  if s.get("roi_png") or s.get("thumb_path")]
        if not chosen:
            return jsonify(error="no exportable slides "
                           "(approve some slides first)"), 400
        res = runner.start(slides=chosen, products=products,
                           tile_size=int(data.get("tile_size", 256)))
        code = 409 if "error" in res and "in progress" in res["error"] else 200
        return jsonify(**res), code

    @app.get("/export/status")
    def export_status():
        runner: ExportRunner = app.config["EXPORT_RUNNER"]
        return jsonify(running=runner.is_running(),
                       run=store.latest_export_run())

    @app.post("/export/cancel")
    def export_cancel():
        runner: ExportRunner = app.config["EXPORT_RUNNER"]
        res = runner.cancel()
        return jsonify(**res), (409 if "error" in res else 200)


def _annotation_png_from_request(req) -> Optional[bytes]:
    """Accept either a multipart file field 'annotation' or a JSON data URL."""
    if "annotation" in req.files:
        return req.files["annotation"].read()
    data = req.get_json(silent=True) or {}
    data_url = data.get("data_url")
    if data_url and "," in data_url:
        import base64
        return base64.b64decode(data_url.split(",", 1)[1])
    return None


def _version() -> str:
    from . import __version__
    return __version__


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="stroi-studio",
        description="Local web GUI for the stROI ROI workflow.",
        epilog="From scratch (only raw slides, no QC yet):\n"
               "  stroi-studio --slide-dir DIR --studio-out OUT\n"
               "  then click 'Run HistoQC' on the dashboard.\n"
               "With existing HistoQC output:\n"
               "  stroi-studio --results-dir QCDIR --slide-dir DIR --studio-out OUT",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--slide-dir", default=None,
                   help="dir of original WSI files. Needed to run HistoQC from "
                        "scratch and for ROI back-mapping. Give this alone to "
                        "start without any QC results yet.")
    p.add_argument("--results-dir", default=None,
                   help="existing HistoQC results dir (results.tsv + per-slide "
                        "subdirs). Omit to start from scratch — a results dir is "
                        "created under --studio-out and filled when you run QC.")
    p.add_argument("--studio-out", required=True,
                   help="where studio writes its state + per-slide outputs")
    p.add_argument("--batch", default=None, help="project name (default: "
                   "slide/results dir name)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5005)
    args = p.parse_args(argv)

    if not args.results_dir and not args.slide_dir:
        p.error("provide --slide-dir (start from scratch and run HistoQC) "
                "and/or --results-dir (use existing HistoQC output)")

    app = create_app(results_dir=args.results_dir, studio_out=args.studio_out,
                     slide_dir=args.slide_dir, batch=args.batch)
    # threaded so the (later) SSE progress stream doesn't block other requests;
    # no reloader so a running QC subprocess is never orphaned by a reload.
    app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
