"""Map a thumbnail-resolution ROI mask back to the original slide.

The studio's deliverables for downstream high-resolution training are produced
here, without ever modifying the original slide:

  * **GeoJSON polygons in level-0 pixel coordinates** (the primary handoff for
    QuPath / openslide ``read_region``). Excluded islands inside the ROI become
    polygon holes.
  * The low-resolution ROI mask + sidecar (written by
    :mod:`stroi_studio.roi_pipeline`) carries the exact per-axis downsample so a
    consumer can also upscale on the fly.

We trace the mask's boundary along pixel *edges* (a "crack-following" tracer),
which is exact for a binary mask — no smoothing, no extra dependency. Each
4-connected component yields one exterior ring plus a hole ring per interior
gap; vertices are in pixel-corner coordinates and scaled by the per-axis
downsample to land in level-0 space.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import numpy as np
from scipy import ndimage

# Directed boundary edges keep the filled region on a consistent side; we orient
# each closed ring afterwards by signed area (exterior CCW, holes CW).
# Direction codes for choosing turns at shared corners.
_DIRS = {(1, 0): 0, (0, 1): 1, (-1, 0): 2, (0, -1): 3}  # (dx, dy) -> index


def _component_edges(comp: np.ndarray) -> dict[tuple[int, int],
                                               list[tuple[int, int]]]:
    """Directed pixel-edge segments for one binary component.

    Returns ``{start_corner: [end_corner, ...]}`` in ``(x, y)`` corner coords
    (x = column, y = row). Region is kept on the left of each directed edge.
    """
    padded = np.pad(comp, 1)
    succ: dict[tuple[int, int], list[tuple[int, int]]] = {}

    def add(p0: tuple[int, int], p1: tuple[int, int]) -> None:
        succ.setdefault(p0, []).append(p1)

    ys, xs = np.where(comp)
    for r, c in zip(ys.tolist(), xs.tolist()):
        pr, pc = r + 1, c + 1
        if not padded[pr - 1, pc]:                 # up neighbour empty
            add((c + 1, r), (c, r))
        if not padded[pr + 1, pc]:                 # down neighbour empty
            add((c, r + 1), (c + 1, r + 1))
        if not padded[pr, pc - 1]:                 # left neighbour empty
            add((c, r), (c, r + 1))
        if not padded[pr, pc + 1]:                 # right neighbour empty
            add((c + 1, r + 1), (c + 1, r))
    return succ


def _walk_rings(succ: dict[tuple[int, int], list[tuple[int, int]]]
                ) -> list[list[tuple[int, int]]]:
    """Chain directed edges into closed rings, preferring left turns at forks."""
    rings: list[list[tuple[int, int]]] = []
    for start in list(succ.keys()):
        while succ.get(start):
            ring = [start]
            cur = start
            prev_dir: Optional[int] = None
            while True:
                outs = succ.get(cur)
                if not outs:
                    break
                nxt = _pick_next(cur, outs, prev_dir)
                outs.remove(nxt)
                if not outs:
                    succ.pop(cur, None)
                dx, dy = nxt[0] - cur[0], nxt[1] - cur[1]
                prev_dir = _DIRS[(dx, dy)]
                cur = nxt
                if cur == start:
                    break
                ring.append(cur)
            if len(ring) >= 4:
                rings.append(ring)
    return rings


def _pick_next(cur: tuple[int, int], outs: list[tuple[int, int]],
               prev_dir: Optional[int]) -> tuple[int, int]:
    """At a fork, take the most counter-clockwise turn (hug the interior)."""
    if len(outs) == 1 or prev_dir is None:
        return outs[0]
    best, best_score = outs[0], -1
    for cand in outs:
        dx, dy = cand[0] - cur[0], cand[1] - cur[1]
        d = _DIRS[(dx, dy)]
        score = (d - prev_dir) % 4          # 1=left, 2=straight-back, 3=right
        if score > best_score:
            best, best_score = cand, score
    return best


def _drop_collinear(ring: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Remove vertices that lie on a straight run between their neighbours.

    The crack-following tracer emits a vertex at every unit pixel-edge step, so
    a long straight edge becomes hundreds of collinear points. Merging them
    shrinks the GeoJSON dramatically while staying pixel-exact (we only drop a
    point when the cross product with its neighbours is zero).
    """
    n = len(ring)
    if n < 3:
        return ring
    out: list[tuple[int, int]] = []
    for i in range(n):
        x0, y0 = ring[(i - 1) % n]
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        cross = (x1 - x0) * (y2 - y0) - (y1 - y0) * (x2 - x0)
        if cross != 0:
            out.append(ring[i])
    return out or ring


