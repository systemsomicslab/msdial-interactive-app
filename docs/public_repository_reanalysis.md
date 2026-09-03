# Public repository reanalysis

This experimental workflow prepares public untargeted LC-MS/MS DDA and
DIA/AIF/SWATH projects for
reproducible MS-DIAL reanalysis. The general Interactive application supports
additional workflows, but the current repository campaign is intentionally
narrow. It reads metadata from:

- Metabolomics Workbench (`metabolomics_workbench`)
- MetaboLights (`metabolights`)
- MB-POST (`mb_post`, `MPST...` accessions)
- MetaboBank (`metabobank`, `MTBKS...` accessions)

These are distinct repositories. MetaboBank metadata and original raw-data
references are read from the DDBJ Search API and its MAGE-TAB SDRF/file list.

The current scope excludes GC-MS, targeted SIM/MRM experiments, proteomics
projects, and LC-MS experiments whose acquisition cannot be resolved as DDA or
DIA/AIF/SWATH with product-ion spectra.
Ambiguous records are placed in `raw_metadata_required`; they are not silently
treated as eligible.

Agent-driven reanalysis also requires a user-reviewed `analysis_purpose` before
download. That purpose anchors Class/contrast selection, annotation strategy,
QA, and requested outputs; it is retained with repository provenance.

## Workspace and retention policy

On Windows, the default workspace is `D:\MSDIAL_Public_Reanalysis`. Each
accession receives separate `raw`, `output`, and `provenance` directories.
Downloaded raw data are a temporary lease:

1. Repository metadata are inspected before download.
2. Declared and transferred bytes are bounded.
3. Repository checksums are verified when available.
4. TAR and ZIP archives are checked for path traversal, links, and excessive
   extracted size.
5. Raw headers are inspected when a compatible metadata reader is available.
6. Cleanup remains locked until at least one retained mzTab-M file passes
   validation.
7. Raw data can then be deleted while provenance and analysis artifacts remain.

The retained set includes repository and publication metadata, reviewed
sample metadata, `analysis_files.csv`, parameter/method files, mzTab-M,
`mdpeak`, `mdscan`, `mdmsp`, and `mdalign`. GUI project artifacts such as
`dcl`, `arf`, and `arf2` are also collected into
`msdial-project-artifacts.zip`. The manifest stores SHA-256 and byte size for
each retained artifact. Original project files are not removed by this archive
step.

This order makes disk cleanup routine without allowing an incomplete or failed
analysis to erase its only input copy.

## Reproducible candidate selection

Run commands from the repository root with the Python used for MS-DIAL
Interactive. The same seed and repository state produce the same shuffled
candidate order.

```powershell
python scripts/repository-reanalysis.py select metabolomics_workbench `
  --count 10 --seed 20260824 --inspection-limit 200 `
  --max-download-gb 5 --max-samples 40 `
  --output D:\MSDIAL_Public_Reanalysis\selection-metabolomics_workbench.json

python scripts/repository-reanalysis.py select metabolights `
  --count 10 --seed 20260824 --inspection-limit 200 `
  --max-download-gb 5 --max-samples 40 `
  --output D:\MSDIAL_Public_Reanalysis\selection-metabolights.json

python scripts/repository-reanalysis.py select mb_post `
  --count 10 --seed 20260824 --inspection-limit 200 `
  --max-download-gb 5 --max-samples 40 `
  --output D:\MSDIAL_Public_Reanalysis\selection-mb_post.json

python scripts/repository-reanalysis.py select metabobank `
  --count 10 --seed 20260824 --inspection-limit 200 `
  --max-download-gb 5 --max-samples 40 `
  --output D:\MSDIAL_Public_Reanalysis\selection-metabobank.json
```

The selection JSON records inspected projects, selected projects, projects that
need raw-header preflight, excluded projects, repository URLs, declared file
sizes, inferred modality, ion mode, acquisition type, and every review reason.

With seed `20260824`, a 5 GiB download limit, and a 40-sample limit, the current
repository snapshot produced the following pilot pools:

| Repository | Inspected | Immediately eligible | Raw-metadata review |
| --- | ---: | ---: | ---: |
| Metabolomics Workbench | 200 | 8 | 50 |
| MetaboLights | 80 | 4 | 10 |
| MB-POST | 104 | 2 | 15 |

The requested count is a target, not a quota. Selection does not weaken the
GC/LC, untargeted, scan-acquisition, sample-count, or download-size criteria to
fill ten positions. LC-MS records reported as `Both` require confirmation of
true polarity switching or separation into positive and negative file groups.

Inspect one accession without downloading raw data:

```powershell
python scripts/repository-reanalysis.py inspect mb_post MPST000007

python scripts/repository-reanalysis.py inspect metabobank MTBKS47
```

## Repository metadata and MS-DIAL Class

The Data tab contains a **Repository metadata handler**. It can inspect an
accession directly or reopen `run-manifest.json`, `repository-metadata.json`,
or a reviewed metadata JSON. Each repository is normalized to one row per
analysis file while all source values remain available for audit and editing.

Select metadata fields in the intended hierarchy, for example `Genotype`,
`Region`, then `Sex`. MS-DIAL Interactive projects these fields into its single
Class value as `KO_North_F`. Spaces and underscores inside values become
hyphens, and missing values use `NA`, so downstream R code can split the Class
on `_` without silently changing the number of levels. Blank, pooled QC, and
standard labels are also used to populate MS-DIAL's File type when the
repository metadata states them.

