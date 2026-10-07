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
`msdial_app.mzxml_conversion`, with every inference flag off but one: a scan
whose mzXML records no polarity is given the unit's declared ion mode, read from
its Catalog handoff (`project.repository_metadata.catalog_handoff.technical_settings.ion_mode`,
the field the gate's CONV-1 holds an imputation to), and only where that declares
exactly one polarity, Positive or Negative. `project.ion_mode` is never read for
it: the raw-header preflight rewrites it from headers that carry what was
imputed, and a split part's is its part's polarity, while the handoff a part
copies from its parent is the parent's declaration. Both, Unknown or no
declaration imputes nothing, and CONV-1 fails the spectra left without a
polarity. A file some of whose scans record the other polarity and some none
contradicts the declaration: the converter refuses the imputation (its record's
`refused_inference` is `polarity_imputation`), and, as the user decided on
2026-10-03, the lease excludes that file unconverted with reason
`polarity_contradicts_declaration` and the rest of the unit runs. The refusal
is not a conversion record: it is kept in `input_conversions`'
`polarity_contradictions` (the mzXML, the declared polarity and the converter's
record), counted in `counts.polarity_contradicts_declaration`, and the file is
listed as a failed conversion is (below), so the gate's INP-1 accounts for it
and CONV-1 has no conversion of it to hold. A file whose every scan records
the other polarity asks for no imputation: it is converted with the polarity it
records, and the raw-header preflight splits the unit by polarity. The
declaration and its field are recorded in
the options of every conversion record and of `input_conversions`, and in its
`polarity_declaration`; each imputation is an inference with its count
(`counts.imputed_polarity_spectra` sums them). A record made under another
declaration is not reused by a later lease. The converted mzML
are the unit's input candidates. A project with no `analysis_unit_id` converts
nothing. Each conversion is recorded in the manifest's `input_conversions` and
in `provenance\input-conversions.json`; each converted input has an
`input_lineage` row of kind `converted` whose `source.conversion` names the
mzXML it was read from (path, sha256, md5), the output's sha256, the converter
and the validation, and carries the mzXML's own row (`source_row`). A file whose
conversion fails is kept out with reason `conversion_failed` and the rest run:
the unit's `excluded_input_candidates`, `input_lineage.excluded` and analysis-CSV
record name it, and its `campaign_disposition` lists it in `excluded_inputs`,
as it lists an mzML excluded as `unsupported_mzml_encoding` or an mzXML as
`polarity_contradicts_declaration`, so the gate's INP-1 accounts for the
declared input. A unit left with no input is excluded with the reasons, and
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

A unit whose Catalog declared no analysis inputs admits a file by its listing,
by the file names its samples declare, or through an archive one sample names.
An archive member that carries a declared name only behind a prefix is admitted
too (0.5.25): Metabolomics Workbench ST001264 declares `BioRec1.raw` and its
study archive holds `021518_387057_CSHp_BioRec1.raw`. The member's name (or its
stem, for a name declared without an extension) must end in `_`, `-`, `.` or a
space and then the declared name, compared without case; exact matches are
decided first; and the pairing must be one to one, so `Youn_sa1.raw` never
takes `..._Youn_sa11.raw` and a name two members carry is given to neither. The
input's `input_lineage` row records `name_pairing` (`declared_raw_file`,
`member_name`, `paired_by` `prefixed_member_name`), and the analysis CSV finds
its sample row by it. A declared name no member carries leaves its sample row
in the CSV record's `samples_without_input`, and the rest run; the CSV record
then carries the warning `sample_rows_without_input` and `sample_row_coverage`.

