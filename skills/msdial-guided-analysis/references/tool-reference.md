# MCP Tool Reference

## General interactive utilities

- `msdial_interactive_status`: report backend compatibility, application/API
  versions, capabilities, and compact job status.
- `msdial_interactive_launch`: start the local backend when it is not already
  running. Starting a process does not reload an already-running MCP server.
- `msdial_interactive_open`: open the local browser UI.
- `msdial_list_worksets`: list built-in and user-saved reusable scientific
  settings.
- `msdial_save_workset`: save reviewed answers and workflow overrides without
  input or output paths.
- `msdial_prepare_guided_analysis`: write the reviewed CSV, method, manifest,
  command, and reproduction bundle without launching MS-DIAL.
- `msdial_estimate_peak_height`: summarize a completed zero-threshold diagnostic
  and propose a stepped minimum peak height for review.
- `msdial_interactive_job`: retrieve one job's current status and optional full
  details.
- `msdial_interactive_create_handoff`: create a data-mining handoff from one
  completed production job.

## Guided answers

Common keys:

```json
{
  "project_type": "lcms",
  "ion_mode": "Negative",
  "target_omics": "Lipidomics",
  "parameter_strategy": "auto_peak_range",
  "target_peak_count_min": 3000,
  "target_peak_count_max": 6000,
  "smoothing_method": "TimeBasedLinearWeightedMovingAverage",
  "acquisition_type": "DDA",
  "execute_rt_correction": false,
  "execute_automatic_rt_correction": true,
  "automatic_rt_correction_reference_file_id": -1,
  "automatic_rt_correction_minimum_anchors": 3,
  "automatic_rt_correction_maximum_anchors": 6,
  "automatic_rt_correction_minimum_sample_coverage": 0.5,
  "rt_correction_anchor_path": "D:/anchors.txt",
  "rt_correction_peak_selection_mode": "HighestIntensity",
  "library_strategy": "official",
  "run_qa": true,
  "internal_standards": [],
  "generate_materials_methods": true,
  "alignment_light_mode": false,
  "console_path": "D:/path/to/MSDIALCUI.exe",
  "export_folder_path": "D:/analysis-output",
  "output_root": "D:/analysis-output"
}
```

`execute_rt_correction` and `execute_automatic_rt_correction` are mutually
exclusive. The former warps the data-point RT axis before peak detection using
reviewed user anchors. The latter selects anchors after peak detection and
annotation and applies a piecewise-linear map only during alignment. `-1`
selects the reference file automatically. The Console retains
`automatic_alignment_rt_correction_summary.tsv` and
`automatic_alignment_rt_correction_anchors.tsv` as audit evidence.

`console_path` accepts an absolute path to `MSDIALCUI.exe` or `MSDIALCUI.dll`.
Use `msdial_check_console_path` before asking the user to locate it manually, and
`msdial_set_console_path` to persist an accepted path. Unknown answer keys are
reported in `warnings` and `unknown_answer_keys`; they are never silently applied.

Use `msdial_check_official_console_releases` when release recency matters. For a
developer source tree, use `msdial_check_local_console_source` to fetch and
compare the current checkout with `origin/master` without changing the working
tree. Call `msdial_build_console_from_local_source` first with
`confirmed=false` to show the Git commit, dirty status, command, and output.
Call it again with `confirmed=true` only after explicit approval.

Use `parameter_strategy: "auto_peak_range"` for the 3,000-6,000 stepped
repository workflow, or `"target_peak_count"` with an exact
`target_peak_count`. Add `minimum_peak_height` after diagnostic review; zero is
a valid accepted result.

When `run_qa` is true, the planner enables `Height matrix export` and sets
`Export folder path` to `output_root` unless `export_folder_path` is supplied.
The production job records only files created or updated during that run.

For existing libraries:

```json
{
  "library_strategy": "existing",
  "libraries": {
    "msp_paths": ["D:/libraries/public.msp"],
    "text_paths": ["D:/libraries/standards.txt"],
    "lbm_path": "D:/libraries/lipids.lbm2"
  },
  "library_provenance": [
    {
      "path": "D:/libraries/public.msp",
      "version": "2026-08",
      "doi": "10.xxxx/example",
      "source": "https://example.org/library",
      "license": "CC BY 4.0"
    }
  ]
}
```

For the tiered LC-MS pipeline, provide one MSP path. The official versioned LBM
catalog entry is used for the lipid-rule tier:

```json
{
  "library_strategy": "tiered_lipid_msp",
  "libraries": {
    "msp_paths": ["D:/libraries/private-neg-vs20.msp"],
    "msp_version": "VS20",
    "msp_source": "institutional library",
    "msp_license": "institutional/private"
  }
}
```

The generated cascade is:

| Tier | Annotator ID | Priority | Mode | MS/MS tol. | Weighted/simple/reverse | Matched peaks |
|---|---|---:|---|---:|---|---:|
| Lipid rules | LBM path | 3 | Lipidomics | LBM setting | LBM setting | LBM setting |
| MSP high | `msp_high_quality` | 2 | Metabolomics | 0.05 Da | 0.6 / 0.6 / 0.8 | >= 3 |
| MSP low candidate | `msp_low_quality` | 1 | Metabolomics | 0.25 Da | 0.5 / 0.5 / 0.5 | >= 1 |

A lower-priority MS/MS reference match outranks a higher-priority precursor-m/z-only
suggestion. If no tier yields an MS/MS match, MS-DIAL retains the highest-priority
suggestion. Both MSP rows set matched-peak percentage to 0 and disable RT scoring/filtering.
MS-DIAL loads the shared MSP once and evaluates both settings. The low tier is
an intentionally broad candidate search and requires downstream evidence review.

