const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
const functions = source.slice(source.indexOf("function rtAuditNumber("),
  source.indexOf("function renderRtCorrectionResult("));
const elements = {
  "#rtAuditFileId": { value: "0", addEventListener() {} },
  "#rtAuditFileDetails": { innerHTML: "" },
  "#rtAuditExtractEic": { addEventListener() {} },
  "#automaticRtReview": { hidden: true, innerHTML: "" },
};
const context = vm.createContext({
  state: { automaticRtReview: null },
  $: (selector) => elements[selector],
  escapeHtml: (value) => String(value),
  qaAxisTicks: (min, max) => [min, max],
});
vm.runInContext(functions, context);

const accepted = (id, rt) => ({ file_id: "0", anchor_id: id, mz: 100,
  original_rt: rt, reference_rt: rt - 0.1, offset: -0.1,
  quality_score: 0.8, coverage: 1, used: true, status: "Used" });
const missing = { file_id: "0", anchor_id: "3", mz: 200, original_rt: null,
  reference_rt: 5, offset: null, quality_score: 0, coverage: 0.6,
  used: false, status: "Missing" };

context.state.automaticRtReview = {
  files: [{ file_id: "0", name: "Sample", model_source: "DetectedAnchors",
    model_reconstructable: true, used_anchors: 2, matched_anchors: 2 }],
  anchors: [accepted("1", 2), accepted("2", 4), missing],
};
context.renderAutomaticRtFile();
let html = elements["#rtAuditFileDetails"].innerHTML;
assert.ok(html.includes("<svg"), "Valid anchors still produce plots");
assert.ok(html.includes("Missing") && html.includes("N/A"), "Missing rows remain visible");
assert.ok(!html.includes('<option value="3">'), "An unknown apex is not offered for EIC review");
assert.ok(!html.includes("NaN") && !html.includes("Infinity"), "SVG coordinates remain finite");
assert.equal(context.rtAuditCorrect(null, context.state.automaticRtReview.anchors), null);

const plot = context.rtAuditPlot([{ label: "Partial", color: "blue", line: true,
  points: [[2, 1], [null, null], [4, 2], [NaN, 3], [5, Infinity]] }], "RT", "Offset");
assert.ok(!plot.includes("NaN") && !plot.includes("Infinity"), "Invalid points are excluded from SVG marks too");

context.state.automaticRtReview.anchors = [missing];
context.state.automaticRtReview.files[0].model_reconstructable = false;
context.renderAutomaticRtFile();
html = elements["#rtAuditFileDetails"].innerHTML;
assert.ok(html.includes("No numeric data") && html.includes("Missing"));
assert.match(html, /id="rtAuditExtractEic"[^>]* disabled/);

const badUsed = { ...missing, used: true };
assert.equal(context.rtAuditCorrect(3, [accepted("1", 2), accepted("2", 4), badUsed]), null,
  "An incomplete Used anchor cannot silently change the fitted model");

const eic = { file_name: "Sample", anchor_id: "2", mz: 195.05, tolerance: 0.01,
  original_rt: 4.88, reference_rt: 4.9, quality_score: 0.88, coverage: 1,
  smoothing: { available: true, method: "LinearWeightedMovingAverage", level: 3, note: "Full EIC before cropping", ms1_tolerance: "0.01" },
  points: [{ original_rt: 4.8, corrected_rt: 4.82, intensity: 10, smoothed_intensity: 20 },
    { original_rt: 4.88, corrected_rt: 4.9, intensity: 80, smoothed_intensity: 50 }] };
