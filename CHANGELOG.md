# Changelog

All notable changes to stroi-studio are documented here.

## v0.1.0 — first release

The initial public release: a local Flask web GUI that takes a folder of
whole-slide images through HistoQC quality control, an in-browser review +
annotation step, and bulk export of training-ready ROIs mapped back to the
original slide's full resolution. The original slides are never modified.

### Workflow
- **One-click HistoQC**: run quality control on a folder of slides from the
  dashboard. HistoQC runs as a background subprocess in its own environment
  (never imported in-process), with live progress over Server-Sent Events and a
  cancel button. Can also consume an existing HistoQC output dir.
- **From-scratch start**: pass only `--slide-dir` (no QC output yet); an empty
  dashboard guides you to run HistoQC, after which slides appear for review.
- **2-step review wizard** per slide: **Mark** (adjust the ROI) → **Preview**
  (inspect + set review status). One step visible at a time.

### ROI model — HistoQC tissue is the base
- The ROI **starts as the HistoQC tissue mask**; the reviewer never re-traces
  what QC already found.
- Three region brushes only adjust it:
  - **green — add back** tissue HistoQC wrongly dropped (unioned in);
  - **red — exclude** artefacts / unwanted tissue (subtracted);
  - **cyan — limit to area** (optional): restrict the ROI to drawn loop(s).
- `edited = (tissue ∪ green) \ red`; `ROI = edited ∩ cyan_loops` if any loop is
  drawn, else `edited`. Drawing nothing yields exactly the HistoQC tissue.
- Every brush is **region-filled**: a drawn circle contributes its enclosed
  area, not just the stroke (a thin loop, a solid blob, or a small-gapped loop
  all work; the solid blob never grows spuriously).

### Annotation canvas
- Three stacked canvases (thumbnail / HistoQC tissue tint / paint) plus a
  preview layer that tints the computed ROI on the slide in the Preview step.
- Brush + eraser + undo + clear, adjustable brush size, tissue-overlay toggle.
- **Live per-brush pixel tally** so a missing layer is obvious at a glance.
- Strokes autosave (debounced); a half-finished review survives a reload.
- Colours are recovered server-side by hue, so the three brushes never bleed
  into each other.

### Back-mapping & export (deliverables; originals untouched)
- **Level-0 GeoJSON** polygons (with holes for excluded islands), traced from
  the ROI mask with a dependency-free pixel-exact boundary tracer and scaled by
  the **per-axis, per-slide downsample** read from the slide / `results.tsv`
  (never assumed isotropic).
- **Low-resolution ROI mask PNG + JSON sidecar** (ROI stats, level-0 dims,
  per-axis downsample) for on-the-fly upscaling.
- **High-resolution ROI tiles** cut via openslide `read_region`, plus a
  `tiles_index.tsv` (opt-in).
- **Full-resolution level-0 binary mask** (opt-in; large).
- **Four-panel QC overlay** (thumbnail / tissue / ROI / overlay).
- **Bulk export from the dashboard**: select products (GeoJSON / tiles / mask),
  default to all `approved` slides, run as a background job with a progress bar.
  An approved slide that was never explicitly computed (left unedited) has its
  ROI computed on the fly during export — equal to the HistoQC tissue — so a
  reviewer can simply approve unedited slides without clicking Compute.

### State & robustness
- Per-project **SQLite** store (slides, review status, QC runs, export runs);
  survives reloads and restarts.
- Routes key on an integer `slide_id`, so filenames with `&`, spaces, or double
  extensions are handled safely.
- Gigapixel thumbnails/masks are allowed (Pillow decompression-bomb guard
  disabled for this trusted local data).

### Scope
- **First release targets `.svs`.** The architecture is format-agnostic via
  openslide; unopenable slides (e.g. uncorrected Ventana `.bif`, which fail with
  `Bad direction attribute "LEFT"`) are flagged and skipped, never blocking the
  batch. Integrating a `.bif` orientation pre-fix is future work.

### Library change (in the companion `stroi` package)
- `stroi.build_roi` gained backward-compatible `add_back_mask` / `exclude_mask`
  parameters. (stroi-studio's final ROI composition lives in the studio, not the
  library, to keep `stroi` a clean dependency-light core.)

### Tests
- 44 tests covering ingest, the ROI pipeline / composition, canvas colour
  splitting, level-0 mapping (incl. non-integer anisotropic downsample), tile
  export, batch export, and Flask routes. No real slide data is used; HistoQC
  and openslide are mocked or fed synthetic fixtures.
