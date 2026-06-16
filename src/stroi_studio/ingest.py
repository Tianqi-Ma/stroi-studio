"""Ingest a HistoQC results directory into the project store.

We read ``results.tsv`` (5 comment lines + a ``#dataset:filename`` header) for
level-0 dimensions and metadata, locate each slide's per-slide subdirectory and
its ``_thumb.png`` / ``_mask_use.png``, optionally locate the original WSI on
disk, and probe whether openslide can open it (``.svs`` yes; unfixed Ventana
``.bif`` no — those are flagged ``unsupported`` but do not block the batch).

The downsample factor is read per-slide and per-axis from the real thumbnail vs
level-0 dimensions — never hard-coded — because it is non-integer (~14–16x) and
can differ between axes.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Optional

from PIL import Image

from . import config
from .state import Store


def _read_results_tsv(results_path: Path) -> dict[str, dict[str, str]]:
    """Return ``{slide_file: {column: value}}`` from a HistoQC results.tsv."""
    out: dict[str, dict[str, str]] = {}
    header: Optional[list[str]] = None
    with open(results_path, newline="") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line.startswith("#" + config.RESULTS_HEADER_COL0):
                header = line.lstrip("#").split("\t")
                continue
            if line.startswith("#") or not line.strip():
                continue
            if header is None:
                continue
            cells = line.split("\t")
            row = {h: (cells[i] if i < len(cells) else "")
                   for i, h in enumerate(header)}
            sid = row.get(config.RESULTS_HEADER_COL0, "")
            if sid:
                out[sid] = row
    return out


def _to_float(s: str) -> Optional[float]:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _probe_openslide(slide_path: Path) -> tuple[bool, Optional[str],
                                                 Optional[tuple[int, int]]]:
    """Try to open the WSI. Returns ``(readable, error, (w, h) or None)``.

    Imported lazily and locally so a missing/odd openslide never breaks ingest,
    and so we never import the HistoQC env's openslide build.
    """
    try:
        import openslide
        s = openslide.OpenSlide(str(slide_path))
        try:
            return True, None, s.dimensions
        finally:
            s.close()
    except Exception as e:  # noqa: BLE001 - we want the message, any error
        return False, f"{type(e).__name__}: {e}"[:300], None


def _find_slide_file(slide_dir: Optional[Path], slide_file: str) -> Optional[Path]:
    if not slide_dir:
        return None
    cand = slide_dir / slide_file
    if cand.exists():
        return cand
    # Fall back to a recursive search (the dir may be nested).
    matches = list(slide_dir.rglob(slide_file))
    return matches[0] if matches else None


def ingest(store: Store, *, results_dir: str | Path,
           slide_dir: Optional[str | Path] = None,
           probe_openslide: bool = True) -> dict[str, Any]:
    """Populate ``store`` from a HistoQC results directory. Idempotent.

    ``probe_openslide`` opens each located slide to confirm readability and read
    exact level-0 dims. It is the slow part on a large cohort (one openslide
    open per slide), so callers can pass ``False`` for a fast first paint and
    rely on the results.tsv dims; readability is then resolved lazily (e.g. at
    tile-export time).
    """
    results_dir = Path(results_dir)
    slide_dir = Path(slide_dir) if slide_dir else None
    results_tsv = results_dir / config.RESULTS_FILENAME

    meta = _read_results_tsv(results_tsv) if results_tsv.exists() else {}

    n_total = n_readable = n_unsupported = n_missing_thumb = 0
    for subdir in sorted(p for p in results_dir.iterdir() if p.is_dir()):
        slide_file = subdir.name           # named with the full filename + ext
        thumb = subdir / f"{slide_file}{config.THUMB_SUFFIX}"
        mask = subdir / f"{slide_file}{config.MASK_SUFFIX}"
        thumb_small = subdir / f"{slide_file}{config.THUMB_SMALL_SUFFIX}"
        if not thumb.exists():
            n_missing_thumb += 1
            continue
        n_total += 1

        thumb_w = thumb_h = None
        try:
            with Image.open(thumb) as im:
                thumb_w, thumb_h = im.size
        except Exception:  # noqa: BLE001
            pass

        row = meta.get(slide_file, {})
        level0_w = _to_float(row.get("width", "")) or None
        level0_h = _to_float(row.get("height", "")) or None

        slide_path = _find_slide_file(slide_dir, slide_file)
        # readable: 1 openable / 0 unopenable / -1 not yet probed (lazy).
        readable, open_error, os_dims = -1, None, None
        if slide_path is not None and probe_openslide:
            ok, open_error, os_dims = _probe_openslide(slide_path)
            readable = 1 if ok else 0
            if os_dims is not None:
                # Prefer openslide's exact dims over the TSV when both present.
                level0_w, level0_h = float(os_dims[0]), float(os_dims[1])
        elif slide_path is None:
            readable = 0

        ds_x = (level0_w / thumb_w) if (level0_w and thumb_w) else None
        ds_y = (level0_h / thumb_h) if (level0_h and thumb_h) else None

        if readable == 1:
            n_readable += 1
        elif readable == 0 and slide_path is not None:
            n_unsupported += 1

        store.upsert_slide({
            "slide_file": slide_file,
            "display_name": slide_file,
            "subdir": str(subdir),
            "thumb_path": str(thumb),
            "mask_use_path": str(mask) if mask.exists() else None,
            "thumb_small_path": str(thumb_small) if thumb_small.exists() else None,
            "slide_path": str(slide_path) if slide_path else None,
            "readable": readable,
            "open_error": open_error,
            "thumb_w": thumb_w,
            "thumb_h": thumb_h,
            "level0_w": int(level0_w) if level0_w else None,
            "level0_h": int(level0_h) if level0_h else None,
            "downsample_x": ds_x,
            "downsample_y": ds_y,
            "mpp_x": _to_float(row.get("mpp_x", "")),
            "mpp_y": _to_float(row.get("mpp_y", "")),
            "base_mag": _to_float(row.get("base_mag", "")),
        })

    return {
        "n_total": n_total,
        "n_readable": n_readable,
        "n_unsupported": n_unsupported,
        "n_missing_thumb": n_missing_thumb,
        "has_results_tsv": results_tsv.exists(),
    }
