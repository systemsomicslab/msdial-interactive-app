// The web page posts each MSP annotator row the run writes into msp_annotator_settings.tsv. It rebuilt the row
// from a fixed key list without library_kind or suggestions, so an in-silico row loaded from a template's table
// (library_kind predicted, suggestions false) was written without either column, and the Console searched it
// as acquired spectra with its suggestions on, while every record still named the in-silico tier.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
const cutoffs = source.slice(source.indexOf("const DEFAULT_MSP_CUTOFFS"), source.indexOf("const DEFAULT_TEXT_SETTINGS"));
const row = source.slice(source.indexOf("function mspAnnotatorRow("), source.indexOf("function workflow("));
const context = vm.createContext({});
vm.runInContext(`${cutoffs}\n${row}\nthis.mspAnnotatorRow = mspAnnotatorRow;`, context);
assert.ok(/\.map\(mspAnnotatorRow\)/.test(source), "the posted state builds its MSP rows with mspAnnotatorRow");

// As load_parameter_template reads a predicted row back from a template's table.
const loaded = {
  annotator_id: "msp_in_silico", msp_file_path: " Synthetic-GLACIER_neg.msp ", target_omics: "Metabolomics",
  priority: 1, rt_tolerance: 0.5, ms1_tolerance: 0.01, ms2_tolerance: 0.05, use_rt_scoring: false,
  use_rt_filtering: false, weighted_dot_product_cutoff: 0.5, simple_dot_product_cutoff: 0.5,
  reverse_dot_product_cutoff: 0.5, matched_peaks_percentage_cutoff: 0, minimum_spectrum_match: 3,
  evidence_tier: "In-silico MSP (GLACIER)", library_kind: "predicted", suggestions: false,
};
const posted = context.mspAnnotatorRow(loaded);
assert.equal(posted.library_kind, "predicted");
assert.equal(posted.suggestions, false);
assert.equal(posted.msp_file_path, "Synthetic-GLACIER_neg.msp");
assert.equal(posted.evidence_tier, "In-silico MSP (GLACIER)");
assert.equal(posted.minimum_spectrum_match, 3);

// A row of a table without the columns stays without them, so the run writes the table as it was.
const plain = context.mspAnnotatorRow({ annotator_id: "msp_annotator_1", msp_file_path: "a.msp" });
assert.ok(!("library_kind" in plain), "no library_kind where the row states none");
assert.ok(!("suggestions" in plain), "no suggestions where the row states none");
assert.equal(plain.weighted_dot_product_cutoff, 0.6);
assert.equal(context.mspAnnotatorRow({ msp_file_path: "a.msp", suggestions: null }).suggestions, undefined);
assert.equal(context.mspAnnotatorRow({ msp_file_path: "a.msp", suggestions: true }).suggestions, true);
assert.equal(context.mspAnnotatorRow({ msp_file_path: "a.msp", library_kind: "acquired" }).library_kind, "acquired");

console.log("msp annotator row tests passed");
