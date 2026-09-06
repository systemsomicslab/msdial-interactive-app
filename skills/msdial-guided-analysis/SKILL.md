---
name: msdial-guided-analysis
description: Guide and execute reproducible local MS-DIAL analyses from raw LC-MS or GC-MS data through mzTab-M validation, LC-MS quality assurance, Materials and Methods generation, and reusable workset registration. Use when a user asks to analyze a local mass-spectrometry folder or CSV, run MS-DIAL Interactive, tune peak counts, apply RT correction, choose annotation libraries, inspect QA, or prepare publication artifacts.
---

# MS-DIAL Guided Analysis

Use the `msdial-interactive` MCP tools as the execution layer. Keep raw data on the user's computer and treat the conversation as the scientific decision layer.

## Start

1. Call `msdial_interactive_status`.
2. Call `msdial_interactive_launch` when the local app is not running.
3. When `compatible` is false, call `msdial_interactive_restart` with `confirmed=false`, explain the detected and required API versions and recognized process, obtain confirmation, then call it with `confirmed=true`.
4. Call `msdial_check_console_path`. Use a discovered path or the persisted setting. If none is found, ask for a search root or explicit `MSDIALCUI.exe`/`MSDIALCUI.dll`, then call `msdial_set_console_path`.
5. Call `msdial_guided_analysis_plan` with the user's local input path and an empty `answers` object, or with a named `workset_id`.
6. Report the recognized file count, formats, rejected paths, warnings, and unknown answer keys before discussing parameters.

Keep `open_browser=false` unless the user asks to inspect or edit the web UI. The normal guided path is conversation plus MCP only; do not tell the user to click through the UI.

Do not infer GC-MS versus LC-MS, ion mode, or target omics from a filename alone. Ask when the user has not explicitly supplied the value. Explain that guided execution currently supports LC-MS and GC-MS; direct other project types to the web UI without pretending they are supported.

## Public Repository Reanalysis

When the user supplies a Metabolomics Workbench (`ST...`), MetaboLights
(`MTBLS...`), MB-POST (`MPST...`), or MetaboBank (`MTBKS...`) accession, use
the repository MCP workflow rather than asking the user to download files or
operate the browser UI. Read
[repository-reanalysis.md](references/repository-reanalysis.md) for the exact
tool sequence, confirmation boundaries, metadata review, raw-header cross-check,
QA target handling, and retained-artifact rules.

Never silently accept a projected Class hierarchy, partial filename mapping,
inferred internal-standard ion, or raw-data deletion policy. Repository and raw
metadata are evidence; unresolved scientific choices remain questions for the
user. Exact internal-standard candidates drafted by the desktop agent must be
reported as reviewable candidates, with RT left unset unless it is recorded.

## Collect Decisions

Follow `next_question` from `msdial_guided_analysis_plan`. Retain all accepted values in one `answers` object and call the planner again after each answer. Ask one scientific decision at a time unless the user explicitly requests a compact questionnaire.

Present every choice neutrally and in the order returned by the planner. Do not append evaluative labels or imply that the first choice is scientifically superior. Explain tradeoffs only when evidence or the user's stated goal supports them.

Use these branches:

1. Select `lcms` or `gcms`.
2. For LC-MS, collect `ion_mode` and `target_omics`.
3. Select template defaults, `auto_peak_range`, or an exact `target_peak_count`.
4. For LC-MS, decide whether to apply retention-time correction. If enabled, collect an anchor library and peak-selection rule.
5. Select official, existing, tiered lipid/MSP, or no annotation libraries.
6. For LC-MS, decide whether to generate QA and collect internal-standard definitions when available. QA without internal standards still evaluates distributions and sample topology, but must not claim internal-standard stability.
7. Decide whether to generate Materials and Methods and supplementary tables.

Load [tool-reference.md](references/tool-reference.md) when constructing nested `answers`, internal standards, library settings, or worksets.

## Tune Peak Count

When `parameter_strategy` is `auto_peak_range` or `target_peak_count`:

1. Call `msdial_start_peak_count_diagnostic` with `confirmed=false` and explain that one representative file will be processed.
2. Obtain explicit user confirmation.
3. Call it again with `confirmed=true`.
4. Poll the returned job with `msdial_interactive_job` until completed or failed.
5. For `auto_peak_range`, call `msdial_estimate_peak_height` without an exact
   target. The diagnostic automatically selects a mid-run QC, or a mid-run
   non-blank sample when no QC exists. It targets 3,000-6,000 retained peaks in
   100-unit threshold steps for QTOF-type data and 1,000-unit steps for
   Fourier-transform data. A diagnostic count at or below 6,000 keeps the
   threshold at 0. For an exact target, pass `target_peak_count`.
