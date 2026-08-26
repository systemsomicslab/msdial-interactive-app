# Public Repository Reanalysis

Use this workflow when the user supplies a repository accession and wants an
agent-only MS-DIAL reanalysis. The web UI is optional.

## Supported repositories

- `metabolomics_workbench`: `ST...`
- `metabolights`: `MTBLS...`
- `mb_post`: `MPST...`
- `metabobank`: `MTBKS...`

## Plan and download

1. Call `msdial_repository_reanalysis_plan`. Report the title, publication,
   sample/file counts, declared bytes, LC-MS/GC-MS, polarity, acquisition mode,
   target omics, eligibility, warnings, default Class hierarchy, and internal-
   standard metadata evidence.
2. Ask the user to review uncertain scientific fields. Do not infer a missing
   value from the accession or filename alone.
3. Ask whether raw data should be kept or deleted only after a successful run
   and validated mzTab-M output.
4. Call `msdial_download_repository_raw` with `confirmed=false`. Show the local
   destination, size bound, accession, and retention policy. Call it again with
   `confirmed=true` only after approval.
5. Poll the returned download job with `msdial_interactive_job`. Report bytes,
   percentage, speed, ETA, and current object until it completes.

## Raw metadata and Class

After download, call `msdial_repository_raw_metadata_preflight` when repository
metadata leaves LC-MS/GC-MS, polarity, acquisition mode, or untargeted status
uncertain, or when the user requests a raw-header cross-check. The tool can find
the sibling `msrawdataworkbench/RawMetadataConsoleApp` build or accept an
explicit extractor path. Set `confirm_untargeted=true` only after the user has
explicitly accepted that scientific classification.

Call `msdial_prepare_repository_reanalysis` with `confirmed=false`. Review:

- the ordered metadata fields joined into MS-DIAL `Class`;
- matched, unmatched, and ambiguous raw-file names;
- inferred file type, DDA/SWATH/AIF, batch, and analytical order;
- the generated LC-MS/GC-MS, polarity, and target-omics answer seed.

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
validation succeeds and the retained-artifact inventory is complete.

## Example request

For `MB-POST MPST000007`, begin with:

```text
msdial_repository_reanalysis_plan(
  repository="mb_post",
  accession="MPST000007",
  workspace_root="D:/MSDIAL_Public_Reanalysis",
  raw_retention_policy="keep"
)
```

Do not bake the pilot's historical conclusions into other accessions. For this
record too, report current repository and raw-header evidence before accepting
negative DDA LC-MS lipidomics or any Class hierarchy.
