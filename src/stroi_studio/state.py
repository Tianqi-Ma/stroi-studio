"""SQLite-backed project state.

The workflow spans a slow QC run plus a long human review session with the
dashboard polling for status, so state must survive page reloads and server
restarts. SQLite (WAL mode) gives atomic per-slide commits without a server.

The store lives at ``<studio_out>/<batch>/studio.sqlite``. Large artefacts
(annotation PNG, ROI mask, overlay) live on disk; only their paths are stored.
Slides are keyed by an integer ``slide_id`` so routes never have to embed
filenames containing ``&``, spaces, or double extensions.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS project (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    schema_version  INTEGER NOT NULL,
    batch           TEXT NOT NULL,
    results_dir     TEXT NOT NULL,
    slide_dir       TEXT,
    studio_out      TEXT NOT NULL,
    loop_color      TEXT NOT NULL DEFAULT 'cyan',
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS qc_run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    status      TEXT NOT NULL,            -- starting|running|done|failed|cancelled
    pid         INTEGER,
    cmd         TEXT,
    log_path    TEXT,
    n_slides    INTEGER DEFAULT 0,
    n_done      INTEGER DEFAULT 0,
    last_line   TEXT,
    started_at  REAL,
    ended_at    REAL
);

CREATE TABLE IF NOT EXISTS export_run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    status      TEXT NOT NULL,            -- running|done|failed|cancelled
    products    TEXT,                     -- comma list: geojson,tiles,mask
    n_slides    INTEGER DEFAULT 0,
    n_done      INTEGER DEFAULT 0,
    last_line   TEXT,
    out_dir     TEXT,
    started_at  REAL,
    ended_at    REAL
);

CREATE TABLE IF NOT EXISTS slide (
    slide_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    slide_file      TEXT NOT NULL UNIQUE,   -- full name incl. extension
    display_name    TEXT NOT NULL,
    subdir          TEXT NOT NULL,          -- per-slide HistoQC dir (abs)
    thumb_path      TEXT,
    mask_use_path   TEXT,
    thumb_small_path TEXT,
    slide_path      TEXT,                   -- original WSI on disk, if located
    readable        INTEGER DEFAULT 0,      -- 1 if openslide can open the WSI
    open_error      TEXT,                   -- why it couldn't (e.g. .bif)
    thumb_w         INTEGER,
    thumb_h         INTEGER,
    level0_w        INTEGER,
    level0_h        INTEGER,
    downsample_x    REAL,
    downsample_y    REAL,
    mpp_x           REAL,
    mpp_y           REAL,
    base_mag        REAL,
    review_status   TEXT NOT NULL DEFAULT 'unreviewed',
    reviewer_note   TEXT,
    roi_mode        TEXT,
    roi_px          INTEGER,
    tissue_px       INTEGER,
    annotation_png  TEXT,
    roi_png         TEXT,
    roi_json        TEXT,
    geojson         TEXT,
    overlay         TEXT,
    updated_at      REAL
);
"""


class Store:
    """Thin wrapper over a per-project sqlite database."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def _init_db(self) -> None:
        with self._connect() as con:
            con.executescript(_SCHEMA)

    # --- project ----------------------------------------------------------

    def set_project(self, *, batch: str, results_dir: str, studio_out: str,
                    slide_dir: Optional[str] = None,
                    loop_color: str = "cyan") -> None:
        with self._connect() as con:
            con.execute(
                "INSERT INTO project (id, schema_version, batch, results_dir, "
                "slide_dir, studio_out, loop_color, created_at) "
                "VALUES (1, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET batch=excluded.batch, "
                "results_dir=excluded.results_dir, slide_dir=excluded.slide_dir, "
                "studio_out=excluded.studio_out, loop_color=excluded.loop_color",
                (SCHEMA_VERSION, batch, results_dir, slide_dir, studio_out,
                 loop_color, time.time()))

    def get_project(self) -> Optional[dict[str, Any]]:
        with self._connect() as con:
            row = con.execute("SELECT * FROM project WHERE id=1").fetchone()
        return dict(row) if row else None

    # --- slides -----------------------------------------------------------

    def upsert_slide(self, rec: dict[str, Any]) -> int:
        """Insert or update a slide by its unique ``slide_file``. Returns id."""
        rec = dict(rec)
        rec["updated_at"] = time.time()
        cols = [c for c in rec if c != "slide_id"]
        placeholders = ", ".join(f":{c}" for c in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols
                            if c != "slide_file")
        sql = (
            f"INSERT INTO slide ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(slide_file) DO UPDATE SET {updates}"
        )
        with self._connect() as con:
            con.execute(sql, rec)
            row = con.execute(
                "SELECT slide_id FROM slide WHERE slide_file=:slide_file",
                rec).fetchone()
        return int(row["slide_id"])

    def list_slides(self) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM slide ORDER BY slide_id").fetchall()
        return [dict(r) for r in rows]

    def get_slide(self, slide_id: int) -> Optional[dict[str, Any]]:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM slide WHERE slide_id=?", (slide_id,)).fetchone()
        return dict(row) if row else None

    def update_slide(self, slide_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._connect() as con:
            con.execute(f"UPDATE slide SET {sets} WHERE slide_id=?",
                        (*fields.values(), slide_id))

    # --- qc runs ----------------------------------------------------------

    def create_qc_run(self, *, cmd: str, log_path: str, n_slides: int) -> int:
        with self._connect() as con:
            cur = con.execute(
                "INSERT INTO qc_run (status, cmd, log_path, n_slides, "
                "started_at) VALUES ('starting', ?, ?, ?, ?)",
                (cmd, log_path, n_slides, time.time()))
            return int(cur.lastrowid)

    def update_qc_run(self, run_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._connect() as con:
            con.execute(f"UPDATE qc_run SET {sets} WHERE id=?",
                        (*fields.values(), run_id))

    def get_qc_run(self, run_id: int) -> Optional[dict[str, Any]]:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM qc_run WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def latest_qc_run(self) -> Optional[dict[str, Any]]:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM qc_run ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    # --- export runs ------------------------------------------------------

    def create_export_run(self, *, products: str, n_slides: int,
                          out_dir: str) -> int:
        with self._connect() as con:
            cur = con.execute(
                "INSERT INTO export_run (status, products, n_slides, out_dir, "
                "started_at) VALUES ('running', ?, ?, ?, ?)",
                (products, n_slides, out_dir, time.time()))
            return int(cur.lastrowid)

    def update_export_run(self, run_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._connect() as con:
            con.execute(f"UPDATE export_run SET {sets} WHERE id=?",
                        (*fields.values(), run_id))

    def latest_export_run(self) -> Optional[dict[str, Any]]:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM export_run ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None