def _signed_area(ring: list[tuple[int, int]]) -> float:
    area = 0.0
    n = len(ring)
    for i in range(n):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % n]
        area += x0 * y1 - x1 * y0
    return area / 2.0


def mask_to_rings(mask: np.ndarray) -> list[dict[str, Any]]:
    """Return one entry per 4-connected component: exterior ring + holes.

    Each entry is ``{"exterior": [(x, y), ...], "holes": [[(x, y), ...], ...]}``
    in pixel-corner coordinates. Exterior rings are oriented CCW (positive
    signed area), holes CW.
    """
    mask = np.asarray(mask, dtype=bool)
    labels, n = ndimage.label(mask)            # 4-connectivity by default
    out: list[dict[str, Any]] = []
    for lab in range(1, n + 1):
        comp = labels == lab
        rings = _walk_rings(_component_edges(comp))
        if not rings:
            continue
        # The exterior boundary of a single component encloses all its holes,
        # so it has the largest absolute area.
        rings.sort(key=lambda r: abs(_signed_area(r)), reverse=True)
        exterior = _drop_collinear(rings[0])
        holes = [_drop_collinear(h) for h in rings[1:]]
        if _signed_area(exterior) < 0:
            exterior = exterior[::-1]
        holes = [h if _signed_area(h) < 0 else h[::-1] for h in holes]
        out.append({"exterior": exterior, "holes": holes})
    return out


def _scale(ring: list[tuple[int, int]], sx: float, sy: float
           ) -> list[list[float]]:
    return [[x * sx, y * sy] for (x, y) in ring]


