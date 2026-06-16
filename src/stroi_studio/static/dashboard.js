// stROI Studio — dashboard: run HistoQC, stream progress, re-ingest.
(function () {
  "use strict";

  const banner = document.getElementById("qc-banner");
  if (!banner) return;

  const runBtn = document.getElementById("qc-run-btn");
  const cancelBtn = document.getElementById("qc-cancel-btn");
  const reingestBtn = document.getElementById("reingest-btn");
  const statusLabel = document.getElementById("qc-status-label");
  const progress = document.getElementById("qc-progress");
  const lineEl = document.getElementById("qc-line");

  let evtSource = null;

  function setRunning(running) {
    if (runBtn) runBtn.style.display = running ? "none" : "";
    if (cancelBtn) cancelBtn.style.display = running ? "" : "none";
  }

  function render(p) {
    if (!p) return;
    statusLabel.textContent = "QC: " + (p.status || "?");
    statusLabel.className = "qc-status qc-" + (p.status || "none");
    if (p.n_slides != null) {
      progress.textContent = `${p.n_done || 0}/${p.n_slides || 0}`;
    }
    if (p.last_line) lineEl.textContent = p.last_line;
    setRunning(!!p.running);
    if (!p.running && ["done", "failed", "cancelled"].includes(p.status)) {
      if (evtSource) { evtSource.close(); evtSource = null; }
      if (p.status === "done") {
        // QC finished: re-parse the results so new slides/masks show up.
        fetch("/qc/ingest", { method: "POST" }).then(() =>
          setTimeout(() => location.reload(), 600));
      }
    }
  }

  function startStream() {
    if (evtSource) evtSource.close();
    evtSource = new EventSource("/qc/stream");
    evtSource.onmessage = (e) => {
      try { render(JSON.parse(e.data)); } catch (_) { /* ignore */ }
    };
    evtSource.onerror = () => { /* keep last state; status poll on reload */ };
  }

  if (runBtn) {
    runBtn.addEventListener("click", async () => {
      runBtn.disabled = true;
      const resp = await fetch("/qc/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      const r = await resp.json();
      runBtn.disabled = false;
      if (!resp.ok) { lineEl.textContent = "QC start failed: " + (r.error || resp.status); return; }
      setRunning(true);
      startStream();
    });
  }

  if (cancelBtn) {
    cancelBtn.addEventListener("click", async () => {
      cancelBtn.disabled = true;
      await fetch("/qc/cancel", { method: "POST" });
      cancelBtn.disabled = false;
    });
  }

  if (reingestBtn) {
    reingestBtn.addEventListener("click", async () => {
      reingestBtn.disabled = true;
      await fetch("/ingest", { method: "POST" });
      location.reload();
    });
  }

  // If a run is already in progress when the page loads, attach to the stream.
  fetch("/qc/status").then((r) => r.json()).then((s) => {
    if (s.running) { setRunning(true); startStream(); }
    else if (s.run) render({ ...s.run, running: false });
  });

  // ---- bulk export of approved ROIs -----------------------------------
  const expBar = document.getElementById("export-bar");
  if (expBar) {
    const runBtnE = document.getElementById("export-run-btn");
    const cancelBtnE = document.getElementById("export-cancel-btn");
    const statusE = document.getElementById("export-status");
    const progWrap = document.getElementById("export-progress");
    const progBar = document.getElementById("export-progress-bar");
    const tileSize = document.getElementById("exp-tile-size");
    let poll = null;

    if (tileSize) {
      tileSize.addEventListener("input", () => {
        document.getElementById("exp-tile-size-val").textContent = tileSize.value;
      });
    }

    function expRunning(running) {
      runBtnE.style.display = running ? "none" : "";
      cancelBtnE.style.display = running ? "" : "none";
      progWrap.hidden = !running && !progBar.style.width;
    }

    function renderExp(s) {
      const run = s.run;
      if (!run) return;
      const done = run.n_done || 0, total = run.n_slides || 0;
      const pct = total ? Math.round((done / total) * 100) : 0;
      progWrap.hidden = false;
      progBar.style.width = pct + "%";
      statusE.textContent =
        `${run.status} · ${done}/${total}` + (run.last_line ? ` · ${run.last_line}` : "");
      expRunning(!!s.running);
      if (!s.running && ["done", "failed", "cancelled"].includes(run.status)) {
        if (poll) { clearInterval(poll); poll = null; }
      }
    }

    function startPoll() {
      if (poll) clearInterval(poll);
      poll = setInterval(() => {
        fetch("/export/status").then((r) => r.json()).then(renderExp);
      }, 1000);
    }

    runBtnE.addEventListener("click", async () => {
      const products = [];
      if (document.getElementById("exp-geojson").checked) products.push("geojson");
      if (document.getElementById("exp-tiles").checked) products.push("tiles");
      if (document.getElementById("exp-mask").checked) products.push("mask");
      if (!products.length) { statusE.textContent = "pick at least one product"; return; }
      runBtnE.disabled = true;
      const resp = await fetch("/export/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ products, tile_size: parseInt(tileSize.value, 10) }),
      });
      const r = await resp.json();
      runBtnE.disabled = false;
      if (!resp.ok) { statusE.textContent = "export failed: " + (r.error || resp.status); return; }
      expRunning(true);
      startPoll();
    });

    cancelBtnE.addEventListener("click", async () => {
      cancelBtnE.disabled = true;
      await fetch("/export/cancel", { method: "POST" });
      cancelBtnE.disabled = false;
    });

    // Resume display if an export is already running.
    fetch("/export/status").then((r) => r.json()).then((s) => {
      if (s.running) { expRunning(true); startPoll(); }
      else if (s.run) renderExp(s);
    });
  }
})();
