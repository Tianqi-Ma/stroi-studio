"""Phase 5: HistoQC orchestration — log parsing + runner + routes (mocked).

HistoQC itself is never launched; we mock subprocess.Popen with a fake process
that writes a HistoQC-style log so the progress watcher and routes can be tested.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from stroi_studio.app import create_app
from stroi_studio.histoqc_runner import QCRunner, _parse_line
from stroi_studio.state import Store


def test_parse_working_line():
    n_done, total, disp = _parse_line(
        "2026-05-15 06:34:00,000 - INFO - -----Working on:\tA.svs\t\t1 of 369")
    assert (n_done, total) == (0, 369)          # slide 1 started -> 0 done
    assert "1/369" in disp
    n_done, total, _ = _parse_line(
        "... -----Working on:\tB.svs\t\t5 of 369")
    assert (n_done, total) == (4, 369)


def test_parse_done_and_noise():
    assert _parse_line("--- INFO - ----------Done-----------")[2] == "Done"
    assert _parse_line("random warning line") == (None, None, None)


class _FakePopen:
    """A subprocess.Popen stand-in that writes a HistoQC-style log then exits."""

    def __init__(self, *a, **k):
        self.pid = 4242
        self._poll = None
        # Write the log the runner tails.
        # In Popen(stdout=logfile,...) the file object is passed as stdout.
        self._logfile = k.get("stdout")
        self._started = time.time()
        self._lines = [
            "INFO - -----Working on:\tH&E_A.svs\t\t1 of 2\n",
            "INFO - -----Working on:\tH&E_B.svs\t\t2 of 2\n",
            "INFO - ----------Done-----------\n",
        ]
        self._i = 0

    def poll(self):
        # Emit one line per poll, then finish.
        if self._i < len(self._lines):
            if self._logfile:
                self._logfile.write(self._lines[self._i])
                self._logfile.flush()
            self._i += 1
            return None
        return 0

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def fake_popen(monkeypatch):
    import stroi_studio.histoqc_runner as hr
    monkeypatch.setattr(hr.subprocess, "Popen",
                        lambda *a, **k: _FakePopen(*a, **k))
    # os.killpg / getpgid are irrelevant for the fake; stub them out.
    monkeypatch.setattr(hr.os, "killpg", lambda *a, **k: None)
    monkeypatch.setattr(hr.os, "getpgid", lambda pid: pid)
    return hr


def test_runner_tracks_progress(tmp_path, fake_popen):
    store = Store(tmp_path / "s.sqlite")
    runner = QCRunner(store)
    res = runner.start(slides=["a.svs", "b.svs"],
                       out_dir=str(tmp_path / "Results" / "Demo"),
                       nprocesses=1)
    assert "run_id" in res
    # wait for the watcher thread to finish (fake process completes quickly)
    for _ in range(40):
        run = store.get_qc_run(res["run_id"])
        if run and run["status"] in ("done", "failed", "cancelled"):
            break
        time.sleep(0.2)
    run = store.get_qc_run(res["run_id"])
    assert run["status"] == "done"
    assert run["n_slides"] == 2
    assert run["n_done"] >= 1
    log = Path(run["log_path"])
    assert log.exists() and "Working on" in log.read_text()


def test_qc_routes(results_dir, tmp_path, fake_popen):
    # results_dir doubles as the slide_dir for the run (it has no .svs, so we
    # point pattern at the existing thumb png to get a non-empty slide list).
    app = create_app(results_dir=str(results_dir),
                     studio_out=str(tmp_path / "out"),
                     slide_dir=str(results_dir))
    c = app.test_client()
    # No matching .svs -> 400
    assert c.post("/qc/run", json={"pattern": "*.svs"}).status_code == 400
    # Match something that exists so the runner starts.
    r = c.post("/qc/run", json={"pattern": "**/*_thumb.png", "nprocesses": 1})
    assert r.status_code == 200
    run_id = r.get_json()["run_id"]
    # status endpoint responds
    st = c.get("/qc/status").get_json()
    assert "running" in st
    # let it finish
    for _ in range(40):
        run = app.config["STORE"].get_qc_run(run_id)
        if run and run["status"] in ("done", "failed", "cancelled"):
            break
        time.sleep(0.2)
    assert app.config["STORE"].get_qc_run(run_id)["status"] == "done"


def test_qc_run_requires_slide_dir(results_dir, tmp_path, fake_popen):
    app = create_app(results_dir=str(results_dir),
                     studio_out=str(tmp_path / "out"))  # no slide_dir
    r = app.test_client().post("/qc/run", json={})
    assert r.status_code == 400
