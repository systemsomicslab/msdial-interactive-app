const state = {
  files: [],
  lipidQueries: [],
  jobId: null,
  tuningJobId: null,
  tuningResult: null,
  config: null,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}

function workflow() {
  return {
    files: state.files,
    ion_mode: $("#ionMode").value,
    target_omics: $("#targetOmics").value,
    ms1_data_type: $("#ms1Type").value,
    ms2_data_type: $("#ms2Type").value,
    number_of_threads: Number($("#numberOfThreads").value),
    minimum_peak_height: Number($("#minimumPeakHeight").value),
    mass_slice_width: Number($("#massSliceWidth").value),
    minimum_peak_width: Number($("#minimumPeakWidth").value),
    retention_time_begin: Number($("#rtBegin").value),
    retention_time_end: Number($("#rtEnd").value),
    ms1_tolerance: Number($("#ms1Tolerance").value),
    ms2_tolerance: Number($("#ms2Tolerance").value),
    alignment_rt_tolerance: Number($("#alignmentRtTolerance").value),
    alignment_ms1_tolerance: Number($("#alignmentMs1Tolerance").value),
    solvent: $("#solvent").value,
    console_path: $("#consolePath").value.trim(),
    template_path: $("#templatePath").value.trim(),
    output_root: $("#outputRoot").value.trim(),
    msp_path: $("#mspPath").value.trim(),
    lbm_path: $("#lbmPath").value.trim(),
    text_db_path: $("#textDbPath").value.trim(),
    msp_weighted_dot_product: Number($("#mspWeighted").value),
    msp_simple_dot_product: Number($("#mspSimple").value),
    msp_reverse_dot_product: Number($("#mspReverse").value),
    msp_matched_peaks_percentage: Number($("#mspMatchedPercentage").value),
    msp_minimum_spectrum_match: Number($("#mspMinimumSpectrumMatch").value),
    together_with_alignment: true,
    stage_inputs: $("#stageInputs").checked,
    selected_lipids: state.lipidQueries.filter((item) => item.selected),
  };
}

function setStatus(text) { $("#status").textContent = text; }

function renderVendorTips() {
  const panel = $("#vendorTips");
  const hasAgilent = state.files.some((file) => file.vendor === "Agilent");
  if (!hasAgilent) {
    panel.hidden = true;
    panel.innerHTML = "";
    return;
  }
  panel.hidden = false;
  panel.innerHTML = `
    <h2>Agilent .d support tip</h2>
    <p>Agilent reading depends on the vendor <code>BaseDataAccess.dll</code> files
    shipped with the selected MS-DIAL Console package.</p>
    <p>On Windows, the reader may also require
    <a href="https://support.microsoft.com/en-us/topic/update-for-visual-c-2013-and-visual-c-redistributable-package-5b2ac5ab-4139-8acc-08e2-9578ec9b2cf1"
       target="_blank" rel="noreferrer">Microsoft Visual C++ 2013 Redistributable Package x64</a>.
    The app diagnoses the DLL deployment first, then native runtime errors.</p>`;
}

