"""Turn a flattened RGB annotation PNG into cyan/green/red masks.

The browser canvas flattens three brush layers onto one RGB image the same size
as the thumbnail (black background):

  * cyan  (0,255,255) — a closed LOOP marking the region of interest
  * green (0,255,0)   — ADD BACK: tissue HistoQC wrongly removed
  * red   (255,0,0)   — EXCLUDE: regions to drop from the ROI

Colours are hue-disjoint, so we recover each layer with :func:`stroi.detect_color_loop`
(used purely as a colour→mask extractor). All three brushes are *region* tools:
whether the user draws a thin closed loop or scribbles a solid blob, we want the
ENCLOSED AREA, not just the painted pixels. So each colour mask is filled the
same way cyan is — ``paint ∪ fill_loop(paint)`` — which yields the loop interior
for a thin outline and the blob itself for a solid fill. The three colours are
mutually exclusive per pixel because the client paints opaque single-colour
layers and flattens with a fixed precedence, so anti-aliased edges never produce
an ambiguous mix.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
from PIL import Image
from scipy import ndimage

from stroi import detect_color_loop

from . import config

# Small morphological closing (in pixels) to bridge tiny hand-drawn gaps before
# filling. Brush strokes are thick, so real gaps are tiny; a large value would
# let an open stroke spuriously fill, so keep it modest.
_GAP_BRIDGE = 4


def fill_region(paint_mask: np.ndarray) -> np.ndarray:
    """Return the *enclosed area* of a brush mark, not just the painted pixels.

    Every brush is a region tool: drawing a circle should select what is inside
    it, exactly like the cyan ROI loop. Behaviour:

    * a closed loop (thin outline) -> stroke plus its filled interior;
    * a solid blob                 -> the blob unchanged (no growth);
    * a stroke with a tiny gap      -> bridged (closing) then filled;
    * an open stroke / wide gap     -> just the painted pixels (nothing to fill).

    Implementation: close by a few pixels to bridge small gaps, fill holes, and
    keep only the *enclosed* hole (``filled & ~closed``) so the closing's outer
    dilation halo is discarded and a solid blob never grows.
    """
    paint_mask = np.asarray(paint_mask, dtype=bool)
    if int(paint_mask.sum()) < 30:
        return paint_mask
    structure = ndimage.generate_binary_structure(2, 2)   # 8-connectivity
    closed = ndimage.binary_closing(paint_mask, structure=structure,
                                    iterations=_GAP_BRIDGE)
    filled = ndimage.binary_fill_holes(closed)
    interior = filled & ~closed                            # enclosed hole only
    return paint_mask | interior


def load_annotation(path: str, *, size: Optional[tuple[int, int]] = None
                    ) -> np.ndarray:
    """Load an annotation PNG as ``(H, W, 3)`` uint8 RGB.

    ``size`` is ``(width, height)``; if given the image is nearest-neighbour
    resized to it (preserving the pure annotation colours) so a canvas exported
    at a slightly different size still aligns to the thumbnail.
    """
    img = Image.open(path).convert("RGB")
    if size is not None and img.size != size:
        img = img.resize(size, Image.NEAREST)
    return np.asarray(img)


def split_layers(annotation_rgb: np.ndarray,
                 *, loop_color: str = config.LOOP_COLOR,
                 add_back_color=config.ADD_BACK_COLOR,
                 exclude_color=config.EXCLUDE_COLOR
                 ) -> dict[str, np.ndarray]:
    """Return boolean masks ``{"loop", "add_back", "exclude"}`` by colour.

    ``loop`` is the raw cyan *stroke* — it is filled later by ``stroi.build_roi``
    (which needs the unfilled loop to run its own enclosure logic). ``add_back``
    and ``exclude`` are returned already *region-filled* (see :func:`fill_region`)
    so a drawn circle adds/removes its enclosed area, not just the painted line.
    """
    return {
        "loop": detect_color_loop(annotation_rgb, loop_color),
        "add_back": fill_region(detect_color_loop(annotation_rgb, add_back_color)),
        "exclude": fill_region(detect_color_loop(annotation_rgb, exclude_color)),
    }


def has_any_annotation(layers: dict[str, np.ndarray]) -> bool:
    return any(int(m.sum()) > 0 for m in layers.values())
