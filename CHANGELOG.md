# Changelog

Notable changes to MS-DIAL Interactive. The package version is kept in
`pyproject.toml` and `msdial_app/__init__.py`; the Agent API version is separate.
Agent API 0.5 requires the repository split endpoint introduced after API 0.4.

## [0.5.0] - Unreleased

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
  Validation requires LC-MS, alignment enabled, no simultaneous user-defined RT
  correction, at least two minimum anchors, a maximum not below the minimum,
  positive RT bin width/match tolerance/MAD threshold, and quantiles, coverage,
  and centrality weight within [0,1]. The template reader, the validator and the
  method writer share one set of defaults, so an absent value is validated as the
  value that will be written. The UI and agent expose these settings, and a blank
  UI field takes its default. The zero-threshold peak diagnostic turns automatic
  RT correction and alignment light mode off, since it runs without alignment.
  Methods and Table S1 describe correction only when the Console summary and
  anchor audit TSVs plus `method.keys.json` prove it ran for this run: the
  method-key record must carry the hash of the run's `method.txt`, both TSVs must
  be no older than that record, and at least one file other than the reference
  must have been corrected from its own detected anchors. With the feature off,
  Table S1 lists none of its settings. Repository runs warn when Blank models would
  be interpolated along an analytical order inferred from file names or the file
  listing. The Console is recognised as implementing the feature by the method key
  its parser reads or the audit line it writes, both in `MSDIALCUI.exe` itself;
  the title-case field label is in `MsdialCore.dll` and is not accepted. This
  requires a Console built from the feature branch; it is not yet on
  MsdialWorkbench master.
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
