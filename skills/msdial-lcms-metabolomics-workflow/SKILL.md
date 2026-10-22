---
name: msdial-lcms-metabolomics-workflow
description: Run a laboratory LC-MS/MS hydrophilic metabolomics batch (HILIC, or a polar-retaining reversed phase such as HSS T3) end to end through MS-DIAL Interactive - check the inputs, choose the annotation libraries (one MSP; the reanalysis tiers; or in-house, predicted-RT and MS/MS-only MSPs, each with its own retention-time use), agree the sample table, run MS-DIAL in one or both polarities, find the internal standards, normalize every feature by one internal standard, and merge the polarities into one metabolome by InChIKey. Use when someone points at folders of raw hydrophilic metabolomics data and asks for the laboratory's standard analysis, or asks to normalize or merge such a run.
---

# Laboratory LC-MS metabolomics workflow

A person hands over one or two folders of raw data (positive, negative, or both) and
expects a normalized metabolome back. As in lipidomics, the work is mostly decisions:
which libraries annotate and with which retention-time use, which salt, which internal
standard normalizes, which sample belongs to which group.

Five MCP tools of the `msdial-interactive` server carry it, with the guided-run tools in
between: `msdial_lcms_metabolomics_check_inputs`, `msdial_lcms_metabolomics_write_contract`,
`msdial_lcms_metabolomics_resolve_standards`, `msdial_lcms_metabolomics_normalize` and
`msdial_lcms_metabolomics_merge`. If they are not among your tools, the MCP server is not
connected or the Interactive in use predates 0.5.46. Say so and stop.

**Ask generously the first time, in one message.** Gather every question into one
message, so the person answers once. **Every Console start waits for a yes**, and so does
a copy of raw data and each confirmed write (contract, normalize, merge). The tools
preview by default (`confirmed=false`) and act only with `confirmed=true`. A yes covers
the one action it was given for.

## A. Before starting

- The backend must be running: `msdial_interactive_status`, and
  `msdial_interactive_launch(open_browser=false)` if it is not.
- The Console needs the annotation candidate table and the automatic-RT local test
  (MsdialWorkbench#826); `check_inputs` refuses any other. Normalization runs in Python,
  so normalize's `--peak-ids-only` is not needed. Name the Console per analysis as
  `console_path`; never select it with `msdial_set_console_path`.
- Raw data and outputs stay off the C: drive and out of any code checkout.

## B. Look at the data before asking anything

Call `msdial_lcms_metabolomics_check_inputs(pos_path=..., neg_path=...)` with the folders
only; give one of them for a single-polarity batch. It reads and never writes. Report the
raw files per polarity, MS-DIAL intermediates already beside them, and the candidate
inputs beside the data (internal-standard texts, `.msp` and `.lbm2` libraries). Name the
libraries found, but never propose one: which library matches this chromatography is the
person's choice.

## C. Ask everything in one message

`questions` lists what is open. Ask all of it at once:

- **What the analysis is for** (`analysis_purpose`): the question, the comparison,
  whether annotation or comparative profiling is central, the outputs needed. Never fill
  it in yourself.
- **The annotation libraries** (`annotation`), highest priority first. Offer the three
  presets and name each slot's file; a slot takes one path for every polarity or
  `{"Positive": ..., "Negative": ...}`:
  - `single_msp` (slot `msp`): one MSP, no retention time. What most users and the
    reanalysis agent run.
  - `campaign` (slots `lbm`, `msp`, optional `in_silico`): the LBM2 for the few lipids,
    the MSP searched strictly and then mildly, and an in-silico MSP mildly, all without
    retention time.
  - `lab_rt_tiers` (slots `in_house`, `predicted_rt`, `no_rt`): the laboratory's own
    standards with measured RT and MS/MS at 0.5 min, a predicted-RT MSP at 2.0 min, and an
    MSP of MS/MS without RT, no RT filter.
  Or the person lists the libraries: `{"libraries": [{"kind": "msp", "path": ...,
  "thresholds": "standard"|"strict"|"mild", "retention_time": "none"|"use",
  "rt_tolerance": ..., "label": ..., "library_kind": "acquired"|"predicted"}, ...]}`.
  Each library's RT use is its own and no run-wide answer changes it. Any other key is
  refused rather than ignored, so a setting the preset does not take needs the list form.
- **What each MSP's spectra are**: acquired, or computed in silico (`library_kind`
  `predicted`, which MS-DIAL reports as in-silico evidence rather than a reference
  spectrum). The campaign's `in_silico` slot is predicted; any other slot may say so as
  `{"path": ..., "library_kind": "predicted"}`. Ask it of every library whose spectra the
  person has not described, the predicted-RT library included.
- **The salt or acid** in the mobile phase (`solvent_type`). Formic acid and ammonium
  formate read as `HCOONH4`, acetic acid and ammonium acetate as `CH3COONH4`, ammonium
  bicarbonate as `NH4HCO3`. It sets the negative adduct the merge prefers besides
  `[M-H]-` (formate `[M+HCOO]-`, acetate `[M+CH3COO]-`, none for bicarbonate) and the
  negative LBM2 records MS-DIAL searches. Read the mapping back.
