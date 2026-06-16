"""Phase 4: high-res tile export from the ROI (openslide mocked, no real WSI)."""
from __future__ import annotations

import csv
import sys
import types
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from stroi_studio import mapping


class _FakeSlide:
    """Minimal openslide stand-in recording read_region calls."""

    def __init__(self, level0_wh, level_downsamples=(1.0, 4.0, 16.0)):
        self.dimensions = level0_wh
        self.level_downsamples = level_downsamples
        self.calls = []

    def read_region(self, location, level, size):
        self.calls.append((location, level, size))
        # solid grey RGBA tile of the requested size
        return Image.new("RGBA", size, (128, 128, 128, 255))

    def close(self):
        pass


@pytest.fixture
def fake_openslide(monkeypatch):
    """Install a fake ``openslide`` module that returns a recording slide."""
    created = {}

    def OpenSlide(path):
        s = _FakeSlide(level0_wh=(3200, 2400))
        created["slide"] = s
        return s

    mod = types.ModuleType("openslide")
    mod.OpenSlide = OpenSlide
    monkeypatch.setitem(sys.modules, "openslide", mod)
    return created


def test_export_tiles_grid_and_index(tmp_path, fake_openslide):
    # thumb 200x150 -> level0 3200x2400 means ds = 16 on both axes
    roi = np.zeros((150, 200), bool)
    roi[40:110, 60:160] = True            # a filled rectangle
    out = tmp_path / "_tiles"
    summary = mapping.export_tiles(
        roi, "fake.svs", str(out), tile_size=256, level=0,
        min_roi_frac=0.5, downsample_x=16.0, downsample_y=16.0)
    assert summary["n_written"] > 0
    assert summary["n_written"] == summary["n_candidate"]   # no limit
    tiles = sorted(out.glob("tile_*.png"))
    assert len(tiles) == summary["n_written"]
    # index has a header + one row per tile, with level-0 coords
    rows = list(csv.reader(open(out / "tiles_index.tsv"), delimiter="\t"))
    assert rows[0] == ["tile", "level0_x", "level0_y", "level", "tile_size",
                       "roi_frac"]
    assert len(rows) - 1 == summary["n_written"]
    # every recorded read_region used level 0 and the requested tile size
    s = fake_openslide["slide"]
    for loc, lvl, size in s.calls:
        assert lvl == 0 and size == (256, 256)
        # location is in level-0 coords (multiple of step*ds, within bounds)
        assert 0 <= loc[0] < 3200 and 0 <= loc[1] < 2400


def test_export_tiles_respects_min_frac(tmp_path, fake_openslide):
    roi = np.zeros((150, 200), bool)
    roi[40:110, 60:160] = True
    strict = mapping.export_tiles(
        roi, "fake.svs", str(tmp_path / "strict"), tile_size=256,
        min_roi_frac=0.99, downsample_x=16.0, downsample_y=16.0)
    loose = mapping.export_tiles(
        roi, "fake.svs", str(tmp_path / "loose"), tile_size=256,
        min_roi_frac=0.1, downsample_x=16.0, downsample_y=16.0)
    assert loose["n_candidate"] >= strict["n_candidate"]


def test_export_tiles_limit_truncates(tmp_path, fake_openslide):
    roi = np.ones((150, 200), bool)        # whole slide is ROI -> many tiles
    summary = mapping.export_tiles(
        roi, "fake.svs", str(tmp_path / "lim"), tile_size=128,
        min_roi_frac=0.1, downsample_x=16.0, downsample_y=16.0, limit=3)
    assert summary["n_written"] == 3
    assert summary["truncated"] is True
    assert summary["n_candidate"] > 3


def test_export_tiles_level_scales_step(tmp_path, fake_openslide):
    """A coarser level reads fewer, larger-footprint tiles."""
    roi = np.ones((150, 200), bool)
    l0 = mapping.export_tiles(
        roi, "fake.svs", str(tmp_path / "l0"), tile_size=256, level=0,
        min_roi_frac=0.1, downsample_x=16.0, downsample_y=16.0)
    l1 = mapping.export_tiles(
        roi, "fake.svs", str(tmp_path / "l1"), tile_size=256, level=1,
        min_roi_frac=0.1, downsample_x=16.0, downsample_y=16.0)
    # level 1 (downsample 4) covers 4x the area per tile -> far fewer tiles
    assert l1["n_candidate"] < l0["n_candidate"]


def test_export_tiles_empty_roi(tmp_path, fake_openslide):
    roi = np.zeros((150, 200), bool)
    summary = mapping.export_tiles(
        roi, "fake.svs", str(tmp_path / "empty"), tile_size=256,
        downsample_x=16.0, downsample_y=16.0)
    assert summary["n_written"] == 0
    # index still written (header only)
    assert (tmp_path / "empty" / "tiles_index.tsv").exists()


def test_export_full_mask_upscales(tmp_path):
    roi = np.zeros((10, 12), bool)
    roi[2:8, 3:9] = True
    out = tmp_path / "m.png"
    summary = mapping.export_full_mask(roi, str(out), level0_w=120, level0_h=100,
                                       block=32)
    assert (summary["level0_w"], summary["level0_h"]) == (120, 100)
    big = np.asarray(Image.open(out).convert("L")) > 127
    assert big.shape == (100, 120)
    # upscaled footprint ~ roi scaled by 10x
    assert big.sum() > 0
    assert big[:20, :].sum() == 0          # top rows (row<2 in thumb) are empty
    assert big[50, 60]                     # centre is ROI
