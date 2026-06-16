"""Run HistoQC as a subprocess and track its progress.

HistoQC lives in a *separate* Python environment and bakes multiprocessing +
argparse into its ``__main__``, so it cannot be imported in-process — and its
openslide build differs from ours and must never share our process. We therefore
launch it strictly as a subprocess in its own session (so the whole worker tree
can be killed on cancel), tee its output to a log file, and parse the per-slide
``-----Working on: <file>  <i> of <N>`` lines it logs to stderr for progress.

State lives in the ``qc_run`` table; the app exposes status/SSE/cancel routes.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from . import config
from .state import Store

# HistoQC worker line: "...-----Working on:\t<file>\t\t<i> of <N>"
_WORKING_RE = re.compile(r"Working on:\s*(.+?)\s+(\d+)\s+of\s+(\d+)")
_DONE_RE = re.compile(r"-+\s*Done\s*-+")


class QCRunner:
    """Owns at most one running HistoQC subprocess for a project."""

    def __init__(self, store: Store):
        self.store = store
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._run_id: Optional[int] = None
        self._lock = threading.Lock()

    # --- lifecycle --------------------------------------------------------

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, *, slides: list[str], out_dir: str,
              config_name: str = config.HISTOQC_CONFIG,
              nprocesses: int = 4,
              histoqc_python: str = config.HISTOQC_PYTHON,
              force: bool = True) -> dict:
        """Launch HistoQC over ``slides``. Returns ``{run_id, ...}``."""
        with self._lock:
            if self.is_running():
                return {"error": "a QC run is already in progress",
                        "run_id": self._run_id}

            out = Path(out_dir)
            out.mkdir(parents=True, exist_ok=True)
            log_dir = out.parent / "qc_runs"
            log_dir.mkdir(parents=True, exist_ok=True)

            cmd = [histoqc_python, "-m", "histoqc",
                   "-c", config_name, "-o", str(out),
                   "-n", str(nprocesses)]
            if force:
                cmd.append("-f")
            cmd += list(slides)

            run_id = self.store.create_qc_run(
                cmd=" ".join(cmd), log_path="", n_slides=len(slides))
            log_path = str(log_dir / f"run_{run_id}.log")
            self.store.update_qc_run(run_id, log_path=log_path)

            # A clean environment: do not leak our venv's VIRTUAL_ENV/PATH into
            # the HistoQC interpreter (it has its own openslide build).
            env = {k: v for k, v in os.environ.items()
                   if k not in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME")}

            logfile = open(log_path, "w")
            proc = subprocess.Popen(
                cmd, stdout=logfile, stderr=subprocess.STDOUT,
                start_new_session=True, env=env, cwd=str(out.parent))
            self._proc = proc
            self._run_id = run_id
            self.store.update_qc_run(run_id, status="running", pid=proc.pid)

            self._thread = threading.Thread(
                target=self._watch, args=(run_id, log_path, logfile),
                daemon=True)
            self._thread.start()
            return {"run_id": run_id, "pid": proc.pid, "n_slides": len(slides)}

    def cancel(self) -> dict:
        """Kill the whole process group of the running HistoQC subprocess."""
        with self._lock:
            if not self.is_running():
                return {"error": "no QC run is in progress"}
            assert self._proc is not None
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            run_id = self._run_id
        # give it a moment, then SIGKILL if needed
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        if run_id is not None:
            self.store.update_qc_run(run_id, status="cancelled",
                                     ended_at=time.time())
        return {"ok": True, "run_id": run_id}

    # --- progress watcher -------------------------------------------------

    def _watch(self, run_id: int, log_path: str, logfile) -> None:
        """Tail the log, update n_done/last_line, finalise on exit."""
        proc = self._proc
        assert proc is not None
        last_done = 0
        try:
            with open(log_path, "r") as fh:
                while True:
                    line = fh.readline()
                    if line:
                        n_done, total, last = _parse_line(line)
                        fields = {}
                        if last is not None:
                            fields["last_line"] = last
                        if n_done is not None and n_done > last_done:
                            last_done = n_done
                            fields["n_done"] = n_done
                        if total is not None:
                            fields["n_slides"] = total
                        if fields:
                            self.store.update_qc_run(run_id, **fields)
                        continue
                    # no new line: stop if the process has exited
                    if proc.poll() is not None:
                        # drain any final buffered lines
                        rest = fh.read()
                        for rl in rest.splitlines():
                            _, _, last = _parse_line(rl)
                            if last:
                                self.store.update_qc_run(run_id, last_line=last)
                        break
                    time.sleep(0.5)
        finally:
            try:
                logfile.close()
            except Exception:
                pass
            rc = proc.poll()
            cur = self.store.get_qc_run(run_id) or {}
            if cur.get("status") not in ("cancelled",):
                status = "done" if rc == 0 else "failed"
                self.store.update_qc_run(run_id, status=status,
                                         ended_at=time.time())


def _parse_line(line: str) -> tuple[Optional[int], Optional[int], Optional[str]]:
    """Return ``(n_done, total, display_line)`` parsed from one log line."""
    line = line.rstrip("\n")
    m = _WORKING_RE.search(line)
    if m:
        # "i of N" means slide i has STARTED; treat i-1 as completed.
        i, n = int(m.group(2)), int(m.group(3))
        return i - 1, n, f"Working on {m.group(1)} ({i}/{n})"
    if _DONE_RE.search(line):
        return None, None, "Done"
    return None, None, None
