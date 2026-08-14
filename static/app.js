const state = {
  files: [],
  lipidQueries: [],
  adducts: { Positive: [], Negative: [] },
  jobId: null,
  tuningJobId: null,
  tuningResult: null,
  rtCorrectionJobId: null,
  rtCorrectionResult: null,
  rtCorrectionAnchors: [],
  rtCorrectionAnchorsDirty: false,
  rtCorrectionAnchorSourcePath: "",
  analysisCsvSource: "",
  config: null,
  outputRootAutomatic: true,
  gcmsRiMap: {},
  mspAnnotators: [],
  textAnnotators: [],
  lbmAnnotator: {},
  libraryJobs: {},
  libraryProvenance: [],
  consoleDiscovery: null,
  jobs: [],
  mztabFiles: [],
  selectedMzTabPath: "",
  selectedMzTabScope: "",
  qaReport: null,
  qaSourceJobId: "",
  pathPicker: {
    mode: "vendor",
    currentPath: "",
    parent: "",
    roots: [],
    entries: [],
  },
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const DEFAULT_MSP_CUTOFFS = {
  weighted_dot_product_cutoff: 0.6,
  simple_dot_product_cutoff: 0.6,
  reverse_dot_product_cutoff: 0.8,
  matched_peaks_percentage_cutoff: 0.1,
  minimum_spectrum_match: 3,
};
const DEFAULT_TEXT_SETTINGS = {
  rt_tolerance: 0.5,
  ms1_tolerance: 0.01,
  total_score_cutoff: 0.8,
};
const DEFAULT_LBM_SETTINGS = {
  rt_tolerance: 100,
  ms1_tolerance: 0.01,
  ms2_tolerance: 0.025,
  weighted_dot_product_cutoff: 0.15,
  simple_dot_product_cutoff: 0.15,
  reverse_dot_product_cutoff: 0.3,
  matched_peaks_percentage_cutoff: 0,
  minimum_spectrum_match: 1,
  use_rt_scoring: false,
  use_rt_filtering: false,
};
const RT_WORKSPACE_STORAGE_KEY = "msdialInteractive.rtCorrectionWorkspace.v1";

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const text = await response.text();
  let result;
  try {
    result = text ? JSON.parse(text) : {};
  } catch {
    throw new Error(`The local app returned an invalid response (HTTP ${response.status}).`);
  }
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}

function workflow() {
  return {
    files: state.files,
    project_type: $("#projectType").value,
    ion_mode: $("#ionMode").value,
    target_omics: $("#targetOmics").value,
    ms1_data_type: $("#ms1Type").value,
    ms2_data_type: $("#ms2Type").value,
    number_of_threads: Number($("#numberOfThreads").value),
    smoothing_method: $("#smoothingMethod").value,
    minimum_peak_height: Number($("#minimumPeakHeight").value),
    mass_slice_width: Number($("#massSliceWidth").value),
    minimum_peak_width: Number($("#minimumPeakWidth").value),
    retention_time_begin: Number($("#rtBegin").value),
    retention_time_end: Number($("#rtEnd").value),
    ms1_tolerance: Number($("#ms1Tolerance").value),
    ms2_tolerance: Number($("#ms2Tolerance").value),
    alignment_rt_tolerance: Number($("#alignmentRtTolerance").value),
    alignment_ms1_tolerance: Number($("#alignmentMs1Tolerance").value),
    alignment_light_mode: Boolean($("#alignmentLightMode")?.checked),
    run_qa: $("#projectType").value === "lcms" && Boolean($("#heightMatrixExport")?.checked),
    height_matrix_export: $("#projectType").value === "lcms" && Boolean($("#heightMatrixExport")?.checked),
    export_folder_path: $("#projectType").value === "lcms" && $("#heightMatrixExport")?.checked
      ? $("#outputRoot").value.trim()
      : "",
    execute_rt_correction: Boolean($("#executeRtCorrection")?.checked),
    rt_correction_anchor_path: $("#rtCorrectionAnchorPath")?.value.trim() || "",
    rt_correction_anchor_source_path: state.rtCorrectionAnchorSourcePath || "",
    rt_correction_selection_path: $("#rtCorrectionSelectionPath")?.value.trim() || "",
    rt_correction_diff_method: $("#rtCorrectionDiffMethod")?.value || "SampleMinusSampleAverage",
    rt_correction_smooth_rt_diff: Boolean($("#rtCorrectionSmoothDiff")?.checked),
    rt_correction_intercept: Number($("#rtCorrectionIntercept")?.value || 0),
    rt_correction_extrapolation_begin: $("#rtCorrectionExtrapolationBegin")?.value || "UserSetting",
    rt_correction_extrapolation_end: $("#rtCorrectionExtrapolationEnd")?.value || "LastPoint",
    rt_correction_peak_selection_mode: $("#rtCorrectionPeakSelectionMode")?.value || "HighestIntensity",
    rt_correction_peak_selection_rt_weight: Number($("#rtCorrectionPeakSelectionRtWeight")?.value || 0.5),
    solvent: $("#solvent").value,
    console_path: $("#consolePath").value.trim(),
    template_path: $("#templatePath").value.trim(),
    output_root: $("#outputRoot").value.trim(),
    msp_path: "",
    lbm_path: (state.lbmAnnotator.lbm_file_path || "").trim(),
    lbm_rt_tolerance: Number(state.lbmAnnotator.rt_tolerance ?? DEFAULT_LBM_SETTINGS.rt_tolerance),
    lbm_ms1_tolerance: Number(state.lbmAnnotator.ms1_tolerance ?? DEFAULT_LBM_SETTINGS.ms1_tolerance),
    lbm_ms2_tolerance: Number(state.lbmAnnotator.ms2_tolerance ?? DEFAULT_LBM_SETTINGS.ms2_tolerance),
    lbm_weighted_dot_product: Number(state.lbmAnnotator.weighted_dot_product_cutoff ?? DEFAULT_LBM_SETTINGS.weighted_dot_product_cutoff),
    lbm_simple_dot_product: Number(state.lbmAnnotator.simple_dot_product_cutoff ?? DEFAULT_LBM_SETTINGS.simple_dot_product_cutoff),
    lbm_reverse_dot_product: Number(state.lbmAnnotator.reverse_dot_product_cutoff ?? DEFAULT_LBM_SETTINGS.reverse_dot_product_cutoff),
    lbm_matched_peaks_percentage: Number(state.lbmAnnotator.matched_peaks_percentage_cutoff ?? DEFAULT_LBM_SETTINGS.matched_peaks_percentage_cutoff),
    lbm_minimum_spectrum_match: Number(state.lbmAnnotator.minimum_spectrum_match ?? DEFAULT_LBM_SETTINGS.minimum_spectrum_match),
    lbm_use_rt_scoring: Boolean(state.lbmAnnotator.use_rt_scoring),
    lbm_use_rt_filtering: Boolean(state.lbmAnnotator.use_rt_filtering),
    text_db_path: "",
    msp_weighted_dot_product: state.mspAnnotators[0]?.weighted_dot_product_cutoff ?? DEFAULT_MSP_CUTOFFS.weighted_dot_product_cutoff,
    msp_simple_dot_product: state.mspAnnotators[0]?.simple_dot_product_cutoff ?? DEFAULT_MSP_CUTOFFS.simple_dot_product_cutoff,
    msp_reverse_dot_product: state.mspAnnotators[0]?.reverse_dot_product_cutoff ?? DEFAULT_MSP_CUTOFFS.reverse_dot_product_cutoff,
    msp_matched_peaks_percentage: state.mspAnnotators[0]?.matched_peaks_percentage_cutoff ?? DEFAULT_MSP_CUTOFFS.matched_peaks_percentage_cutoff,
    msp_minimum_spectrum_match: state.mspAnnotators[0]?.minimum_spectrum_match ?? DEFAULT_MSP_CUTOFFS.minimum_spectrum_match,
    msp_annotators: state.mspAnnotators
      .filter((item) => (item.msp_file_path || "").trim())
      .map((item) => ({
        annotator_id: (item.annotator_id || "").trim(),
        msp_file_path: (item.msp_file_path || "").trim(),
        priority: Number(item.priority || 1),
        rt_tolerance: Number(item.rt_tolerance || 0),
        use_rt_scoring: Boolean(item.use_rt_scoring),
        use_rt_filtering: Boolean(item.use_rt_filtering),
        weighted_dot_product_cutoff: Number(item.weighted_dot_product_cutoff ?? DEFAULT_MSP_CUTOFFS.weighted_dot_product_cutoff),
        simple_dot_product_cutoff: Number(item.simple_dot_product_cutoff ?? DEFAULT_MSP_CUTOFFS.simple_dot_product_cutoff),
        reverse_dot_product_cutoff: Number(item.reverse_dot_product_cutoff ?? DEFAULT_MSP_CUTOFFS.reverse_dot_product_cutoff),
        matched_peaks_percentage_cutoff: Number(item.matched_peaks_percentage_cutoff ?? DEFAULT_MSP_CUTOFFS.matched_peaks_percentage_cutoff),
        minimum_spectrum_match: Number(item.minimum_spectrum_match ?? DEFAULT_MSP_CUTOFFS.minimum_spectrum_match),
      })),
    text_annotators: state.textAnnotators
      .filter((item) => (item.text_db_file_path || "").trim())
      .map((item) => ({
        annotator_id: (item.annotator_id || "").trim(),
        text_db_file_path: (item.text_db_file_path || "").trim(),
        priority: Number(item.priority || 1),
        rt_tolerance: Number(item.rt_tolerance ?? DEFAULT_TEXT_SETTINGS.rt_tolerance),
        ms1_tolerance: Number(item.ms1_tolerance ?? DEFAULT_TEXT_SETTINGS.ms1_tolerance),
        total_score_cutoff: Number(item.total_score_cutoff ?? DEFAULT_TEXT_SETTINGS.total_score_cutoff),
        use_rt_scoring: Boolean(item.use_rt_scoring),
        use_rt_filtering: Boolean(item.use_rt_filtering),
      })),
    library_provenance: state.libraryProvenance,
    gcms_accuracy_type: $("#gcmsAccuracyType").value,
    gcms_ri_compound_type: $("#gcmsRiCompoundType").value,
    gcms_retention_type: $("#gcmsRetentionType").value,
    gcms_alignment_index_type: $("#gcmsAlignmentIndexType").value,
    gcms_ri_alignment_tolerance: Number($("#gcmsRiAlignmentTolerance").value),
    gcms_ri_source: $("#gcmsRiSource").value,
    gcms_ri_standard_path: $("#gcmsRiStandardPath").value.trim(),
    gcms_ri_dictionary_path: $("#gcmsRiDictionaryPath").value.trim(),
    gcms_ri_file_map: state.files.map((file) => ({
      file_path: file.file_path,
      file_name: file.file_name,
      ri_path: state.gcmsRiMap[file.file_path] || "",
    })),
    together_with_alignment: true,
    stage_inputs: false,
    selected_lipids: state.lipidQueries.filter((item) => item.selected),
    selected_adducts: (state.adducts[$("#ionMode").value] || [])
      .filter((item) => item.selected)
      .map((item) => item.adduct),
    msdial_interactive_version: state.config?.app_version || "not recorded",
  };
}

function llmConfig() {
  return {
    provider: $("#llmProvider").value,
    endpoint: $("#llmEndpoint").value.trim(),
    deployment: $("#llmDeployment").value.trim(),
    api_key: $("#llmApiKey").value,
    api_version: $("#llmApiVersion").value.trim(),
  };
}

function setStatus(text) { $("#status").textContent = text; }

function showImportMessages(messages, level = "warning", useAlert = false) {
  const panel = $("#importMessages");
  const unique = [...new Set((messages || []).filter(Boolean))];
  if (!unique.length) {
    panel.hidden = true;
    panel.textContent = "";
    panel.classList.remove("error");
    return;
  }
  panel.hidden = false;
  panel.classList.toggle("error", level === "error");
  panel.textContent = unique.join("\n");
  setStatus(unique[0]);
  if (useAlert) window.alert(unique.join("\n"));
}

async function runUiAction(action) {
  try {
    await action();
  } catch (error) {
    showImportMessages([error.message || String(error)], "error");
  }
}

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
      if (state.outputRootAutomatic) setOutputRootFromFirstFile();
      renderFiles();
      refreshQuestion();
    });
    body.appendChild(row);
  });
  setStatus(`${state.files.length} analysis file(s)`);
  renderVendorTips();
  renderTuningFiles();
  renderGcmsRiMap();
}

function renderRtCorrectionAnchors() {
  const rows = state.rtCorrectionAnchors || [];
  $("#rtCorrectionAnchorEditor").hidden = !rows.length;
  updateRtCorrectionAnchorCount();
  $("#rtCorrectionAnchorRows").innerHTML = rows.map((row, index) => `
    <tr data-index="${index}">
      <td><input data-key="include" type="checkbox" ${row.include ? "checked" : ""}></td>
      <td><input data-key="name" value="${escapeHtml(row.name)}"></td>
      <td><input data-key="rt" type="number" step="any" min="0" value="${Number(row.rt)}"></td>
      <td><input data-key="rt_tolerance" type="number" step="any" min="0" value="${Number(row.rt_tolerance)}"></td>
      <td><input data-key="mz" type="number" step="any" min="0" value="${Number(row.mz)}"></td>
      <td><input data-key="mz_tolerance" type="number" step="any" min="0" value="${Number(row.mz_tolerance)}"></td>
      <td><input data-key="minimum_height" type="number" step="any" min="0" value="${Number(row.minimum_height)}"></td>
    </tr>`).join("");
  $$("#rtCorrectionAnchorRows tr").forEach((tableRow) => {
    const row = rows[Number(tableRow.dataset.index)];
    tableRow.querySelectorAll("[data-key]").forEach((element) => {
      element.addEventListener("input", () => {
        const key = element.dataset.key;
        row[key] = element.type === "checkbox"
          ? element.checked
          : element.type === "number"
            ? Number(element.value)
            : element.value;
        state.rtCorrectionAnchorsDirty = true;
        updateRtCorrectionAnchorCount();
      });
    });
  });
}

function updateRtCorrectionAnchorCount() {
  const rows = state.rtCorrectionAnchors || [];
  const dirty = state.rtCorrectionAnchorsDirty ? " | Unsaved changes" : "";
  $("#rtCorrectionAnchorCount").textContent =
    `${rows.filter((row) => row.include).length} / ${rows.length} anchors enabled${dirty}`;
}

async function loadRtCorrectionAnchors(path = $("#rtCorrectionAnchorPath").value.trim()) {
  if (!path) throw new Error("Select an RT correction anchor library.");
  const result = await api("/api/rt-correction/anchors/load", {
    method: "POST",
    body: JSON.stringify({ path }),
  });
  $("#rtCorrectionAnchorPath").value = result.path;
  state.rtCorrectionAnchorSourcePath = result.path;
  state.rtCorrectionAnchors = result.rows || [];
  state.rtCorrectionAnchorsDirty = false;
  state.rtCorrectionResult = null;
  $("#rtCorrectionReview").hidden = true;
  renderRtCorrectionAnchors();
  saveRtWorkspaceState();
  setStatus(`Loaded ${state.rtCorrectionAnchors.length} RT correction anchors.`);
}

async function saveEditedRtCorrectionAnchors() {
  if (!state.rtCorrectionAnchors.length) {
    throw new Error("Load an RT correction anchor library before saving.");
  }
  if (!$("#outputRoot").value.trim()) setOutputRootFromFirstFile();
  if (!$("#outputRoot").value.trim()) {
    throw new Error("Set Output root before saving the edited anchor library.");
  }
  const result = await api("/api/rt-correction/anchors/save", {
    method: "POST",
    body: JSON.stringify({ workflow: workflow(), rows: state.rtCorrectionAnchors }),
  });
  $("#rtCorrectionAnchorPath").value = result.anchor_file;
  state.rtCorrectionAnchorsDirty = false;
  updateRtCorrectionAnchorCount();
  saveRtWorkspaceState();
  setStatus(`Saved edited RT correction anchors: ${result.anchor_file}`);
  return result.anchor_file;
}

function options(values, selected) {
  return values.map((value) => `<option ${value === selected ? "selected" : ""}>${value}</option>`).join("");
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[char]);
}

function sciexDescriptor(file) {
  const match = String(file.file_path || "").match(/^(.*)\.(wiff2|wiff)$/i);
  return match ? { sample: match[1].toLowerCase(), extension: match[2].toLowerCase() } : null;
}

function mergeFiles(files, messages = []) {
  const wasEmpty = state.files.length === 0;
  const combined = [...state.files, ...files];
  const sciexBySample = new Map();
  combined.forEach((file) => {
    const descriptor = sciexDescriptor(file);
    if (!descriptor) return;
    if (!sciexBySample.has(descriptor.sample)) sciexBySample.set(descriptor.sample, new Set());
    sciexBySample.get(descriptor.sample).add(descriptor.extension);
  });
  const conflicts = new Set(
    [...sciexBySample.entries()]
      .filter(([, extensions]) => extensions.has("wiff") && extensions.has("wiff2"))
      .map(([sample]) => sample),
  );
  const conflictMessages = [...conflicts].map((sample) =>
    `Both .wiff and .wiff2 were supplied for '${sample}'. Choose exactly one; neither new file was added.`
  );
  for (const file of files) {
    const descriptor = sciexDescriptor(file);
    if (descriptor && conflicts.has(descriptor.sample)) continue;
    if (!state.files.some((item) => item.file_path.toLowerCase() === file.file_path.toLowerCase())) {
      state.files.push(file);
    }
  }
  state.files.forEach((item, index) => item.analytical_order = index + 1);
  renderFiles();
  if (wasEmpty && state.files.length) {
    applyFormatStartingValues();
    if (state.outputRootAutomatic) setOutputRootFromFirstFile();
  }
  refreshQuestion().catch((error) => showImportMessages([error.message || String(error)], "error"));
  const allMessages = [...messages, ...conflictMessages];
  showImportMessages(allMessages, conflictMessages.length ? "error" : "warning", conflictMessages.length > 0);
}

