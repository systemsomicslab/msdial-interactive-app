const state = {
  files: [],
  lipidQueries: [],
  jobId: null,
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
    stage_inputs: $("#stageInputs").checked,
    selected_lipids: state.lipidQueries.filter((item) => item.selected),
  };
}

function setStatus(text) { $("#status").textContent = text; }

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
  for (const file of files) {
    if (!state.files.some((item) => item.file_path.toLowerCase() === file.file_path.toLowerCase())) {
      state.files.push(file);
    }
  }
  state.files.forEach((item, index) => item.analytical_order = index + 1);
  renderFiles();
  refreshQuestion();
}

async function addServerPaths(paths) {
  const result = await api("/api/files/expand", {
    method: "POST", body: JSON.stringify({ paths })
  });
  mergeFiles(result.files);
}

async function uploadDropped(files) {
  setStatus(`Uploading ${files.length} file(s)...`);
  const { session } = await api("/api/upload-session", { method: "POST", body: "{}" });
  const paths = [];
  for (let index = 0; index < files.length; index++) {
    const file = files[index];
    setStatus(`Uploading ${index + 1}/${files.length}: ${file.name}`);
    const response = await fetch(`/api/uploads/${session}/${encodeURIComponent(file.name)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/octet-stream" },
      body: file,
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Upload failed");
    paths.push(result.path);
  }
  await addServerPaths(paths);
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
dropZone.addEventListener("drop", (event) => uploadDropped([...event.dataTransfer.files]).catch((error) => setStatus(error.message)));

$("#ionMode").addEventListener("change", () => { renderLipids(); refreshQuestion(); });
$("#targetOmics").addEventListener("change", refreshQuestion);
$("#lipidFilter").addEventListener("input", renderLipids);
$("#refreshQuestion").addEventListener("click", refreshQuestion);

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