A declared name still unpaired is then paired by its leading identifier
(0.5.25, the user's decision of 2026-10-06): Metabolomics Workbench ST001359
declares `VV_13_HEpG2_C1_pos.raw`, and its archive holds
`VV_13_HEpG2_C1_exp344_pos.raw`. The key is the stem split on `_`, `-`, `.` and
spaces, taken up to and including the first token that contains a digit,
compared without case (`vv_13`); a key of digits only gives none. The key must
be unique among the declared names and among the candidate members. Exact
matches come first, then prefixed ones, then these. No inferred pairing crosses
a polarity: a member whose path, or a declared name, carries `pos`, `neg`,
`positive` or `negative` as a token of its own that is not the unit's ion mode
(or the two disagree) is refused, and the refusal is recorded. A polarity token
beside a `control`, `ctrl`, `blank` or `qc` token in the file name is read as
part of a sample's name (`Neg_Ctrl_1.raw` is a negative control) and refuses
nothing. A folder's token always states its polarity (`QC_NEG/`, `Blank_POS/`,
as `NEG/`). In a file name it is still read as a polarity in two cases: where it is the
name's only polarity token and that side's names (declared, or members) name
their files by polarity elsewhere, as the Catalog's `20200715_004_QC-neg.mzML` is
beside its `_pos` and `_neg` files; and where the name has a polarity token of
its own elsewhere, which is then the one it states. Each
inferred pairing is left on record: `name_pairing` on the lineage row
(`paired_by` `leading_identifier_token` and its `key`), `inferred_name_pairings`
and `refused_name_pairings` on the attribute stage, `input_name_pairings` and the
warning `input_names_paired_by_inference` in the run manifest and in every
campaign disposition (a split part's manifest carries them for its own inputs
and sample rows), the same warning on the CSV record, and the column
`raw_file_paired_by` (`exact`, `prefixed_member_name`,
`leading_identifier_token`) in the reviewed sample TSV.