async function addServerPaths(paths) {
  const result = await api("/api/files/expand", {
    method: "POST", body: JSON.stringify({ paths })
  });
  const rejected = (result.rejected || []).map((path) =>
    `Rejected unsupported analysis input: ${path}. Use .wiff or .wiff2 for SCIEX data.`
  );
  mergeFiles(result.files || [], [...(result.warnings || []), ...rejected]);
}

function pathPickerStartPath() {
  if (state.pathPicker.mode === "mztab") {
    return state.selectedMzTabPath
      ? parentDirectory(state.selectedMzTabPath)
      : $("#runPath").textContent.trim()
        || $("#outputRoot").value.trim()
        || "";
  }
  return $("#serverPath").value.trim()
    || $("#outputRoot").value.trim()
    || (state.files[0] ? parentDirectory(state.files[0].file_path) : "");
}

function pathPickerCanSelect(entry) {
  if (state.pathPicker.mode === "vendor") return entry.is_vendor_folder;
  if (state.pathPicker.mode === "mztab") {
    const name = String(entry.name || "").toLowerCase();
    return entry.is_file && (
      [".mztab", ".mztabm"].includes(String(entry.suffix || "").toLowerCase())
      || name.endsWith(".mztab.txt")
      || name.includes("mztab")
    );
  }
  return entry.is_supported;
}

async function browseLocalPath(path = "") {
  const result = await api("/api/files/browse", {
    method: "POST",
    body: JSON.stringify({ path }),
  });
  state.pathPicker.currentPath = result.path;
  state.pathPicker.parent = result.parent || "";
  state.pathPicker.roots = result.roots || [];
  state.pathPicker.entries = result.entries || [];
  renderPathPicker();
}

async function openPathPicker(mode) {
  state.pathPicker.mode = mode;
  $("#pathPickerModal").hidden = false;
  $("#pathPickerTitle").textContent =
    mode === "vendor"
      ? "Add vendor folders (.d/.raw)"
      : mode === "mztab"
        ? "Select mzTab-M file"
        : "Add local folder or supported files";
  $("#pathPickerHelp").textContent =
    mode === "vendor"
      ? "Select one or more Agilent/Bruker .d folders or Waters .raw folders. Open ordinary folders to navigate."
      : mode === "mztab"
        ? "Select one mzTab-M output file. Open folders to navigate. The newest mzTab-M in the output folder is selected by default when you refresh the list."
        : "Select supported raw files/folders, or add the current folder to import its immediate supported children.";
  await browseLocalPath(pathPickerStartPath());
}

function closePathPicker() {
  $("#pathPickerModal").hidden = true;
}

function renderPathPicker() {
  $("#pathPickerCurrent").textContent = state.pathPicker.currentPath;
  $("#pathPickerPath").value = state.pathPicker.currentPath;
  $("#pathPickerUp").disabled = !state.pathPicker.parent;
  $("#pathPickerRoot").innerHTML = state.pathPicker.roots
    .map((root) => `<option value="${escapeHtml(root.path)}">${escapeHtml(root.label)}</option>`)
    .join("");
  const matchingRoot = state.pathPicker.roots.find((root) =>
    state.pathPicker.currentPath.toLowerCase().startsWith(root.path.toLowerCase())
  );
  if (matchingRoot) $("#pathPickerRoot").value = matchingRoot.path;
  const rows = state.pathPicker.entries.map((entry, index) => {
    const canSelect = pathPickerCanSelect(entry);
    const canOpen = entry.is_dir && !entry.is_vendor_folder;
    const typeLabel = state.pathPicker.mode === "mztab" && canSelect
      ? "mzTab-M"
      : entry.is_supported
      ? entry.format
      : entry.is_dir
      ? "Folder"
      : entry.suffix || "File";
    return `<div class="path-picker-row ${canSelect ? "" : "unsupported"}">
      <input type="checkbox" data-index="${index}" ${canSelect ? "" : "disabled"}>
      <div class="path-picker-icon">${entry.is_dir ? "DIR" : "FILE"}</div>
      <div>
        <div class="path-picker-entry-name">${escapeHtml(entry.name)}</div>
        <div class="path-picker-entry-path">${escapeHtml(entry.path)}</div>
      </div>
      <div class="button-row">
        <span class="muted">${escapeHtml(typeLabel)}</span>
        ${canOpen ? `<button class="quiet path-open" type="button" data-index="${index}">Open</button>` : ""}
      </div>
    </div>`;
  }).join("");
  $("#pathPickerEntries").innerHTML = rows || `<div class="path-picker-row unsupported">No entries.</div>`;
  $$(".path-open").forEach((button) => {
    button.addEventListener("click", () => {
      const entry = state.pathPicker.entries[Number(button.dataset.index)];
      if (entry) browseLocalPath(entry.path).catch((error) => showImportMessages([error.message], "error"));
    });
  });
}

async function addSelectedPathPickerEntries() {
  const paths = $$("#pathPickerEntries input[type='checkbox']:checked")
    .map((input) => state.pathPicker.entries[Number(input.dataset.index)]?.path)
    .filter(Boolean);
  if (!paths.length) {
    showImportMessages(["Select at least one supported entry in the local path browser."], "warning");
    return;
  }
  if (state.pathPicker.mode === "mztab") {
    setSelectedMzTabPath(paths[0]);
    closePathPicker();
    setStatus(`Selected mzTab-M: ${paths[0]}`);
    return;
  }
  await addServerPaths(paths);
  closePathPicker();
}

function parentDirectory(path) {
  const normalized = String(path || "").replace(/[\\/]+$/, "");
  const separatorIndex = Math.max(normalized.lastIndexOf("/"), normalized.lastIndexOf("\\"));
  if (separatorIndex < 0) return "";
  if (separatorIndex === 2 && /^[a-zA-Z]:/.test(normalized)) {
    return normalized.slice(0, 3);
  }
  return separatorIndex === 0 ? normalized[0] : normalized.slice(0, separatorIndex);
}

