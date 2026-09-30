# Changelog

Notable changes to MS-DIAL Interactive. The package version is kept in
`pyproject.toml` and `msdial_app/__init__.py`; the Agent API version is separate.
Agent API 0.5 requires the repository split endpoint introduced after API 0.4.

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
