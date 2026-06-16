"""Synthetic HistoQC-style fixtures — no real slide data anywhere.

We fabricate a minimal results dir that mirrors HistoQC's layout: a results.tsv
with 5 comment lines + a ``#dataset:filename`` header, and per-slide subdirs
named with the full filename, each holding ``<name>_thumb.png`` and
``<name>_mask_use.png``.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image


def _draw_rect_loop(img, color, y0, y1, x0, x1, width=3):
    c = np.array(color, dtype=np.uint8)
    img[y0:y0 + width, x0:x1] = c
    img[y1 - width:y1, x0:x1] = c
    img[y0:y1, x0:x0 + width] = c
    img[y0:y1, x1 - width:x1] = c
    return img


# A slide filename with the awkward characters real data has.
SLIDE_FILE = "H&E_Demo-1.svs"
THUMB_W, THUMB_H = 240, 200
LEVEL0_W, LEVEL0_H = 3427, 2851          # non-integer, non-equal downsample


@pytest.fixture
def results_dir(tmp_path: Path) -> Path:
    root = tmp_path / "Results" / "Demo"
    sub = root / SLIDE_FILE
    sub.mkdir(parents=True)

    thumb = np.full((THUMB_H, THUMB_W, 3), (220, 180, 210), np.uint8)
    Image.fromarray(thumb).save(sub / f"{SLIDE_FILE}_thumb.png")

    mask = np.zeros((THUMB_H, THUMB_W), np.uint8)
    mask[40:160, 40:200] = 255
    Image.fromarray(mask, mode="L").save(sub / f"{SLIDE_FILE}_mask_use.png")

    header = ["dataset:filename", "comments", "image_bounding_box", "base_mag",
              "type", "levels", "height", "width", "mpp_x", "mpp_y", "warnings"]
    row = [SLIDE_FILE, "", "", "20.0", "aperio", "3", str(LEVEL0_H),
           str(LEVEL0_W), "0.5", "0.5", ""]
    with open(root / "results.tsv", "w") as fh:
        fh.write("#start_time:\t2026-01-01\n#pipeline:\tdemo\n#outdir:\t.\n")
        fh.write("#config_file:\tv2.1\n#command_line_args:\t-c v2.1\n")
        fh.write("#" + "\t".join(header) + "\n")
        fh.write("\t".join(row) + "\n")
    return root


@pytest.fixture
def cyan_annotation_png(tmp_path: Path) -> Path:
    """A flattened RGB annotation PNG: cyan loop + green block + red block."""
    img = np.zeros((THUMB_H, THUMB_W, 3), np.uint8)
    _draw_rect_loop(img, (0, 255, 255), 60, 140, 60, 180, width=4)   # cyan loop
    img[150:170, 50:120] = (0, 255, 0)                                # green add-back (below loop)
    img[95:115, 90:130] = (255, 0, 0)                                 # red exclude (inside loop)
    p = tmp_path / "annotation.png"
    Image.fromarray(img).save(p)
    return p