function setOutputRootFromFirstFile() {
  $("#outputRoot").value = state.files.length
    ? parentDirectory(state.files[0].file_path)
    : "";
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

function renderAdducts() {
  const ionMode = $("#ionMode").value;
  const filter = $("#adductFilter").value.toLowerCase();
  const adducts = state.adducts[ionMode] || [];
  const visible = adducts.filter((item) =>
    item.adduct.toLowerCase().includes(filter)
  );
  $("#adductCount").textContent =
    `${adducts.filter((item) => item.selected).length} selected / ${visible.length} shown`;
  $("#adductList").innerHTML = visible.map((item) => {
    const index = adducts.indexOf(item);
    return `<label class="adduct-item">
      <input type="checkbox" data-index="${index}" ${item.selected ? "checked" : ""}>
      <span>${escapeHtml(item.adduct)} (z=${item.charge})</span>
    </label>`;
  }).join("");
  $$("#adductList input").forEach((input) => {
    input.addEventListener("change", () => {
      adducts[Number(input.dataset.index)].selected = input.checked;
      renderAdducts();
    });
  });
}

function updateGcmsRiUI() {
  const usesRi = $("#gcmsRetentionType").value === "RI" || $("#gcmsAlignmentIndexType").value === "RI";
  const source = $("#gcmsRiSource").value;
  $("#gcmsRiSource").closest("label").hidden = !usesRi;
  $("#gcmsRiStandardField").hidden = !usesRi || source !== "single";
  $("#gcmsRiDictionaryField").hidden = !usesRi || source !== "dictionary";
  $("#gcmsRiMapPanel").hidden = !usesRi || source !== "perFile";
  if (!usesRi) {
    $("#gcmsRiHelp").innerHTML = "RT-only mode does not require RI dictionary files.";
  } else if (source === "single") {
    $("#gcmsRiHelp").innerHTML =
      "The app will generate <code>ri_dictionary_paths.txt</code> in the run folder, assigning the same carbon-RT file to every analysis file.<br>"
      + "Carbon-RT file format: tab-delimited text with header <code>Num</code> and <code>RT(min)</code>, e.g. <code>10&lt;tab&gt;4.024</code>.";
  } else if (source === "perFile") {
    $("#gcmsRiHelp").innerHTML =
      "Enter one RI carbon-RT file for each analysis file. The app will generate the CUI dictionary for you.<br>"
      + "Generated dictionary format: <code>analysis_file_path&lt;tab&gt;ri_carbon_rt_file_path</code>.<br>"
      + "Carbon-RT file format: <code>Num&lt;tab&gt;RT(min)</code>, one carbon number and retention time per line.";
  } else {
    $("#gcmsRiHelp").innerHTML =
      "Existing dictionary format is tab-delimited text with no special quoting:<br>"
      + "<code>D:\\data\\sample1.abf&lt;tab&gt;D:\\data\\alkaneinfo.txt</code><br>"
      + "<code>D:\\data\\sample2.abf&lt;tab&gt;D:\\data\\alkaneinfo.txt</code><br>"
      + "Every imported analysis file must appear in the first column. The second column points to a carbon-RT file with header <code>Num</code> and <code>RT(min)</code>.";
  }
  renderGcmsRiMap();
}

function renderGcmsRiMap() {
  const table = $("#gcmsRiMapTable tbody");
  if (!table) return;
  table.innerHTML = "";
  state.files.forEach((file) => {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td title="${escapeHtml(file.file_path)}">${escapeHtml(file.file_name)}<br><span class="muted">${escapeHtml(file.file_path)}</span></td>
      <td><input data-file-path="${escapeHtml(file.file_path)}" value="${escapeHtml(state.gcmsRiMap[file.file_path] || "")}" placeholder="D:\\...\\alkaneinfo.txt"></td>`;
    row.querySelector("input").addEventListener("input", (event) => {
      state.gcmsRiMap[file.file_path] = event.target.value.trim();
    });
    table.appendChild(row);
  });
}

function defaultMspAnnotatorRow(overrides = {}) {
  const index = state.mspAnnotators.length + 1;
  return {
    annotator_id: `msp_annotator_${index}`,
    msp_file_path: "",
    priority: index,
    rt_tolerance: 0.5,
    use_rt_scoring: false,
    use_rt_filtering: false,
    ...DEFAULT_MSP_CUTOFFS,
    ...overrides,
  };
}

function defaultTextAnnotatorRow(overrides = {}) {
  const index = state.textAnnotators.length + 1;
  return {
    annotator_id: `text_annotator_${index}`,
    text_db_file_path: "",
    priority: index,
    ...DEFAULT_TEXT_SETTINGS,
    use_rt_scoring: false,
    use_rt_filtering: false,
    ...overrides,
  };
}

function defaultLbmAnnotator(overrides = {}) {
  return {
    lbm_file_path: "",
    ...DEFAULT_LBM_SETTINGS,
    ...overrides,
  };
}

function setTemplateControl(id, value, checkbox = false) {
  const control = $(`#${id}`);
  if (!control || value === undefined || value === null) return;
  if (checkbox) {
    control.checked = Boolean(value);
    return;
  }
  const text = String(value);
  if (control.tagName === "SELECT" && ![...control.options].some((item) => item.value === text)) return;
  control.value = text;
}

function applyLoadedParameterTemplate(result) {
  const values = result.workflow || {};
  const controls = {
    project_type: "projectType",
    ion_mode: "ionMode",
    target_omics: "targetOmics",
    ms1_data_type: "ms1Type",
    ms2_data_type: "ms2Type",
    number_of_threads: "numberOfThreads",
    smoothing_method: "smoothingMethod",
    minimum_peak_height: "minimumPeakHeight",
    mass_slice_width: "massSliceWidth",
    minimum_peak_width: "minimumPeakWidth",
    retention_time_begin: "rtBegin",
    retention_time_end: "rtEnd",
    ms1_tolerance: "ms1Tolerance",
    ms2_tolerance: "ms2Tolerance",
    alignment_rt_tolerance: "alignmentRtTolerance",
    alignment_ms1_tolerance: "alignmentMs1Tolerance",
    solvent: "solvent",
    gcms_accuracy_type: "gcmsAccuracyType",
    gcms_ri_compound_type: "gcmsRiCompoundType",
    gcms_retention_type: "gcmsRetentionType",
    gcms_alignment_index_type: "gcmsAlignmentIndexType",
    gcms_ri_alignment_tolerance: "gcmsRiAlignmentTolerance",
    gcms_ri_dictionary_path: "gcmsRiDictionaryPath",
  };
  Object.entries(controls).forEach(([key, id]) => setTemplateControl(id, values[key]));
  setTemplateControl("alignmentLightMode", values.alignment_light_mode, true);
  setTemplateControl("heightMatrixExport", values.height_matrix_export, true);
  if (result.path) $("#templatePath").value = result.path;
  if (Array.isArray(result.msp_annotators)) state.mspAnnotators = result.msp_annotators;
  if (Array.isArray(result.text_annotators)) state.textAnnotators = result.text_annotators;
  if (result.lbm_annotator) state.lbmAnnotator = result.lbm_annotator;
  if (Array.isArray(result.lipid_queries) && result.lipid_queries.length) {
    state.lipidQueries = result.lipid_queries;
  }
  if (Array.isArray(result.selected_adducts) && result.selected_adducts.length) {
    Object.values(state.adducts).flat().forEach((item) => {
      item.selected = result.selected_adducts.includes(item.adduct);
    });
  }
  if (values.gcms_ri_dictionary_path) $("#gcmsRiSource").value = "dictionary";
  renderLipids();
  renderAdducts();
  renderLbmAnnotator();
  renderMspAnnotators();
  renderTextAnnotators();
  updateProjectUI();
  renderConsoleDiscovery(state.consoleDiscovery);
  refreshQuestion();
}

function applyCatalogLibrary(item) {
  const path = item.local_path || "";
  if (!path) return;
  state.libraryProvenance = [
    ...state.libraryProvenance.filter((entry) => entry.catalog_id !== item.id),
    {
      catalog_id: item.id,
      label: item.label,
      record_url: item.record_url,
      doi: item.doi,
      local_path: path,
      filename: item.filename,
      md5: item.md5,
      license: item.license,
    },
  ];
  if (item.kind === "lbm") {
    $("#projectType").value = "lcms";
    $("#targetOmics").value = "Lipidomics";
    state.lbmAnnotator = { ...defaultLbmAnnotator(), ...state.lbmAnnotator, lbm_file_path: path };
    renderLbmAnnotator();
  } else {
    if (item.kind === "gcms_msp") {
      $("#projectType").value = "gcms";
      $("#targetOmics").value = "Metabolomics";
      if (item.ri_compound_type) $("#gcmsRiCompoundType").value = item.ri_compound_type;
    } else if (item.ion_mode) {
      $("#ionMode").value = item.ion_mode;
    }
    const existing = state.mspAnnotators[0] || defaultMspAnnotatorRow();
    state.mspAnnotators = [{ ...existing, annotator_id: existing.annotator_id || "msp_annotator_1", msp_file_path: path }, ...state.mspAnnotators.slice(1)];
    renderMspAnnotators();
  }
  updateProjectUI();
  renderLipids();
  renderAdducts();
  setStatus(`Using downloaded library: ${path}`);
}

function renderLibraryCatalog() {
  const catalog = state.config?.library_catalog || [];
  $("#libraryCatalogDirectory").innerHTML = `Library directory: <code>${escapeHtml(state.config?.library_directory || "")}</code>`;
  $("#libraryCatalogList").innerHTML = catalog.map((item) => {
    const job = state.libraryJobs[item.id];
    const status = job
      ? job.status === "failed" ? `Failed: ${escapeHtml(job.error || "download error")}` : `${escapeHtml(job.status)} ${escapeHtml(job.progress || 0)}%`
      : item.downloaded ? "Downloaded and checksum verified" : `${item.size_mb} MB download`;
    return `<article class="library-card" data-library-id="${escapeHtml(item.id)}">
      <div class="library-card-head">
        <div><strong>${escapeHtml(item.label)}</strong><span class="muted">${escapeHtml(item.scope)} | ${escapeHtml(item.license)}</span></div>
        <span class="library-progress">${status}</span>
      </div>
      ${item.local_path ? `<div class="muted"><code>${escapeHtml(item.local_path)}</code></div>` : ""}
      <div class="button-row">
        <button type="button" class="secondary library-action" ${job && !["completed", "failed"].includes(job.status) ? "disabled" : ""}>${item.downloaded ? "Use in workflow" : "Download and use"}</button>
        <a href="${escapeHtml(item.record_url)}" target="_blank" rel="noreferrer">Open Zenodo record</a>
      </div>
    </article>`;
  }).join("");
  $$(".library-card").forEach((card) => {
    card.querySelector(".library-action").addEventListener("click", () => runUiAction(async () => {
      const item = catalog.find((entry) => entry.id === card.dataset.libraryId);
      if (item.downloaded) {
        applyCatalogLibrary(item);
        return;
      }
      const result = await api("/api/libraries/download", {
        method: "POST",
        body: JSON.stringify({ catalog_id: item.id }),
      });
      state.libraryJobs[item.id] = { id: result.job_id, status: "queued", progress: 0 };
      renderLibraryCatalog();
      pollLibraryDownload(item.id, result.job_id).catch((error) => {
        state.libraryJobs[item.id] = { status: "failed", error: error.message };
        renderLibraryCatalog();
      });
    }));
  });
}

async function pollLibraryDownload(catalogId, jobId) {
  const job = await api(`/api/jobs/${jobId}`);
  state.libraryJobs[catalogId] = job;
  renderLibraryCatalog();
  if (["queued", "running"].includes(job.status)) {
    window.setTimeout(() => pollLibraryDownload(catalogId, jobId).catch((error) => {
      state.libraryJobs[catalogId] = { status: "failed", error: error.message };
      renderLibraryCatalog();
    }), 750);
    return;
  }
  if (job.status === "failed") throw new Error(job.error || "Library download failed.");
  const item = state.config.library_catalog.find((entry) => entry.id === catalogId);
  Object.assign(item, job.result, { downloaded: true });
  delete state.libraryJobs[catalogId];
  renderLibraryCatalog();
  applyCatalogLibrary(item);
}

function updateLbmAnnotator(key, value) {
  state.lbmAnnotator = { ...state.lbmAnnotator, [key]: value };
}

function renderLbmAnnotator() {
  const table = $("#lbmAnnotatorTable tbody");
  if (!table) return;
  const item = { ...defaultLbmAnnotator(), ...state.lbmAnnotator };
  state.lbmAnnotator = item;
  table.innerHTML = "";
  const row = document.createElement("tr");
  row.innerHTML = `
    <td><input data-key="lbm_file_path" value="${escapeHtml(item.lbm_file_path || "")}" placeholder="D:\\...\\lipid_library.lbm2"></td>
    <td><input data-key="rt_tolerance" type="number" step="any" value="${escapeHtml(item.rt_tolerance)}"></td>
    <td><input data-key="ms1_tolerance" type="number" step="any" value="${escapeHtml(item.ms1_tolerance)}"></td>
    <td><input data-key="ms2_tolerance" type="number" step="any" value="${escapeHtml(item.ms2_tolerance)}"></td>
    <td><input data-key="use_rt_scoring" type="checkbox" ${item.use_rt_scoring ? "checked" : ""}></td>
    <td><input data-key="use_rt_filtering" type="checkbox" ${item.use_rt_filtering ? "checked" : ""}></td>
    <td><input data-key="weighted_dot_product_cutoff" type="number" min="0" max="1" step="0.01" value="${escapeHtml(item.weighted_dot_product_cutoff)}"></td>
    <td><input data-key="simple_dot_product_cutoff" type="number" min="0" max="1" step="0.01" value="${escapeHtml(item.simple_dot_product_cutoff)}"></td>
    <td><input data-key="reverse_dot_product_cutoff" type="number" min="0" max="1" step="0.01" value="${escapeHtml(item.reverse_dot_product_cutoff)}"></td>
    <td><input data-key="matched_peaks_percentage_cutoff" type="number" min="0" max="1" step="0.01" value="${escapeHtml(item.matched_peaks_percentage_cutoff)}"></td>
    <td><input data-key="minimum_spectrum_match" type="number" min="0" step="1" value="${escapeHtml(item.minimum_spectrum_match)}"></td>`;
  row.querySelectorAll("input").forEach((input) => {
    input.addEventListener("input", () => {
      const value = input.type === "checkbox"
        ? input.checked
        : input.type === "number"
          ? Number(input.value)
          : input.value;
      updateLbmAnnotator(input.dataset.key, value);
    });
    input.addEventListener("change", () => {
      if (input.type === "checkbox") updateLbmAnnotator(input.dataset.key, input.checked);
    });
  });
  table.appendChild(row);
}

function updateMspAnnotator(index, key, value) {
  state.mspAnnotators[index] = { ...state.mspAnnotators[index], [key]: value };
}

function renderMspAnnotators() {
  const table = $("#mspAnnotatorTable tbody");
  if (!table) return;
  table.innerHTML = "";
  state.mspAnnotators.forEach((item, index) => {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td><input data-key="annotator_id" value="${escapeHtml(item.annotator_id || "")}" placeholder="msp_annotator_1"></td>
      <td><input data-key="msp_file_path" value="${escapeHtml(item.msp_file_path || "")}" placeholder="D:\\...\\library.msp"></td>
      <td><input data-key="priority" type="number" step="1" value="${escapeHtml(item.priority ?? index + 1)}"></td>
      <td><input data-key="rt_tolerance" type="number" step="any" value="${escapeHtml(item.rt_tolerance ?? 0.5)}"></td>
      <td><input data-key="use_rt_scoring" type="checkbox" ${item.use_rt_scoring ? "checked" : ""}></td>
      <td><input data-key="use_rt_filtering" type="checkbox" ${item.use_rt_filtering ? "checked" : ""}></td>
      <td><input data-key="weighted_dot_product_cutoff" type="number" min="0" max="1" step="0.01" value="${escapeHtml(item.weighted_dot_product_cutoff ?? 0.6)}"></td>
      <td><input data-key="simple_dot_product_cutoff" type="number" min="0" max="1" step="0.01" value="${escapeHtml(item.simple_dot_product_cutoff ?? 0.6)}"></td>
      <td><input data-key="reverse_dot_product_cutoff" type="number" min="0" max="1" step="0.01" value="${escapeHtml(item.reverse_dot_product_cutoff ?? 0.8)}"></td>
      <td><input data-key="matched_peaks_percentage_cutoff" type="number" min="0" max="1" step="0.01" value="${escapeHtml(item.matched_peaks_percentage_cutoff ?? 0.1)}"></td>
      <td><input data-key="minimum_spectrum_match" type="number" min="0" step="1" value="${escapeHtml(item.minimum_spectrum_match ?? 3)}"></td>
      <td><button type="button" class="quiet remove">Remove</button></td>`;
    row.querySelectorAll("input").forEach((input) => {
      input.addEventListener("input", () => {
        const key = input.dataset.key;
        const value = input.type === "checkbox"
          ? input.checked
          : input.type === "number"
            ? Number(input.value)
            : input.value;
        updateMspAnnotator(index, key, value);
      });
      input.addEventListener("change", () => {
        if (input.type === "checkbox") updateMspAnnotator(index, input.dataset.key, input.checked);
      });
    });
    row.querySelector(".remove").addEventListener("click", () => {
      state.mspAnnotators.splice(index, 1);
      renderMspAnnotators();
    });
    table.appendChild(row);
  });
}

function updateTextAnnotator(index, key, value) {
  state.textAnnotators[index] = { ...state.textAnnotators[index], [key]: value };
}

function renderTextAnnotators() {
  const table = $("#textAnnotatorTable tbody");
  if (!table) return;
  table.innerHTML = "";
  state.textAnnotators.forEach((item, index) => {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td><input data-key="annotator_id" value="${escapeHtml(item.annotator_id || "")}" placeholder="text_annotator_1"></td>
      <td><input data-key="text_db_file_path" value="${escapeHtml(item.text_db_file_path || "")}" placeholder="D:\\...\\internal_standards.txt"></td>
      <td><input data-key="priority" type="number" step="1" value="${escapeHtml(item.priority ?? index + 1)}"></td>
      <td><input data-key="rt_tolerance" type="number" step="any" value="${escapeHtml(item.rt_tolerance ?? DEFAULT_TEXT_SETTINGS.rt_tolerance)}"></td>
      <td><input data-key="ms1_tolerance" type="number" step="any" value="${escapeHtml(item.ms1_tolerance ?? DEFAULT_TEXT_SETTINGS.ms1_tolerance)}"></td>
      <td><input data-key="total_score_cutoff" type="number" min="0" max="1" step="0.01" value="${escapeHtml(item.total_score_cutoff ?? DEFAULT_TEXT_SETTINGS.total_score_cutoff)}"></td>
      <td><input data-key="use_rt_scoring" type="checkbox" ${item.use_rt_scoring ? "checked" : ""}></td>
      <td><input data-key="use_rt_filtering" type="checkbox" ${item.use_rt_filtering ? "checked" : ""}></td>
      <td><button type="button" class="quiet remove">Remove</button></td>`;
    row.querySelectorAll("input").forEach((input) => {
      input.addEventListener("input", () => {
        const key = input.dataset.key;
        const value = input.type === "checkbox"
          ? input.checked
          : input.type === "number"
            ? Number(input.value)
            : input.value;
        updateTextAnnotator(index, key, value);
      });
      input.addEventListener("change", () => {
        if (input.type === "checkbox") updateTextAnnotator(index, input.dataset.key, input.checked);
      });
    });
    row.querySelector(".remove").addEventListener("click", () => {
      state.textAnnotators.splice(index, 1);
      renderTextAnnotators();
    });
    table.appendChild(row);
  });
}

function maybeSwitchTemplateForProject(project) {
  const template = $("#templatePath").value.trim();
  const lcmsDefault = state.config?.default_template || "";
  const gcmsDefault = state.config?.default_gcms_template || "";
  if (!template || template === lcmsDefault || template === gcmsDefault) {
    $("#templatePath").value = project === "gcms" ? gcmsDefault : lcmsDefault;
  }
}

function updateProjectUI() {
  const project = $("#projectType").value;
  const isLcms = project === "lcms";
  const isGcms = project === "gcms";
  const hasChromatography = ["lcms", "lcimms", "gcms"].includes(project);
  if (isGcms) $("#targetOmics").value = "Metabolomics";
  if (isGcms) $("#ionMode").value = "Positive";
  $("#targetOmics").disabled = isGcms;
  const lipidomics = $("#targetOmics").value === "Lipidomics";
  $("#solvent").disabled = isGcms || !lipidomics;
  $("#solventField").title = lipidomics
    ? "Solvent type is used by lipidomics annotation."
    : "Solvent type is not used for Metabolomics.";
  const labels = {
    lcms: "LC-MS is executable in the current version.",
    gcms: "GC-MS is executable with EI MSP annotation and optional RT/RI retention-index settings.",
    dims: "DI-MS parameter mode is scaffolded; Console execution is not enabled yet.",
    lcimms: "LC-IM-MS parameter mode is scaffolded; mobility settings are not implemented yet.",
    imms: "IM-MS parameter mode is scaffolded; mobility settings are not implemented yet.",
    imaging: "Imaging-MS parameter mode is scaffolded; imaging import and ROI settings are not implemented yet.",
  };
  $("#projectSupport").textContent = labels[project];
  $("#gcmsAnnotationNote").hidden = !isGcms;
  $("#gcmsSettings").hidden = !isGcms;
  $("#adductPanel").hidden = isGcms;
  $("#lbmAnnotatorPanel").hidden = isGcms || !lipidomics;
  $("#lbmQueriesPanel").hidden = isGcms || !lipidomics;
  $("#multiMspPanel").hidden = !(isLcms || isGcms);
  $("#textAnnotatorPanel").hidden = !isLcms;
  $("#lipidQuerySection").hidden = isGcms || !lipidomics;
  $("#alignmentLightModeField").hidden = !isLcms;
  $("#lcmsQaExportField").hidden = !isLcms;
  const isRtWorkspace = location.pathname.startsWith("/rt-correction");
  $("#rtCorrectionSettings").hidden = !isLcms || !isRtWorkspace;
  $("#rtCorrectionLauncher").hidden = !isLcms || isRtWorkspace;
  if (!isLcms) $("#alignmentLightMode").checked = false;
  if (!isLcms) $("#executeRtCorrection").checked = false;
  $("#ionMode").closest("label").hidden = isGcms;
  $("#solventField").hidden = isGcms;
  ["rtBegin", "rtEnd", "alignmentRtTolerance"].forEach((id) => {
    $(`#${id}`).closest("label").hidden = !hasChromatography;
  });
  $("#runTuning").disabled = !(isLcms || isGcms);
  $("#tuningRequirements").textContent = isLcms
    ? "Required: LC-MS project, one imported analysis file, an existing MS-DIAL Console path, parameter template, and output folder. WIFF import accepts the primary file alone, but SCIEX processing requires its adjacent WIFF.SCAN to remain accessible."
    : isGcms
      ? "Required: GC-MS project, one imported analysis file, an existing MS-DIAL Console path, parameter template, RI settings when RI is enabled, and output folder. The diagnostic reads the generated mdscan file and uses the current peak-height setting."
      : `${labels[project]} Run diagnostic is not enabled for this project type yet.`;
  if (!(isLcms || isGcms)) {
    $("#tuningLog").textContent = "Diagnostic tuning is currently enabled for LC-MS mdpeak and GC-MS mdscan outputs.";
  }
  updateGcmsRiUI();
  updateRtCorrectionSelectionUI();
  updateRtCorrectionLauncher();
}

function updateRtCorrectionSelectionUI() {
  const weighted = $("#rtCorrectionPeakSelectionMode")?.value === "Weighted";
  if ($("#rtCorrectionPeakSelectionRtWeight")) {
    $("#rtCorrectionPeakSelectionRtWeight").disabled = !weighted;
  }
  if ($("#rtCorrectionPeakSelectionRtWeightField")) {
    $("#rtCorrectionPeakSelectionRtWeightField").title = weighted
      ? "Blend normalized peak height and proximity to the reference RT."
      : "RT weight is used only by Weighted selection.";
  }
}

function updateRtCorrectionLauncher() {
  const anchorPath = $("#rtCorrectionAnchorPath")?.value.trim() || "";
  const selectionPath = $("#rtCorrectionSelectionPath")?.value.trim() || "";
  const ready = Boolean(anchorPath && selectionPath);
  const checkbox = $("#executeRtCorrection");
  checkbox.disabled = !ready;
  if (!ready) checkbox.checked = false;
  $("#rtCorrectionLauncherStatus").innerHTML = ready
    ? `<strong>Approved setup is ready.</strong><br>Anchor: ${escapeHtml(anchorPath)}<br>Peak selections: ${escapeHtml(selectionPath)}`
    : "Open the review workspace, inspect the anchor peaks, and save an approved peak-selection TSV.";
}

function saveRtWorkspaceState() {
  const current = workflow();
  const payload = {
    files: state.files,
    analysis_csv_source: state.analysisCsvSource,
    output_root_automatic: state.outputRootAutomatic,
    project_type: current.project_type,
    ion_mode: current.ion_mode,
    target_omics: current.target_omics,
    ms1_data_type: current.ms1_data_type,
    ms2_data_type: current.ms2_data_type,
    console_path: current.console_path,
    template_path: current.template_path,
    output_root: current.output_root,
    execute_rt_correction: current.execute_rt_correction,
    rt_correction_anchor_path: current.rt_correction_anchor_path,
    rt_correction_anchor_source_path: current.rt_correction_anchor_source_path,
    rt_correction_selection_path: current.rt_correction_selection_path,
    rt_correction_diff_method: current.rt_correction_diff_method,
    rt_correction_smooth_rt_diff: current.rt_correction_smooth_rt_diff,
    rt_correction_intercept: current.rt_correction_intercept,
    rt_correction_extrapolation_begin: current.rt_correction_extrapolation_begin,
    rt_correction_extrapolation_end: current.rt_correction_extrapolation_end,
    rt_correction_peak_selection_mode: current.rt_correction_peak_selection_mode,
    rt_correction_peak_selection_rt_weight: current.rt_correction_peak_selection_rt_weight,
  };
  sessionStorage.setItem(RT_WORKSPACE_STORAGE_KEY, JSON.stringify(payload));
}

function restoreRtWorkspaceState() {
  let saved;
  try {
    // Earlier versions persisted analysis paths across app restarts. Remove that
    // legacy cache while retaining same-tab handoff to the RT review workspace.
    localStorage.removeItem(RT_WORKSPACE_STORAGE_KEY);
    saved = JSON.parse(sessionStorage.getItem(RT_WORKSPACE_STORAGE_KEY) || "null");
  } catch {
    saved = null;
  }
  if (!saved) return;
  if (Array.isArray(saved.files)) state.files = saved.files;
  state.analysisCsvSource = saved.analysis_csv_source || "";
  state.outputRootAutomatic = Boolean(saved.output_root_automatic);
  const values = {
    projectType: saved.project_type,
    ionMode: saved.ion_mode,
    targetOmics: saved.target_omics,
    ms1Type: saved.ms1_data_type,
    ms2Type: saved.ms2_data_type,
    consolePath: saved.console_path,
    templatePath: saved.template_path,
    outputRoot: saved.output_root,
    rtCorrectionAnchorPath: saved.rt_correction_anchor_path,
    rtCorrectionSelectionPath: saved.rt_correction_selection_path,
    rtCorrectionDiffMethod: saved.rt_correction_diff_method,
    rtCorrectionIntercept: saved.rt_correction_intercept,
    rtCorrectionExtrapolationBegin: saved.rt_correction_extrapolation_begin,
    rtCorrectionExtrapolationEnd: saved.rt_correction_extrapolation_end,
    rtCorrectionPeakSelectionMode: saved.rt_correction_peak_selection_mode,
    rtCorrectionPeakSelectionRtWeight: saved.rt_correction_peak_selection_rt_weight,
  };
  Object.entries(values).forEach(([id, value]) => {
    if (value !== undefined && value !== null && $(`#${id}`)) $(`#${id}`).value = value;
  });
  $("#executeRtCorrection").checked = Boolean(saved.execute_rt_correction);
  $("#rtCorrectionSmoothDiff").checked = Boolean(saved.rt_correction_smooth_rt_diff);
  state.rtCorrectionAnchorSourcePath = saved.rt_correction_anchor_source_path
    || saved.rt_correction_anchor_path
    || "";
}

function updateLlmUI() {
  const provider = $("#llmProvider").value;
  const isLocal = provider === "local";
  ["llmEndpoint", "llmDeployment", "llmApiKey"].forEach((id) => {
    $(`#${id}`).disabled = isLocal;
  });
  $("#llmApiVersionField").hidden = provider !== "azure";
  if (isLocal) {
    $("#llmStatus").textContent = state.config?.llm_environment?.azure_configured
      ? "Local retrieval is active. Azure OpenAI environment variables are available if Azure is selected."
      : "Local retrieval is active.";
  } else {
    $("#llmStatus").textContent =
      "The key is kept in browser memory only and sent to localhost for each Ask request.";
  }
  const uiConfigured = Boolean(
    $("#llmApiKey").value.trim()
    && $("#llmEndpoint").value.trim()
    && $("#llmDeployment").value.trim()
  );
  const configured = !isLocal && (
    uiConfigured
    || (provider === "azure" && Boolean(state.config?.llm_environment?.azure_configured))
  );
  $("#searchLiterature").disabled = !configured;
  $("#literatureStatus").textContent = configured
    ? "Ready to search explicitly licensed open-access Crossref records."
    : "Configure an API provider and key to enable this search.";
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
  const sidecarNote = file?.format === "SCIEX WIFF" && !file.sidecar_available
    ? "<br><strong>WIFF.SCAN is not accessible from this imported path.</strong> "
      + "Add the original WIFF file or its containing folder so the sibling remains accessible."
    : "";
  $("#tuningFormat").innerHTML = file
    ? `<strong>${escapeHtml(file.format)}</strong><br>
       Detected as ${escapeHtml(file.vendor)} / ${escapeHtml(file.instrument_family)}.
       Format-based starting values: Minimum peak height ${file.minimum_peak_height}, Mass slice width ${file.mass_slice_width}.
       ${sidecarNote}`
    : "No representative file selected.";
}

function applyFormatStartingValues() {
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
    weighted: Number($("#tuneWeightedNumber").value),
    simple: Number($("#tuneSimpleNumber").value),
    reverse: Number($("#tuneReverseNumber").value),
    matched_percentage: Number($("#tuneMatchedPercentageNumber").value),
    matched_count: Number($("#tuneMinimumMatchNumber").value),
  };
  const passing = result.msp_scores.filter((score) =>
    score.weighted >= thresholds.weighted &&
    score.simple >= thresholds.simple &&
    score.reverse >= thresholds.reverse &&
    score.matched_percentage >= thresholds.matched_percentage &&
    score.matched_count >= thresholds.matched_count
  ).length;
  $("#annotationPassCount").textContent = passing;
}

function connectThresholdInputs(rangeId, numberId, digits = 2) {
  const range = $(`#${rangeId}`);
  const number = $(`#${numberId}`);
  const clamp = (value) => Math.min(Number(range.max), Math.max(Number(range.min), value));
  range.addEventListener("input", () => {
    number.value = Number(range.value).toFixed(digits);
    updateTuningCounts();
  });
  number.addEventListener("input", () => {
    const parsed = Number(number.value);
    if (!Number.isFinite(parsed)) return;
    range.value = clamp(parsed);
    updateTuningCounts();
  });
  number.addEventListener("change", () => {
    const parsed = Number(number.value);
    const value = clamp(Number.isFinite(parsed) ? parsed : Number(range.value));
    range.value = value;
    number.value = value.toFixed(digits);
    updateTuningCounts();
  });
}

function renderTuningResult(result) {
  state.tuningResult = result;
  const maxHeight = result.heights.length ? result.heights[result.heights.length - 1] : 10000;
  const percentileIndex = Math.max(0, Math.ceil(result.heights.length * 0.99) - 1);
  const sliderMax = Math.max(100, Math.ceil(result.heights[percentileIndex] || maxHeight));
  $("#tuningHeight").max = sliderMax;
  const startingValue = Number(selectedTuningFile()?.minimum_peak_height || 100);
  $("#tuningHeight").value = Math.min(startingValue, sliderMax);
  $("#tuningHeightNumber").value = startingValue;
  $("#tuningSummary").innerHTML = `
    <div class="metric"><strong>${result.peak_count}</strong><span>peaks at height 0</span></div>
    <div class="metric"><strong>${result.msp_candidate_count}</strong><span>MSP reference candidates</span></div>
    <div class="metric"><strong>${result.msp_scored_count}</strong><span>MS/MS-scored candidates</span></div>`;
  updateTuningCounts();
}

function renderWorkflowExport(result) {
  const panel = $("#workflowExport");
  panel.hidden = false;
  panel.innerHTML = `
    <strong>Reusable workflow created</strong><br>
    ${escapeHtml(result.preparation.run_directory)}<br>
    Includes final CSV, method.txt, settings JSON, command.txt,
    PowerShell/Bash scripts, and reproduction instructions.<br>
    <a class="download-link" href="${escapeHtml(result.download_url)}">
      Download msdial-workflow-bundle.zip
    </a>`;
}

function renderMzTabValidation(validation) {
  const panel = $("#mztabValidation");
  if (!panel) return;
  if (!validation) {
    panel.hidden = true;
    panel.innerHTML = "";
    return;
  }
  const files = validation.files || [];
  const summary = validation.summary || {};
  const details = files.map((file) => {
    const counts = Object.entries(file.counts || {})
      .filter((entry) => entry[1])
      .map((entry) => `${escapeHtml(entry[0])}=${escapeHtml(entry[1])}`)
      .join(", ");
    const errors = (file.errors || []).map((message) =>
      `<div class="issue error">ERROR: ${escapeHtml(message)}</div>`
    ).join("");
    const warnings = (file.warnings || []).map((message) =>
      `<div class="issue warning">WARNING: ${escapeHtml(message)}</div>`
    ).join("");
    return `<div class="mztab-file">
      <strong>${escapeHtml((file.status || "unknown").toUpperCase())}: ${escapeHtml(file.file_name || file.file)}</strong>
      <div class="muted">${escapeHtml(file.file)}</div>
      ${counts ? `<div class="muted">Sections: ${counts}</div>` : ""}
      ${errors}${warnings}
    </div>`;
  }).join("");
  const external = validation.external_validator
    ? `<div class="muted">External validator: ${escapeHtml(validation.external_validator.message || validation.external_validator.mode || "")}</div>`
    : "";
  panel.innerHTML = `
    <strong>mzTab-M validation: ${escapeHtml(summary.status || validation.status || "unknown")}
      (${escapeHtml(summary.passed || 0)} passed,
      ${escapeHtml(summary.warnings || 0)} warning,
      ${escapeHtml(summary.failed || 0)} failed)</strong>
    ${external}
    ${details || `<div class="issue warning">No mzTab-M file was found.</div>`}`;
  panel.hidden = false;
}

function mztabRunDirectory() {
  return $("#runPath").textContent.trim() || $("#outputRoot").value.trim();
}

function currentMzTabFilePath() {
  const select = $("#mztabFileSelect");
  return (select?.value || state.selectedMzTabPath || "").trim();
}

function renderMzTabFileChoices() {
  const select = $("#mztabFileSelect");
  if (!select) return;
  const knownPaths = new Set(state.mztabFiles.map((file) => file.file));
  const files = [...state.mztabFiles];
  if (state.selectedMzTabPath && !knownPaths.has(state.selectedMzTabPath)) {
    files.unshift({
      file: state.selectedMzTabPath,
      file_name: state.selectedMzTabPath.split(/[\\/]/).pop(),
      is_default: false,
      modified_time_iso: "selected manually",
    });
  }
  select.innerHTML = files.length
    ? files.map((file, index) => {
        const prefix = state.selectedMzTabScope === "job" ? "Job output: " : index === 0 && file.is_default ? "Latest: " : "";
        const label = `${prefix}${file.file_name} (${file.modified_time_iso || "mtime unknown"})`;
        return `<option value="${escapeHtml(file.file)}">${escapeHtml(label)}</option>`;
      }).join("")
    : `<option value="">Latest mzTab-M in output folder</option>`;
  if (state.selectedMzTabPath && files.some((file) => file.file === state.selectedMzTabPath)) {
    select.value = state.selectedMzTabPath;
  } else if (files.length) {
    state.selectedMzTabPath = files[0].file;
    select.value = files[0].file;
  }
  const hint = $("#mztabFileHint");
  if (hint) {
    hint.textContent = files.length
      ? `Selected: ${select.value || "none"}`
      : state.jobId
        ? `Selected job ${state.jobId} did not create or update an mzTab-M file.`
        : "Run or select an analysis job, or choose an archived mzTab-M file manually.";
  }
}

function setSelectedMzTabPath(path, scope = "manual") {
  state.selectedMzTabPath = path || "";
  state.selectedMzTabScope = path ? scope : "";
  if (path && !state.mztabFiles.some((file) => file.file === path)) {
    state.mztabFiles.unshift({
      file: path,
      file_name: path.split(/[\\/]/).pop(),
      modified_time_iso: "selected manually",
      is_default: false,
    });
  }
  renderMzTabFileChoices();
}

async function refreshMzTabFiles(keepSelection = true) {
  if (state.jobId) {
    const job = await api(`/api/jobs/${state.jobId}`);
    state.mztabFiles = jobArtifactFiles(job, "mztab");
    state.selectedMzTabPath = keepSelection && state.mztabFiles.some((file) => file.file === state.selectedMzTabPath)
      ? state.selectedMzTabPath
      : state.mztabFiles[0]?.file || "";
    state.selectedMzTabScope = state.selectedMzTabPath ? "job" : "";
    renderMzTabFileChoices();
    return;
  }
  const runDirectory = mztabRunDirectory();
  if (!runDirectory) {
    state.mztabFiles = [];
    if (!keepSelection) state.selectedMzTabPath = "";
    renderMzTabFileChoices();
    return;
  }
  const previous = keepSelection ? state.selectedMzTabPath : "";
  const result = await api("/api/mztab/list", {
    method: "POST",
    body: JSON.stringify({ run_directory: runDirectory }),
  });
  state.mztabFiles = result.mztab?.files || [];
  state.selectedMzTabScope = "manual";
  if (previous && state.mztabFiles.some((file) => file.file === previous)) {
    state.selectedMzTabPath = previous;
  } else {
    state.selectedMzTabPath = result.mztab?.default_file || "";
  }
  renderMzTabFileChoices();
}

async function selectedMzTabPayload() {
  if (!currentMzTabFilePath() && mztabRunDirectory()) {
    await refreshMzTabFiles(false);
  }
  const runDirectory = mztabRunDirectory();
  const filePath = currentMzTabFilePath();
  if (!runDirectory && !filePath) {
    throw new Error("Run MS-DIAL first, set an output folder, or choose an mzTab-M file.");
  }
  if (state.jobId && state.selectedMzTabScope === "job") {
    return { job_id: state.jobId, file_path: filePath };
  }
  return { run_directory: runDirectory, file_path: filePath };
}

function renderMzTabPreview(preview) {
  const panel = $("#mztabPreview");
  if (!panel) return;
  if (!preview) {
    panel.hidden = true;
    panel.innerHTML = "";
    return;
  }
  if (!preview.file) {
    panel.innerHTML = `<div class="issue warning">${escapeHtml(preview.message || "No mzTab-M file was found.")}</div>`;
    panel.hidden = false;
    return;
  }
  const metadata = preview.metadata || {};
  const metadataRows = [
    "mzTab-version",
    "mzTab-ID",
    "title",
    "description",
    "ms_run[1]-location",
  ].filter((key) => metadata[key]).map((key) =>
    `<tr><th>${escapeHtml(key)}</th><td>${escapeHtml(metadata[key])}</td></tr>`
  ).join("");
  const countCards = Object.entries(preview.counts || {})
    .filter((entry) => entry[1])
    .map((entry) => `<div class="metric compact"><strong>${escapeHtml(entry[1])}</strong><span>${escapeHtml(entry[0])}</span></div>`)
    .join("");
  const sectionBlocks = ["SML", "SMF", "SME"].map((name) =>
    renderMzTabPreviewSection(name, (preview.sections || {})[name] || {})
  ).join("");
  const validation = preview.validation
    ? `<div class="muted">Validation: ${escapeHtml(preview.validation.status || "unknown")}</div>`
    : "";
  panel.innerHTML = `
    <strong>mzTab-M preview: ${escapeHtml(preview.file_name || preview.file)}</strong>
    <div class="muted">${escapeHtml(preview.file)}</div>
    ${validation}
    <div class="metric-grid mztab-counts">${countCards}</div>
    ${metadataRows ? `<h2>Metadata</h2><table class="preview-table">${metadataRows}</table>` : ""}
    ${sectionBlocks}`;
  panel.hidden = false;
}

function renderMzTabPreviewSection(name, section) {
  const columns = section.columns || [];
  const rows = section.rows || [];
  const numeric = section.numeric_columns || [];
  const suggested = section.suggested_columns || {};
  const suggestedText = Object.entries(suggested)
    .filter((entry) => (entry[1] || []).length)
    .map((entry) => `<div><strong>${escapeHtml(entry[0])}</strong>: ${escapeHtml(entry[1].slice(0, 8).join(", "))}</div>`)
    .join("");
  const numericRows = numeric.slice(0, 10).map((column) => `
    <tr>
      <td>${escapeHtml(column.name)}</td>
      <td>${escapeHtml(column.numeric_count)}</td>
      <td>${escapeHtml(column.missing_rate)}</td>
      <td>${escapeHtml(column.min)}</td>
      <td>${escapeHtml(column.max)}</td>
      <td>${escapeHtml(column.mean)}</td>
    </tr>`).join("");
  const visibleColumns = columns.slice(0, 12);
  const dataRows = rows.map((row) => `
    <tr>${visibleColumns.map((column) => `<td>${escapeHtml(row[column] ?? "")}</td>`).join("")}</tr>`
  ).join("");
  return `
    <details class="mztab-preview-section" ${name === "SML" ? "open" : ""}>
      <summary><strong>${escapeHtml(name)}</strong>: ${escapeHtml(section.row_count || 0)} rows, ${escapeHtml(columns.length)} columns</summary>
      ${suggestedText ? `<div class="muted mztab-suggested">${suggestedText}</div>` : ""}
      ${numericRows ? `
        <h2>Numeric columns</h2>
        <table class="preview-table compact-preview">
          <thead><tr><th>Column</th><th>n</th><th>missing rate</th><th>min</th><th>max</th><th>mean</th></tr></thead>
          <tbody>${numericRows}</tbody>
        </table>` : ""}
      ${dataRows ? `
        <h2>First rows</h2>
        <div class="table-wrap preview-wrap">
          <table class="preview-table">
            <thead><tr>${visibleColumns.map((column) => `<th>${escapeHtml(column)}</th>`).join("")}</tr></thead>
            <tbody>${dataRows}</tbody>
          </table>
        </div>` : `<div class="muted">No rows in this section.</div>`}
    </details>`;
}

function parseQaInternalStandards() {
  return $("#qaInternalStandards").value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line, index) => {
      const fields = line.split(/[\t,]/).map((item) => item.trim());
      if (index === 0 && fields.some((item) => item.toLowerCase() === "m/z" || item.toLowerCase() === "mz")) return null;
      const hasAdduct = fields.length >= 6;
      const offset = hasAdduct ? 1 : 0;
      return {
        name: fields[0] || `Internal standard ${index + 1}`,
        adduct: hasAdduct ? fields[1] : "",
        mz: Number(fields[1 + offset]),
        rt: Number(fields[2 + offset]),
        mz_tolerance: Number(fields[3 + offset] || 0.01),
        rt_tolerance: Number(fields[4 + offset] || 0.5),
      };
    })
    .filter((item) => item && Number.isFinite(item.mz) && item.mz > 0 && Number.isFinite(item.rt) && item.rt >= 0);
}