The same operation is available without the browser:

```powershell
python scripts/repository-reanalysis.py metadata inspect `
  metabolomics_workbench ST002419 `
  --output D:\MSDIAL_Public_Reanalysis\ST002419-metadata.json

python scripts/repository-reanalysis.py metadata project `
  D:\MSDIAL_Public_Reanalysis\ST002419-metadata.json `
  --field treatment --field "sample source" `
  --analysis-csv D:\MSDIAL_Public_Reanalysis\analysis_files.csv `
  --destination D:\MSDIAL_Public_Reanalysis\ST002419\provenance
```

`metadata project` writes reviewed JSON and TSV files plus an updated
`analysis_files.csv`. Field order on the command line is the Class hierarchy
order. These APIs are also exposed by the local service for agent workflows:
`/api/repository/metadata/inspect`, `/load`, `/project`, and `/save`. The Data
tab then exposes **Download and recognize raw data**. This creates a bounded
workspace, reconstructs folder-type vendor data, and fills the analysis-file
table. Raw-data retention is selected per repository run: keep the raw data,
or delete it only after MS-DIAL succeeds and mzTab-M validation passes.
The transfer view reports declared bytes, measured throughput, percentage, and
ETA, including repositories that expose one large archive. Repository metadata
can then populate Class, file type, DDA/SWATH/AIF acquisition, batch, and
analytical order automatically or through **Apply metadata now**.

Internal-standard declarations such as a named commercial mixture are extracted
locally and shown in Quality assurance. If an Azure OpenAI or OpenAI-compatible
endpoint is configured, the user may draft m/z/adduct QA targets from that public
metadata. These remain reviewable drafts; RT is left blank unless the repository
explicitly supports it, and m/z-only QA matching does not report RT error.

## Download and preflight

Download one accession from a saved selection:

```powershell
python scripts/repository-reanalysis.py download `
  D:\MSDIAL_Public_Reanalysis\selection-mb_post.json MPST000007 `
  --workspace-root D:\MSDIAL_Public_Reanalysis --max-download-gb 5
```

For an accession in `raw_metadata_required`, add `--allow-preflight`. Then run
the metadata extractor against representative inputs:

```powershell
python scripts/repository-reanalysis.py preflight `
  D:\MSDIAL_Public_Reanalysis\mb_post\MPST000007\provenance\run-manifest.json `
  --extractor D:\0_SourceCode\msrawdataworkbench\RawMetadataConsoleApp\bin\Release\net48\RawMetadataConsoleApp.exe `
  --max-inputs 3
```

If the proprietary reader is unavailable, preflight is recorded as unavailable.
It does not revoke eligibility that was already explicit in repository metadata,
but it cannot promote an ambiguous project to executable status.

## Finalize and clean up

After MS-DIAL Interactive has produced mzTab-M, QA, and publication artifacts,
validate the retained output and unlock cleanup:

```powershell
python scripts/repository-reanalysis.py finalize `
  D:\MSDIAL_Public_Reanalysis\mb_post\MPST000007\provenance\run-manifest.json

python scripts/repository-reanalysis.py cleanup `
  D:\MSDIAL_Public_Reanalysis\mb_post\MPST000007\provenance\run-manifest.json `
  --confirmed
```

Use `discard --confirmed` only for a downloaded candidate rejected during
preflight. It removes the temporary raw data but preserves the rejection and
download provenance.

## Pilot record

The first end-to-end validation used one project from each repository:

| Repository | Accession | Scope | Result |
| --- | --- | --- | --- |
| MetaboLights | MTBLS341 | 18 GC-MS CDF files | mzTab-M passed validation; 410 SML and 410 SMF rows |
| Metabolomics Workbench | ST002419 | 24 GC-MS CDF files | mzTab-M passed validation; 823 SML and 823 SMF rows |
| MB-POST | MPST000007 | 30 LC-QTOF/MS LCD files, negative DDA | mzTab-M passed validation; 2448 SML and 2448 SMF rows |

The MetaboLights adapter selects an assay-specific raw-file set, so mixed
studies do not accidentally combine GC-MS and LC-MS assays. MB-POST detailed
analytical presets are inspected to reject targeted SIM records even when the
download is small.

For MPST000007, QA covered 30 injections and 2448 aligned features. The median
MS/MS acquisition rate was 96.6%, and the correlation between analytical order
and total intensity was 0.085. The repository metadata did not designate Blank
or QC injections, so Blank separation and QC topology were correctly reported
as not evaluable. File names such as `QA1_neg` were not reclassified without
metadata evidence.

The MPST000007 pilot also exposed a compatibility defect in the LC-MS
alignment-light long-format QA exporter. Peak picking and light alignment
completed, but QA export raised a dictionary-key error before mzTab-M export.
The same 30 files completed with normal alignment at about 1.1 GB working set.
This is recorded as an alignment-light exporter issue rather than a Shimadzu
LCD reader failure.

Repository APIs and file documentation used by the adapters:

- [Metabolomics Workbench REST API](https://www.metabolomicsworkbench.org/tools/MWRestAPIv1.0.pdf)
- [MetaboLights file guide](https://ebi-metabolights.github.io/guides/Files/)
- [MB-POST repository](https://repository.massbank.jp/)
- [MetaboBank](https://www.ddbj.nig.ac.jp/metabobank/)
