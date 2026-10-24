# Changelog

Notable changes to MS-DIAL Interactive. The package version is kept in
`pyproject.toml` and `msdial_app/__init__.py`; the Agent API version is separate.
Agent API 0.5 requires the repository split endpoint introduced after API 0.4.

## [0.5.47] - Unreleased

### Fixed
- **The tiered profile loads the LBM2 its answers name, and every record names the one it loaded.**
  - `library_strategy: "tiered_lipid_msp"` took its lipid-rule tier from the official catalog LBM2
    (`lipidomics`) whatever `libraries.lbm_path` said: `_apply_libraries` read `lbm_path` only for the other
    strategies. MS-DIAL loaded the official LBM2, and the method file's `Lbm file path`,
    `workflow-settings.json`, `library_provenance` and the run manifest named it; the answers
    (`guided-answers.json`), and the records a repository campaign built from them, named the LBM2 the
    profile gave. A campaign whose profile named a laboratory LBM2 therefore annotated with the official
    one while its records said otherwise. Runs already prepared are not rewritten: their `method.txt` names
    the LBM2 that ran.
  - A given `lbm_path` is now the tier. It reaches `Lbm file path`, `lbm_annotator`,
    `workflow-settings.json`, `library_provenance` (by `name`, `size_bytes` and `sha256` measured from the
    file, with `lbm_version`, `lbm_source`, `lbm_doi` and `lbm_license` as given, private by default), the
    run manifest's libraries, the shared library identities and the Materials and Methods.
  - The library catalog is consulted, and an official LBM2 that is not downloaded is a blocker, only when no
    `lbm_path` is given. Without one the profile is as before.
  - A given `lbm_path` that is not an `.lbm2` file with content is a blocker (`workflow.lbm_library_problem`,
    the laboratory lipidomics rule, which now also refuses a folder); a path that does not exist is "LBM
    library not found", as for any library.
  - A given `lbm_path` the plan cannot read through (denied, held by another process while it copies, or failing
    partway) is a blocker and is recorded by name only, whatever its size, rather than a `PermissionError` out
    of the plan, which reads the file for its sha256. The plan's check and its record both read the file
    through to its end and hash it themselves, at any size. Neither takes the digest `workflow.file_identity`
    caches for a file of 64 MiB or more, as a campaign LBM2 is, against its path, size and time: the cache
    answers without reading the file, so once it held the digest an LBM2 failing past its first byte would plan
    as ready and be recorded by its sha256. A plan therefore reads the LBM2 twice.
  - A relative `lbm_path` is made absolute against the backend's working directory, where the plan checks and
    hashes it (`workflow.absolute_library_path`), and the state, the method file and every record name that
    path. Written as given, it would be read by the Console against the method file's folder and then its own,
    and the LBM2 skipped without a word while every record named it by its sha256.
  - A second `msp_paths` entry is a blocker. The profile searches one MSP in both of its MSP tiers, and left
    any further entry out of the run without a word.
  - A `text_paths` entry is a blocker. The profile has no text tier, and left a text library out of the method
    file and every record but the answers without a word; `library_strategy: "existing"` searches one.
  - A `workflow_overrides` key that names or records a library (`lbm_path`, `lbm_annotator`, `msp_path`,
    `msp_annotators`, `text_db_path`, `text_annotators`, `library_provenance` or `annotation_pipeline_profile`)
    is a blocker under this profile. Overrides reach the state after the profile, so an `lbm_path` there was
    the LBM2 the method file named, unchecked, while `lbm_annotator`, `library_provenance` and every record
    named the given one. The other library strategies apply such an override as before.
- `msp_annotator_settings.tsv` keeps an explicit priority 0, which the Console reads as a priority; the row's
  position replaced it. No profile writes 0 today.
- The method file's `Lbm file path` is written absolute for every library strategy, as the MSP and text rows
  of the annotator settings files are. A relative `libraries.lbm_path` of `existing` libraries reached it as
  given, with the same effect; the workflow state and provenance keep the path as the answers give it.

### Changed
- The plan's `libraries` question for `tiered_lipid_msp` asks for `libraries.lbm_path` beside the one MSP, and
  says the official LBM library is the tier only without it. `scripts/create-tiered-annotation-demo.py` takes
  `--lbm`.

## [0.5.46] - Unreleased