function parsePublicationLibraryProvenance() {
  return $("#publicationLibraryProvenance").value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line, index) => {
      const fields = line.split("|").map((item) => item.trim());
      if (index === 0 && fields[0].toLowerCase().includes("library path")) return null;
      const persistentId = fields[2] || "";
      return {
        label: fields[0] ? fields[0].split(/[\\/]/).pop() : `User library ${index + 1}`,
        local_path: fields[0] || "",
        version: fields[1] || "not recorded",
        doi: persistentId.toLowerCase().includes("doi.org/") ? persistentId.replace(/^https?:\/\/doi\.org\//i, "") : "",
        record_url: persistentId,
        license: fields[3] || "not recorded",
        source: "user supplied",
      };
    })
    .filter(Boolean);
}

function publicationQaCriteria() {
  return {
    median_qc_rsd_percent_max: Number($("#qaCriterionMedianRsd").value),
    qc_features_rsd_le_30_fraction_min: Number($("#qaCriterionRsdFraction").value),
    median_qc_detection_rate_min: Number($("#qaCriterionDetection").value),
    sample_blank_ratio_ge_3_fraction_min: Number($("#qaCriterionBlankSeparation").value),
    qc_pca_relative_dispersion_max: Number($("#qaCriterionPca").value),
    median_blank_carryover_ratio_max: Number($("#qaCriterionCarryover").value),
    run_order_intensity_abs_correlation_max: Number($("#qaCriterionOrderCorrelation").value),
  };
}

