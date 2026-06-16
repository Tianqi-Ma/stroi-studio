"""Compute an ROI from a slide's annotation and write the per-slide outputs.

ROI composition (the studio's review semantics — every brush is a region):

    ROI = (cyan_interior ∪ green_region) − red_region

* **cyan** — the region(s) of interest; the loop is filled and **kept whole**
  (the reviewer's circle is trusted, no tissue intersection).
* **green** — additional region(s) to ADD (e.g. tissue HistoQC dropped), unioned
  in as independent ROI area even when drawn outside the cyan loop.
* **red** — region(s) to EXCLUDE, subtracted last so it always wins.
* nothing drawn → fall back to the HistoQC tissue mask (whole-slide tissue).

Green and red are already region-filled by :func:`stroi_studio.canvas_io.split_layers`
(a drawn circle becomes its enclosed area); the cyan loop is filled here with
:func:`stroi.fill_loop`. This composition lives in the studio, not in the
``stroi`` library, whose ``build_roi`` keeps its own (loop ∩ tissue) contract.

Then it writes ``<slide>_roi.png`` (binary mask), ``<slide>_roi.json`` (sidecar
with the back-mapping geometry), ``<slide>_overlay.png`` (four-panel QC figure)
and, when the per-axis downsample is known, ``<slide>_roi.geojson``.

Nothing here touches the original slide or the HistoQC output — all writes go to
the per-slide studio output directory.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import numpy as np
from PIL import Image

from stroi import four_panel

from . import config, canvas_io, mapping

_MIN_ROI_PX = 200


def _load_tissue(mask_path: Optional[str], size: tuple[int, int]
                 ) -> Optional[np.ndarray]:
    """Load the HistoQC mask_use as a boolean array sized ``(W, H)``."""
    if not mask_path or not Path(mask_path).exists():
        return None
    img = Image.open(mask_path).convert("L")
    if img.size != size:
        img = img.resize(size, Image.NEAREST)
    return np.asarray(img) > 127


def compose_roi(layers: Optional[dict[str, np.ndarray]],
                tissue: Optional[np.ndarray], shape: tuple[int, int]
                ) -> tuple[np.ndarray, str, str]:
    """Combine the HistoQC tissue mask with the reviewer's brush edits.

    Returns ``(mask, mode, detail)``.

    The HistoQC tissue is the BASE — the reviewer does not have to re-trace what
    QC already found. Brushes only adjust it:

    * **green** (add back) — union into the base (tissue QC wrongly dropped);
    * **red** (exclude) — subtract from the base (artefacts / unwanted tissue);
    * **cyan** (ROI loop, optional) — *restrict* the result to the loop area(s):
      when one or more cyan loops are drawn, the ROI is kept only where it falls
      inside them (``base ∩ loops``). Without a cyan loop the whole edited base
      is the ROI.

    So: ``edited = (tissue ∪ green) \\ red``; ``ROI = edited ∩ loops`` if any
    loop was drawn, else ``edited``. All brushes are region-filled
    (:func:`canvas_io.fill_region`) so a drawn circle contributes its enclosed
    area, not just the stroke.
    """
    H, W = shape
    base = (np.zeros((H, W), dtype=bool) if tissue is None
            else np.asarray(tissue, dtype=bool).copy())
    parts = [f"tissue={int(base.sum())}"]

    loop_region = None
    if layers is not None:
        # green: add back to the tissue base
        add_back = canvas_io.fill_region(np.asarray(layers["add_back"], bool))
        if add_back.any():
            base |= add_back
            parts.append(f"addback={int(add_back.sum())}")
        # red: exclude from the base
        exclude = canvas_io.fill_region(np.asarray(layers["exclude"], bool))
        if exclude.any():
            base &= ~exclude
            parts.append(f"exclude={int(exclude.sum())}")
        # cyan: restrict to the drawn loop(s), if any
        loop = np.asarray(layers["loop"], dtype=bool)
        if int(loop.sum()) >= 30:
            filled = canvas_io.fill_region(loop)
            if int(filled.sum()) >= _MIN_ROI_PX:
                loop_region = filled
                parts.append(f"loop={int(filled.sum())}")

    if loop_region is not None:
        roi = base & loop_region
        mode = "loop"
    else:
        roi = base
        mode = "tissue_edited" if (layers is not None and len(parts) > 1) \
            else "tissue_only"

    if int(roi.sum()) >= _MIN_ROI_PX:
        return roi, mode, " ".join(parts)
    # Edits/loop wiped everything out (or no tissue at all).
    return np.zeros((H, W), dtype=bool), "empty", " ".join(parts)


def compute_roi(slide: dict[str, Any], out_dir: Path,
                *, loop_color: str = config.LOOP_COLOR) -> dict[str, Any]:
    """Run the ROI pipeline for one slide. Returns a summary dict.

    ``slide`` is a row from the store; ``out_dir`` is its studio output dir.
    """
    thumb_path = slide["thumb_path"]
    thumb = np.asarray(Image.open(thumb_path).convert("RGB"))
    H, W = thumb.shape[:2]

    tissue = _load_tissue(slide.get("mask_use_path"), (W, H))

    layers = None
    ann_path = slide.get("annotation_png")
    if ann_path and Path(ann_path).exists():
        annotation = canvas_io.load_annotation(ann_path, size=(W, H))
        layers = canvas_io.split_layers(annotation, loop_color=loop_color)

    mask, mode, detail = compose_roi(layers, tissue, (H, W))
    roi_px = int(mask.sum())
    tissue_px = int(tissue.sum()) if tissue is not None else 0
    loop_px = int(layers["loop"].sum()) if layers else 0

    out_dir.mkdir(parents=True, exist_ok=True)
    base = slide["slide_file"]

    roi_png = out_dir / (base + config.ROI_PNG_BASENAME)
    Image.fromarray((mask.astype(np.uint8) * 255), mode="L").save(
        roi_png, optimize=True)

    roi_over_tissue = roi_px / tissue_px if tissue_px else 0.0
    sidecar = _build_sidecar(slide, mode, detail, roi_px, tissue_px, loop_px,
                             W, H)
    roi_json = out_dir / (base + config.ROI_JSON_BASENAME)
    roi_json.write_text(json.dumps(sidecar, indent=2))

    overlay_png = out_dir / (base + config.OVERLAY_BASENAME)
    loop_vis = layers["loop"] if layers else None
    tissue_vis = tissue if tissue is not None else np.zeros((H, W), bool)
    fig = four_panel(
        thumb, tissue_vis, mask, loop_mask=loop_vis, title=base,
        subtitle=f"{mode} roi/tis={roi_over_tissue:.2f} {detail}".strip())
    fig.save(overlay_png, optimize=True)

    # Level-0 GeoJSON (primary downstream handoff). Only when we know the
    # per-axis downsample; otherwise skip rather than emit wrong coordinates.
    geojson_path = None
    n_polygons = 0
    ds_x, ds_y = slide.get("downsample_x"), slide.get("downsample_y")
    if roi_px > 0 and ds_x and ds_y:
        gj = mapping.roi_to_geojson(
            mask, downsample_x=float(ds_x), downsample_y=float(ds_y),
            properties={"slide": base, "mode": mode,
                        "object_type": "annotation", "name": "ROI"})
        n_polygons = len(gj["features"])
        geojson_path = out_dir / (base + config.ROI_GEOJSON_BASENAME)
        mapping.write_geojson(str(geojson_path), gj)

    return {
        "mode": mode,
        "method": mode,
        "roi_px": roi_px,
        "tissue_px": tissue_px,
        "loop_px": loop_px,
        "detail": detail,
        "roi_over_tissue": roi_over_tissue,
        "roi_png": str(roi_png),
        "roi_json": str(roi_json),
        "overlay": str(overlay_png),
        "geojson": str(geojson_path) if geojson_path else None,
        "n_polygons": n_polygons,
        "sidecar": sidecar,
    }


def _build_sidecar(slide: dict[str, Any], mode: str, detail: str, roi_px: int,
                   tissue_px: int, loop_px: int, thumb_w: int, thumb_h: int
                   ) -> dict[str, Any]:
    """ROI sidecar carrying the geometry needed to map back to level-0."""
    return {
        "mode": mode,
        "detail": detail,
        "roi_px": roi_px,
        "tissue_px": tissue_px,
        "loop_px": loop_px,
        "slide_file": slide["slide_file"],
        "slide_path": slide.get("slide_path"),
        "thumb_w": thumb_w,
        "thumb_h": thumb_h,
        "level0_w": slide.get("level0_w"),
        "level0_h": slide.get("level0_h"),
        "downsample_x": slide.get("downsample_x"),
        "downsample_y": slide.get("downsample_y"),
        "mpp_x": slide.get("mpp_x"),
        "mpp_y": slide.get("mpp_y"),
        "base_mag": slide.get("base_mag"),
    }
