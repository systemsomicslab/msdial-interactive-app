# Changelog

Notable changes to MS-DIAL Interactive. The package version is kept in
`pyproject.toml` and `msdial_app/__init__.py`; the Agent API version is separate.
Agent API 0.5 requires the repository split endpoint introduced after API 0.4.

## [0.5.2] - Unreleased

### Changed
- A repository unit's analytical order is the order its files were acquired, as
  each raw header records it, whenever every input has a readable acquisition
  start time. The raw-metadata preflight already read that time (mzML
  `run@startTimeStamp`, vendor headers) and the summary dropped it; the order
  was the file listing, or a number read out of the names, and a repository
  sample table's declared order overwrote either. The header order is applied
  last, so it outranks both; a unit where any file lacks a time keeps the old
  order and the record says which files lack one. Equal times keep listing
  order and are named, and timezone-aware and naive times are not mixed.
  `msdial_prepare_repository_reanalysis` shows the order in its preview, writes
  it into `analysis_files.csv`, and records how it was decided, with every
  file's start time, as `analytical_order` in the unit's run manifest. A
  preflight summarised before this release is read through the extractor
  output it names. The guided plan then reports the order as
  `raw_header_acquisition_start_time`, and whether the analysis CSV still
  carries it. Found on MTBLS2207, where the listing put a December 2019
  acquisition last, and the only QA criterion the run could evaluate was a
  run-order drift computed against that listing.

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