async function copyPublicationText(selector, label) {
  const text = $(selector).value;
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
  } else {
    $(selector).focus();
    $(selector).select();
    document.execCommand("copy");
  }
  setStatus(`${label} copied to the clipboard.`);
}

function renderPublicationReport(result) {
  const report = result.report || {};
  const downloads = result.downloads || {};
  $("#publicationOutput").hidden = false;
  $("#materialsMethodsText").value = report.methods_text || "";
  $("#qaResultsText").value = report.qa_results_text || "";
  updatePublicationTextDownloads();
  $("#downloadSupplementaryWorkbook").href = downloads.supplementary_workbook || "#";
  $("#downloadSupplementaryTable").href = downloads.supplementary_table || "#";
  $("#downloadPublicationAudit").href = downloads.audit || "#";
  $("#downloadPublicationBundle").href = downloads.bundle || "#";
  const assessment = report.qa_assessment || {};
  $("#publicationSource").textContent = result.used_saved_settings
    ? `Run settings loaded from ${result.settings_file}. QA criteria passed: ${assessment.passed || 0}/${assessment.evaluated || 0}.`
    : `Current UI settings were used. QA criteria passed: ${assessment.passed || 0}/${assessment.evaluated || 0}.`;
  if (result.qa_file) {
    $("#publicationSource").textContent += ` QA matrix: ${result.qa_file}.`;
  }
  $("#publicationWarnings").innerHTML = (report.warnings || [])
    .map((message) => `<div class="issue warning">${escapeHtml(message)}</div>`)
    .join("");
}

function setPublicationTextDownload(linkSelector, text, filename) {
  const link = $(linkSelector);
  if (link.dataset.objectUrl) URL.revokeObjectURL(link.dataset.objectUrl);
  const objectUrl = URL.createObjectURL(new Blob([`${text}\n`], { type: "text/plain;charset=utf-8" }));
  link.href = objectUrl;
  link.download = filename;
  link.dataset.objectUrl = objectUrl;
}

function updatePublicationTextDownloads() {
  setPublicationTextDownload(
    "#downloadMaterialsMethods",
    $("#materialsMethodsText").value,
    "MS_DIAL_Materials_and_Methods.txt",
  );
  setPublicationTextDownload(
    "#downloadQaResults",
    $("#qaResultsText").value,
    "MS_DIAL_QA_Results.txt",
  );
}

function qaValue(value, digits = 2) {
  if (value === null || value === undefined || value === "") return "N/A";
  return Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : "N/A";
}

function qaPercent(value, digits = 1) {
  if (value === null || value === undefined || value === "") return "N/A";
  return Number.isFinite(Number(value)) ? `${(Number(value) * 100).toFixed(digits)}%` : "N/A";
}

function qaRawPercent(value, digits = 2) {
  const formatted = qaValue(value, digits);
  return formatted === "N/A" ? formatted : `${formatted}%`;
}

function renderQaReport(report) {
  state.qaReport = report;
  const panel = $("#qaReport");
  panel.hidden = false;
  const summary = report.summary || {};
  const counts = summary.category_counts || {};
  const metrics = [
    [summary.sample_count ?? 0, "files"],
    [summary.alignment_spot_count ?? 0, "alignment spots"],
    [`${counts.Sample || 0} / ${counts.QC || 0} / ${counts.Blank || 0}`, "Sample / QC / Blank"],
    [qaRawPercent(summary.median_qc_rsd_percent), "median QC feature RSD"],
    [qaPercent(summary.qc_features_rsd_le_30_percent), "QC features with RSD ≤30%"],
    [qaPercent(summary.median_qc_detection_rate), "median QC detection rate"],
    [qaPercent(summary.sample_blank_ratio_ge_3), "features with Sample/Blank ≥3"],
    [qaValue(summary.qc_pca_relative_dispersion, 3), "QC PCA dispersion / all samples"],
    [qaPercent(summary.median_blank_carryover_ratio), "median blank / previous injection"],
    [qaValue(summary.run_order_intensity_correlation, 3), "order vs median intensity r"],
    [qaPercent(summary.median_msms_acquisition_rate), "median MS/MS acquisition rate"],
    [qaValue(summary.median_sample_sn, 1), "median sample raw S/N"],
  ].map(([value, label]) => `<div class="metric"><strong>${escapeHtml(value)}</strong><span>${escapeHtml(label)}</span></div>`).join("");
  const warnings = (report.warnings || []).map((message) => `<div class="issue warning">${escapeHtml(message)}</div>`).join("");
  const standardCards = (report.internal_standards || []).map((standard, index) => {
    const label = standard.adduct ? `${standard.name} ${standard.adduct}` : standard.name;
    if (standard.status !== "matched") {
      return `<article class="qa-chart-card wide"><h3>${escapeHtml(label)}: not found within the supplied tolerances</h3></article>`;
    }
    return `<article class="qa-chart-card wide">
      <h3>${escapeHtml(label)} | Alignment ID ${escapeHtml(standard.alignment_id)} | median m/z ${qaValue(standard.median_mz, 5)} | median RT ${qaValue(standard.median_rt, 3)}</h3>
      <div class="qa-chart-grid">
        <div><strong>Intensity</strong><canvas data-qa-standard="${index}" data-qa-value="log_height"></canvas></div>
        <div><strong>Mass error (ppm)</strong><canvas data-qa-standard="${index}" data-qa-value="ppm_error"></canvas></div>
        <div><strong>RT error (min)</strong><canvas data-qa-standard="${index}" data-qa-value="rt_delta"></canvas></div>
      </div>
    </article>`;
  }).join("");
  panel.innerHTML = `
    <strong>LC-MS QA report: ${escapeHtml(report.file_name || report.file)}</strong>
    <div class="muted">${escapeHtml(report.file)}</div>
    <div class="metric-grid mztab-counts">${metrics}</div>
    ${warnings}
    <div class="qa-chart-grid">
      <article class="qa-chart-card"><h3>Blank / QC / Sample intensity distributions</h3><canvas id="qaIntensityDistribution"></canvas></article>
      <article class="qa-chart-card"><h3>PCA topology</h3><canvas id="qaPca"></canvas><div class="muted">PC1 ${qaPercent(report.pca?.explained_variance?.[0])}; PC2 ${qaPercent(report.pca?.explained_variance?.[1])}</div></article>
      <article class="qa-chart-card"><h3>Median detected intensity by analytical order</h3><canvas id="qaIntensityOrder"></canvas></article>
      <article class="qa-chart-card"><h3>Reference-matched count by analytical order</h3><canvas id="qaReferenceOrder"></canvas></article>
      <article class="qa-chart-card"><h3>MS/MS acquisition rate by analytical order</h3><canvas id="qaMsmsOrder"></canvas></article>
      <article class="qa-chart-card"><h3>Raw S/N distribution by analytical order</h3><canvas id="qaSnOrder"></canvas></article>
      ${standardCards}
    </div>
    <div class="muted qa-method">${escapeHtml(Object.values(report.method || {}).join(" | "))}</div>`;
  requestAnimationFrame(() => {
    drawQaHistogram($("#qaIntensityDistribution"), report.intensity_distributions || []);
    drawQaPca($("#qaPca"), report.pca || {});
    drawQaOrderDistribution($("#qaIntensityOrder"), report.samples || [], "median_log_intensity", "q25_log_intensity", "q75_log_intensity", "log10 intensity");
    drawQaOrderSeries($("#qaReferenceOrder"), report.samples || [], "reference_matched_count", "matched features");
    drawQaOrderSeries($("#qaMsmsOrder"), report.samples || [], "msms_acquisition_rate", "MS/MS acquisition rate");
    drawQaOrderDistribution($("#qaSnOrder"), report.samples || [], "median_sn", "q25_sn", "q75_sn", "raw S/N");
    $$("[data-qa-standard]").forEach((canvas) => {
      const standard = (report.internal_standards || [])[Number(canvas.dataset.qaStandard)];
      drawQaOrderSeries(canvas, standard?.values || [], canvas.dataset.qaValue, canvas.dataset.qaValue);
    });
  });
}

function qaCanvas(canvas, height = 280) {
  if (!canvas) return null;
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(340, canvas.clientWidth || 520);
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  const context = canvas.getContext("2d");
  context.scale(ratio, ratio);
  context.clearRect(0, 0, width, height);
  return { context, width, height, padding: { left: 72, right: 18, top: 18, bottom: 52 } };
}

function qaAxisTicks(minimum, maximum, integerOnly = false) {
  if (!integerOnly) {
    return Array.from({ length: 5 }, (_, index) => minimum + (index / 4) * (maximum - minimum));
  }
  const lower = Math.ceil(minimum);
  const upper = Math.floor(maximum);
  if (upper <= lower) return [lower];
  const step = Math.max(1, Math.ceil((upper - lower) / 6));
  const ticks = [];
  for (let value = lower; value <= upper; value += step) ticks.push(value);
  if (ticks[ticks.length - 1] !== upper) ticks.push(upper);
  return ticks;
}

function qaAxes(frame, xMin, xMax, yMin, yMax, xLabel, yLabel, options = {}) {
  const { context, width, height, padding } = frame;
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const sx = (value) => padding.left + ((value - xMin) / Math.max(xMax - xMin, 1e-12)) * plotWidth;
  const sy = (value) => padding.top + plotHeight - ((value - yMin) / Math.max(yMax - yMin, 1e-12)) * plotHeight;
  context.font = "14px Arial, sans-serif";
  context.fillStyle = "#526975";
  context.textBaseline = "middle";
  const tickLabel = (value) => {
    const absolute = Math.abs(value);
    if ((absolute > 0 && absolute < 0.01) || absolute >= 10000) return value.toExponential(1);
    if (absolute >= 100) return value.toFixed(0);
    if (absolute >= 10) return value.toFixed(1);
    return value.toFixed(2);
  };
  context.strokeStyle = "#e3eaed";
  context.lineWidth = 1;
  const xTicks = qaAxisTicks(xMin, xMax, Boolean(options.integerX));
  const yTicks = qaAxisTicks(yMin, yMax);
  xTicks.forEach((xValue) => {
    const x = sx(xValue);
    context.beginPath();
    context.moveTo(x, padding.top);
    context.lineTo(x, padding.top + plotHeight);
    context.stroke();
    context.textAlign = "center";
    context.fillText(options.integerX ? String(Math.round(xValue)) : tickLabel(xValue), x, padding.top + plotHeight + 18);
  });
  yTicks.forEach((yValue) => {
    const fraction = (yValue - yMin) / Math.max(yMax - yMin, 1e-12);
    const y = padding.top + plotHeight - fraction * plotHeight;
    context.beginPath();
    context.moveTo(padding.left, y);
    context.lineTo(padding.left + plotWidth, y);
    context.stroke();
    context.textAlign = "right";
    context.fillText(tickLabel(yValue), padding.left - 8, y);
  });
  context.strokeStyle = "#718994";
  context.beginPath();
  context.moveTo(padding.left, padding.top);
  context.lineTo(padding.left, padding.top + plotHeight);
  context.lineTo(padding.left + plotWidth, padding.top + plotHeight);
  context.stroke();
  context.font = "bold 14px Arial, sans-serif";
  context.textAlign = "center";
  context.fillText(xLabel, padding.left + plotWidth / 2, height - 10);
  context.save();
  context.translate(15, padding.top + plotHeight / 2);
  context.rotate(-Math.PI / 2);
  context.fillText(yLabel, 0, 0);
  context.restore();
  context.font = "14px Arial, sans-serif";
  context.textBaseline = "alphabetic";
  return { sx, sy };
}

const QA_COLORS = { Sample: "#2879b9", QC: "#007f86", Blank: "#d85f2a" };

function drawQaHistogram(canvas, distributions) {
  const frame = qaCanvas(canvas);
  if (!frame || !distributions.length) return;
  const xs = distributions.flatMap((item) => item.bin_centers || []);
  const ys = distributions.flatMap((item) => item.density || []);
  const axes = qaAxes(frame, Math.min(...xs), Math.max(...xs), 0, Math.max(...ys, 1e-4), "log10(height + 1)", "density");
  distributions.forEach((item) => {
    frame.context.strokeStyle = QA_COLORS[item.category] || "#6e55a5";
    frame.context.lineWidth = 2;
    frame.context.beginPath();
    (item.bin_centers || []).forEach((value, index) => {
      const x = axes.sx(value);
      const y = axes.sy(item.density[index] || 0);
      if (index === 0) frame.context.moveTo(x, y); else frame.context.lineTo(x, y);
    });
    frame.context.stroke();
  });
  distributions.forEach((item, index) => {
    frame.context.fillStyle = QA_COLORS[item.category] || "#6e55a5";
    frame.context.fillText(item.category, frame.padding.left + 8 + index * 65, frame.padding.top + 12);
  });
}

function drawQaPca(canvas, pca) {
  const frame = qaCanvas(canvas);
  const points = pca?.points || [];
  if (!frame || !points.length) return;
  const xs = points.map((item) => Number(item.pc1));
  const ys = points.map((item) => Number(item.pc2));
  const xPad = Math.max((Math.max(...xs) - Math.min(...xs)) * 0.08, 1e-6);
  const yPad = Math.max((Math.max(...ys) - Math.min(...ys)) * 0.08, 1e-6);
  const axes = qaAxes(frame, Math.min(...xs) - xPad, Math.max(...xs) + xPad, Math.min(...ys) - yPad, Math.max(...ys) + yPad, "PC1", "PC2");
  points.forEach((point) => {
    frame.context.fillStyle = QA_COLORS[point.category] || "#6e55a5";
    frame.context.beginPath();
    frame.context.arc(axes.sx(point.pc1), axes.sy(point.pc2), point.category === "QC" ? 5 : 3.5, 0, Math.PI * 2);
    frame.context.fill();
  });
}

function drawQaOrderSeries(canvas, values, key, yLabel) {
  const frame = qaCanvas(canvas);
  const points = values
    .map((item) => ({
      ...item,
      x: Number(item.order),
      y: item[key] === null || item[key] === undefined ? Number.NaN : Number(item[key]),
    }))
    .filter((item) => Number.isFinite(item.x) && Number.isFinite(item.y));
  if (!frame || !points.length) return;
  points.sort((left, right) => left.batch - right.batch || left.x - right.x);
  const xs = points.map((item) => item.x);
  const ys = points.map((item) => item.y);
  const yPad = Math.max((Math.max(...ys) - Math.min(...ys)) * 0.08, 1e-6);
  const axes = qaAxes(frame, Math.min(...xs), Math.max(...xs), Math.min(...ys) - yPad, Math.max(...ys) + yPad, "analytical order", yLabel, { integerX: true });
  frame.context.strokeStyle = "#b5c2c8";
  frame.context.lineWidth = 1;
  frame.context.beginPath();
  points.forEach((point, index) => {
    if (index === 0) frame.context.moveTo(axes.sx(point.x), axes.sy(point.y));
    else frame.context.lineTo(axes.sx(point.x), axes.sy(point.y));
  });
  frame.context.stroke();
  points.forEach((point) => {
    frame.context.fillStyle = QA_COLORS[point.category] || "#6e55a5";
    frame.context.beginPath();
    frame.context.arc(axes.sx(point.x), axes.sy(point.y), point.category === "QC" ? 4.5 : 3, 0, Math.PI * 2);
    frame.context.fill();
  });
}

function drawQaOrderDistribution(canvas, values, medianKey, lowKey, highKey, yLabel) {
  const frame = qaCanvas(canvas);
  const points = values
    .map((item) => ({
      ...item,
      x: Number(item.order),
      y: item[medianKey] === null || item[medianKey] === undefined ? Number.NaN : Number(item[medianKey]),
      low: item[lowKey] === null || item[lowKey] === undefined ? Number.NaN : Number(item[lowKey]),
      high: item[highKey] === null || item[highKey] === undefined ? Number.NaN : Number(item[highKey]),
    }))
    .filter((item) => [item.x, item.y, item.low, item.high].every(Number.isFinite));
  if (!frame || !points.length) return;
  points.sort((left, right) => left.batch - right.batch || left.x - right.x);
  const xs = points.map((item) => item.x);
  const ranges = points.flatMap((item) => [item.low, item.high]);
  const yPad = Math.max((Math.max(...ranges) - Math.min(...ranges)) * 0.08, 1e-6);
  const axes = qaAxes(frame, Math.min(...xs), Math.max(...xs), Math.min(...ranges) - yPad, Math.max(...ranges) + yPad, "analytical order", yLabel, { integerX: true });
  frame.context.strokeStyle = "#b5c2c8";
  frame.context.lineWidth = 1;
  frame.context.beginPath();
  points.forEach((point, index) => {
    if (index === 0) frame.context.moveTo(axes.sx(point.x), axes.sy(point.y));
    else frame.context.lineTo(axes.sx(point.x), axes.sy(point.y));
  });
  frame.context.stroke();
  points.forEach((point) => {
    const x = axes.sx(point.x);
    const color = QA_COLORS[point.category] || "#6e55a5";
    frame.context.strokeStyle = color;
    frame.context.lineWidth = 2;
    frame.context.beginPath();
    frame.context.moveTo(x, axes.sy(point.low));
    frame.context.lineTo(x, axes.sy(point.high));
    frame.context.moveTo(x - 4, axes.sy(point.low));
    frame.context.lineTo(x + 4, axes.sy(point.low));
    frame.context.moveTo(x - 4, axes.sy(point.high));
    frame.context.lineTo(x + 4, axes.sy(point.high));
    frame.context.stroke();
    frame.context.fillStyle = color;
    frame.context.beginPath();
    frame.context.arc(x, axes.sy(point.y), point.category === "QC" ? 5 : 3.5, 0, Math.PI * 2);
    frame.context.fill();
  });
}

