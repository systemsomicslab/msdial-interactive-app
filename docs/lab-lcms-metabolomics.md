# Laboratory LC-MS metabolomics agent

The laboratory lipidomics agent's counterpart for untargeted LC-MS metabolomics: hydrophilic
metabolites on HILIC or a polar-retaining reversed phase such as HSS T3. A person hands over one or
two raw-data folders (positive, negative, or both). The result is a metabolome normalized by one
internal standard per polarity, merged across the polarities when both were run.

It keeps everything the lipidomics agent established:
- one message of questions;
- a contract that fixes the inputs;
- a yes before every Console start and every confirmed write;
- a one-file diagnostic for the peak-height threshold;
- QA, internal-standard resolution, normalization and a merge, each checked again from what the run
  wrote.

What it drops is lipid-specific: the LBM2 as the main library, the class-to-standard lookup, the
amount per sample unit and the representative-adduct selection.

Code: `msdial_app/lab_lcms_metabolomics.py` (libraries, normalization, merge) and
`msdial_app/lab_lcms_metabolomics_agent.py` (check, contract, plan answers, standards, normalize,
merge). Agent Skill: `skills/msdial-lcms-metabolomics-workflow`.

## 1. Inputs

| Input | Notes |
|---|---|
| Raw folders | `pos_path`, `neg_path`, or both. A batch of one polarity is a complete batch. |
| Annotation | An ordered list of libraries (section 2), usually from a preset. |
| Salt | As for lipidomics; formic acid reads as `HCOONH4`. It also chooses the negative adduct the merge prefers. |
| Internal standards | An MS-DIAL text library per polarity (Name, MZ or Pos-m/z, RT, Adduct; Ontology, Formula, InChIKey optional; a standard without a class is written as "Internal standard"). One file may hold both. |
| Duplicates | `duplicate_rule` (evidence, intensity or keep_all) and `inchikey_match` (first_block or first_two_blocks), asked before anything runs. |
| Pairing | Two polarities pair by name, otherwise by the words that set each polarity's files apart, or by `sample_pairs` the person gives. Paired by those words, the class is proposed from letters (section 1b) only where the file names give no Sample pair a class both polarities agree on. |
| Acquisition type | Read from each file's raw header (section 1a). `acquisition_type_pos` / `acquisition_type_neg` (DDA, SWATH or AIF) is asked for every file whose own header does not settle it; `acquisition_type_mixed_pos` / `acquisition_type_mixed_neg` (`as_read`) where the files' own headers give more than one type. |
| Normalizing standard | One name per polarity (`normalizing_standard_pos`, `normalizing_standard_neg`), from that polarity's library. |
| Internal-standard RT tolerance | 0.5 min unless given. |
| Alignment RT tolerance | `alignment_rt_tolerance`, minutes, optional: the template's 0.1 min unless given. Capillary electrophoresis migration times usually need more; the person decides. Either value is fixed for both polarities (section 3). |
| Analysis root, raw location, Console, threads | As for lipidomics. |
| Sample table | As for lipidomics. File types are Sample, QC, Blank and Standard. A reference material such as NIST SRM 1950 is a Standard. |
| Analysis purpose | Recorded and never filled in for the person. |

The Console needs the annotation candidate table and the automatic-RT local test. Normalization runs in
Python, so normalize's `--peak-ids-only` is not needed.

## 1a. Raw headers: acquisition type and analytical order

