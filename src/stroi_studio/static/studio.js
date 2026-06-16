// stROI Studio — review page: paint canvas + ROI compute.
//
// Three brushes paint onto one canvas at the thumbnail's native resolution:
//   cyan  (0,255,255) — closed ROI loop      green (0,255,0) — add back
//   red   (255,0,0)   — exclude              eraser           — clear to transparent
//
// On export we DO NOT ship the live canvas (whose anti-aliased edges blend
// toward the backdrop). Instead we re-flatten each colour onto an opaque black
// RGB image with a fixed precedence so every painted pixel is exactly one of
// the three pure colours — what canvas_io.split_layers expects.
(function () {
  "use strict";

  const stage = document.getElementById("stage");
  if (!stage) return;

  const slideId = stage.dataset.slideId;
  const W = parseInt(stage.dataset.thumbW, 10);
  const H = parseInt(stage.dataset.thumbH, 10);
  const saveUrl = stage.dataset.saveUrl;
  const computeUrl = stage.dataset.computeUrl;

  const COLORS = {
    cyan: [0, 255, 255],
    green: [0, 255, 0],
    red: [255, 0, 0],
  };

  const cThumb = document.getElementById("c-thumb");
  const cTissue = document.getElementById("c-tissue");
  const cPaint = document.getElementById("c-paint");
  const cPreview = document.getElementById("c-preview");
  [cThumb, cTissue, cPaint, cPreview].forEach((c) => { c.width = W; c.height = H; });

  const pctx = cPaint.getContext("2d", { willReadFrequently: true });
  pctx.lineCap = "round";
  pctx.lineJoin = "round";
  const prevctx = cPreview.getContext("2d", { willReadFrequently: true });

  // Draw the computed ROI mask, tinted, on the preview layer (step 2). Shows
  // EXACTLY what was selected (cyan ∪ green − red), so the union is visible.
  function showRoiPreview(url) {
    const img = new Image();
    img.onload = () => {
      const tmp = document.createElement("canvas");
      tmp.width = W; tmp.height = H;
      const tctx = tmp.getContext("2d");
      tctx.drawImage(img, 0, 0, W, H);
      const id = tctx.getImageData(0, 0, W, H);
      const d = id.data;
      for (let i = 0; i < d.length; i += 4) {
        if (d[i] > 127) { d[i] = 255; d[i + 1] = 80; d[i + 2] = 220; d[i + 3] = 110; }
        else { d[i + 3] = 0; }
      }
      tctx.putImageData(id, 0, 0);
      prevctx.clearRect(0, 0, W, H);
      prevctx.drawImage(tmp, 0, 0);
      cPreview.style.display = "";
    };
    img.src = url + "?t=" + Date.now();
  }
  function hideRoiPreview() {
    prevctx.clearRect(0, 0, W, H);
    cPreview.style.display = "none";
  }
  hideRoiPreview();   // hidden until an ROI is computed

  // --- load thumbnail + tissue tint -------------------------------------
  const thumbImg = new Image();
  thumbImg.onload = () => cThumb.getContext("2d").drawImage(thumbImg, 0, 0, W, H);
  thumbImg.src = stage.dataset.thumbUrl;

  const maskImg = new Image();
  maskImg.crossOrigin = "anonymous";
  maskImg.onload = () => drawTissueTint();
  maskImg.src = stage.dataset.maskUrl;

  function drawTissueTint() {
    const ctx = cTissue.getContext("2d");
    ctx.clearRect(0, 0, W, H);
    if (!document.getElementById("show-tissue").checked) return;
    // Render the mask, then tint white tissue pixels with a translucent blue.
    const tmp = document.createElement("canvas");
    tmp.width = W; tmp.height = H;
    const tctx = tmp.getContext("2d");
    tctx.drawImage(maskImg, 0, 0, W, H);
    const id = tctx.getImageData(0, 0, W, H);
    const d = id.data;
    for (let i = 0; i < d.length; i += 4) {
      if (d[i] > 127) { d[i] = 40; d[i + 1] = 120; d[i + 2] = 230; d[i + 3] = 60; }
      else { d[i + 3] = 0; }
    }
    tctx.putImageData(id, 0, 0);
    ctx.drawImage(tmp, 0, 0);
  }

  // --- restore a prior annotation, if any -------------------------------
  // The saved PNG is OPAQUE (black background + pure-colour strokes) because
  // that is what the server's colour splitter expects. If we drew it straight
  // onto the paint layer it would cover the thumbnail with solid black, so we
  // first turn every black pixel back into transparency and keep only strokes.
  if (stage.dataset.hasAnnotation === "1") {
    const ann = new Image();
    ann.onload = () => {
      const tmp = document.createElement("canvas");
      tmp.width = W; tmp.height = H;
      const tctx = tmp.getContext("2d");
      tctx.drawImage(ann, 0, 0, W, H);
      const id = tctx.getImageData(0, 0, W, H);
      const d = id.data;
      for (let i = 0; i < d.length; i += 4) {
        // Unpainted background is near-black -> make fully transparent.
        if (d[i] < 24 && d[i + 1] < 24 && d[i + 2] < 24) d[i + 3] = 0;
      }
      tctx.putImageData(id, 0, 0);
      pctx.drawImage(tmp, 0, 0);
      pushHistory();
      updateBrushCounts();
    };
    ann.src = stage.dataset.annotationUrl + "?t=" + Date.now();
  }

  // --- tool state -------------------------------------------------------
  // Default to the add-back brush: editing the tissue base is the common case,
  // and it matches the brush card marked active in the template.
  let tool = "green";
  let brush = 6;
  let drawing = false;
  let last = null;
  const history = [];

  // Tool buttons: the three region brushes and the eraser utility.
  const toolButtons = document.querySelectorAll("[data-tool]");
  toolButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      tool = btn.dataset.tool;
      toolButtons.forEach((b) => b.classList.toggle("active", b === btn));
    });
  });

  const sizeInput = document.getElementById("brush-size");
  sizeInput.addEventListener("input", () => {
    brush = parseInt(sizeInput.value, 10);
    document.getElementById("brush-size-val").textContent = brush;
  });
  document.getElementById("show-tissue").addEventListener("change", drawTissueTint);

  // --- drawing ----------------------------------------------------------
  function canvasPos(ev) {
    const r = cPaint.getBoundingClientRect();
    const x = (ev.clientX - r.left) * (W / r.width);
    const y = (ev.clientY - r.top) * (H / r.height);
    return { x, y };
  }

  function stroke(a, b) {
    pctx.lineWidth = brush;
    if (tool === "eraser") {
      pctx.globalCompositeOperation = "destination-out";
      pctx.strokeStyle = "rgba(0,0,0,1)";
    } else {
      pctx.globalCompositeOperation = "source-over";
      const [r, g, bl] = COLORS[tool];
      pctx.strokeStyle = `rgb(${r},${g},${bl})`;
    }
    pctx.beginPath();
    pctx.moveTo(a.x, a.y);
    pctx.lineTo(b.x, b.y);
    pctx.stroke();
  }

  cPaint.addEventListener("pointerdown", (ev) => {
    drawing = true;
    last = canvasPos(ev);
    stroke(last, last);
    cPaint.setPointerCapture(ev.pointerId);
  });
  cPaint.addEventListener("pointermove", (ev) => {
    if (!drawing) return;
    const p = canvasPos(ev);
    stroke(last, p);
    last = p;
  });
  function endStroke() {
    if (!drawing) return;
    drawing = false;
    pushHistory();
    updateBrushCounts();
    scheduleAutosave();
  }
  cPaint.addEventListener("pointerup", endStroke);
  cPaint.addEventListener("pointerleave", endStroke);

  // --- history / undo ---------------------------------------------------
  function pushHistory() {
    try {
      history.push(pctx.getImageData(0, 0, W, H));
      if (history.length > 25) history.shift();
    } catch (e) { /* ignore */ }
  }
  document.getElementById("undo-btn").addEventListener("click", () => {
    if (history.length < 2) {
      pctx.clearRect(0, 0, W, H);
      history.length = 0;
    } else {
      history.pop();
      pctx.putImageData(history[history.length - 1], 0, 0);
    }
    updateBrushCounts();
    scheduleAutosave();
  });
  document.getElementById("clear-btn").addEventListener("click", () => {
    pctx.clearRect(0, 0, W, H);
    history.length = 0;
    pushHistory();
    updateBrushCounts();
    scheduleAutosave();
  });

  // --- export: flatten to pure opaque colours ---------------------------
  // Precedence (high to low): cyan loop > red exclude > green add-back. A pixel
  // is assigned the highest-precedence channel that dominates, so AA edges snap
  // to one pure colour and never blend.
  function flattenToCanvas() {
    const src = pctx.getImageData(0, 0, W, H).data;
    const out = document.createElement("canvas");
    out.width = W; out.height = H;
    const octx = out.getContext("2d");
    const id = octx.createImageData(W, H);
    const d = id.data;
    const counts = { cyan: 0, green: 0, red: 0 };
    for (let i = 0; i < src.length; i += 4) {
      const r = src[i], g = src[i + 1], b = src[i + 2], a = src[i + 3];
      let cr = 0, cg = 0, cb = 0;
      if (a > 16) {
        const cyanScore = Math.min(g, b) - r;          // high for cyan
        const redScore = r - Math.max(g, b);            // high for red
        const greenScore = g - Math.max(r, b);          // high for green
        if (cyanScore > 40) { cr = 0; cg = 255; cb = 255; counts.cyan++; }
        else if (redScore > 40) { cr = 255; cg = 0; cb = 0; counts.red++; }
        else if (greenScore > 40) { cr = 0; cg = 255; cb = 0; counts.green++; }
      }
      d[i] = cr; d[i + 1] = cg; d[i + 2] = cb; d[i + 3] = 255;
    }
    octx.putImageData(id, 0, 0);
    return { canvas: out, counts };
  }

  function flattenedPNG() {
    const { canvas } = flattenToCanvas();
    return new Promise((res) => canvas.toBlob(res, "image/png"));
  }

  // Live tally of painted pixels per brush, so a missing layer is obvious.
  const brushTally = document.getElementById("brush-tally");
  function updateBrushCounts() {
    if (!brushTally) return;
    const { counts } = flattenToCanvas();
    brushTally.innerHTML =
      `<span class="t-green">add ${counts.green}</span> · ` +
      `<span class="t-red">excl ${counts.red}</span> · ` +
      `<span class="t-cyan">limit ${counts.cyan}</span>`;
  }

  // --- autosave + manual save ------------------------------------------
  let autosaveTimer = null;
  const saveState = document.getElementById("save-state");

  function scheduleAutosave() {
    if (autosaveTimer) clearTimeout(autosaveTimer);
    autosaveTimer = setTimeout(save, 1200);
  }

  async function save() {
    saveState.textContent = "saving…";
    const blob = await flattenedPNG();
    const fd = new FormData();
    fd.append("annotation", blob, "annotation.png");
    const resp = await fetch(saveUrl, { method: "POST", body: fd });
    saveState.textContent = resp.ok ? "saved ✓" : "save failed";
    return resp.ok;
  }

  const saveBtn = document.getElementById("save-btn");
  if (saveBtn) saveBtn.addEventListener("click", save);

  const computeBtn = document.getElementById("compute-btn");
  computeBtn.addEventListener("click", async () => {
    computeBtn.disabled = true;            // guard against double-clicks
    try {
      const ok = await save();
      if (!ok) { saveState.textContent = "save failed"; return; }
      saveState.textContent = "computing…";
      const resp = await fetch(computeUrl, { method: "POST" });
      if (!resp.ok) { saveState.textContent = "compute failed"; return; }
      const r = await resp.json();
      document.getElementById("roi-summary").textContent =
        `ROI: ${r.mode} · ${r.roi_px} px · roi/tis=${r.roi_over_tissue.toFixed(2)}` +
        (r.detail ? ` · ${r.detail}` : "");
      const link = document.getElementById("overlay-link");
      if (link && r.overlay_url) {
        link.href = r.overlay_url + "?t=" + Date.now();
        link.hidden = false;               // clear the `hidden` attribute
      }
      if (r.roi_url) showRoiPreview(r.roi_url);   // tint the computed ROI
      saveState.textContent = "done ✓";
      wizard.markComputed();
      wizard.go(2);                        // advance to the preview step
    } catch (e) {
      saveState.textContent = "error: " + e;
    } finally {
      computeBtn.disabled = false;         // never leave it stuck grey
    }
  });

  // --- wizard step navigation ------------------------------------------
  // Two steps: 1 Mark -> 2 Preview. Step 2 unlocks once an ROI is computed.
  // Bulk export happens on the dashboard, not here.
  const wizard = (function () {
    const root = document.getElementById("wizard");
    const steps = [...root.querySelectorAll(".step")];
    const indItems = [...document.querySelectorAll("#step-indicator li")];
    let computed = root.dataset.hasRoi === "1";

    function go(n) {
      if (n > 1 && !computed) return;            // preview needs a computed ROI
      steps.forEach((s) => { s.hidden = s.dataset.step !== String(n); });
      indItems.forEach((li) => {
        const s = Number(li.dataset.step);
        li.classList.toggle("active", s === n);
        li.classList.toggle("done", s < n);
        li.classList.toggle("locked", s > 1 && !computed);
      });
      // Show the ROI tint only on the preview step; hide it while editing.
      if (n === 1) hideRoiPreview();
    }
    function markComputed() { computed = true; }

    indItems.forEach((li) =>
      li.addEventListener("click", () => go(Number(li.dataset.step))));
    root.querySelectorAll("[data-goto]").forEach((b) =>
      b.addEventListener("click", () => go(Number(b.dataset.goto))));

    return { go, markComputed };
  })();

  // --- review status buttons -------------------------------------------
  const statusBadge = document.getElementById("status-badge");
  document.querySelectorAll(".status-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const status = btn.dataset.status;
      const resp = await fetch(`/slide/${slideId}/status`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ review_status: status }),
      });
      if (resp.ok) {
        document.querySelectorAll(".status-btn").forEach((b) =>
          b.classList.toggle("active", b === btn));
        if (statusBadge) {
          statusBadge.textContent = status;
          statusBadge.className = "badge badge-" + status;
        }
      }
    });
  });
})();
