---
name: msdial-lipidomics-workflow
description: Run an LC-MS/MS lipidomics batch with internal standards end to end through MS-DIAL Interactive - check the inputs, agree the sample table, run MS-DIAL in both polarities with the laboratory's LBM2, salt and internal-standard libraries, find each internal standard, normalize to concentration from quant_utility_table.xlsx, and merge positive and negative into lipid_metabolome.csv. Use when someone points at folders of positive and negative raw lipidomics data and asks for a quantified lipidome, asks to normalize or merge such a run, or wants to run the public MS-DIAL lipidomics demo.
---

# Laboratory lipidomics workflow

A person hands over two folders of raw data, positive and negative, and expects a
quantified lipidome back. The work is mostly decisions, not computation: which library,
which salt, which retention-time window, which sample belongs to which group, which
standard quantifies which class.

Five MCP tools of the `msdial-interactive` server carry it, with the guided-run tools in
between: `msdial_lipidomics_check_inputs`, `msdial_lipidomics_write_contract`,
`msdial_lipidomics_resolve_standards`, `msdial_lipidomics_normalize` and
`msdial_lipidomics_merge`. If they are not among your tools, the MCP server is not
connected or the Interactive in use predates 0.5.38. Say so, point the person to
Interactive's setup (`scripts/setup_claude.py`, or `Setup MS-DIAL for Claude.cmd` on
Windows) and its tutorial (`docs/tutorials/lab-lipidomics.md`), and stop.

**Ask generously the first time, in one message.** A laboratory dataset has an analyst
behind it who can answer in a word, and reaching a correct result safely matters more
than reaching it in few turns. This is the opposite of repository reanalysis, which has
to run systematically with nobody to ask. Gather every question into one message, so the
person answers once rather than ten times.

**Then save it.** The contract fixes the answers for both polarities of this batch; the
workset carries the method to the next batch (step L). What carries is the method; what
does not carry is anyone's agreement about a particular set of files.

**Every Console start waits for a yes, and so does a copy of raw data.** That is each
diagnostic, each production run and each normalize. The tools preview by default
(`confirmed=false`) and act only with `confirmed=true`. A yes covers the one action it
was given for, and a yes for one run does not carry to the next.

**The answers are the person's.** Never fill in the analysis purpose, the library, the
salt, a tolerance, the workbook, the sample table or a standard's decision yourself, not
even from a tutorial, an earlier batch or a file name. Propose where the tools propose,
and let the person confirm.

## What a batch needs

- **Two raw-data folders**, one per polarity, holding the files MS-DIAL reads (a SCIEX
  `.wiff` with its `.wiff.scan`, a `.d` or `.raw` folder whole, mzML).
- **An LBM2 library** built for this chromatography. A laboratory that has none can use
  the official one in Interactive's library catalog, `lipidomics` (Zenodo record
  21904324, about 785 MB): `msdial_download_official_library(catalog_id="lipidomics")`
  previews, and downloads only with the person's yes (`confirmed=true`). Whether its
  retention times fit their chromatography is theirs to judge.
- **The salt** in the mobile phase.
- **A text library of internal standards** per polarity, or one file holding both.
- **`quant_utility_table.xlsx`**: sheet 1 the standards and their amounts, sheet 2 which
  standard quantifies which class, sheet 3 which class and adduct each polarity keeps.
  A laboratory without one can start from Interactive's example,
  `examples/lab-lipidomics/quant_utility_table.xlsx` (its `README.md` explains the three
  sheets), and edit its own copy in Excel. You never edit a workbook.
- **An MS-DIAL Console** new enough (step A).

The public demo is Zenodo record 10.5281/zenodo.10957708 (CC BY 4.0), file
`metabolomics_hands-on.zip`, which holds `metabolomics_hands-on/data2.zip`; that unpacks
to `data2/` with `pos/` and `neg/` (mouse plasma, SCIEX), the two EquiSPLASH standard
texts and an LBM2. Its workbook is Interactive's example. The person downloads and
unpacks it; if they ask you to, state the source, the size (773 MB) and the destination,
and wait for their yes. The tutorial lists the answers the MS-DIAL team gave for it;
they are an example, and the person still answers each question.

## A. Before starting

