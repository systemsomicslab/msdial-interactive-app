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
console.log("Automatic RT viewer regression checks passed.");