An archive member that no sample row pairs with, exactly, behind a prefix or by
its leading identifier, is still an input where the unit's download is its own
alone (0.5.31, the user's decision of 2026-10-07): the Catalog declared no inputs
for the unit, and its download scope is `unit_files` or every bundle URL has
`shared_unit_count` 1. Metabolomics Workbench ST001264 has 31 members and 31
sample rows, and only its three BioRec rows pair; its 28
`021518_387057_CSHp_Youn_saN.raw` members run as unattributed inputs. Each is
preflighted like any input, so its raw header decides its polarity and
acquisition. Its `input_lineage` row has `name_pairing`
`{"paired_by": "unattributed_member", "member_name": ...}`, its stem as
`sample_id`, and `sample_row` null. Its analysis-CSV row is a `Sample` of the
unit's abstention Class (`All`) where the Class is an abstention, and of the
Class `Unattributed` otherwise. The reviewed sample TSV gets a row per member with
`raw_file_paired_by` `unattributed_member`. The run manifest records
`unattributed_members` (`count`, `members`, `paths`, `rule`
`unit_scoped_archive_2026_10_07`, `scope`). `members` names each member by its
basename, as its lineage row's `member_name` does, and `paths` gives the same
members' '/'-separated paths relative to the unit's raw data root (its parent's
for a split part); each `left_out` entry has both `member_name` and `path`. The
warning
`unattributed_members_included` is in its warnings, the attribute stage's, every
campaign disposition's and the CSV record's (a split part carries the record and
the warning for its own members only). A shared archive (`shared_unit_count`
above 1 for any bundle URL) takes none: `unattributed_members` then says
`applied` false with its `reason` (`shared_archive`, or
`download_scope_not_unit_scoped` where the scope says nothing) and lists the
members as `left_out`. Left out on record too: an mzXML member
(`requires_conversion`), a member whose path names the other polarity by a token
of its own (`polarity_token_contradicts_ion_mode`), and a member whose name
another member carries in another encoding, admitted or not
(`two_encodings_of_one_name`).

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
and an approval that keeps raw data deletes nothing. A deletion's preview says
so: its `download_store` gives the bytes of the tree's links to the store's
files, which deleting the tree does not free (`tree_bytes_kept_by_store`), and
what a collection under an approval would delete with the release
(`bytes_collectable_after_release`, an archive's extraction tree included). A
store object is collected only under an approval that covers boundary 5 for
every unit that released it, so what a person's confirmation leaves - under
`store_mode` `always` outside a campaign, every object - stays in `_dl` until a
cleanup or discard is repeated under such an approval.

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

Rows may share a sample id: a repository lists each injection of a sample as a
row of its own (MetaboLights MTBLS291's five replicates of `Cel`, MetaboBank
MTBKS64's `S01_M01` and `S01_M02` of `S01`). Each such row is its own analysis
input and analysis-CSV row, with its own raw file and its sample's Class;
nothing is merged, averaged or dropped. A repository unit's CSV row records the
sample row it came from (`sample_row_index`, `sample_raw_file`, and `sample_row`
on its `input_lineage` row). What stays refused is what is ambiguous: one row
two inputs name (`sample_row_with_two_inputs`), one input two rows name
(`input_with_two_sample_rows`), and an input of a sample several rows describe
that none of them names (`sample_row_not_identified`). A declared input is
paired with its row by its path first and its file name second, the same way
before the download (the handoff check) and after it (the analysis CSV). A split
gives each part the rows of its own inputs, so a sample whose replicates differ
in format or acquisition is in each part with only that part's rows
(`sample_row_indexes`).

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
msrawdataworkbench `5f604462d` with MsdialWorkbench `f0583493a`, and the earlier
built pins (`a12293c61` and `592b6dbce`, each with `f0583493a`) are still pinned
builds a campaign accepts. A build lives beside the Interactive checkout in
`RawMetadataExtractor-<raw>-<common>`, named by nine characters of each commit,
unless its entry names another folder (`build_folder`): the `5f604462d` build is in
`RawMetadataExtractor-5f60446-f0583493a`. A unit under a
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
run), and the execution gate then admits each file only as that type, and never
as one that contradicts the Console type its header settles
(`header_console_acquisition_type`: a DDA or AIF header, or a DIA header whose
isolation targets recur at two or more m/z, which is SWATH). A DIA header with one
recorded isolation target or none binds no type: the extractor records targets
only from MS2 headers that carry a precursor m/z. A disposition applied before
Interactive 0.5.29 (it records no `declared_acquisition_source`) is decided again
at the gate from the same records, and the run is refused where the new decision
would not run the unit, would exclude a row's file, or would give it another type
on its header's word. Preparing the unit again
(`msdial_prepare_repository_reanalysis`) clears it: the unit is decided again from
its recorded preflight before its rows are built (in memory for the preview, on disk
when the call writes), the analysis CSV follows the new decision, and the reply
reports it as `legacy_disposition_redecision`. The old disposition is kept under the
new one's `supersedes`.

A unit past its run is never decided again in place, and a prepare never writes over
that run's files. Past its run means a status of `mztab_validated`, `completed`,
`cleanup_pending_confirmation`, `raw_cleaned` or `discarded`, or a production run
that was finalised (`finalized_at`, `validation_failed` included); its output
directory holds the analysis CSV the run read, and `retained_artifact_inventory`
their checksums. Preparing such a unit returns `ok: false`, `reason: run_finished`,
and writes nothing. To run it again, prepare a new production run with
`new_run=true`:

- the finished run's records are copied unchanged into a new entry of
  `superseded_runs` (its status, `cleanup_allowed`, `output_directory`,
  `finalized_run`, `mztab_validation`, retained artifacts and their inventory,
  `analysis_csv`, `analytical_order`, campaign disposition and project, the
  lineage rows' written names and types as `input_lineage_written`, and the
  preflight's per-file Console types as `preflight_per_file`). The old output
  directory and its files are left as they are;
- the finished run's own records are taken off the top level, `output_directory`
  becomes `<workspace>\output-run-<n>` (n = 2 for the second run), and the analysis
  CSV and the new run's outputs go there;
- `cleanup_allowed` becomes false and the status `preflight_passed` (`prepared`
  where no raw-header preflight is recorded);
- a disposition applied before 0.5.29 is decided again under the header-first rule,
  which then sets the status and `execution_allowed`.

The preview (`confirmed=false`) does this in memory, creates nothing and reports it
as `preview.new_run`. A new run is refused, and nothing written, for a unit whose raw
data were released (`raw_released`, also without `new_run`), while a run attempt of
the unit may still be running (`run_in_progress`), where the legacy disposition
decided again would not run the unit (`would_not_run`, with the decision in
`new_run.legacy_disposition_redecision`), or where that decision fails
(`redecision_failed`). The execution gate holds a workflow to the unit's current
`output_directory`, so the new run cannot write into the finished run's folder.

A confirmed new run is all or nothing. The run is decided in memory, its rows
built, its aliases made, and its reviewed metadata and analysis CSV written into a
hidden staging folder beside the new one. Only then, under the manifest lock, the
staging folder becomes `output-run-<n>` and the manifest is written once, with the
superseded run, the new `output_directory`, the CSV record, the analytical order and
any campaign crossing. If anything before that fails, the manifest and every file of
the finished run are left byte for byte as they were, and the staging folder and the
aliases the call made are removed. That covers rows that disagree or an alias that
cannot be made (`analysis_csv_failed`, with nothing recorded in the manifest), files
that do not map, and errors. If another writer changed the manifest meanwhile, the
call returns `new_run_conflict`. The unit stays finished, and the same call can be
made again.

An alias's path in `raw\console-aliases` is fixed by its input, so two prepares of
one unit share it. An abandoned call removes an alias it made only if no analysis CSV
of the unit's committed manifest names it, so a concurrent prepare that reused the
alias and committed keeps it. A commit whose CSV names an alias that has since been
removed is refused as `new_run_conflict`. Both checks hold the manifest lock.

Raw-data deletion is judged by the unit's current run. Until a new run prepared after
a validated run has validated itself, its raw data are kept for it:
`msdial_cleanup_repository_raw` refuses because the current run is not validated, and
`msdial_discard_repository_raw` refuses because the unit did produce a validated
output. Each preview names the superseded validated run. Once the new run validates,
the cleanup judges it as it judges any other run. A split part is held the same way,
approved or not: its discard under a campaign approval is refused, and its parent's
raw release counts it as not ended, whatever its status and however many runs failed.

Each file's acquisition is its raw header's wherever the header was read (user
decision, 2026-10-06). A file with MS2 whose header gives DDA, DIA, AIF or SWATH
runs as that, whatever the unit declares and whatever confidence the extractor
gave: that confidence is a constant per branch of its classifier, and a
declaration is the Catalog's keyword match over the assay's text. Each
declaration a header overrode is listed in `declared_vs_header` with its
`declaration_source` (`catalog_keyword_inference`, `split_part` or
`unattributed`; the record's `declared_acquisition_source`); an override is
reported only for a file that reaches the run, and one excluded afterwards is
listed with `decided: excluded` and its `excluded_reason`. A file whose header
gives Unknown is excluded as `acquisition_unresolved`, declared unit or not, and
one whose header gives PRM, SRM, MRM or SIM as out of scope. A unit left with only
Unknown-header and MS1-only files is skipped as `acquisition_unresolved`, not
excluded. The declaration
decides only a unit none of whose headers could be read
(`acquisition_declared_only`), and SWATH or AIF for a DIA header whose isolation
settles neither. MS1-only files are folded into a DDA run, except in a unit
declared DIA or AIF, where they may be all-ion data exported as MS1 scans and are
excluded as `ms1_only_in_declared_dia_unit`. A split part is held to its parent's
declaration there (`split_from.parent_declared_acquisition_mode`).

AIF runs as SWATH where it has one collision energy (0.5.31, the user's decision
of 2026-10-07). A unit that would run as AIF counts the distinct MS2 collision
energies, to 0.1 eV, over every input that runs (`ms2_collision_energies` on each
per-file record, from the extractor's `acquisition.collisionEnergies`). A Waters
LockSpray reference function contributes none: the extractor marks it as
reference and gives it no MS level, and a record whose reference function does
carry an MS level leaves its energies unresolved. One energy runs the unit as
SWATH, which the pinned Console deconvolutes alike (ST004304 gave identical
results, and MTBKS281 matched its 30 eV collection): the disposition records
`aif_run_as_swath` (`collision_energies`, `rule`
`single_ce_aif_as_swath_2026_10_07`), and each input's per-file record has
`console_acquisition_type` SWATH with `console_acquisition_basis`
`aif_single_ce_as_swath`, its `header_console_acquisition_type` staying AIF. More
than one energy holds the unit until a Console that settles an all-ion spot's
representative energy exists: `disposition` skip, `reasons`
`aif_multi_ce_awaiting_console`, `hold` true. A unit whose inputs record no
energy is held the same way as `aif_collision_energy_unrecorded`. A held unit is
no failure, neither a campaign approval nor `confirmed=true` discards its raw
data, and a held split part has not ended for its parent's raw release, even once
discarded. Only an operator's explicit skip lifts the hold:
`release_disposition_hold=true` (default false) on `msdial_discard_repository_raw`,
`discard_download_lease`, a split part's discard, `cleanup_split_parent`, and the
CLI's `discard --release-disposition-hold`. With it and an approval covering
boundary 5 (or `confirmed=true`) the discard proceeds, and the unit or part
records `disposition_hold_released_by` `operator_skip` (and
`disposition_hold_release`: who, when, the hold's reasons) before anything is
deleted. On a split parent it lifts the hold of each held part, which is then
listed as `hold_released` in `raw_release.parts`. The campaign runner's `skip` of
a `disposition_held` unit passes it; an agent passes it only on that explicit
decision, never because a hold blocks a discard. A unit to be split is split
first, and each part is decided by the rule on its own. The execution gate runs a
header's AIF as SWATH only where the applied disposition records
`aif_run_as_swath`, and refuses it anywhere else. `classify_preflight` reads the
energies of a preflight recorded before 0.5.31 from the extractor records it
left.

Multi-energy AIF runs with a Console that has MsdialWorkbench#825 (0.5.34). #825
deconvolutes an AIF file separately at each collision energy and represents each
peak, in the per-file export and in alignment, by the energy of its MS/MS
reference-spectrum match, or else by the energy whose deconvoluted spectrum has
the most product ions (the lowest such energy on a tie); it reads the energies
from the raw data, so the analysis CSV says `AIF` and nothing more. The
preflight and `classify_preflight` decide for the configured Console
(`console_path`, else the saved `console_path` setting, else
`MSDIAL_CONSOLE_PATH`), whose assembly is read, never started, for two messages
of #825's multi-energy reader (capability
`multi_energy_aif_representative_collision_energy`). Both are required: an
earlier local AIF patch build carries only the one that came with the
per-energy files, and still takes an unannotated peak's spectrum from the first
energy; its probe is `marker_incomplete`, and it holds as a Console without #825
does. Every AIF unit's disposition
records that probe as `multi_energy_aif_console` (`available`, `probe`,
`console_assembly`, `assembly_sha256`). With #825, a unit every AIF input of
which records the same energies, more than one, is decided `run` as AIF:
`aif_multi_ce_run` (`collision_energies`, `rule` `multi_ce_aif_with_console_825`),
each per-file record `console_acquisition_basis` `aif_multi_ce_console_825`.
#825 chooses a representative energy among the energies of one file, never
across files (a file with one energy keeps its single deconvolution result), so
with #825 inputs whose energies differ from one another are held as
`aif_collision_energies_differ_between_inputs`, raw data kept, each input's
energies in `aif_collision_energies_by_input`; no Console releases that hold,
only an operator's decision. One energy still
runs as SWATH, and an unrecorded energy is still held, since the #825 Console
stops on an AIF file whose MS2 scans carry no energy. Without #825 nothing
changes. A unit held earlier as `aif_multi_ce_awaiting_console` is released by
the operator's recheck: preflighting it again (or `classify_preflight`) with
the #825 Console configured decides it `run`. The execution gate refuses an
`aif_multi_ce_run` unit when the workflow's Console lacks #825, a per-file
record that claims the basis without the record, and a per-file record whose
own energies are not the recorded ones. The Materials and Methods text
and Table S1 state the energies and the representative-energy rule.

A repository declaration of PRM, SRM, MRM, SIM or full scan is a declaration like
any other, and untargeted status is never inferred over a declared targeted
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
`held` and writes nothing. A finished unit whose applied disposition predates
0.5.29 is decided again only as a new production run is prepared for it (above),
after its finished run's records have moved to `superseded_runs`. A split parent
that is read all the same (outside a
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
authorized discard. A part its campaign disposition holds has not ended, skipped
or discarded, until an operator's skip lifts the hold
(`release_disposition_hold`, above). A run is recorded as failed (`run_failures`) when its
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
