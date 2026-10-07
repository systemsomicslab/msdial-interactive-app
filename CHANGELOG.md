# Changelog

Notable changes to MS-DIAL Interactive. The package version is kept in
`pyproject.toml` and `msdial_app/__init__.py`; the Agent API version is separate.
Agent API 0.5 requires the repository split endpoint introduced after API 0.4.

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