html = context.rtAuditEicHtml(eic);
assert.ok(html.includes("Original RT axis") && html.includes("Projected alignment RT axis"));
assert.ok(html.includes("Unsmoothed EIC") && html.includes("Smoothed EIC"));
assert.ok(html.includes("LinearWeightedMovingAverage") && html.includes("Level 3"));
assert.ok(!html.includes("NaN") && !html.includes("Infinity"));
const hiddenRaw = context.rtAuditEicHtml(eic, { showRaw: false });
assert.ok(!hiddenRaw.includes("</i>Unsmoothed EIC"), "The raw legend and curve are hidden together");
const unavailable = context.rtAuditEicHtml({ ...eic,
  smoothing: { available: false, method: "SavitzkyGolayFilter", level: 3, note: "Not implemented" },
  points: eic.points.map((point) => ({ ...point, smoothed_intensity: null })) });
assert.match(unavailable, /id="rtAuditShowSmoothed"[^>]* disabled/);
assert.ok(!unavailable.includes("</i>Smoothed EIC"), "No alternate smoother is silently substituted");
assert.ok(context.rtAuditEicHtml(eic, { showRaw: false, showSmoothed: false }).includes("No numeric data"));
const correspondence = context.rtAuditCorrespondence([
  accepted("1", 3), { ...accepted("2", 2), reference_rt: 4, used: false, status: "NonMonotonic" }, missing,
]);
assert.ok(correspondence.includes("Crossing lines") && correspondence.includes("NonMonotonic"));
assert.ok(!correspondence.includes("NaN") && !correspondence.includes("Infinity"));
assert.ok(!correspondence.includes("not used by design"), "The Blank legend appears only with Blank anchors");

// A Blank's anchors are unused by design: neither styled nor plotted nor counted as rejected.
const blankAnchor = (id, rt, status) => ({ file_id: "2", anchor_id: id, mz: 100, original_rt: rt,
  reference_rt: rt - 0.05, offset: -0.05, quality_score: 0.6, coverage: 1, used: false, status, category: "blank" });
const blankFile = { file_id: "2", name: "Blank-1", type: "Blank", model_source: "InterpolatedBlank",
  model_reconstructable: false, used_anchors: 0, matched_anchors: 2 };
context.state.automaticRtReview = { files: [blankFile],
  anchors: [blankAnchor("1", 2, "BlankInterpolateByOrder"), blankAnchor("2", 4, "BlankInterpolateByOrder")] };
elements["#rtAuditFileId"].value = "2";
context.renderAutomaticRtFile();
html = elements["#rtAuditFileDetails"].innerHTML;
assert.ok(!html.includes("rt-audit-rejected"), "Blank anchor rows are not styled as rejected");
assert.ok(html.includes('class="rt-audit-unused"'));
assert.ok(html.includes("Blank anchors, not used by design"));
assert.ok(context.rtAuditCorrespondence(context.state.automaticRtReview.anchors).includes("Blank, not used by design"));

const nonMonotonic = { ...accepted("3", 5), file_id: "1", used: false, status: "NonMonotonic", category: "rejected" };
context.renderAutomaticRtReview({
  run_directory: "run", files: [{ ...blankFile, order: 3, median_absolute_offset: 0, note: "" }],
  anchors: [...context.state.automaticRtReview.anchors, { ...missing, category: "unmatched" }, nonMonotonic],
  reference: null, model_counts: { InterpolatedBlank: 1 },
  method_audit: { status: "method_key_not_applied", reason: "method_key_not_applied" },
  warnings: ["Rejected anchors require review: NonMonotonic (1)"],
  notes: ["2 anchor record(s) are in Blank files, not used by design: BlankInterpolateByOrder (2)."],
});
html = elements["#automaticRtReview"].innerHTML;
assert.match(html, /<strong>1<\/strong><span>anchors rejected by the model/, "Only the model's rejections are counted");
assert.ok(html.includes('<p class="muted">2 anchor record(s) are in Blank files, not used by design'), "Notes are not warnings");
assert.ok(html.includes("not verified (method_key_not_applied); the publication report does not describe a correction"));

