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
| Internal standards | An MS-DIAL text library per polarity (Name, MZ, RT, Adduct, Ontology; Formula, InChIKey optional). One file may hold both. |
| Normalizing standard | One name per polarity (`normalizing_standard_pos`, `normalizing_standard_neg`), from that polarity's library. |
| Internal-standard RT tolerance | 0.5 min unless given. |
| Analysis root, raw location, Console, threads | As for lipidomics. |
| Sample table | As for lipidomics. File types are Sample, QC, Blank and Standard. A reference material such as NIST SRM 1950 is a Standard. |
| Analysis purpose | Recorded and never filled in for the person. |

The Console needs the annotation candidate table and the automatic-RT local test. Normalization runs in
Python, so normalize's `--peak-ids-only` is not needed.

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
must be ASCII, since msp_annotator_settings.tsv is written and read as ASCII.

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
annotation candidates, QA). One Console runs at a time.

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
3. **Duplicates:** rows sharing the InChIKey's first 14 characters are one compound, within a polarity and
   across polarities. One row is kept, ranked by:
   1. a preferred adduct: `[M+H]+`, `[M-H]-`, and the salt's negative adduct (`[M+HCOO]-` for ammonium
      formate or formic acid, `[M+CH3COO]-` for ammonium acetate, none for ammonium bicarbonate). Adducts
      are compared as ions, so `[M+FA-H]-` counts as `[M+HCOO]-`.
   2. matched on MS/MS before a precursor-only suggestion;
   3. the larger mean normalized height over the Sample and QC injections;
   4. then, deterministically: the higher library priority, the higher total score, positive before
      negative, and the lower alignment ID.
4. **Rows without a usable InChIKey** are kept as their own rows and never merged with anything.
   **Possible isomers:** the first block joins stereoisomers (glucose and mannose 6-phosphate), and the ranking
   puts intensity before library priority, as the laboratory asked. A group whose rows differ in stereo block
   or lie more than 0.5 min apart is reported (`possible_isomers`) with the row kept; nothing is split.
5. **Sample columns:** both polarities must describe the same samples in the same order (the names with the
   polarity suffix removed). A merge of two polarities names the columns by stem; one polarity keeps the
   exported names.
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
