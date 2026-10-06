# Public Repository Reanalysis

Use this workflow when the user supplies a repository accession and wants an
agent-only MS-DIAL reanalysis. The web UI is optional.

The current public-repository campaign accepts untargeted LC-MS/MS data acquired
by DDA or DIA/AIF/SWATH. The wider Interactive application still supports
additional LC-MS and GC-MS workflows, but agents must not expand this campaign
to GC-MS without a separately approved policy change.

MS-DIAL reads mzML but does not provide an mzXML or mzData reader. Treat
`.mzXML`, `.mzData`, and `.mzData.xml` as `requires_conversion`: do not download
or queue them as MS-DIAL inputs. Convert them to centroided mzML with a reviewed
ProteoWizard `msconvert` workflow, then create a new auditable input manifest.

Only in a campaign, where a campaign authorization covers the unit, is an mzXML
converted in the download lease: its `convert` stage writes each of the unit's
mzXML as mzML under `raw\converted` with Interactive's converter, every
inference flag off but the polarity of scans that record none, which is the
unit's declared ion mode where its Catalog handoff declares exactly one
polarity (Positive or Negative; never `project.ion_mode`, and nothing for Both
or Unknown), records each conversion (`input_conversions`,
`provenance\input-conversions.json`, and `input_lineage` rows of kind
`converted`), and the converted mzML are the unit's inputs. A file whose
conversion fails is excluded with reason `conversion_failed`, and a file some
of whose scans record the other polarity and some none, unconverted, with
reason `polarity_contradicts_declaration`; each is listed in the unit's
`campaign_disposition`, and the rest run. A file whose every scan records the
other polarity is converted as recorded, and the unit splits by polarity. Do
not convert mzXML by hand or relabel it as `converted`. mzData, which nothing
converts, still excludes its unit.

Before selecting an analysis unit, ask what the user wants to learn. Capture the
scientific question and comparison, whether annotation or comparative profiling
is central, and required outputs as `analysis_purpose`. Pass it to batch, plan,
and download tools. Missing purpose is an explicit download blocker.

## Supported repositories

- `metabolomics_workbench`: `ST...`
- `metabolights`: `MTBLS...`
- `mb_post`: `MPST...`
- `metabobank`: `MTBKS...`

## Plan and download

1. Query the local Catalog by repository and accession. When an accession has
   more than one analysis unit, review every unit and never send the accession
   directly to an MS-DIAL run.
2. For each selected unit, obtain `msdial_catalog_reanalysis_handoff`. It writes
   the complete handoff locally and returns a bounded summary with
   `handoff_path`. Do not copy the external file/sample manifests into chat.
3. For multiple units, call `msdial_repository_batch_plan` with
   `analysis_unit_handoff_paths`. It reloads and validates the complete local
   handoffs, then creates independent run plans and output workspaces. Do not
   merge units, polarities, separation modes, or acquisition modes into one run.
4. Call `msdial_repository_reanalysis_plan` with the matching
   `analysis_unit_handoff_path`. Report the title, publication, sample/file
   counts, both selected-unit bytes and required bundle bytes, LC-MS/MS status,
   polarity, acquisition mode, target omics, eligibility, warnings, default
   Class hierarchy, and internal-standard metadata evidence.
5. Ask the user to review uncertain scientific fields. Do not infer a missing
   value from the accession or filename alone. A Catalog handoff without a
   saved Class proposal remains blocked; preview and save that proposal only
   after explicit confirmation, then regenerate the handoff.
6. Record whether the user intends to keep raw data or consider cleanup after a
   successful run. This retention preference is not permission to delete data.
7. Call `msdial_download_repository_raw` with the same
   `analysis_unit_handoff_path` and `confirmed=false`. Show the local destination,
   size bound, accession, unit ID, required bundle bytes, and retention policy. Call it again with
   `confirmed=true` only after approval. For an accession-bundle URL,
   Interactive must admit only handoff-listed paths into the analysis manifest.
8. Poll the returned download job with `msdial_interactive_job`. Report bytes,
   percentage, speed, ETA, and current object until it completes.

## Raw metadata and Class

After download, call `msdial_repository_raw_metadata_preflight` when repository
metadata leaves LC-MS/MS, polarity, DDA versus DIA/AIF/SWATH, MS2 status, or
untargeted status uncertain, or when the user requests a raw-header cross-check. The tool uses
the explicit extractor path, then the `raw_metadata_extractor_path` setting, then
`MSDIAL_RAW_METADATA_EXTRACTOR`, then the sibling `msrawdataworkbench/RawMetadataConsoleApp`
build (reported as `working_checkout_default`); `msdial_check_raw_metadata_extractor` shows which
one and whether its build record verifies. Set `confirm_untargeted=true` only after the user has
explicitly accepted that scientific classification; it is recorded as an inference, not a
confirmation.