// MsdialWorkbench#845: a preview lists every anchor once with Accept/Reject and the approval controls; a run's
// audit lists them by source without; an audit before #845 shows no such table.
const anchorEntry = (key, source, name, flagged = false) => ({ key, source, name, anchor_id: key.split("-")[1],
  library_rt: source === "User" ? 1.25 : null, reference_rt: 1.2, mz: 150.05, observed_mz: 150.0501,
  peak_height: 12000, reference_status: "Reference", files_detected: 5, non_blank_files: 6,
  outlier_flagged: flagged, detection: source === "User" ? "The only peak in the window." : "" });
const previewReview = { run_directory: "preview", files: [{ ...blankFile, type: "Sample", order: 1, median_absolute_offset: 0, note: "" }],
  anchors: [], reference: { file_id: "2", name: "Blank-1" }, model_counts: {}, method_audit: { status: "preview_only", reason: "preview_only" },
  warnings: [], notes: [], exclusions: [{ anchor_name: "old", mz: 500, rt: 5, mz_tolerance: 0.01, rt_tolerance: 0.1, excluded_candidates: 0 }],
  anchor_review: { run: "Preview", anchor_mode: "Hybrid", anchor_mode_columns: true, anchor_library: "lib.txt",
    anchor_library_sha256: "abc", user_anchor_count: 1, automatic_anchor_count: 1,
    anchors: [anchorEntry("User-1", "User", "IS-a", true), anchorEntry("Automatic-2", "Automatic", "")] } };
context.state.automaticRtDecisions = { "User-1": false };
html = context.automaticRtAnchorReviewHtml(previewReview);
assert.ok(html.includes("accept or reject each anchor"));
assert.ok(html.includes('data-anchor-key="User-1" value="reject" checked'), "A decision made is shown");
assert.ok(!html.includes('data-anchor-key="Automatic-2" value="accept" checked'), "No anchor is accepted for the person");
assert.ok(html.includes("IS-a") && html.includes("1.250") && html.includes("150.0501") && html.includes("1.200e+4"));
assert.ok(html.includes("rtAnchorApprovedBy") && html.includes("rtAnchorApprove"));
assert.ok(html.includes("Exclusions") && html.includes("old"));
assert.ok(html.includes("lib.txt (SHA-256 abc)"));
assert.ok(html.includes("No reference file is recorded."), "An audit without a reference says so");
// The preview's reference file, which the approval records and the production run is compared with.
const withReference = context.automaticRtAnchorReviewHtml({ ...previewReview, anchor_review: { ...previewReview.anchor_review,
  reference_file_id: "2", reference_file_name: "C1", reference_selection: "Automatic (coverage and centrality)" } });
assert.ok(withReference.includes("Reference file: C1 (file ID 2), chosen by Automatic (coverage and centrality)."));
const runHtml = context.automaticRtAnchorReviewHtml({ ...previewReview, anchor_review: { ...previewReview.anchor_review, run: "Run" } });
assert.ok(runHtml.includes("Anchors by source") && !runHtml.includes("rtAnchorApprove") && !runHtml.includes('type="radio"'));
assert.equal(context.automaticRtAnchorReviewHtml({ ...previewReview, anchor_review: { run: "", anchor_mode_columns: false, anchors: [anchorEntry("Automatic-1", "Automatic", "")] } }), "");
context.renderAutomaticRtReview(previewReview);
assert.ok(elements["#automaticRtReview"].innerHTML.includes("Write approved anchor library"));
assert.deepEqual({ ...context.state.automaticRtDecisions }, {}, "A review of another directory starts with no decisions");

// A failed preview's partial audit (no reference file) cannot be approved here either (CONF-50, PLAU-4).
const failedHtml = context.automaticRtAnchorReviewHtml({ ...previewReview, reference: null });
assert.ok(failedHtml.includes("This preview cannot be approved") && !failedHtml.includes("rtAnchorApprove")
  && !failedHtml.includes('type="radio"'));
const unverifiedHtml = context.automaticRtAnchorReviewHtml({ ...previewReview,
  method_audit: { status: "method_key_record_not_from_this_method_file", reason: "method_key_record_not_from_this_method_file" } });