- The backend must be running: `msdial_interactive_status`, and
  `msdial_interactive_launch(open_browser=false)` if it is not. When an older backend
  holds the port (`compatible: false`), `msdial_interactive_restart` previews; restart
  only on the person's yes.
- A laboratory lipidomics run needs a Console that offers `normalize --peak-ids-only`
  (MsdialWorkbench#830), the annotation candidate table and the automatic-RT local test
  (MsdialWorkbench#826). `check_inputs` refuses any other. `msdial_check_console_path`
  lists the Consoles it finds; prefer one whose `provenance_status` is `verified`.
- Where to get one: the official MS-DIAL Console prerelease 5.5.2610xx or later (the
  exact tag is still to be announced in `docs/tutorials/lab-lipidomics.md`);
  `msdial_check_official_console_releases` shows the newest stable and prerelease
  Console packages. Releases dated 2026-08-20 and earlier lack both. Otherwise, from a
  local clone of MsdialWorkbench with the .NET SDK, `msdial_check_local_console_source`
  compares it with `origin/master`, and `msdial_build_console_from_local_source(...,
  select_after_build=false)` previews the build, which runs only on the person's yes.
- The Console is named per analysis, as `console_path`, and the contract fixes it for
  every run and normalize. Never select it with `msdial_set_console_path`, and do not let
  a build select it: that changes the shared settings other work reads.
- The analysis root is never a code checkout or a temporary folder; the check refuses
  both. A system drive or the user profile is the person's call; the check says so rather
  than refuses. Short paths outside cloud-synced folders keep Windows path limits and
  file locking out of the way.

## B. Look at the data before asking anything

Call `msdial_lipidomics_check_inputs(pos_path, neg_path)` with the two folders only. It
reads and never writes. For each polarity it reports the raw files with their format and
vendor, the polarity the folder and file names say, and any MS-DIAL intermediates
already there (an earlier run read the data in place). It also lists files beside the
data that look like inputs: internal-standard texts, workbooks, Excel lock files and
`.lbm2` libraries.

Report this before asking anything. A person can correct a misreading immediately, and
cannot correct a question they do not yet understand. Name the `.lbm2` files found, but
never propose one: a library lying beside old data is often an older build, and the
library is the person's choice.

## C. Ask everything in one message

`questions` lists what is still open. Ask all of it at once, each with what B found that
bears on it:

- **R0. What the analysis is for.** The question, the biological comparison, whether
  annotation or comparative profiling is central, the outputs needed. It is recorded as
  `analysis_purpose` and steers the sample table and the report. Never fill it in
  yourself.
- **R1. The LBM2** built for this chromatography. A laboratory keeps variants per
  gradient, and the wrong one annotates at the wrong retention times without a warning.
  If they have none, offer the official one (see "What a batch needs").
- **R2. The salt** in the mobile phase (see "The salt" below).
- **R3. The annotation RT tolerance**, in minutes: how far from the LBM2's retention time
  an annotation is accepted. Usually 0.1–3.
- **R4. The internal-standard texts**, positive and negative. They are tab-separated,
  with Name, MZ, RT, Adduct and Ontology (InChIKey, Formula, SMILES and CCS may follow),
  and MZ is the adduct's m/z. One file holding both polarities may be named for both; it
  is split by the adducts' charge.
- **R5. The quant workbook**, `quant_utility_table.xlsx`: sheet 1 the standards and
  their amounts, sheet 2 which standard quantifies which class, sheet 3 which class and
  adduct each polarity keeps. It is read as last saved and never edited.
- **R6. The amount column** of sheet 1: F (pmol/mg tissue), G (pmol/10^6 cells) or H
  (pmol/µL plasma). It sets the unit.
- **R7. The analysis root.** `pos`, `neg`, `inputs` and `quant` are made inside it. It
  must not be a raw-data folder, a code checkout or a temporary folder.
- **Where MS-DIAL reads the raw data** (`raw_location`), with this sentence: *MS-DIAL
  writes its per-file `.dcl`, `.pai2` and tag files and the alignment container beside
  the raw files it reads, the one-file diagnostic included, and no output setting
  prevents it.* The choice is between the folders as they are (`in_place`) and a copy
  elsewhere (`copy`). The check asks it as a question, and the contract records the
  answer.
- The Console, if A did not settle it, and the thread count if they care.

In the same message, say what is set without asking, so an objection costs one word:
- **Standards:** a standard the LBM2 annotated is accepted up to R3 from the RT in its
  text (up to 0.5 min when R3 is smaller), one within 0.5 min first. A farther one
  needs their confirmation when another spot lies within 0.5 min. Its own text row,
  and any other spot at its m/z, count only within 0.5 min, separately from R3: a
  wider window admits other spots at the standard's m/z. They may take any other
  listed spot instead.
- **Peak height:** tuned on a one-file zero-threshold diagnostic. Smoothing is
  `TimeBasedLinearWeightedMovingAverage`.
- **RT correction:** automatic, with 12 anchors and the 1.5 min local window. Blanks are
  interpolated by the confirmed analytical order.
- **LBM cutoffs:** 0.1 for weighted, simple and reverse dot product, the values of
  Interactive's laboratory lipidomics template.
- **Merge:** rows that are not reference matched are dropped. Before sheet 3 selects,
  a negative PG row that positive mode annotates as BMP becomes BMP, and a negative PE
  O- row that positive mode annotates as PE P- takes the PE P- name (step K); sheet 3
  should therefore take BMP and EtherPE from negative `[M-H]-` only, and the check asks
  about a sheet 3 that does not (step E). Every adduct sheet
  3 selects is kept, with likely double counts reported; a selected class whose
  standard is not in its polarity goes to `lipid_metabolome_not_quantified.csv`. The
  statistics tables take the Sample and QC files, never a Blank.
- **Dilution:** a sample's dilution factor multiplies its concentrations.

### The salt

The salt decides which negative-mode adduct the LBM2 searches: acetate, formate or
bicarbonate. It changes nothing in positive mode and nothing in the internal-standard
library. MS-DIAL reads only its own three names and silently keeps CH3COONH4 for
anything else, so map the answer and read it back:
- ammonium acetate, NH4OAc, acetic acid, 酢酸アンモニウム → `CH3COONH4`
- ammonium formate, formic acid, ギ酸アンモニウム → `HCOONH4`
- ammonium bicarbonate, ammonium hydrogen carbonate, 重炭酸アンモニウム,
  炭酸水素アンモニウム → `NH4HCO3`

An answer naming two anions ("ammonium acetate with formic acid") is refused; ask which
one is the buffer salt. The wrong salt does not fail loudly. PC n:x [M+HCOO]- and
PC (n−1):x [M+CH3COO]- have the same m/z, so under the wrong salt PCs are lost and
nothing says so.

Once the texts and the workbook are named, `salt_evidence` sets the answer beside two
things: the adducts the negative standards use, and the negative adducts sheet 3
selects. It also lists `salts_consistent_with_evidence`. Read it back with the mapping.
- A selected sheet-3 row the salt would drop is a blocker: that class could never be
  annotated.
- A standard whose adduct the salt drops is only a warning, because its text row still
  finds it.

If the person does not know the salt, the evidence narrows it but does not decide it;
ask them to find it in the method record. There is no salt probe for laboratory runs.

## D. Make the copy, if they chose one

Copying is a confirmation of its own. Before it, state the source, the destination, the
file count and size, and that the copy is kept afterwards. Copy only on a yes, never on
your own initiative.
- Copy the raw files with their companions: a `.wiff` with its `.wiff.scan`, a `.d` or
  `.raw` folder whole. Leave out intermediates already beside them.
- Then compare names and sizes with the source.
- Nothing is moved; the originals are only read.

From here on, `pos_path` and `neg_path` are the folders MS-DIAL will read, with
`raw_location="copy"`. The check binds the folders by where they are, so check the copy
again before the contract.

## E. Check the answers

Call `msdial_lipidomics_check_inputs` again with every answer. It is still read-only.
Report:
- `blockers` (each one stops the contract) and `warnings`.
- The salt as mapped, with its evidence.
- The workbook: the amount column's header and unit, the sample amount behind it
  (row 4), and any note below the standards. Read the sample amount aloud, because every
  concentration scales with it. If it is wrong, the person corrects and saves the
  workbook and you check again. Never edit it.
- `expected_resolution`: for each polarity, the classes that can only come out NOT
  QUANTIFIED, because the standard sheet 2 names for them is not in that polarity's
  text. It also names the standard that covers "Any others".
- Standards prepared but used by no class, and sheet-3 classes with no MS-DIAL class
  name.
- `merge_selection_warnings`: sheet 3 selects BMP or EtherPE in positive mode, or does
  not select BMP `[M-H]-` or EtherPE `[M-H]-`. The merge's reconciliation takes both
  classes from negative `[M-H]-`, so a positive selection counts the class twice, and
  without `[M-H]-` the renamed rows are lost. While there is any, the check asks
  `merge_selection_accepted` and is not ready for a contract. Read each warning, and
  say why it must be settled now: the contract binds the whole workbook, so changing
  sheet 3 after it needs a new contract and new MS-DIAL runs of both polarities. Then
  either the person changes sheet 3 (BMP and EtherPE positive adducts FALSE, BMP
  `[M-H]-` and EtherPE `[M-H]-` TRUE), saves it, and you check again; or they keep it
  as it is, and you pass `merge_selection_accepted=true`, which the contract records.
  Never edit the workbook yourself, and never answer the question for them.

Keep `check_digest`: the contract refuses any input that changed after the person saw
this.

## F. Agree the sample table, then write the contract

**This one blocks.** `paired_sample_table` is one table for both polarities, paired by
file stem. Show every column that reaches MS-DIAL: positive and negative file, type
(Sample/Blank/QC), class, analytical order, batch, dilution factor, acquisition type and
included. `class_proposal` and `order_proposal` say where the class and the order came
from; read both aloud, with the alternatives the class proposal rejected. A metadata
table given as `sample_metadata_path` only corroborates, so report where it disagrees.

- **Analytical order** decides what "injection order" means in every drift plot. Here it
  also decides how the RT correction interpolates the blanks.
- **Dilution factor** is 1 because nothing said otherwise. A factor other than 1
  multiplies that file's concentrations (normalize `-d`), so a wrong one scales them.
- **Acquisition type** is DDA unless the files or the metadata say otherwise.

Corrections go in `sample_table` by stem, with every sample listed. `included=false`
drops a sample from both polarities.

On their yes, call `msdial_lipidomics_write_contract(..., check_digest,
sample_table_confirmed=true, sample_table=<corrections>, confirmed_by=<who>)`, with
`merge_selection_accepted=true` when they kept a sheet 3 the check warned about. When
the check was filled from a workset (`filled_from_workset`), pass the same
`workset_id` too: the digest binds what the workset answered. It previews. Check that
it holds the table they confirmed, then call again with `confirmed=true`. It writes
`<root>/inputs/`:
- the contract;
- `is_pos.txt` and `is_neg.txt`, in the column order MS-DIAL reads;
- `standards_template.tsv` and `quant_rules.tsv`;
- the record.

If it refuses because an input changed, check again and show the person the new result;
never pass a digest they have not seen. The contract and the record hold the LBM2's
path, so they are local only.

If a contract is already at that root, even the preview is refused, and the refusal
holds `existing_contract`: when it was written and the diagnostics, runs, normalizes
and merges recorded under it. Read it to the person. Replacing it leaves every one of
those unusable, to be made again. Only if they want it replaced, preview again with
`replace_existing=true`, and confirm that preview as usual. Otherwise choose another
analysis root.

## G. Diagnostics, positive then negative

Plan each polarity with `msdial_guided_analysis_plan(input_path=<its raw folder>,
answers={"lab_lipidomics_contract_path": ..., "ion_mode": "Positive"})`. The contract
fixes the salt, the LBM2, both tolerances, the internal-standard library, the sample
table, the output and export folder, the Console, and the parameter template and LBM
query list; neither an answer, a workset nor `workflow_overrides` can change them.
Any other answer is applied and listed in `lab_lipidomics.deviations`, which must be
empty unless the person asked for that change; read every deviation to them. The
diagnostic is refused while the plan has a blocker other than the missing threshold
(`diagnostic_blockers`), a raw folder other than the contract's included.

The diagnostic runs one file at zero threshold, without annotation. The file is a QC
near the middle of the analytical order, or a non-blank sample there if there is no QC.
It writes intermediates beside that one raw file.
1. Confirm it, then call `msdial_start_peak_count_diagnostic(..., confirmed=true)` with
   the same answers.
2. `msdial_estimate_peak_height(job_id)` proposes the threshold. It uses the instrument
   family's step (100 QTOF-type, 1,000 FT) and takes the highest multiple whose
   estimate stays at or above 3,000 peaks, or 0 when the diagnostic found 6,000 or
   fewer. Leave `threshold_step` at 0.
3. Show the threshold with its count, step and representative file, and accept it with
   the person. The record beside the contract keeps the estimate.

Then do the negative polarity the same way.

## H. Production runs, one after the other

Call `msdial_prepare_guided_analysis` with the same answers plus
`"minimum_peak_height": <accepted>`. It writes the bundle into the polarity's output
folder without running anything. Show the command and what matters in the method file:
the `Solvent type` line, the LBM2 by name, both RT tolerances, the internal-standard
annotator, the threshold, the smoothing and the RT correction.

On a yes, call `msdial_start_guided_analysis(..., confirmed=true)`. Then call
`msdial_complete_guided_analysis(job_id, internal_standards=<the plan's
post_run_actions.internal_standards>)`, which waits, validates mzTab-M and writes QA. A
run of a few files takes minutes; say so.

- **One Console at a time.** Start the negative run only after the positive one has
  finished.
- **A silent start may still have started.** If a start returns nothing within the MCP's
  120 s, look for the job with `msdial_interactive_status`. Never start it again
  blindly.
- **A failed RT correction stops the run.** If the log says "Automatic alignment RT
  correction failed", stop and report it. Running again without the correction
  (`execute_automatic_rt_correction=false`, recorded as a deviation) is the person's
  decision and needs a new confirmation.

Until both polarities are normalized, nothing may be moved: neither what the run wrote
beside the raw data, nor the project and its library copy in the output folder.
Normalize loads them from there.

## I. Find the internal standards

Normalization divides by an internal-standard peak. If the wrong peak is taken, every
lipid of its classes is wrong by the same factor, and nothing downstream shows it.

Call `msdial_lipidomics_resolve_standards(contract_path, pos_job_id, neg_job_id)`. It is
read-only, and first checks that the run belongs to the contract and was annotated. No
`LbmDB` annotation at all means the LBM2 was not loaded, which MS-DIAL does not say;
stop there.

Each standard is then looked for at the m/z of the adduct its text names, so a spot of
another adduct never competes. There are four tiers:
- **A:** the LBM2 annotated it, within the annotation RT tolerance, or the
  internal-standard tolerance when that is wider. A sum-level name (PC 33:1(d7))
  counts; a chain-level name must agree chain for chain, so an isobaric endogenous
  lipid is not the standard.
- **B:** its own text row annotated it as a reference match, within the
  internal-standard tolerance.
- **C:** its text row matched, but another annotation won the spot, or the match is
  below the score cutoff.
- **D:** a spot in the m/z and RT window, annotated as anything or nothing.

The first tier with a member decides. In A, a spot within the internal-standard
tolerance comes first. Then the most intense spot over the sample and QC files wins. C
and D are the person's to accept. So is an A spot outside the internal-standard
tolerance when another spot lies inside it (`needs_confirmation`): a stale text RT, or
another lipid named as the standard.

Read each standard aloud: its tier and alignment ID, MS-DIAL's name for the spot, Δm/z
and ΔRT, an adduct label that disagrees with the text, other spots of the same standard
at another adduct or RT, and files where the spot has no height. A Blank or a
Standard-type file without the standard is allowed and left empty; a sample or QC
without it makes the standard invalid. Read its `valid_alternatives` too: the other
spots listed for it (`resolution_table`) that have a height in every sample and QC.
Then read the lists:
- `unresolved_expected`: standards that belong to the other polarity. Say which, so the
  rest stand out.
- `unexpected_unresolved`: standards that were not found, are invalid, or are contested.
  These matter.
- `needs_confirmation`: the C and D standards, and any A standard taken from outside
  the internal-standard tolerance while a spot lies inside it.
- `classes_not_quantified_if_unchanged`.

The person accepts or declines each standard in `needs_confirmation`, and accepts each
unexpected unresolved one as unresolved. For any standard, an invalid or contested one
included, they may instead take one of its valid alternatives. A declined or unresolved
standard empties its classes. Never decide these yourself.

## J. Normalize each polarity

Call `msdial_lipidomics_normalize(contract_path, job_id, resolution_digest,
accept_searched=[...], accept_unresolved=[...], accept_spot={standard: alignment ID})`.
`accept_spot` holds the alternatives the person took; each standard takes one of the
three decisions. It previews:
- the standards table (StandardName, TargetClass, Concentration, PeakID, AssignName);
- the unit;
- the exact command, and why each flag is there:
  - `--peak-ids-only`, always: a standard is taken from its alignment ID or not at all,
    never looked up by name;
  - `--allow-unresolved-standards` when a standard stays unresolved, as expected or as
    decided;
  - `--allow-mismatched-peak-ids` when MS-DIAL's stored name for a chosen spot differs
    from the standard's;
  - `-d` when a dilution factor differs from 1.
- `warnings`: the Console is not the build the contract recorded (it was rebuilt or
  replaced). Say so before the confirmation; both builds are recorded with the result.

Confirm each polarity separately, then call with `confirmed=true` and poll
`msdial_interactive_job(job_id)`. The job compares normalize's own report with the table,
and recomputes every quantified cell from the Height matrix the same call wrote. Report
`verified`, `quantified_rows` and `failed_rows`. A row that fails is set aside by the
merge, never repaired by hand.

State what MS-DIAL computed in the result:
- The spot of a standard sheet 2 uses is its amount times the file's dilution factor
  (its amount when no factor differs from 1). A prepared standard that sheet 2 does not
  use is quantified like any other lipid.
- Every other spot is amount × its height / the standard's height in that file, times
  the dilution factor. The standard is its class's own, or "Any others". A spot that is
  not reference matched goes to "Any others".
- A class whose standard did not resolve is emptied, with a `NOT QUANTIFIED` comment. A
  file where the standard has no height is empty for its classes.
- Blanks are normalized like samples; nothing is subtracted.
- An accepted C or D standard, or a C or D spot taken instead, is named as the
  standard in MS-DIAL's own exports (annotation method "internal standard
  assignment").

Both matrices are written to `<root>/quant` as `NormalizedHeight_{Pos,Neg}.txt` and
`Height_{Pos,Neg}.txt`. A concentration is a height divided by a standard, and nobody can
check the division with only one side of it, so both are kept and both are reported.

## K. Merge the polarities

Each lipid class is kept from the polarity and adduct that sheet 3 selects. That is a
rule table, not something the data decides. `resources/LbmQueries.txt` is the annotation
search list, not a selection, and `lipids_class_list.txt` is not an input.

The outputs are those of metaboservice, the MS-DIAL laboratory's earlier program for this
merge, so that its R script, `metabolomics_service.R`, and anything else built on those
tables reads them unchanged. Where this section says "as metaboservice did", that is the
reason.

The merge runs in three stages:
1. **Row filters.** A row is set aside when it is unannotated, an internal standard, not
   reference matched, excluded by name (`w/o`, `RIKEN`, `low score: `, `no MS2: `), or
   when its Comment starts with a lower-case `x`. The name and Comment tests are
   metaboservice's, on the cells as written: a Comment starting `X` or ` x` keeps its row.
   If the person marked rows to exclude, check they used a lower-case `x` as the first
   character.
2. **Positive/negative reconciliation** (the MS-DIAL laboratory's rules). Two classes are
   named differently by the two polarities, and this stage only renames rows; it keeps
   and drops nothing.
   - **BMP.** BMP is annotatable only in positive mode; negative mode annotates a BMP as
     PG. Take a kept positive BMP row and replace BMP with PG in every `|` part of its
     name. A kept negative PG row with exactly that name, within 0.1 min
     (`Average Rt(min)`), becomes BMP: name and Ontology.
   - **PE P-.** Negative mode annotates PE P- (plasmenyl) and PE O- (plasmanyl) both as
     PE O-. Take a kept positive PE P- row and spell its ether chain `P-x:y` as
     `O-x:(y+1)` in every part (`PE P-18:0_16:0` is `PE O-18:1_16:0`, `PE P-34:1` is
     `PE O-34:2`). A kept negative EtherPE row with exactly that name, within 0.1 min,
     takes the PE P- name.
   - Names must match exactly; there is no fallback to the sum composition. Of several
     negative candidates, the smallest |ΔRT| is taken, then the higher mean intensity.
     When two positive rows claim one negative row, the closer one wins (then the more
     intense), and the other takes its next candidate that no closer positive row holds.
     It is left unmatched only when every candidate went to a closer row. The claims are
     settled together, so the pairs do not depend on the order of the rows.
   - When positive mode also annotates a renamed negative row's own name within the
     tolerance (a positive PE O- or PG of exactly that name), the two positive
     annotations disagree. The row is still renamed as the rule says, and the pair is
     reported as ambiguous, kind `conflicting positive evidence`.
   - The renamed row's Comment says what it was renamed from, by which positive row, and
     at what ΔRT. Its value stays the negative `[M-H]-` measurement, normalized as it was
     annotated. A PG renamed BMP was therefore normalized against the PG standard; the
     report warns when sheet 2 names another standard for BMP.
3. **Sheet 3's selection**, then a valid concentration. A positive BMP or PE P- row with no
   negative match is in the lipidome only if sheet 3 selects its positive adduct. The
   example workbook's sheet 3 does not, so it is left out.

The reconciliation is on by default, at the contract's tolerance (0.1 min).
`reconcile_polarities=false` or another `reconciliation_rt_tolerance` is applied only
when the person asks, and is recorded as a deviation from the contract.

`msdial_lipidomics_merge(contract_path)` previews. Read out:
- the kept rows from each polarity, the dropped rows by reason, and selected pairs that
  have no rows;
- NOT QUANTIFIED rows by class;
- `possible_double_counts`: one lipid kept in both polarities or under two adducts,
  reported and never removed;
- `polarity_reconciliation`:
  - `counts` per class;
  - every `matched` pair: the positive name, alignment ID and RT, the negative ID and RT,
    and ΔRT;
  - every `unmatched` positive row, with the negative rows of the same name outside the
    window and the other names inside it, so the person can see what exact matching left
    out;
  - every `ambiguous` entry. Kind `several negative candidates` gives the candidates and
    `chosen_alignment_id`, the row the positive row was finally given (none when it lost
    them all). Kind `conflicting positive evidence` gives the positive rows that annotate
    the renamed row's own name: read each one out, since the spectra need a look;
  - every `lost_claims` entry: a positive row that lost every candidate, each with the
    positive row that took it. A matched pair with `after_losing` took its next
    candidate after losing one;
  - `not_converted`: positive rows of either class whose name names no negative row. A
    PE O- that positive mode names itself is expected here, and `names_negative` lists
    the negative rows of exactly its name. When one of them was renamed PE P- by another
    positive row, the reason says so and the pair is also in `ambiguous`; read it out. A
    BMP without a BMP token is not expected, so read it out too;
  - `renamed_rows`: how many renamed rows were then kept, not quantified, or not
    selected;
- `samples.taken`: the Sample and QC files the statistics use. A Blank never counts.
- `warnings`. In particular, a sheet 3 that selects BMP or EtherPE in positive mode, or
  does not select their `[M-H]-`. At this step that is said as what this lipidome
  counts (EtherPE from both polarities, say), because sheet 3 is part of the contract
  the person accepted it under. Read it out as a fact about the result. Do not suggest
  editing sheet 3 now: that makes the contract, both runs and both normalizes unusable,
  and the merge refuses. If the person wants another selection, it is a new contract
  (`replace_existing`) and both polarities run again; say so and let them decide. Also
  read out anything `metabolomics_service.R` would stumble on: repeated sample names,
  constant lipids, `-?` intensities, or other folders beside the tables. A Sample or QC
  file with no class label is taken under an empty class, as metaboservice took it, and
  is warned about; the R script compares it as a group of its own, so ask the person
  what it is.

A count of kept lipids is only meaningful beside the count of everything that did not
qualify. The merge pairs the polarities' sample columns by position and refuses when
they differ, file types included. If it refuses, the two runs disagree about their
samples; do not work around it.

With the person's go, `confirmed=true` writes everything into `<root>/quant/lipidome`,
the folder to run `metabolomics_service.R` in. That script reads every subfolder of the
folder it runs in, which is why the merge does not write into `quant`, where normalize's
`pos` and `neg` folders are. The files are what metaboservice wrote:
- `lipid_metabolome.csv`;
- `lipid_metabolome4stat.csv`, `lipid_metabolome4classsummary.csv` and
  `lipid_metabolome4summaryplot.csv`;
- the `Summary_Dotplots`, `category_<category>` and `subclass_<ontology>` folders.

They are CRLF, comma separated and never quoted, with rows in .NET's Windows order and
numbers as .NET Framework printed them. They are ASCII unless a cell is not: a Japanese
class label, file name or comment is written as UTF-8 and warned about, where
metaboservice wrote one `?` per byte (and two labels of one length became one class). When
that warning appears, tell the person, because R must then read the tables as UTF-8.
Beside them the merge writes `lipid_metabolome_not_quantified.csv` and
`merge-report.json`.

The category folders follow the class table metaboservice was built with, not current
MS-DIAL's. A class newer than that table (WE, NATau and the other N-acyl amino acids,
ASHexCer, DGTA, PT, ...) goes to `category_Unassigned lipid`, as metaboservice filed it.
`merge-report.json` lists those ontologies (`outputs.category_unassigned_ontologies`);
name them when there are any.

Merging again replaces only the dotplot files the last merge wrote. Any other file in
those folders refuses the write; ask the person to move it. Another `output_name` renames
only `lipid_metabolome.csv`, and never to one of the files the merge or normalize writes.
The three statistics tables always keep the names above, because the R script reads them
by name. A lipidome an earlier merge left there under another name stays and is warned
about; say which one is current.

## L. Save the method, and report

Offer `msdial_save_workset(name, lab_lipidomics_contract_path=...)`, and read both halves
out:
- **It carries** the LBM2, the salt, both RT tolerances, the internal-standard texts, the
  workbook and its amount column, the Console and the method. Each file is carried with
  its sha256.
- **It does not carry** the sample table, the analysis purpose, the workbook's sample
  amount, the dilution factors, any path, or the accepted thresholds.

The next batch starts with `msdial_lipidomics_check_inputs(pos_path, neg_path,
workset_id=...)`, which fills in what carried (`filled_from_workset`) and warns when a
file has changed. Its `msdial_lipidomics_write_contract` takes the same `workset_id`.
The person is still asked R0, R7 and where MS-DIAL reads, still confirms the sample
table, and hears the sample amount again.

Say what ran, on which files, with which library and settings, and where the artifacts
are:
- the LBM2 by name and sha256, never its path in anything shared;
- the salt, and how the answer was mapped;
- both RT tolerances;
- each polarity's threshold, step and representative file;
- each standard's tier, and what the person accepted, took instead or left unresolved;
- the Console's version and build for the runs and for normalize (`normalize_console` in
  `merge-report.json`), and whether it changed after the contract;
- the unit, and the sample amount as the workbook was saved. When an earlier analysis of
  the same samples used another amount, say by what factor the concentrations differ and
  why;
- the NOT QUANTIFIED classes, the verification and the merge counts;
- the reconciliation: its tolerance, the BMP and PE P- rows renamed, the positive rows left
  unmatched or that lost their claims, any conflicting positive evidence, and whether it
  ran as the contract states;
- whether the person kept a sheet 3 that contradicts the reconciliation
  (`merge.selection_warnings_accepted` in the contract), and what the lipidome therefore
  counts;
- where the R inputs are (`<root>/quant/lipidome`), and that the statistics took the
  Sample and QC files.

Name what was assumed rather than confirmed, such as the acquisition type or a dilution
factor nobody stated. When a step was skipped, say so: a workflow that reports success
for work it did not do is worse than one that fails. The raw data, or its copy, is kept.
Deleting it is a separate confirmation, after the outputs are validated.

## Private resources

A laboratory's own LBM2, MSP libraries, standards texts and workbooks may be private.
Use them where they are, locally. Never copy them into a bundle, a log, a repository, an
issue or a shared report, and never put their paths in one: name a library by its file
name and sha256 instead. The contract and its record name the LBM2 by where it is kept
(`sharing: local_only`), so they stay on this computer. The raw data and the libraries
are read where they are and never uploaded; what reaches the conversation is what the
tools report about them.

## What is not automated yet

- Reviewing the annotations for false positives and false negatives, between H and I, is
  manual.
- `metabolomics_service.R` itself is not run. Its comparisons (`my_comparisons`) and
  working folder are set by hand in the script.
- These tools cannot resolve, normalize or merge a run that was not made under a contract
  (in the GUI, or by another tool). Offer to run it under one; do not fall back to running
  `MSDIALCUI normalize` by hand.