function renderLiterature(result) {
  $("#literatureSummary").hidden = false;
  $("#literatureSummary").textContent = result.summary;
  $("#literatureStatus").textContent =
    `${result.works.length} open-access candidate(s) for: ${result.query}`;
  $("#literatureWorks").innerHTML = result.works.map((work, index) => `
    <article class="card">
      <strong>${index + 1}. ${escapeHtml(work.title)}</strong>
      <div>${escapeHtml(work.year || "Year unknown")} | Crossref citations ${work.citations}
        | confidence ${escapeHtml(work.confidence)}</div>
      <div class="muted">Direct parameter terms:
        ${escapeHtml(work.direct_parameter_terms.join(", ") || "none")}</div>
      <a href="${escapeHtml(work.url)}" target="_blank" rel="noreferrer">Open source record</a>
    </article>`).join("");
}

async function pollTuningJob() {
  if (!state.tuningJobId) return;
  try {
    const job = await api(`/api/jobs/${state.tuningJobId}`);
    $("#tuningLog").textContent = job.logs.join("\n") || job.status;
    $("#tuningLog").scrollTop = $("#tuningLog").scrollHeight;
    setStatus(`Tuning job ${job.status}`);
    if (["queued", "running"].includes(job.status)) {
      setTimeout(pollTuningJob, 1000);
    } else if (job.status === "completed" && job.result) {
      renderTuningResult(job.result);
      $("#tuningLog").textContent += `\nLoaded ${job.result.source_file}`;
    } else if (job.error) {
      $("#tuningLog").textContent += `\n${job.error}`;
    }
  } catch (error) {
    $("#tuningLog").textContent += `\nDiagnostic status error: ${error.message}`;
    setStatus("Tuning job status failed");
  }
}

