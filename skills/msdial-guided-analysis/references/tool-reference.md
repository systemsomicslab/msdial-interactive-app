# MCP Tool Reference

## Guided answers

Common keys:

```json
{
  "project_type": "lcms",
  "ion_mode": "Negative",
  "target_omics": "Lipidomics",
  "parameter_strategy": "default",
  "target_peak_count": 10000,
  "minimum_peak_height": 300,
  "acquisition_type": "DDA",
  "execute_rt_correction": false,
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

`console_path` accepts an absolute path to `MSDIALCUI.exe` or `MSDIALCUI.dll`.
Use `msdial_check_console_path` before asking the user to locate it manually, and
`msdial_set_console_path` to persist an accepted path. Unknown answer keys are
reported in `warnings` and `unknown_answer_keys`; they are never silently applied.

Use `parameter_strategy: "target_peak_count"` only with `target_peak_count`. Add `minimum_peak_height` after diagnostic review.

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

Do not set `confirmed=true` until the user approves the corresponding action in the current conversation.