SWATH or AIF data run as DDA give almost no feature an MS/MS spectrum, and annotation then fails without a
word. So the check reads each raw file's header with the configured raw-metadata extractor (the one a
repository preflight outside a campaign runs: the argument-free `select_raw_metadata_extractor`), with the
campaign's bounded reader (`raw_metadata_preflight.run_extractor`, `--max-spectrum-headers 200`) and its
parsing (`repository_reanalysis._summarize_raw_metadata`), but with limits of its own:
- Every file of a polarity is read up to 200 files; past that, only the first and the last, and the type
  of every file between them is asked (the two ends' type, when they agree, is said as a suggestion only).
- A check, and the check `write_contract` runs, returns within the MCP tool's 600 s: each file gets at most
  30 s (plus 120 s per GB where the reader reads every spectrum, as for Waters, and 30 s per GB for
  ion-mobility data; 180 s at most), and no extractor process starts, or runs on, past 240 s for the whole
  check. Files go to the extractor in chunks whose summed limits take at most half that time (4 files at
  30 s), so a file that hangs costs its chunk its own limit, and the chunk's other files are then read one
  at a time and kept; a chunk the 240 s cut loses every file in it. A file not read in time is asked about
  (`not_read_in_time`); the next check reads such files first, one at a time, each within its own limit,
  so one that hangs is then kept as a failed read and the others are read.
- A header read is kept in the backend process by the extractor build (the sha256 of its exe and of every
  file beside it, as `inspect_raw_metadata_extractor` reports them) and the file's path, size and
  modification time as they were before the read, a read that failed included. So a check repeated does
  not read an unchanged file again, nor wait again for one that could not be read: that file is read again
  only once it changes, the extractor changes, or the backend restarts. A read the 240 s cut short is not
  kept. An input that changed during its read (a copy still writing a file, or completing a Waters
  folder) is read once more by the next check, as it then is. A check that finds a file being read by
  another check waits for that read rather than start a second extractor on it.
- The extractor works in a temporary folder outside the raw data, removed afterwards. Its Bruker BAF reader
  writes `analysis.sqlite` into a `.d` folder, so such a folder is never given to it, and its type is asked.
  Any other reader is not prevented from writing: what appears beside a file input under that file's name
  with another ending (`S1_Pos.mzML.index`; its folder is listed before and after the read), or inside a
  folder input where a known reader rule names it (`reader_created`: baf2sql's `analysis.sqlite`), is
  detected afterwards, said in a warning (`reader_created_files`), kept for the life of the backend process
  so every later check says it again, bound into `check_digest`, and recorded in the contract. Any other
  name that appears beside or inside an input meanwhile (raw data, a copy's files, MS-DIAL's intermediates,
  a sync client's file) is said in that check's warnings as having appeared (`appeared_during_read`),
  without naming a writer, and is neither kept nor bound. `write_contract`'s preview and its result give
  its own check's warnings (`warnings`), so a name that appears during its reads is shown, and the
  contract records its `appeared_during_read`.

Each file's type is the one its header gives the Console (`header_console_acquisition_type`): DDA, AIF or
SWATH as the header says; DIA with two or more isolation targets is SWATH, with none AIF, with one
unresolved (one wide window or one targeted precursor). Only a DDA or SWATH verdict of the file's own header
settles its type. For every other file `acquisition_type_pos` / `acquisition_type_neg` is a required
question (DDA, SWATH or AIF), and the answer fills exactly those files; its prompt counts them by why:
- no extractor is configured, or the header was not read (it could not be, was not in time, or is a BAF
  folder);
- the header gives no type the Console reads;
- the header gives AIF. An AIF verdict is asked rather than taken: a SWATH file whose sampled headers show
  no isolation reads as AIF too (one positive file of the CE demo does), and the repository campaign runs
  AIF of one MS2 collision energy as SWATH, which the Console deconvolutes alike. Whether laboratory AIF runs
  as AIF or as SWATH is the person's answer. The prompt names each file's MS2 collision energies;
- the file lies between the first and the last of more than 200.

A type is never assumed or inferred. An answer is warned about when the files read from their own headers
give another type; the warning counts those files and the answered ones apart. For every answered file,
why it was asked and what its header suggested (the type, the collision energies) are bound into
`check_digest`, so a file answered while not read, and read as AIF before the contract is written, is
shown again. Where the files' own
headers give more than one type within a polarity (a misread header and a batch acquired so look alike),
`acquisition_type_mixed_pos` / `acquisition_type_mixed_neg` is asked, naming the files of each type:
`as_read` keeps each file's own header type, and a header the person says is wrong is corrected in
`sample_table`.

The files to run as AIF meet the repository campaign's collision-energy guards
(`raw_metadata_preflight.decide_disposition`), per polarity, from what their headers record: the distinct
MS2 collision energies (to 0.1 eV) over all of them. More than one, whether one file records several or
files record different ones (pooled QCs at 40 eV beside samples at 20 eV), needs a Console with
MsdialWorkbench#825, which settles each spot's representative energy among each file's own energies, so
another Console is a blocker, naming each energy set and its files. With #825, files whose energy sets
differ are a warning (the campaign runs them, on record), and a file whose MS2 scans carry no energy is a
blocker, since that Console stops on it. AIF whose energies are not recorded, with a Console without #825,
and AIF whose header was not read, are warnings. The check counts every file of the polarity, since it reads
no sample table. `write_contract` judges the guards again over the rows the confirmed table includes, with
the types it states, so a file left out (`included` false) no longer counts, and a check's energy blocker
does not by itself refuse the contract. The table's AIF warnings are in the preview and the result
(`acquisition_warnings`, and among `warnings`) and are recorded in the contract (`raw_headers.aif_warnings`).

A header recording the other polarity than its folder's is a blocker, as file names saying so already
are; so is one recording both (PolaritySwitching, MixedFunctions), since MS-DIAL runs one ion mode per
analysis.

The analytical order follows the acquisition start times the headers record when every file of every
polarity was read and records one, and the times order the files (files of one time keep their listing
order). Otherwise the order proposed from the names or the listing stands, and `order_proposal` says why.

A paired row carries one acquisition type and one order for both polarities. Its type is its files' type
when both have one and they agree. A row with a file still asked about has none until the answer, and a
pair whose two files' types differ (`acquisition_type_conflicts`, which gives both) is proposed with none:
`acquisition_type_required` lists such rows, and the contract refuses each until the person gives its
`acquisition_type` in `sample_table`. A table sent back as proposed therefore carries no type nobody gave,
and an empty or blank type, which the Console would read as DDA, is refused as missing. Polarities really
acquired as different types are checked one contract each. The order is the positive file's, else the
negative's; where the negative files were acquired in another order, a warning names the samples. Each
row's `acquisition_type_basis` says where its type came from (`raw_header`, `answer`, `sample_table`, or
`polarities_disagree` for a pair proposed without one).

`raw_headers` in the result gives the extractor (its path, source and build hashes) and per polarity the
coverage, the methods, the isolation target counts, the MS2 collision energies, the instruments, the
separations, the acquisition types and what proposed them (`proposed_by`), the files that could not be
read or were not read in time, the files a reader left beside the raw data, and per file read its method,
isolation targets, collision energies, type, polarity and start time. The proposal is bound into
`check_digest`, and the contract records `raw_headers` with the acquisition answers taken (`answers`). A sample
metadata sheet (`sample_metadata_path`) is compared with the table proposed, whose order and classes may be
the headers' and the letters', not with the names.

On the CE demo (16 + 16 Thermo Q Exactive SWATH files with 8 isolation windows), the pinned extractor gives
SWATH for 31 files and AIF for one positive file whose sampled headers show broad isolation: that file is
asked about, and once answered SWATH its pair agrees.

The contract's schema is `msdial-interactive.lab-lcms-metabolomics-contract.v2`. A contract of the earlier
v1 schema (its rows may carry DDA nobody stated, the positive file's type for a pair of two types, or AIF
taken from a header) is refused by every step that loads it, and a contract without `raw_headers` by every
plan; so is a plan from a contract whose included row carries no type the Console reads. Check the inputs
again and write the contract again.

## 1b. Classes from the letters of shared words

Paired by the words that set each polarity's files apart, each pair's class is first the one its file names
give (`sample_grouping.propose_grouping`, per polarity). Only where those fail for this pairing, because no
Sample pair's two files were given one class (case aside) other than none or "Sample" (the positive files
give no class and the negative ones d2 against a re-injection's d2r2, say), is each Sample pair's class read
from the letters of its one shared letter+number word (A1 -> A). Where even one Sample pair's names agree,
every pair keeps its names' class: one pair's typo, or WT against wt, never relabels the batch, and each pair
whose two classes differ is said in `pairing_warnings`. Letters that name a blank, a QC or a standard (Blank1,
QC2, Std3) make no group, and such a pair keeps its own class; every other Sample pair must have exactly one
such word, and the letters must make at least two groups of at least two pairs each. So sample numbers (S1 to
S16, even beside QC1 to QC3), subject numbers beside a numbered blank (Ctrl_Mouse1 ... Blank1) and plate wells
beside a factor the names give (WT_A1 ... KO_D3) keep the names' classes. `class_proposal` and
`pairing_warnings` say when the letters set the class, for the person to confirm, and
`class_proposal.polarity_disagreements` keeps each pair whose names' classes differed.

## 2. Annotation: an ordered list of libraries

`annotation` is `{"preset": name, "libraries": {slot: path}}`, or `{"libraries": [entry, ...]}` with
entries in priority order. A path is one file for every polarity or `{"Positive": ..., "Negative": ...}`.

| Field | Values |
|---|---|
| `kind` | `msp` or `lbm` (at most one LBM2) |
| `path` | the library file, per polarity where it differs |
| `thresholds` | `standard`, `strict` or `mild` (MSP only) |
| `retention_time` | `none` (neither filtering nor scoring) or `use` (filtering and scoring) |
| `rt_tolerance` | minutes, required when `retention_time` is `use` |
| `label` | what the results and the report call it; printable ASCII, at most 60 characters |
| `library_kind` | `acquired` (default) or `predicted` (in silico; the campaign's `in_silico` slot), MSP only |

| Preset | Libraries, highest priority first |
|---|---|
| `single_msp` | `msp`: one MSP, `standard`, no RT |
| `campaign` | `lbm`: the LBM2, no RT; `msp` searched `strict`, then `mild`, no RT; optionally `in_silico`, `mild`, no RT |
| `lab_rt_tiers` | `in_house`: in-house RT and MS/MS, RT 0.5 min; `predicted_rt`: predicted RTs, RT 2.0 min; `no_rt`: MS/MS without RT. All `standard`. |

The thresholds are those MS-DIAL Interactive already uses:
- `standard`: a guided run's own MSP row: weighted and simple dot product 0.6, reverse 0.8, matched peaks
  10%, at least 3 matched peaks, the method's MS/MS tolerance.
- `strict`: the campaign's strict tier: MS/MS tolerance 0.05, dot products 0.6/0.6/0.8, at least 3 matched
  peaks.
- `mild`: the campaign's broad tier: MS/MS tolerance 0.25, dot products 0.5/0.5/0.5, at least 1 matched
  peak.

A library searched with retention time is read for its records' RTs first, as MS-DIAL's MSP reader reads
them: MS-DIAL keeps a record without an RT in every RT window and counts its RT as met, so such a record is
matched on MS/MS alone under the RT tier's label. None with an RT refuses the library; some without one is a
warning with the count. An optional preset library may serve one polarity only. A preset slot may be
`{"path": ..., "library_kind": "predicted"}`. Any key the annotation does not read is refused. Library paths
must be ASCII, since msp_annotator_settings.tsv is written and read as ASCII; so must the analysis root, and the
raw files' paths must be ASCII without commas or quotes, which analysis_files.csv cannot carry. A raw folder of
such a path refuses the check; a file of its own is a warning, and the contract refuses it only when it is included.
The sample table's classes and acquisition types must be printable ASCII without commas or quotes too.

With n libraries, the first takes priority n+1 and the last 2; the internal-standard text row keeps
priority 1, below every library. MS-DIAL picks a feature's representative annotation by reference match
first, then by suggestion, evidence and priority, so a lower-priority MS/MS match still outranks a
higher-priority precursor-only suggestion. MSP annotator IDs are `lib<position>_<id>` (`lib1_in_house`, ...);
the LBM2's is the Console's own, `LbmDB: <file stem>`. An export's Comment names them, and the merge reports
each row's library by its label. resolve_standards warns about a library that left no annotation candidate,
since the Console skips a library it reads as empty without a word.

The guided plan takes the list as `library_strategy: "annotators"`, `libraries.annotators`. Each MSP row is
written into `msp_annotator_settings.tsv` with its own RT settings and `target_omics` Metabolomics, and is
marked `rt_fixed`: the run-wide retention-time answer reaches the LBM2 only.

## 3. Runs

For each polarity, as for lipidomics:
1. the plan from the contract (`lab_lcms_metabolomics_contract_path`, `ion_mode`);
2. a one-file zero-threshold diagnostic and the threshold it suggests, accepted by the person;
3. the production run, confirmed;
4. completion: mzTab-M, QA and the per-run QA figures.

The method template is the laboratory LC-MS template, with Target omics set to Metabolomics. The method
answers are lipidomics' (smoothing, automatic RT correction with 12 anchors and the 1.5 min local window,
annotation candidates, QA). A contract's `alignment_rt_tolerance` reaches the method file as "Retention time
tolerance for alignment"; without one the template's value (0.1 min) stands. Either is fixed for both
polarities: a plan's own value, or a workset's, that differs is a blocker, as `workflow_overrides` for it
is. One Console runs at a time.

## 4. Internal standards

Each standard of a polarity is looked for in that polarity's run with lipidomics' resolver, at the m/z of
its adduct and within the internal-standard RT tolerance:
- A: one of the contract's libraries annotated it by its name;
- B: its own text row annotated it as a reference match;
- C: its text row matched, but another annotation won the spot or the match is below the score cutoff;
- D: any spot there.

The first tier with a member decides, then the most intense spot over the Sample and QC injections. C and
D need the person's acceptance. Every standard is reported with its spot and its CV over the QC injections
and over the Sample and QC injections.

Only the normalizing standard must be found. Found as C or D, the person accepts it (`accept_searched`) or
takes another listed spot (`accept_spot`). Not found, or with no height in a Sample or QC, it can only be
replaced by a listed spot; otherwise a new contract names another standard.

## 5. Normalization (per polarity)

For every feature `s` and injection `i`:

    normalized[s][i] = height[s][i] / height[IS][i] x mean(height[IS] over the Sample and QC injections) x factor[i]

- The mean is taken over the Sample and QC injections only. Blank and Standard injections are normalized
  by the same formula but do not enter the mean.
- A Sample or QC without the normalizing standard refuses normalization, and so does one in which MS-DIAL
  did not detect it (the run's `<alignment>.mdpeakid.tsv` gives -1: the height is a gap fill), unless the
  person accepts those heights (`accept_gap_filled`).
- A Blank or Standard injection keeps its raw height, and is reported with why, when the standard has no
  height there, was not detected there, or is below 10% of the lowest Sample or QC height: a gap fill puts
  noise where the spike would be, and dividing by it would multiply the injection by its inverse.
- `factor[i]` is the injection's dilution factor from the sample table, 1 unless given.
- It is computed from the run's raw Height export. MS-DIAL's own internal-standard normalization averages
  over every file, Blank included, and replaces a zero with 1, so it is not used.
- The matrix keeps MS-DIAL's export layout and is written as `<root>/quant/NormalizedHeight_<Pos|Neg>.txt`.
  Only the sample cells change; MS-DIAL's per-class Average and Stdev columns are left empty, since they
  were computed from heights. Every cell is then recomputed on its own from the copy of the Height export
  (which must be the export normalize read) and compared.
- Beside it: a copy of the Height export, `is_resolution_<pos|neg>.tsv`, `normalize_<pos|neg>.local.json`,
  and figure 04 (every internal standard's response) in `<root>/quant/qa_figures`.

## 6. Merge

The result is one metabolome from the normalized matrices of every polarity the contract covers.

1. **Rows dropped before anything else:**
   - unannotated rows (name empty, "Unknown" or "null");
   - internal standards: the normalizing standard's spot as decided, every spot a library or a standard's own
     text row annotated as that standard (tiers A and B, whatever else the resolver said of it), and every row
     the internal-standard text row annotated. A labelled standard shares its InChIKey's first block with the
     endogenous compound, because the label sits in the second block. A spot only searched for a standard
     nobody confirmed (tiers C and D) stays a feature, kept as its own row outside the InChIKey groups so that
     it never removes another, and the report names it with any kept row it shares a first block with
     (`unconfirmed_standard_spots`).

   The matrices are the exports the run wrote. Names, Unknowns and comments set in the GUI after the run do not
   reach them; normalize warns when the project was saved well after the exports.
2. **Options**, chosen when the merge is called and recorded with it:
   - `reference_matched_only`: keep only rows whose representative annotation is a reference match, what
     the GUI shows as "Ref. matched". Default on.
   - `preferred_adducts_only`: keep only the preferred adducts. Default off.
3. **Duplicates:** rows of one compound are one, within a polarity and across polarities. What one compound
   is, `inchikey_match`, and which row is kept, `duplicate_rule`, are the person's answers at the check, fixed
   in the contract; a merge may be given others, which it reports as deviations.
   - `inchikey_match`: `first_block` (the first 14 characters: stereoisomers are one compound) or
     `first_two_blocks` (they stay apart).
   - `duplicate_rule` `evidence`: the annotation matched on RT and MS/MS (the export's RT matched and MS/MS
     matched), then on MS/MS alone, then on RT alone; then the higher library priority, the preferred adduct,
     the larger mean normalized height over Sample and QC, the higher total score, positive before negative,
     the lower alignment ID. A match with the laboratory's own RT is kept over a stronger MS/MS-only one.
   - `duplicate_rule` `intensity`: a preferred adduct (`[M+H]+`, `[M-H]-`, and the salt's negative adduct:
     `[M+HCOO]-` for ammonium formate or formic acid, `[M+CH3COO]-` for ammonium acetate, none for ammonium
     bicarbonate; compared as ions, so `[M+FA-H]-` counts as `[M+HCOO]-`); matched on MS/MS before a
     precursor-only suggestion; the larger mean; then the library priority, the total score, positive before
     negative, the lower alignment ID.
   - `duplicate_rule` `keep_all`: every row is kept, and the rows of one compound are counted
     (`duplicates_kept`).
4. **Rows without a usable InChIKey** are kept as their own rows and never merged with anything.
   **Possible isomers:** a group whose rows differ in stereo block or lie more than 0.5 min apart is reported
   (`possible_isomers`, by `compound_key`) with the row kept; nothing is split beyond what `inchikey_match` says.
5. **Sample columns:** the confirmed sample table names each export column's sample, which pairs the
   polarities whatever their file names; the negative columns are put in the positive's sample order, and a
   sample of another file type in one polarity than the other refuses the merge. The columns take the
   table's sample names.
6. **Outputs** in `<root>/quant/<output_folder>` (default `metabolome`):
   - `metabolome.csv`: quoted CSV. Polarity, alignment ID, InChIKey first block, library label, reference
     matched, preferred adduct, then MS-DIAL's metadata columns and the normalized sample columns.
   - `metabolome_samples.csv`: each sample's class, file type, injection order and batch.
   - `metabolome_removed.tsv`: every row the merge removed, with its reason and, for a duplicate, the row
     that was kept.
   - `merge-report.json`: the inputs with their sha256, the options, the preferred adducts, the ranking and
     the counts.

Merges with different options may sit side by side under different `output_folder` names; `pos`, `neg` and
`qa_figures` are refused, and so is a name ending in a dot, which Windows strips.

The two laboratory agents write the same file names into an analysis root, so each refuses a root that holds
the other's contract.

## 7. Tools

| Tool | Role |
|---|---|
| `msdial_lcms_metabolomics_check_inputs` | read-only; lists what is there and every open question |
| `msdial_lcms_metabolomics_write_contract` | previews, then writes `<root>/inputs/lab-lcms-metabolomics-contract.json` |
| `msdial_lcms_metabolomics_resolve_standards` | read-only; finds the internal standards in each run |
| `msdial_lcms_metabolomics_normalize` | previews, then writes the normalized matrix of one polarity |
| `msdial_lcms_metabolomics_merge` | previews, then writes the metabolome |

The guided-analysis tools run the diagnostic and the production runs. Their plan takes
`lab_lcms_metabolomics_contract_path` and `ion_mode`.

## 8. What stays local

- The contract and its record name the libraries and the raw data by their locations, and are local only
  (`sharing.LOCAL_ONLY_FILE`).
- A laboratory's private libraries are named by file name and sha256 in anything shared; their provenance
  entries carry the licence `institutional/private`.

## 9. Not yet

- Worksets that carry a metabolomics contract's method to the next batch.
- A batch QA page across both polarities; each run has its own, and normalize draws figure 04.
- Curation in the GUI between the run and normalize. Lipidomics carries it, because its normalize runs the
  Console on the saved project; here the run's own exports are read.
- GC-MS metabolomics, which comes next.