assert.ok(unverifiedHtml.includes("method_key_record_not_from_this_method_file") && !unverifiedHtml.includes("rtAnchorApprove"));

// Decision 4: an anchor's flags are shown before the approval, and the anchor's row stands out.
const flaggedReview = { ...previewReview, anchor_review: { ...previewReview.anchor_review, anchors: [
  { ...anchorEntry("Automatic-6", "Automatic", ""), flags: [{ code: "used_only_in_reference", message: "used only in the reference file." }] },
  anchorEntry("Automatic-2", "Automatic", ""),
] } };
html = context.automaticRtAnchorReviewHtml(flaggedReview);
assert.ok(html.includes("Before you approve") && html.includes("used only in the reference file."));
assert.ok(html.includes("1 anchor(s) are flagged"), "The approval panel says how many are flagged");
assert.ok(html.includes("it is not pinned to this one"), "The reference is said to be chosen again");
// A Duplicate or a failed run's Detected record is not styled as a rejection (CONF-49).
context.state.automaticRtReview = { files: [{ file_id: "0", name: "Sample", model_source: "DetectedAnchors",
  model_reconstructable: true, used_anchors: 2, matched_anchors: 2 }],
  anchors: [accepted("1", 2), accepted("2", 4), { ...accepted("3", 5), used: false, status: "Duplicate", category: "duplicate" }] };
elements["#rtAuditFileId"].value = "0";
context.renderAutomaticRtFile();
assert.ok(elements["#rtAuditFileDetails"].innerHTML.includes('<tr class="rt-audit-unused"><td>3</td>'), "A Duplicate is unused, not rejected");
// The analysis batch is shown when the Console writes it.
context.renderAutomaticRtReview({ ...previewReview, run_directory: "batched",
  files: [{ ...blankFile, type: "Sample", order: 1, median_absolute_offset: 0, note: "", analysis_batch: "2" }] });
assert.ok(elements["#automaticRtReview"].innerHTML.includes("<th>Analysis batch</th>"));

// CONF-6, CONF-10: an approval names the job of the preview on screen, or none.
const jobContext = vm.createContext({ state: { automaticRtPreviewJobs: { "D1": "J1", "D2": "J2" } } });
vm.runInContext(source.slice(source.indexOf("function automaticRtPreviewJobFor("),
  source.indexOf("function updateRtCorrectionSelectionUI(")), jobContext);
assert.equal(jobContext.automaticRtPreviewJobFor("D1"), "J1");
assert.equal(jobContext.automaticRtPreviewJobFor("D3"), "", "A directory typed in has no job to send");
assert.ok(!source.includes("state.automaticRtPreviewDirectory = review.run_directory"),
  "Rendering a review never makes it the latest preview's");

// CONF-21: adding or removing a file keeps every order already given.
const fileContext = vm.createContext({
  state: { files: [], outputRootAutomatic: false },
  renderFiles() {}, applyFormatStartingValues() {}, setOutputRootFromFirstFile() {},
  refreshQuestion: () => Promise.resolve(), showImportMessages() {},
});
vm.runInContext(source.slice(source.indexOf("function sciexDescriptor("), source.indexOf("async function addServerPaths(")), fileContext);
fileContext.mergeFiles([{ file_path: "C:/a.mzML", analytical_order: 2 }, { file_path: "C:/b.mzML", analytical_order: 1 }]);
fileContext.state.files[0].analytical_order = 7;
fileContext.mergeFiles([{ file_path: "C:/c.mzML", analytical_order: 1 }, { file_path: "C:/d.mzML" }]);
assert.deepEqual(fileContext.state.files.map((file) => file.analytical_order), [7, 1, 8, 9],
  "Given orders are kept; added files follow the last one");
assert.ok(!source.includes("state.files.forEach((item, order) => item.analytical_order = order + 1)"),
  "Removing a file does not renumber the others");
console.log("Automatic RT viewer regression checks passed.");