The reply's `campaign_disposition` says what a campaign would do with the unit (run, skip, exclude
or split, with reason codes). Outside a campaign it is advice only: report it, and keep every
confirmation below.

If preflight reports `acquisition_mode=Mixed`, call
`msdial_split_repository_unit` with `confirmed=false` and review every proposed
child unit. Call it with `confirmed=true` only after the user accepts the split.
Never run the Mixed parent. Run raw-metadata preflight independently for every
child, and prepare/start only children whose DDA or DIA/AIF/SWATH mode is
resolved and scientifically accepted.

Call `msdial_prepare_repository_reanalysis` with `confirmed=false`. Review:

- the ordered metadata fields joined into MS-DIAL `Class`;
- matched, unmatched, and ambiguous raw-file names;
- inferred file type, DDA/DIA/AIF/SWATH status, batch, and analytical order;
- the generated LC-MS/MS, polarity, and target-omics answer seed.

Continuous subject-level fields such as age or BMI can create one Class per
sample. Do not include them merely because they exist. After the user accepts
the hierarchy, call the tool with `confirmed=true`. Do not set
`allow_partial_mapping=true` unless the user explicitly accepts unmatched or
ambiguous files. The result contains `input_path` for `analysis_files.csv` and
an `answer_seed` that preserves repository provenance and the raw-data retention
policy. Complete repository rows stay in the local reviewed JSON and are passed
to the workflow by `repository_metadata_path`; do not copy thousands of sample
rows into the conversation.

## Complete scientific choices

Pass `input_path` and `answer_seed` to `msdial_guided_analysis_plan`. Continue
through its remaining questions. In particular, confirm peak-picking strategy,
RT correction, annotation libraries, QA, and publication output. Do not let an
inferred value remove a question when its evidence is weak or contradictory.

The repository answer seed uses `auto_peak_range`. Run the zero-threshold
single-file diagnostic on a QC nearest the run midpoint, or a non-blank sample
nearest the midpoint when no QC is available. Estimate a threshold retaining
3,000-6,000 peaks, constrained to 100-unit steps for QTOF-type data or
1,000-unit steps for Fourier-transform data. Only when no such step lands in
range, fall back to 10-unit (QTOF-type) or 100-unit (Fourier-transform) steps,
and never finer (the user's decision of 2026-10-06). The fallback is recorded as
`step_fallback` and `fallback_reason`, and a threshold that misses the range even
at the fine step as `within_target_range: false` with a warning. Keep 0 when the
diagnostic finds no more than 6,000 peaks. Add the accepted threshold to `answer_seed` before
production. Repository runs use `TimeBasedLinearWeightedMovingAverage` and must
record it in the generated method and provenance.

For QA, call `msdial_repository_qa_evidence`. A commercial mixture declaration
such as EquiSPLASH is evidence, not a complete target table. The desktop agent
may draft name/adduct/m/z/tolerances from that evidence, but must:

- distinguish repository facts from its scientific inference;
- label candidates as requiring review;
- leave RT null unless repository metadata records it;
- avoid claiming internal-standard stability when no accepted target exists.

Pass only accepted candidates as `internal_standards` to
`msdial_complete_guided_analysis`.

## Run and retain

Prepare and start the production analysis with the normal guided tools and their
confirmation boundary. Then call `msdial_complete_guided_analysis` with the
exact production `job_id`. It waits, validates and previews mzTab-M, generates
requested LC-MS QA and publication files, and creates the data-mining handoff.

Retain repository/publication metadata, reviewed sample metadata,
`analysis_files.csv`, parameters, mzTab-M, `mdpeak`/`mdscan`, `mdmsp`, `mdalign`,
QA, publication artifacts, and the ZIP of `dcl`/`arf` project files. When the
retention policy requests cleanup, raw data may be deleted only after mzTab-M
validation succeeds and the retained-artifact inventory is complete. Then call
`msdial_cleanup_repository_raw` with `confirmed=false`, show the exact workspace,
raw directory, manifest, and retained-artifact inventory, and obtain a separate
deletion confirmation before calling it with `confirmed=true`. A previously
accepted retention policy never substitutes for this deletion approval.

## Example request

For `MB-POST MPST000007`, begin with:

```text
msdial_repository_reanalysis_plan(
  repository="mb_post",
  accession="MPST000007",
  workspace_root="D:/13_MSDIAL_Public_Reanalysis/analysis",
  analysis_unit_handoff_path=<handoff_path returned by msdial_catalog_reanalysis_handoff>,
  raw_retention_policy="keep",
  analysis_purpose="<reviewed scientific purpose>"
)
```

Do not bake the pilot's historical conclusions into other accessions. For this
record too, report current repository and raw-header evidence before accepting
negative DDA LC-MS lipidomics or any Class hierarchy.