- **The internal-standard texts** (`is_text_pos`, `is_text_neg`): tab-separated, with
  Name, MZ, RT, Adduct and Ontology, MZ being the adduct's m/z.
- **The normalizing standard** of each polarity (`normalizing_standard_pos`,
  `normalizing_standard_neg`), one name from that polarity's text.
- **The analysis root** (`analysis_root`): `pos`, `neg`, `inputs` and `quant` are made
  inside it. Not a raw-data folder, a code checkout or a temporary folder.
- **Where MS-DIAL reads the raw data** (`raw_location`), with this sentence: *MS-DIAL
  writes its per-file `.dcl`, `.pai2` and tag files and the alignment container beside
  the raw files it reads, the one-file diagnostic included, and no output setting
  prevents it.* `in_place` or `copy`.
- The Console, and the thread count if they care.

Say in the same message what is set without asking: the internal-standard RT tolerance
(0.5 min); the method (one-file peak-height diagnostic, smoothing
`TimeBasedLinearWeightedMovingAverage`, automatic RT correction with 12 anchors and the
1.5 min local window, annotation candidates, QA); the thresholds (`standard`: dot
products 0.6/0.6/0.8 and 3 matched peaks; `strict` and `mild`: the reanalysis campaign's
MSP tiers); the internal-standard text row searched at priority 1, below every library;
and the merge defaults below.

## D. Make the copy, if they chose one

As in lipidomics: state source, destination, file count and size, and that the copy is
kept; copy only on a yes; compare names and sizes; check the copy again before the
contract.

## E. Check the answers, agree the sample table, write the contract

Call `msdial_lcms_metabolomics_check_inputs` again with every answer. Report the blockers
and warnings, the libraries as they will be searched (`annotation.libraries`: priority,
label, thresholds, RT use, library_kind, file names with sha256 in `annotation.files`), the
salt with its preferred adducts, and the normalizing standards. A library searched with RT
is read for its records' retention times (`annotation.retention_time_census`): MS-DIAL
matches a record without one on MS/MS alone, so none refuses the library and some missing
is a warning to read out. Library paths and the analysis root must be ASCII, and raw data
in `.d` or `.raw` folders need a Console built from MsdialWorkbench#739 with its build
record; the check says so before the contract fixes the Console.

Show `paired_sample_table`: file, type (Sample, QC, Blank, Standard; a reference
material such as NIST SRM 1950 plasma is a Standard), class, analytical order, batch,
dilution factor, acquisition type and included. Read `class_proposal` and
`order_proposal` aloud. Corrections go in `sample_table` by stem, every sample listed.

On their yes, call `msdial_lcms_metabolomics_write_contract(..., check_digest,
sample_table_confirmed=true, sample_table=<corrections>, confirmed_by=<who>)`. It
previews; check it, then call again with `confirmed=true`. An existing contract there
refuses even the preview: read `existing_contract` to the person, and replace it
(`replace_existing=true`) only if they want it replaced.

## F. Diagnostic and production run, one polarity at a time

Plan with `msdial_guided_analysis_plan(input_path=<the polarity's raw folder>,
answers={"lab_lcms_metabolomics_contract_path": ..., "ion_mode": "Positive"})`. The
contract fixes the libraries with their RT settings and thresholds, the salt, the
internal-standard library, the sample table, the output and export folder, the Console
and the template; `lab_lcms_metabolomics.deviations` must be empty unless the person
asked for a change, and every deviation is read to them.

1. Confirm, then `msdial_start_peak_count_diagnostic(..., confirmed=true)`: one file at
   zero threshold, without annotation, writing intermediates beside that file.
2. `msdial_estimate_peak_height(job_id)`; show the threshold with its count, step and
   file, and accept it with the person.
3. `msdial_prepare_guided_analysis` with `"minimum_peak_height": <accepted>`. Show the
   command and, from the method and `msp_annotator_settings.tsv`, each library with its
   priority, RT use and tolerance, the `Solvent type` line, the internal-standard row and
   the threshold.
4. On a yes, `msdial_start_guided_analysis(..., confirmed=true)`, then
   `msdial_complete_guided_analysis(job_id, internal_standards=<the plan's
   post_run_actions.internal_standards>)`.

One Console at a time. A start that returns nothing within 120 s may still have started:
look with `msdial_interactive_status`, never start it again blindly. A failed automatic
RT correction stops the run; running uncorrected is the person's decision.

A predicted-RT MSP can be large: the run loads every library in memory, and MS-DIAL keeps
a copy of the loaded libraries beside the project (`*_Loaded.msp2.dbs`). Say so before
the run when a library is over a few hundred MB.

## G. Find the internal standards

`msdial_lcms_metabolomics_resolve_standards(contract_path, pos_job_id, neg_job_id)` is
read-only. Each standard is looked for at its adduct's m/z within the internal-standard
RT tolerance: A, a library annotated it by its name; B, its own text row annotated it; C,
its row matched but another annotation won; D, any spot there. C and D need the person's
word.

