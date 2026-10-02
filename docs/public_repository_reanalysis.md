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

MS-DIAL reads mzML but does not support mzXML or mzData. Those legacy formats
are classified as `requires_conversion` and blocked before download. Convert
them to centroided mzML with a reviewed ProteoWizard `msconvert` workflow and
record the conversion in a new input manifest; changing the role label alone is
not sufficient.

In a campaign, and only there, an mzXML is converted by Interactive itself. A
unit a campaign authorization covers (`campaign_authorization_path` on the plan
and download tools, recorded in the manifest's `campaign_authorizations`) is
judged with `EligibilityPolicy.convert_mzxml`: an mzXML (packed or not) only
plans a conversion (`project.conversion_plan`), and the download lease's
`convert` stage, after extraction and before input discovery, writes each of the
unit's mzXML as mzML under `raw\converted\<relative path>.mzML` with
`msdial_app.mzxml_conversion`, with every inference flag off. The converted mzML
are the unit's input candidates. A project with no `analysis_unit_id` converts
nothing. Each conversion is recorded in the manifest's `input_conversions` and
in `provenance\input-conversions.json`; each converted input has an
`input_lineage` row of kind `converted` whose `source.conversion` names the
mzXML it was read from (path, sha256, md5), the output's sha256, the converter
and the validation, and carries the mzXML's own row (`source_row`). A file whose
conversion fails is kept out with reason `conversion_failed` and the rest run:
the unit's `campaign_disposition` lists it in `excluded_inputs`, as it lists an
mzML excluded as `unsupported_mzml_encoding`, so the gate's INP-1 accounts for
the declared input. A unit left with no input is excluded with the reason, and
its preflight skips it. A full disk, or a file another process holds
(`EACCES`/`EPERM`, after the converter has waited for it), is not a failed
conversion: the lease stops at its `convert` stage (`download_failed`), keeping
the records written so far, and a retry converts the rest. A re-lease reuses a
conversion whose record still holds. Where the unit holds a vendor container or
an mzML of the same sample beside an mzXML, the Catalog's encoding rule (shared
test vectors `tests/vectors/encoding_preference.v1.json`) analyses that one and
the mzXML is not converted. Files are one sample's where their folders agree
once the words naming an encoding are set aside (`mzML/x.mzML` and
`mzXML/x.mzXML`), or where the unit admits the readable file by itself, by its
listing, its sample names or the archive a sample names, whatever its folder
(`Thermo_RAW/x.raw` beside `mzXML/x.mzXML` for a sample named `x`); a study
archive's `POS/QC_01.mzML` is no encoding of `NEG/QC_01.mzXML` for a unit whose
samples name `QC_01.mzXML`. mzData, which nothing converts, excludes its unit in
a campaign too.

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
`dcl`, `arf`, and `arf2` in the output are also collected into
`msdial-project-artifacts.zip`. The manifest stores SHA-256 and byte size for
each retained artifact. Original project files are not removed by this archive
step.

