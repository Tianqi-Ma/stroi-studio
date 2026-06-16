"""Studio-wide constants and path conventions.

Nothing here is hard-coded to a particular dataset; the per-project locations
(HistoQC results dir, slide dir, studio output dir) are supplied at runtime and
persisted in the sqlite project store (:mod:`stroi_studio.state`).
"""
from __future__ import annotations

import os

# Python interpreter of the environment where HistoQC is installed. HistoQC is
# launched as a subprocess (``<python> -m histoqc``): its __main__ bakes in
# multiprocessing + argparse, and it uses a *different* openslide build that must
# never share our process. This MUST be a python executable, not the ``histoqc``
# launcher script. Set it for your machine via the environment variable, e.g.
#   export STROI_STUDIO_HISTOQC_PYTHON=/path/to/histoqc-venv/bin/python
# The fallback below is just "python on PATH"; override it if HistoQC lives in a
# separate environment (the usual case).
HISTOQC_PYTHON = os.environ.get(
    "STROI_STUDIO_HISTOQC_PYTHON", os.environ.get("PYTHON", "python3"))

# Default HistoQC config name (resolved by histoqc.config, e.g. "v2.1").
HISTOQC_CONFIG = os.environ.get("STROI_STUDIO_HISTOQC_CONFIG", "v2.1")

# Per-slide HistoQC output suffixes (appended to the full slide filename).
THUMB_SUFFIX = "_thumb.png"
MASK_SUFFIX = "_mask_use.png"
THUMB_SMALL_SUFFIX = "_thumb_small.png"

# results.tsv layout: 5 leading comment lines, then a header line beginning
# "#dataset:filename".
RESULTS_FILENAME = "results.tsv"
RESULTS_HEADER_COL0 = "dataset:filename"

# Annotation brush colours. Cyan is a closed LOOP (filled by stroi.fill_loop);
# green/red are PAINT (used as masks directly, never filled).
LOOP_COLOR = "cyan"            # ROI region of interest
ADD_BACK_COLOR = "green"       # tissue HistoQC wrongly removed -> add back
EXCLUDE_COLOR = (255, 0, 0)    # regions to drop from the ROI (red)

# Review states a slide can be in.
REVIEW_STATES = ("unreviewed", "in_progress", "approved", "skipped", "flagged")

# Studio output basenames (written under <studio_out>/<batch>/<slide_file>/).
ANNOTATION_BASENAME = "_annotation.png"
ROI_PNG_BASENAME = "_roi.png"
ROI_JSON_BASENAME = "_roi.json"
ROI_GEOJSON_BASENAME = "_roi.geojson"
OVERLAY_BASENAME = "_overlay.png"
TILES_DIRNAME = "_tiles"
