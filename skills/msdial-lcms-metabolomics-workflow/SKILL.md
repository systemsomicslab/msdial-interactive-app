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

It also reads each raw file's header with the configured raw-metadata extractor
(`raw_headers`): every file up to 200 per polarity, past that the first and the last, within
240 s for the whole check, so that it returns within the tool's time limit. A file not read
in time is asked about, and checking again reads it first, on its own, so a file that hangs is
then kept as a failed read. Report per polarity the methods, the
isolation target counts, the MS2 collision energies, the instrument, the acquisition types
proposed and from what (`proposed_by`), the files that could not be read or not in time, and
whether the analytical order follows the acquisition start times. Only a DDA or SWATH verdict
of a file's own header (DIA with isolation windows is SWATH) settles its type; an AIF verdict
(DIA without isolation) is asked, because a SWATH file whose sampled headers show no
isolation reads as AIF too. Read out every file the headers did not settle with its method,
isolation targets and collision energies from `raw_headers.per_file`. A header recording the
other polarity, or both, is a blocker: the files belong in another folder or out of the
batch. If `reader_created_files` names anything, a reader wrote beside the raw data: say so.
Without a configured extractor (`msdial_check_raw_metadata_extractor`), the type is asked,
never assumed.

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
    the MSP searched strictly and then mildly, and an in-silico MSP at the `in_silico`
    thresholds (0.05 Da, 0.5/0.5/0.5, at least 3 matched peaks) with its suggestions off,
    all without retention time. The in-silico slot is labelled `in-silico MSP`: the GLACIER
    evidence tier, citation and preprocessing record are the guided tiered profile's.
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
  Name, MZ (or MS-DIAL's Pos-m/z), RT and Adduct, MZ being the adduct's m/z; Ontology may
  be left out.
- **When one compound is found more than once** (`duplicate_rule`): `evidence`
  (recommended: the more reliable library match, the person's own RT and MS/MS before an
  MS/MS-only hit even if weaker, then the library's priority, the preferred adduct and the
  intensity), `intensity` (the preferred adduct, then the larger intensity) or `keep_all`.
- **What one compound is** (`inchikey_match`): the InChIKey's `first_block` (stereoisomers
  are one compound) or `first_two_blocks` (they stay apart).
- **The normalizing standard** of each polarity (`normalizing_standard_pos`,
  `normalizing_standard_neg`), one name from that polarity's text.
- **The acquisition type** (`acquisition_type_pos`, `acquisition_type_neg`: DDA, SWATH or
  AIF), only when it is among the questions: no extractor is configured; a header was not
  read (could not be, not in time, or a Bruker BAF folder the check never gives the reader);
  a header gives no type the Console reads; a header gives AIF; or the file lies between the
  first and the last of more than 200. Its prompt counts the files by why, and gives what
  the headers suggest only as a suggestion. The answer fills exactly those files. For AIF,
  say that the repository campaign runs AIF of one MS2 collision energy as SWATH (the Console
  deconvolutes it alike), that AIF of more than one MS2 collision energy over the polarity's
  AIF files (one file with several, or files with different ones, such as QCs at 40 eV
  beside samples at 20 eV) needs a Console with MsdialWorkbench#825, and that such a
  Console stops on AIF that records no energy; the check refuses either case and names each
  energy set with its files. SWATH or AIF data run as DDA give almost no MS/MS, so never
  answer it for the person.
- **Files whose own headers give different types** (`acquisition_type_mixed_pos`,
  `acquisition_type_mixed_neg`), only when asked: read the files of each type from the
  prompt. `as_read` keeps each file's header type; a header the person says is wrong is
  corrected in `sample_table`. Never answer it for them.
- **For capillary electrophoresis** (migration time on the RT axis), say that the template
  aligns within 0.1 min, which CE migration times usually exceed, and ask whether they want
  a wider `alignment_rt_tolerance` (minutes). The value is the person's; leave it unset
  unless they give one.
- **The analysis root** (`analysis_root`): `pos`, `neg`, `inputs` and `quant` are made
  inside it. Not a raw-data folder, a code checkout or a temporary folder.
- **Where MS-DIAL reads the raw data** (`raw_location`), with this sentence: *MS-DIAL
  writes its per-file `.dcl`, `.pai2` and tag files and the alignment container beside
  the raw files it reads, the one-file diagnostic included, and no output setting
  prevents it.* `in_place` or `copy`.
- The Console, and the thread count if they care.