MS-DIAL writes its per-file (`<file>_<time>.dcl`, `.pai2`, `_tags.xml`) and
alignment (`AlignResult-<time>.*`) containers beside the raw files it reads.
For a unit a campaign approval has been recorded for (its manifest's
`campaign_authorizations`, or those of the unit it was split from), after the
run and before validation, the finalised run's set moves to
`output\msdial-intermediates\` with its path under the raw directory kept, and
is retained file by file; the job that moved it is in the record. Sets left by
earlier attempts are recorded as superseded and go with the raw data.
`<project>_Loaded.msp2.dbs`, MS-DIAL's copy of every library the run loaded, is
deleted and recorded by name, size and SHA-256. The saved project cannot then be
reopened as it stands: its `.mddata` names the containers by their
raw-directory paths and its loader needs the library copy. Any other run,
including a trial unit whatever its retention policy, keeps its containers
beside its inputs and its library copy in its output, as before.

What a repository unit shares carries no path from this machine: the mzTab-M's
`database[n]-uri` is `null` for a library without a DOI or https source and
the DOI otherwise, `ms_run[n]-location` is workspace-relative (`raw/...`), and
the publication report, tables and workflow bundle name libraries by file name
and SHA-256 and declare `shared_path_policy`. The original locations are kept
only in `provenance\mztab-redaction.local.json`.

A file another process holds open cannot be replaced or deleted on Windows,
and a container's path below the output can pass MAX_PATH (moves use
extended-length paths). Each step is retried for up to a minute. What still
fails is recorded in the unit manifest's `finalisation_holds`: an mzTab-M that
could not be redacted, or a library copy that could not be deleted, holds
sharing, and the publication report refuses the unit; a container that could
not be moved holds the raw deletion, and the raw cleanup and discard refuse it,
a split parent's included. Each of those steps retries what is held first.

This order makes disk cleanup routine without allowing an incomplete or failed
analysis to erase its only input copy.

### The accession download store

A campaign's lease, and any other only when the `store_mode` setting is
`always` (the default, `campaign`, keeps every other lease as it was), fetches
through the accession download store,
`<workspace_root>\<repository>\<accession>\_dl`. Each object is fetched once for
every unit of the accession that lists it, under one claim per unit, with the
download's idle timeouts and retries, and its published MD5 compared before
anything else reads it; each archive is extracted there once. The unit's own
`raw\data` is then a tree of hardlinks to the store's files, placed where the
per-unit lease put them (a bundle at the data root, a per-sample container in
its listed directory, nothing overwritten); a link that fails is a copy. Once
the lease knows the unit's inputs, members and sidecars, the links to anything
else (the other polarity's samples in a shared study archive, say) are pruned.
What the SCIEX reader opens beside a kept `.wiff` or `.wiff2` stays with it
(`x.wiff2`'s `x.wiff.scan` and `x.timeseries.data`, any `x.wiff.<n>.scan`), by
the rule the analysis CSV's aliases carry them by. MS-DIAL's containers are
written beside the links, in the unit's tree, and never reach the store.

The manifest records `download_cache` (the store, each object, whether it was
fetched or reused, the claims), `raw_storage` (`materialization`: hardlink,
copy or mixed; the bytes linked from the store and those the unit holds itself;
what was pruned), and on each `downloads[]` entry `cache_object_path`,
`sha256_origin` (`fetched_by_this_unit` or `inherited_from_cache`) and
`declared_checksum_verified`. A per-file object's `path` is the unit's link; an
archive's is the store's object. A lease that waits for another lease's
transfer or extraction of the same object shows the job state
`waiting_for_shared_download`, and can still be cancelled.

The unit's cleanup, discard, or a split parent's release, once its tree is
gone, releases its claims (`download_store_release`); under the campaign
approval that covered the deletion, the store then deletes each released object
no live claim still holds, leaving its record as a tombstone. A batch
pre-claim (`msdial_repository_batch_plan` with `pre_claim=true`) keeps an
object for units that have not run yet, a split parent's claims stand for its
parts, a person's confirmation releases claims without deleting store objects,
and an approval that keeps raw data deletes nothing.

A cleanup, discard or split-parent release asked for again deletes nothing and
releases what is still unreleased, so one whose release failed or stopped is
finished by its repeat (`already_cleaned`, `already_discarded`,
`already_released`). `download_store_release` stays the record of the release
that last changed something - released a claim, or collected an object or a
partial transfer - with the repeats that changed nothing listed in its
`repeats` and a record it replaced in `earlier`. `_dl` and `_campaigns` are
never taken for units. `msdial_download_store_status` shows every store, read
only.

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

The extractor is chosen in this order: the explicit path, the
`raw_metadata_extractor_path` setting (`msdial_set_raw_metadata_extractor_path`,
which refuses a build whose record does not verify), `MSDIAL_RAW_METADATA_EXTRACTOR`,
and last the build in the sibling `msrawdataworkbench` working checkout, reported
as `working_checkout_default`. `msdial_check_raw_metadata_extractor` lists every
candidate with its provenance status and whether it is a pinned build. The pinned
builds are data (`PINNED_BUILDS` in `raw_metadata_extractor.py`); the current one is
msrawdataworkbench `592b6dbce` with MsdialWorkbench `f0583493a`. A unit under a
campaign approval is preflighted only by the first extractor named, and only when it
inspects as verified and pinned; anything else is refused before a file is read.

The extractor reads at most 20 inputs per process, with no command line over
32,767 characters, and each process has a time limit made of its inputs' limits:
five minutes for a metadata reader, plus 1,800 s per GB for Waters `.raw` (which
has no metadata-only reader) and 600 s per GB for ion-mobility data. A group that
fails or times out is read again one input at a time, so every input gets its own
outcome (`ok`, `reused`, `unsupported_format`, `failed`, `timed_out` or `os_error`)
in `raw_metadata_preflight.summary.per_file`, with only a tail of stderr. An input
read before by the same extractor binary, at the same size and modification time,
is not read again; a split part reuses its parent's reads.

Every preflight ends with `campaign_disposition` (`msdial-campaign-disposition.v1`):
`run`, `skip`, `exclude` or `split`, with reason and warning codes, the inputs it
excludes and, for a split, the grouping by acquisition and polarity. Outside a
campaign it is advice and changes nothing else. Under a campaign approval it is
applied: it sets `execution_allowed`, the status (`preflight_passed`,
`skipped_by_preflight`, `excluded_by_preflight`) and each input's
`console_acquisition_type` (DDA, SWATH or AIF; none for an input that does not
run), and the execution gate then admits each file only as that type.

A repository declaration of PRM, SRM, MRM, SIM or full scan is a declaration like
any other: the unit is excluded unless a header of confidence 0.8 or more says
otherwise, and untargeted status is never inferred over a declared targeted
acquisition. An input that is missing, or that the recorded preflight never read,
skips the unit (`inputs_missing`, `raw_metadata_incomplete`) instead of shrinking
the run; only an input that was read and failed is excluded on its own. Outside a
campaign, an eligible unit with an unreadable input ends as it always did, as
`preflight_unavailable` (or `preflight_unsupported_format`) with
`execution_allowed` kept, since nothing outside a campaign can exclude that input.

No disposition changes a unit that was split, whose run has finished
(`mztab_validated`, `cleanup_pending_confirmation`, `raw_cleaned`) or whose run
attempt is still open: a campaign preflight of such a unit reads nothing and
reports `preflight_held`, and `classify_preflight` returns its decision with
`held` and writes nothing. A split parent that is read all the same (outside a
campaign, or split while its headers were being read) records the reads for its
parts and keeps its status and the disposition it carries.
`classify_preflight` decides a summary written before the per-file fields
existed from the extractor records its preflight left.

If raw headers report more than one acquisition mode, the result is `Mixed`.
Preview `msdial_split_repository_unit`, confirm the proposed DDA and
DIA/AIF/SWATH child units, then preflight each child independently. The Mixed
parent must never be passed to an MS-DIAL production run.

The split key has three parts: the acquisition mode, the ion-mobility regime
(the header's `has_ion_mobility`, or an ion-mobility container such as a Bruker
TDF folder) and the polarity. A unit whose inputs differ in any of them is
split along every part in which they differ, and the part id names each of
those parts after the acquisition mode: `<unit>-dda` and `<unit>-dia` as
before, `<unit>-dda-im` for the ion-mobility part of a BAF/TDF unit, and
`<unit>-dda-neg` / `<unit>-dda-pos` for a polarity split. An ion-mobility part
is written excluded (`excluded_by_preflight`, `split_exclusion`
`ion_mobility_out_of_scope`): LC-IM-MS is outside this campaign's scope. A
part split by polarity carries its polarity as its ion mode, and each part
lists only its own entries of the parent's file list, its folders' members
matched by path, and its own samples: those its declared inputs or its lineage
name, and otherwise those whose `raw_file` matches its inputs' paths, a sample's
name being used only where no path accounts for it. An input two samples match
equally well is neither one's, and those samples are reported unclaimed.

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

For agents, cleanup is a separate confirmation boundary: first preview
`msdial_cleanup_repository_raw`, report the exact raw directory and retained
artifact inventory, and call it with `confirmed=true` only after the user
approves that deletion. Selecting a retention policy earlier in the workflow
does not authorize deletion. `msdial_discard_repository_raw` previews and
performs the discard of a unit that produced no validated output.

In a campaign, `campaign_authorization_path` stands in for `confirmed=true` on
both tools (and on `cleanup_download_lease`, `discard_download_lease` and
`cleanup_split_parent` called in-process) when the approval covers boundary 5
for the unit, or the unit it was split from, and the approval and the unit both
state `delete_after_validated_output`. Under it a failed unit whose output
holds an unvalidated or invalid mzTab-M may be discarded: that mzTab-M, its
validation (`failure-artifacts/output-validation.json`) and the failure record
(`failure-artifacts/run-failure-record.json`) are kept under output and listed
in `failure_artifacts`. Both are written as shared artifacts: they declare
`shared_path_policy`, the raw directory reads `raw/`, a workspace path is
relative, any other location is withheld, and the failure record carries each
failure's reason, exit code and time and each attempt's identifiers, never a
log line, a host or an output directory; the full record stays in the
provenance manifest. Every deletion refuses while a retained artifact lies
under its target, unlinks a multiply-linked file without touching its
attributes, and records what it removed and kept (`raw_deletion`); one that a
held file or a crash stopped is resumed by the next call, keeping the failure
artifacts it wrote first. A discard that has finished, asked for again, returns
its record (`already_discarded`) and writes nothing.

A split parent's raw tree, which every part reads, is released by
`cleanup_split_parent` (also reached through either tool on the parent's
manifest) once every part has ended: validated, failed after its retries
(three recorded run failures), skipped, excluded, or discarded by its own
authorized discard. A run is recorded as failed (`run_failures`) when its
Console exits non-zero, and also when it exits 0 without a validated mzTab-M:
it wrote none, what it wrote failed validation, or finalisation found another
mzTab-M in the output that fails (the status `validation_failed` is kept). The
job itself stays `completed`. The release takes a lock, writes `raw_release`
before the first file goes, marks validated parts `raw_cleaned` with
`raw_released_by`, and leaves the parent `split_by_acquisition`. A finished
run's post-run hook only records the parent's pending plan
(`raw_release_pending`); in a campaign the runner is the one trigger of every
deletion. A unit whose raw data were released (`raw_cleaned`, `discarded`, or a
part of a released parent) never passes the execution gate again.

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