6. Present the proposed `minimum_peak_height`, diagnostic peak count, and estimated retained count.
7. Add the threshold to `answers` only after the user accepts it or supplies a replacement.

Treat the estimate as a reproducible starting point, not a biological quality
guarantee. Public repository reanalysis uses
`TimeBasedLinearWeightedMovingAverage`; retain that choice in the method and
provenance. It handles irregular scan intervals at a modest additional compute
cost.

## Resolve Libraries

For `official`, use the catalog ID returned by the planner or choose the matching versioned catalog entry:

- `metabolomics-positive`
- `metabolomics-negative`
- `lipidomics`
- `gcms-kovats`
- `gcms-fiehn`

Call `msdial_download_official_library` with `confirmed=false` first. State the record, DOI, download size, and local destination. Download only after explicit confirmation. For user libraries, preserve paths and provenance supplied by the user; prompt for a version, DOI/repository URL, or checksum before publication when none is recorded.

For an untargeted LC-MS repository workflow that should retain broad annotation evidence, `tiered_lipid_msp` applies the official LBM rule-based search and reuses one user-supplied MSP in two parameter tiers. A lower-priority MS/MS reference match outranks a higher-priority precursor-m/z-only suggestion. Among results with the same match status, the priorities are LBM 3, strict MSP 2, and broad MSP candidate 1. If no tier yields an MS/MS match, MS-DIAL may retain the highest-priority `no MS2` suggestion. The broad tier uses 0.25 Da MS/MS tolerance and is a tentative candidate tier; never describe it as equivalent to a high-quality match. Read [tool-reference.md](references/tool-reference.md) for the exact thresholds and nested `libraries` object. Download the official LBM only after the normal confirmation boundary, and never copy or redistribute a private MSP.

## Review And Run

Do not start a production analysis while `remaining_questions` or `blockers` are present.

1. Call `msdial_prepare_guided_analysis` to validate and write the reproducible CSV, method, manifest, scripts, and workflow bundle.
2. Summarize project type, ion mode, target omics, file count, output directory, peak-picking strategy, RT correction, libraries, alignment-light mode, QA, and publication actions.
3. Call `msdial_start_guided_analysis` with `confirmed=false`.
4. Ask the user to approve the displayed plan and command.
5. Call it with `confirmed=true` only after approval.
6. Poll the returned `job_id` with `msdial_interactive_job` or pass that exact ID to `msdial_interactive_wait_for_completion`. Never wait for an unspecified latest job.

Never silently overwrite the scientific meaning of an existing output folder. If generated files already exist, describe the collision and ask the user to choose another output directory or explicitly accept reuse.

## Complete The Workflow

After starting a production run, normally call `msdial_complete_guided_analysis` with the accepted QA and publication choices. It waits for that exact job and performs mzTab-M validation/preview, requested LC-MS QA, publication generation, and handoff without browser interaction.

If individual control is needed after a successful run:

1. Call `msdial_interactive_validate_mztab` with the completed `job_id` and report pass, warning, and failure counts.
2. Call `msdial_interactive_preview_mztab` with the same `job_id` for a compact content sanity check.
3. For requested LC-MS QA, call `msdial_generate_lcms_qa` with the same `job_id`. A missing job-owned QA matrix is an error; never search for an older matrix. Distinguish passed, failed, and not-evaluable checks.
4. When requested, call `msdial_generate_publication_report` with the same `job_id`. Set `run_qa=false` when publication files should be generated without QA. Return the Materials and Methods file, QA Results file, supplementary Excel workbook, audit JSON, and bundle paths.
5. Call `msdial_interactive_create_handoff` when downstream PCA, UMAP, HCA, chromatogram visualization, or other data-mining tools will consume the mzTab-M output.
6. Ask whether to save the accepted scientific choices as a workset. Call `msdial_save_workset` only when the user agrees and provides a name.

Do not store raw-data paths or output directories in reusable worksets. Preserve accepted peak-picking thresholds and other scientific choices; each run supplies its own input and output locations.

## Recover

On failure, read the job log tail before changing parameters. Preserve the failed workflow files for audit. Explain vendor sidecar/runtime errors, missing libraries, invalid paths, and mzTab-M validation failures separately. Do not retry a production run with changed scientific parameters without telling the user exactly what changed.