Say in the same message what is set without asking: the internal-standard RT tolerance
(0.5 min); the alignment RT tolerance (the template's 0.1 min unless given); the method
(one-file peak-height diagnostic, smoothing
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
label, thresholds, RT use, library_kind; `annotation.files` lists each library's file by
label and polarity with its sha256), the
salt with its preferred adducts, and the normalizing standards. A library searched with RT
is read for its records' retention times (`annotation.retention_time_census`): MS-DIAL
matches a record without one on MS/MS alone, so none refuses the library and some missing
is a warning to read out. Library paths, raw-data paths and the analysis root must be
printable ASCII (raw paths also without commas or quotes), and raw data
in `.d` or `.raw` folders need a Console built from MsdialWorkbench#739 with its build
record; the check says so before the contract fixes the Console.

Show `paired_sample_table`: file, type (Sample, QC, Blank, Standard; a reference
material such as NIST SRM 1950 plasma is a Standard), class, analytical order, batch,
dilution factor, acquisition type and included. Read `class_proposal` and
`order_proposal` aloud. Corrections go in `sample_table` by stem, every sample listed.
Two polarities pair by name (S1_Pos with S1_Neg); files acquired under other names pair by
the words that set each polarity's files apart (A1 in 20230125_SWATH_A1_C_CE20_d1 and
20230208_SWATH_A1_A_CE20_d2), shown in `pairing` and each row's `paired_by`: read every pair
to the person. When the pairing fails or is wrong, they give `sample_pairs`
(`[{sample, positive_file, negative_file}]`). The merge pairs the columns by this table.
Paired by those words, where even one Sample pair's file names give both polarities one
class (case aside), every pair keeps its names' class, and each pair whose two classes differ
is a warning to read out. Only where no pair agrees (d2 against a re-injection's d2r2, or no
class at all) is each Sample pair's class proposed from the letters of its letter+number word
(A1 to D4 -> A), and never from a blank, QC or standard (Blank1, QC2, Std3); read that
warning, the classes and `class_proposal.polarity_disagreements` to the person.
Classes and acquisition types reach the Console's ASCII analysis file: give them in
printable ASCII without commas or quotes (a Japanese class name as a romanized word). The
acquisition type is DDA, SWATH or AIF, the words the Console reads; it reads any other as
DDA. Each row's type is its files' headers', or the person's answer where a header did not
settle it (`acquisition_type_basis`); read it out, and correct a row only on the person's
word. A paired row carries one type and one analytical order for both polarities. Where a
pair's two files are of different types (`acquisition_type_conflicts` gives both), the row is
proposed with no type and listed in `acquisition_type_required`: ask which it is and give
that row's `acquisition_type` in `sample_table`. The contract refuses every included row
without a type, so sending the table back as proposed never passes for the person's word.
Where the negative files were acquired in another order, read that warning.
`order_proposal` says whether the order follows the acquisition start times or the names.
Analytical and batch orders are ranks from 1. A raw file whose own name the Console cannot
carry may be left out (`included: false`); the contract refuses it included.

An AIF collision-energy blocker of the check counts every file of the polarity: leaving the
files of the other energy out (`included: false`, on the person's word) clears it, since the
contract judges the guards over the rows it runs. On their yes, call
`msdial_lcms_metabolomics_write_contract(..., check_digest, sample_table_confirmed=true,
sample_table=<corrections>, confirmed_by=<who>)`. It previews; read its `warnings` and
`acquisition_warnings` to the person (a file that appeared beside the raw data during its own
reads, AIF energy sets that differ), then call again with `confirmed=true`. An existing contract there
refuses even the preview: read `existing_contract` to the person, and replace it
(`replace_existing=true`) only if they want it replaced.

## F. Diagnostic and production run, one polarity at a time

Plan with `msdial_guided_analysis_plan(input_path=<the polarity's raw folder>,
answers={"lab_lcms_metabolomics_contract_path": ..., "ion_mode": "Positive"})`. The
contract fixes the libraries with their RT settings and thresholds, the salt, the
internal-standard library, the sample table with each file's acquisition type, the output
and export folder, the Console, the template and the alignment RT tolerance (the person's,
else the template's, alike for both polarities);
`lab_lcms_metabolomics.deviations` must be empty unless the person asked for a change, and
every deviation is read to them.

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
   post_run_actions.internal_standards>)`, called again while it answers
   `call_again: true` (each call waits at most 25 s).

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
accept (`accept_gap_filled=true`). Read every warning: each says what the merge will do with
another standard's spot, dropped as an internal standard (a library or its own text row
annotated it as the standard), dropped as unannotated or not reference matched, or kept as
its own row. On a yes, call again with `confirmed=true`: it writes
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

Rows of one compound (the contract's `inchikey_match`) are one, within and across
polarities, and the contract's `duplicate_rule` says which is kept (`evidence`,
`intensity` or `keep_all`); either may be given otherwise to one merge, which then reports
it in `deviations_from_contract`. A labelled standard shares its first block with the
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
its priority, thresholds and RT use; the salt and its mapping; each polarity's acquisition
types and what they came from (the raw headers, the person's answer, a row they corrected),
whether the order followed the acquisition start times, and the alignment RT tolerance;
each polarity's threshold, step and representative file; the normalizing standard's tier,
what the person accepted or took, its mean and the injections left raw; the other
standards' QC CVs; the merge options and counts. Name what was assumed rather than confirmed. When a step was skipped,
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
