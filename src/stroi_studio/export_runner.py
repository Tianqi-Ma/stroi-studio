"""Batch export of the deliverables for a set of reviewed slides.

Unlike HistoQC (a foreign-env subprocess), exporting runs in *our* process — it
is just our own openslide + numpy — so we run it on a background thread and
track progress in the ``export_run`` table. The dashboard starts a run over the
selected slides (by default the ``approved`` ones) for any combination of three
products:

* ``geojson`` — level-0 ROI polygons (fast);
* ``tiles``   — high-resolution ROI tiles via openslide ``read_region`` (slow);
* ``mask``    — a level-0 binary ROI mask PNG (large).

The ROI itself must already have been computed for the slide (its ``roi_png``
exists). The original slide is only read, never modified.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

from . import config
from .mapping import export_full_mask, export_tiles, roi_to_geojson, write_geojson
from .state import Store


class ExportRunner:
    """Owns at most one running batch export for a project."""

    def __init__(self, store: Store, out_root: Path):
        self.store = store
        self.out_root = Path(out_root)
        self._thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._run_id: Optional[int] = None
        self._lock = threading.Lock()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, *, slides: list[dict], products: list[str],
              tile_size: int = 256, min_roi_frac: float = 0.5) -> dict:
        """Begin exporting ``products`` for ``slides`` on a background thread."""
        with self._lock:
            if self.is_running():
                return {"error": "an export is already in progress",
                        "run_id": self._run_id}
            products = [p for p in products if p in ("geojson", "tiles", "mask")]
            if not products:
                return {"error": "no products selected"}
            if not slides:
                return {"error": "no slides selected"}
            run_id = self.store.create_export_run(
                products=",".join(products), n_slides=len(slides),
                out_dir=str(self.out_root))
            self._run_id = run_id
            self._cancel.clear()
            self._thread = threading.Thread(
                target=self._run, args=(run_id, slides, products, tile_size,
                                        min_roi_frac), daemon=True)
            self._thread.start()
            return {"run_id": run_id, "n_slides": len(slides),
                    "products": products}

    def cancel(self) -> dict:
        if not self.is_running():
            return {"error": "no export is in progress"}
        self._cancel.set()
        return {"ok": True, "run_id": self._run_id}

    # --- worker -----------------------------------------------------------

    def _run(self, run_id: int, slides: list[dict], products: list[str],
             tile_size: int, min_roi_frac: float) -> None:
        n_done = 0
        try:
            for slide in slides:
                if self._cancel.is_set():
                    self.store.update_export_run(
                        run_id, status="cancelled", ended_at=time.time())
                    return
                name = slide["slide_file"]
                self.store.update_export_run(
                    run_id, last_line=f"exporting {name}…", n_done=n_done)
                try:
                    self._export_one(slide, products, tile_size, min_roi_frac)
                except Exception as e:  # noqa: BLE001 - report, keep going
                    self.store.update_export_run(
                        run_id, last_line=f"{name}: {type(e).__name__}: {e}"[:200])
                n_done += 1
                self.store.update_export_run(run_id, n_done=n_done)
            self.store.update_export_run(
                run_id, status="done", n_done=n_done,
                last_line=f"exported {n_done} slide(s)", ended_at=time.time())
        except Exception as e:  # noqa: BLE001
            self.store.update_export_run(
                run_id, status="failed",
                last_line=f"{type(e).__name__}: {e}"[:200], ended_at=time.time())

    def _export_one(self, slide: dict, products: list[str], tile_size: int,
                    min_roi_frac: float) -> None:
        out_dir = self.out_root / slide["slide_file"]
        out_dir.mkdir(parents=True, exist_ok=True)
        base = slide["slide_file"]

        # Compute the ROI on the fly if the reviewer never clicked Compute — for
        # an unedited slide this yields exactly the HistoQC tissue. Persist it so
        # the slide is fully exportable and the result shows up on its page.
        roi_png = slide.get("roi_png")
        if not roi_png or not Path(roi_png).exists():
            from .roi_pipeline import compute_roi
            summary = compute_roi(slide, out_dir)
            roi_png = summary["roi_png"]
            self.store.update_slide(
                slide["slide_id"], roi_mode=summary["mode"],
                roi_px=summary["roi_px"], tissue_px=summary["tissue_px"],
                roi_png=summary["roi_png"], roi_json=summary["roi_json"],
                geojson=summary.get("geojson"), overlay=summary["overlay"])

        roi_mask = np.asarray(Image.open(roi_png).convert("L")) > 127
        ds_x, ds_y = slide.get("downsample_x"), slide.get("downsample_y")

        if "geojson" in products and ds_x and ds_y:
            gj = roi_to_geojson(roi_mask, downsample_x=float(ds_x),
                                downsample_y=float(ds_y),
                                properties={"slide": base, "name": "ROI",
                                            "object_type": "annotation"})
            write_geojson(str(out_dir / (base + config.ROI_GEOJSON_BASENAME)), gj)

        if "mask" in products:
            l0w, l0h = slide.get("level0_w"), slide.get("level0_h")
            if l0w and l0h:
                export_full_mask(roi_mask,
                                 str(out_dir / (base + "_roi_level0.png")),
                                 level0_w=int(l0w), level0_h=int(l0h))

        if "tiles" in products and slide.get("slide_path"):
            # Resolve readability lazily (first-run ingest skips the probe), and
            # raise if the slide can't be opened so the per-slide error surfaces.
            if slide.get("readable") != 1:
                ok, err, _ = self._probe(slide["slide_path"])
                if not ok:
                    raise RuntimeError(f"slide not openable: {err}")
            export_tiles(roi_mask, slide["slide_path"],
                         str(out_dir / config.TILES_DIRNAME),
                         tile_size=tile_size, min_roi_frac=min_roi_frac,
                         downsample_x=float(ds_x) if ds_x else None,
                         downsample_y=float(ds_y) if ds_y else None)

    @staticmethod
    def _probe(slide_path: str):
        """Open with our openslide build; return ``(ok, error, dims)``."""
        try:
            import openslide
            s = openslide.OpenSlide(slide_path)
            try:
                return True, None, s.dimensions
            finally:
                s.close()
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"[:200], None
