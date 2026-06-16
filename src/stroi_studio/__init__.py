"""stroi-studio — a local web GUI around the :mod:`stroi` ROI workflow.

The studio wraps four steps into one browser app served on localhost (reached
over an SSH tunnel on a headless server):

1. Run HistoQC quality control on a folder of whole-slide images (subprocess).
2. Review each slide's thumbnail and tissue mask.
3. Paint annotations directly on the thumbnail with three brushes:
   cyan = ROI loop, green = add-back tissue, red = exclude region.
4. Build the ROI (``stroi.build_roi``) and map it back to the original slide's
   level-0 coordinate space for downstream high-resolution training — without
   modifying the original slide.

The package keeps :mod:`stroi` a clean library; everything web/HistoQC/stateful
lives here.
"""

__version__ = "0.1.0"

# Whole-slide thumbnails and level-0 masks are legitimately gigapixel; the data
# is local and trusted, so disable Pillow's decompression-bomb guard (which
# otherwise refuses to open images above ~179 Mpx).
try:
    from PIL import Image as _Image
    _Image.MAX_IMAGE_PIXELS = None
except Exception:  # pragma: no cover - Pillow always present in practice
    pass
