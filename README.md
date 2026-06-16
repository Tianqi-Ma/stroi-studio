# stroi-studio

**English** · [简体中文](README.zh-CN.md)

A local web GUI around the [stROI](https://github.com/Tianqi-Ma/stROI) ROI
workflow for digital pathology. It turns marker-pen–annotated whole-slide images
into clean, high-resolution training regions — in the browser, on a (often
headless) server reached over an SSH tunnel.

```
 ┌─ HistoQC ─┐   ┌─ Review + adjust ───────────┐   ┌─ Map back ──────────┐
 │ run QC on │ → │ ROI = HistoQC tissue,        │ → │ level-0 GeoJSON     │
 │ a folder  │   │ brushes only fix it:         │   │ + low-res mask+json │
 │ of slides │   │ green add · red exclude ·    │   │ + high-res tiles    │
 │           │   │ cyan limit-to-area (optional)│   │ (bulk, background)  │
 └───────────┘   └──────────────────────────────┘   └─────────────────────┘
```

The original slide is **never modified**; everything is written to a separate
studio output directory.

## How the ROI is built

The ROI **starts as the HistoQC tissue mask** — you don't re-trace what QC
already found. The three brushes only adjust it:

- **green — add back**: regions HistoQC wrongly dropped, unioned into the tissue;
- **red — exclude**: artefacts / unwanted tissue, subtracted from it;
- **cyan — limit to area** (optional): when you only want certain regions, draw a
  loop and the ROI is restricted to what falls inside it.

Formally: `edited = (tissue ∪ green) \ red`, then `ROI = edited ∩ cyan_loops`
if any loop was drawn, else `ROI = edited`. Every brush is region-filled, so a
drawn circle contributes its **enclosed area**, not just the stroke. Drawing
nothing yields exactly the HistoQC tissue.

## The 2-step review flow

1. **Mark** — adjust the ROI on the thumbnail with the three brushes (live
   per-brush pixel tally; undo / clear; adjustable brush size; HistoQC tissue
   overlay toggle). Click **Compute ROI**.
2. **Preview** — the computed ROI is tinted on the slide; open the four-panel QC
   overlay; set a review status (`approved` / `skipped` / `flagged` / …) and move
   to the next slide.

Export is **not** per-slide: once slides are approved, export them **in bulk**
from the dashboard (GeoJSON / high-res tiles / level-0 mask, chosen as
checkboxes, run as a background job with a progress bar). An approved slide you
left unedited (never clicked Compute) is computed on the fly during export — its
ROI is simply the HistoQC tissue — so you can approve unchanged slides directly.

## Install

```bash
pip install -e /path/to/stROI          # the stroi library (if not already)
pip install -e /path/to/stroi-studio   # this package (pulls in flask)
```

HistoQC must live in a **separate Python environment** (it has its own openslide
build and is launched only as a subprocess). Point the studio at that
environment's interpreter:

```bash
export STROI_STUDIO_HISTOQC_PYTHON=/path/to/histoqc-venv/bin/python
# optional: export STROI_STUDIO_HISTOQC_CONFIG=v2.1
```

## Run

```bash
stroi-studio \
  --results-dir /path/to/histoqc_output \   # existing QC output (optional)
  --slide-dir   /path/to/slides \           # original WSIs
  --studio-out  /path/to/studio_output \
  --port 5005
```

Then from your laptop:

```bash
ssh -L 5005:localhost:5005 <server>
# open http://localhost:5005
```

- `--results-dir` — an existing HistoQC output dir (with `results.tsv` and
  per-slide subdirs). **Omit it to start from scratch**: pass only `--slide-dir`
  and run HistoQC from the dashboard; a results dir is created under
  `--studio-out`.
- `--slide-dir` — folder of original slides; required for back-mapping and for
  running HistoQC from the GUI.
- `--studio-out` — where studio writes its state and per-slide outputs.

## Outputs (per slide, under `<studio-out>/<batch>/<slide_file>/`)

| File | What |
|---|---|
| `<slide>_annotation.png` | your flattened green/red/cyan strokes (re-openable) |
| `<slide>_roi.png` | thumbnail-resolution binary ROI mask |
| `<slide>_roi.json` | sidecar: ROI stats + level-0 dims + per-axis downsample |
| `<slide>_roi.geojson` | ROI polygons in **level-0 pixel coords** (QuPath / openslide) |
| `<slide>_overlay.png` | four-panel QC figure (thumb / tissue / ROI / overlay) |
| `_tiles/` + `tiles_index.tsv` | high-res tiles cut from the ROI (opt-in) |
| `<slide>_roi_level0.png` | full-resolution binary mask (opt-in; large) |

Project state lives in `<studio-out>/<batch>/studio.sqlite`.

## Scope & notes

- **First release targets `.svs`.** The architecture is format-agnostic (it goes
  through openslide), but `.svs` is what is currently verified end-to-end.
- **Ventana `.bif`** is not yet supported: libopenslide (3.x/4.x) fails to open
  these files with `Bad direction attribute "LEFT"`. A slide whose TIFF
  orientation tag has been corrected (LEFT→RIGHT) opens fine; integrating that
  fix as an ingest pre-step is future work. Unopenable slides are flagged and
  skipped, never blocking the rest of a batch.
- The per-slide downsample is **read per-axis** from the slide / `results.tsv`
  (e.g. 16.0000 × 15.9949) — never assumed isotropic.

## Tests

```bash
python -m pytest tests -q          # GUI + mapping + QC + export (mocked)
```

No real slide data is used in tests; HistoQC and openslide are mocked or fed
synthetic fixtures.