def roi_to_geojson(mask: np.ndarray, *, downsample_x: float, downsample_y: float,
                   properties: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Build a GeoJSON FeatureCollection of the ROI in level-0 pixel coords.

    Each connected ROI component becomes one ``Polygon`` feature whose first
    ring is the exterior and whose remaining rings are holes (excluded islands).
    Coordinates are ``[x, y]`` in level-0 pixels: ``corner * downsample`` per
    axis (the downsample is read per-slide/per-axis, never assumed isotropic).
    """
    features = []
    for comp in mask_to_rings(mask):
        rings = [_scale(comp["exterior"], downsample_x, downsample_y)]
        rings += [_scale(h, downsample_x, downsample_y) for h in comp["holes"]]
        # GeoJSON rings must be explicitly closed (first point repeated).
        for r in rings:
            if r and r[0] != r[-1]:
                r.append(list(r[0]))
        features.append({
            "type": "Feature",
            "properties": dict(properties or {}),
            "geometry": {"type": "Polygon", "coordinates": rings},
        })
    return {"type": "FeatureCollection", "features": features}


def write_geojson(path: str, geojson: dict[str, Any]) -> None:
    with open(path, "w") as fh:
        json.dump(geojson, fh)


def export_tiles(roi_mask: np.ndarray, slide_path: str, out_dir: str,
                 *, tile_size: int = 256, level: int = 0,
                 min_roi_frac: float = 0.5,
                 downsample_x: Optional[float] = None,
                 downsample_y: Optional[float] = None,
                 limit: Optional[int] = None) -> dict[str, Any]:
    """Cut high-resolution tiles from the original slide inside the ROI.

    The ROI mask is at thumbnail resolution; ``downsample_x/y`` map it to
    level-0. We grid the requested ``level`` in tile_size steps, keep a tile when
    at least ``min_roi_frac`` of the corresponding thumbnail patch is in the ROI,
    read it with openslide ``read_region``, and write ``tile_x{X}_y{Y}.png`` plus
    a ``tiles_index.tsv`` (level-0 x/y, level, size). Streams tile-by-tile so the
    full-resolution image is never materialised in memory.

    Returns a summary dict. The original slide is only read, never written.
    """
    import csv
    from pathlib import Path

    import openslide

    roi_mask = np.asarray(roi_mask, dtype=bool)
    th, tw = roi_mask.shape
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    slide = openslide.OpenSlide(slide_path)
    try:
        if downsample_x is None or downsample_y is None:
            l0w, l0h = slide.dimensions
            downsample_x = l0w / tw
            downsample_y = l0h / th
        # Level-0 pixels per tile, and the matching thumbnail-space step.
        level_ds = slide.level_downsamples[level]
        tile_l0 = tile_size * level_ds                  # level-0 px spanned
        step_x_thumb = tile_l0 / downsample_x           # thumb px per tile
        step_y_thumb = tile_l0 / downsample_y

        ys = _frange(0, th, step_y_thumb)
        xs = _frange(0, tw, step_x_thumb)

        rows = []
        n_written = n_candidate = 0
        for ty in ys:
            for tx in xs:
                patch = roi_mask[int(round(ty)):int(round(ty + step_y_thumb)),
                                 int(round(tx)):int(round(tx + step_x_thumb))]
                if patch.size == 0:
                    continue
                frac = float(patch.mean())
                if frac < min_roi_frac:
                    continue
                n_candidate += 1
                if limit is not None and n_written >= limit:
                    continue
                # Level-0 top-left for read_region (always level-0 coords).
                x0 = int(round(tx * downsample_x))
                y0 = int(round(ty * downsample_y))
                region = slide.read_region((x0, y0), level,
                                           (tile_size, tile_size)).convert("RGB")
                fname = f"tile_x{x0}_y{y0}.png"
                region.save(out / fname, optimize=True)
                rows.append((fname, x0, y0, level, tile_size, round(frac, 4)))
                n_written += 1

        with open(out / "tiles_index.tsv", "w", newline="") as fh:
            w = csv.writer(fh, delimiter="\t")
            w.writerow(["tile", "level0_x", "level0_y", "level", "tile_size",
                        "roi_frac"])
            w.writerows(rows)
    finally:
        slide.close()

    return {
        "n_written": n_written,
        "n_candidate": n_candidate,
        "tile_size": tile_size,
        "level": level,
        "out_dir": str(out),
        "truncated": bool(limit is not None and n_candidate > n_written),
    }


def export_full_mask(roi_mask: np.ndarray, out_path: str,
                     *, level0_w: int, level0_h: int,
                     block: int = 4096) -> dict[str, Any]:
    """Write a level-0-resolution binary mask PNG by nearest-neighbour upscale.

    Built block-by-block so the full (potentially ~gigapixel) array is never
    held in memory at once. This is opt-in — for most downstream uses the
    GeoJSON + per-axis downsample sidecar are enough and far cheaper.
    """
    from pathlib import Path

    from PIL import Image

    roi_mask = np.asarray(roi_mask, dtype=bool)
    th, tw = roi_mask.shape
    out = Image.new("L", (level0_w, level0_h))
    for y0 in range(0, level0_h, block):
        y1 = min(y0 + block, level0_h)
        # source rows for this block (nearest-neighbour)
        srcy = (np.arange(y0, y1) * th // level0_h).clip(0, th - 1)
        for x0 in range(0, level0_w, block):
            x1 = min(x0 + block, level0_w)
            srcx = (np.arange(x0, x1) * tw // level0_w).clip(0, tw - 1)
            tile = roi_mask[np.ix_(srcy, srcx)].astype(np.uint8) * 255
            out.paste(Image.fromarray(tile, mode="L"), (x0, y0))
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    out.save(out_path, optimize=True)
    return {"path": out_path, "level0_w": level0_w, "level0_h": level0_h}


def _frange(start: float, stop: float, step: float) -> list[float]:
    vals, v = [], float(start)
    if step <= 0:
        return [start]
    while v < stop:
        vals.append(v)
        v += step
    return vals


def point_in_rings(x: float, y: float, exterior: list, holes: list) -> bool:
    """Even-odd test: inside the exterior and outside every hole. (Test aid.)"""
    def inside(ring, px, py):
        c = False
        n = len(ring)
        for i in range(n):
            x0, y0 = ring[i][0], ring[i][1]
            x1, y1 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
            if ((y0 > py) != (y1 > py)) and \
               (px < (x1 - x0) * (py - y0) / (y1 - y0) + x0):
                c = not c
        return c
    if not inside(exterior, x, y):
        return False
    return not any(inside(h, x, y) for h in holes)