Built on 0.5.45 (#78), which is not merged yet; merge this after it.

### Added
- **A laboratory LC-MS metabolomics agent** for hydrophilic metabolomics (HILIC, or a polar-retaining
  reversed phase such as HSS T3), in one polarity or both. Five MCP tools carry it, with the guided-run
  tools in between: `msdial_lcms_metabolomics_check_inputs`, `msdial_lcms_metabolomics_write_contract`,
  `msdial_lcms_metabolomics_resolve_standards`, `msdial_lcms_metabolomics_normalize` and
  `msdial_lcms_metabolomics_merge`. The Agent Skill `skills/msdial-lcms-metabolomics-workflow` walks
  through them, and `docs/lab-lcms-metabolomics.md` is the specification.
  - **Annotation is an ordered list of libraries**, each with its own retention-time use and thresholds.
    Three presets cover the cases seen so far:
    - `single_msp`: one MSP without RT;
    - `campaign`: the LBM2, one MSP searched strictly and mildly, optionally an in-silico MSP, all without RT;
    - `lab_rt_tiers`: an in-house MSP at RT 0.5 min, a predicted-RT MSP at RT 2.0 min, and an MSP of MS/MS
      without RT.
    Each row is written with its own settings into `msp_annotator_settings.tsv` and marked `rt_fixed`, so the
    run-wide retention-time answer leaves it as it is. The internal-standard text row keeps priority 1, below
    every library. The guided plan takes the list as `library_strategy: "annotators"`.
  - **Normalization by one internal standard per polarity**, computed in Python from the run's Height export:
    height / the standard's height in that injection x the standard's mean over the Sample and QC injections,
    x the dilution factor. A Sample or QC without the standard refuses; a Blank or Standard injection without
    it keeps its raw height and is reported. Every written cell is checked again. MS-DIAL's own normalization
    is not used, because it averages over every file, Blank included, and replaces a zero with 1.
  - **The merge** keeps one row per compound, within and across polarities. What one compound is
    (`inchikey_match`: the InChIKey's first block, or its first two blocks) and which row is kept
    (`duplicate_rule`: `evidence`, a match with the laboratory's own RT and MS/MS before an MS/MS-only one
    even if weaker; `intensity`, the preferred adduct then the larger intensity; or `keep_all`) are asked
    before anything runs and fixed in the contract. Internal standards and unannotated rows are dropped
    first. `reference_matched_only` (default on) and `preferred_adducts_only` (default off) are options of
    each merge. It writes `metabolome.csv`, `metabolome_samples.csv`, `metabolome_removed.tsv` and
    `merge-report.json`.
  - **Polarities acquired under other names pair** by the words that set each polarity's files apart (A1 in
    `20230125_SWATH_A1_C_CE20_d1` and `20230208_SWATH_A1_A_CE20_d2`), for the person to confirm, or by the
    pairs they give (`sample_pairs`); the merge pairs the columns by the confirmed table. Only where the file
    names give no Sample pair a class both polarities agree on, case aside (d2 against a re-injection's d2r2,
    or no class at all), is each Sample pair's class proposed from the letters of its shared letter+number
    word (A1 to D4 -> A), and `pairing_warnings` says so. Where even one pair agrees, every pair keeps its
    names' class, and each pair whose classes differ is a warning (one typo, or WT against wt, never relabels
    the batch). Letters naming a blank, QC or standard (Blank1, QC2, Std3) make no group, and the letters must
    make at least two groups of at least two pairs each, so sample numbers, subject numbers and plate wells
    beside a factor the names give keep the names' classes.
  - **Each file's acquisition type is read from its raw header**, never assumed or inferred. Before this,
    every file was proposed as DDA, and SWATH or AIF data run as DDA get almost no MS/MS.
    - The check reads the headers with the configured raw-metadata extractor, through the repository
      preflight's bounded reader and parsing, with limits of its own so that a check (and the one
      `write_contract` runs) returns within the MCP tool's 600 s: every file up to 200 per polarity, past
      that the first and the last; at most 30 s per file (more for a large Waters or ion-mobility folder) and
      240 s for the whole check, in chunks that take at most half of it. `run_extractor` takes those limits
      and the deadline as optional arguments; a campaign passes none.
    - Reads are kept in the backend process by the extractor build (the sha256 of its exe and of every file
      beside it) and the file's path, size and modification time before the read, failed reads included; a
      read the deadline cut short is not kept, and the next check reads such files first, one at a time, so a
      file that hangs meets its own limit and is kept as a failed read. An input that changed during its read,
      a file or a folder, is read again. A check waits for a read another check has in flight rather than
      start a second extractor on the same files.
    - Only a DDA or SWATH verdict of a file's own header settles its type (DIA with isolation windows is
      SWATH). `acquisition_type_pos` / `acquisition_type_neg` (DDA, SWATH or AIF) is asked for every other
      file: no extractor, a header not read (or not in time, or a Bruker BAF folder, which is never given to
      the reader), no type the Console reads, an AIF verdict, or a file between the first and the last,
      whose type those two only suggest. The prompt and the warnings count header-read and answered files
      apart. Files whose own headers give more than one type ask `acquisition_type_mixed_pos` /
      `acquisition_type_mixed_neg` (`as_read`).
    - AIF meets the repository campaign's collision-energy guards, over all of a polarity's AIF files: more
      than one MS2 collision energy among them (in one file or across files) with a Console lacking
      MsdialWorkbench#825, and AIF recording no energy with a Console carrying it (which stops on such a
      file), are blockers; with #825, files whose energy sets differ are a warning. `write_contract` judges
      them again over the rows the confirmed table includes and the types it states, so a file left out no
      longer counts; the table's AIF warnings are in the preview and the result (`acquisition_warnings`) and
      recorded in the contract (`raw_headers.aif_warnings`).
    - A header recording the other polarity than its folder's, or both (polarity switching), is a blocker.
    - A pair whose two files are of different types is proposed with no type (`acquisition_type_conflicts`
      gives both, `acquisition_type_required` the rows), and the contract refuses every included row without
      one until the person states it in `sample_table`; a table sent back as proposed, or a blank type, never
      passes for the person's word.
    - The analytical order follows the headers' acquisition start times when every file of every polarity
      records one. `raw_headers` shows the evidence per polarity (methods, isolation targets, MS2 collision
      energies, the extractor build), the proposal is bound into `check_digest`, and the contract records it
      with the answers taken; for an answered file, why it was asked and what its header suggested are bound
      too. What a reader leaves beside a file input under that file's name, or inside a folder input where a
      known reader rule names it (`reader_created`), is detected afterwards, not prevented: it is said on every
      later check, bound into `check_digest` and recorded in the contract. Any other name that appears
      meanwhile is said in that check only (`appeared_during_read`), without naming a writer and unbound;
      `write_contract`'s preview and result give its own check's warnings (the check's AIF warnings over every
      file replaced by the table's), and the contract records every name a check of the digest it confirms saw
      appear, a preview's included.
    - Sample metadata is compared with the table proposed, whose order and classes may be the headers' and
      the letters', rather than with the names.
    - The contract schema is `...lab-lcms-metabolomics-contract.v2`: a v1 contract, written before this, is
      refused wherever it is loaded, and a plan refuses a contract row of no type the Console reads. Check and
      write again.
  - **`alignment_rt_tolerance`** (minutes, optional) is recorded in the contract and reaches both polarities'
    runs ("Retention time tolerance for alignment"); unset, the template's 0.1 min stands. Either value is
    fixed for both polarities: a plan's or a workset's other value is a blocker, as `workflow_overrides` for it
    is. Capillary electrophoresis migration times usually need more, and the person decides it.
  - MS-DIAL's four-column internal-standard text (Name, Pos-m/z, RT, Adduct) is read as it is.
  - The contract and its record (`lab-lcms-metabolomics-contract.json`, `lab-lcms-metabolomics-record.jsonl`)
    name the libraries and the raw data by location, and are local only.
  - An in-silico MSP is searched as `library_kind: predicted`, so MS-DIAL publishes its matches as in-silico
    evidence. `msp_annotator_settings.tsv` carries the column only when a row states it.
  - A library searched with retention time is read for its records' RTs: none refuses the library, and some
    missing is a warning, because MS-DIAL matches a record without an RT on MS/MS alone.
  - The two laboratory agents refuse an analysis root that holds the other's contract, since both write the
    same file names there.
  - Gap-filled standards: the run's `<alignment>.mdpeakid.tsv` says where MS-DIAL did not detect the
    normalizing standard. A Sample or QC there refuses normalization unless the person accepts the heights
    (`accept_gap_filled`); a Blank or Standard injection there, without the standard, or with it below 10% of
    the lowest Sample or QC height keeps its raw heights, with the reason.
  - Which spots are internal standards: every spot a library or a standard's own text row annotated as that
    standard (tiers A and B), whatever else the resolver said of it. A spot only searched for a standard (C,
    D) stays a feature, kept as its own row outside the InChIKey groups, so a labelled standard never takes
    its endogenous compound's place; the merge names each, with any row it shares a first block with.
  - The merge reports possible isomers: InChIKey groups whose rows differ in stereo block or lie more than
    0.5 min apart, with the row kept.
  - The check refuses what a later step would: an annotation key it does not read, a library path or
    analysis root outside ASCII, and folder raw data (`.d`, `.raw`) with a Console that cannot be shown to
    read them.

### Changed
- `lab_lipidomics.resolve_standards` takes which spots tier A reads as a library annotation
  (`library_annotated`, the LBM library's by default), and `lab_lipidomics_agent.run_outputs` takes the
  contract's key and whether the library copy is needed. Laboratory lipidomics is otherwise unchanged, but for
  the next item.
- The guided plan takes `alignment_rt_tolerance` as an answer (a positive number of minutes; anything else is
  a blocker). Unset, the parameter template's value stands, as before. A laboratory lipidomics contract does
  not fix it, so a lipidomics plan given it applies it as a deviation the plan records, as it applies
  `workflow_overrides.alignment_rt_tolerance`; before, the answer was not applied.
- `raw_metadata_preflight.run_extractor` and `input_timeout_seconds` take optional per-input limits, and
  `run_extractor` a deadline, after which no extractor process starts or runs on (an input it leaves unread
  is `timed_out` with `deadline` true). A campaign passes neither, and its reads are as before.

## [0.5.45] - Unreleased

### Fixed
- **The analyst's retention-time decision now reaches MSP libraries.**
  - Each MSP annotator row in `msp_annotator_settings.tsv` carries its own RT settings, and the Console reads the row.
  - `_apply_retention_time_use` wrote the answer to `use_retention_time_for_annotation` and `retention_time_tolerance` into the LBM lines and the Text annotator rows only. An MSP with retention times was therefore always searched without them, with a tolerance of 0.5 min, whatever the analyst answered.
  - The decision is now written into every MSP and Text row, except the internal-standard row and a row marked `rt_fixed`, whose RT use was decided for that library alone. The laboratory metabolomics presets planned next need such rows.
  - Not affected: laboratory lipidomics, which uses only the LBM2, and the repository campaign, which gives no RT answer.

## [0.5.44] - Unreleased

### Added
- **A defined quantification input for laboratory lipidomics, format 1**
  (`msdial-lipidomics-quant-input/1`, specified in `docs/lab-lipidomics-quant-input.md`).
  A first-time user can prepare one correctly, and Interactive reads it without a
  language model.
  - **What it holds:** what `quant_utility_table.xlsx` and the two internal-standard
    text libraries hold, in four tables: Settings, Standards, Class standards and
    Selection.
  - **Containers:** the recommended one is one workbook; the same tables as
    `.tsv`/`.csv` files in one folder also work.
  - **Settings:** the sample amount and its basis (uL plasma, mg tissue, 10^6 cells),
    and the unit of the spiked amounts.
  - **Standards:** one row per standard, with the amount added to each sample and the
    adduct, m/z and RT in each polarity. The amount per sample unit is computed as
    spiked amount / sample amount. A blank m/z is computed from the formula; a given one
    is checked against it.
  - **Columns and class names:** columns are found by header, in any order and any
    case. Lipid class names are accepted in either MS-DIAL spelling (Cer_NS or Cer-NS)
    and converted, with a note.
  - **Reporting:** every problem is named by table, row and column.
- **Reading it** (`msdial_app/lab_quant_input.py`):
  - It gives the shapes the earlier form gives: the standards and their amounts, the
    lookup, the selection and an `InternalStandard` library per polarity. The contract,
    the resolution, normalize, the merge and worksets are therefore unchanged.
  - `quant_table_path` takes either form, and `quant_format` says which was read. A
    format-1 input is given alone, without `is_text_pos`, `is_text_neg` or
    `amount_column`.
  - The contract records `quant_format`. A folder input is bound by one digest of its
    four tables.
- **`msdial_lipidomics_quant_input_template`** writes a new input. It previews by
  default, writes with `confirmed=true`, and never overwrites.
  - By default it holds the laboratory's EquiSPLASH set without amounts or retention
    times, the laboratory's class standards and selection, an Instructions sheet and a
    Class names sheet. In Excel, the Polarity, Use, Sample basis and unit cells are
    drop-down lists.
  - `salt="HCOONH4"` takes formate adducts in negative mode.
  - It can also convert the earlier form, with `from_quant_table_path`,
    `amount_column` and both texts.
  - After writing, it checks the input, so a template lists the cells still to fill
    in.
- **`msdial_lipidomics_check_quant_input`** checks an input on its own, before any raw
  data are at hand. It reports the tables as read, the classes each polarity cannot
  quantify, the selection's reconciliation warnings and, with `solvent_type`, the salt
  evidence.
- **Examples:** `examples/lab-lipidomics/quant_input.xlsx` and `quant_input_tsv/` are
  the demo's inputs in format 1. They are generated, with
  `resources/lab_quant_input_defaults.json`, by
  `scripts/generate-lab-quant-input-defaults.py`.
  - The tests check that they match the earlier form.
  - They also check that the same batch gives a byte-identical `lipid_metabolome.csv`
    from the earlier form, the workbook and the folder.

### Changed
- `msdial_lipidomics_check_inputs` asks for the quantification input first. It asks
  for the texts and the amount column only for the earlier form; until the form is
  known, those two questions are marked not required.
- `msdial_lipidomics_write_contract`: `is_text_pos`, `is_text_neg` and
  `amount_column` are optional, since a format-1 input does not take them.
- The lipidomics skill:
  - It says how to help a person prepare the input: offer the template, say what to
    fill in, and check with `msdial_lipidomics_check_quant_input`.
  - It never guesses a value and never edits the person's file.
  - R4 to R6 now cover both forms.

### Fixed before release (review)
- The template's Settings drop-down lists were one row too high, so Excel refused a
  typed sample amount and basis. They are now placed from the rows written, and a
  test reads them back.
- **Units in headers:** a unit in a header's parentheses must agree with what the
  column is read in. `Spiked amount (nmol)` under pmol, or `Pos RT (s)`, was read
  1000 times off, or in the wrong unit, without a word; now it is a problem. So is an
  unknown setting whose name says "unit".
- **Selection, Use column:**
  - With a Use column, every row says TRUE or FALSE; a cleared cell is a problem.
  - Use may also be headed Include or Report.
  - A TRUE/FALSE column under another header, with no Use column, is a problem rather
    than every row being used.
  - Classes reported by more than one adduct are now a warning, as the specification
    says.
- **Text the library could not hold:** non-ASCII text that MS-DIAL's ASCII library
  cannot hold is now a problem in the check, instead of making the contract write fail
  later. This covers a class, an adduct, and full-width digits in a number. Infinite
  and NaN numbers, an RT over 300 min (a date, or seconds) and an adduct without charge
  are problems too.
- **Rows and formulas:**
  - In a `.csv` file, a row with more cells than the header (a decimal comma splits a
    number in two) is a problem.
  - An Excel formula showing nothing is blank, not "never calculated".
  - A row is read only when a column of the format holds something, so helper columns
    of the person's own make no rows.
  - Error values are named as such.
- The m/z columns may be left out when every standard has a formula.
- **Telling the forms apart:**
  - A workbook with format-1 sheets but a misnamed Settings sheet is format 1, and its
    missing sheet is named.
  - A missing `quant_table_path` is said as missing, and the earlier form's questions
    are not asked.
  - Workbooks beside the raw data are described by their sheet names alone: format 1,
    the earlier form, another workbook, or unreadable. A damaged one no longer stops
    the check.
- **The converter:**
  - It passes on the earlier workbook's warnings.
  - It works the spiked amounts back in the direction sheet 1's formula takes, since
    the per-10^6-cells column multiplies by row 4.
  - It says when the converted sample amount differs from row 4.
  - It uses sheet 1's pmol column only when the amount column is in pmol.
- **Wording:**
  - The standalone check warns about the classes each polarity cannot quantify.
  - Messages, the salt evidence, the drift check and the merge use format 1's names
    (Selection, Class standards, quantification input), and say each changed input
    once.
  - check_inputs returns the tables compactly.
- The folder template also writes `instructions.txt` and `class_names.tsv`.

## [0.5.43] - Unreleased

Contains 0.5.41 (#74) and 0.5.42 (#75), which are not merged yet; merge this after them.

### Added
- **Set up by asking Claude Code.** A beginner opens Claude Code anywhere and asks it to
  set up this repository for MS-DIAL agent analyses. README.md gains a quick start with
  that one prompt, and instructions for the agent to follow:
  - check Git and Python 3.10+, without installing either unasked;
  - clone into a folder that will stay;
  - show the `--dry-run` plan, and run the setup with `--yes` only after the person
    agrees, since the setup cannot ask through the agent's tool;
  - report every `attention` or `failed` step;
  - hand over to a new session, with the first prompt.

  The tutorial points to it as well.
- **The setup installs the official Console** (`scripts/setup_claude.py`, step 7). When
  no Console found is new enough, it offers the MS-DIAL Console prerelease 5.5.261018
  (tag `MSDIAL-v5.5.261018`, Yuki's release of 2026-10-18) for Windows, macOS or Linux:
  - It downloads the archive from GitHub and checks its size and SHA-256 against
    values pinned in the script.
  - It unpacks it beside the checkout, where both this setup and Interactive's Console
    search look, refusing any member outside that folder.
  - It makes the launcher executable, since the archives carry no Unix modes, then
    judges the Console and starts it for its `--help`.
  - On a Mac without Rosetta 2 it prints the install command and downloads nothing.
  - A dry run downloads nothing; `--no-console-download` turns the offer off.
- The Claude Code registration also works where `claude` is not on `PATH`, as in the
  Claude desktop app's Code tab: the setup then uses the Claude Code that started it
  (`CLAUDE_CODE_EXECPATH`).

### Changed
- The Console placeholder "5.5.2610xx" is replaced by the published 5.5.261018 in the
  README, the tutorial, the lipidomics skill and the setup. MsdialWorkbench's tag
  `MSDIAL-v5.5.261018` contains #826, #830, #832 and #837.
- Interactive's Console search (`discover_console_paths`) and the setup's find a
  self-contained Linux or macOS Console by its `MSDIALCUI` launcher, not by its dll,
  which would need a separate `dotnet`.

## [0.5.42] - Unreleased

Contains 0.5.41 (#74), which is not merged yet; merge this after it.

### Added
- **QA figures, drawn automatically inside the analysis flow** (the user's request of
  2026-10-18, after the laboratory lipidomics agent's first end-to-end run on a Mac:
  QA came out as tables only, and the agent wrote and ran its own R script for the
  figures, at a cost in tokens). `msdial_app/qa_figures.py` is a small SVG plotting
  layer on the standard library only (no numpy, matplotlib or R): nice-number axes,
  small multiples, points and lines, boxplots, stacked histograms, a horizontal dot
  plot, legends, titles and wrapped subtitles, and text shortened from Helvetica's
  metrics so that it never leaves its panel. The look follows the laboratory's reference
  figures: group colours from the reference categorical palette in a fixed order over
  the sorted class names (the same group, the same colour, in every figure of a page),
  QC near-black, Blank grey and drawn as a cross. No hue is generated beyond the
  palette's eight: the ninth to sixteenth study groups reuse it with squares, then
  triangles, then diamonds, so that every group stays distinct (beyond 32 groups the
  rest share one "other" legend entry). Eight figures, each a function of
  parsed inputs that returns SVG text or the reason it was not drawn:
  01 aligned features with a detected peak per injection; 02 summed peak height per
  injection (both on an axis that starts and ends on a labelled line, the line joining
  the study injections only, never through a Blank); 03 log10 peak-height boxplots per
  injection, the median drawn in whichever of near-black or the surface stands out on
  the box (light on a QC box), and, where a box is narrower than 8 px, only its most
  extreme outlier on each side and no outline, so that the figure stays small on a long
  run; 04 each internal standard's
  height relative to its mean over the non-Blank injections, a +/-20% band and its CV in
  the panel title, the subtitle naming the standards not drawn: those the standards table
  could not resolve in that polarity, and those the resolution found but normalize did
  not use; 05 a PCA of the study injections (Blank excluded) from the
  reference-matched features, log10(height + 1), autoscaled, by the eigenvectors of the
  samples x samples Gram matrix (cyclic Jacobi up to 80 injections, subspace iteration
  above), with explained variance, feature count and labels placed so that they do not
  overlap; 06 the sample-to-Blank ratio per feature, log10((mean sample + 1) / (mean
  Blank + 1)), stacked by reference match, with the share at 3x or more and, beside it,
  the QA report's `sample_blank_ratio_ge_3` computed the report's way (median Sample over
  median Blank height, over the features detected in a Blank only): a feature with no
  Blank signal counts as clean in the figure and is left out by the criterion; 07 the
  automatic RT correction's anchor offsets per injection, used and not used, from
  `automatic_alignment_rt_correction_anchors.tsv`; 08 the lipid class totals per sample
  from `lipid_metabolome4classsummary.csv`, on a log scale, classes ordered by median,
  one lane per group with its samples spread across it, so that samples with equal
  totals stay separate points. The QA matrix marks `Reference matched` per injection: a
  feature counts as reference-matched when the peak of any study injection in it is, and
  then all its study heights enter the PCA; figure 06 counts each feature once; figure 04
  draws the standards normalize used, not every one the resolution found. Figures drawn
  earlier by ad-hoc code that set the unmatched peaks of a matched feature to 0, or
  counted a feature once per flag value, show other numbers (see Validated).
  A figure the data cannot support is skipped with its reason (no Blank, no anchor file or
  one naming another run's files, fewer than three study injections or two varying
  features for the PCA, one non-Blank injection for 04, no positive concentration for
  08, a missing polarity); an empty or non-numeric height counts as not detected and the
  page says how many there were. Nothing depends on a clock or a random number: the same
  inputs give the same bytes.
- **Where they are written** (`msdial_app/qa_figure_outputs.py`), each beside a step that
  already writes, and returned by path only (`qa_figures`: the folder, `qa_report.html`,
  each figure's file and title, skipped figures with their reasons, warnings; never SVG
  text or image data):
  - a job's LC-MS QA (`/api/qa/report`, so `msdial_generate_lcms_qa` with a `job_id` or
    `manifest_path`, and `msdial_complete_guided_analysis`, which now passes `qa_figures`
    on): 01, 02, 03, 05, 06 and 07 into `<run directory>/qa_figures/`, with a
    self-contained `qa_report.html` (every SVG inline, a one-sentence caption and a note
    on how to read each) and `figures.json`, which records the QA matrix and the anchor
    file by name and sha256 and the Interactive version that drew them. A figure no
    longer drawn has its earlier file removed. A QA matrix named by `file_path` alone gets
    none (`not_written`): it has no run folder. The matrix is read once: the QA report
    hands each spot's parsed rows to the figures (`build_lcms_qa_report`'s new
    `spot_observer`; the report does not depend on it and never sees its failure), so the
    figures add their drawing, not a second read. On a synthetic 10,000-feature,
    300-injection matrix (3 million rows) the QA took 21 s alone and 30 s with the
    figures, so `msdial_generate_lcms_qa` now gives the request 180 s instead of 120 s,
    as `msdial_complete_guided_analysis` already did;
  - a confirmed `msdial_lipidomics_normalize`: figure 04 for its polarity, from the run's
    Height export and the standards table just written, into
    `<root>/quant/qa_figures/`; the preview and `msdial_lipidomics_resolve_standards`
    stay without side effects;
  - a confirmed `msdial_lipidomics_merge`: the batch's page,
    `<root>/quant/qa_figures/qa_report.html`, with both polarities side by side for 01 to
    03 and 05 to 07 (each run's QA matrix found beside its candidate table, as the
    normalize record names it), 04 for both polarities from normalize's standards tables
    and Height matrices, 08, and relative links to each run's own page. They are kept out
    of `quant/lipidome`, whose every subfolder `metabolomics_service.R` reads; the preview
    writes nothing.
  A figure that fails, or a step whose figures cannot be written at all, is a warning in
  the result: the QA report and its status, the normalize and the merge are exactly as
  before. An input that cannot be read costs only the figures it feeds (an anchor audit,
  figure 07; the class summary, 08; a polarity's standards table or Height matrix, its
  04; a polarity's QA matrix, its run panels), with the reason on the page and in the
  warnings, and the reason names no path of this machine. One writer at a time works in a
  figures folder (the backend serves requests on threads), and each file is written under
  a temporary name of its own and renamed into place. The batch page's links to each
  run's page are percent-encoded, so a folder name with `#`, `?`, `%` or a space works.

### Changed
- **The publication bundle carries the run's QA figures** (`qa_figures/`: the SVG files,
  `qa_report.html` and `figures.json`) when the report is made for a job with QA. The
  figures are drawn first unless the ones in `<run directory>/qa_figures` were drawn by
  this Interactive version from the same QA matrix and anchor audit (`figures.json`'s
  version and sha256s) and every one is still there, so a standalone
  `msdial_generate_publication_report` has them too and figures drawn before an upgrade
  are never bundled; a redraw is fed by the report's own read of the matrix. Each is
  scanned as the bundle is;
  one that names what a shared report may not is left out with a warning
  (`bundle_warnings`) rather than refusing the report. Without figures the report, its
  audit and its bundle are as before.
- The skills say where the figures appear, that the agent gives the person the paths of
  the page and the figures (and may open the page), reads out skipped figures, and never
  writes its own plotting code for them (`msdial-guided-analysis`, "QA figures";
  `msdial-lipidomics-workflow`, "The QA figures"). The README and the laboratory
  lipidomics tutorial describe them where the outputs are described.

### Validated
- On the laboratory's Mac output of the public demo (mouse plasma, SPF vs GF, 6 samples
  and a Blank per polarity), read only, every figure and both pages were drawn and
  checked against the R script the agent had written. The internal-standard CVs agree
  with its panel titles for every standard (positive CE 11%, TG 13%, MG 4%, ...;
  negative 3% to 7%); figure 04 uses the standards normalize used, so the negative
  figure has no FA 16:0(d3), which the R script took from the resolution table. Two
  numbers differ, deliberately, because the R script read the per-injection
  `Reference matched` flag of the QA matrix as if it were the feature's: its PCA set a
  study height to 0 wherever that injection's own peak was not matched (positive PC1/PC2
  52%/15%, negative 37%/20%), and its ratio histogram counted each feature whose flag
  differed between injections twice (1,637 and 1,696 rows for 1,323 and 1,490 features:
  63% and 59% at 3x or more). Here a feature matched in any study injection keeps every
  study height and each feature counts once: PC1/PC2 66%/14% and 52%/18% (R's prcomp on
  the same matrix gives the same), and 55% and 55% at 3x or more. Fed the R script's
  matrix, the PCA here reproduces its 52.06%/15.36% and 37.07%/19.74%. Beside that share,
  figure 06 prints the QA criterion as the QA report computes it, 45% (482 of 1,075
  Blank-detected features) and 23% (209 of 906), equal to the run's
  `sample_blank_ratio_ge_3` (0.448 and 0.231). The positive figure 04 names FA 18:0(d3)
  as not resolved in that polarity; the negative one names CE, MG, DG and TG as not
  resolved and FA 16:0(d3) as resolved but not used by normalize. The figures fed by the
  QA report's read of each matrix equal those from a read of their own.

## [0.5.41] - Unreleased

### Added
- **The laboratory lipidomics skill ships with Interactive, for anyone** (the
  user's decision of 2026-10-16). `skills/msdial-lipidomics-workflow` is the
  Agent Skill that drives the five `msdial_lipidomics_*` tools and the guided-run
  tools from two raw folders to `lipid_metabolome.csv`. It was kept in the public
  reanalysis repository (`.claude/skills/msdial-lipidomics-workflow`, at 3f01ee0)
  and is now here, reviewed for a general audience: any laboratory's own LBM2,
  standards and workbook, the official LBM2 (`msdial_download_official_library`
  `lipidomics`) for a laboratory without one, the public demo and the example
  workbook and standard texts named, where a new enough Console comes from (the
  prerelease, named from `msdial_check_official_console_releases`, or the
  local-source build with `select_after_build=false`, which needs `git` and the
  .NET SDK 8+ on the MCP server's PATH), and the analysis root rules as
  `check_inputs` applies them (a git checkout or a temporary folder is refused, a
  system drive or the user profile is said). The raw-data copy is made by the
  agent only where it has file tools of its own (Claude Code); in Claude Desktop
  the person makes it, and either way the copy is checked with `check_inputs`
  before the contract. The normalize job is polled by the `job_id` the confirmed
  call returns. Every confirmation rule is kept, and so is the private-library
  rule, now its own section. Interactive is its canonical home;
  `msdial-guided-analysis` points to it for such a batch.
- **`scripts/setup_claude.py` and `Setup MS-DIAL for Claude.cmd`: setup in one
  command.** Standard library only, no administrator rights; it explains each
  step and asks before it changes anything (`--yes` answers yes, `--dry-run`
  shows the plan, changes nothing and starts no Console). The `.cmd` takes
  `py -3`, then `python`, only when it runs Python 3.10 or newer (so not the
  Microsoft Store alias, a launcher without Python 3, or an older Python), with
  the Python install manager's automatic install turned off
  (`PYTHON_MANAGER_AUTOMATIC_INSTALL=false`): it never installs a Python before
  the setup asks anything. The setup checks Python 3.10+; runs
  `<python> -m pip install -e .[mcp]` only when `mcp` or `psutil` is missing or
  outside the extra's range (`--user` where site-packages is not writable);
  registers the MCP server with `claude mcp add --scope user` when the `claude`
  CLI is on PATH, showing an existing user-scope registration first (command and
  arguments, environment variable names only) and replacing it only on a yes,
  after saving the whole entry to `~/.claude/mcp-backups/` and restoring it with
  `claude mcp add-json` when the add fails after the removal; merges an
  `msdial-interactive` entry into Claude Desktop's `claude_desktop_config.json`
  (the Microsoft Store installation's copy included) after a timestamped backup,
  every other key kept, or creates one with `--desktop`; copies both skills into
  `~/.claude/skills` (or `$CLAUDE_CONFIG_DIR/skills`), moving a differing copy to
  `~/.claude/skill-backups` first, outside the folder Claude Code loads skills
  from, and removing a link or junction there without writing through it;
  builds the skill ZIPs; and judges every MS-DIAL Console it finds without
  starting it, by what its assembly holds (`--peak-ids-only` and the markers
  `check_inputs` requires, in its words, `LAB_CONSOLE_NEEDS`). A Console named
  with `--console`, or the ones found after one question, is then started as
  `check_inputs` starts it (`console_capabilities`); a named Console that is not
  there, a stale saved setting included, is said, and a folder stands for the
  `MSDIALCUI` in it. Redirected output is UTF-8, so that a non-ASCII path in a
  command to copy survives. It never saves a Console setting and never prints an
  environment variable's value.
- **`examples/lab-lipidomics/`**: the inputs of the public demo that its archive
  lacks or holds in a form the agent cannot use, under the demo's CC BY 4.0
  licence, with a README crediting the record and explaining them.
  - `quant_utility_table.xlsx`, the workbook the MS-DIAL laboratory uses with the
    demo (sheet 3 already reconciled for the merge), with its three sheets and the
    columns the agent reads. Two parts differ from the laboratory's copy:
    `docProps/core.xml`'s creator and last-modified-by are empty, and
    `xl/workbook.xml` no longer holds Excel's `absPath`, the local folder it was
    last saved in. Every other member is the source's, byte for byte and in
    order, and Interactive's reader reads it exactly as it read the source
    (`tests/test_example_workbook.py`).
  - `EquiSPLASH_Pos.txt` and `EquiSPLASH_Neg.txt`, the archive's two standard
    texts as the laboratory edited them for its 2026-10-15 demo run (the sha256
    digests its contract recorded): `SM 18:1;2O/18:1(d9)` and
    `Cer 18:1;2O/15:0(d7)` renamed with `;O2`, as sheet 1 names them, and the
    negative text's `PA 15:0_18:1(d7)` row, which sheet 1 does not prepare,
    removed. As published, the texts stop `check_inputs` with the example
    workbook (two prepared standards in neither library).
    `tests/test_example_standards.py` undoes the edits and compares the result
    with the record's CRC-32s, and checks that the shipped texts give the
    tutorial's step-4 read-out with no blocker. `.gitattributes` keeps their
    CRLF bytes as they are.
- **`docs/tutorials/lab-lipidomics.md`**: a beginner-safe English tutorial with
  the public demo (Zenodo 10.5281/zenodo.10957708, credited in full): what you
  get, what you need, the setup (Windows options through `py -3` or the `.cmd`),
  the demo's download into one folder with the example files copied into
  `data2`, the analysis root and `raw_location` (who makes a copy in Claude Code
  and in Claude Desktop), the conversation step by step with the MS-DIAL
  laboratory's answers, every confirmation and why, timings, the outputs and the
  R script from the same record, your own data (standards text format
  included), troubleshooting and privacy. The Console it needs is named as "the
  MS-DIAL Console prerelease 5.5.2610xx or later", a marked placeholder until the
  prerelease is cut (its comment lists every file that holds it), with the build
  from source (Git and the .NET SDK 8+) as the way meanwhile.

### Changed
- `scripts/package-agent-skill.py` packages every skill under `skills/` into
  `dist/<name>.skill.zip` (`msdial-guided-analysis.skill.zip` keeps its name);
  `--skill NAME` packages one (repeatable) and `--list` prints the names. Each
  ZIP holds the skill's folder at its root, with a fixed timestamp so that the
  same skill always packages to the same bytes, and a skill whose frontmatter
  does not name its own folder, or has no description, is refused.
- The distribution ZIP carries the setup wrapper and `examples/`.
- README: a quick start for laboratory lipidomics with Claude near the top, and
  the Agent Skill section names both skills, the packager options and the setup.
  `docs/agent_integration.md` describes the setup and the manual route for both
  hosts. Both now use placeholder paths where they named this project's own
  folders, and the test prompts are in English. So do
  `skills/msdial-guided-analysis/references/repository-reanalysis.md` (the
  example `workspace_root`), `docs/distribution_ja.md` and
  `docs/public_repository_reanalysis.md`, which named folders of the developer's
  PC; the setup copies the first into every user's skills.
- `lab_lipidomics_agent.LAB_CONSOLE_NEEDS`: what `check_inputs` requires of a
  Console, in the words of its refusal, now a module constant the setup reads too.
  The check itself is unchanged.
- `.gitattributes`: `*.cmd` keeps CRLF in every checkout and source ZIP,
  `*.xlsx` is binary, and the example standard texts are not converted.

## [0.5.40] - Unreleased

### Changes a campaign run planned with this version
- **The folders inside one declared per-sample archive are copies** (the
  user's decision of 2026-10-15: "1つだけで良いと思っています", one is enough).
  0.5.39 left this undecided and refused such an archive: a file of the used
  one's stem in another folder of the same declared zip counted as the declared
  input found a second time, so `S1.zip` holding `S1/A/S1.mzML` and
  `S1/B/S1.mzML`, `S1/RAW/S1.d` and `S1/mzML/S1.mzML`, `S1/run_a/data.mzML` and
  `S1/run_b/data.mzML`, or, in a positive unit, `S1/POS/S1.mzML` and
  `S1/S1.mzML`, stopped the lease ("2 inputs in what the archive held"). Now
  the one encoding rule applies as written: it uses one copy (by rank, then the
  first by path on a tie) and records the others with their reasons in
  `encoding_choices`, and such a unit runs. What is still refused as found more
  than once is what the rule itself keeps apart: two polarities' folders
  (`S1/NEG/S1.mzML` beside `S1/POS/S1.mzML`, and `S1/POS/S1.mzML` beside
  `S1/S1.mzML` in a negative unit), read by the polarity tokens the rule
  already reads (`name_polarities`, `_one_sample_polarities`), and two stems
  (`run_1.mzML` beside `run_2.mzML`, in one folder or two), which are two runs.
  Also still refused, as 0.5.39 refused it: a pair whose folders differ by one
  that may state a polarity by a mark the rule does not read (`Posi/` and
  `Nega/`, `ESI+/` and `ESI-/`, `ESIpos/` and `ESIneg/`, `P/` and `N/`, `(+)/`
  and `(-)/`, `ポジ/` and `ネガ/`, and such a folder beside one of no mark), in a
  unit of either polarity (`_unread_polarity_mark`,
  `_folders_bear_an_unread_polarity_mark`). The rule reads a polarity only from
  a `pos`, `positive`, `neg` or `negative` token, and such a mark is still not
  read as one; but before the review the decision took `S1/Posi/S1.mzML` and
  `S1/Nega/S1.mzML` for copies, and a positive unit used the `Nega/` file, the
  first by path, under its positive declaration. A false mark costs a refusal
  for a person to look at, never a run; folders whose names bear none (`A/`,
  `RAW/`, a desktop's `S1 - Copy/`) are copies.
  `_another_encoding_beside` decides it; its docstring, the `encoding_rule`
  module docstring and `docs/public_repository_reanalysis.md` state the
  decision in place of the undecided exception, and the README says it too.

### Added
- The copies left unused are on record. A `manifest.declared_archive_inputs`
  entry whose archive held its sample in more than one folder gains
  `copies_unused` (the paths under the data root of the copies left in other
  folders) and `copy_rule` `per_sample_archive_folders_are_copies_2026_10_15`.
  The lease warns of them: `declared_archive_copies_unused` in the manifest's
  and the attribute stage's warnings (the stage counts
  `declared_archive_copies_unused`), and one `archive_warnings` entry per such
  archive whose message names the archive, the copy used and the copies left.
  The preflight's `campaign_disposition` carries the warning with a detail line
  naming them, and the analysis-CSV record carries it with
  `declared_archive_copies`. A split part carries the parent's
  `declared_archive_inputs` entries of its own inputs, and the warning only
  where one of them left a copy. Only where a copy was left, so every other
  lease records what it always did.
- **A unit whose declared inputs hold no format MS-DIAL opens is an exclusion,
  not a failure to retry.** MB-POST MPST000213 (unit `334ab3e2f0e7600f4290`):
  its three per-sample zips inside `MPST000213.tar` each held a JEOL msAxel
  project (`.xml`, `.pm`, `.dfa`, `.ba`, `.ppl` ...), and the campaign runner
  counted its lease as a failed download three times, though every retry
  downloads the same objects into the same files. Where every missing declared
  input is a declared archive the extract stage expanded, whose files the lease
  saw on disk, none of them of a format MS-DIAL opens, a campaign converts or an
  archive left packed, and no declared input is found more than once, the
  attribute stage raises
  `DeclaredInputsUnsupportedError` (a `ValueError` with the same text), and the
  failed lease's `download_failure` gains `retryable: false` and `disposition`
  `{decision: "exclude", reason: "declared_inputs_unsupported_format", inputs:
  [{declared, archive, files, suffixes}]}` (the archive's path under the data
  root as its extraction recorded it, the files it held and its ten commonest
  suffixes). `error_type` names the subclass. Any other combination (an input
  missing for another reason, an input found more than once) keeps the plain
  failure with no `disposition`. `download_exclusion(manifest)` reads it.
  An exclusion is never retried and deletes the raw data, so it stands only on
  what the lease saw (review of 0.5.40): an archive holding a format MS-DIAL
  opens by `SupportFormat.cs` (`encoding_preference`'s vendor, open and folder
  suffixes as well as the lease's raw suffixes, so `.ibf`, `.lrp` and `.imzML`,
  which the lease takes as no input, too), an mzXML, or an archive the extract
  stage left packed; an archive none of whose places is on disk (a record at
  odds with the disk); and one that expanded to no file are each the plain
  failure, and its message names what lies there or says `(not on disk)`. The
  place itself is walked too, so a zip that expanded to the vendor folder
  `S1.d` is seen to hold one.
- A failed repository download job carries the record its lease wrote into the
  unit manifest, verbatim, as `download_failure` (the job's summary, which
  `/api/jobs/<id>` and `msdial_interactive_job` return, includes it), so a
  poll of the job says what the manifest says, `disposition` and `retryable`
  included. A job whose lease wrote no record (refused before it began) has
  none.
- The capabilities `repository_declared_archive_copies` and
  `repository_download_exclusion_disposition`.

### Changed
- The campaign-authorized discard (`msdial_discard_repository_raw`,
  `discard_download_lease`) already took a `download_failed` unit; one whose
  failure is such an exclusion is discarded the same way, under an approval
  covering boundary 5 or with `confirmed=true`, and its `discard_reason` now
  names `declared_inputs_unsupported_format` (its `download_failure` stays on
  record).

### Tests
- `test_nested_archive_inputs`: the tests that pinned the 0.5.39 refusal are
  updated to the decision. `test_one_name_in_two_folders_of_a_declared_zip_is_refused_until_the_user_decides`
  is now `test_one_name_in_two_folders_of_a_declared_zip_is_copies_and_one_is_used`
  (the same four pairs, and three copies, each with one used and the others
  recorded and warned of), and
  `test_two_runs_of_one_file_name_in_their_own_folders_are_refused_not_chosen_by_path`
  is now `test_one_file_name_in_two_run_folders_is_one_acquisitions_copies_by_the_decision`.
  Still refused: both polarities' folders (unchanged), a copy beside one of the
  other polarity in a negative unit (new), two stems, now also in two folders,
  and (review) folders a polarity mark the rule does not read may separate,
  `Posi/`/`Nega/`, `ESI+/`/`ESI-/`, `ESIpos/`/`ESIneg/`, `P/`/`N/`,
  `(+)/`/`(-)/`, `ポジ/`/`ネガ/` and `Posi/` beside an unmarked folder, in units
  of both polarities (new), with the marks read folder by folder (new); a
  desktop's `S1 - Copy/` is one more pair of copies.
- `test_declared_inputs_disposition` (new): the all-unsupported case, the
  commonest ten suffixes, an input missing for another reason, a declared
  archive no record says was expanded, one the download does not hold, an input
  found more than once, the job's record and its poll, and the discard of such a
  unit under a campaign approval (and its refusal where the unit keeps its raw
  data). From the review: an archive holding `.ibf`, `.lrp`, `.imzML`, an
  archive left packed, or a JEOL project beside an `.ibf` is a plain failure; on
  the records alone, a place not on disk, an archive that expanded to nothing
  and a zip that expanded to the vendor folder `S1.d` are plain failures, and a
  place on disk holding only JEOL files is still an exclusion whose `files`
  counts what was seen.

## [0.5.39] - Unreleased

### Fixed
- A declared analysis input that is an archive inside the download is matched
  by what it held. MB-POST MPST000213 (unit `334ab3e2f0e7600f4290`) failed its
  lease three times in the MB-POST preflight campaign of 2026-10-14: "3 of its 3
  declared analysis inputs are not in the download:
  Rubroshorea_leprosula_from_Borneo_ID_01.zip, ...". Its one download object is
  the project tar, and the Catalog declares, for each sample row, the per-sample
  zip inside it (kind `file`). The extract stage expands every archive inside a
  download in place (`archives._expand_nested`, with the outer archive's listing
  validation, member, byte and depth budget, digests and member listing), so the
  declared zip was no longer on disk, and only a path equal to it was looked for.
  - The place a nested archive expanded to is read from its extraction record's
    destination rule (`nested_rooted`, `nested_directory`, `container_*`,
    `stream_file`), so a download-store record made by an earlier version
    answers too. `declared_archive_containers` returns that place as `path/`,
    and the input at it or under it is that declared input's
    (`match_declared_inputs`). A place is one location under the data root: it
    is tested against the input's full path there, never against the shorter
    forms a listed name is read in, so `QC/S1/x.mzML` is not under the place
    `S1/`. The lease, the one encoding rule, the convert stage, the analysis-CSV
    builder and a split part ask the same function, so they answer alike.
  - What one declared archive held is one sample's. The one encoding rule
    (2026-10-09) uses one of its encodings and records the others. Of those,
    a file beside the one used (the same folder and stem, whatever its rank:
    `S1/S1.mzML` beside `S1/S1.cdf`, or `S1/S1.wiff` beside `S1/S1.d`, a tie
    the rule's clause 2 gives the first by path) does not count as the
    declared input found twice. Two runs (`run_1.mzML`, `run_2.mzML`) and both
    polarities' folders (`POS/S1.mzML`, `NEG/S1.mzML`) in one declared zip are
    refused as one declared input found more than once ("2 inputs in what the
    archive held"), as one input per sample requires. An archived container,
    or a declared file, counts every input it matched, as it always did.
  - **An exception to the one encoding rule that the user has not decided.**
    A file the rule left unused in another folder of the same declared zip
    also counts, so the zip is refused: `S1/A/S1.mzML` beside `S1/B/S1.mzML`,
    `S1/RAW/S1.d` beside `S1/mzML/S1.mzML`, and, in a positive unit,
    `S1/POS/S1.mzML` beside `S1/S1.mzML`. This departs from the rule's clauses
    1 and 2 ("copies in other folders included"; a tie goes to the first by
    path), under which one of them would run and the other would be recorded.
    The folders inside one sample's archive may be its runs (`run_a/data.mzML`
    and `run_b/data.mzML`, the shape of MPST000213's
    `Data/<run>/RD/MsData.RPS`), and the rule's grouping (one sample row and
    one stem) cannot tell runs from copies. Whether such folders are runs or
    copies is a scientific decision that has not been made. Until it is, the
    lease stops rather than choose one by path. This is a stop, not a reading
    of the rule, and the `encoding_rule` module says so.
  - An mzXML inside a declared archive is converted in a campaign, as a
    declared mzXML is.
  - A declared per-sample archive the unit downloads itself, which expands at
    the data root with its own paths, is matched through the sample the
    archive's name gives its files, as an archived container already was.
  - A declared archive whose contents hold nothing MS-DIAL opens is still
    missing, and the error now says what it held: for MPST000213, "3 of them
    were archives inside MPST000213.tar that the extract stage expanded, and
    none of the files they held is one MS-DIAL opens
    (Rubroshorea_leprosula_from_Borneo_ID_01.zip: 1028 file(s) under
    MB-POST_files_MPST000213.0/Rubroshorea_leprosula_from_Borneo_ID_01, ...)",
    with the commonest suffixes when the tree is on disk. The place it names is
    the folder as it is written on disk (see `expanded_to` below).

### Added
- `manifest.declared_archive_inputs`, only where a declared input was such an
  archive: for each, the declared path and sample; the archive's path under the
  data root, the archive it was in, its depth, format and destination rule;
  where it expanded (`expanded_to`, each folder as it is written on disk: a zip
  holding exactly its own folder in another case, `S1.zip` holding `s1/`, is
  `nested_rooted`, and its place is `s1`, which a case-sensitive file system
  finds, not the zip's `S1`), its file count and bytes; its sha256 and
  md5 from before it expanded and whether the declared checksum matched them;
  and the inputs it gave (an mzML the convert stage wrote is named by the mzXML
  it stands for). Its nested extraction record names the declared input
  (`declared_input`), and the attribute stage counts them
  (`declared_archive_inputs`).

### Not changed
- What is extracted, and how: `archives.py`, the extraction records and the
  member listing are as they were. An unsafe member name in an inner zip still
  refuses the whole extraction, and its members still count against the one
  member budget.
- MPST000213 itself does not become runnable. Each of its zips holds a JEOL
  msAxel project (`Data/<run>/RD/MsData.RPS`, `MS/<n>/Profile.dfa`, ...) of
  several acquisitions (5, 10 and 4 sample runs, with PEG calibration runs and,
  in ID_01, a reference run), which no RawDataHandler reader opens; the unit is
  DART-TOF MS without chromatography or product-ion spectra, the Catalog
  already blocks it for its acquisition mode, and the campaign gate's ELIG-1
  failed it. It now fails with the error above instead of the misleading one.

## [0.5.38] - Unreleased

### Changes a campaign run planned with this version
- **LBM score cutoffs are 0.1 / 0.1 / 0.1** (weighted, simple and reverse dot
  product) by default, for laboratory and public-repository runs alike (decided
  2026-10-14). They were 0.15 / 0.15 / 0.3: Interactive's own defaults, which
  replaced the laboratory template's 0.1 / 0.1 / 0.1 on every guided and
  repository run. Every campaign unit planned with this version annotates lipids
  with the lower cutoffs, so more LBM annotations pass. The campaign now running
  was planned and preflighted with a frozen 0.5.37 and is not affected. A
  parameter template that states its own cutoffs is now followed; the GUI's
  defaults are the same 0.1.

### Added
- `lab_lipidomics`, the laboratory lipidomics agent's reading and checking, with
  the standard library only. Nothing in it runs MS-DIAL; it writes only the files
  it is handed a path for.
  - An .xlsx reader (zipfile + ElementTree). It reads the values Excel saved:
    cached formula values, shared and inline strings, rich text without the
    phonetic guides a Japanese Excel adds, booleans, and merged ranges as their
    top-left value. An error cell (`#N/A`) is no value. A formula Excel never
    calculated is refused when read rather than read as empty. A workbook asking
    to be recalculated on open, and an Excel lock file beside it, are warned about.
  - `quant_utility_table.xlsx`, sheets taken by position.
    - Sheet 1: the header is the row with Name in B and Class name in D, and the
      standards run to the first empty B, so the worked example below them is
      never read. The amount column's header names normalize's `-u`
      (`int (pmol/ul plasma)` -> `pmol_per_microL_plasma`; F, G and H, and nmol
      and fmol). The sample amount above it (H4) and free notes below the
      standards are read back, never interpreted. A formula that multiplies by
      the sample amount (G) is warned about.
    - Sheet 2: no header; A the standard, C the class, read down to the sheet's
      last row: a blank separator row is skipped, never taken as the end, which
      would send every class below it to `Any others`. B is recomputed from the
      chosen sheet-1 column, because it is a VLOOKUP into a copy of the plasma
      column. A class normalize never assigns, a class named twice (normalize
      would use the first), `Any others` missing, repeated or in another case,
      and a standard sheet 1 does not prepare are blockers.
    - Sheet 3: classes must be LbmClass names (the export's Ontology spelling).
      A selected class MS-DIAL has no class definition for (LipidA, OxPA) is
      flagged as falling to `Any others`. An adduct must read and match the ion
      mode. A problem in an unselected row is only a warning. Classes selected
      with more than one adduct are listed.
  - The internal-standard text libraries, by header in any order and case,
    ASCII or UTF-8 with or without a BOM. Name, MZ, RT, Adduct and Ontology are
    required; the adduct's charge must match the polarity; a name may appear once
    per polarity, so one file can hold both; an MZ more than 5 mDa from formula +
    adduct is warned about. A canonical writer gives the Console's
    positional layout and never writes an empty RT or CCS field, either of which
    makes the Console drop the row.
  - Adducts and masses: `[M+CH3COO]-` and `[M+Hac-H]-` are one ion, as are
    `[M+HCOO]-` and `[M+FA-H]-`.
  - The salt: every spelling of the three anions (Japanese included) maps to the
    Console's enum string, and an answer naming two is not guessed. Each salt's
    negative-mode LBM filter is checked against sheet 3 (a dropped selected row
    is a blocker) and the standards (a dropped standard is a warning, because
    its text-library row still finds it).
  - The expected resolution, before anything runs: per polarity, the standards
    absent from its library, the selected classes that can only come out NOT
    QUANTIFIED, and those quantified by `Any others`.
  - The internal-standard resolver. Every candidate sits at the library adduct's
    m/z (MS-DIAL's Text-DB window: 0.01 Da, or 20 ppm above m/z 500), so a spot
    of another adduct never competes. Tier A: the LBM library annotated it
    (reference matched, name-equivalent), within the annotation RT tolerance, or
    the IS RT tolerance where that is wider (an annotation tolerance below the IS
    tolerance used to pass an LBM-confirmed standard over for a minor spot only
    its text row named). A
    sum-level name is equivalent to any chain-level name of its sum; two
    chain-level names must agree chain for chain, so an isobaric endogenous lipid
    (TG 16:0_17:0_18:0 beside TG 17:0_17:0_17:0) is not the standard. B: its text
    row annotated it as a reference match, within the IS RT tolerance (default
    0.5 min). C: its text row matched but another annotation won, or matched only
    below the score cutoff (MS-DIAL names the annotator in the Comment for a
    suggestion too). D: any spot in that window. The first tier with a member
    decides; in A a spot inside the IS RT tolerance comes before a farther one,
    whatever its height; then the most intense spot over the sample and QC files.
    C and D need the person's confirmation, as does an A spot outside the IS RT
    tolerance chosen while another spot lies inside it. A chosen spot with no
    height in a sample or QC file is invalid; a Blank or Standard file without it
    is allowed and recorded. One spot chosen for two standards is refused for
    both. Every candidate is listed with the files it has no height in (a
    non-whole Height cell is MS-DIAL's replacement for a zero), and the valid
    ones besides the chosen spot as `valid_alternatives`. Other adducts, isotopes
    and label disagreements are recorded, and the result is written to
    `is_resolution_{pos,neg}.tsv` and bound by a digest.
  - The standards table normalize reads: `StandardName`, `TargetClass`,
    `Concentration`, `PeakID` and `AssignName`. A standard without a usable spot
    gets PeakID -1, which `--peak-ids-only` never replaces by a name lookup. A
    standard awaiting acceptance gets its ID only once accepted, and an accepted
    tier C or D standard gets AssignName, so MS-DIAL's export names it. A spot
    the person takes instead (any candidate the resolver listed for that
    standard, with a height in every sample and QC) is used whatever the
    resolver chose, with AssignName when it is a tier C or D spot.
    The standards normalize will report as mismatched are predicted from the
    candidate table with the Console's own alias rule.
  - normalize's report is read (the current and the earlier format) and compared
    with the table. Its numbers are recomputed from the Height matrix the same
    call wrote: within the interval the export's rounding of heights allows,
    with dilution factors multiplying, and with each spot's class decided as
    `GetAnnotatedLipidClass` decides it (a spot not reference matched goes to
    `Any others` whatever its Ontology). A zero the template's "Replace true zero
    values" replaced is checked against 0.1 x the smallest true value of its row
    in that matrix, as MS-DIAL writes it. A class whose standard did not resolve
    must be emptied with a NOT QUANTIFIED comment, a file whose standard has no
    height must be empty for it, and no row may be raw Intensity.
  - Sample tables with MS-DIAL's GUI headers (`Acquisiotion type` included),
    from .xlsx, .tsv or .csv, joined to the proposed files by stem. A dilution
    factor other than 1 is accepted, and said to multiply. Both polarities pair
    into one table.
  - The positive and negative raw folders must be two folders, neither inside
    the other. An analysis root in a code checkout or a temporary folder is
    refused, and so is one inside a raw folder or whose `pos`, `neg`, `inputs` or
    `quant` folder would be, hold or lie inside a raw folder (a root named as
    the parent of raw folders called pos and neg made each run's output folder
    the raw folder itself); the system drive and the user profile are said.
  - `lab_method_answers`: what every laboratory run states rather than asks,
    written out in full: 12 anchors, the 1.5 min local window, blank
    interpolation from the confirmed sample table, Materials and Methods, and
    the rest, so that a plan from the contract leaves no question open.
- `resources/lipid_class_vocabulary.tsv`, the LbmClass members and the class
  definition MS-DIAL gives each, generated from MsdialWorkbench b01effaaa by
  `scripts/generate-lipid-class-vocabulary.py`, which reads only live code. 28
  classes are spelled differently (`Cer_NS` -> `Cer-NS`), and 24 members have no
  definition.
- `resources/lipid_category_table.tsv`, the lipid category metaboservice filed
  each Ontology's dotplots under
  (`LipidomicsConverter.ConvertMsdialClassDefinitionToSuperClassVS2`: 228
  classes, `Unassigned lipid` for any other), generated by
  `scripts/generate-lipid-category-table.py` from the converter metaboservice
  was built with: the CommonStandard copy `metaboservice.csproj` references
  (`metaboservice_csharp_source/CommonStandard/Lipidomics/LipidomicsConverter.cs`,
  June 2023, identified by its sha256 since it is not under version control;
  the 2024 binary's `Common.dll` agrees with it). It is not current
  MsdialWorkbench's switch (248 classes): 46 classes newer than that copy are
  `Unassigned lipid` here, as metaboservice filed them, where MS-DIAL now names
  a category (WE and the N-acyl amino acids NATau, NAPhe, NASer, NAAla, NAGln,
  NALeu, NAVal, NAAnt, NAGABA, NA5HT and NATryA as FattyAcyls; DGTA, LDGTA and
  BisMeLPA; ASHexCer, PT and Cer_AH/NH/ABP; DMEDFA, DMEDFAHFA and DMEDOxFA; the
  deuterated standards' classes), and 26 classes only that copy names
  (GlcCer_*, GalCer_*, Gal2Cer, GalGlcCer, BA_Conjugated, BA_Unconjugated) are
  filed as it files them. No class both name is filed differently. Sheet 3 of
  the hands-on workbook selects three of the 46 (ASHexCer, NATau, WE). The
  merge report lists the kept ontologies filed under `Unassigned lipid`
  (`outputs.category_unassigned_ontologies`).
- `tests/vectors/metaboservice_2024_data2.v1.json`: the sha256 of the 361 files
  the 2024 `metaboservice.exe` wrote from the hands-on data2 exports, run in a
  32-bit process, for the opt-in golden test. The opt-in tests that read the
  hands-on `quant_utility_table.xlsx` name it by sha256 and fail with "the demo
  workbook changed" once it is edited, since their counts are that sheet 3's;
  one of them is the laboratory's configuration (sheet 3, the reconciliation on:
  seven negative EtherPE rows renamed, each beside its positive PE P- among the
  possible double counts while sheet 3 still selects EtherPE `[M+H]+`).
- Five MCP tools take a laboratory lipidomics batch from its two raw folders to
  `lipid_metabolome.csv` (`lab_lipidomics_agent`; endpoints
  `/api/lipidomics/check`, `/write-contract`, `/resolve`, `/normalize` and
  `/merge`). Each writing step previews by default and acts only on a literal
  `confirmed=true`, in the MCP tool and again in the backend. The tools'
  confirmations and switches are declared as strict booleans (and the merge's
  tolerance as a strict number), so the MCP SDK refuses `"yes"`, `"true"`,
  `"false"`, `0` and `1` (and `"0.2"`) as the endpoints do, instead of
  converting them before the tool sees them.
  - `msdial_lipidomics_check_inputs`, read-only. With the folders alone it lists
    the raw files of each polarity, MS-DIAL intermediates already beside them,
    and the text libraries, workbooks and LBM2 files beside the data (listed, an
    LBM2 never proposed), and Excel lock files, and asks every open question: the
    analysis purpose, the LBM2, the salt, the annotation RT tolerance, the
    internal-standard libraries (two files, or one split by charge), the workbook
    and its amount column, the analysis root, `raw_location` (the raw folders
    are the data as acquired, `in_place`, or a copy, `copy`; asked with the
    sentence that MS-DIAL writes its intermediates beside them) and the Console.
    With the answers it checks the two folders are distinct and each holds its
    polarity's files (a `.wiff` needs its `.wiff.scan`); the LBM2 is an `.lbm2`
    with content, identified by sha256, its sibling variants listed; the Console
    offers `normalize --peak-ids-only`, the annotation candidate table and the
    local RT test (MsdialWorkbench#826); the analysis root and its folders lie
    outside the raw folders, a code checkout and the temporary folder. It reports
    the salt beside the adducts the negative standards and sheet 3 use, the
    classes each polarity can only leave NOT QUANTIFIED, the workbook's sample
    amount and notes read back, a sample metadata table joined by stem
    (corroboration only), and one sample table proposed for both polarities.
    `check_digest` binds what was read, the raw folders by location as well as
    by file names and sizes. The internal-standard RT tolerance is 0.5 min unless
    given or carried by a workset; what a workset filled in is shown as
    `filled_from_workset`.
  - `msdial_lipidomics_write_contract` writes `<root>/inputs/`:
    `lab-lipidomics-contract.json`
    (`msdial-interactive.lab-lipidomics-contract.v1`), the internal-standard
    libraries in the Console's column order (`is_pos.txt`, `is_neg.txt`),
    `standards_template.tsv`, `quant_rules.tsv` and
    `lab-lipidomics-record.jsonl`. It is refused while a question or a blocker
    stands, when an input differs from what `check_digest` bound, and until the
    person has confirmed the sample table (`sample_table_confirmed`; corrections
    by stem in `sample_table`, every sample listed; `included: false` leaves a
    sample out of both polarities). A check filled from a workset is written with
    the same `workset_id`. The preview names the raw folders and the
    `raw_location` answer, and the contract and its record keep both. A contract
    already at the root refuses even the preview until `replace_existing=true`,
    and shows `existing_contract`: when it was written, the diagnostics, runs,
    normalizes and merges its record holds, and that replacing it leaves them
    unusable; it is then kept under a dated name. The contract records
    Interactive's laboratory parameter template and LBM query list by path and
    sha256, and the Console's build (assembly and folder inventory sha256,
    version). Every later step hashes the inputs and the derived files again and
    refuses on any difference.
  - `msdial_lipidomics_resolve_standards`, read-only: each polarity's standards,
    decided from its run's own exports (a job's files; with
    `read_output_folders`, the output folder when it holds one run). The run must
    be the contract's (its output folder, its `guided-answers.json` naming this
    contract and its sha256, so a run planned from a contract since replaced at
    the same path is refused, the contract's salt in `method.txt`), with the
    candidate table exported, the
    library copy beside its project, and LBM annotations at all, since the Console
    skips a missing LBM2 without a word. A run whose automatic RT correction
    failed is reported, and running it again uncorrected is left to the person.
    A digest binds the resolution and the contract.
  - `msdial_lipidomics_normalize`: the resolution recomputed and matched to the
    digest shown, and every searched standard and every standard not found
    decided by the person (`accept_searched`, `accept_unresolved`, or
    `accept_spot`, which takes another listed candidate for any standard, an
    invalid or contested one included; one decision per standard, and no spot
    for two standards). A Console that is not the build the contract recorded is
    warned about in the preview, and both builds are recorded. The command
    is `MSDIALCUI normalize -i <project> -s <quant>/standards_{pos,neg}.tsv -o
    <quant>/{pos,neg} -u <unit> --peak-ids-only`, with
    `--allow-unresolved-standards` when a standard stays unresolved,
    `--allow-mismatched-peak-ids` when a mismatch is predicted, and `-d` when a
    sample's dilution factor differs from 1 (MS-DIAL multiplies by it); the
    preview says why each is there. Confirmed, it runs as a cancellable
    `normalize` job that compares normalize's report with the table and its
    mismatches with the prediction, recomputes every quantified cell, copies the
    matrices to `<quant>/NormalizedHeight_{Pos,Neg}.txt` and
    `Height_{Pos,Neg}.txt`, and records it all in
    `normalize_{pos,neg}.local.json`. A job whose cells were not all verified
    fails, and the merge sets those rows aside.
  - `msdial_lipidomics_merge` previews the counts and, confirmed, writes into
    `<root>/quant/lipidome` (the folder `metabolomics_service.R` is run in: it
    reads every subfolder of its folder as a dotplot folder, and `quant` holds
    normalize's `pos` and `neg`) `lipid_metabolome.csv`, the three statistics
    tables and the dotplot folders (see `lipidome_merge` under Changed),
    `lipid_metabolome_not_quantified.csv` and `merge-report.json`, with sheet 3
    as the rules, the resolved standards and the candidate table's reference
    matches as filters, and the contract's unit. The report holds the
    verification, the unit, the sample amount, the build of each polarity's
    normalize Console, and the positive/negative reconciliation with the
    standard each renamed row was normalized against (a PG renamed BMP was
    normalized as PG; a difference from the standard sheet 2 gives BMP, its own
    row or `Any others` when it names none, is warned about and says which).
    `reconcile_polarities` and `reconciliation_rt_tolerance` follow the
    contract unless given; another value is applied, warned about and recorded
    as a deviation. `reconcile_polarities` is true or false; anything else (the
    text `"false"`) is refused, by the endpoint and by the MCP tool, where the
    endpoint read it as on. The record's
    `merged` event keeps the four tables' sha256, the dotplot count, the sample
    file types and the reconciliation's parameters and counts. The tool returns the report without the per-file
    dotplot manifest, which stays in `merge-report.json`. It refuses a polarity
    not normalized under this contract, not completed as planned, or whose
    matrix or candidate table changed since, and an `output_name` that would
    write over its own side file or report, one of the three statistics
    tables, or a file normalize wrote. A lipidome an earlier merge wrote there
    under another name, which stays, is warned about.
  - The contract states the merge method (`merge`: Sample and QC columns, the
    reconciliation on with 0.1 min), and `msdial_lipidomics_check_inputs` warns
    (`merge_selection_warnings`) when sheet 3 selects BMP or EtherPE in
    positive mode, or does not select BMP `[M-H]-` or EtherPE `[M-H]-`: the
    reconciliation takes both from negative `[M-H]-`, so a positive selection
    counts the class twice and a missing `[M-H]-` loses the renamed rows. The
    workbook is never changed; the person edits sheet 3. While it contradicts the
    reconciliation, the check asks `merge_selection_accepted` and is not ready
    for a contract: the contract binds the workbook, so a change after it needs
    a new contract and new runs of both polarities. The person changes sheet 3
    and the check runs again, or keeps it (`merge_selection_accepted=true`, bound
    by `check_digest`), and the contract and its record keep the warnings they
    accepted (`merge.selection_warnings_accepted`). At the merge the same
    contradictions are said as what the lipidome counts, with the sentence that
    sheet 3 is part of the contract, never as an edit to make: the merge warned
    "set the positive row(s) FALSE", which voided the contract and every run
    under it. A workset does not carry the answer.
- The guided path's laboratory lipidomics profile: a plan from a contract
  (`lab_lipidomics_contract_path`), or LC-MS lipidomics with
  `library_strategy: existing` outside a repository unit.
  - New answers: `solvent_type` (any spelling of the three anions, Japanese
    included), `sample_table`, `internal_standard_text_path`,
    `internal_standard_rt_tolerance`, `export_annotation_candidates`,
    `lab_lipidomics_contract_path` and `analysis_purpose`.
  - Required on the profile: `analysis_purpose`, `solvent_type`,
    `libraries.lbm_path`, `use_retention_time_for_annotation` and then
    `retention_time_tolerance`, and `output_root`. The LBM2 must be an `.lbm2`
    with content.
  - From a contract, the plan takes the polarity from `ion_mode` and must be given
    the contract's raw folder. What the contract fixes (salt, LBM2, tolerances,
    internal-standard library, sample table, output and export folder, Console,
    purpose, parameter template and LBM query list) cannot be answered otherwise,
    by the caller, a workset or `workflow_overrides`; the shared settings'
    template and query list are not read. A method default changed, any other
    answer (an RT-correction tuning value, say) or another override is applied
    and reported as a deviation. A template or query list changed since the
    contract was written stops the plan. A Console without
    `normalize --peak-ids-only` is refused before the run. The one-file
    diagnostic is refused while the plan has any blocker but the missing
    threshold (`diagnostic_blockers`): it starts the Console on `input_path` and
    writes beside its raw files. The run's `guided-answers.json` keeps the plan's
    `lab_lipidomics` block with the contract's sha256.
  - The internal standards are searched in the same run as the Text-DB annotator
    `internal_standards_{pos,neg}`: retention time used for scoring and
    filtering within the internal-standard tolerance (0.5 min by default, apart
    from the 1.5 min annotation tolerance), priority 1, score cutoff 0.8. Only a
    library in the Console's column order is used; the contract writes one. QA's
    internal standards default to the same library.
  - `Export annotation candidates: True` is written when asked, and refused with
    a Console that does not write the table; a diagnostic never writes it.
  - A diagnostic's estimate and each production start of a contract run are
    recorded in the contract's record.
- Console capabilities `normalize_peak_ids_only` (`normalize --help` names
  `--peak-ids-only`, asked only of a Console that started) and
  `annotation_candidates_export` (both of its assembly markers).
- Worksets of kind `lab-lipidomics` (`msdial_save_workset` with
  `lab_lipidomics_contract_path`): the LBM2, the salt, both tolerances, the
  internal-standard libraries and the workbook with their sha256, the amount
  column, the Console and the method, for the next batch's
  `msdial_lipidomics_check_inputs(workset_id=...)`, which warns when a carried
  file has changed. The sample table, the purpose, the workbook's sample amount,
  dilution factors, paths, `raw_location` and the accepted threshold are not
  carried. A guided plan refuses one.

### Changed
- `lipidome_merge` writes what the laboratory's own program (metaboservice,
  `MetabolomicsServiceUtility.IntegratePosNegResults`) wrote, every file of it.
  On the 2024 hands-on exports and its `targetadduct.txt`, with the
  reconciliation off and the 2024 sample rule, `lipid_metabolome.csv`,
  `lipid_metabolome4stat.csv` and `lipid_metabolome4classsummary.csv` (372
  rows) and 356 of the 357 dotplot files are the 2024 program's byte for byte;
  `lipid_metabolome4summaryplot.csv` and `Summary_Dotplots/Glycerophospholipids.csv`
  differ in one digit of one value, `-0.0479667909938783` where the 32-bit .NET
  Framework printed `...784` (its unpublished x87 formatting rounds about 0.4 %
  of values lying just below a 15-digit midpoint up; no rounding rule
  reproduces it). With sheet 3 of `quant_utility_table.xlsx` it keeps 394 rows
  (the same plus 22 positive EtherPE).
  - The three tables `metabolomics_service.R` reads, always under
    metaboservice's names (`lipid_metabolome4stat.csv`,
    `lipid_metabolome4classsummary.csv`, `lipid_metabolome4summaryplot.csv`)
    whatever the lipidome is called, since the R script reads those names; a
    lipidome may not take one of them. And the `Summary_Dotplots`,
    `category_<category>` and `subclass_<ontology>`
    folders it reads too, with metaboservice's quirks kept: metabolite keys are
    the last `|` part of the name and the alignment ID, a lipid's dotplot file
    only within the top 41 of its class, invalid file-name characters as `_`.
  - The statistics are over the Sample and QC columns of the positive header,
    never a Blank (metaboservice's 2025 rule, the laboratory's decision; the
    2024 program took Sample only, kept as `METABOSERVICE_2024_SAMPLE_FILE_TYPES`
    for reproducing it). Sample names are Class_Injection order. The sample
    columns end only at a Class of exactly `NA`, as metaboservice's
    `set_metadata` ended them: a sample whose class is empty (or `Na`) is
    taken, and an empty class is warned about. They ended at the first empty
    class, so every later sample was dropped from the tables without a word.
  - Rows are chosen as metaboservice's `isKeepLipid` chose them, on the cells
    as written: untrimmed, case-sensitive and ordinal. A Comment starting with
    a lower-case `x` sets its row aside, and one starting `X` or ` x` does not;
    the name filters and the Ontology/Adduct match compare the cells untrimmed.
    A text rule table selects a row only when its flag is exactly `TRUE`, with
    Class and Adduct as written (`readReferenceFile`); `true`, `1` or `yes` no
    longer select, and are warned about. The reconciliation compares names and
    ontologies untrimmed too. They were trimmed, `x` ignored case, and `true`,
    `1` and `yes` selected.
  - ASCII, CRLF after every line, no byte-order mark, comma separated and never
    quoted, as the program wrote them (a cell holding a comma is warned about:
    its row has an extra column); UTF-8 with a warning where a cell is not ASCII,
    where the program wrote one `?` per byte (a recorded decision: two class
    labels of one length, such as `対照` and `処理`, would have become one class).
    Rows keep the order those `?` gave.
  - Rows ordered by ontology and then name as .NET's default comparison ordered
    them on Windows, stably: the NLS "word sort" key, checked byte for byte
    against `LCMapStringEx` (`BileAcid` before `BRSE`, `TG 56:1|` before
    `TG 56:10|`, `-` and `'` only a last tie-break), without touching the
    process locale. The earlier approximation ignored those last rules.
  - Numbers as .NET Framework's `Double.ToString()`: 15 significant digits,
    rounded half-up, `E+XX` outside 1e-5..1e15, `-0` as `0`, -infinity as `-?`
    (an all-zero lipid in the summary plot) and `NaN`. Values parse as
    `double.Parse` does.
  - The positive/negative reconciliation (the laboratory's rules, 2026-10-14),
    on by default, between the row filters and sheet 3's selection. BMP is
    annotatable only in positive mode and is annotated PG in negative mode: a
    kept negative PG row whose name equals a kept positive BMP row's with BMP ->
    PG in every `|` part, within 0.1 min (`Average Rt(min)`, compared exactly as
    decimals), becomes BMP, name and Ontology. Negative mode annotates PE P- and
    PE O- both as PE O-: a kept negative EtherPE row whose name equals a kept
    positive PE P- row's PE O- spelling (`P-x:y` -> `O-x:(y+1)` in every part:
    `PE P-34:1` is `PE O-34:2`) takes the PE P- name. Exact names only, no
    sum-composition fallback. Several candidates: the smallest |delta RT|, then
    the higher mean over the sample and QC columns; one negative row claimed
    twice: the closer positive row wins (then the more intense), and the other
    takes its next candidate no closer positive row holds, or is left unmatched
    when none is left (`lost_claims`, with every candidate it lost and to whom).
    The claims are settled together (deferred acceptance), so the pairs do not
    depend on the rows' order: two positive BMPs of one name 0.04 min apart and
    two negative PGs of that name now both become BMP, where the loser of the
    first claim was left unmatched with its other candidate left as PG, and its
    `ambiguous` entry named the row it lost. A renamed negative row whose own
    name positive mode also annotates within the tolerance (a positive PE O- or
    PG of that very name) is renamed as the rule says and reported as
    `ambiguous`, kind `conflicting positive evidence`. The renamed row's Comment
    says what it was, from which positive row and at what delta RT. It keeps and drops nothing: sheet 3 decides (BMP and EtherPE
    positive adducts FALSE, BMP `[M-H]-` and EtherPE `[M-H]-` TRUE), and a
    renamed row sheet 3 does not select, or a positive selection of either
    class, is warned about. The report lists every matched pair, every
    unmatched positive row with the same-name negative rows outside the window
    and the other names inside it, every tie and every lost claim, and
    (`not_converted`) every positive row of either class whose name names no
    negative row: a PE O- positive mode names itself, with the negative rows of
    that very name within the tolerance and whether a PE P- rename took one
    (`names_negative`; it said "names no negative PE O- row" whatever the
    negative rows were), or a BMP with no BMP token, which was listed as
    unmatched without being counted. Counts per class: `positive_rows` =
    `matched` + `unmatched` + `lost_claims`, with `rematched` (within matched),
    `ambiguous`, `conflicting_positive_evidence` and
    `positive_rows_not_converted`. On the 2024
    hands-on data 7 of 14 positive PE P- rows name a negative row; 3 of the 7
    others have a negative row of the same sum and other chains.
  - A dotplot file an earlier merge recorded writing (its manifest in
    `merge-report.json`) and this one does not write is removed, with a folder it
    leaves empty, so R does not plot it with the new ones; any other file in the
    dotplot folders refuses the write. Another folder beside the tables, which R
    would also read, is warned about, and so are repeated sample names, constant
    lipids (`prcomp(scale=TRUE)` fails on them), lipids without a finite log10
    mean and repeated lipid keys.
  - A selected row with a sample or QC cell that holds no number goes to the
    not-quantified file (metaboservice stopped on it). The two exports must also
    agree on each sample's file type, since the positive header decides which
    columns are samples.
  - The rule table may be the workbook itself; its third sheet is read.
  - New filters, the first that applies being the reason: an internal standard
    by the resolver's alignment IDs (beside the `(d` rule), and a row whose
    representative annotation is not reference matched, from the candidate
    table. The `low score:` prefix filter is kept for old exports; current
    exports no longer write the prefix.
  - A selected row with no valid concentration (NOT QUANTIFIED, raw Intensity,
    another unit, or failed verification) goes to its own file
    (`lipid_metabolome_not_quantified.csv`) instead of the lipidome.
  - The report adds drops by polarity, selected pairs without rows, ontologies
    from both polarities, same-formula rows within an ontology (possible double
    counts, such as EtherPE in both polarities), and the inputs' sha256. It can
    be written as `merge-report.json`. With `write=False` it writes nothing and
    reports what it would write, with the output's sha256.
- Text annotator rows from `libraries.text_paths` follow
  `use_retention_time_for_annotation` and `retention_time_tolerance`. They were
  written with retention time off and 0.5 min whatever was answered, because each
  row's own value wins in `text_annotator_settings.tsv`.
- The parameter template's LBM score cutoffs reach the method file. Only the
  template's workflow section reached the state, so its values were replaced by
  the writer's defaults.
- A guided plan for LC-MS lipidomics with the laboratory's own libraries
  (`library_strategy: existing`, no repository unit) is the laboratory
  lipidomics profile and is not ready until the salt, the analysis purpose, the
  output folder and the annotation RT decision are answered and the LBM2 is an
  `.lbm2` with content. A workset carries the salt once it has been answered;
  the purpose and the output folder belong to each dataset and are asked again.
- `class_assignment_confirmed: false` no longer answers the Class question: the
  plan went ahead with the classes it had just been told were wrong. A
  `sample_table` with the assignment to use answers it.
- `Solvent type` must be one of CH3COONH4, HCOONH4 and NH4HCO3, or the plan is
  refused. The Console reports any other value as applied, ignores it, and
  searches the CH3COONH4 records.
- The laboratory lipidomics contract (`lab-lipidomics-contract.json`, and the
  dated copy a replacement keeps) and its record (`lab-lipidomics-record.jsonl`)
  are local only: they name the LBM2 and the raw data where they are kept.

### Not changed
- MsdialWorkbench is not changed here. `normalize --peak-ids-only`, the
  `AssignName` column, the class-spelling redaction and the empty cells for a
  standard without height are the Console's (MsdialWorkbench, normalize track);
  a Console without `--peak-ids-only` is refused by every laboratory lipidomics
  step, so no run of this version normalizes by name.
- The running campaign is unaffected: it runs a frozen Interactive with its own
  preflight. Its Console path comes from the campaign's own answers, as the lab
  agent's comes from its contract; neither is read from Interactive's settings.

## [0.5.37] - Unreleased

### Changed
- A campaign unit's `<project>_Loaded.msp2.dbs`, MS-DIAL's copy of every library
  the run loaded, goes into a library-copy store instead of being deleted. This
  is the user's decision of 2026-10-11: keep one copy per library set and
  annotation settings in a local-only store, keep the containers in
  `output/msdial-intermediates` as before, and reopen a unit through a restore
  step. The store is `<workspace_root>\_library_copies` (`library_copy_store`).
  - A copy is identified in one pass over the file. Its content key is the
    sha256 of its sorted zip entries (name with `/` for `\`, CRC-32, size)
    under a version prefix, and the object id is the key's first 32
    characters. Its normalised sha256 is the file's sha256 with every DOS time
    and date field zeroed, in the local headers and the central directory. Its
    sha256 is the file as written, as recorded before. A copy is recognised
    when it holds exactly one `Storage` entry and otherwise only
    MetabolomicsDB, ProteomicsDB or EadLipidomicsDB databases. A file that is
    not is keyed by its sha256 and is only ever found equal to the same bytes.
  - A new content is moved in (copied and compared on another volume, or when
    other names share the file), checked with `zipfile.testzip`, and
    installed with `entry.json` and a claim. Its intent is written into
    `incoming\<uuid>\` before the move, so a crash is completed by
    `status --repair`, not set aside.
  - The install is one step with its `entry.json`. If the record cannot be
    written, the copy goes back to the output and is held there as any copy
    the store could not take. If it cannot be taken back either, it is in the
    store and not the output, so no hold there would keep it: the unit records
    it as `pending_repair`, and `status --repair` completes it and turns that
    item into the object it equals. A claim that cannot be written leaves the
    copy stored and recorded (`claim_error`). A file with no record that an
    install moves out of an object's place is named in the object's
    `repairs`, and the unit whose copy it was is found equal to the object.
  - Repair completes a staged copy only when it is the copy its intent
    records. One cut short by a stopped process is quarantined, and no unit
    records it. It removes an ingest's intent only once the unit manifest
    names the outcome. A manifest it cannot write is an error in the repair's
    result, and the intent stays, with `repaired.json` beside it, for the next
    repair to record (action `record_unit`).
  - A copy the store already holds is removed only after the object's own
    bytes, re-read under its lock, prove equal (`deduplicated`), or after
    every entry decompresses to the object's (`deduplicated_by_content`). One
    with an object's key and other content goes to `quarantine\` with a
    warning. A damaged object is replaced by the copy and kept beside it
    (`damaged-<utc>.dbs`).
  - A copy is removed or moved only as the file whose identity was read. It
    is renamed into the store, or to a private name beside it, and compared
    there by file id, size and time. MS-DIAL saves the `.dbs` by deleting it
    and moving a new file into place, and a file a save put at the path in
    between is left there, the put refused. A copy taken back after an install
    stopped never overwrites a file that has taken its place.
  - An object is called damaged only when it is verified to differ from its
    record: its size, its entry table or its bytes. An object that cannot be
    read now refuses the put, and the copy stays held. MS-DIAL loading a
    restored project through its link opens the object sharing nothing. Such
    an object used to be replaced as damaged.
  - An entry whose bytes differ is read to its end on both sides before it is
    called different, so zipfile's CRC check names a damaged side. Real
    entries are far larger than one read, and a damaged object used to send an
    equal copy to quarantine. The object is also re-read whole before any copy
    is quarantined.
  - A damaged deflate stream (`zlib.error`, which zipfile does not catch) is
    damage like any other. The copy is held under the `loaded_library_copy`
    hold its retry may lift, not the `finalisation` hold a person must clear.
  - A copy that cannot be stored stays in the output under the existing
    `loaded_library_copy` sharing hold, now with `store_errors`, and the
    hold's retry stores it. This covers no store located, a lock not taken
    and any I/O error. A lock timeout no longer reaches the generic
    `finalisation` hold that a person must clear.
  - A held copy that is gone when its hold is retried is looked up in the
    store. A retry that stored it and then could not write the manifest leaves
    exactly that. Its claim, the intent of an unfinished install or a
    quarantine's reason says where it went, and the hold is lifted with the
    copy recorded. A copy the store does not name keeps the hold, with an
    error, for a person. A store whose directories cannot be listed keeps the
    hold too, and any error a retry raises is written as its type, its code
    and its files relative to the workspace root, never as Python's text.
  - The unit records `console_run_finalisation.loaded_library_copy` (schema
    `msdial-loaded-library-copy.v1`): the store relative to the workspace
    root, each copy's identity, outcome and object id, and the errors.
    Readers still accept `loaded_library_copies_deleted` from earlier runs,
    together with the copies a 0.5.36 retry deleted and recorded only in its
    hold's resolution (`finalisation_hold_resolutions`, `deleted`). A
    copy stored by the retry of a run that is no longer the unit's current one
    is named in the top-level `library_copy_records` (schema
    `msdial-library-copy-record.v1`). So is every copy that is not the run's
    own: one a Console left in its temporary directory (action
    `console_temp`) and one MS-DIAL's GUI wrote during a restore (action
    `undo_gui_copy`). Neither is ever the copy the run is reopened from.
  - `entry.json` records the libraries by name and sha256 from
    `console_run_finalisation.libraries`, never a path, and the run's
    annotation settings: the method file's annotation lines and the annotator
    tables, with every path reduced to its file name.
  - No record, log line, job record or error names the store by an absolute
    path. An OSError is written as its type, its Windows or POSIX code, its
    text and the file relative to the workspace root. An object whose file or
    record cannot be read is reported as such, never raised.
- Every Console Interactive starts for a repository unit gets TMP, TEMP and
  TMPDIR pointed at `<workspace_root>\_tmp\<job id>`. TMPDIR too, because .NET
  reads it outside Windows and Git Bash exports one that every child inherits.
  This covers production runs, peak-count diagnostics and the retention-time
  preview, which now carries the unit's manifest in its preparation. MS-DIAL
  writes every stream it saves, the whole library copy among them, to a
  temporary file before moving it into place, so every campaign run used to
  write the private copy through the user profile's TEMP on C:.
  - After the Console ends, what is left there is listed by name and size in
    the job's `console_outcome.console_temp` and in the run attempt.
  - A leftover laid out as a library copy goes to the store, or is held from
    sharing; the rest is removed. Every copy the store took is named in the
    unit's `library_copy_records` (action `console_temp`), in the same
    manifest write as the hold on any it could not take. A symbolic link or
    junction is listed and never followed, and only the link itself is
    removed.
  - A unit whose workspace root cannot be located runs as before and says so
    in its log.
  - What is left is settled once, as the Console call returns. A backend that
    stops while its Console runs, or a Console that outlives its stop and
    writes on, leaves `_tmp\<job id>` unsettled, and nothing settles it later.
  - Laboratory runs are unchanged: their Console keeps the TEMP its user has.
- `_library_copies`, `_tmp` and `_viewing` are reserved workspace names, like
  `_dl` and `_campaigns`: never a unit, an accession or a repository.
- `refresh_retained_artifacts` leaves a unit restored for viewing alone
  (`viewing_restore_active`).

### Added
- MCP tools `msdial_restore_project_for_viewing` and
  `msdial_undo_project_restore` (`project_restore`), each a preview unless
  `confirmed=true`.
  - A restore copies each retained container from `output/msdial-intermediates`
    back to the path under the raw directory its `relative_path` names. That
    is where the `.mddata` looks. It creates the directories it needs, with
    the raw files present or released. Containers are copied, never moved or
    linked.
  - It links the run's store object into the output under the run's own
    `.dbs` name: a hard link on the same volume, else a verified copy.
  - It backs up the `.mdproject`, `.mddata` and `_Loaded.msp2` into
    `<workspace_root>\_viewing\r-<restore id>\`, whose `restore.local.json` lists
    every path it made, journalled before each step.
  - It records `viewing_restores[]` and a hold `{step: viewing_restore, blocks:
    [sharing, raw_deletion]}` that no retry lifts. Publication, raw cleanup,
    discard and split-parent release refuse while it stands.
  - It holds the raw-deletion locks of the unit, and of the unit whose raw
    tree a split part reads, from before its hold is written until its last
    copy is made. A cleanup, discard or split-parent release under way
    refuses it, and none starts meanwhile. It reads the deletion state, a
    download under way, the finalised run and the unit's live jobs again under
    the manifest's writer lock as it records the hold. A deletion reads the
    holds again in the write of its intent, so one judged before the restore
    stood deletes nothing.
  - It refuses, writing nothing, a unit with no finalisation, no campaign
    approval or no store object for its run. It also refuses a restore already
    standing, any other finalisation hold, a run, retention-time preview or
    download under way, a raw deletion begun and not finished, an output that
    already holds a library copy, any target that exists, and any path it
    would write outside the workspace root its record names paths against.
  - It restores the containers the run's finalisation moved and those a retry
    of its held move moved later (`finalisation_hold_resolutions`). It refuses
    a run whose pending containers no retry moved.
  - While it stands, no Console starts on the unit (409, code
    `viewing_restore_active`; MCP reason `viewing_restore_active`), no new
    production run is prepared (reason `viewing_restore_active`), and a
    finalisation leaves its link alone. A Console job still being prepared
    refuses the restore. A job prepared while a restore was recorded stops
    before its Console starts, and is not recorded as a failed run. The hold
    is read again in the write that opens the run attempt
    (`record_run_start(refuse_viewing_restore=True)`), so a restore made in
    another backend between the two is found there.
  - No new download lease is taken into the unit, and it is not split, while
    a restore stands: either would replace the record the undo reads, and the
    restore could never be taken back. The lease is refused under the
    manifest's writer lock, before it makes any directory in the unit.
  - The preview reports counts, bytes, whether the raw files are present, and
    every path relative to the workspace root.
  - Its record names the output directory it restored into, and the undo reads
    that one. A copy of the object made on another volume is journalled by its
    file id before its first byte (`copy_pending`): the undo removes it as the
    restore's own however far it got, and one that does not match the object
    is removed by the restore itself. `copy_pending` is written only once the
    object is open, and taken back if the copy's file cannot be made, so a
    restore that could not start its copy records none, and its undo sends
    whatever is at the link's place through the store.
  - The undo removes exactly what the record lists and puts back a project file
    whose sha256 changed, keeping the changed one under
    `gui-saved\<utc>\` (one directory per call, so a later call never writes
    over it). It reports, never deletes, any other file that appeared in the
    output or the raw-side directories. A library copy MS-DIAL's GUI wrote,
    over the link on a save or beside it on a "Save as", goes through the
    store (`library_copy_records`, action `undo_gui_copy`). That record is
    kept whole in `restore.local.json` as it is stored, and the first call
    that writes the manifest names it, the call that only lifts the hold
    included. The link is removed only as the file the undo read: renamed
    aside and compared first, so a save MS-DIAL makes on close in between is
    stored as the GUI's instead. A file still held open, a GUI copy the store
    could not take, a library copy saved while the undo ran, a directory the
    restore made that holds files it did not make, or a path its record could
    not name leaves the restore `partial` and held.
  - Two split parts restored into one released raw tree share its
    directories. The other part's copies are never reported as new files, and
    a directory this restore made that holds only them keeps the restore held
    (`shared_directories`), with a message to undo the other restore first
    rather than to remove its files.
  - The undo records `undone` only once the manifest no longer holds the
    unit. A manifest write that fails leaves the record `lifting_hold` and the
    hold standing, and the next call only lifts the hold. If MS-DIAL wrote a
    library copy into the output, or changed a project file, since then, that
    call takes the restore back as a full one does before it lifts the hold.
  - A confirmed undo is refused while MS-DIAL's GUI is running (`MSDIAL.exe`,
    from `tasklist`). MS-DIAL holds no file open between saves, so an open
    project makes nothing busy, and a save on close after the undo would put a
    whole library copy into the output with no hold. The preview names the
    running GUI (`msdial_gui`). Where the process list cannot be read, the undo
    goes ahead and says so in `warnings`.
  - A confirmed undo is also refused while the unit's raw-deletion locks are
    held, by a restore still making its copies or by a deletion. An undo run
    during a restore used to lift the hold, and the restore then finished and
    left its link and copies with no hold.
  - Nothing launches MS-DIAL.
- `python -m msdial_app.library_copy_store` for migration, not run on real data
  by this version. Each step is a preview unless `--confirmed` is given.
  - `seed <dbs> --workspace-root <root>` copies a verified copy into the store,
    never moving or linking it. It records the libraries and settings of the
    run directory that holds it.
  - `attach <manifest> --object <id>` appends a `library_copy_records` item
    (action `attach`, verified by `library_and_settings_identity`) to a unit
    finalised before 0.5.37, including one whose copy a 0.5.36 retry deleted.
    It refuses unless the unit's libraries (name and sha256) and annotation
    settings equal the object's.
  - `status --workspace-root <root> [--verify] [--repair]` reports objects,
    claims, unfinished ingests and quarantined copies.

## [0.5.36] - Unreleased

### Changed
- Multi-energy AIF whose inputs record different MS2 collision-energy sets runs
  as AIF with a Console that has MsdialWorkbench#825, instead of being held.
  This is the user's decision of 2026-10-08 ("run as is"), and it replaces the
  hold `aif_collision_energies_differ_between_inputs` of 0.5.34.
  - It covers one energy per input but not the same one, different sets, and a
    multi-energy input beside a single-energy one. The unit is decided `run` as
    AIF under the same rule, `multi_ce_aif_with_console_825`, with each per-file
    record's basis `aif_multi_ce_console_825`.
  - #825 chooses a representative energy among one file's energies only. Each
    file is therefore processed with its own per-file representative collision
    energy, and representative energies can differ between files.
  - It is on record:
    - the disposition warning `aif_energy_sets_differ_between_inputs`;
    - `aif_multi_ce_run.energy_sets_differ` true and
      `aif_multi_ce_run.collision_energy_sets` (each distinct set and its file
      count), carried into the analysis-row record and the prepare preview;
    - each input's own set in `aif_collision_energies_by_input`, keyed by the
      input's path relative to the unit's raw data root (below);
    - a project-evidence line, `aif_collision_energy_sets` in the MCP preflight
      reply, and one Materials and Methods sentence. Table S1 adds the sets and
      the per-file scope.
  - The execution gate checks each such input's energies against its own
    recorded set, not the unit's union.
  - A unit held under `aif_collision_energies_differ_between_inputs` by 0.5.34
    or 0.5.35 is released by the operator's recheck with the #825 Console
    configured (preflight or `classify_preflight`), which decides it `run`.
  - The Agent API lists `campaign_multi_ce_aif_differing_energy_sets`.
- `aif_collision_energies_by_input` is keyed by each input's path relative to
  the unit's raw data root, never by its basename (the user's decision of
  2026-10-08, second round, answer 2). Two inputs of one name in two folders
  (`POS/QC_01.mzML` and `NEG/QC_01.mzML`) each keep their own set; keyed by
  basename, the second overwrote the first.
  - The key is the input's path relative to the manifest's `input_directory`
    (its parent's for a split part), '/'-separated, as
    `unattributed_members.paths` names a member. An mzML the convert stage
    wrote beside the data root is `../converted/<its path>.mzML`. A manifest
    that records no `input_directory` keys the path as the per-file record
    gives it. Keys are compared without case.
  - The disposition records the keying as
    `aif_collision_energies_by_input_key` `path_relative_to_input_directory`,
    and each key's basename in `aif_collision_energies_input_names`, for
    display only.
  - The execution gate looks each per-file record up by the same key
    (`raw_metadata_preflight.aif_input_key`), and names a mismatched input by it.
- ONE ENCODING PER SAMPLE: the user's one rule of 2026-10-09 ("A: この一つの
  ルールで統一"). It supersedes every earlier case-by-case answer about a
  sample whose data arrive more than once, and every implementation of them in
  this version (below). When one sample's data arrive in several encodings
  (`S1.raw`, `S1.mzML`, `S1.mzXML`; copies in other folders included):
  1. Of the readable ones exactly one is used: the highest in the order vendor
     format (a folder or a container) -> mzML -> mzXML. An mzXML is converted
     to mzML in a campaign; outside a campaign it is no input.
  2. A tie (the same rank: one format in two folders, or two vendor formats)
     goes to the first by path name in lexicographic order: the path relative
     to the data root, '/'-separated, compared without case.
  3. Where the chosen one cannot be read or decoded, or its conversion fails,
     the next in order is taken.
  4. The file used is that sample's own input, paired to its sample row and in
     its Class, whatever encoding the sample row names.
  5. Every file not used is recorded with its reason, naming the file used.

  How it is implemented:
  - One function decides it: `msdial_app.encoding_rule.choose_encoding`, given
    one sample's candidates and what can be read of each, returns the file
    used and every other with its reason. The lease groups each sample's
    candidates (`_encoding_groups`) and asks it once per sample
    (`_choose_encodings`). Inputs, the conversions, the inferred pairings, the
    unattributed members, the lineage and the split parts are all read from
    what it returns.
  - "The same sample": the files a sample row pairs with (exactly, by its
    extensionless stem, a prefixed name or a leading identifier) and every
    other candidate of the unit of the same file stem (its name less its
    container suffix), or, for candidates no row pairs with, those of one stem,
    whatever folders they lie in. Two polarities that paths of one stem state
    by tokens of their own keep files apart (`POS/S1.raw` and `NEG/S1.raw` are
    two acquisitions, not two encodings of one; a unit of both polarities is
    split by them later), and so do two sample rows (rows naming `S1.raw` and
    `S1.mzML` each keep their own file; an unattributed member of that stem is
    left out, on record, as `stem_of_several_sample_rows`). Neither case is in
    the rule's words; both keep the existing behaviour. A path that states no
    polarity is one sample's with a path that states the unit's own
    (`POS/S1.raw` and `S1.raw` in a positive unit), or, in a unit that gives
    none, the only one its stem's paths state; the pairing rules read one
    sample the same way. One row's files are one sample's whatever their
    stems: a file of the stem of the name the row gives, in any encoding, and
    one a prefix or a leading identifier pairs with the row
    (`021518_387057_CSHp_S7.mzML` for a row naming `S7.mzML`, beside `S7.raw`),
    their polarities parting them as they part one stem's paths.
  - The candidates: what the unit admits by itself, the members of its own
    archive no row pairs with, and every other file found of an admitted
    file's stem and sample polarity (`_stem_mates`), or, for a file paired by
    a prefix or a leading identifier, of the stem of the name its row gives. The analysis CSV pairs
    such a file with the row of that stem whatever encoding the row names, so
    a shared archive's `S1.raw` beside the `S1.mzML` a row names is that
    sample's encoding, not one of the archive's unpaired members, and the rule
    reaches it "across folders and archives". A shared archive's members of no
    admitted file's stem are still none of the unit's candidates (excluded on
    record, as before: 2026-10-08, second round, answer 1).
  - A unit whose Catalog declared its inputs is matched by path, and every
    member no declaration names is left out (second round, answer 3). The rule
    chooses among the declared inputs of one sample row and one stem
    (`_declared_candidates`): the row of the input's `sample_id` whose
    `raw_file` names its path, else its file name, else the sample's only row.
    A declaration that lists one sample's file in two folders (the Catalog's
    encoding preference leaves equally preferred copies both declared) runs the
    first by path, and the analysis CSV accounts for the other declared input
    by the record instead of calling it missing. Declared inputs of different
    rows (MTBKS64's `raw/batch1/QC.RAW` and `raw/batch2/QC.RAW`, two rows of
    S01) stay two samples.
  - A re-encoding MS-DIAL opens that no instrument writes (netCDF/AIA `.cdf`,
    Reifycs `.abf` and `.ibf`) is in none of the rule's ranks. It ranks after
    every vendor format and before mzML: the place it held before, where the
    Catalog's preference counted it among the vendor files and a real vendor
    file still beat it. So it never wins a tie against a `.raw` or `.d`. This
    is outside the rule's words; the existing behaviour is kept.
  - What the lease can read: an mzML is scanned for arrays RawDataHandler can
    decode, and an mzXML is converted (in a campaign). A vendor file or folder
    is unreadable where it is a listed folder that did not arrive whole
    (`incomplete_container`), or where the configured raw-metadata extractor
    cannot read its header (`raw_header_unreadable`, or
    `raw_header_unsupported_format` where it has no reader for it: the reasons
    the preflight excludes an input for). The lease reads a vendor header only
    where the rule reaches a vendor file of a sample that has another
    candidate. A campaign's lease reads only with a verified, pinned extractor,
    the one its preflight will run. Without an extractor, a vendor file is
    taken as readable, as before. A sample none of whose files can be used,
    one of which was refused only for its header, still uses that vendor file:
    the header read at the lease is no exclusion, and the preflight decides the
    file, as it decides a sample's only file. Its other files keep the reasons
    they could not be used for (`undecodable`, `conversion_failed`,
    `requires_conversion`, a header of their own), never an order reason, so
    the preflight never reads one as a fallback not taken. Readability is asked in the
    rule's order and only until one file is readable, so an mzXML is converted
    only where nothing before it can be read, and where its conversion fails
    the next mzXML of the sample is converted. The convert stage starts for
    an mzXML that is only another encoding of an admitted sample only where
    the rule reaches it.
  - The pairing rules (prefixed name, leading identifier) take one sample's
    files as one candidate and pair each of them with the row; which runs is
    the rule's. A file used for a sample whose row paired with another of its
    files is that row's input, but no rule paired it: the pairing stays the
    member's it was made for (see the record below). A name a member carries
    exactly keeps a prefix from pairing another member with it only where that
    member is of the same encoding rank (a prefixed `.raw` beside `S7.raw` is a
    different file, as before); one of another rank is the row's sample's other
    encoding and is paired (a prefixed `.raw` beside the `S7.mzML` an
    extensionless row `S7` names).
  - A leading identifier is never paired beside a member that carries the
    declared name exactly, in any encoding, and is paired only where it matches
    uniquely on both sides (2026-10-06), the row's own files counting. The one
    encoding rule does not say whether a file that shares only a row's leading
    identifier is that row's sample, so the token rule keeps its words. Beside
    a row naming `VV_13_HEpG2_C1_pos.raw` that is there,
    `VV_13_HEpG2_C1_rep2_pos.mzML` (or `.raw`) is not paired: it runs as an
    unattributed input of a unit-scoped archive, or is left out on record from
    a shared one, and the row's `.raw` runs. A row naming the mzML that is
    there runs that mzML beside `VV_13_HEpG2_C1_exp344_pos.raw`. Where the row's
    name is not there, a member of its own stem in another encoding
    (`VV_13_HEpG2_C1_pos.mzML`) still counts for uniqueness: beside it
    `VV_13_HEpG2_C1_exp344_pos.raw` is refused (`leading_identifier_not_unique`),
    and the own-stem file, which is the row's by clause 4, is paired by the key
    as it is when it is alone, and runs.

  The record (the schema gate #37 is to read):
  - `manifest.encoding_choices`: every sample the rule chose for among more
    than one candidate, `{rule: "one_encoding_per_sample_2026_10_09", used,
    unused: [{path, reason}]}`, paths relative to the data root, '/'-separated,
    in their own case, `unused` in the rule's order. `used` is null where no
    candidate could be read; those candidates keep the exclusions the lease
    always recorded (`unsupported_mzml_encoding`, `conversion_failed`,
    `polarity_contradicts_declaration` in `excluded_input_candidates` and
    `input_lineage.excluded`).
  - Reasons: `lower_in_encoding_order`, `tie_lexicographic`, `undecodable`,
    `conversion_failed` (or the convert stage's
    `polarity_contradicts_declaration`), `requires_conversion` (an mzXML outside
    a campaign), `incomplete_container`, `raw_header_unreadable` and
    `raw_header_unsupported_format`.
  - Each input used for such a sample carries the same choice on its lineage
    row as `encoding_choice`, and `encoding_choice.stands_for` (the absolute
    path of the file the row pairs with) where the row names another file of
    the sample. The row then takes that file's sample, declared names and
    pairing, and `lineage_stands_for` reads `stands_for` before a conversion's
    source, so the analysis-CSV builder and a split find the same row. An mzXML
    used runs as the mzML the convert stage wrote, whose row carries both its
    conversion and the choice.
  - Where a rule (a prefix, a leading identifier) paired the row with the file
    `stands_for` names, the input's `name_pairing` is that pairing as it was
    made: its `member_name` is the member the rule paired, not the input, which
    no rule paired. `input_name_pairings.paired` (and the attribute stage's
    `inferred_name_pairings`) lists it once, by that member, with
    `encoding_used`, the path of the file the rule used for its sample, as the
    sample's choice names it; a split part lists it where it holds that file.
    The analysis-CSV record's `inferred_name_pairings` (`analysis_csv`) lists
    the input that runs with the same `member_name` and `encoding_used`; every
    entry there now names its `member_name`.
    A row naming `S7.mzML` beside `021518_387057_CSHp_S7.mzML` and `S7.raw`
    runs `S7.raw` as the row's sample, and the record says the prefix paired
    `021518_387057_CSHp_S7.mzML` (`encoding_used` `S7.raw`). An earlier state of
    this version recorded `S7.raw` as paired by a prefix, and where the file
    used was an mzXML converted, left its lineage row with no sample.
  - A file left unused is no input and no excluded candidate: it is on record
    in its sample's choice only (a listed folder left unused is not held to the
    completeness check either). The attribute stage counts `encoding_choices`
    and `unused_encodings`, and, where the lease read vendor headers,
    `vendor_headers_read` and `vendor_headers_unreadable`; the convert stage's
    `not_converted_readable_encoding` counts the mzXML a readable encoding was
    used before. A split part carries its own samples' choices as its
    `encoding_choices`.
  - A preflight that cannot read the header of a vendor file the rule used,
    where the lease read none and the sample has an encoding ranked below it,
    warns `encoding_fallback_not_taken` and names the next encoding in its
    detail. Leasing the unit again with the extractor configured takes it.
  - The Agent API lists `repository_one_encoding_rule` (and
    `repository_unattributed_mzxml` for the conversion of an unpaired mzXML,
    below).

  What it changes against 0.5.35, by the rule's words:
  - A row naming `S1.mzML` beside a `.raw` of that sample runs on the `.raw`,
    in the row's Class (clause 1 and 4), whether the mzML can be decoded or
    not. A row naming an mzXML beside a decodable mzML of its sample runs on
    the mzML, in a campaign or not.
  - Copies of one file in two folders run once, the first by path (`A/S1.raw`
    before `b/S1.raw`, `RAW/S1.raw` before `S1.raw`), not the one nearest the
    data root; two vendor encodings of one name run once (`S1.d` before
    `S1.raw`, `S1.wiff` before `S1.wiff2`). Before, a name a row admitted in
    two folders gave the row two inputs and the analysis CSV refused the unit
    (`sample_row_with_two_inputs`), and so did a row naming `S1` beside
    `S1.raw` and `S1.mzML`; two unpaired copies of one name in two folders both
    ran as unattributed inputs. The same holds for a copy in the unit's own
    polarity folder beside an untokened one, and for two declared copies of
    one row's file.
  - A vendor file whose header the lease's extractor cannot read, or a listed
    folder that did not arrive whole, gives way to its sample's next encoding.
    Before, the first was excluded at the preflight with the sample's mzML
    unused, and the second failed the lease.
  - A row's file paired by a prefix and a file of the row's own stem run once,
    the higher in the order: a row naming `S7.mzML` beside
    `021518_387057_CSHp_S7.mzML` and `S7.raw` runs `S7.raw` as the row's
    sample, unit-scoped archive or shared; a row naming `S7.raw` or `S7` beside
    the prefixed `.raw` and `S7.mzML` runs the prefixed `.raw`. Before, both ran
    (the second as an unattributed sample), or the shared archive's vendor file
    was left out. A file that shares only the row's leading identifier is not
    one of them (above): an earlier state of this version paired it there,
    and ran it in place of the row's own file.
  - Folders no longer decide whether two encodings are one sample's: the
    folder words that named an encoding (`_sample_locus`) are gone, and one
    stem with no stated polarity is one sample's in any folder.
  - A shared archive's encoding of a sample a row names is that sample's
    candidate. ST003038's shape (an mzML archive beside an mzXML archive, rows
    naming the mzXML, a download not the unit's own alone) runs the mzML for
    each row and converts no mzXML, as it did at 0.5.35, now on record in each
    sample's choice; a row naming an undecodable mzML beside a shared
    archive's `.raw` runs the `.raw`. An earlier state of this version
    converted the mzXML and listed the mzML as left out, against clause 1.

  Superseded and removed, before any release: the readable-twin answer of
  2026-10-08 and the record defects reviewed at `06d2891`, `3a4463b` and
  `418766b` as this version had implemented them. `unattributed_members`
  carries none of `chosen_other_encoding`, `chosen`, `chosen_by`,
  `two_encodings_of_one_name`, `copy_of_the_chosen_member`,
  `analysed_for_an_admitted_sample`, `stands_for`, `stands_for_reason`,
  `undecodable_mzml`, `admitted_mzxml_not_converted`, `twin_of`,
  `twin_of_reason` or `replaced_undecodable` any more; lineage rows carry no
  `replaces_undecodable`, exclusions no `replaced_by`, the attribute stage no
  `undecodable_mzml_replaced`, and `input_conversions` no `encoding_choices`
  list. The Agent API no longer lists
  `repository_unattributed_mzxml_and_encoding_order`,
  `repository_undecodable_mzml_readable_twin`,
  `repository_one_sample_encodings_pair_as_one` or
  `repository_encoding_copies_are_one_candidate`.
- Archive members a unit-scoped archive's undeclared unit leaves unpaired (the
  user's decision of 2026-10-08, second round, answer 3; all on record in
  `unattributed_members`):
  - An unpaired mzXML is converted to mzML in a campaign's lease like any other
    mzXML input, under the rule of 2026-09-30 that converts mzXML-only data,
    where the encoding rule uses it. The mzML written from it is the
    unattributed input. Its lineage row is `kind` `converted`, with the
    conversion as its source and the mzXML's basename as
    `name_pairing.member_name`, so `members` and `paths` name the mzXML, and
    `unattributed_members.converted` lists the members converted. Outside a
    campaign nothing converts, and it stays left out as `requires_conversion`.
  - Opposite-polarity names stay left out
    (`polarity_token_contradicts_ion_mode`), a shared archive still takes none,
    and a unit whose Catalog declared its inputs still takes only those.
  - `unattributed_members.count`, `members` and `paths` list every lineage
    row, in `rows` and in `excluded`, that says `unattributed_member`, once per
    path, as the gate's PAIR-1 counts them. A converted member is named by its
    mzXML. A member the lease excluded after admitting it (an mzXML whose
    conversion failed or whose scans contradict the declared polarity, an mzML
    it cannot decode) used to be missing from `count` and `members` while its
    excluded lineage row still said `unattributed_member`, so PAIR-1 FAILed
    (record only). It is now counted, and listed in
    `unattributed_members.excluded` with the lease's reason. The warning is
    recorded wherever the lineage holds such a row.
  - A split part names a converted unattributed member by its mzXML, as the
    parent and its lineage row do, and lists it in `converted`. It used to
    name it by the converted mzML's lower-cased name, so PAIR-1 FAILed (record
    only).
- A unit whose Catalog declared its inputs now records the archive members no
  declaration names (2026-10-08, second round, answer 3: left out "on record").
  `unattributed_members` says `applied` false, `reason`
  `catalog_declared_inputs` and `count` 0, and lists each such member in
  `left_out` with `reason` `not_named_by_the_catalog_declaration`, by its
  basename and its path under the data root. Only analysable members are
  listed, and only those no declaration names that reached nothing of the
  lease. Such a member is not run, even where it is a readable encoding of a
  declared mzML that cannot be decoded: the declaration names the sample's
  file. Before, the unit carried no `unattributed_members`, and the archive's
  member listing was their only trace. The Agent API lists
  `repository_members_no_declaration_names`.

### Not changed
- All inputs sharing exactly one energy run as SWATH.
- An input with no recorded MS2 collision energy holds the unit
  (`aif_collision_energy_unrecorded`, raw data kept), with #825 or without.
- Without #825, more than one energy over the unit is held as
  `aif_multi_ce_awaiting_console`.
- The Catalog's encoding preference (`encoding_preference`, its shared test
  vectors) still decides what the Catalog lists; the one encoding rule decides
  among the candidates a lease finds.

### Upgrade order
- The before-production gate of the reanalysis repository, as of its gate PR
  #34, still requires every multi-energy AIF row to record the unit's union of
  energies, and FAILs ACQ-1 otherwise. ACQ-1 blocks the run, and the campaign
  runner counts that block as a failure: after the retries the unit ends as
  failed and its raw data are deleted, with no MS-DIAL run.
- Gate PR #37 accepts per-file energy sets that differ. It has to read
  `aif_collision_energies_by_input` by the relative-path keys above, not by
  basename. For an input directly in the data root the two are the same, but a
  gate that looks up `POS/QC_01.mzML` by its basename `QC_01.mzML` finds no set,
  and FAILs ACQ-1 the same way.
- Gate PR #37 also reads this version's encoding records. As of its head
  `e7b2901` it reads the superseded ones (`chosen_other_encoding` and
  `chosen_by` in `unattributed_members.left_out`, `replaces_undecodable`,
  `replaced_undecodable`, `encoding_choice.stands_for` only through an mzXML).
  It has to read `manifest.encoding_choices` and the lineage's
  `encoding_choice` (`used`, `unused`, `stands_for`) instead: an input used for
  a sample whose row names another file stands for that file, whatever its
  encoding, and an unused file is accounted for by its sample's choice, not by
  an exclusion. Its PAIR-1 reads `name_pairing` from the lineage rows as
  before; for such an input, `name_pairing.member_name` is the member the rule
  paired (the file `stands_for` names), not the input's own name.
- So gate #37, updated to read the relative-path keys and the encoding records,
  has to merge with or before the 0.5.36 pin. The campaign must not run 0.5.36
  before then: neither a recheck nor a fresh preflight with 0.5.36 is safe,
  because either decides such a unit `run`.

## [0.5.35] - Unreleased

### Changed
- The current pinned raw-metadata extractor is msrawdataworkbench `5f604462d`
  (master: #43 and #42) with MsdialWorkbench `f0583493a`, built as
  `RawMetadataExtractor-5f60446-f0583493a` (inventory `a224c0fee2385b70`,
  provenance verified). `plan_extractor_build` and `extractor_build_root`
  default to this pair and name that folder. Compared with the `a12293c61` build, which stays a built
  pin that a campaign accepts:
  - #43: the Console's Waters spectrum reader skips the LockSpray function the
    SDK names or marks as a reference scan, and reads the lower-energy MSe
    function as MS1 whatever the function count.
  - #42: an mzML product-ion scan's collision energy comes from the spectrum when
    it parses, else from the last precursor's activation, the rule the
    production Console's RawDataHandler 1.3.9776.346 uses.
  - On 21 real files (Waters MSe, Thermo AIF mzML, Agilent multi-energy AIF,
    Waters, mzML and SCIEX DDA) every verdict is the `a12293c61` build's.
- A campaign's `extractor_not_pinned` refusal lists the three built pins,
  current first.
- A `PINNED_BUILDS` entry can name the folder its build is in (`build_folder`)
  when that is not the folder the commits would name. The `5f604462d` pin does,
  because its folder carries a seven-character commit. `extractor_build_root`
  uses it for that pair, in full or abbreviated, so the default extractor
  location beside the Interactive checkout is the existing build, and a plan
  without commits reports that the build already exists rather than planning a
  second build of the same pin into an empty folder.

## [0.5.34] - Unreleased

### Added
- Multi-energy AIF runs with a Console that has MsdialWorkbench#825. The user
  held multi-energy AIF on 2026-10-07 "until the patched Console"; #825 is that
  Console. It deconvolutes an AIF file separately at each collision energy and
  represents each peak, in the per-file export and in alignment, by the energy
  of its MS/MS reference-spectrum match, or else by the energy whose
  deconvoluted spectrum has the most product ions (the lowest on a tie). It
  reads the energies from the raw data, so the analysis CSV says `AIF` and
  nothing more.
  - A Console capability, `multi_energy_aif_representative_collision_energy`,
    found by two messages of #825's multi-energy reader in the Console
    assembly (present in the Console of MS-DIAL master bb90e0e51, absent from
    that of f0583493a). Both are required: an earlier local AIF patch build has
    only the one that came with the per-energy files, and still takes an
    unannotated peak's spectrum from the first energy (probe
    `marker_incomplete`, no capability). `console_capabilities` reports it, and
    `multi_energy_aif_console` reads it from the assembly without starting the
    Console.
  - The raw-header preflight (`run_raw_metadata_preflight`, the MCP tool's new
    `console_path`) and `classify_preflight` decide for the configured Console:
    `console_path`, else the saved `console_path` setting, else
    `MSDIAL_CONSOLE_PATH`. Every AIF unit's disposition records the probe as
    `multi_energy_aif_console`. With #825, a unit every AIF input of which
    records the same MS2 collision energies, more than one, runs as AIF,
    recorded as `aif_multi_ce_run` (energies, rule
    `multi_ce_aif_with_console_825`) and on each per-file record as basis
    `aif_multi_ce_console_825`.
  - #825 chooses a representative energy among the energies of one file, never
    across files: a file with one energy keeps its single deconvolution result.
    So with #825, AIF inputs whose energies differ from one another (one energy
    each but not the same one, or different sets) are held as
    `aif_collision_energies_differ_between_inputs`, raw data kept, with each
    input's energies in `aif_collision_energies_by_input`; no Console releases
    that hold. The MCP preflight reply gives the distinct sets and their file
    counts.
  - A unit held earlier as `aif_multi_ce_awaiting_console` is released by the
    operator's recheck: preflighted again, or classified again, with the #825
    Console configured, it is decided `run`.
  - The execution gate refuses an `aif_multi_ce_run` unit when the workflow's
    Console lacks #825, a per-file record that claims the basis without the
    record, and a per-file record whose own energies are not the recorded ones.
  - The Materials and Methods text and Table S1 state the energies and the
    representative-energy rule of a multi-energy AIF run.

### Not changed
- Single-energy AIF still runs as SWATH (`single_ce_aif_as_swath_2026_10_07`),
  whatever the Console. AIF whose collision energy is unrecorded is still held:
  the #825 Console stops on an AIF file whose MS2 scans carry no energy.
- Without #825 in the configured Console, multi-energy AIF is held exactly as
  before. Operator skips, hold releases, split parents and durable manifests are
  unchanged.

## [0.5.33] - Unreleased

### Fixed
- Every method file Interactive writes now names one blank filtering
  comparison. The shipped LC-MS and GC-MS templates wrote
  `Blank filtering: SampleMaxOverBlankAve`, `Sample max / blank average: 5` and
  `Sample average / blank average: 5`; since MsdialWorkbench#823 the Console
  refuses, before any processing, a method file that has both ratio keys, so a
  Console built from MS-DIAL master refused every run. Both templates drop the
  `Sample average / blank average` line, which no Console before #823 read, so
  old and new Consoles read the remaining two lines alike: sample maximum over
  blank average, fold change 5.
- A parameter template whose blank filtering lines disagree by #823's rule (both
  ratio keys; a ratio key whose comparison differs from `Blank filtering`; or
  one whose value differs from `Fold change for blank filtering`) is refused,
  naming the template and the two lines, when it is loaded
  (`load_parameter_template`), when a method file is written from it, and before
  the RT correction preview copies it. Interactive does not choose between the
  lines, because which comparison was meant is the template author's decision.
  As in the Console, each key counts by its last readable line.

### Not changed
- No answer, workflow setting or report sets, maps or describes blank
  filtering: the lines come from the template alone, and a template that agrees
  with itself is copied through as before.

## [0.5.32] - Unreleased

### Added
- Support for MsdialWorkbench#826, which improves the automatic alignment RT
  correction of #810. #826 judges each anchor against the median of the offsets
  of the other compounds matched in the file within a local support window
  (status `LocalOutlier`): reference candidates whose peak tops lie within two
  MS1 scans of each other in both files (isotope peaks, adducts; only identical
  RTs where a file's scans are unknown) count as one compound, and the anchor's
  own compound is no support for it. Where fewer
  than three compounds are found it falls back to the median of the file's
  anchors (`MadOutlier`). It floors the robust scale at the MS1 cycle time
  around the anchor. The public-repository campaign is to use it, with 12
  anchors and the default window, as the user decided on 2026-10-07; a
  repository run is refused with a Console that predates #826 (see Changed).
  - A new answer key, `automatic_rt_correction_local_support_rt_window` (minutes,
    0 or more; 0 keeps the run-wide test only). It is written to the method file
    as `Automatic RT correction local support RT window: <v>` only when the
    answers, the workflow or the template set it. Unset, nothing is written: a
    Console with #826 uses its default of 1.5 min, and an older Console is never
    handed a key it does not know. A template's line for it is read into the
    state and is not copied through past it.
  - It is validated as the other automatic RT settings are: a number the Console
    reads, finite as a single-precision float, and not negative. Setting it with
    a Console that predates #826 is refused: the probe looks for #826's method
    key in the Console assembly (capability
    `automatic_alignment_rt_correction_local_support`).
  - The guided form has a field for it (blank leaves it to the Console), and the
    supplementary workbook lists it under the automatic RT correction settings
    when it is set.

### Changed
- The automatic RT correction evidence (`automatic_rt_correction_proof`), which
  the audit viewer and the publication report share, reads #826's audits as well
  as #810's. It records `outlier_test` (`local_support_with_ms1_cycle_floor`,
  `run_wide_with_ms1_cycle_floor` for a window of 0, `run_wide_mad` for a
  Console before #826, or `off` for a threshold of 0), the window and whether it
  came from the method file or the Console default, the threshold, the
  `Outlier test` counts and the `LocalOutlier` and `MadOutlier` counts. A run
  whose method file set the window and whose Console listed it as unrecognised
  is not proof (`method_key_not_recognised_by_console`).
  - #826's appended columns are read by name: in the anchor audit `Outlier
    test`, `Local support count`, `Expected offset (min)`, `Outlier scale (min)`
    and `MS1 cycle at anchor (min)`; in the summary `Estimated scan interval
    (min)`, `First used anchor RT (min)`, `Last used anchor RT (min)`, `Peaks
    before first used anchor` and `Peaks after last used anchor`. An audit
    without them, from a Console before #826, reads as before, and the required
    column sets are unchanged.
  - The audit viewer counts `LocalOutlier` as a rejection beside `MadOutlier`,
    shows the new anchor columns and each file's MS1 scan interval where the
    audit has them, and lists the outlier test's settings.
  - The Methods text says, for a #826 run, that anchors were judged against the
    other compounds matched in the file within the window, co-eluting reference
    candidates counting as one compound and the anchor's own left out, with the
    MS1-cycle floor, and how many each test rejected. It says nothing of either
    for a Console before #826 or with the threshold at 0. Table S1 and the
    workbook carry the new evidence fields.
- A public-repository reanalysis (a run with a repository run manifest) that
  turns automatic RT correction on is refused when the selected Console
  predates #826, also when the window is left to the Console's default. Were
  only a set window checked, a Console with #810 alone would run the older
  run-wide test and the run would still complete as corrected. A guided or
  laboratory analysis may still use a Console with #810 alone.

### Not changed
- Interactive's defaults for guided and laboratory analyses: automatic RT
  correction stays off and the maximum anchors stay 6. A campaign profile turns
  it on with `execute_automatic_rt_correction` and
  `automatic_rt_correction_maximum_anchors`, which the campaign runner passes to
  the production prepare and run. The zero-threshold diagnostic still turns it
  off, so its method file has no automatic RT correction line.

## [0.5.31] - Unreleased

### Changed
- AIF runs as SWATH where it has one collision energy, as the user decided on 2026-10-07. A unit that would run as
  AIF counts the distinct MS2 collision energies, to 0.1 eV, over every input that runs. Each per-file record now
  carries them (`ms2_collision_energies`, with `ms2_collision_energies_unresolved` and `reference_functions`), from
  the extractor's `acquisition.collisionEnergies`; a Waters LockSpray reference function contributes none, and a
  record whose reference function carries an MS level leaves the energies unresolved.
  - One energy: the unit runs as SWATH. The disposition records
    `aif_run_as_swath = {"collision_energies": [...], "rule": "single_ce_aif_as_swath_2026_10_07"}`, and each input
    runs with `console_acquisition_type` SWATH and `console_acquisition_basis` `aif_single_ce_as_swath`; its
    `header_console_acquisition_type` stays AIF. ST004304 gave identical results, and MTBKS281 matched its 30 eV
    collection.
  - More than one: the unit is held, not run (`disposition` skip, `reasons` `aif_multi_ce_awaiting_console`,
    `hold` true), until a Console that settles an all-ion spot's representative energy exists. An AIF unit whose
    inputs record no energy is held as `aif_collision_energy_unrecorded`.
  - A held unit is no failure and keeps its raw data: neither a campaign approval nor `confirmed=true` discards it,
    and a held split part has not ended for its parent's raw release (`held_by_disposition`). Only an operator's
    explicit skip lifts the hold: `release_disposition_hold` (default false) on `discard_download_lease`, the
    split-part discard, `cleanup_split_parent`, `msdial_discard_repository_raw` and the CLI's `discard
    --release-disposition-hold`. With it, under an approval covering boundary 5 (or `confirmed=true`), the discard
    proceeds and the unit or part records `disposition_hold_released_by: "operator_skip"` (with
    `disposition_hold_release`: who, when, the hold's reasons) before anything is deleted; on a split parent it lifts
    its held parts' holds the same way. A held part discarded without it (as 68f1cc0 let an approval do) still holds
    its parent's raw data (review r9-64).
  - A unit to be split is split first; each part is decided by the rule on its own.
  - The execution gate runs a header's AIF as SWATH only where the applied disposition records `aif_run_as_swath`,
    and refuses it anywhere else.
  - `classify_preflight` reads the energies of a preflight recorded before 0.5.31 from the extractor records it left.
  - The analysis-CSV record and the prepare preview carry `aif_run_as_swath`.

### Added
- Unattributed archive members, as the user decided on 2026-10-07. In a unit whose Catalog declared no inputs and
  whose download is its own alone (`download_scope.kind` `unit_files`, or every bundle URL with `shared_unit_count`
  1), each analysable archive member that no sample row pairs with (exactly, behind a prefix, or by its leading
  identifier) is an input all the same, and is preflighted like any other. ST001264 runs 31 inputs: 3 paired and
  28 `..._Youn_saN.raw` unattributed.
  - Its `input_lineage` row has `name_pairing = {"paired_by": "unattributed_member", "member_name": ...}`, its stem
    as `sample_id`, and `sample_row` null.
  - Its analysis-CSV row is a `Sample` of the abstention's Class (`All`) where the unit's Class is an abstention, and
    of `Unattributed` otherwise. The reviewed sample TSV gets a row per member with `raw_file_paired_by`
    `unattributed_member`.
  - The run manifest records `unattributed_members = {"count", "members", "paths", "rule":
    "unit_scoped_archive_2026_10_07", "applied", "scope"}`. `members` gives each member's basename, as its lineage
    row's `name_pairing.member_name` does; `paths` gives the same members' '/'-separated paths relative to the unit's
    raw data root (the parent's for a split part), and each `left_out` entry has both `member_name` and `path`
    (review r9-64: a nested archive listed paths in the record and basenames in the lineage). The warning `unattributed_members_included` goes into the manifest's warnings, the
    attribute stage's, every campaign disposition's and the CSV record's. A split part carries the record and the
    warning for its own members only.
  - Never for a shared archive (`shared_unit_count` above 1 for any bundle URL): the record then says
    `applied: false` with its `reason` (`shared_archive`, or `download_scope_not_unit_scoped`), and lists the
    members as `left_out`. Also left out, on record: an mzXML (`requires_conversion`), a member whose path names the
    other polarity (`polarity_token_contradicts_ion_mode`), and a member whose name another member carries in another
    encoding, admitted or not (`two_encodings_of_one_name`).
- Agent capabilities `campaign_single_ce_aif_as_swath`, `repository_unattributed_archive_members` and
  `discard_release_disposition_hold`.

## [0.5.30] - Unreleased

### Fixed
- `test_a_study_archive_of_per_sample_archives_expands_without_collisions` no
  longer fails intermittently. It packed a per-sample zip into the study, then
  built the same zip a second time to compute the SHA-256 and MD5 it expected.
  The test helper `_zip_bytes` wrote entries with `ZipFile.writestr(name, ...)`,
  which stamps the current time at the format's 2-second resolution, so the two
  builds differed whenever a 2-second boundary fell between them. The helper now
  writes a fixed date (1980-01-01 00:00:00), as `_tar_bytes` already fixes
  `mtime`, and is otherwise byte-for-byte what `writestr` produced. The test now
  hashes the bytes it actually packed. The same race affected
  `test_a_nested_archive_whose_expansion_exists_beside_it_is_left_packed`, which
  compared an unexpanded `S1.zip` with a second build, and the same changes fix it.

### Not changed
- Production code. The extraction recorded the digest of the archive it
  expanded each time; only the tests' expectations were rebuilt. Each assertion
  still checks the same value.

## [0.5.29] - Unreleased

### Changed
- A campaign disposition takes each file's acquisition from its raw header first,
  as the user decided on 2026-10-06. A file whose header was read, that has MS2,
  and whose header gives DDA, DIA, AIF or SWATH runs as its header says, whatever
  the unit declares and whatever confidence the extractor gave.
  `HEADER_OVERRIDE_CONFIDENCE` (0.8) is gone. The extractor's confidence is a
  constant per branch of its classifier: every DDA read without an isolation width
  is 0.75. The declaration is the Catalog's keyword match over the assay's text
  (`infer_acquisition`), not a structured repository field. Under the old rule
  MTBLS1572 ran its six DDA files, and a blank whose header is Unknown, as SWATH.
  - Each declaration a header overrode is listed in `declared_vs_header` with
    `decided` (the header's mode), `basis: header` and `declaration_source`. The
    record also carries `declared_acquisition_source`, one of
    `catalog_keyword_inference` (the Catalog handoff's
    `technical_settings.acquisition_mode`, which the Catalog writes only by keyword
    matching and with no source of its own), `split_part` or `unattributed`.
    `acquisition_header_disagrees_low_confidence` is no longer written.
  - The override is reported (the warning `acquisition_header_overrides_declaration`
    and the "run as their raw headers give" detail) only for files that reach the
    run. A file whose header contradicted the declaration and that was then
    excluded (`dia_scheme_unresolved`, `product_ion_only`, an out-of-scope method,
    polarity) is listed in `declared_vs_header` with `decided: excluded`,
    `basis: excluded` and its `excluded_reason`, and counted in a detail of its own.
  - A file whose header gives Unknown is excluded as `acquisition_unresolved` in a
    declared unit too, as it already was in an undeclared one. A header that gives
    PRM, SRM, MRM or SIM is excluded as `acquisition_out_of_scope:<method>`,
    whatever the declaration.
  - A unit nothing of which runs, whose inputs are Unknown-header files and MS1-only
    files only, is skipped as `acquisition_unresolved` instead of being excluded as
    `acquisition_out_of_scope:FullScan`: the MS1-only files are out of scope only for
    want of an MS2 file beside them, which a header that settles the others may give.
    This holds for undeclared units too, which were excluded before. A unit with an
    intrinsically out-of-scope input beside the Unknown ones (a targeted method, ion
    mobility, polarity switching, an mzXML) is still excluded.
  - In a unit declared DIA, AIF or SWATH, MS1-only files are not folded into the DDA
    run its headers make. They are excluded as `ms1_only_in_declared_dia_unit`,
    since they may be all-ion data exported as MS1 scans (MTBLS1572's bbCID files).
    Elsewhere the fold is unchanged, and `ms1_only_beside_dia` still applies beside
    SWATH or AIF files. An MS1-only file whose header gives SWATH or AIF is never
    folded into DDA (`ms1_only_header_contradicts_dda`).
  - A split part is held to its parent's declaration for that rule, since its own is
    the mode its split wrote. A split records the parent's declared mode in
    `split_from.parent_declared_acquisition_mode`; for a part split before 0.5.29
    it is read from the parent manifest. The disposition records it as
    `split_parent_declared_acquisition_mode`. A part split under the old rule could
    carry MS1-only files its DDA part folded in; decided again, they are excluded.
  - Unchanged: a unit none of whose headers could be read is taken at its
    declaration (`acquisition_declared_only`); a DIA header whose isolation settles
    neither SWATH nor AIF takes the declared one; mixed acquisitions still split;
    untargeted status is never inferred over a declared targeted acquisition.
- The execution gate refuses a row whose acquisition type contradicts the Console
  type its file's header settles (`header_console_acquisition_type`), whatever an
  applied disposition decided: DDA or AIF for a DDA or AIF header, and SWATH for a
  DIA header whose isolation targets recur at two or more m/z. A manifest decided
  under the old rule can no longer run DDA-header files as SWATH. Outside a
  campaign this is stricter than before in one case: AIF is refused for a DIA header
  whose isolation targets make it SWATH, where the header's DIA used to admit SWATH
  or AIF.
  - A DIA header with one recorded isolation target or none binds no type. The
    extractor records targets only from MS2 headers that carry a precursor m/z, so
    none recorded is no evidence of all-ion acquisition. Such a row is held, as
    before, to SWATH or AIF, and under an applied disposition to the type it
    decided; a SWATH row the disposition decided is not refused.
- The execution gate decides an applied disposition from before 0.5.29 again (one
  with no `declared_acquisition_source`), in memory and from the same records, as
  `classify_preflight` would. It refuses the run where that decision would not run
  the unit, would exclude a row's file, or would give it another type on its
  header's word, and says to prepare the unit again. This refuses MTBKS217's
  `z_014nn` (an MS1-only file folded into the DDA run of a unit declared DIA) and
  MTBLS1572's blank (an Unknown header run as SWATH by the declaration). In a
  read-only simulation over the 10 recorded runs that have an analysis CSV, every
  other run still passes.
  A legacy row's type is not refused where the new decision rests on the
  declaration or on a DIA header with no recorded isolation target.
- `msdial_prepare_repository_reanalysis` decides such a legacy disposition again,
  from the unit's recorded preflight, before it builds the rows, and the analysis
  CSV is written from the new decision. This is what clears the gate's refusal.
  - The preview decides in memory and writes nothing. The re-decision is written
    when the call writes (`confirmed=true`, or under a campaign approval), before
    the CSV and whether or not the CSV then fails. The reply reports it in
    `preview.legacy_disposition_redecision`: old and new disposition, Console
    type, reasons, excluded inputs, and the status before and after.
  - The new disposition keeps the old one, with each input's former type and
    basis, under `supersedes`, and records `redecided` (by, when).
  - A unit held for any reason is not decided again in place: split, excluded at
    its split, past its run (`mztab_validated`, `completed`,
    `cleanup_pending_confirmation`, `raw_cleaned`, `discarded`), a run attempt
    open, or a production run finalised under another status
    (`validation_failed`).
- `msdial_prepare_repository_reanalysis` never writes over a finished run's files.
  A unit past its run (the statuses above, or any unit with `finalized_at`) is
  refused with `ok: false`, `reason: run_finished`, and nothing is written. Before,
  preparing such a unit rewrote `output\analysis_files.csv`, the CSV its run read
  and a retained artifact, while the unit stayed `mztab_validated` and
  cleanup-allowed.
  - New argument `new_run=true` prepares a new production run for such a unit. The
    finished run's records are copied unchanged into a new entry of
    `superseded_runs`, and its own records (`finalized_at`, `finalized_run`,
    `mztab_validation`, `retained_artifacts`, `retained_artifact_inventory`,
    `project_archive`, `console_run_finalisation`, `analysis_csv`,
    `analytical_order` and the like) are taken off the top level.
    `output_directory` becomes `<workspace>\output-run-<n>`, where the analysis
    CSV and the new run's outputs go; the old output directory is left as it is.
    `cleanup_allowed` becomes false and the status `preflight_passed` (or
    `prepared`). A legacy disposition is decided again then, and only then. The
    preview does this in memory and reports it in `preview.new_run`.
  - Refused, and nothing written: a unit whose raw data were released
    (`raw_released`, with or without `new_run`), a new run while a run attempt
    may still be running (`run_in_progress`), one a legacy disposition decided
    again would not run (`would_not_run`), and one whose decision fails
    (`redecision_failed`).
  - A confirmed new run is all or nothing. It is decided in memory, its rows
    are built, its aliases made, and its reviewed metadata and analysis CSV
    written into a hidden staging folder beside the new one
    (`.output-run-<n>.<random>.staging`). Only then, under the manifest lock,
    the staging folder is renamed to `output-run-<n>` and the manifest is
    written once, with the superseded run, the new `output_directory`, the CSV
    record, the analytical order and any campaign crossing together.
  - If any step before that fails, the manifest and every file of the finished
    run stay byte for byte as they were, and the staging folder and the aliases
    the call made are removed. The failing steps are rows that disagree or an
    alias that cannot be made (`analysis_csv_failed`, recorded nowhere in the
    manifest, `analysis_csv.written_to_manifest: false`), files that do not
    map, or an error. A manifest written by another writer meanwhile is refused
    as `new_run_conflict`.
  - Aliases in `raw\console-aliases` have fixed paths, so two prepares of one
    unit share them: the second reuses what the first made. A call that is
    abandoned now removes an alias it made only if no analysis CSV of the
    unit's committed manifest names it (the current run's or a superseded
    run's, read from the CSV and its lineage rows, sidecars included). Before,
    a prepare refused as `new_run_conflict` removed the alias a concurrent
    prepare had reused and committed, leaving that run's CSV naming a missing
    input (review r7-62). In the other order, a commit whose CSV names an
    alias that is gone, because the prepare that made it was abandoned, is
    refused as `new_run_conflict`. Both checks run under the manifest lock.
  - Before this, a CSV that failed after the new run was written left a
    validated, cleanup-ready unit as `preflight_passed`, `cleanup_allowed:
    false`, with an empty `output-run-<n>` and no new run prepared (review
    r6-62).
  - Raw-data deletion is judged by the unit's current run. While a new run
    prepared after a validated run has not validated (prepared, running or
    failed), `msdial_discard_repository_raw` refuses: before, it saw no mzTab-M
    in the new folder and would have discarded the unit as one with no
    validated output. `msdial_cleanup_repository_raw` refuses as before, since
    the current run is not validated. Both previews name the superseded
    validated run. The raw data are kept for the new run and are deleted by
    the cleanup once it validates.
  - The same holds for a split part, approved or not (review r7-62). Under a
    campaign approval, the discard of a part with such a pending new run is
    refused with the same blocker, and nothing is recorded. Before, it marked
    the part `discarded`, and the parent's release then deleted the raw tree
    the new run reads. The parent's release (`plan_split_parent_cleanup`,
    `cleanup_split_parent`) counts such a part as not ended (`pending`),
    whatever its status and however many runs failed, a part already marked
    `discarded` that way included. Once the new run validates, the part ends as
    any validated part does.
  - The gate's legacy refusal names `new_run=true` for a unit past its run.
  - On disk, as recorded on 2026-10-07, 11 manifests carry a legacy applied
    disposition. Five are past their run (MTBKS217, MTBKS236, MTBLS1572,
    MTBLS417, ST001337; all `mztab_validated`): a prepare refuses them, and they
    are decided again only by a prepare with `new_run=true` that writes. The
    other six (MTBKS281 `run_failed`; MTBLS291 and ST004304 `preflight_passed`;
    MPST000015 twice and MTBLS548 `excluded_by_preflight`) have no `finalized_at`
    and are decided again by any prepare that writes, where nothing else holds
    them; MTBKS281 then becomes `preflight_passed` if the decision runs it, and an
    `excluded_by_preflight` unit becomes runnable if the recorded preflight no
    longer excludes it. A preview changes none of them.

### Added
- Agent capability `campaign_header_first_acquisition`.
- Agent capability `repository_prepare_new_production_run`.
## [0.5.28] - Unreleased

Version order: 0.5.25 (#58), 0.5.26 (#59), 0.5.27 (#60) and 0.5.29 (#62) are
open beside this one, and all five branch from 0.5.24.

### Changed
- The stepped Minimum peak height (`estimate_peak_height_range`, the "quantized
  height-range search" behind `msdial_estimate_peak_height` and
  `POST /api/agent/tuning/estimate` without an exact target) follows the user's
  decisions of 2026-10-06:
  - A zero-threshold count of 6,000 or fewer still keeps 0.
  - Otherwise, of the multiples of the instrument-family step (100 for
    QTOF-type data, 1,000 for Fourier-transform data), the threshold is the
    HIGHEST whose estimated count is still at least 3,000: the lower end of
    3,000-6,000, for MS/MS of higher quality, since gap filling recovers the
    peaks a threshold leaves out of a file. Before, it was the multiple nearest
    the range's midpoint (`selection_rule` is now
    `highest_threshold_keeping_at_least_minimum`).
  - Only when no multiple of the family step lands in range does it make the
    same choice in the fine step, 10 for QTOF-type and 100 for FT
    (`fine_threshold_step`).
  - The fine step is an absolute floor, never a tenth of the step a caller
    passes: a request with `threshold_step` 10, which is what a fallback's
    estimate reports as the step used, searched in steps of 1 in this PR's first
    draft and reached a threshold of 2 on the Waters MSe demo, where the median
    S/N is 2.6.
  - The family step is always the coarse step, whatever `threshold_step` is
    requested. The diagnostic's `instrument_family` decides it
    (`family_threshold_step`) and the fine step; a request below the fine step
    is raised to it, and any other request that is not the family step is
    recorded (`requested_threshold_step`, `requested_step_disposition`: null
    when none was made, `family_step`, `fine_step`, `raised_to_fine_step` or
    `recorded_only`) with a warning, and never searched in the family step's
    place. This PR's second draft let a request between the floor and the
    family step become the coarse step: MTBLS2207's DDA unit asked for steps of
    100 got 19,700 instead of 19,000, and MTBLS417 asked for steps of 10 got
    1,820 instead of 1,800, both recorded with that request as
    `coarse_threshold_step` and no fallback. Only without a family does the
    request name one: 1,000 or more is the FT family's step, anything else the
    QTOF-type family's. A `threshold_step` of 0 is no request.
  - When even the fine step misses, the threshold is the candidate nearest the
    range (the one keeping more peaks on a tie), `within_target_range` is false
    and the estimate carries a `warnings` entry, which the Tune parameters view
    shows.
- Of the fourteen diagnostics on disk (0.5.24 -> 0.5.28):
  - The Thermo Fusion pilot unit ST001337 goes from 181,000 (an estimated 4,495
    peaks) to 323,000 (3,004); MTBLS1572 from 700 (4,678) to 1,200 (3,162);
    MTBLS417 from 1,100 (4,529) to 1,800 (3,032); MTBLS2207 DDA from 9,000
    (4,397) to 19,000 (3,058); the Agilent DIA demo from 200 (4,661) to 300
    (3,643).
  - The Bruker compact DDA demo goes from 100 (1,932) to 50 (3,257) and the
    Waters Premier DDA demo from 100 (754) to 20 (3,627), both by the fine
    step. MetaboBank MTBKS281 goes from 100 (338) to 10 (2,484) and the Waters
    MSe demo from 100 (26) to 10 (522), both still out of range, with a warning.
  - MTBKS217, MTBKS236, MTBLS2207 DIA, the Waters Xevo demo and the Bruker
    SWATH demo do not change.
- The instrument family, which decides the diagnostic's threshold step, is read
  from an mzML's header: before, every mzML was QTOF, so the pilot's ST004304, a
  Thermo Q Exactive published as mzML, was diagnosed in steps of 100 (as was
  MTBLS2207, an Orbitrap ID-X published as mzML). `detect_raw_format` reads the
  cvParam names and userParam values of an mzML's referenceableParamGroupList
  and instrumentConfigurationList, from the head of the file only.
  - Orbitrap-class instruments (Q Exactive, Exactive, Exploris, Orbitrap
    Fusion/Lumos/Eclipse/Ascend, Astral, LTQ Orbitrap, Orbitrap Velos/Elite,
    ID-X, the Tribrids) are `Fourier-transform MS`.
  - FT-ICRs (solariX, apex, scimaX, LTQ FT) are `FT-ICR`.
  - A model name outranks the FT-ICR analyzer term ProteoWizard writes for a
    Thermo model it does not know. A TOF analyzer or model name makes the mzML
    QTOF on its own header.
  - Each file row now says what its family rests on: `instrument_family_source`
    is `vendor_format`, `mzml_instrument_configuration` or `format_default`,
    with `instrument_evidence` naming the text that decided it.
  - An Orbitrap or FT-ICR mzML now gets the Fourier-transform format
    suggestions (Minimum peak height 10,000, mass slice width 0.05), as a
    Thermo .raw always has.
- The diagnostic's representative file is read again when it is on disk, so a
  workflow state from before this cannot keep a Q Exactive on steps of 100.
  Only where the file's family is a format default (an mzML naming no
  instrument, a Bruker or unrecognised .d) does the repository unit's declared
  instrument (the Catalog handoff's `technical_settings.instrument`, read by
  the new `declared_instrument`) name a Fourier-transform family, recorded as
  `instrument_family_source: repository_declared_instrument`.
  - The profile records `instrument_family`, `instrument_family_source`,
    `instrument_evidence` and `declared_instrument`.
  - Every completed pilot unit keeps its family: ST001337 (Thermo .raw) FT; the
    Waters, SCIEX and Bruker units QTOF. ST004304 and MTBLS2207 become FT.
- The estimate reads a stored diagnostic's family again
  (`current_peak_tuning_profile`) instead of the one the version that started
  it stored. A diagnostic recorded before 0.5.28 and re-estimated through
  `manifest_path` kept the QTOF family and step 100 stored for every mzML:
  MTBLS2207's DDA diagnostic re-estimated to 19,700 and recorded QTOF. The
  representative file's own format and mzML header decide when it is on disk;
  otherwise the stored family does, and for a profile recorded before 0.5.28,
  which kept no `instrument_family_source`, an mzML or .d family is taken as a
  format default, so the unit's declared instrument can still name an FT
  family after the raw data are deleted. The response's `representative` and
  the `peak_height_diagnostics` record carry the re-derived family, with
  `instrument_family_rederived` and, when it changed, `stored_peak_tuning_profile`
  (the stored family, source and step). The diagnostic's own
  `diagnostic-job.json` is not rewritten.

### Added
- The estimate records `threshold_step` (the step actually used),
  `requested_threshold_step`, `requested_step_disposition`,
  `coarse_threshold_step` (the family step),
  `fine_threshold_step`, `step_fallback`, `fallback_reason`
  (`no_coarse_step_in_range` or null), `coarse_minimum_peak_height` and
  `coarse_estimated_peak_count` (what the family step alone chose),
  `instrument_family` and `selection_rule`, beside `within_target_range` and
  `warnings`.
- Each `peak_height_diagnostics` record in the unit's manifest keeps the whole
  estimate, as before, and now also names `coarse_threshold_step`,
  `fine_threshold_step`, `step_fallback`, `fallback_reason`,
  `within_target_range`, `selection_rule`, `instrument_family` and
  `instrument_family_source` beside `threshold_step`. A record of an estimate
  from before this reads as its own step with no fallback and a null
  `selection_rule`.
- `production_peak_counts` in the unit's manifest, one record per production
  run, appended by the run's finalisation (`finalise_console_run`) before the
  mzTab-M is validated: the applied `minimum_peak_height` (from the run's
  method file), every file's actual count from its .mdpeak export (`files`, with
  `peak_count_min`, `peak_count_median`, `peak_count_max` and
  `peak_count_total`), and the representative file's `representative_peak_count`
  against `estimated_peak_count`, with `representative_to_estimate_ratio` and
  `representative_within_target_range`. It names the diagnostic it is read
  against (`diagnostic_job_id`): the latest whose threshold the run applied,
  else the latest, said by `diagnostic_matches_applied_threshold`. The
  finalisation record carries the same summary without the per-file list.
- Agent capabilities `peak_height_fine_step_fallback`,
  `peak_height_lower_end_selection`, `mzml_header_instrument_family` and
  `production_peak_counts`.
- `tests/vectors/peak_height_diagnostics.v1.json`: the height histograms of the
  fourteen diagnostics, each height rounded down to a multiple of the step the
  estimate uses, which leaves every count the search reads unchanged.
## [0.5.27] - Unreleased

Version order: 0.5.25 (#58) and 0.5.26 (#59) are open beside this one, and all
three branch from 0.5.24.

### Fixed
- A repository unit's MS1 and MS2 data type are what its inputs deliver to
  MS-DIAL. These two settings tell MS-DIAL whether the spectra it receives still
  need centroiding, so they must describe what RawDataHandler hands it, not what
  the instrument stored. Every unit used to run with the template's Centroid.
  MS-DIAL 5 loads every LC-MS input with `getProfileData=false`, and the readers
  then differ:
  - Waters (MassLynx scans read unprocessed), mzML (a converted mzXML included)
    and NetCDF hand MS-DIAL the stored points. There the header decides, with
    basis `raw_header`: per MassLynx function for Waters (`continuum`), per file
    for the others.
  - SCIEX WIFF and WIFF2, Bruker BAF and TSF, and Shimadzu hand it centroids
    whatever was stored, so the level is Centroid with basis
    `delivered_centroid`.
  - Thermo: a centroid scan arrives as centroids. An FTMS profile scan arrives as
    its centroid stream, but an ITMS profile scan arrives as profile points. The
    extractor records no analyzer or scan filter, so a profile file is Centroid
    (`delivered_centroid`) only on an instrument with no ion trap (Q Exactive,
    Exploris, Exactive). On any other model it is `unresolved`.
  - Agilent hands over peak spectra where the file has them and profile spectra
    otherwise, and the extractor records neither, so it is `unresolved`. So are
    Bruker TDF and any reader not in the table.

  `raw_metadata_preflight` keeps the reader table, with file and line citations
  into RawDataHandler.
  - Each per-file preflight record now carries what the header stored:
    `spectrum_representation`, its `spectrum_representation_source`, and
    `spectrum_representation_by_level`. Only a Waters record fills the last.
    The record also carries the `reader`, `native_format` and
    `instrument_model` that say what MS-DIAL receives. The preflight summary
    gains `data_types` (`msdial-interactive.delivered-data-types.v1`, scope
    `inspected_inputs`).
  - `build_guided_plan`, which campaign and MCP repository runs both go through,
    sets `ms1_data_type` and `ms2_data_type` from the run's inputs. Inputs a
    campaign disposition excluded are left out, and each Console alias is read
    as the input it stands for. A level is decided only when every input with
    that level delivers one representation and all agree. Its basis is
    `raw_header`, `delivered_centroid`, or both. Otherwise the default stays,
    with the reason (`inputs_disagree`, `unresolved` with `unresolved_by`,
    `unrecorded`, or `no_input_at_level`), per-value and per-delivery counts,
    example file names and a warning. No input is dropped to make the rest
    agree.
  - The decision is kept as `data_type_provenance` in the workflow and in
    `workflow-settings.json`. The run's `run-manifest.json` and the preparation
    gain `data_types`: each value with its basis, plus the decision. Each unit
    `run_attempts` entry records the values, bases and warnings. A laboratory
    analysis records basis `unrecorded` and is otherwise unchanged.
  - A peak-count diagnostic runs with the unit's values, so its count stands for
    the production run. Its `data_types` describe the one input it ran (scope
    `diagnostic_input`), and keep the unit's decision in brief under
    `unit_decision`. Where that input itself delivers another representation, a
    warning says so.
  - A data type set explicitly in `workflow_overrides` stands, with basis
    `workflow_override`, or with the decided basis where it agrees. The
    execution gate refuses only a run that contradicts a decided level, from any
    path, the GUI's included. An `unresolved` or `default` level refuses
    nothing.
  - For a preflight recorded before this version, the representation, reader,
    format and model are read from the extractor records kept in
    `provenance\raw-metadata-preflight.json` (`read_from_preflight_output`).
    A preflight that kept no `summary.per_file` at all is read the same way:
    its extractor records are summarised as a preflight summary would have
    them, MS-level flags included, so an input without MS2 does not vote on
    the MS2 data type. Where that file is absent, the inputs stay
    `unrecorded`; that is so for all four summary-less preflights in the
    reanalysis workspace on 2026-10-07 (MPST000007 twice, MTBLS341, ST002419).
  - What the 2026-10-03 pilot units get, read from their records:
    - ST001337 (Orbitrap Fusion Lumos, headers Profile): Centroid, `unresolved`
      (`thermo_analyzer_unrecorded`). That is the value it ran with, and the
      gate refuses neither value.
    - MTBKS217 and MTBKS281 (Waters, every function `continuum=false`):
      Centroid, `raw_header`.
    - MTBLS1572 (converted), MTBLS291, MTBLS417, ST004304 and MTBLS2207: mzML
      Centroid, `raw_header`.
    - MTBKS236 (WIFF, header null): Centroid, `delivered_centroid`.
    - MPST000015 (Exploris 120, Profile): MS2 Centroid, `delivered_centroid`.
- Converted mzXML units take the representation the converted mzML records
  (MS:1000127/MS:1000128, from the mzXML's `centroided`). Where the mzXML
  recorded none, the level stays at the default as `unrecorded`. This replaces
  the data-type half of 0.5.21's known limitation; the threshold step is
  unchanged.
- Agent capability `repository_data_type_as_delivered`.
## [0.5.26] - Unreleased

### Changed
- The LC-MS peak-count diagnostic behind `msdial_start_peak_count_diagnostic`
  (`POST /api/agent/tuning/run`, the campaign runner's diagnose step) no longer
  annotates. Only its `.mdpeak` rows and their Height column are read.
  - The Console fixes which peaks there are, and their heights, in peak
    spotting, isotope estimation and deconvolution (`MsdialLcMsApi`
    `FileProcess.RunAsync`).
  - It writes the `.mdpeak` after annotation and the characterisation that
    follows it (`LcmsProcess.ExecuteAsync`). Neither adds or removes a peak or
    changes its height.
  - Without reference matches, the characterisation can give a peak another
    adduct, charge or isotope assignment. So a diagnostic's Adduct, Isotope and
    MS1 isotopes columns are not the production run's. Nothing reads them from
    a diagnostic.
- On a campaign unit the diagnostic searched the tiered LBM, strict MSP and
  broad MSP annotators at Minimum peak height 0, where every peak above the
  noise is a query. In the pilot, the diagnostic of one Waters MSE (AIF) file,
  MTBKS281 Lm1, took 2,650.5 s (44 min).
- Measured with the Console (`f56d4478a`) on two pilot files, each diagnostic
  run once with annotation (in the pilot) and once without it:
  - MTBKS281 Lm1 (Waters MSE, AIF, negative): 20,057 peaks in both, with the
    same sorted Height list. 2,650.5 s with annotation against 927.9 s without.
    Without annotation, deconvolution took 768.2 s of the 927.9 s.
  - ST001337 Human feces_ALA007 (Thermo, DDA, positive): 13,599 peaks in both,
    with the same sorted Height list. 267.0 s against 12.1 s.
  - In both, the same Peak IDs, the same values in every peak-spotting column,
    byte-identical deconvolution (`.dcl`) files, and the same estimated
    threshold. Adduct differed in 8,987 and 8,378 rows and MS1 isotopes in 284
    and 1,566 rows; Isotope differed in none.
- The diagnostic's method writes every library line blank (Msp, MSP annotator
  settings, Lbm, Text DB, Text annotator settings, Isotope text DB) and the
  Annotation pipeline profile line blank, also when the template carries one of
  them. It writes no annotator settings file, and its run manifest lists no
  library. Every peak-spotting and deconvolution setting is still the
  production method's; only Minimum peak height (0) and the alignment switches
  differ, as before. The production run is unchanged.
- The diagnostic records `diagnostic_annotation` (`status`
  `skipped_for_peak_count`, the reason, the production annotation profile and the
  library roles it did not load, never their paths) in its
  `workflow-settings.json` and `run-manifest.json`, as `annotation` in its
  `diagnostic-job.json`, and as `annotation` on the unit manifest's
  `peak_height_diagnostics` entry, including an estimate made from disk.
- The GUI's diagnostic panel (`POST /api/tuning/run`) still annotates, because
  its MSP sliders read the match scores of the same run; it records `status`
  `performed`. A GC-MS diagnostic is unchanged.
- Agent capability `peak_count_diagnostic_without_annotation`.

### Known limitations
- The count was measured the same with and without annotation on two files
  only, a Waters MSE AIF file and a Thermo DDA file. SCIEX, Bruker and Agilent
  raw data, mzML inputs and ion-mobility data were not measured; for them it
  rests on the Console source.
- Each timing is one Console run per arm. The two runs of a file were on
  different days, under different machine load, and run-to-run variance was not
  measured. The share of the pilot run's time spent in annotation is inferred
  from the difference, not timed by stage.
- The measured runs used a method built by hand to the same rule, not one
  written by this version's `_write_method`.
## [0.5.25] - Unreleased

### Fixed
- Replicate injections. A repository lists each injection as a sample row of its
  own, and the rows of one sample share its id: MetaboLights MTBLS291 names `Cel`
  in five rows, one mzML each, and MetaboBank MTBKS64 names `S01` in two, one .RAW
  each. Both were refused as "two inputs on one sample". The unit of mapping is
  now the sample row:
  - `msdial_download_repository_raw` and every handoff mapping: the analysis-input
    check (`_inputs_unpaired_with_rows`) pairs each declared input with the row of
    its sample whose `raw_file` is the input's path (or its archive's), else the
    one with its file name. Catalog 0.6.x already lists one input per row and
    counts them alike, so MTBKS64 is consistent and is no longer excluded as
    `analysis_input:count_mismatch`. Two inputs on one row, more inputs than a
    sample has rows, or an input none of its sample's rows names, still are.
    The analysis-CSV builder pairs a declared input with a row the same way
    (`rows_naming_input`, shared by both): before, it matched by base name only,
    so rows naming `raw/batch1/QC.RAW` and `raw/batch2/QC.RAW` passed the check
    before the download and were refused after it (`sample_without_input`).
  - `msdial_prepare_repository_reanalysis`: the analysis-CSV builder maps each
    input to the row of its sample that names it (by its own name, its lineage
    row's declared names, its declared path, or its `name_pairing`), and every
    replicate is a CSV row of its own with its sample's Class. Nothing is merged,
    averaged or dropped. `sample_with_two_inputs` is renamed
    `sample_row_with_two_inputs` and fires only for one row two inputs name; an
    input two rows name is `input_with_two_sample_rows` (it was reported as
    `input_without_sample`), and an input of a sample several rows describe that
    none of them names is `sample_row_not_identified`. All three are mapping
    failures `allow_partial_mapping` may accept. `sample_without_input` and the
    exclusions are counted by row, and a replicate is named with its raw file.
  - Each CSV row records `sample_row_index` and `sample_raw_file`; each lineage
    row the CSV was written from records `sample_row` (`index`, `sample_id`,
    `raw_file`) beside the lease's own `sample_id`; the excluded inputs carry
    `sample_raw_file`; the CSV record adds `sample_rows_without_input`.
  - A handoff that counts no inputs gives `sample_count` as its rows that name a
    sample, not its distinct sample ids.
  - A split divides a sample's replicate rows by the part their inputs go to.
    MetaboBank MTBKS220 gives its samples rows of timsOFF BAF folders and rows of
    timsON TDF folders under one sample id; `plan_acquisition_split` and the
    confirmed split picked rows and declared inputs by sample id, so each part
    held both formats' rows and inputs and its CSV was refused after the 14 GB
    download (`analysis_input_not_found`, `sample_without_input`). A row an input
    of a part is (as the analysis-CSV builder finds it) is now that part's alone,
    a row of an excluded input is no part's, and a declared input goes with the
    row it is; each part records `sample_row_indexes`. Only a row no input is
    found to be is still matched by its sample. On a synthetic unit built from
    MTBKS220's real handoff, the parts hold 24 and 29 rows and declared inputs
    (48 and 47 before).
- Declared raw file names an archive member carries behind a prefix. Metabolomics
  Workbench ST001264 declares `BioRec1.raw`, and its study archive holds
  `021518_387057_CSHp_BioRec1.raw`; the lease admitted nothing and failed at its
  attribute stage. For a unit whose Catalog declared no analysis inputs, an archive
  member (an analysable file, or the outermost .d/.raw folder) whose name - or
  stem, for a name a row records without an extension - ends in `_`, `-`, `.` or a
  space and then a declared name, compared without case, is that declared file's
  (`_prefixed_member_pairing`), where no member carries the name exactly, the
  member carries no declared name exactly, and the pairing is one to one: a member
  ending in two declared names, or a name two members end in, pairs neither, so
  `Youn_sa1.raw` never takes `..._Youn_sa11.raw`. No pairing crosses a polarity:
  where the member's path (its folders under the data root and its name) or the
  declared name carries `pos`, `neg`, `positive` or `negative` as a token of its
  own, in any case, and that is not the unit's ion mode, or the two disagree, the
  pairing is refused (`polarity_token_contradicts_ion_mode`,
  `polarity_token_contradicts_declared_name`). A polarity token beside a `control`,
  `ctrl`, `blank` or `qc` token in the file name is read as part of a sample's name
  and refuses nothing (`name_polarities`): a positive unit's
  `Neg_Ctrl_1.raw` (a negative control) pairs with `021518_Neg_Ctrl_1.raw`, as do
  `pos_ctrl`, `Positive_control` and `neg_blank`. Two cases keep it a polarity. A
  name with a polarity token of its own elsewhere states that one only
  (`Pos_Ctrl_1_neg.raw` is Negative), and a polarity folder always states its
  polarity, whatever stands beside the token: `QC_NEG/2020_QC_1.raw` is Negative, as
  `NEG/` is, so a positive unit's `QC_1.raw` is not paired with it. And where such a token is a name's only polarity token and that side of
  the pairing (the declared names, or the members' paths) names its files by
  polarity elsewhere (`names_state_polarity`), it is the file's polarity. Read-only
  over the Catalog, an unconditional exemption would have stopped 157 sample rows in
  19 LC-MS units from contradicting their unit's ion mode, and every one of those
  units names its other files by polarity: ST002251's positive unit lists
  `20200715_004_QC-neg.mzML` beside its `_pos` files, ST003858's negative unit lists
  `Blank_POS_001.mzML`, and ST002510 lists `GL_NEG_Ctrl_B3_1.raw`. With the
  condition, none does. Every stage that admits a member
  by a sample's name admits a paired one (conversion sources, the encoding choice,
  the attribute stage, the extracted files kept), and its lineage row records
  `name_pairing` (`declared_raw_file`, `member_name`, `paired_by`
  `prefixed_member_name`) and takes that file's sample; the attribute stage counts
  `prefixed_member_pairings`. The analysis-CSV builder finds the sample row by the
  same pairing. A declared name nothing carries still admits nothing: its row is
  left in `samples_without_input`, and a unit left with no input still refuses to
  fall back to accession-level inputs.

### Added
- Declared raw file names paired with archive members by their leading identifier
  (the user's decision of 2026-10-06). Metabolomics Workbench ST001359 declares
  `VV_13_HEpG2_C1_pos.raw`, and its archive holds
  `VV_13_HEpG2_C1_exp344_pos.raw`; the row for VV_14 even reads `HEepG2`. In the
  same pairing code and with the same scope as the prefixed rule (units whose
  Catalog declared no inputs), after exact and then prefixed matches, a declared
  name still unpaired is paired with the member whose `leading_identifier_key` is
  its own: the stem (less a raw or converted suffix) split on `_`, `-`, `.` and
  spaces, taken up to and including the first token that contains a digit,
  compared without case and joined by `_` (`vv_13`). A key of digits only (a run
  date such as `021518`) or a name without a digit gives none. The key must be
  unique among the declared names and among the candidate members, and no pairing
  crosses a polarity token. Applied to ST001359's real member listing, all six
  declared names pair; applied to ST001264's, the rows named `Sample1`..`Sample28`
  still pair with nothing.
- Every inferred pairing is left on record, as the user required:
  - the input's lineage row records `name_pairing` with `paired_by`
    `leading_identifier_token` and the `key` (or `prefixed_member_name`), the
    declared and the member name;
  - the attribute stage counts `prefixed_member_pairings` and
    `leading_identifier_token_pairings`, lists `inferred_name_pairings` and any
    `refused_name_pairings` (with the rule and the reason), and carries the warning
    `input_names_paired_by_inference`;
  - the run manifest carries `warnings: ["input_names_paired_by_inference"]` and
    `input_name_pairings` (`paired`, `refused`); every campaign disposition of such
    a unit lists the same warning;
  - a split part's run manifest carries the same record for itself: the pairings
    of its own inputs (or of the mzXML they stand for), the refusals for its own
    sample rows, and the warning only where it has a pairing of its own. A pairing
    of an excluded input, or a refusal for a row no part holds, stays in the
    parent's record only. An excluded ion-mobility part's disposition carries the
    warning too;
  - the analysis-CSV build, its preview and the manifest's `analysis_csv` record
    carry the warning and `inferred_name_pairings`;
  - the reviewed sample TSV (and JSON) of a repository unit gains the column
    `raw_file_paired_by`: `exact`, `prefixed_member_name` or
    `leading_identifier_token`, and empty for a row without an input.
- A unit whose sample rows the download did not all deliver says so beside its CSV:
  the build, its preview and the `analysis_csv` record carry the warning
  `sample_rows_without_input` and `sample_row_coverage` (`sample_rows`,
  `with_input`, `without_input`). ST001264 runs its 3 BioRec rows of 31; this is
  recorded and does not stop the run.
- Agent capabilities `repository_replicate_rows_as_inputs`,
  `repository_prefixed_member_names` and `repository_leading_identifier_names`.

## [0.5.24] - Unreleased

### Added
- In a campaign, the lease's `convert` stage gives an mzXML scan that records no
  polarity (absent, or `any`) the unit's declared ion mode, as the user decided on
  2026-10-02. It does so only where the unit's Catalog handoff declares exactly one
  polarity (`technical_settings.ion_mode` Positive or Negative), the field the gate's
  CONV-1 checks an imputation against. `project.ion_mode` is never read for it,
  since a raw-header preflight rewrites it, and a split part's conversions stand for
  its parent's declaration. Each imputation is recorded as a `polarity_imputation`
  inference with its count. `ConversionOptions` gains `declared_ion_mode` and
  `declared_ion_mode_field`, kept with every record's options, so a re-lease never
  reuses a conversion made under another declaration. A declaration of Both or
  Unknown, or none, imputes nothing, and CONV-1 then fails the spectra left without
  a polarity.
- An mzXML some of whose scans record the polarity opposite to the declaration
  while others record none refuses the imputation, and is excluded as
  `polarity_contradicts_declaration` (the user's default of 2026-10-03, as a failed
  conversion is excluded): it is listed in `excluded_input_candidates`,
  `input_lineage.excluded`, the campaign disposition's `excluded_inputs` and the
  analysis CSV record, and the rest of the unit runs. A file whose scans all record
  the opposite polarity converts with its recorded polarity, and the preflight's
  per-file polarity rule decides what happens to it.
- Agent capability `campaign_mzxml_polarity_from_declared_ion_mode`.

## [0.5.23] - Unreleased

### Added
- The download lease fetches through the accession download store
  (`<workspace_root>\<repository>\<accession>\_dl`, 0.5.13). A campaign's lease
  always does; any other lease does only when the new `store_mode` setting is
  `always` (`user_settings.save_download_store_mode`, or POST
  `/api/settings/store-mode`). With the default, `campaign`, every other lease is
  what it was.
  - fetch: each object is fetched once for every unit of the accession that lists
    it, under one claim per unit, with the client's idle timeouts and retries, and
    its published MD5 is compared before anything reads it.
  - extract: each archive is extracted once, in the store
    (`DownloadStore.ensure_extracted`); its member listing is copied into the
    unit's provenance.
  - materialise: the unit's `raw\data` becomes hardlinks to the store's files at
    the placements the per-unit lease used. A failed link is a copy, and so is a
    file a reader rewrites in place (a BAF `.d`'s `analysis.sqlite`).
  - prune: once the unit's inputs, members and sidecars are known, other links are
    removed. What the SCIEX reader opens beside a kept input stays
    (`x.wiff2`'s `x.wiff.scan` and `x.timeseries.data`, an Analyst `x.wiff`'s
    `x.wiff.<n>.scan`; `travels_with_sciex_file`).
  - The manifest records `download_cache`, `raw_storage` (hardlink, copy or mixed;
    bytes linked and held; what was pruned) and, on each `downloads[]` entry,
    `cache_object_path`, `sha256_origin` and `declared_checksum_verified`.
- Cleanup, discard and split-parent release release the unit's store claims once
  its raw tree is gone (`download_store_release`). Under the campaign approval that
  covered the deletion (boundary 5), the store then collects each released object
  no live claim holds, and leaves a tombstone. A batch pre-claim keeps an object for
  units that have not run, a split parent's claims stand for its parts, and an
  approval that keeps raw data deletes nothing. A person's confirmation releases
  claims and deletes no store object. A repeated release is recorded under
  `repeats` and never overwrites the release that changed something; a release that
  stopped after the deletion is finished by a repeat.
- `msdial_repository_batch_plan` returns `download_plan`: distinct objects with
  their consumers, per-unit against distinct bytes (unknown sizes counted, never
  priced at 0), objects already in the store, sharing groups, and a `run_order`
  that keeps each group together. With a campaign approval, `pre_claim=true`
  records a pending claim for each covered ready unit.
- The download preview of a store lease reports what the store already holds and
  `distinct_bytes_to_transfer`; deletion previews say which bytes the store keeps.
- New read-only MCP tool `msdial_download_store_status`.
- New live job state `waiting_for_shared_download`: a lease waiting for another
  lease's transfer or extraction of the same object. It polls, beats the lease
  heartbeat, hears a cancel, and the GUI keeps polling through it.
- `_dl` and `_campaigns` are refused as repository, accession or unit names, and
  are never listed as units.

### Known limitations
- Under `store_mode` `always` outside a campaign, a person's confirmation leaves a
  released object in `_dl` until a cleanup or discard is repeated under a campaign
  approval that covers it.

## [0.5.22] - Unreleased

### Added
- One split key. `plan_acquisition_split` keys a part on its acquisition mode, its
  ion-mobility regime (the header's `has_ion_mobility`, or an ion-mobility container
  such as a Bruker TDF folder) and its polarity (an applied disposition's, else the
  header's). The part id names every part of the key in which the parts differ:
  `<unit>-dda`/`<unit>-dia` as before, `<unit>-dda-im`, `<unit>-dda-neg`/`-pos`. The
  plan and the parent record `split_key` (`msdial-split-key.v1`).
  - An ion-mobility part is written excluded (`ion_mobility_out_of_scope`, the
    LC-IM-MS reason) and stays excluded.
  - A polarity part carries its polarity as its ion mode.
  - A part lists only its own entries of the parent's file list, its folders'
    members matched by path before name, and its samples matched by their
    `raw_file`'s path, so a polarity split of same-named folders (`pos/S1.raw`,
    `neg/S1.raw`) gives each part only its own samples.
- Split-parent release. `plan_split_parent_cleanup` and `cleanup_split_parent`
  release a split parent's raw tree once every part has ended: validated, failed
  after its retries (3 recorded run failures), skipped, excluded, or discarded. The
  release needs delete_after_validated_output, parts that partition the parent's
  inputs, no live Console, no artifact under the tree and no finalisation hold; it
  takes a lock, records `raw_release` (`msdial-split-parent-raw-release.v1`) before
  the first file goes, and resumes when stopped. The parent stays
  `split_by_acquisition`.
- `msdial_discard_repository_raw`: preview and perform a discard, with
  `campaign_authorization_path`.
- Capabilities `split_key_acquisition_ion_mobility_polarity`,
  `split_parent_raw_release` and `campaign_authorized_raw_cleanup_and_discard`.

### Changed
- `cleanup_download_lease`, `discard_download_lease` and `cleanup_split_parent`
  take `campaign_authorization_path`. It stands in for `confirmed=true` when it
  covers boundary 5 for the unit (or the unit it was split from) and the approval
  and the unit both state `delete_after_validated_output`. A refusal returns
  blockers with nothing recorded or deleted.
- Under such an approval, a failed unit whose output holds an unvalidated or
  invalid mzTab-M is discarded. The mzTab-M is kept in place, and its validation
  and the failure record go to `output/failure-artifacts/`, written through the
  unit's sharing context: no log lines, hosts or local paths. A discard asked for
  again returns its record (`already_discarded`) and writes nothing.
- Every raw deletion refuses while a retained artifact lies under its target or a
  run attempt's Console may be running, unlinks multiply-linked files without
  touching their attributes, and records `raw_deletion` (`msdial-raw-deletion.v1`).
- A run whose Console exits 0 without a validated mzTab-M is recorded as a failed
  run, so a split part that fails this way three times ends and its parent can be
  released.
- The execution gate refuses `raw_cleaned` and `discarded` units, a unit whose
  deletion has begun, and a part whose parent's raw tree was released.
- The post-run hook only records the pending plan (`raw_release_pending` on the
  parent for a split part). In a campaign the runner is the one trigger of every
  deletion.

## [0.5.21] - Unreleased

### Added
- A campaign unit whose samples exist only as mzXML is converted to mzML and run,
  instead of being excluded (the user's decision of 2026-09-30). This happens only
  where a campaign authorization covers the unit; outside a campaign an mzXML or
  mzData still excludes its unit before download, as before, and mzData excludes a
  campaign unit too.
  - Eligibility (`EligibilityPolicy.convert_mzxml`) records
    `project.conversion_plan` (`msdial-mzxml-conversion-plan.v1`) for such a unit,
    in the MCP plan, download and batch-plan tools, the backend's download route,
    the preflight, the disposition and the split.
  - The lease's `convert` stage, a placeholder since 0.5.16, runs after `extract`
    and before `discover`. It writes each of the unit's mzXML, packed or not, to
    `raw\converted\<relative path>.mzML` with `mzxml_conversion`, every inference
    flag off. Conversions are recorded as they complete in `input_conversions` and
    `provenance\input-conversions.json`, and a re-lease reuses a record whose
    mzXML, options, converter and output still hold.
  - Each converted input gets an `input_lineage` row of kind `converted` whose
    `source.conversion` holds the mzXML's path, sha256, md5 and sha1, the output's
    sha256, the converter's identity, the validation and the mzXML's own row; the
    mzXML rows are listed under `conversion_sources`. These are what the gate's
    SUM-1 converted link and CONV-1 read. `lineage_stands_for` attributes a
    converted `X.mzML` to `FILES/X.mzXML` and its sample everywhere an input is
    matched.
  - A file whose conversion fails is excluded as `conversion_failed`, and the rest
    of the unit runs. A unit left with no input is excluded and its preflight skips
    it. A full disk, or a file another process still holds after about 16 s of
    rename retries, stops the lease at `convert`, so that a retry converts the rest.
- `msdial_app.encoding_preference`: the Catalog's rule for which encoding of a
  sample is analysed, held to the Catalog's own vectors. The lease applies it to
  files an archive revealed: a readable twin is analysed instead of the mzXML where
  it lies in the same place once folder words naming an encoding are set aside, or
  wherever the unit admits it by itself. A convertible mzXML outranks a twin that
  RawDataHandler cannot decode. Each choice is recorded with `paired_by`.
- Agent capability `campaign_mzxml_converted_to_mzml`.

### Changed
- `CONVERSION_REQUIRED_SUFFIXES` is split into `CONVERTIBLE_SUFFIXES` (`.mzxml`)
  and `UNSUPPORTED_ENCODING_SUFFIXES` (`.mzdata`, `.mzdata.xml`).
- Every campaign disposition lists the inputs the lease kept out
  (`excluded_input_candidates`: `conversion_failed`, `unsupported_mzml_encoding`)
  in `excluded_inputs` with the lease's reason, so the gate's INP-1 accounts for a
  declared input that is not a candidate.

### Known limitations
- Converted units take the mzML defaults for the MS1/MS2 data type and the
  threshold step (Centroid, QTOF).
- MTBLS688's roughly 105 `.dat` files spelled differently from their mzXML still
  need a conversion nothing performs, so that unit stays excluded.

## [0.5.20] - Unreleased

### Added
- One analysis input per vendor folder (Catalog 0.6.0, `analysis_input_model`
  `one-input-per-sample.v1`). The handoff mapper keeps each
  `vendor_folder_member` as a member of the folder it names. It reads
  `analysis_inputs` (inline, or from `analysis_input_manifest_path`),
  `analysis_inputs_declared`, `analysis_input_issues` and `split_hint`. When a
  handoff's counts disagree with its own listing, that unit is excluded with
  `analysis_input:count_mismatch`; the batch no longer raises. MetaboBank
  MTBKS217 positive is 12 inputs, not 477 samples.
- `container_completeness`. Before discovery, the lease checks every folder the
  unit lists, member by member: the folder is there, every member has its
  listed size, and nothing is left as `.part`. A folder short of a member stops
  the lease by name. Files a reader writes into the folder it reads are told
  apart by the shared rules in `msdial_app.reader_created`. Only a Bruker BAF
  `.d` has them: baf2sql's `analysis.sqlite`, with its journal and write-ahead
  files. An `analysis.sqlite` in an Agilent `.d` is recorded as unlisted.
- The analysis CSV of a manifest with `input_lineage` is built from the lineage
  (`repository_analysis_rows`), one row per analysis input.
  - `acquisition_type` is, in order:
    - under an applied campaign disposition, the type the disposition decided;
    - otherwise the file's own `console_acquisition_type`;
    - failing that, the unit's declared DDA, SWATH or AIF.
  - A bare `DIA` is refused as `acquisition_type_ambiguous`. An input the
    disposition read and decided no type for is refused as
    `acquisition_type_not_decided`.
  - Some paths or names cannot be read back by the Console's parser (a comma, a
    quote, non-ASCII). Such an input is read through an alias in
    `raw\console-aliases`. A folder gets a junction. A file gets a hard link,
    and so do its SCIEX companions (`{stem}.wiff.scan`, `{stem}.wiff2.scan`,
    `{stem}.timeseries.data`).
  - When a unit's records disagree, `analysis_csv` is recorded as failed with
    codes, and the tool returns `ok: false`.
- Two kinds of excluded input get no CSV row:
  - inputs an applied `campaign_disposition` excluded;
  - an mzML the lease excluded as `unsupported_mzml_encoding` (0.5.18).
  Each is named with its reason and sample in `analysis_csv.excluded_inputs`.
  Its declared input and its sample are not counted missing.
- The execution gate refuses a workflow that runs an input with a different
  acquisition type than its lineage-built CSV row. That type is recorded on
  the input's lineage row. The gate reads a Console alias as the input it
  stands for.

### Changed
- Declared inputs are matched by path, and every one is required. A basename
  match is no longer a fallback (`match_declared_inputs`). A declared archived
  container is also found:
  - at the `container_path` recorded when its own archive was extracted;
  - failing that, through the sample its archive names.
  A MetaboLights `A.d.zip` holding `B.d` is therefore B.d's sample's input.
- The execution gate now looks up an input's decided type and its disposition
  exclusion through its Console alias, as it already did its header.
- The CSV builder reads the campaign disposition through the execution gate's
  own readers.
- `_find_msdial_inputs` returns only the outermost `.d`/`.raw` folders.
- A split part keeps only its own folders' inputs and members.
- A Console is trusted to read folder inputs on the strength of its
  `msdial-console-build-provenance.json`, not its path. The record's git head
  must descend from 77a42a87c (#739). Both pinned Consoles and 31dea2b39 are
  accepted, and `MSDIAL_ASSUME_FOLDER_TYPE_CSV_SUPPORTED=1` still overrides.
- The lineage path's answer seed carries the acquisition type every row
  shares, and none where the rows differ. It used to carry the unit's label
  ('DIA' as SWATH), which the guided plan wrote over every file.
- `_tree_size` counts an aliased folder or a hard-linked file once.

### Known limitations
- The gate's INP-1 counts only a binding disposition's `excluded_inputs` beside
  the input candidates until its next release, so a declared unit whose lease
  excluded an undecodable mzML FAILs INP-1 although its CSV is correct. That
  gate change must land before a campaign runs. CLS-2 and ORD-1's recorded-order
  source do not yet read aliased rows, folded Class labels or excluded inputs.

## [0.5.19] - Unreleased

### Changed
- The current pinned raw-metadata extractor is msrawdataworkbench `a12293c61`
  (#41, on master ecb5a86 with #40) with MsdialWorkbench `f0583493a`, built as
  `RawMetadataExtractor-a12293c61-f0583493a` (inventory `5acfab21512e96e3`,
  provenance verified). Compared with the `592b6dbce` build, which stays a built
  pin:
  - Waters DDA is DDA at confidence 0.95, told from MSe by whether a daughter
    function's precursor changes (Xevo G2, Q-TOF Premier and Synapt XS DDA were
    Unknown at 0.30, so a Waters unit without a declared mode could not run).
  - A WIFF2 that SCIEX Data API 1.0 cannot open is reported as unsupported
    (exit 82, naming SQLITE_NOTADB) instead of crashing (exit 1).
  - Shimadzu `.lcd` files read (DDA, 15 Da SWATH, survey), where every one used
    to fail.
  - Every other verdict on the 56-input demo and MTBLS2207 corpus is unchanged.

## [0.5.18] - Unreleased

### Fixed
- An mzML whose binary arrays RawDataHandler cannot decode is no longer run.
  RawDataHandler decodes only 32- and 64-bit float arrays, zlib-compressed or not.
  Numpress, other compressions, integer arrays, and type or compression terms given
  only through a param group reached a branch that only logs, and produced garbage or
  empty spectra without an error. `mzml_encoding` scans each mzML input's first
  spectra and chromatograms; the lease excludes such a file as
  `unsupported_mzml_encoding` with the accessions found, in `excluded_input_candidates`,
  `input_lineage.excluded` and the attribute stage. `mzml_encoding_problems(path)`
  returns the problems.
- A swapped Console dependency is no longer inspected as verified. The build record
  carries an inventory of every file beside MSDIALCUI.exe (logs and temporary files
  left out) and the ProductVersions of the key assemblies (RawDataHandler.dll,
  Common.dll, MsdialCore.dll and the other MS-DIAL assemblies). A changed, added or
  removed file is `stale_mismatch`, named. A record without an inventory verifies with
  the warning `inventory_not_recorded`;
  `scripts/record-console-inventory.py <MSDIALCUI.exe> --confirmed` adds one without a
  rebuild. The run manifest's Console block and each run attempt record
  `inventory_sha256`.
- A stalled repository download no longer holds its lease and then fails as a network
  error. Every read carries an idle timeout (`RepositoryHttpClient(idle_timeout=...)`,
  default 120 s); a stall, a lost connection or a short body is retried up to 3 times
  after 10, 30 and 90 s and resumes from the `.part` under the If-Range rules, each
  attempt recorded. Reads take what the socket has (`read1`), and progress is reported
  at least once a second, so a cancel is heard within the idle timeout plus a second
  however slowly bytes arrive; a stalled `.part` keeps every byte that arrived.
- A resume hashes its `.part` before it connects, reporting about once a second. It
  used to connect first and read nothing while hashing, and a server that drops an
  idle client dropped every resume of a large `.part`.

### Added
- `reader_created`: files a raw-data reader writes into an input container, such as
  the `analysis.sqlite` Bruker's baf2sql writes into a BAF `.d` that arrived without
  one. They are not inputs, container members or checksum failures. The container's
  lineage row carries `reader_created_files`, and the unit manifest lists them with
  size and sha256 after each repository run and wherever `record_reader_created_files`
  is called.

## [0.5.17] - Unreleased

### Added
- `msdial_check_raw_metadata_extractor` and `msdial_set_raw_metadata_extractor_path`.
  They list the extractors a preflight would consider, each with its source,
  provenance status, commits and pin state, and save one as the new
  `raw_metadata_extractor_path` setting. The setter refuses a build whose record does
  not verify unless `allow_unverified=true`. The candidate order is the argument, the
  setting, `MSDIAL_RAW_METADATA_EXTRACTOR`, then the sibling working checkout's build,
  reported as `working_checkout_default`.
- Pinned extractor builds are data (`PINNED_BUILDS`). The built pin is
  msrawdataworkbench `592b6dbce` with MsdialWorkbench `f0583493a`; `b34c857a5`/`c471463a5`
  is kept as planned. Under a campaign approval a preflight runs only the first
  extractor named, and refuses one that does not inspect as verified and pinned
  (`raw_metadata_extractor_refused`); the working-checkout build never qualifies.
- `campaign_disposition` (`msdial-campaign-disposition.v1`), written by every preflight
  and by `classify_preflight`: run, skip, exclude or split, with reason and warning
  codes, the excluded inputs, the split grouping by acquisition and polarity, and the
  extractor's identity. Under a campaign approval it is applied: `execution_allowed`,
  the status (`preflight_passed`, `skipped_by_preflight`, `excluded_by_preflight`) and
  each input's `console_acquisition_type` follow it, the execution gate admits each
  file only as its decided type and refuses excluded inputs, and the split planner
  leaves excluded inputs out. Outside a campaign it is advice only.
  - Rules: the header decides where the repository metadata is silent, and an
    unresolved header skips the unit; a declared mode stands when the headers are
    unreadable (`acquisition_declared_only`), and yields only to a header of
    confidence 0.8 or more; ion mobility is excluded (`ion_mobility_out_of_scope`);
    PRM/SRM/MRM/SIM and declared full scan are out of scope; MS1-only files beside DDA
    files run with them; mixed acquisition or polarity splits; unreadable files are
    excluded and the rest run; a missing or never-read input skips the unit.
  - A unit that was split, whose run finished, or that has an open run attempt is
    never changed by a disposition (`disposition_hold`).
- `raw_metadata_preflight_progress` records each extractor attempt's deadline and
  owner process; `preflight_progress_state` says whether it is running, overdue or
  gone.

### Changed
- The extractor reads at most 20 inputs per process, with no command line over
  32,767 characters. Each process's limit is the sum of its inputs' limits: 300 s per
  metadata reader, plus 1,800 s/GB for Waters and other full-spectrum formats, plus
  600 s/GB for ion-mobility data, at most 12 h per input. A group that fails or times
  out is read again one input at a time. OSError and TimeoutExpired are recorded.
- Every input has its own record in `summary.per_file`: outcome, format,
  `console_acquisition_type` (DDA, SWATH or AIF; a DIA verdict is resolved by its
  recorded isolation, never by default), `has_ms1`, `has_ms2`, `has_ion_mobility`,
  `method_source` and `reader_created_files`.
- The manifest keeps a command template, a chunk log and stderr tails instead of the
  full command and output. An input read before by the same extractor binary at the
  same size and modification time is not read again; a split part reuses its
  parent's reads.
- The preflight writes the manifest through `update_manifest`, so a write made
  meanwhile is kept, and a confirmed split holds the parent manifest's lock from plan
  to last write.
- Outside a campaign, an eligible unit with an unreadable input ends as before
  (`preflight_unavailable` or `preflight_unsupported_format`, `execution_allowed`
  kept).

### Fixed
- A unit of more than about 250 inputs can be preflighted; its single command line
  used to exceed the Windows limit.
- PRM is recognised; PRM, SRM, MRM and SIM are out of scope instead of "Unknown".
- `confirm_untargeted` is recorded as an inference from the headers, not as a
  confirmation.

## [0.5.16] - Unreleased

### Fixed
- Metabolomics Workbench units can be leased. 661 of the 839 declared
  campaign units list only a study archive with a published MD5.
  `_verify_project_allowlist_checksums` used to search `raw\data` for the
  archive's own name, found nothing once it had been extracted, and raised
  "resolved to 0 extracted files" after the whole download. A downloaded
  archive item (`raw_archive`, `shared_raw_archive`, or listed under the
  archive's own name) now counts as `archives_verified_at_download`, from the
  MD5 comparison made at download. A SHA-256 or SHA-1 on an archive is
  compared with the archive afterwards, and a per-sample zip inside an MB-POST
  tar with the digests taken before it expanded. The check uses one
  relative-path index, so it is linear in the number of files.
- `extracted_files` lists the unit's own members: the files its samples name,
  their `.wiff.scan`, and the contents of the `.d`/`.raw` folders they name.
  It used to be `[]` for every Workbench unit.
- A per-sample container archive is attributed to its sample: `X.raw.zip`
  stands for `X.raw` (`archives.container_alias`) in `_project_allowlist`,
  `_sample_file_names` and the input lineage. A sample that names a plain
  per-sample archive (`X.zip`) claims the files and the outermost `.d`/`.raw`
  folder that came out of it. In the 2026-09-22 snapshot that affects 4,540
  Workbench sample names in 76 units and 8,874 MetaboLights names in 159
  units, each of which failed at the attribute stage.
- A repository object is named by its percent-decoded URL basename.
  MetaboLights serves `130612_EG_NaHCO3_1 mM-1.raw.zip` as `..._1%20mM-1.raw.zip`
  (1,575 per-sample archives in 67 units), which used to unpack to a folder no
  sample names. An encoded separator or a name Windows cannot hold is kept as
  served.
- Two per-sample zips whose members sit at their root no longer overwrite each
  other's `_FUNC001.DAT`.
- `.7z`, `.rar`, bare `.gz` and `.lzma` objects are extracted. An HTML page
  saved as `.zip` is a failed download, not "no inputs".
- A download refused after the server sent a changed object whole no longer
  leaves the old `.part` beside the new object's validators, which let the
  next attempt append the new object's tail to the old head. A 206 whose
  ETag or Last-Modified differs from the stored one restarts from zero.

### Changed
- `create_download_lease` runs, and records in `lease_stages`, these stages:
  fetch, verify_declared_checksums, extract, materialise and convert
  (recorded as `not_used` until the download store and the mzXML converter are
  wired in), discover, attribute and record. A failed lease records
  `download_failure.stage` and, for an archive refusal, `archive_failure`.
- Archives are routed and opened only through `archives.py`.
  `ARCHIVE_SUFFIXES`, `_is_archive`, `_extract_archive` and the
  five-times-the-download-limit extraction bound are removed. Each archive is
  expanded into `raw\x<n>` under its guards, then moved into the data root
  without replacing anything; identical files already in place are counted as
  present, and any other collision refuses the archive before anything moves.
  A file inside a vendor folder is never opened as an archive.
- The manifest gains `archive_extractions[]` and `archive_warnings`. Member
  listings go to `provenance\archive-members-<sha12>.tsv` with their sha256.
  `downloads[]` entries record `archive` and `declared_checksum_algorithm`.
- `input_lineage` rows of kind `extracted_member` and `archived_container`
  carry a `basis`: `archive_declared_checksum` (the verified archive MD5, with
  the chain of nested archives to the nearest verified one),
  `member_declared_checksum` or `archive_download_hash`, and the member-listing
  row that accounts for them. For such inputs the Materials and Methods say
  "extracted from an archive whose published MD5 matched", never
  "checksum-verified".
- A repository download resumes only under the validator it began with: the
  strong ETag, or Last-Modified, is kept in `<name>.part.json` and sent back as
  If-Range.
- `archives.py` reads legacy LZMA-alone `.lzma` streams, confirmed by decoding
  the header. `.mzxml` is a container suffix, so `x.mzXML.lzma` stands for
  `x.mzXML`. Nested archive records keep their sha256, md5 and sha1.
- New agent capability: `repository_archive_extraction`.

### Known limitations
- A collision between two archives of one unit refuses the archive even when
  the colliding file (a README, say) is not an input.
- A plain archive that more than one sample names still fails at the
  attribute stage, because whose files it holds is not known.

## [0.5.15] - Unreleased

### Added
- `msdial_app/sharing.py`, the one rule for what leaves this machine.
  - A library is identified by `{name, sha256, bytes, private}`. It is public
    only with a DOI or an https source and no private licence, as the gate's
    SEC-1 decides it. A library no record describes is private.
  - A `SharingContext` rewrites locations in every encoding (backslashes,
    doubled backslashes, forward slashes, `file://`, `%20`). A repository
    unit's shared artifacts become workspace-relative (`raw/...`,
    `output/...`), name libraries and the Console by file name, and withhold
    every other absolute path. Each file is scanned with the gate's own
    patterns before it is left on disk. A laboratory run changes only where a
    private library would have been named.
- `msdial_app/run_finalisation.py`, called once from the production job after
  the Console returns and before validation.
  - For every run, the mzTab-M's `database[n]-uri` becomes `null` for a
    private library and the DOI for a public one, and a `custom[n]` line names
    the library by file name and sha256 (quoted where the name holds a comma).
    For a repository unit, `ms_run[n]-location` becomes workspace-relative. The
    original values go only to `provenance/mztab-redaction.local.json`
    (`sharing: local_only`).
  - For a unit with a recorded campaign approval (its own
    `campaign_authorizations`, or those of the unit it was split from), the
    finalised run's MS-DIAL containers move from beside the inputs to
    `output/msdial-intermediates/<raw-relative path>`, chosen by the mzTab-M's
    alignment timestamp, and are retained and inventoried file by file:
    `<file>_<ts>.dcl/.pai2/.rtc/.sfs/_tags.xml`, per-energy `.dcl`, and
    `AlignResult-<ts>.arf2/.dcl/.EIC.aef/_PeakProperties.arf/_DriftSopts.arf/_tags.xml`.
    Earlier attempts' sets are recorded as superseded and go with the raw data.
    The loaded-library copy `<project>_Loaded.msp2.dbs` is deleted and
    recorded by name, size and sha256. The saved project is recorded as not
    reopenable in place, with what it references. Any other run keeps its
    containers and library copy, as before, so its project still reopens.
  - Every file step retries on `PermissionError` within one 60 s budget of
    waiting, and uses extended-length paths, because a moved container's path
    can pass MAX_PATH when LongPathsEnabled is 0.
  - What still fails is recorded in the unit manifest's `finalisation_holds`.
    An unredacted mzTab-M or an undeleted library copy blocks sharing: the
    publication report refuses the unit (`finalisation_held [sharing]`). A
    container still beside the inputs blocks raw deletion: the cleanup plan
    lists it as a blocker, and cleanup (preview and confirmed) and discard
    refuse it, a split parent's included. Each of these steps retries the held
    step first.

### Changed
- Publication report, Materials and Methods, supplementary TSV and workbook
  are redacted as above. The report declares `shared_path_policy` and lists
  libraries by name and sha256; its `workflow` block is redacted. A library
  with a recorded checksum is no longer warned about as having no identifier.
- Workflow bundle: portable renderings of `method.txt`, `analysis_files.csv`,
  the annotator settings, `run-manifest.json` and `workflow-settings.json`,
  with `SHARED-PATHS.json` as its declaration. No library file, library copy
  or local-only record is ever a member. `guided-answers.json` is marked
  `local_only`.
- A repository unit's `run-manifest.json` names libraries by identity only.

## [0.5.14] - Unreleased

### Added
- A watch on the MS-DIAL Console. `run_console` takes `timeout_seconds` and
  `idle_timeout_seconds`, a `cancel_event` and an `on_start(pid)` callback.
  Idle means no output line, and no change in the Console's output and export
  folders or in the folders holding its inputs (where MS-DIAL writes its
  intermediates), for that long.
  - A stop kills the whole process tree: `taskkill /T /F` on Windows, the
    process group elsewhere. The tree is also stopped whenever anything fails
    after the process exists (the deadline, `on_start`, a line handler, the
    watch itself).
  - Every line the pipe delivered within 10 s of the Console's exit is passed
    on, however slow the consumer. A child that inherited the pipe and keeps
    writing cannot hold the job.
  - Exit codes: -3 for either limit, -4 for a cancel; -2 stays the SCIEX
    sidecar stop. A limit past ten years is refused before any job is
    registered (HTTP 400).
  - With no limit and no cancel flag, the Console runs exactly as before.
- `POST /api/jobs/<id>/cancel` and the MCP tool `msdial_cancel_job` stop a
  queued or running run, peak-count diagnostic, or repository download.
  - A queued job stops before it starts anything.
  - A Console job fails with exit code -4 and `stop_reason: cancelled`.
  - A download stops at its next progress report. Its lease records
    `download_failed` with reason `cancelled` and keeps the partial file for a
    resume.
  - Jobs record the Console's process id and deadline (`console_process`) and
    how it ended (`console_outcome`); job summaries carry `stop_reason`.
- `timeout_seconds` and `idle_timeout_seconds` on `/api/agent/run`,
  `/api/agent/tuning/run`, `msdial_start_guided_analysis` and
  `msdial_start_peak_count_diagnostic`. There is no limit unless one is given.
- `run_attempts[]` in the unit manifest. `record_run_start` opens an attempt
  before the Console starts: attempt number per kind, job, UTC start, the
  backend process, the Console's version and checksums (never its location),
  the command's sha256, the output directory and the limits.
  `record_run_process` adds the Console's process id and creation time, and
  `record_run_end` closes the attempt with the exit code and the reason
  (exited, timeout, idle_timeout, cancelled, sciex_scan_sidecar, start_failed
  or error). All three write through the manifest lock and never raise.
- One live Console per repository unit. A run or diagnostic for a unit that
  already has one queued or running, or whose `run_attempts` name a Console
  that is still alive, is refused with HTTP 409 (`code: unit_busy`,
  `live_job_id`) before anything is prepared; the MCP tools report
  `reason: unit_busy`. Liveness is read through `process_liveness`.
- Capabilities `console_time_limits`, `cancel_console_and_download_jobs`,
  `repository_run_attempt_records` and `one_console_per_repository_unit`.

### Changed
- When the watch stopped a job's Console, the job's error says so first.

### Known limitations
- A cancel is noticed at a download's next progress report, so a download
  whose read has stalled is not stopped until the read returns.

## [0.5.13] - Unreleased

### Added
- An accession-scoped download store, `msdial_app/download_store.py`, which
  fetches each repository object once and gives every unit a tree of
  hardlinks to it. The lease does not use it yet. In the declared pool 489
  units list a URL that another unit also lists; fetching each URL once
  instead of once per unit moves 8.22 TB instead of 14.37 TB. No sharing
  group crosses an accession, so the store lives beside the units it serves,
  at `<workspace>/<repository>/<accession>/_dl`. Objects are identified by
  the sha256 of their bytes and found by URL. A URL whose bytes change
  upstream becomes a new object, and earlier consumers keep the bytes they
  used.
  - A cached object is reused only when the unit's declared MD5 matches it, a
    HEAD Content-Length (when the server answers) matches it, and every file
    units link to still has its recorded size and mtime_ns. A HEAD size that a
    refetch showed to be wrong for unchanged bytes is remembered
    (`head_size_mismatch`). `force_refetch` lets a retry policy ignore the
    cache.
  - Fetched bytes are compared with the declared MD5 before anything is
    extracted, so 7-Zip never sees unverified bytes. A declared MD5 counts as
    wrong only after two full fetches return the same other bytes: one
    damaged transfer does not fail a unit for good, and a wrong declaration
    costs at most two transfers. A consumer with no declaration of its own
    does not reuse bytes another unit's declaration disputes.
  - A linked file written in place is detected, because a hardlink is the
    store's own file record. The object is then fetched again, or the member
    re-extracted from the kept archive.
  - Locks are exclusive-create files with a heartbeat. A lock is stale only
    when its heartbeat has lapsed and its holder is dead, read through
    OpenProcess or psutil (`msdial_app.process_liveness`), never `os.kill`,
    which on Windows terminates the process it is pointed at. A second
    consumer waits (`waiting_for_shared_download`). Claim writes and GC
    decisions on a URL share a short claim lock, so a batch pre-claim cannot
    have its object deleted from under it.
  - `gc` deletes raw data only as far as the campaign approval reaches. It
    reads the `msdial-campaign-authorization.v1` record from its file
    (`CampaignAuthorization.load`) and deletes nothing when the approval is
    revoked, does not cover boundary 5, or keeps raw data. An object or an
    abandoned partial transfer is deleted only when boundary 5 is covered for
    every unit whose release freed it; one that no unit ever claimed is kept
    and reported. It leaves `entry.json` as a tombstone recording the
    approval, the manifest digest, the sha256 of the record's bytes and the
    units covered, never the record's location, and keeps the member listing.
  - Unit-tree materialization refuses any directory path over 247
    characters during planning (`path_too_long`).
  - `unlink_tree` never changes the attributes of a file that other links
    share. NTFS keeps the read-only attribute on the record every link names,
    so clearing it to delete one unit's link would clear it on the store
    object and on every other unit's link.

## [0.5.12] - Unreleased

### Added
- `msdial_app/raw_metadata_extractor.py` gives the raw-metadata extractor the
  kind of identity the Console already has. Until now preflight knew it only
  by path, size and mtime, and the binary in use names 448cc99 in its
  ProductVersion while its tree is at edcc2e6. Nothing selects or runs the
  extractor differently yet.
  - `plan_extractor_build` previews a build at msrawdataworkbench b34c857a5
    and MsdialWorkbench c471463a5 into
    `RawMetadataExtractor-b34c857a5-c471463a5\{msrawdataworkbench,MsdialWorkbench}`.
    The trees are local clones (`git clone --no-hardlinks --no-checkout`,
    then a detached checkout), never worktrees in the checkouts they come
    from. The command passes `-p:SolutionDir=<tree>\` with its trailing
    separator and runs without `EAZFUSCATOR_NET_HOME`. The preview clones,
    builds and writes nothing, reports its blockers, and lists the upstream
    fetch it leaves out.
  - `record_build` writes `raw-metadata-extractor-build-provenance.json`
    beside the exe, once: the binary's sha256; an inventory sha256 over every
    file in the output folder; the ProductVersion of the exe,
    `RawDataHandler.dll`, `Common.dll` and `NCDK.dll`; the
    `project.assets.json` sha256; the git state of both trees, the SDK and the
    command. It refuses unless the binary is
    `RawMetadataConsoleApp\bin\Release\net48\RawMetadataConsoleApp.exe` in the
    recorded tree (and the plan's `binary_path` when a plan is given), and
    refuses a record the build contradicts: an assembly whose embedded
    revision is not its tree's head, a restore graph that compiled a project
    from outside the two trees, or a Common tree that is not the sibling the
    project references.
  - `inspect_raw_metadata_extractor` re-hashes the folder and returns
    `verified`, `absent`, `stale_mismatch` (naming the changed, added and
    removed files), `unreadable` or `dirty_source`, with `inventory_sha256`
    and `file_count` whenever the binary exists. A vendor DLL swapped beside
    an unchanged exe therefore reads as stale, and an extractor without a
    record is still identified by the files it ran with.
  - `record_verification` adds the post-build verification block, which is
    not part of the identity.

## [0.5.11] - Unreleased

### Added
- `msdial_app/mzxml_conversion.py` converts mzXML 2.x and 3.x to the plain
  mzML that MS-DIAL's RawDataHandler reads, using only the standard library.
  MS-DIAL has no mzXML reader, so a sample whose only encoding is mzXML could
  not be analysed. The repository lease does not call it yet.
  - `convert_mzxml_to_mzml` streams the file with iterparse, flattening
    nested scans in document order, and writes polarity as a spectrum-level
    cvParam; the precursorMz as both the isolation target (MS:1000827) and
    the selected ion (MS:1000744); MS:1000828/829 only where windowWideness
    is recorded; activation and collision energy only where recorded (an
    absent method is a userParam, never CID); scan start time in seconds
    (UO:0000010), 64-bit m/z, intensity at its source precision, and zlib;
    no self-closed or empty containers, no referenceableParamGroup and no
    startTimeStamp.
  - The mzXML's embedded sha1 is verified, and a mismatch fails the file by
    default. The output bytes depend only on the mzXML, its
    repository-relative name, the options and the converter identity, which
    names the zlib build.
  - `validate_conversion` re-reads the output with an independent expat
    reader, lints what the pinned parser cannot read, and compares every
    spectrum and the exact array bytes with the mzXML. A conversion is
    validated before it is renamed into place.
  - Every failure is returned as a record (schema
    `msdial-mzxml-conversion.v1`), never raised. The converter replaces or
    removes only a destination file it wrote itself (its own mzML header and
    processing record, or byte for byte the output of the previous record).
    Anything else at the destination, such as a repository's own mzML, fails
    the conversion, is left in place, and is flagged `foreign_file_kept`.
  - A previous record is reused when the source, source file name, output,
    options and converter still match. The reused record names this call's
    paths and times, and keeps the original under `reused_from`.
  - Polarity imputation from the declared ion mode, DIA window inference
    from a uniform ladder, all-ion window synthesis from the scan range, and
    the spectrum-level collision energy that AIF deconvolution needs in the
    pinned Console are each off by default and recorded when used. The
    spectrum-level collision energy departs from the PSI mapping rules.
  - Arrays are not padded to hide RawDataHandler's upstream defect of never
    decoding the last array element.

## [0.5.10] - Unreleased

### Added
- `msdial_app/archives.py`, the one place that decides what a downloaded
  archive is and how it is opened. Nothing calls it yet; the lease
  integration follows separately.
  - `archive_kind` reads the name's suffix (`.tar.gz`/`.tgz`,
    `.tar.bz2`/`.tbz2`, `.tar.xz`/`.txz`, `.zip`, `.tar`, `.7z`, `.rar`, and
    bare `.gz`/`.bz2`/`.xz`) and confirms it by magic bytes. An HTML error
    page saved as `.zip` raises `not_an_archive` instead of yielding no
    inputs. A bare `.gz` that holds a tar is treated as a tar.
  - zip, tar and compressed streams are read with the standard library. 7z,
    RAR4/RAR5, and zips whose method Python cannot read (Deflate64 from
    Windows Explorer, PPMd) go to 7-Zip, found through the injected setting,
    `MSDIAL_SEVENZIP`, the registry, Program Files, then PATH. It must be
    25.00 or later, and the sha256 of `7z.exe` and `7z.dll` is recorded.
  - `7z x` rewrites `../evil.txt` to `evil.txt` and `C:/abs.txt` to
    `C_/abs.txt` and exits 0, so every listing is validated before anything
    is written. An archive is refused as a whole for traversal, drive or UNC
    paths, or `:`; reserved device names, or trailing dots or spaces; names
    that collide up to case, or file/directory conflicts; links, reparse
    points or special files; encrypted members; a path over 259 characters or
    a folder over 247 (`path_too_long`, the CreateDirectoryW limit without
    long paths); and a tar member name that is not UTF-8
    (`undecodable_name`).
  - 7-Zip runs with stdin closed and a sentinel password, so an encrypted
    archive fails at once instead of waiting for a password. Its failures are
    named from what it says, damage before encryption: a truncated or
    CRC-failed zip, 7z or rar is `corrupt_archive`, and only 'Wrong
    password', 'Cannot open encrypted archive' or '... in encrypted file' make
    `encrypted_archive`. A zip that zipfile refuses and 7-Zip cannot read is
    `corrupt_archive`, with the zipfile error in `detail.stdlib_error`. A
    multi-line archive comment is read as a value, not as warnings.
  - One budget covers the whole lineage: a disk reserve checked against the
    exact listing; an expansion ratio limit (100, above 10 GB); at most
    1,000,000 members and nesting depth 3; a watchdog that kills 7-Zip on a
    disk or size breach, and a timeout. The watchdog is stopped whatever ends
    supervision, and a free-space probe that raises stops 7-Zip as
    `watchdog_failed`.
  - An archive expands into `<destination>.partial`, is checked against its
    listing, and only then is renamed. No `.partial` is left after a failure;
    an unexpected exception becomes `extraction_failed`.
  - Operating-system metadata (`__MACOSX/`, `.DS_Store`, `._` AppleDouble
    files, `Thumbs.db`) is validated, then dropped and recorded in
    `dropped_metadata`, so a Finder-made `S1.d.zip` unpacks as `S1.d/`.
  - Root-less per-sample containers (`X.raw.zip`, `X.d.zip`) get a folder
    named after the container, so they no longer write `_FUNC001.DAT` over
    each other. `container_rooted` requires the top folder to be the
    archive's own stem; `A.raw.zip` holding `B.raw` is
    `container_rooted_other_name` and is extracted as packed. Each record
    names the container it produced (`container_root`) and whether that
    differs from the alias (`container_name_mismatch`).
  - An empty nested archive expands to an empty folder. A nested archive
    whose expansion already exists beside it (`run.mzML.gz` next to
    `run.mzML`) is left packed and recorded in `nested_skipped` as
    `destination_exists`.
  - The returned record carries the reader and its version, the command, the
    counts, the destination rule and the nested records. The member listing
    is written as `archive-members-<sha12>.tsv`, with its sha256 recorded.

### Known limitations
- Zip names without the UTF-8 flag are decoded as cp437 by zipfile, so
  Shift-JIS names extract as mojibake. A legacy `.lzma` stream is not yet a
  recognised kind.

## [0.5.9] - Unreleased

### Added
- Campaign authorization records (`msdial_app/campaign_authorization.py`,
  schema `msdial-campaign-authorization.v1`), the first piece of an
  unattended campaign.
  - A record holds the approval id, the campaign manifest digest, the covered
    boundaries (1, 3, 4, 5 and `split`), the unit ids, the raw retention, and
    the libraries by file name and sha256.
  - `validate(unit_id, boundary)` returns a crossing record or refuses with
    codes.
  - Download (boundary 1), split, cleanup (5), preparation (3), run and
    diagnostic (4) take `campaign_authorization_path`; the plan and batch-plan
    tools report coverage.
  - When the record covers the boundary for the unit, it stands in for
    `confirmed=true`, and the crossing is written to the manifest's
    `campaign_authorizations` before the step runs. With no record nothing
    changes.
  - A record that does not hold is refused (`reason:
    campaign_authorization_refused`, `codes`), even when `confirmed=true` is
    also passed. A record lifts no size limit, never covers boundaries 2 or 6,
    and is refused when it carries a library location.
- `input_lineage` in the run manifest (`msdial-input-lineage.v1`): one row per
  analysis input with its kind (file, vendor_folder, archived_container,
  extracted_member), source, checksums, declared names and sample id. When the
  allow-list check compared a file's or an extracted member's own declared
  md5, sha1 or sha256, the row carries `declared`, `declared_algorithm` and
  `declared_verified: true`. Split parts inherit their rows. A manifest
  without the block is legacy.
- Re-entry by `manifest_path` in place of the download or diagnostic job id,
  for the raw-metadata preflight, split, repository preparation, QA evidence,
  the peak-height estimate, LC-MS QA and the publication report, after the
  job registry has forgotten the job. Diagnostics record themselves in
  `diagnostics/<job>/diagnostic-job.json`, and finalisation records the
  production run as `finalized_run`.
- `refresh_retained_artifacts`, called after a repository unit's publication
  report, so the retained inventory lists the publication artifacts.
- `MSDIAL_INTERACTIVE_JOBS_FILE` gives a backend its own job registry.
- `msdial_app.process_liveness` reads whether a process is alive through
  OpenProcess or psutil, never `os.kill`.

### Changed
- Unit manifests are written atomically (temporary file, fsync, `os.replace`)
  under a per-manifest operating-system lock (`<name>.lock`). A crash leaves
  the previous record readable, and two writers no longer lose each other's
  update. Lock and temporary files are never retained artifacts.
- Every unit-manifest reader goes through `read_manifest`, which waits out a
  concurrent rename (on Windows the rename makes a reader fail with
  PermissionError) for about 25 s and then raises `ManifestBusyError`. MCP
  tools report `reason: manifest_busy` (retryable) and `os_error` as
  structured failures instead of "Error executing tool".
- `create_download_lease` records the lease before the first byte (`status:
  downloading`), with `lease_owner` (lease id, job id, process id and creation
  time, host), and refreshes `download_progress_at` while bytes arrive. A lease
  that fails is recorded as `download_failed`, with the reason and the objects
  that arrived, so `discard_download_lease` can release its bytes.
- A retried lease keeps the record it replaces. Before its first write it
  copies an existing manifest byte for byte to
  `provenance\run-manifest.superseded-<UTC>.json`, unless that manifest is
  itself an unfinished or discarded lease, and records the copy's path and
  sha256 in `previous_manifest.superseded_copy` and `superseded_manifests`,
  which every later record carries forward. A split parent
  (`split_by_acquisition`) is not re-leased, and a lease into a workspace that
  another live lease is still downloading into is refused.
- `discard_download_lease` refuses a lease that is still downloading, unless
  its owner is provably gone (the process has exited or its pid was reused);
  it then records `stale_lease_discarded`. An owner that is alive, or cannot
  be read, is still refused.

## [0.5.8] - Unreleased

### Fixed
- The method writer reads a template line the way the MS-DIAL Console does:
  the key before the first `:` or `=`, trimmed and case-folded, and a `#`
  line as a comment (`workflow.console_method_key`). It treats every spelling
  the Console reads as one setting as that setting
  (`workflow.CONSOLE_KEY_ALIASES`, the case labels that share an arm in the
  Console's ConfigParser at MsdialWorkbench f0583493a). Such a line is
  rewritten with Interactive's value, as an exact `key:` line already was.
  Before, only an exact `key:` prefix matched. A template line under an alias
  (`Console alignment light mode`, `LBM annotation priority`,
  `MSP search settings file path`, ...), with `=`, or with a space before the
  colon was left in place, and Interactive inserted its own line at the top of
  the file. Since MsdialWorkbench #817 every Console reader takes the last line
  that sets a value, so the template line decided the run: alignment light
  mode, the LBM annotator priority, the MSP or Text annotator settings file.
  The main reader was already last-wins, so `Minimum peak height = 1000` after
  Interactive's `Minimum peak height: 9000` ran at 1000 on every Console. Method
  files written from the shipped templates are byte-identical to before.

## [0.5.7] - Unreleased

### Changed
- The run manifest identifies a net8 Console by its assembly, which lifts the
  0.5.0 known limitation that it hashed only the launcher. The `console` block
  of `run-manifest.json` and the run's `software_provenance` record
  `assembly_path` and `assembly_sha256` beside the launcher's `binary_sha256`.
  The assembly is the `MSDIALCUI.dll` beside a launcher (`MSDIALCUI.exe`, or
  `MSDIALCUI` on Linux and macOS): a file with a `MSDIALCUI.runtimeconfig.json`
  beside it and no CLI header in its own image. Otherwise it is the Console
  path itself, so for net48 both checksums are the exe's, and a stale net8 dll
  beside it is not recorded, even after a net48 archive is unpacked over a net8
  folder. Two net8 launchers differ only in their version string, and not at
  all after a rebuild at the same commit, so the launcher checksum alone did
  not identify the code that ran. The capability probe reads the same file,
  through one shared rule. The build-provenance record, which the build tool
  writes for the assembly it built, is compared with the assembly: a
  tool-built net8 Console selected by its launcher read as `stale_mismatch` and
  now reads as `verified`, and a dll rebuilt outside the tool reads as
  `stale_mismatch` whichever file is selected. `provenance_mismatch` names the
  file compared as `actual_binary_path`.

## [0.5.6] - Unreleased

### Fixed
- The automatic RT correction audit viewer (#36) counted anchors in Blank files
  as rejected. The Console gives a Blank's matched anchors the status
  `BlankInterpolateByOrder` or `BlankNotCorrected` and never uses them: a
  Blank's model is interpolated or copied from neighbouring injections, or it
  keeps its original RT. So every run with a Blank warned "Rejected anchors
  require review", and the UI counted, highlighted and plotted those anchors as
  rejections. Unused anchor records are now reported in three groups:
  rejections by the model (`MadOutlier`, `NonMonotonic`, `InsufficientAnchors`,
  and any status the viewer does not know), which keep the warning, the
  highlight and the count, now labelled "anchors rejected by the model";
  records in a non-Blank file that matched no single peak (`Missing`,
  `Ambiguous`); and every record in a Blank file, not used by design. The last
  two groups are notes, not warnings (`unmatched_status_counts`,
  `blank_status_counts`, `notes`, and a `category` on each anchor), and a Blank's
  anchors are drawn in grey. The warning about anchors without an RT counts only
  records whose status does not explain it: a `Missing` or `Ambiguous` record has
  none by construction.
- The smoothed EIC preview was never withheld when the Console had discarded a
  smoothing setting. The Console records a value it could not use as
  `<key>: <value>` in `method.keys.json`, and the viewer compared those entries
  whole with the bare key names, so none ever matched. Entries are now read as
  the publication report reads them, by the key before the first colon,
  case-folded, and a smoothing method or level listed under `blank` withholds
  the preview too. It is withheld even when another line of the method file
  applied the key, because the preview takes the method file's last line for
  it, which need not be the value the Console kept.
- The audit viewer and the publication report judged the same records, the two
  audit TSVs and `method.keys.json`, with separately written rules, so the
  viewer could call a run's evidence verified while the report refused to
  describe its correction: when the Console had not recorded the automatic RT
  correction key as applied (the viewer warned and still said verified), when
  it had discarded an automatic RT correction setting, or when no file other
  than the reference was corrected from its own anchors. Both now take one
  verdict from `automatic_rt_evidence.automatic_rt_correction_proof`. The viewer
  calls the evidence verified exactly when the report describes the correction,
  shows the report's reason code otherwise (`method_audit.reason`), and extracts
  an EIC only on the same verdict. Its own status names `method_mismatch`,
  `stale_rt_audit`, `missing` and `unreadable` give way to the report's
  `method_key_record_not_from_this_method_file`,
  `audit_older_than_method_key_record` and `retained_evidence_missing`. The
  report reaches the same verdicts as before.

## [0.5.5] - Unreleased

### Fixed
- **Extract EICs and detect anchors** in the RT correction review workspace
  (`/api/rt-correction/run`) no longer writes into the folder that holds the
  parameter template. It passed the template itself to the Console's
  `rtcorrection` as `-m`, and `ConfigParser.ReadForLcmsParameter`, in the pinned
  Console (MsdialWorkbench 31dea2b39) and on master, writes
  `<method stem>.keys.json` beside the method file it reads. For the default
  template, every audit therefore left
  `resources/msdial_console_param4lipidomics.keys.json` in the Interactive
  checkout. The audit now copies the template byte for byte to
  `rt_correction_method.txt` in its output directory and passes that copy, so
  the record lands there as `rt_correction_method.keys.json`. The name is its
  own, so it cannot overwrite the `method.keys.json` of a run in the same
  directory. `rtcorrection` resolves no path against the method file's folder,
  so the copy is read exactly as the template was. The job's preparation records
  both files (`template_file`, `method_file`). MS-DIAL runs and the
  zero-threshold diagnostic were not affected: they already wrote their own
  `method.txt` into the run directory.

## [0.5.4] - Unreleased

### Changed
- The absolute run-order/intensity correlation is not assessed where a
  repository unit's analytical order is the file listing or a sequence number
  read out of the file names (decided 2026-09-29): neither is a recorded
  injection order. The correlation is computed against whatever
  analytical_order the analysis CSV carries; against the names it describes the
  names, and for a study with no QC it was the one criterion left to report as
  met. Its reason is "the injection order was not recorded for every file", one
  of `quality_assurance.NOT_ASSESSED_REASON_PHRASES`, unless the counts decide
  first (no features, fewer than three injections), as they do for a value that
  was never computed. The value is withheld from the assessment, the QA summary,
  the text, the Supplementary Table and the workbook alike, and so is
  `run_order_reference_match_correlation`, computed against the same order.
- `msdial_prepare_repository_reanalysis` records, with the header decision in the
  unit manifest's `analytical_order`, what the order was taken from in the end:
  `order_source` is `raw_header_acquisition_start_time`, `repository_sample_table`
  (every file's order declared by the repository), `embedded` or `listing`, with
  `declared_files` when only some files had a declared order (the rest are
  named by what filled them, so a partly declared order is withheld too), and
  None when the recognised order cannot be told apart. The record lists every
  file with the order it was given; the preview gives only `files_recorded`, the
  count, so that a large unit does not land in the model context.
- The publication report reads that source through the run's
  `repository_run_manifest` and writes it to the audit as
  `analytical_order_source`. It applies while the run's files are among the
  unit's inputs (by name) and keep the relative order the record gives them, a
  file dropped since or the ranks renumbered included; a reordered CSV carries
  an order nobody recorded and is assessed. The header adoption keeps its exact
  match, since it calls an order measured.
- A job's live QA report (`/api/qa/report`, and through it
  `msdial_generate_lcms_qa`, `msdial_complete_guided_analysis` and the QA panel)
  withholds the same statistics and carries `analytical_order_source`, read
  from the run's workflow-settings.json, so that it says what the publication
  report will. A QA matrix read by path alone is unchanged.
- `with_qc_minimum` fills the reasons a summary lacks instead of replacing them
  all, so a reason another rule gave survives whatever order they run in; for a
  QC criterion below the QC minimum the minimum's reason still decides.

### Not changed
- A laboratory analysis, whose order is the analyst's, is assessed as before.
- A repository unit prepared through the GUI writes no order record (the GUI
  path has never ranked by the raw headers either), so it is assessed as
  before; the reanalysis campaign prepares units through the MCP tools.
- A unit prepared before this release is assessed as before unless it is
  prepared again: the record belongs to the unit, so a report regenerated for
  an earlier run of a re-prepared unit follows the new record.

## [0.5.3] - Unreleased

0.5.3 is the first version whose every build contains #36 (the automatic
alignment RT correction audit viewer and the UI fixes below). #36 reached main
at 212675a while the package still read 0.5.2, so a build reading 0.5.2 may be
6d164c6, without it, or 212675a, with it.

### Added
- An audit viewer for a completed LC-MS run that used automatic alignment-only
  RT correction (#36), in **5. Validate & run**. It reads the Console's
  `automatic_alignment_rt_correction_summary.tsv` and
  `automatic_alignment_rt_correction_anchors.tsv`, checks the method hash
  against `method.keys.json`, and shows the reference file, the accepted and
  rejected anchors, each file's RT shift and the model's warnings. It extracts an
  anchor's MS1 EIC on demand through the Console recorded by the run, on the
  original and on the projected alignment RT axis; raw data are neither copied
  nor changed, and nothing in the run is altered. It needs a Console built with
  MsdialWorkbench #810 or later; the Console at 31dea2b39 writes neither audit
  file. See `docs/automatic_alignment_rt_review.md`.

### Fixed
- Minimum peak height and Mass slice width in the UI take their starting values
  from the fields the backend returns (`suggested_minimum_peak_height`,
  `suggested_mass_slice_width`), falling back to the template's peak-picking
  defaults, now served as `default_peak_picking` by `/api/config` (#36). From
  0.4.7 until #36, adding the first file, importing a CSV or a repository unit
  in the UI, or applying the format starting values, emptied both fields, so a
  run prepared in the UI wrote `Minimum peak height: 0` and `Mass slice width: 0`
  to method.txt unless the values were typed again, and validation raised
  nothing. The backend had renamed the fields in 0.4.7 and the UI still read the
  old names. Runs prepared through the MCP tools do not pass through these
  fields: both MTBLS2207 production method files read Mass slice width 0.1.
- The UI's zero-threshold diagnostic and RT-correction jobs read the compact
  job summary and fetch the full job (`?detail=full`) for its result (#36).

### Changed
- A QC-based QA criterion (median QC feature RSD, the fraction of QC features with
  RSD <=30%, the median QC detection rate and the QC PCA relative dispersion)
  is assessed only from three or more QC injections. The QA warning already said
  three were needed, while an RSD or a dispersion was computed from two QC and a
  detection rate from one, so a run with two QC was assessed on a precision it
  cannot show and written up as having too few QC at the same time.
- Every criterion that could not be assessed now carries the reason it fell to,
  recorded with the QA summary (`not_assessed_reasons`) and on its check in the
  assessment (`reason`), from the fixed phrases in
  `quality_assurance.NOT_ASSESSED_REASON_PHRASES`: no features, too few QC
  injections, no feature detected in two or more QC, no QC dispersion from the
  PCA, no Blank files, no feature in both a Blank and a study sample, no Blank
  after an injection with detected features in its batch, too few injections,
  run order or intensity not varying, and the QA matrix giving no value; without
  a QA matrix, that none was supplied. The
  Methods and QA results name each criterion with its reason ("could not be
  assessed: A and B because ...; C because ..."), instead of one reason for all
  that named a QC shortage for blank-based criteria and gave none when the run
  had three QC and blanks. The Supplementary Table gives each such criterion a
  "Not assessed because" row, and the workbook the same in its note; the
  reasons are no longer listed as an observed metric. A QA report written before
  this release is published as this release would have written it: the QC
  minimum applied and the reasons filled in, in the text, the table and the
  audit alike.

## [0.5.2] - Unreleased

### Changed
- A repository unit's analytical order is the order its files were acquired, as
  each raw header records it, whenever every input has a readable acquisition
  start time. The raw-metadata preflight already read that time (mzML
  `run@startTimeStamp`, vendor headers) and the summary dropped it; the order
  was the file listing, or a number read out of the names, and a repository
  sample table's declared order overwrote either. The header order is applied
  last, so it outranks both. Nothing is reordered, and the record says why,
  when the unit's headers were never read, when a file has no time or an
  unreadable one, when every file shares one time, or when files of different
  Classes share a time: in each case the listing, not the headers, would decide.
  Equal times within one Class keep listing order and are named. .NET fractions
  of any length are read. The extractor writes every time with an offset,
  assuming one where the header had none, so each file's extractor evidence is
  kept with its time. `msdial_prepare_repository_reanalysis` shows the order in
  its preview, writes it into `analysis_files.csv`, and records how it was
  decided, with every file's start time, as `analytical_order` in the unit's run
  manifest, but only once the CSV exists. A preflight summarised before this
  release is read through the extractor output it names, and a split part
  without its own preflight through its parent's. The guided plan reports the
  order as `raw_header_acquisition_start_time`, in both its inspection and its
  workflow, only while the analysis CSV carries exactly the recorded order;
  otherwise the name-derived source stands, so the Blank-interpolation warning
  still fires, and the record is attached as a note. A record is adopted only
  for the unit whose inputs it describes. Found on MTBLS2207, where the listing
  put a December 2019 acquisition last, and the only QA criterion the run could
  evaluate was a run-order drift computed against that listing.
- Without a QC, the zero-threshold diagnostic's representative is the Sample
  nearest the run midpoint, not any non-Blank file: a Standard is a chemical
  mix, not the matrix the threshold is for. Other non-Blank files are used only
  when there is no Sample. File types are read as the Console reads them,
  numbers included (Sample 0, Standard 1, QC 2, Blank 3). Found on MTBLS2207,
  where with the header order the standard mix sat at the DDA midpoint.

## [0.5.1] - 2026-09-26

### Fixed
- The publication report warned that no persistent identifier was recorded for
  every library of an agent-guided run, including libraries whose DOI and Zenodo
  record it held. The guided workflow records a library under `path` and its
  repository URL under `source`; the report looked only for `local_path`, and for
  `doi` or `record_url`. It now reads both spellings and accepts any identifier the
  warning asks for (version, DOI, repository URL or checksum). Found by the public
  reanalysis gate's LIB-1 on the first MTBLS2207 production run.
- The Methods and QA results text recited the whole QA battery, including QC
  precision and blank separation, whatever the sample types allowed, and reported
  "1 of 1 evaluable criteria". It now names the criteria that could be evaluated,
  those that could not, and why (too few QC injections, no Blank files).

## [0.5.0] - 2026-09-26

Agent API 0.5 rejects a 0.4 backend as incompatible.

### Added
- Repository split preview and confirmed split of a Mixed unit by acquisition mode.
  Parts retain their own files and Class assignments, share raw data without copying
  it, and start with `execution_allowed=false`. (#27)
- Interrupted repository downloads resume from `.part` using HTTP Range; library
  storage can be set with `library_directory` or `MSDIAL_LIBRARY_DIRECTORY`. (#24)
- Run manifests record zero-threshold peak diagnostics, retention policy, failed
  runs, and the CLI `--raw-retention-policy` choice. (#23)
- Agent status reports `app_version`, Agent API 0.5, and capabilities for unit
  splitting, confirmed raw cleanup, download resume, required mzXML conversion,
  and automatic alignment RT correction. The MCP server reports its package version.
- Optional LC-MS automatic alignment-only RT correction: selects anchors from
  detected peaks and applies corrected times for alignment while leaving peak
  picking and annotation on original RTs. The 15 method keys and defaults are:
  `execute automatic rt correction for alignment` (false),
  `automatic rt correction reference file id` (-1, automatic),
  `automatic rt correction rt bin width` (0.5 min),
  `automatic rt correction match rt tolerance` (0.5 min),
  `automatic rt correction minimum anchors` (3),
  `automatic rt correction maximum anchors` (6),
  `automatic rt correction minimum sample coverage` (0.5),
  `automatic rt correction intensity quantile` (0.75),
  `automatic rt correction maximum peak width quantile` (0.5),
  `automatic rt correction minimum signal to noise` (3),
  `automatic rt correction minimum gaussian similarity` (0),
  `automatic rt correction minimum ideal slope` (0),
  `automatic rt correction outlier mad threshold` (3.5),
  `automatic rt correction reference centrality weight` (0.35), and
  `automatic rt correction interpolate blanks by analytical order` (true).
  Validation requires LC-MS, alignment enabled and no simultaneous user-defined RT
  correction, and refuses every value the Console would not use as written: a
  value that is not a finite number in the invariant-culture spelling the Console
  parses (so not `True`, `1_000` or a full-width digit); an anchor count or
  reference file ID that is
  fractional or outside 32 bits; a reference file ID other than -1 (automatic) or
  the row of a non-Blank file in the file list; fewer than two minimum anchors or
  a maximum below the minimum; an RT bin width or match tolerance that is not above
  0 in the single precision the Console stores it in; an alignment MS1 tolerance
  that is not above 0, which the correction's anchor matching refuses; a
  negative outlier MAD threshold (0 turns outlier rejection off, as the Console
  reads it) or minimum signal-to-noise; and quantiles, coverage, centrality
  weight, minimum Gaussian similarity and minimum ideal slope outside [0,1]. A
  fractional count is refused rather than truncated, from the UI, the agent and
  a parameter template alike: the Console reads these as whole numbers and keeps
  its default otherwise. The template reader, the validator, the agent answers
  and the method writer share one set of defaults, so an absent value is
  validated as the value that will be written, and the writer writes each
  setting as the parsed value: surrounding whitespace or a line break cannot split
  the method line, and the string `false` for Blank interpolation is written as
  False, not True. The UI and agent expose these settings, and a blank UI field takes its
  default. The zero-threshold peak diagnostic turns automatic RT correction and
  alignment light mode off, since it runs without alignment.
  Methods and Table S1 describe correction only when the Console summary and
  anchor audit TSVs plus `method.keys.json` prove it ran for this run: the
  method-key record must carry the hash of the run's `method.txt`, both TSVs must
  be no older than that record, and at least one file other than the reference
  must have been corrected from its own detected anchors; a correction setting
  the Console recorded as unusable or blank, and so replaced by its default, is
  not proof.
  The Methods text names the reference file, which defines the axis and keeps its
  measured RTs, and says of the other files how many were corrected from their
  own anchors, how many Blanks took an interpolated or nearest-sample model and
  how many kept their original RTs. It says that aligned feature RTs, mzTab-M
  included, are on the reference file's axis only when no file was left
  uncorrected. Otherwise it says how each exported value is made: the aligned RT
  (mzTab-M `retention_time_in_seconds`) is the mean of the contributing apex RTs,
  on the reference axis only where no uncorrected file contributes, and start and
  end are single apex RTs, either of which can be measured; the report warns.
  Per-file peak lists and annotation keep measured RTs. With the feature
  off, Table S1 lists none of its settings; `Supplementary_Table_MS_DIAL.tsv`
  still records every workflow key. Repository runs warn when Blank models would
  be interpolated along an analytical order inferred from file names or the file
  listing, if the run has a Blank. The Console is recognised as implementing the
  feature by the method key its parser reads or the audit line it writes. Both
  are in the Console assembly: `MSDIALCUI.exe` for net48, and for net8 the
  `MSDIALCUI.dll` beside the launcher (`MSDIALCUI.exe`, or `MSDIALCUI` on Linux
  and macOS), which is read only when the file is a launcher: a
  `MSDIALCUI.runtimeconfig.json` beside it, and no CLI header in its own image. A
  net48 exe is a managed assembly, so a stale net8 dll beside it lends it
  nothing, even after a net48 archive is unpacked over a net8 folder. The
  title-case field label is in `MsdialCore.dll` and is not accepted. This requires a Console
  built from the feature branch; it is not yet on MsdialWorkbench master.
- Agent-guided runs keep `sample_table_proposal` in the workflow state: how the
  analytical order and dilution factors were proposed, with the alternatives. It
  is listed in `Supplementary_Table_MS_DIAL.tsv`, which records every workflow
  key, and not in the Table S1 workbook, because it is a proposal, not a setting
  the run used.

### Changed
- Raw-header preflight checks all inputs by default; capped inspection is marked
  partial and remains under review. Mixed acquisition modes or contradictory
  headers block execution; DIA with missing file types also blocks. (#26)
- Split parts are judged from their own files and record retained Class coverage.
  (#28, #30)
- A repository unit matches analysis inputs by its sample file names; stem matching
  is allowed only when the listed name has no extension. mzML is accepted. (#25)
- `.mzXML`, `.mzData`, and `.mzData.xml` are `requires_conversion`: they are excluded
  before download with msconvert guidance. File picker, CSV import, and path
  expansion reject them. This corrects the earlier mzXML acceptance in #25.
- MCP restart now reports that it does not reload changed Python source and tells
  callers to reconnect the MCP process. (#29)
- Unknown raw-retention policies resolve to keep and produce a warning. (#23)

### Fixed
- The reproduction scripts in a run bundle (`run-msdial.ps1`, `run-msdial.sh`)
  read the run's own `method.txt`. The Console writes `method.keys.json` beside
  the method file it reads, so a reproduction overwrote the original run's key
  record, on which its automatic RT-correction evidence rests. The scripts now
  run from a byte copy, `method.reproduce.txt`, beside `method.txt`, so relative
  paths and the file's encoding are kept and the key record is
  `method.reproduce.keys.json`; and from a copy of `analysis_files.csv` in
  `reproduced-results`, so the Console's project folder, the CSV's directory,
  leaves the run too. The Console runs from the bundle directory, so a relative
  path in `method.txt` is read against it in every mode (the LC-MS Console reads
  library paths against its working directory, GC-MS against the method file's).
  A Console path the caller gives keeps its meaning: an absolute path is used as
  given, a relative one is the caller's whether or not it exists, and a bare name
  is a file in the caller's directory or a command on PATH.
  A copy that fails stops the script before the Console starts, a read-only
  record does not stop a second reproduction, and a Console that cannot be
  started exits non-zero instead of 0. The
  scripts pass `-p` only when the run did, `run-msdial.ps1` is written with a
  BOM so Windows PowerShell 5.1 reads a non-ASCII Console path, and
  `run-msdial.sh` no longer needs bash 4. `REPRODUCE.txt` says which paths are
  absolute, including the MSP/Text annotator settings that name the run
  directory, and that per-file and alignment intermediates still land beside the
  inputs. A reproduction's mzTab-M under `reproduced-results` is no longer
  discovered as the run's own result, which, being newest, it had become.
- The injection-order proposal said Blanks and QCs "keep the place the listing
  gave them"; the order it proposes places them after every sample. The reason
  now says so, and names a Blank or QC that carries a number in the sequence
  token, since its place after the samples then contradicts that number.
- The Blank-interpolation warning reads the file type as the Console does, which
  also accepts the enum number (Blank is 3).
- Integer parameters are written as integers, so the Console can parse them. (#23)
- Agent-driven runs retain mzTab-M validation, artifact inventory, and the raw
  retention verdict. (#23)
- Truncated downloads fail while preserving their `.part`; archive-only units do
  not lose their input allow-list. (#24, #25)
- Conversion-required entries do not appear as eligible analysis inputs. A
  real-shape ST003038 fixture (parallel mzML and mzXML archives, `.mzXML` sample
  names) checks that the unit is excluded with the mzXML reason, and a split of a
  unit with `.mzXML` sample names keeps every part excluded for the same reason.

### Known limitations
- Runs record the package version (`msdial_interactive_version`) but not the
  Interactive commit, and this release does not change that. Runs from 2026-09-03
  until this release all record 0.4.7 across different code states, so the version
  alone does not identify the code that produced them.
- Metabolomics Workbench study archives can fail checksum validation after a full
  download. Units with parallel mzML and mzXML archives but `.mzXML` sample names
  remain excluded even when an mzML encoding exists.
- The mzXML exclusion is unit-wide: one file listed with role `requires_conversion`,
  or one sample naming an `.mzXML`/`.mzData` file, excludes the whole unit, even
  when its other inputs are vendor raw files or mzML that MS-DIAL can read.
- On a net8 launcher the capability probe reads `MSDIALCUI.dll`, but the run
  manifest still hashes only the launcher, which differs between builds only in
  its version string.
- The automatic RT-correction evidence compares file modification times, so it
  is read in the run directory the Console wrote. A copy that does not keep
  modification times reads as not proven (a warning, never a false claim).
- Console source builds need an explicit provenance check; the MCP backend is still
  embedded in its process and must be reconnected to load new code.
- Run-start provenance, instrument family from headers, analytical-order timestamps,
  split-parent raw cleanup, and complete method-key accounting remain future work.

## [0.4.7] - 2026-09-07

This version label appeared across more than one code state before #22 merged.

### Added
- Repository reanalysis automation, tiered LC-MS annotation, Console source-build
  inspection, and release-channel selection. (86c4da6; reached main with #22)
- Confirmed raw cleanup after preview; the server no longer deletes raw data
  automatically after a run. (#12)
- Positive/negative lipidome merge, Class proposals, opt-in input staging, and
  saved run worksets. (#21)

### Changed
- Approved Catalog Class assignments apply directly. (#15)
- MCP errors expose backend detail, HTTP status, and endpoint. (#17)
- Download limits use decimal GB and are checked against the handoff. (#18)
- Console provenance distinguishes verified, absent, stale, and unreadable;
  unsupported raw formats are separate from an unavailable preflight. (#19, #20)
- Run manifests retain Console and library checksums and a suggested per-file
  minimum peak height; Class grouping needs confirmation. (#21)

### Fixed
- `execution_allowed` is enforced at every Console start, and diagnostics do not
  write into production outputs. (#16)
- Production runs check expected exports; uncomputed annotation scores accept
  both Console notations. (#17, #11)
- GC-MS method keys use Console names for RI alignment tolerance and MSP cutoffs.
  (#9, merged with #22)

Earlier changes are recorded in Git history.