Read the normalizing standard first (`summary.normalizing_standard`): tier, alignment
ID, MS-DIAL's name, Δm/z and ΔRT, files without a height (`zero_height_files`: a Sample
or QC without it makes it invalid; `tolerated_zero_height_files`: Blank or Standard
injections, which may lack it), `gap_filled_files` (injections where MS-DIAL did not
detect it and filled the gap, from the run's peak-ID matrix), its CVs and
`valid_alternatives`. Then the other standards with `cv_percent_qc` and
`cv_percent_sample_qc`, the run's stability in one number each, and every warning. The
person accepts a C or D normalizing standard (`accept_searched`) or takes another listed
spot (`accept_spot`); never decide it yourself.

## H. Normalize each polarity

`msdial_lcms_metabolomics_normalize(contract_path, job_id, resolution_digest,
accept_searched=[...], accept_spot={name: alignment ID})` previews:
`normalized = height / height of the standard in that injection x the standard's mean
height over the Sample and QC injections`, times the injection's dilution factor. Read
the standard, its mean and the injections it averages over, each injection's multiplier,
the injections left at raw height with why (`raw_height_reasons`: a Blank or Standard
without the standard, with it undetected, or below 10% of the lowest Sample or QC height)
and any dilution factor. A Sample or QC without the standard refuses. So does one where
MS-DIAL did not detect it: its height is a gap fill, and dividing by it scales every
feature of that injection. Read those heights to the person; normalize them only if they
accept (`accept_gap_filled=true`). Read every warning: each other standard's spot is either
dropped by the merge as an internal standard (a library or its own text row annotated it
as the standard) or kept as its own row. On a yes, call again with `confirmed=true`: it writes
`<root>/quant/NormalizedHeight_<Pos|Neg>.txt` in MS-DIAL's layout (the per-class Average
and Stdev columns empty, since MS-DIAL computed them from heights), checks every cell
against the formula, and draws figure 04 into `<root>/quant/qa_figures`. MS-DIAL's own
internal-standard normalization averages over every file, Blank included, and is not
used.

## I. Merge

`msdial_lcms_metabolomics_merge(contract_path)` previews the counts. Every polarity of
the contract must be normalized. Dropped first: unannotated rows and every internal
standard. A spot that was only searched for a standard nobody confirmed stays a feature,
kept as its own row outside the InChIKey groups, so that a labelled standard never takes
the place of its endogenous compound; read `unconfirmed_standard_spots` to the person,
with any row each shares a first block with. Options, read to the person:
- `reference_matched_only`, default true: only rows MS-DIAL calls Ref. matched.
- `preferred_adducts_only`, default false: only `[M+H]+`, `[M-H]-` and the salt's
  negative adduct.

Rows sharing the InChIKey's first block are one compound, within and across polarities;
one is kept: a preferred adduct first, then matched on MS/MS, then the larger mean
normalized height over Sample and QC, then the higher library priority, the higher total
score, positive before negative. A labelled standard shares its first block with the
endogenous compound, which is why standards go first. A row without an InChIKey is kept
as its own; say how many (`counts.kept_without_inchikey`). The first block joins
stereoisomers, and the intensity can pick an MS/MS-only match at another RT over an
RT-confirmed one: read `possible_isomers` (groups whose rows differ in stereo block or lie
more than 0.5 min apart) to the person, with the row kept and the rows removed.

On a yes, `confirmed=true` writes into `<root>/quant/<output_folder>` (default
`metabolome`): `metabolome.csv`, `metabolome_samples.csv`, `metabolome_removed.tsv` (each
removed row with its reason and, for a duplicate, the row kept) and `merge-report.json`.
Merges with other options sit side by side under other `output_folder` names. Data mining
of the result belongs to a separate data-mining tool, not here.

## J. Report

Say what ran, on which files, with which libraries and settings, and where the artifacts
are: each library by label, file name and sha256 (never its path in anything shared),
its priority, thresholds and RT use; the salt and its mapping; each polarity's threshold,
step and representative file; the normalizing standard's tier, what the person accepted
or took, its mean and the injections left raw; the other standards' QC CVs; the merge
options and counts. Name what was assumed rather than confirmed. When a step was skipped,
say so. The raw data, or its copy, is kept; deleting it is a separate confirmation.

## Private resources

A laboratory's MSP libraries (an in-house RT library, a predicted-RT library), LBM2,
standards texts and clinical raw data may be private. Use them where they are, locally.
Never copy them into a bundle, a log, a repository, an issue or a shared report, and
never put their paths in one: name a library by its file name and sha256. The contract
and its record name them by location (`sharing: local_only`) and stay on this computer.

## Not automated yet

- Reviewing annotations for false positives is manual, and curation in the GUI after the
  run does not reach these tools: they read the exports the run wrote. Normalize warns when
  the project was saved well after them; say so to the person.
- Worksets for the next batch are not saved for metabolomics contracts yet.
- A run not made under a contract cannot be resolved, normalized or merged by these
  tools; offer to run it under one.