function renderRtCorrectionResult(result) {
  state.rtCorrectionResult = result;
  const rows = result?.rows || [];
  $("#rtCorrectionReview").hidden = !rows.length;
  $("#saveRtCorrectionSelections").disabled = !rows.length;
  $("#rtCorrectionCount").textContent = `${rows.filter((row) => row.use).length} / ${rows.length} anchors enabled`;
  $("#rtCorrectionSelectionRows").innerHTML = rows.map((row, index) => `
    <tr data-index="${index}">
      <td><input data-key="use" type="checkbox" ${row.use ? "checked" : ""}></td>
      <td title="${escapeHtml(row.file_path)}">${escapeHtml(row.file_name)}</td>
      <td>${escapeHtml(row.standard_name)}</td>
      <td>${Number(row.reference_rt).toFixed(4)}</td>
      <td>${Number(row.detected_rt).toFixed(4)}</td>
      <td><input data-key="selected_rt" type="number" step="any" value="${Number(row.selected_rt)}"></td>
      <td data-value="peak_height">${Number(row.peak_height).toLocaleString()}</td>
    </tr>`).join("");
  $$("#rtCorrectionSelectionRows tr").forEach((tableRow) => {
    const index = Number(tableRow.dataset.index);
    tableRow.querySelector('[data-key="use"]').addEventListener("change", (event) => {
      rows[index].use = event.target.checked;
      $("#rtCorrectionCount").textContent = `${rows.filter((row) => row.use).length} / ${rows.length} anchors enabled`;
    });
    tableRow.querySelector('[data-key="selected_rt"]').addEventListener("input", (event) => {
      rows[index].selected_rt = Number(event.target.value);
    });
  });

  const groups = new Map();
  (result?.series || []).forEach((series) => {
    const key = `${series.standard_id}|${series.standard_name}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(series);
  });
  $("#rtCorrectionCharts").innerHTML = [...groups.entries()].map(([key, series], index) => `
    <article class="rt-chart-card">
      <h3>${escapeHtml(series[0]?.standard_name || key)} | m/z ${Number(series[0]?.target_mz || 0).toFixed(5)}</h3>
      <div class="rt-chart-pair">
        <div><strong>Original EIC</strong><canvas data-chart-index="${index}" data-x-key="rt"></canvas></div>
        <div><strong>Corrected EIC</strong><canvas data-chart-index="${index}" data-x-key="corrected_rt"></canvas></div>
      </div>
      <div class="rt-manual-picker">
        <label>Manual sample
          <select data-rt-manual-group="${index}">
            ${series.map((item, itemIndex) => `<option value="${itemIndex}">${escapeHtml(item.file_name)}</option>`).join("")}
          </select>
        </label>
        <strong>Click the intended peak in the single-sample EIC</strong>
        <canvas data-rt-manual-canvas="${index}"></canvas>
        <div class="muted">The click snaps to the strongest smoothed point in a ±7-point neighborhood and updates Selected RT in the review table.</div>
      </div>
      <div class="muted">${series.map((item, itemIndex) => `${itemIndex + 1}: ${escapeHtml(item.file_name)}`).join(" | ")}</div>
    </article>`).join("");
  const groupedSeries = [...groups.values()];
  requestAnimationFrame(() => {
    $$("#rtCorrectionCharts canvas").forEach((canvas) => {
      if (canvas.dataset.rtManualCanvas !== undefined) return;
      drawRtCorrectionChart(
        canvas,
        groupedSeries[Number(canvas.dataset.chartIndex)] || [],
        canvas.dataset.xKey,
      );
    });
    $$('[data-rt-manual-group]').forEach((select) => {
      const groupIndex = Number(select.dataset.rtManualGroup);
      const draw = () => drawManualRtSelection(groupedSeries, groupIndex, Number(select.value));
      select.addEventListener("change", draw);
      const canvas = $(`[data-rt-manual-canvas="${groupIndex}"]`);
      canvas.addEventListener("click", (event) => selectManualRtFromChart(event, groupedSeries[groupIndex]?.[Number(select.value)]));
      draw();
    });
  });
}

function rtSelectionRowIndex(series) {
  if (!series) return -1;
  const path = String(series.file_path || "").toLowerCase();
  return (state.rtCorrectionResult?.rows || []).findIndex((row) =>
    Number(row.standard_id) === Number(series.standard_id)
    && String(row.file_path || "").toLowerCase() === path
  );
}

function drawManualRtSelection(groupedSeries, groupIndex, seriesIndex) {
  const series = groupedSeries[groupIndex]?.[seriesIndex];
  const canvas = $(`[data-rt-manual-canvas="${groupIndex}"]`);
  if (!series || !canvas) return;
  drawRtCorrectionChart(canvas, [series], "rt");
  const rowIndex = rtSelectionRowIndex(series);
  const row = state.rtCorrectionResult?.rows?.[rowIndex];
  drawSelectedRtMarker(canvas, row);
}

function drawSelectedRtMarker(canvas, row) {
  const geometry = canvas._rtChartGeometry;
  if (!row || !geometry || !Number.isFinite(Number(row.selected_rt))) return;
  const x = geometry.scaleX(Number(row.selected_rt));
  geometry.context.save();
  geometry.context.strokeStyle = "#c44771";
  geometry.context.setLineDash([5, 4]);
  geometry.context.lineWidth = 2;
  geometry.context.beginPath();
  geometry.context.moveTo(x, geometry.padding.top);
  geometry.context.lineTo(x, geometry.padding.top + geometry.plotHeight);
  geometry.context.stroke();
  geometry.context.restore();
}

function selectManualRtFromChart(event, series) {
  const canvas = event.currentTarget;
  const geometry = canvas._rtChartGeometry;
  if (!series || !geometry) return;
  const bounds = canvas.getBoundingClientRect();
  const chartX = event.clientX - bounds.left;
  const clickedRt = geometry.xMin
    + ((chartX - geometry.padding.left) / Math.max(geometry.plotWidth, 1)) * (geometry.xMax - geometry.xMin);
  const rtValues = series.rt || [];
  const intensities = series.smoothed_intensity || [];
  if (!rtValues.length) return;
  let nearest = 0;
  for (let index = 1; index < rtValues.length; index += 1) {
    if (Math.abs(rtValues[index] - clickedRt) < Math.abs(rtValues[nearest] - clickedRt)) nearest = index;
  }
  let selected = nearest;
  const begin = Math.max(0, nearest - 7);
  const end = Math.min(rtValues.length - 1, nearest + 7);
  for (let index = begin; index <= end; index += 1) {
    if (Number(intensities[index] || 0) > Number(intensities[selected] || 0)) selected = index;
  }
  const rowIndex = rtSelectionRowIndex(series);
  if (rowIndex < 0) return;
  const row = state.rtCorrectionResult.rows[rowIndex];
  row.selected_rt = Number(rtValues[selected]);
  row.peak_height = Number(intensities[selected] || 0);
  row.use = true;
  const tableRow = $(`#rtCorrectionSelectionRows tr[data-index="${rowIndex}"]`);
  if (tableRow) {
    tableRow.querySelector('[data-key="use"]').checked = true;
    tableRow.querySelector('[data-key="selected_rt"]').value = row.selected_rt;
    tableRow.querySelector('[data-value="peak_height"]').textContent = row.peak_height.toLocaleString();
  }
  $("#rtCorrectionCount").textContent = `${state.rtCorrectionResult.rows.filter((item) => item.use).length} / ${state.rtCorrectionResult.rows.length} anchors enabled`;
  drawRtCorrectionChart(canvas, [series], "rt");
  drawSelectedRtMarker(canvas, row);
  setStatus(`Manual RT selected: ${series.file_name} / ${series.standard_name} = ${row.selected_rt.toFixed(4)} min`);
}

function drawRtCorrectionChart(canvas, seriesList, xKey) {
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(360, canvas.clientWidth || 520);
  const height = 235;
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  const context = canvas.getContext("2d");
  context.scale(ratio, ratio);
  context.clearRect(0, 0, width, height);
  const padding = { left: 72, right: 16, top: 18, bottom: 48 };
  const xs = seriesList.flatMap((series) => series[xKey] || []);
  const ys = seriesList.flatMap((series) => series.smoothed_intensity || []);
  if (!xs.length || !ys.length) return;
  const referenceRt = Number(seriesList[0]?.reference_rt);
  const rtTolerance = Number(seriesList[0]?.rt_tolerance);
  const hasAnchorRange = Number.isFinite(referenceRt)
    && Number.isFinite(rtTolerance)
    && rtTolerance > 0;
  const xMin = hasAnchorRange ? Math.max(0, referenceRt - rtTolerance) : Math.min(...xs);
  const xMax = hasAnchorRange ? referenceRt + rtTolerance : Math.max(...xs);
  const yMax = Math.max(...ys, 1);
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const scaleX = (value) => padding.left + ((value - xMin) / Math.max(xMax - xMin, 1e-9)) * plotWidth;
  const scaleY = (value) => padding.top + plotHeight - (value / yMax) * plotHeight;
  context.strokeStyle = "#93a7b1";
  context.lineWidth = 1;
  context.beginPath();
  context.moveTo(padding.left, padding.top);
  context.lineTo(padding.left, padding.top + plotHeight);
  context.lineTo(padding.left + plotWidth, padding.top + plotHeight);
  context.stroke();
  context.fillStyle = "#526975";
  context.font = "14px Arial, sans-serif";
  context.textAlign = "center";
  context.fillText(xMin.toFixed(2), padding.left, padding.top + plotHeight + 18);
  context.fillText(xMax.toFixed(2), padding.left + plotWidth, padding.top + plotHeight + 18);
  context.textAlign = "right";
  context.fillText(yMax.toExponential(1), padding.left - 8, padding.top + 5);
  context.fillText("0", padding.left - 8, padding.top + plotHeight);
  context.font = "bold 14px Arial, sans-serif";
  context.textAlign = "center";
  context.fillText("RT (min)", padding.left + plotWidth / 2, height - 8);
  context.save();
  context.translate(15, padding.top + plotHeight / 2);
  context.rotate(-Math.PI / 2);
  context.fillText("Intensity", 0, 0);
  context.restore();
  const colors = ["#007f86", "#d85f2a", "#6e55a5", "#b68a00", "#2879b9", "#c44771"];
  seriesList.forEach((series, index) => {
    const xValues = series[xKey] || [];
    const yValues = series.smoothed_intensity || [];
    context.strokeStyle = colors[index % colors.length];
    context.lineWidth = 1.6;
    context.beginPath();
    let started = false;
    xValues.forEach((x, pointIndex) => {
      if (x < xMin || x > xMax) return;
      const px = scaleX(x);
      const py = scaleY(yValues[pointIndex] || 0);
      if (!started) {
        context.moveTo(px, py);
        started = true;
      }
      else context.lineTo(px, py);
    });
    context.stroke();
  });
  canvas._rtChartGeometry = {
    context, width, height, padding, plotWidth, plotHeight, xMin, xMax, scaleX, scaleY,
  };
}

async function pollRtCorrectionJob() {
  if (!state.rtCorrectionJobId) return;
  try {
    const job = await api(`/api/jobs/${state.rtCorrectionJobId}`);
    $("#rtCorrectionLog").textContent = job.logs.join("\n") || job.status;
    $("#rtCorrectionLog").scrollTop = $("#rtCorrectionLog").scrollHeight;
    setStatus(`RT correction audit ${job.status}`);
    if (["queued", "running"].includes(job.status)) {
      setTimeout(pollRtCorrectionJob, 1000);
    } else if (job.status === "completed" && job.result) {
      renderRtCorrectionResult(job.result);
      $("#rtCorrectionLog").textContent +=
        `\nLoaded ${job.result.rows.length} automatic anchor selections. Review the table, then save the approved peak selections.`;
    } else if (job.error) {
      $("#rtCorrectionLog").textContent += `\n${job.error}`;
    }
  } catch (error) {
    $("#rtCorrectionLog").textContent += `\nRT correction status error: ${error.message}`;
    setStatus("RT correction job status failed");
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

function renderServerNotice() {
  const notice = $("#serverNotice");
  const server = state.config.server || {};
  if (!server.shared_server) {
    notice.hidden = true;
    return;
  }
  const urls = (server.lan_urls || []).map((url) => `<code>${escapeHtml(url)}</code>`).join(", ");
  notice.innerHTML = `<strong>Lab server mode:</strong> This app is being served to the network. ` +
    `Analysis data and output folders must be paths visible from this server. ` +
    `A client PC local path is not readable unless it is mounted on the server. ` +
    (urls ? `<br>Candidate URLs: ${urls}` : "");
  notice.hidden = false;
}

function renderConsoleDiscovery(discovery = state.consoleDiscovery) {
  state.consoleDiscovery = discovery || { candidates: [] };
  const select = $("#consoleCandidateSelect");
  const candidates = state.consoleDiscovery.candidates || [];
  const current = $("#consolePath").value.trim();
  select.innerHTML = candidates.length
    ? candidates.map((candidate) => {
        const qa = (candidate.capabilities || []).includes("lcms_alignment_qa_matrix")
          ? "QA matrix supported"
          : "QA matrix unavailable";
        const label = `${candidate.version || "version unknown"} | ${qa} | ${candidate.source} | ${candidate.path}`;
        return `<option value="${escapeHtml(candidate.path)}">${escapeHtml(label)}</option>`;
      }).join("")
    : `<option value="">No MS-DIAL Console candidate was found</option>`;
  const matched = candidates.find((candidate) => candidate.path.toLowerCase() === current.toLowerCase());
  if (matched) select.value = matched.path;
  renderConsoleCapability(matched || null);
}

function renderConsoleCapability(candidate) {
  const panel = $("#consoleCapabilityStatus");
  const qaControl = $("#heightMatrixExport");
  const path = $("#consolePath").value.trim();
  if (!path) {
    panel.innerHTML = `<div class="issue error">No MS-DIAL Console is selected.</div>`;
    return;
  }
  if (!candidate) {
    panel.innerHTML = `Selected path: <code>${escapeHtml(path)}</code><br><span class="muted">Run Detect / check Consoles to verify version and capabilities.</span>`;
    return;
  }
  const capabilities = candidate.capabilities || [];
  const qa = capabilities.includes("lcms_alignment_qa_matrix");
  const wasUnsupported = qaControl.disabled;
  qaControl.disabled = !qa;
  if (!qa) qaControl.checked = false;
  else if (wasUnsupported) qaControl.checked = true;
  panel.innerHTML = `<strong>MS-DIAL Console ${escapeHtml(candidate.version || "version unknown")}</strong><br>`
    + `<code>${escapeHtml(candidate.path)}</code><br>`
    + `<span class="${qa ? "" : "issue warning"}">${qa
      ? "LC-MS QA matrix export is available."
      : "LC-MS QA matrix export is not available in this Console build. Analysis can run, but *.qa.tsv cannot be requested."}</span>`;
}

async function refreshConsoleDiscovery() {
  const current = $("#consolePath").value.trim();
  const result = await api("/api/agent/console/check", {
    method: "POST",
    body: JSON.stringify({ search_roots: current ? [current] : [] }),
  });
  renderConsoleDiscovery(result);
  return result;
}

function jobArtifactFiles(job, kind) {
  return ((job?.artifacts || {})[kind] || []).map((path) => ({
    file: path,
    file_name: path.split(/[\\/]/).pop(),
    modified_time_iso: "created or updated by this job",
    is_default: true,
  }));
}

function renderJobHistory() {
  const panel = $("#jobHistory");
  const jobs = state.jobs || [];
  panel.innerHTML = jobs.length ? jobs.map((job) => {
    const artifacts = job.artifacts || {};
    const warning = (job.warnings || []).map((message) => `<div class="issue warning">${escapeHtml(message)}</div>`).join("");
    return `<article class="job-card ${job.id === state.jobId ? "active" : ""}" data-job-id="${escapeHtml(job.id)}">
      <div class="job-card-head">
        <div><span class="job-status ${escapeHtml(job.status)}">${escapeHtml(job.status || "unknown")}</span> <strong>${escapeHtml(job.analysis_type || job.kind || "job")}</strong></div>
        <button type="button" class="secondary select-job">Use this job</button>
      </div>
      <div class="job-card-meta">${escapeHtml(job.updated_at || job.created_at || "time unknown")}<br>${escapeHtml(job.run_directory || "No run directory")}<br>
      mzTab-M ${(artifacts.mztab || []).length} | QA ${(artifacts.qa || []).length} | MS-DIAL ${(artifacts.msdial || []).length}</div>${warning}
    </article>`;
  }).join("") : `<div class="muted">No persisted analysis jobs were found.</div>`;
  panel.querySelectorAll(".select-job").forEach((button) => {
    button.addEventListener("click", () => runUiAction(async () => {
      await selectAnalysisJob(button.closest(".job-card").dataset.jobId);
    }));
  });
}

async function refreshJobHistory() {
  const status = await api("/api/agent/status");
  state.jobs = (status.jobs || []).filter((job) => job.kind === "run");
  renderJobHistory();
}

async function selectAnalysisJob(jobId, job = null) {
  const selected = job || await api(`/api/jobs/${jobId}`);
  state.jobId = selected.id;
  if (selected.run_directory) {
    $("#runPath").textContent = selected.run_directory;
    $("#publicationRunDirectory").value = selected.run_directory;
  }
  state.mztabFiles = jobArtifactFiles(selected, "mztab");
  state.selectedMzTabPath = state.mztabFiles[0]?.file || "";
  state.selectedMzTabScope = state.selectedMzTabPath ? "job" : "";
  renderMzTabFileChoices();
  const qaFiles = jobArtifactFiles(selected, "qa");
  $("#qaFilePath").value = qaFiles[0]?.file || "";
  state.qaSourceJobId = qaFiles.length ? selected.id : "";
  state.qaReport = null;
  renderQaFileProvenance(selected);
  renderPublicationJobSource(selected);
  $("#log").textContent = (selected.log_tail || []).join("\n") || selected.status;
  renderJobHistory();
  setStatus(`Selected analysis job ${selected.id}.`);
}

function clearAnalysisJobSelection() {
  state.jobId = null;
  state.qaSourceJobId = "";
  state.selectedMzTabScope = "manual";
  state.qaReport = null;
  renderJobHistory();
  renderQaFileProvenance();
  renderPublicationJobSource();
  setStatus("Job scoping cleared. Explicitly selected files and directories will be used.");
}

function renderQaFileProvenance(job = null) {
  const panel = $("#qaFileProvenance");
  const qaPath = $("#qaFilePath").value.trim();
  if (job && state.qaSourceJobId === job.id && qaPath) {
    panel.innerHTML = `<strong>Job-owned QA matrix</strong><br>Job: <code>${escapeHtml(job.id)}</code><br>File: <code>${escapeHtml(qaPath)}</code><br>Updated: ${escapeHtml(job.updated_at || "unknown")}`;
  } else if (qaPath) {
    panel.innerHTML = `<strong>Manually selected QA matrix</strong><br><code>${escapeHtml(qaPath)}</code><br><span class="muted">This file is not attributed to the selected analysis job.</span>`;
  } else {
    panel.textContent = "No QA matrix is associated with the selected job.";
  }
}

function renderPublicationJobSource(job = null) {
  const panel = $("#publicationJobSource");
  if (!job) {
    panel.textContent = "No analysis job selected. The report will use the explicitly selected directory and files.";
    return;
  }
  const qaCount = ((job.artifacts || {}).qa || []).length;
  panel.innerHTML = `<strong>Selected job:</strong> <code>${escapeHtml(job.id)}</code><br>${escapeHtml(job.run_directory || "")}`
    + `<br>QA matrices created or updated by this job: ${qaCount}`;
}

async function pollJob() {
  if (!state.jobId) return;
  const job = await api(`/api/jobs/${state.jobId}`);
  $("#log").textContent = (job.log_tail || []).join("\n") || job.status;
  $("#log").scrollTop = $("#log").scrollHeight;
  setStatus(`Job ${job.status}`);
  if (["queued", "running"].includes(job.status)) {
    setTimeout(pollJob, 1000);
  } else {
    await selectAnalysisJob(job.id, job);
    if ((job.artifacts?.mztab || []).length) {
      const result = await api("/api/mztab/validate", {
        method: "POST",
        body: JSON.stringify({ job_id: job.id }),
      });
      renderMzTabValidation(result.validation);
    }
    await refreshJobHistory();
  }
}

async function initialize() {
  state.config = await api("/api/config");
  $("#platformPill").textContent =
    `v${state.config.app_version} | ${navigator.platform} | ${state.config.knowledge_cards.ja} JA / ${state.config.knowledge_cards.en} EN cards`;
  renderServerNotice();
  $("#templatePath").value = state.config.default_template;
  $("#queriesPath").value = state.config.default_queries;
  $("#consolePath").value = state.config.default_console || "";
  renderConsoleDiscovery(state.config.console_discovery);
  $("#pathSettingsInfo").textContent = state.config.settings_loaded
    ? `Loaded saved paths from ${state.config.settings_file}`
    : `Paths can be saved locally to ${state.config.settings_file}`;
  if (state.config.smoothing_methods?.length) {
    $("#smoothingMethod").innerHTML = state.config.smoothing_methods
      .map((method) => `<option ${method === "LinearWeightedMovingAverage" ? "selected" : ""}>${escapeHtml(method)}</option>`)
      .join("");
  }
  restoreRtWorkspaceState();
  state.lipidQueries = state.config.lipid_queries;
  state.adducts = state.config.adducts;
  if (!state.mspAnnotators.length) state.mspAnnotators.push(defaultMspAnnotatorRow());
  if (!state.textAnnotators.length) state.textAnnotators.push(defaultTextAnnotatorRow());
  if (!state.lbmAnnotator.lbm_file_path) state.lbmAnnotator = defaultLbmAnnotator(state.lbmAnnotator);
  renderLipids();
  renderAdducts();
  renderLbmAnnotator();
  renderMspAnnotators();
  renderTextAnnotators();
  renderLibraryCatalog();
  await refreshJobHistory();
  renderFiles();
  updateProjectUI();
  applyWorkspaceMode();
  if (location.pathname.startsWith("/rt-correction") && $("#rtCorrectionAnchorPath").value.trim()) {
    await loadRtCorrectionAnchors($("#rtCorrectionAnchorPath").value.trim());
  }
  updateLlmUI();
  refreshQuestion();
}

function applyWorkspaceMode() {
  if (!location.pathname.startsWith("/rt-correction")) return;
  document.body.classList.add("rt-correction-workspace");
  $("#returnToMainAppHeader").hidden = false;
  document.querySelector("header h1").textContent = "MS-DIAL RT Correction Review";
  document.querySelector("header p").textContent =
    "Cross-platform anchor EIC review and retention-time correction";
  $("#projectType").value = "lcms";
  updateProjectUI();
  $$("#tabs button").forEach((button) => {
    button.hidden = !["data", "guide"].includes(button.dataset.tab);
  });
}

$("#tabs").addEventListener("click", (event) => {
  if (!event.target.dataset.tab) return;
  $$("#tabs button").forEach((button) => button.classList.toggle("active", button === event.target));
  $$(".tab").forEach((tab) => tab.classList.toggle("active", tab.id === `tab-${event.target.dataset.tab}`));
});

$("#pickFiles").addEventListener("click", () => runUiAction(async () => {
  const result = await api("/api/dialog/files", { method: "POST", body: "{}" });
  mergeFiles(result.files || [], [
    ...(result.warnings || []),
    ...(result.rejected || []).map((path) => `Rejected unsupported analysis input: ${path}`),
  ]);
}));
$("#pickVendorFolder").addEventListener("click", () => runUiAction(async () => {
  await openPathPicker("vendor");
}));
$("#pickFolder").addEventListener("click", () => runUiAction(async () => {
  await openPathPicker("all");
}));
$("#importAnalysisCsv").addEventListener("click", () => runUiAction(async () => {
  const picked = await api("/api/dialog/reference-file", {
    method: "POST",
    body: JSON.stringify({ kind: "analysis-csv" }),
  });
  if (!picked.path) return;
  const result = await api("/api/files/import-csv", {
    method: "POST",
    body: JSON.stringify({ path: picked.path }),
  });
  if (state.files.length && !window.confirm(
    `Replace the current ${state.files.length} analysis file(s) with ${result.files.length} row(s) from the CSV?`
  )) return;
  state.files = result.files || [];
  state.analysisCsvSource = result.source_csv || picked.path;
  if (state.outputRootAutomatic) setOutputRootFromFirstFile();
  renderFiles();
  applyFormatStartingValues();
  showImportMessages([
    `Imported ${state.files.length} analysis rows from ${state.analysisCsvSource}.`,
    ...(result.warnings || []),
    ...(result.rejected || []).map((path) => `Skipped CSV row: ${path}`),
  ], result.rejected?.length ? "warning" : "info");
  refreshQuestion();
}));
$("#addPath").addEventListener("click", () => runUiAction(async () => {
  if ($("#serverPath").value.trim()) await addServerPaths([$("#serverPath").value.trim()]);
}));
$("#pathPickerClose").addEventListener("click", closePathPicker);
$("#pathPickerModal").addEventListener("click", (event) => {
  if (event.target.id === "pathPickerModal") closePathPicker();
});
$("#pathPickerGo").addEventListener("click", () => runUiAction(async () => {
  await browseLocalPath($("#pathPickerPath").value.trim());
}));
$("#pathPickerPath").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    runUiAction(async () => browseLocalPath($("#pathPickerPath").value.trim()));
  }
});
$("#pathPickerUp").addEventListener("click", () => runUiAction(async () => {
  if (state.pathPicker.parent) await browseLocalPath(state.pathPicker.parent);
}));
$("#pathPickerRoot").addEventListener("change", () => runUiAction(async () => {
  await browseLocalPath($("#pathPickerRoot").value);
}));
$("#pathPickerSelectAll").addEventListener("click", () => {
  $$("#pathPickerEntries input[type='checkbox']").forEach((input) => {
    if (!input.disabled) input.checked = true;
  });
});
$("#pathPickerAddCurrent").addEventListener("click", () => runUiAction(async () => {
  if (state.pathPicker.mode === "mztab") {
    $("#runPath").textContent = state.pathPicker.currentPath;
    state.selectedMzTabPath = "";
    await refreshMzTabFiles(false);
    closePathPicker();
    return;
  }
  await addServerPaths([state.pathPicker.currentPath]);
  closePathPicker();
}));
$("#pathPickerAddSelected").addEventListener("click", () => runUiAction(addSelectedPathPickerEntries));
$("#clearFiles").addEventListener("click", () => {
  state.files = [];
  state.analysisCsvSource = "";
  if (state.outputRootAutomatic) setOutputRootFromFirstFile();
  renderFiles();
  saveRtWorkspaceState();
  showImportMessages([]);
  refreshQuestion().catch((error) => showImportMessages([error.message || String(error)], "error"));
});
$("#outputRoot").addEventListener("input", () => {
  state.outputRootAutomatic = false;
});
$("#useDataDirectory").addEventListener("click", () => {
  state.outputRootAutomatic = true;
  setOutputRootFromFirstFile();
});

$("#savePathSettings").addEventListener("click", () => runUiAction(async () => {
  const result = await api("/api/settings/paths", {
    method: "POST",
    body: JSON.stringify({
      console_path: $("#consolePath").value.trim(),
      template_path: $("#templatePath").value.trim(),
      queries_path: $("#queriesPath").value.trim(),
    }),
  });
  state.config.settings_loaded = true;
  state.config.settings_file = result.settings_file;
  $("#pathSettingsInfo").textContent = `Saved paths to ${result.settings_file}`;
  setStatus("Paths saved for the next launch.");
}));
$("#refreshConsoleCandidates").addEventListener("click", () => runUiAction(async () => {
  const discovery = await refreshConsoleDiscovery();
  setStatus(`Detected ${discovery.candidates?.length || 0} MS-DIAL Console candidate(s).`);
}));
$("#useConsoleCandidate").addEventListener("click", () => runUiAction(async () => {
  const path = $("#consoleCandidateSelect").value;
  if (!path) throw new Error("Select an MS-DIAL Console candidate first.");
  const result = await api("/api/agent/console/set", {
    method: "POST",
    body: JSON.stringify({ console_path: path }),
  });
  $("#consolePath").value = result.console_path;
  state.config.default_console = result.console_path;
  state.config.settings_loaded = true;
  const candidate = {
    path: result.console_path,
    version: result.version,
    capabilities: result.capabilities || [],
    capability_probe: result.capability_probe,
    source: "saved setting",
  };
  renderConsoleCapability(candidate);
  $("#pathSettingsInfo").textContent = `Saved Console path to ${result.settings_file}`;
  setStatus("Selected and saved the MS-DIAL Console path.");
}));
$("#consoleCandidateSelect").addEventListener("change", () => {
  const path = $("#consoleCandidateSelect").value;
  const candidate = (state.consoleDiscovery?.candidates || []).find((item) => item.path === path);
  if (candidate) renderConsoleCapability(candidate);
});
$("#consolePath").addEventListener("input", () => renderConsoleCapability(null));

$("#loadParameterTemplate").addEventListener("click", () => runUiAction(async () => {
  const path = $("#templatePath").value.trim();
  if (!path) throw new Error("Set the Parameter template path before loading it.");
  const result = await api("/api/templates/load", {
    method: "POST",
    body: JSON.stringify({ path, queries_path: $("#queriesPath").value.trim() }),
  });
  applyLoadedParameterTemplate(result);
  const lipidCount = (result.lipid_queries || []).filter((item) => item.selected).length;
  setStatus(`Loaded parameter template. ${lipidCount} lipid queries selected.`);
}));

$("#projectType").addEventListener("change", () => {
  maybeSwitchTemplateForProject($("#projectType").value);
  updateProjectUI();
  refreshQuestion();
});
$("#ionMode").addEventListener("change", () => {
  renderLipids();
  renderAdducts();
  refreshQuestion();
});
$("#targetOmics").addEventListener("change", () => {
  updateProjectUI();
  refreshQuestion();
});
$("#openRtCorrectionWorkspace").addEventListener("click", () => {
  saveRtWorkspaceState();
  location.assign("/rt-correction");
});
function returnToMainApp() {
  saveRtWorkspaceState();
  location.assign("/");
}
$("#returnToMainApp").addEventListener("click", returnToMainApp);
$("#returnToMainAppHeader").addEventListener("click", returnToMainApp);
$("#rtCorrectionPeakSelectionMode").addEventListener("change", () => {
  updateRtCorrectionSelectionUI();
  refreshQuestion();
});
$("#browseRtCorrectionAnchor").addEventListener("click", () => runUiAction(async () => {
  if (state.rtCorrectionAnchorsDirty && !window.confirm(
    "Discard unsaved anchor-library edits and choose another file?"
  )) return;
  const result = await api("/api/dialog/reference-file", {
    method: "POST",
    body: JSON.stringify({ kind: "rt-anchor" }),
  });
  if (result.path) await loadRtCorrectionAnchors(result.path);
}));
$("#loadRtCorrectionAnchors").addEventListener("click", () => runUiAction(async () => {
  if (state.rtCorrectionAnchorsDirty && !window.confirm(
    "Discard unsaved edits and reload the anchor library from disk?"
  )) return;
  await loadRtCorrectionAnchors();
}));
$("#saveRtCorrectionAnchors").addEventListener("click", () =>
  runUiAction(() => saveEditedRtCorrectionAnchors()));
$("#browseRtCorrectionSelection").addEventListener("click", () => runUiAction(async () => {
  const result = await api("/api/dialog/reference-file", {
    method: "POST",
    body: JSON.stringify({ kind: "rt-selection" }),
  });
  if (result.path) {
    $("#rtCorrectionSelectionPath").value = result.path;
    saveRtWorkspaceState();
  }
}));
$("#runRtCorrectionAudit").addEventListener("click", () => runUiAction(async () => {
  if (state.rtCorrectionAnchorsDirty) {
    throw new Error(
      "The anchor editor has unsaved changes. Save the edited anchor library, or reload the source file before extracting EICs."
    );
  }
  const current = workflow();
  const missing = [];
  if (current.project_type !== "lcms") missing.push("Select LC-MS as the project type.");
  if (!current.files.length) missing.push("Add at least one analysis file.");
  if (!current.console_path) missing.push("Set the MS-DIAL Console path.");
  if (!current.template_path) missing.push("Set the parameter template path.");
  if (!current.output_root) missing.push("Set the output root.");
  if (!current.rt_correction_anchor_path) missing.push("Set the RT correction anchor library.");
  if (missing.length) throw new Error(`RT correction audit cannot start:\n${missing.join("\n")}`);
  $("#rtCorrectionLog").textContent = "Preparing RT correction EIC audit...";
  const result = await api("/api/rt-correction/run", {
    method: "POST",
    body: JSON.stringify({ workflow: current }),
  });
  state.rtCorrectionJobId = result.job_id;
  $("#rtCorrectionLog").textContent = `RT correction audit queued.\nOutput folder: ${result.preparation.run_directory}`;
  pollRtCorrectionJob();
}));
$("#saveRtCorrectionSelections").addEventListener("click", () => runUiAction(async () => {
  if (!state.rtCorrectionResult?.rows?.length) throw new Error("Run the RT correction EIC audit first.");
  const result = await api("/api/rt-correction/save", {
    method: "POST",
    body: JSON.stringify({
      workflow: workflow(),
      rows: state.rtCorrectionResult.rows,
    }),
  });
  $("#rtCorrectionSelectionPath").value = result.selection_file;
  $("#executeRtCorrection").checked = true;
  updateRtCorrectionLauncher();
  saveRtWorkspaceState();
  $("#rtCorrectionCompletion").hidden = false;
  $("#rtCorrectionCompletion").innerHTML =
    `<strong>Review completed.</strong><br>Approved peak selections: ${escapeHtml(result.selection_file)}<br>`
    + "Return to the main analysis to apply this RT correction setup in the production run.";
  setStatus(`Saved approved RT correction peak selections: ${result.selection_file}`);
}));
[
  "gcmsRetentionType",
  "gcmsAlignmentIndexType",
  "gcmsRiSource",
  "gcmsRiCompoundType",
  "gcmsAccuracyType",
].forEach((id) => {
  $(`#${id}`).addEventListener("change", () => {
    updateGcmsRiUI();
    refreshQuestion();
  });
});
$("#lipidFilter").addEventListener("input", renderLipids);
$("#fillRiMapFromSingle").addEventListener("click", () => {
  const path = $("#gcmsRiStandardPath").value.trim()
    || Object.values(state.gcmsRiMap).find(Boolean)
    || "";
  state.files.forEach((file) => {
    state.gcmsRiMap[file.file_path] = path;
  });
  renderGcmsRiMap();
  setStatus("Filled GC-MS RI mapping rows.");
});
$("#adductFilter").addEventListener("input", renderAdducts);
$("#selectAllAdducts").addEventListener("click", () => {
  (state.adducts[$("#ionMode").value] || []).forEach((item) => { item.selected = true; });
  renderAdducts();
});
$("#clearAdducts").addEventListener("click", () => {
  (state.adducts[$("#ionMode").value] || []).forEach((item) => { item.selected = false; });
  renderAdducts();
});
$("#addMspAnnotator").addEventListener("click", () => {
  state.mspAnnotators.push(defaultMspAnnotatorRow());
  renderMspAnnotators();
});
$("#clearMspAnnotators").addEventListener("click", () => {
  state.mspAnnotators = [defaultMspAnnotatorRow({ annotator_id: "msp_annotator_1", priority: 1 })];
  renderMspAnnotators();
});
$("#addTextAnnotator").addEventListener("click", () => {
  state.textAnnotators.push(defaultTextAnnotatorRow());
  renderTextAnnotators();
});
$("#clearTextAnnotators").addEventListener("click", () => {
  state.textAnnotators = [defaultTextAnnotatorRow({ annotator_id: "text_annotator_1", priority: 1 })];
  renderTextAnnotators();
});
$("#selectAllLipids").addEventListener("click", () => {
  const ion = $("#ionMode").value;
  state.lipidQueries.forEach((item) => {
    if (item.ion_mode === ion) item.selected = true;
  });
  renderLipids();
});
$("#clearLipids").addEventListener("click", () => {
  const ion = $("#ionMode").value;
  state.lipidQueries.forEach((item) => {
    if (item.ion_mode === ion) item.selected = false;
  });
  renderLipids();
});
$("#llmProvider").addEventListener("change", updateLlmUI);
["llmEndpoint", "llmDeployment", "llmApiKey"].forEach((id) =>
  $(`#${id}`).addEventListener("input", updateLlmUI));
$("#refreshQuestion").addEventListener("click", refreshQuestion);
$("#tuningFile").addEventListener("change", renderTuningFormat);
$("#applyFormatStartingValues").addEventListener("click", applyFormatStartingValues);
$("#runTuning").addEventListener("click", () => runUiAction(async () => {
  const current = workflow();
  const file = selectedTuningFile();
  const missing = [];
  if (!["lcms", "gcms"].includes(current.project_type)) missing.push("Project type must be LC-MS or GC-MS.");
  if (!file) missing.push("Select a representative analysis file.");
  if (file?.format === "SCIEX WIFF" && !file.sidecar_available) {
    missing.push(
      "The imported WIFF path has no adjacent WIFF.SCAN. "
      + "Use Add original files, Add original folder, or Add path "
      + "so MS-DIAL reads the WIFF from its original directory."
    );
  }
  if (!current.console_path) missing.push("Set the MS-DIAL Console path in Guided setup.");
  if (!current.template_path) missing.push("Set the parameter template path.");
  if (!current.output_root) missing.push("Set the output root.");
  if (missing.length) {
    throw new Error(`Diagnostic cannot start:\n${missing.join("\n")}`);
  }
  $("#tuningLog").textContent = "Preparing diagnostic run...";
  try {
    const result = await api("/api/tuning/run", {
      method: "POST",
      body: JSON.stringify({ workflow: current, file_path: file.file_path }),
    });
    state.tuningJobId = result.job_id;
    $("#tuningLog").textContent =
      `Diagnostic queued.\nOutput folder: ${result.preparation.run_directory}`;
    pollTuningJob();
  } catch (error) {
    $("#tuningLog").textContent = `Diagnostic could not start:\n${error.message}`;
    throw error;
  }
}));
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
connectThresholdInputs("tuneWeighted", "tuneWeightedNumber");
connectThresholdInputs("tuneSimple", "tuneSimpleNumber");
connectThresholdInputs("tuneReverse", "tuneReverseNumber");
connectThresholdInputs("tuneMatchedPercentage", "tuneMatchedPercentageNumber");
connectThresholdInputs("tuneMinimumMatch", "tuneMinimumMatchNumber", 0);
$("#applyTuning").addEventListener("click", () => {
  $("#minimumPeakHeight").value = $("#tuningHeightNumber").value;
  if (!state.mspAnnotators.length) state.mspAnnotators.push(defaultMspAnnotatorRow());
  Object.assign(state.mspAnnotators[0], {
    weighted_dot_product_cutoff: Number($("#tuneWeightedNumber").value),
    simple_dot_product_cutoff: Number($("#tuneSimpleNumber").value),
    reverse_dot_product_cutoff: Number($("#tuneReverseNumber").value),
    matched_peaks_percentage_cutoff: Number($("#tuneMatchedPercentageNumber").value),
    minimum_spectrum_match: Number($("#tuneMinimumMatchNumber").value),
  });
  renderMspAnnotators();
  setStatus("Tuning thresholds applied to the workflow.");
});

$("#validate").addEventListener("click", () => runUiAction(async () => {
  const result = await api("/api/validate", { method: "POST", body: JSON.stringify({ workflow: workflow() }) });
  renderIssues(result.issues, result.console_version);
}));
$("#prepare").addEventListener("click", () => runUiAction(async () => {
  const result = await api("/api/prepare", { method: "POST", body: JSON.stringify({ workflow: workflow() }) });
  $("#runPath").textContent = result.preparation.run_directory;
  $("#log").textContent = [...result.messages, JSON.stringify(result.preparation.command)].join("\n");
  renderMzTabValidation(null);
  renderMzTabPreview(null);
  state.mztabFiles = [];
  state.selectedMzTabPath = "";
  state.selectedMzTabScope = "";
  renderMzTabFileChoices();
  renderWorkflowExport(result);
}));
$("#run").addEventListener("click", () => runUiAction(async () => {
  const result = await api("/api/run", { method: "POST", body: JSON.stringify({ workflow: workflow() }) });
  state.jobId = result.job_id;
  $("#runPath").textContent = result.preparation.run_directory;
  renderMzTabValidation(null);
  renderMzTabPreview(null);
  state.mztabFiles = [];
  state.selectedMzTabPath = "";
  state.selectedMzTabScope = "";
  state.qaSourceJobId = "";
  state.qaReport = null;
  $("#qaFilePath").value = "";
  renderQaFileProvenance();
  renderMzTabFileChoices();
  renderWorkflowExport(result);
  pollJob();
}));
$("#refreshJobHistory").addEventListener("click", () => runUiAction(async () => {
  await refreshJobHistory();
  setStatus(`Loaded ${state.jobs.length} analysis job(s).`);
}));
$("#clearJobSelection").addEventListener("click", clearAnalysisJobSelection);
$("#refreshMzTabFiles").addEventListener("click", () => runUiAction(async () => {
  await refreshMzTabFiles(false);
  setStatus(state.selectedMzTabPath ? "mzTab-M list refreshed." : "No mzTab-M file was found.");
}));
$("#browseMzTabFile").addEventListener("click", () => runUiAction(async () => {
  const result = await api("/api/dialog/mztab-file", { method: "POST", body: "{}" });
  if (result.path) {
    setSelectedMzTabPath(result.path, "manual");
    setStatus(`Selected mzTab-M: ${result.path}`);
  }
}));
$("#pickMzTabPath").addEventListener("click", () => runUiAction(async () => {
  await openPathPicker("mztab");
}));
$("#mztabFileSelect").addEventListener("change", () => {
  state.selectedMzTabPath = $("#mztabFileSelect").value;
  renderMzTabFileChoices();
});
$("#validateMzTab").addEventListener("click", () => runUiAction(async () => {
  const payload = await selectedMzTabPayload();
  const result = await api("/api/mztab/validate", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  renderMzTabValidation(result.validation);
}));
$("#previewMzTab").addEventListener("click", () => runUiAction(async () => {
  const payload = await selectedMzTabPayload();
  const result = await api("/api/mztab/preview", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  renderMzTabPreview(result.preview);
}));
$("#useJobQaFile").addEventListener("click", () => runUiAction(async () => {
  if (!state.jobId) throw new Error("Run or select an analysis job first.");
  const job = await api(`/api/jobs/${state.jobId}`);
  const qaFiles = jobArtifactFiles(job, "qa");
  if (!qaFiles.length) throw new Error(`Job ${state.jobId} did not create or update an LC-MS QA matrix.`);
  $("#qaFilePath").value = qaFiles[0].file;
  state.qaSourceJobId = job.id;
  state.qaReport = null;
  renderQaFileProvenance(job);
  setStatus(`Selected the QA matrix created by job ${job.id}.`);
}));
$("#refreshQaFile").addEventListener("click", () => runUiAction(async () => {
  const outputRoot = $("#outputRoot").value.trim();
  if (!outputRoot) throw new Error("Set Output root before searching for the latest QA matrix.");
  const result = await api("/api/qa/list", {
    method: "POST",
    body: JSON.stringify({ path: outputRoot }),
  });
  $("#qaFilePath").value = result.default_file || "";
  state.qaSourceJobId = "";
  state.qaReport = null;
  renderQaFileProvenance();
  setStatus(result.default_file ? `Selected latest QA matrix: ${result.default_file}` : "No *.qa.tsv was found in Output root.");
}));
$("#browseQaFile").addEventListener("click", () => runUiAction(async () => {
  const result = await api("/api/dialog/qa-file", { method: "POST", body: "{}" });
  if (result.path) {
    $("#qaFilePath").value = result.path;
    state.qaSourceJobId = "";
    state.qaReport = null;
    renderQaFileProvenance();
    setStatus(`Selected LC-MS QA matrix: ${result.path}`);
  }
}));
$("#qaFilePath").addEventListener("input", () => {
  state.qaSourceJobId = "";
  state.qaReport = null;
  renderQaFileProvenance();
});
$("#loadQaInternalStandardExample").addEventListener("click", () => {
  $("#qaInternalStandards").value = [
    "FA 16:0,[M-H]-,255.2330,2.107,0.01,0.05",
    "FA 18:0,[M-H]-,283.2643,2.431,0.01,0.05",
  ].join("\n");
  setStatus("Loaded the FA 16:0 / FA 18:0 pseudo internal-standard example.");
});
$("#generateQaReport").addEventListener("click", () => runUiAction(async () => {
  const filePath = $("#qaFilePath").value.trim();
  const runDirectory = $("#outputRoot").value.trim();
  if (!filePath && !runDirectory) throw new Error("Choose an LC-MS QA matrix or set Output root.");
  $("#qaReport").hidden = false;
  $("#qaReport").textContent = "Building LC-MS QA report...";
  const result = await api("/api/qa/report", {
    method: "POST",
    body: JSON.stringify({
      job_id: state.qaSourceJobId || "",
      file_path: filePath,
      run_directory: runDirectory,
      internal_standards: parseQaInternalStandards(),
    }),
  });
  $("#qaFilePath").value = result.report.file;
  state.qaSourceJobId = result.job_id || state.qaSourceJobId;
  renderQaReport(result.report);
  setStatus("LC-MS QA report generated.");
}));
$("#usePublicationOutputRoot").addEventListener("click", () => {
  $("#publicationRunDirectory").value = $("#outputRoot").value.trim();
  setStatus("Publication report directory set from Output root.");
});
$("#generatePublicationReport").addEventListener("click", () => runUiAction(async () => {
  const runDirectory = $("#publicationRunDirectory").value.trim() || $("#outputRoot").value.trim();
  if (!runDirectory) throw new Error("Set an analysis run/output directory.");
  $("#publicationRunDirectory").value = runDirectory;
  $("#publicationOutput").hidden = true;
  $("#publicationSource").textContent = "Generating publication files...";
  const jobScopedQa = Boolean(state.jobId && state.qaSourceJobId === state.jobId);
  const manualQa = Boolean(!state.jobId && $("#qaFilePath").value.trim());
  const result = await api("/api/publication/report", {
    method: "POST",
    body: JSON.stringify({
      workflow: workflow(),
      job_id: state.jobId || "",
      run_directory: runDirectory,
      use_saved_run: $("#publicationUseSavedRun").checked,
      run_qa: $("#publicationIncludeQa").checked,
      qa_report: jobScopedQa || manualQa ? state.qaReport : null,
      qa_file_path: jobScopedQa || manualQa ? $("#qaFilePath").value.trim() : "",
      internal_standards: parseQaInternalStandards(),
      qa_criteria: publicationQaCriteria(),
      additional_library_provenance: parsePublicationLibraryProvenance(),
    }),
  });
  renderPublicationReport(result);
  setStatus("Publication report generated.");
}));
$("#copyMaterialsMethods").addEventListener("click", () => runUiAction(
  () => copyPublicationText("#materialsMethodsText", "Materials and Methods text")
));
$("#copyQaResults").addEventListener("click", () => runUiAction(
  () => copyPublicationText("#qaResultsText", "QA Results text")
));
$("#materialsMethodsText").addEventListener("input", updatePublicationTextDownloads);
$("#qaResultsText").addEventListener("input", updatePublicationTextDownloads);
$("#exportWorkflow").addEventListener("click", () => runUiAction(async () => {
  const result = await api("/api/export-workflow", {
    method: "POST",
    body: JSON.stringify({ workflow: workflow() }),
  });
  $("#runPath").textContent = result.preparation.run_directory;
  renderMzTabValidation(null);
  renderMzTabPreview(null);
  state.mztabFiles = [];
  state.selectedMzTabPath = "";
  state.selectedMzTabScope = "";
  renderMzTabFileChoices();
  renderWorkflowExport(result);
}));
$("#ask").addEventListener("click", () => runUiAction(async () => {
  $("#answer").textContent = "Searching...";
  try {
    const result = await api("/api/assistant", {
      method: "POST",
      body: JSON.stringify({
        query: $("#question").value,
        language: $("#language").value,
        workflow: workflow(),
        llm: llmConfig(),
      }),
    });
    $("#answer").textContent = result.answer;
    $("#llmStatus").textContent = `Answer mode: ${result.mode}`;
    $("#cards").innerHTML = result.cards.map((card) =>
      `<article class="card"><strong>${escapeHtml(card.question)}</strong>
        <div>${escapeHtml(card.answer)}</div>
        <div class="muted">${escapeHtml(card.feature || "")} | score ${card.score}</div></article>`
    ).join("");
  } catch (error) {
    $("#answer").textContent = `LLM request failed: ${error.message}`;
    throw error;
  }
}));
$("#searchLiterature").addEventListener("click", () => runUiAction(async () => {
  $("#literatureStatus").textContent = "Searching open-access Crossref records...";
  $("#literatureSummary").hidden = true;
  $("#literatureWorks").innerHTML = "";
  const result = await api("/api/literature/evidence", {
    method: "POST",
    body: JSON.stringify({
      language: $("#language").value,
      workflow: workflow(),
      llm: llmConfig(),
    }),
  });
  renderLiterature(result);
}));

initialize().catch((error) => { setStatus(error.message); console.error(error); });