For GC-MS RI analysis, add `gcms_retention_type: "RI"`, `gcms_ri_compound_type: "Alkanes"` or `"Fames"`, and an RI standard/dictionary path.

## Internal standards

LC-MS internal standards use objects such as:

```json
{
  "name": "FA 16:0",
  "adduct": "[M-H]-",
  "mz": 255.2330,
  "rt": 2.107,
  "mz_tolerance": 0.01,
  "rt_tolerance": 0.05
}
```

If no true internal standard is present, label any endogenous substitute as a pseudo internal standard in the report.

## Job-scoped post-processing

Pass the production `job_id` to:

- `msdial_interactive_wait_for_completion`
- `msdial_interactive_validate_mztab`
- `msdial_interactive_preview_mztab`
- `msdial_generate_lcms_qa`
- `msdial_generate_publication_report`
- `msdial_complete_guided_analysis`

Do not substitute `run_directory` for `job_id` in an automated workflow. A
directory can contain outputs from unrelated runs. Set `run_qa: false` on
`msdial_generate_publication_report` when Materials and Methods and supplementary
tables are needed without a QA matrix.

## Repository metadata

Repository identifiers accepted by `msdial_inspect_repository_metadata`:

- `metabolomics_workbench` with `ST...`
- `metabolights` with `MTBLS...`
- `mb_post` with `MPST...`

Its `workspace.fields` entries contain `name`, `non_missing`, `missing`,
`unique_count`, and `examples`. Its `workspace.rows` entries preserve
`sample_id`, `source_name`, `raw_file`, and the original `values` object.

Pass the complete workspace and an ordered field-name list to
`msdial_project_repository_classes`. Example:

```json
{
  "hierarchy": ["Genotype", "Region", "Sex"],
  "missing_value": "NA",
  "separator": "_"
}
```

The returned `application` reports `matched_count`, `unmatched`, and
`ambiguous`, and contains analysis-file rows with projected `class_id` values.
Use `msdial_save_repository_metadata` to persist the projected workspace and
those rows before preparing the production analysis.

For an accession-to-mzTab-M workflow, prefer the higher-level repository tools:

- `msdial_repository_batch_plan`: validate Catalog `handoff_path` values and
  expand mixed accessions into independent analysis-unit workspaces. Prefer
  `analysis_unit_handoff_paths` over inlining file/sample manifests. It never
  downloads or executes data. Pass the reviewed `analysis_purpose`; omission is
  reported as a pending decision.
- `msdial_repository_reanalysis_plan`: inspect metadata, eligibility, the
  default Class hierarchy, and internal-standard declarations without
  downloading raw data. Pass `analysis_unit_handoff_path` for Catalog-driven
  runs and `analysis_purpose` so later choices share one stated goal.
- `msdial_download_repository_raw`: preview with `confirmed=false`, then start a
  bounded download with `confirmed=true` after the user approves destination,
  accession, analysis unit, actual bundle size, size limit, and retention policy.
  Reuse the exact handoff path passed to the planner. Download remains blocked
  until both the Class proposal and `analysis_purpose` are present.
- `msdial_repository_raw_metadata_preflight`: run the local RawMetadataConsoleApp
  against every downloaded analysis input by default. A caller may request a cap,
  but capped coverage remains partial and cannot establish production readiness.
- `msdial_split_repository_unit`: preview and, after explicit confirmation,
  split a raw-preflight result whose acquisition mode is `Mixed`. Never execute
  the parent unit; preflight and approve each generated child independently.
- `msdial_prepare_repository_reanalysis`: preview Class matching first, then
  write reviewed metadata and `analysis_files.csv` after confirmation. Its
  `preview.answer_seed` is passed unchanged to `msdial_guided_analysis_plan`.
  The seed points to the complete reviewed metadata JSON locally rather than
  carrying every sample row through the model context.
- `msdial_repository_qa_evidence`: return internal-standard declarations for a
  desktop-agent draft. It does not claim that a name, adduct, m/z, or RT has
  been experimentally confirmed.
- `msdial_cleanup_repository_raw`: preview the exact deletion and retained
  artifacts first, then delete leased raw data only after mzTab-M validation,
  artifact inventory, and a separate explicit confirmation. A retention policy
  is not deletion approval.

MS-DIAL accepts mzML, not mzXML or mzData. Files reported as
`requires_conversion` must be converted to mzML outside the run and entered
through a new reviewed manifest; do not relabel them as `converted`.

`allow_partial_mapping=true` and raw-data cleanup are explicit user decisions;
do not infer either from the absence of an error.

## Worksets

Built-in IDs:

- `gcms-metabolomics`
- `lcms-positive-metabolomics`
- `lcms-negative-metabolomics`
- `lcms-positive-lipidomics`
- `lcms-negative-lipidomics`

User worksets retain scientific `answers`, including an accepted diagnostic threshold, and optional `workflow_overrides`. They intentionally omit `input_path` and `output_root`.

## Confirmation boundaries

These tools return a preview when `confirmed=false`:

- `msdial_interactive_restart`
- `msdial_download_official_library`
- `msdial_start_peak_count_diagnostic`
- `msdial_start_guided_analysis`
- `msdial_download_repository_raw`
- `msdial_prepare_repository_reanalysis`
- `msdial_split_repository_unit`
- `msdial_cleanup_repository_raw`

Do not set `confirmed=true` until the user approves the corresponding action in the current conversation.
