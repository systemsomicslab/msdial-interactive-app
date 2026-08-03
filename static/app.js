const state = {
  files: [],
  lipidQueries: [],
  adducts: { Positive: [], Negative: [] },
  jobId: null,
  tuningJobId: null,
  tuningResult: null,
  config: null,
  outputRootAutomatic: true,
  gcmsRiMap: {},
  mspAnnotators: [],
  textAnnotators: [],
  lbmAnnotator: {},
  mztabFiles: [],
  selectedMzTabPath: "",
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
    applyRecommendedParameters();
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
  if (!isLcms) $("#alignmentLightMode").checked = false;
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
       Recommended: Minimum peak height ${file.minimum_peak_height}, Mass slice width ${file.mass_slice_width}.
       ${sidecarNote}`
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
  const recommended = Number(selectedTuningFile()?.minimum_peak_height || 100);
  $("#tuningHeight").value = Math.min(recommended, sliderMax);
  $("#tuningHeightNumber").value = recommended;
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
        const label = `${index === 0 && file.is_default ? "Latest: " : ""}${file.file_name} (${file.modified_time_iso || "mtime unknown"})`;
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
      : "Default is the newest mzTab-M file in the output folder. Refresh after a run, or choose a file manually.";
  }
}

function setSelectedMzTabPath(path) {
  state.selectedMzTabPath = path || "";
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

async function pollJob() {
  if (!state.jobId) return;
  const job = await api(`/api/jobs/${state.jobId}`);
  $("#log").textContent = job.logs.join("\n") || job.status;
  $("#log").scrollTop = $("#log").scrollHeight;
  renderMzTabValidation(job.mztab_validation);
  setStatus(`Job ${job.status}`);
  if (["queued", "running"].includes(job.status)) {
    setTimeout(pollJob, 1000);
  } else {
    refreshMzTabFiles(false).catch((error) => setStatus(`mzTab-M list refresh failed: ${error.message}`));
  }
}

async function initialize() {
  state.config = await api("/api/config");
  $("#platformPill").textContent =
    `${navigator.platform} | ${state.config.knowledge_cards.ja} JA / ${state.config.knowledge_cards.en} EN cards`;
  renderServerNotice();
  $("#templatePath").value = state.config.default_template;
  $("#queriesPath").value = state.config.default_queries;
  $("#consolePath").value = state.config.default_console || "";
  if (state.config.smoothing_methods?.length) {
    $("#smoothingMethod").innerHTML = state.config.smoothing_methods
      .map((method) => `<option ${method === "LinearWeightedMovingAverage" ? "selected" : ""}>${escapeHtml(method)}</option>`)
      .join("");
  }
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
  renderFiles();
  updateProjectUI();
  updateLlmUI();
  refreshQuestion();
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
  if (state.outputRootAutomatic) setOutputRootFromFirstFile();
  renderFiles();
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
$("#applyRecommended").addEventListener("click", applyRecommendedParameters);
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
  renderMzTabFileChoices();
  renderWorkflowExport(result);
  pollJob();
}));
$("#refreshMzTabFiles").addEventListener("click", () => runUiAction(async () => {
  await refreshMzTabFiles(false);
  setStatus(state.selectedMzTabPath ? "mzTab-M list refreshed." : "No mzTab-M file was found.");
}));
$("#browseMzTabFile").addEventListener("click", () => runUiAction(async () => {
  const result = await api("/api/dialog/mztab-file", { method: "POST", body: "{}" });
  if (result.path) {
    setSelectedMzTabPath(result.path);
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
  const result = await api("/api/literature/recommend", {
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