function renderFiles() {
  const body = $("#filesTable tbody");
  body.innerHTML = "";
  state.files.forEach((file, index) => {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td><input data-key="file_name" value="${escapeHtml(file.file_name)}"></td>
      <td title="${escapeHtml(file.file_path)}">${escapeHtml(file.file_path)}</td>
      <td><select data-key="file_type">${options(["Sample","Blank","QC","Standard"], file.file_type)}</select></td>
      <td><input data-key="class_id" value="${escapeHtml(file.class_id)}"></td>
      <td><span class="format-badge">${escapeHtml(file.format || "Unknown")}</span></td>
      <td><select data-key="acquisition_type">${options(["DDA","SWATH","AIF"], file.acquisition_type)}</select></td>
      <td><input data-key="batch_order" type="number" value="${file.batch_order}"></td>
      <td><input data-key="analytical_order" type="number" value="${file.analytical_order}"></td>
      <td><input data-key="factor" type="number" step="any" value="${file.factor}"></td>
      <td><button class="quiet remove">Remove</button></td>`;
    row.querySelectorAll("[data-key]").forEach((element) => {
      element.addEventListener("change", () => {
        const key = element.dataset.key;
        file[key] = element.type === "number" ? Number(element.value) : element.value;
        refreshQuestion();
      });
    });
    row.querySelector(".remove").addEventListener("click", () => {
      state.files.splice(index, 1);
      state.files.forEach((item, order) => item.analytical_order = order + 1);
      renderFiles();
      refreshQuestion();
    });
    body.appendChild(row);
  });
  setStatus(`${state.files.length} analysis file(s)`);
  renderVendorTips();
  renderTuningFiles();
}

function options(values, selected) {
  return values.map((value) => `<option ${value === selected ? "selected" : ""}>${value}</option>`).join("");
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[char]);
}

function mergeFiles(files) {
  const wasEmpty = state.files.length === 0;
  for (const file of files) {
    if (!state.files.some((item) => item.file_path.toLowerCase() === file.file_path.toLowerCase())) {
      state.files.push(file);
    }
  }
  state.files.forEach((item, index) => item.analytical_order = index + 1);
  renderFiles();
  if (wasEmpty && state.files.length) applyRecommendedParameters();
  refreshQuestion();
}

async function addServerPaths(paths) {
  const result = await api("/api/files/expand", {
    method: "POST", body: JSON.stringify({ paths })
  });
  mergeFiles(result.files);
}

async function uploadRecords(records, roots) {
  setStatus(`Uploading ${records.length} file(s)...`);
  const { session, root } = await api("/api/upload-session", { method: "POST", body: "{}" });
  for (let index = 0; index < records.length; index++) {
    const { file, relativePath } = records[index];
    setStatus(`Uploading ${index + 1}/${records.length}: ${relativePath}`);
    const response = await fetch(`/api/uploads/${session}?path=${encodeURIComponent(relativePath)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/octet-stream" },
      body: file,
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Upload failed");
  }
  const separator = root.includes("\\") ? "\\" : "/";
  await addServerPaths(roots.map((relative) => `${root}${separator}${relative.replaceAll("/", separator)}`));
}

async function uploadDropped(dataTransfer) {
  const entries = [...dataTransfer.items]
    .map((item) => item.webkitGetAsEntry?.())
    .filter(Boolean);
  if (!entries.length) {
    const records = [...dataTransfer.files].map((file) => ({ file, relativePath: file.name }));
    return uploadRecords(records, records.map((item) => item.relativePath));
  }
  const records = [];
  for (const entry of entries) await collectEntryFiles(entry, entry.name, records);
  await uploadRecords(records, entries.map((entry) => entry.name));
}

async function collectEntryFiles(entry, relativePath, records) {
  if (entry.isFile) {
    const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
    records.push({ file, relativePath });
    return;
  }
  const reader = entry.createReader();
  while (true) {
    const children = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
    if (!children.length) break;
    for (const child of children) {
      await collectEntryFiles(child, `${relativePath}/${child.name}`, records);
    }
  }
}

async function uploadBrowserFolder(files) {
  const records = [...files].map((file) => ({
    file,
    relativePath: file.webkitRelativePath || file.name,
  }));
  const roots = [...new Set(records.map((item) => item.relativePath.split("/")[0]))];
  await uploadRecords(records, roots);
}

function renderLipids() {
  const ion = $("#ionMode").value;
  const filter = $("#lipidFilter").value.toLowerCase();
  const visible = state.lipidQueries.filter((item) =>
    item.ion_mode === ion &&
    `${item.lipid_class} ${item.adduct}`.toLowerCase().includes(filter)
  );
  $("#lipidCount").textContent =
    `${state.lipidQueries.filter((item) => item.selected && item.ion_mode === ion).length} selected / ${visible.length} shown`;
  $("#lipidList").innerHTML = visible.map((item) => {
    const index = state.lipidQueries.indexOf(item);
    return `<label class="lipid-item"><input type="checkbox" data-index="${index}" ${item.selected ? "checked" : ""}>
      <span>${escapeHtml(item.lipid_class)} ${escapeHtml(item.adduct)}</span></label>`;
  }).join("");
  $$("#lipidList input").forEach((input) => {
    input.addEventListener("change", () => {
      state.lipidQueries[Number(input.dataset.index)].selected = input.checked;
      renderLipids();
    });
  });
}

function renderTuningFiles() {
  const selected = $("#tuningFile").value;
  $("#tuningFile").innerHTML = state.files.length
    ? state.files.map((file) =>
      `<option value="${escapeHtml(file.file_path)}">${escapeHtml(file.file_name)} | ${escapeHtml(file.format || "Unknown")}</option>`
    ).join("")
    : `<option value="">Add analysis data first</option>`;
  if (state.files.some((file) => file.file_path === selected)) $("#tuningFile").value = selected;
  renderTuningFormat();
}

function selectedTuningFile() {
  return state.files.find((file) => file.file_path === $("#tuningFile").value);
}

function renderTuningFormat() {
  const file = selectedTuningFile();
  $("#tuningFormat").innerHTML = file
    ? `<strong>${escapeHtml(file.format)}</strong><br>
       Detected as ${escapeHtml(file.vendor)} / ${escapeHtml(file.instrument_family)}.
       Recommended: Minimum peak height ${file.minimum_peak_height}, Mass slice width ${file.mass_slice_width}.`
    : "No representative file selected.";
}

function applyRecommendedParameters() {
  const file = selectedTuningFile() || state.files[0];
  if (!file) return;
  $("#minimumPeakHeight").value = file.minimum_peak_height;
  $("#massSliceWidth").value = file.mass_slice_width;
  $("#tuningHeightNumber").value = file.minimum_peak_height;
  $("#tuningHeight").value = file.minimum_peak_height;
  updateTuningCounts();
}

function lowerBound(values, threshold) {
  let low = 0;
  let high = values.length;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (values[middle] < threshold) low = middle + 1;
    else high = middle;
  }
  return low;
}

function updateTuningCounts() {
  const result = state.tuningResult;
  if (!result) return;
  const height = Number($("#tuningHeightNumber").value);
  $("#peakPassCount").textContent = result.heights.length - lowerBound(result.heights, height);
  const thresholds = {
    weighted: Number($("#tuneWeighted").value),
    simple: Number($("#tuneSimple").value),
    reverse: Number($("#tuneReverse").value),
    matched_percentage: Number($("#tuneMatchedPercentage").value),
    matched_count: Number($("#tuneMinimumMatch").value),
  };
  const passing = result.msp_scores.filter((score) =>
    score.weighted >= thresholds.weighted &&
    score.simple >= thresholds.simple &&
    score.reverse >= thresholds.reverse &&
    score.matched_percentage >= thresholds.matched_percentage &&
    score.matched_count >= thresholds.matched_count
  ).length;
  $("#annotationPassCount").textContent = passing;
  $("#tuneWeightedValue").value = thresholds.weighted.toFixed(2);
  $("#tuneSimpleValue").value = thresholds.simple.toFixed(2);
  $("#tuneReverseValue").value = thresholds.reverse.toFixed(2);
  $("#tuneMatchedPercentageValue").value = thresholds.matched_percentage.toFixed(2);
  $("#tuneMinimumMatchValue").value = thresholds.matched_count.toFixed(0);
}

function renderTuningResult(result) {
  state.tuningResult = result;
  const maxHeight = result.heights.length ? result.heights[result.heights.length - 1] : 10000;
  const percentileIndex = Math.max(0, Math.ceil(result.heights.length * 0.99) - 1);
  const sliderMax = Math.max(100, Math.ceil(result.heights[percentileIndex] || maxHeight));
  $("#tuningHeight").max = sliderMax;
  const recommended = Number(selectedTuningFile()?.minimum_peak_height || 100);
  $("#tuningHeight").value = Math.min(recommended, sliderMax);
  $("#tuningHeightNumber").value = recommended;
  $("#tuningSummary").innerHTML = `
    <div class="metric"><strong>${result.peak_count}</strong><span>peaks at height 0</span></div>
    <div class="metric"><strong>${result.msp_candidate_count}</strong><span>MSP-scored peaks</span></div>`;
  updateTuningCounts();
}

async function pollTuningJob() {
  if (!state.tuningJobId) return;
  const job = await api(`/api/jobs/${state.tuningJobId}`);
  $("#tuningLog").textContent = job.logs.join("\n") || job.status;
  $("#tuningLog").scrollTop = $("#tuningLog").scrollHeight;
  setStatus(`Tuning job ${job.status}`);
  if (["queued", "running"].includes(job.status)) {
    setTimeout(pollTuningJob, 1000);
  } else if (job.status === "completed" && job.result) {
    renderTuningResult(job.result);
    $("#tuningLog").textContent += `\nLoaded ${job.result.mdpeak}`;
  } else if (job.error) {
    $("#tuningLog").textContent += `\n${job.error}`;
  }
}

async function refreshQuestion() {
  const result = await api("/api/next-question", {
    method: "POST",
    body: JSON.stringify({ workflow: workflow(), language: $("#language").value }),
  });
  $("#nextQuestion").textContent = result.question?.prompt || "Core settings are complete. Validate the workflow next.";
}

function renderIssues(issues, version = "") {
  $("#issues").innerHTML = (version ? `<div class="issue">Console version: ${escapeHtml(version)}</div>` : "") +
    (issues.length ? issues.map((issue) =>
      `<div class="issue ${issue.level}">${escapeHtml(issue.level.toUpperCase())}: ${escapeHtml(issue.message)}</div>`
    ).join("") : `<div class="issue">OK: Ready to run.</div>`);
}

async function pollJob() {
  if (!state.jobId) return;
  const job = await api(`/api/jobs/${state.jobId}`);
  $("#log").textContent = job.logs.join("\n") || job.status;
  $("#log").scrollTop = $("#log").scrollHeight;
  setStatus(`Job ${job.status}`);
  if (["queued", "running"].includes(job.status)) {
    setTimeout(pollJob, 1000);
  }
}

async function initialize() {
  state.config = await api("/api/config");
  $("#platformPill").textContent =
    `${navigator.platform} | ${state.config.knowledge_cards.ja} JA / ${state.config.knowledge_cards.en} EN cards`;
  $("#templatePath").value = state.config.default_template;
  $("#queriesPath").value = state.config.default_queries;
  state.lipidQueries = state.config.lipid_queries;
  renderLipids();
  renderFiles();
  refreshQuestion();
}

$("#tabs").addEventListener("click", (event) => {
  if (!event.target.dataset.tab) return;
  $$("#tabs button").forEach((button) => button.classList.toggle("active", button === event.target));
  $$(".tab").forEach((tab) => tab.classList.toggle("active", tab.id === `tab-${event.target.dataset.tab}`));
});

$("#pickFiles").addEventListener("click", async () => mergeFiles((await api("/api/dialog/files", { method: "POST", body: "{}" })).files));
$("#pickFolder").addEventListener("click", async () => mergeFiles((await api("/api/dialog/directory", { method: "POST", body: "{}" })).files));
$("#browserFolder").addEventListener("click", () => $("#browserFolderInput").click());
$("#browserFolderInput").addEventListener("change", (event) =>
  uploadBrowserFolder(event.target.files).catch((error) => setStatus(error.message)));
$("#addPath").addEventListener("click", async () => {
  if ($("#serverPath").value.trim()) await addServerPaths([$("#serverPath").value.trim()]);
});
$("#clearFiles").addEventListener("click", () => { state.files = []; renderFiles(); refreshQuestion(); });

const dropZone = $("#dropZone");
["dragenter", "dragover"].forEach((name) => dropZone.addEventListener(name, (event) => {
  event.preventDefault(); dropZone.classList.add("drag");
}));
["dragleave", "drop"].forEach((name) => dropZone.addEventListener(name, (event) => {
  event.preventDefault(); dropZone.classList.remove("drag");
}));
dropZone.addEventListener("drop", (event) => uploadDropped(event.dataTransfer).catch((error) => setStatus(error.message)));

$("#ionMode").addEventListener("change", () => { renderLipids(); refreshQuestion(); });
$("#targetOmics").addEventListener("change", refreshQuestion);
$("#lipidFilter").addEventListener("input", renderLipids);
$("#refreshQuestion").addEventListener("click", refreshQuestion);
$("#tuningFile").addEventListener("change", renderTuningFormat);
$("#applyRecommended").addEventListener("click", applyRecommendedParameters);
$("#runTuning").addEventListener("click", async () => {
  const file = selectedTuningFile();
  if (!file) throw new Error("Select a representative file.");
  $("#tuningLog").textContent = "Preparing diagnostic run...";
  const result = await api("/api/tuning/run", {
    method: "POST",
    body: JSON.stringify({ workflow: workflow(), file_path: file.file_path }),
  });
  state.tuningJobId = result.job_id;
  pollTuningJob();
});
$("#tuningHeight").addEventListener("input", () => {
  $("#tuningHeightNumber").value = $("#tuningHeight").value;
  updateTuningCounts();
});
$("#tuningHeightNumber").addEventListener("input", () => {
  $("#tuningHeight").value = Math.min(
    Number($("#tuningHeightNumber").value),
    Number($("#tuningHeight").max),
  );
  updateTuningCounts();
});
["tuneWeighted", "tuneSimple", "tuneReverse", "tuneMatchedPercentage", "tuneMinimumMatch"]
  .forEach((id) => $(`#${id}`).addEventListener("input", updateTuningCounts));
$("#applyTuning").addEventListener("click", () => {
  $("#minimumPeakHeight").value = $("#tuningHeightNumber").value;
  $("#mspWeighted").value = $("#tuneWeighted").value;
  $("#mspSimple").value = $("#tuneSimple").value;
  $("#mspReverse").value = $("#tuneReverse").value;
  $("#mspMatchedPercentage").value = $("#tuneMatchedPercentage").value;
  $("#mspMinimumSpectrumMatch").value = $("#tuneMinimumMatch").value;
  setStatus("Tuning thresholds applied to the workflow.");
});

$("#validate").addEventListener("click", async () => {
  const result = await api("/api/validate", { method: "POST", body: JSON.stringify({ workflow: workflow() }) });
  renderIssues(result.issues, result.console_version);
});
$("#prepare").addEventListener("click", async () => {
  const result = await api("/api/prepare", { method: "POST", body: JSON.stringify({ workflow: workflow() }) });
  $("#runPath").textContent = result.preparation.run_directory;
  $("#log").textContent = [...result.messages, JSON.stringify(result.preparation.command)].join("\n");
});
$("#run").addEventListener("click", async () => {
  const result = await api("/api/run", { method: "POST", body: JSON.stringify({ workflow: workflow() }) });
  state.jobId = result.job_id;
  $("#runPath").textContent = result.preparation.run_directory;
  pollJob();
});
$("#ask").addEventListener("click", async () => {
  $("#answer").textContent = "Searching...";
  const result = await api("/api/assistant", {
    method: "POST",
    body: JSON.stringify({ query: $("#question").value, language: $("#language").value, workflow: workflow() }),
  });
  $("#answer").textContent = result.answer;
  $("#cards").innerHTML = result.cards.map((card) =>
    `<article class="card"><strong>${escapeHtml(card.question)}</strong>
      <div>${escapeHtml(card.answer)}</div>
      <div class="muted">${escapeHtml(card.feature || "")} | score ${card.score}</div></article>`
  ).join("");
});

initialize().catch((error) => { setStatus(error.message); console.error(error); });
