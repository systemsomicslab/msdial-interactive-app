# MS-DIAL Interactive App

Cross-platform local web application for building and running MS-DIAL Console
LC-MS and GC-MS workflows.

## Quick start: ask Claude Code to set it up

Install [Claude Code](https://claude.com/claude-code) (the terminal CLI, the Claude
desktop app's Code tab, or an IDE extension) and Python 3.10 or newer. Then open Claude
Code in any folder and say:

```text
Set up https://github.com/systemsomicslab/msdial-interactive-app so that I can run
MS-DIAL agent analyses in Claude Code.
```

Claude Code follows [the instructions for Claude Code](#instructions-for-claude-code-setting-this-repository-up)
below. It checks Python and Git, downloads this repository, shows you the setup plan,
and changes your computer only after your yes. The setup:

- registers the `msdial-interactive` MCP server and copies the skills for Claude Code;
- installs the official MS-DIAL Console 5.5.261018 for Windows, macOS or Linux, after
  checking its SHA-256.

When it has finished, open a new Claude Code session; the MS-DIAL tools appear there.

## Quick start: laboratory lipidomics with Claude

Claude Code or Claude Desktop can run a whole lipidomics batch with internal standards
through this app: from a positive and a negative raw-data folder to a quantified
`lipid_metabolome.csv`, asking every scientific question and starting MS-DIAL only after
your yes. Everything runs on your computer.

1. Install Python 3.10 or newer and Claude Code or Claude Desktop.
2. Run the setup, or let Claude Code run it (above): on Windows double-click
   `Setup MS-DIAL for Claude.cmd`; elsewhere run `python3 scripts/setup_claude.py`. It
   explains each step and asks before it changes anything (`--dry-run` shows the plan
   only; on Windows, `py -3 scripts\setup_claude.py --dry-run`). If no Console new
   enough is found, it offers the official MS-DIAL Console prerelease 5.5.261018 for
   your system.
3. Follow [the tutorial](docs/tutorials/lab-lipidomics.md) with the public demo data
   (Zenodo [10.5281/zenodo.10957708](https://doi.org/10.5281/zenodo.10957708), CC BY 4.0)
   and the example workbook and standard texts in
   [`examples/lab-lipidomics`](examples/lab-lipidomics/README.md).

The macOS and Linux Consoles are x64 builds that read mzML, ABF and netCDF, not vendor
formats: convert vendor files such as SCIEX `.wiff` to mzML first (ProteoWizard
msconvert). On Apple silicon the macOS Console runs under Rosetta 2.

## Instructions for Claude Code: setting this repository up

*For an AI coding agent asked to set this repository up for someone. A person can read
the quick starts above instead.*

The person is often a beginner. Explain each step in a sentence, ask before anything
that changes their computer, and report what actually happened.

1. **Check the prerequisites.**
   - Git: `git --version`.
   - Python 3.10 or newer: `python3 --version`, or `py -3 --version` on Windows. The
     `python3` of Apple's Command Line Tools is 3.9, which is too old.
   - If one is missing, tell the person what to install and wait until they have:
     Python from <https://www.python.org/downloads/>, Git from <https://git-scm.com/>
     (`xcode-select --install` on macOS). Do not install either yourself unless they
     ask you to.
2. **Get the repository into a folder that will stay.** The MCP registration and the
   Console point into it, so it must not be moved afterwards.
   - Clone it in the person's home folder, or a short path such as `D:\msdial` on
     Windows:
     `git clone https://github.com/systemsomicslab/msdial-interactive-app.git`
   - If it is already there, update it: `git -C <folder> pull --ff-only`.
   - Without Git, download and unpack
     <https://github.com/systemsomicslab/msdial-interactive-app/archive/refs/heads/main.zip>.
3. **Show the plan.** In that folder run `python3 scripts/setup_claude.py --dry-run`
   (`py -3 scripts\setup_claude.py --dry-run` on Windows). It changes nothing. Summarise
   what it would do:
   - the `pip install` of the MCP dependency;
   - the Claude Code registration, and any existing one it would replace;
   - the skills it would copy, and the Claude Desktop configuration if Desktop is
     installed;
   - the MS-DIAL Console download: its size, URL and folder.
4. **Run it after the person says yes.**
   - Use `--yes`: the setup cannot ask questions through your tool, so without it every
     change is declined. Add `--no-desktop` if they do not use Claude Desktop.
   - Allow up to 10 minutes; pip and the Console download (about 45 MB) take a while.
   - If pip refuses because the Python is "externally managed" (Homebrew), make a
     virtual environment in the folder (`python3 -m venv .venv`) and run the setup
     with its Python (`.venv/bin/python`, or `.venv\Scripts\python.exe` on Windows).
5. **Report the summary it prints.**
   - Mark every step shown as `attention` or `failed`, with what the setup says to do
     about it.
   - Never call a step done unless the summary shows it as `done`, `ok` or `unchanged`.
6. **Hand over.**
   - The MS-DIAL tools appear only in a new session. Tell the person to open a new
     Claude Code session, or to run `/mcp` in the terminal CLI. You cannot use the
     tools in this session.
   - Give them this first prompt for the new session: *Check that MS-DIAL Interactive
     is ready for laboratory lipidomics: start its backend, list the MS-DIAL Consoles
     you can find and say whether one is new enough. Do not run any analysis yet.*
   - Point them to [the tutorial](docs/tutorials/lab-lipidomics.md) for the demo data.

## Why Python + local web UI

- The same application code runs on Windows, macOS, and Linux.
- MS-DIAL Console executables can be selected per operating system.
- Raw files are always read from their original filesystem paths and are never
  copied into the app.
- The browser UI supports native file dialogs and manually entered local paths.
- Folder-type vendor data are kept as one analysis unit: Waters `.raw`,
  Agilent `.d`, and Bruker `.d`.
- The app has no mandatory third-party Python dependencies.

Proprietary vendor formats may still depend on vendor readers supported by the
selected MS-DIAL Console build. For Linux/macOS workflows, mzML is generally the
most portable input format.

Agilent `.d` requires the Agilent reader assemblies, including
`BaseDataAccess.dll`, to be resolvable from the selected Console package. On
Windows, the vendor reader may also require Microsoft Visual C++ 2013
Redistributable Package x64. The app shows both checks separately so that a
missing managed assembly is not mistaken for a missing native runtime.

## Folder-type vendor data

The app detects vendor folders using the same signatures as MS-DIAL:

- Waters: a directory ending in `.raw`
- Agilent: a `.d` directory containing `AcqData`
- Bruker: a `.d` directory containing `analysis.tdf`, `analysis.tsf`, or
  `analysis.baf`

Use Add original files, Add original folder, or Add path. Drag and drop is not
offered because standard web browsers do not reliably expose absolute paths,
and this app intentionally never creates fallback copies.

## SCIEX data

Only `.wiff` and `.wiff2` are added as SCIEX analysis files. A `.wiff.scan`
file is never added as a separate analysis row. It remains beside its matching
`.wiff` in the original directory where the SCIEX reader expects it. Other
SCIEX sidecars are rejected.
When both `.wiff` and `.wiff2` exist for the same sample, the app asks the user
to choose one and does not add the ambiguous pair.

A `.wiff` file is accepted when its original path is available. The app checks
for the adjacent `.wiff.scan` before processing. It never falls back to
`work/uploads`.

Output root defaults to the directory containing the first analysis data item.
It remains editable, and both diagnostic and full-analysis result folders are
created below that selected location.

## Analysis metadata CSV import

**Import analysis CSV** loads an existing MS-DIAL Console metadata CSV and
populates the Data table in one step. It reads `file_path`, `file_name`,
`file_type`, `class_id`, `acquisition_type`, `batch_order`,
`analytical_order`, and `factor`. Relative `file_path` values are resolved
from the CSV directory. The legacy `Included` column is accepted but ignored,
matching the current app behavior in which every displayed row is processed.

## Project types and adducts

The UI separates LC-MS, GC-MS, DI-MS, LC-IM-MS, IM-MS, and Imaging-MS
parameter modes. LC-MS and GC-MS are currently executable workflows. Other
modes show their capability boundary and are blocked at preflight until their
mode-specific parameter backend is implemented.

GC-MS fixes Target omics to Metabolomics and hides solvent, precursor-adduct,
LBM, Text DB, and lipid-query controls. It provides EI MSP annotation settings,
nominal/accurate mass selection, and RT/RI retention-index settings. Other
project types provide searched-adduct selection from the MS-DIAL positive and
negative adduct resource tables.
For non-lipidomics workflows, Solvent is shown disabled because it does not
affect Metabolomics processing.

## Saved paths and parameter-template loading

The Paths panel can persist the MS-DIAL Console, parameter template, and
`LbmQueries.txt` paths for the next launch. This is a JSON file rather than an
INI file. The exact path is shown in the UI and follows the operating system:

- Windows: `%LOCALAPPDATA%\MSDIALInteractive\settings.json`
- macOS: `~/Library/Application Support/MSDIALInteractive/settings.json`
- Linux: `${XDG_CONFIG_HOME:-~/.config}/msdial-interactive/settings.json`

The panel also distinguishes an installed official distribution, a build from
a local `MsdialWorkbench` source tree, and a custom executable path. **Check
official releases** reports stable and prerelease Console channels separately.
**Build and select local source** runs `dotnet build` and writes
`msdial-console-build-provenance.json` beside the binary. That record contains
the Git commit, dirty-tree status, build command, timestamp, and binary SHA-256,
so private developer builds are not mistaken for published releases. It also
holds an inventory of every file beside the binary, with the ProductVersions of
the key assemblies (RawDataHandler.dll, Common.dll, MsdialCore.dll and the other
MS-DIAL assemblies), so a dependency swapped after the build inspects as
`stale_mismatch` naming the changed files. A record written before inventories
still verifies, with the warning `inventory_not_recorded`;
`scripts/record-console-inventory.py <MSDIALCUI.exe> --confirmed` adds the
inventory to it without a rebuild.
**Fetch and compare source** updates only the Git remote-tracking references and
reports how far the current checkout is ahead of or behind `origin/master`; it
does not switch branches, merge, or discard local changes. The build action
always builds the current checkout, including explicitly reported local edits.

**Load template** parses the selected MS-DIAL `Key: Value` parameter file and
applies supported Guided setup and Annotation values. For Lipidomics,
`Searched lipid class` controls the checked lipid queries. When a Lipidomics
template has no explicit list, all currently available `LbmQueries.txt` rows
are selected.

## Official library downloads

The Annotation screen provides an on-demand catalog for the official
MS-DIAL libraries. Downloads are not started automatically because the LBM
library alone is about 785 MB. The app downloads only the selected library,
checks its Zenodo MD5 digest, and keeps it in a per-user data directory outside
the replaceable app package. A downloaded library can then be applied directly
to the MSP or LBM annotation row. Zenodo record URL, filename, checksum,
license, and local path are retained in `workflow-settings.json` as library
provenance for future Materials and Methods generation.

Catalog records:

- [Metabolomics positive MSP](https://zenodo.org/records/21901200)
- [Metabolomics negative MSP](https://zenodo.org/records/21904103)
- [Lipidomics LBM](https://zenodo.org/records/21904324)
- [GC-MS Kovats RI MSP](https://zenodo.org/records/21910638)
- [GC-MS Fiehn RI MSP](https://zenodo.org/records/21910646)

### Tiered LC-MS annotation

The Annotation screen can generate an auditable three-tier LC-MS annotation
profile from one LBM path and one MSP path:

1. LBM rule-based lipid annotation, priority 3
2. `msp_high_quality`, priority 2, 0.05 Da MS/MS tolerance
3. `msp_low_quality`, priority 1, 0.25 Da MS/MS tolerance

The same MSP file is referenced by both rows but loaded once by MS-DIAL Console.
Each row records its own `target_omics`, tolerance, thresholds, annotator ID, and
evidence tier in `msp_annotator_settings.tsv`. A lower-priority MS/MS match outranks
a higher-priority precursor-m/z-only suggestion. The broad third tier is retained
as a tentative candidate source and should not be reported as equivalent to a
high-quality spectral match. Private MSP files remain at their original local
paths and are never bundled or redistributed by the app.

LBM score cutoffs default to the laboratory template's 0.1 (weighted, simple
and reverse dot product) for every run, laboratory and repository alike; a
parameter template that states others is followed.

## Laboratory lipidomics

A laboratory batch is two raw folders (positive and negative), the laboratory's
LBM2 library, the salt in the mobile phase, a text library of internal standards
per polarity, and `quant_utility_table.xlsx` (sheet 1 the standards and their
amounts, sheet 2 which standard quantifies which class, sheet 3 which class and
adduct each polarity keeps). Five MCP tools take it from the folders to
`lipid_metabolome.csv`, each step writing only once the person has confirmed it.
The Agent Skill `skills/msdial-lipidomics-workflow` walks Claude through them;
[`docs/tutorials/lab-lipidomics.md`](docs/tutorials/lab-lipidomics.md) is the
step-by-step tutorial with the public demo data, and
[`examples/lab-lipidomics/quant_utility_table.xlsx`](examples/lab-lipidomics/README.md)
the demo's workbook, to start your own from.

1. `msdial_lipidomics_check_inputs` reads everything and changes nothing. With
   the folders alone it lists what is there and every question still open (what
   the analysis is for, the LBM2, the salt, the annotation RT tolerance, the
   internal-standard libraries, the workbook and its amount column, where to
   write, whether the raw folders are the data as acquired or a copy, since
   MS-DIAL writes its intermediates beside them, and the Console). With the
   answers it checks each input and the inputs against each other: the salt
   against the adducts the standards and sheet 3 use, the classes each polarity
   can only leave NOT QUANTIFIED, no output folder in or over a raw folder, and
   one sample table proposed for both polarities. A sheet 3 that contradicts the
   merge's positive/negative reconciliation (BMP or EtherPE selected in positive
   mode, or BMP or EtherPE `[M-H]-` not selected) is a question,
   `merge_selection_accepted`: the person changes sheet 3 before the contract, or
   keeps it, and the contract records that. Once the contract holds the workbook,
   changing it needs a new contract and new runs.
2. `msdial_lipidomics_write_contract` writes `<root>/inputs/`: the contract, the
   internal-standard libraries in the column order MS-DIAL reads, the standards
   template and the selection rules. It is refused if an input, the raw folders
   included, changed after the check the person saw; a check filled from a
   workset is written with the same `workset_id`. A contract already at the root
   is shown in the preview, with what was done under it, and is replaced only
   when the preview itself was asked with `replace_existing`. Each polarity is
   then planned with `msdial_guided_analysis_plan` and
   `lab_lipidomics_contract_path`; the contract fixes the salt, the LBM2, both RT
   tolerances, the internal-standard library (searched in the same run as a
   Text-DB annotator with retention time on), the sample table, the output and
   export folder, the Console, and Interactive's laboratory parameter template
   and LBM query list (by sha256, whatever the shared settings or a workset
   name), and states the method (`TimeBasedLinearWeightedMovingAverage`,
   automatic RT correction with 12 anchors and the 1.5 min local window, the
   annotation candidate table, QA). Neither the answers, a workset nor
   `workflow_overrides` can change what it fixes; any other answer is applied and
   reported as a deviation. The one-file diagnostic is refused, as the run is,
   for any problem but the missing threshold. Each run records the contract's
   sha256 beside it.
3. `msdial_lipidomics_resolve_standards` decides which aligned spot is each
   standard: at the m/z of the adduct the library names, an LBM annotation of
   that standard first (out to the annotation RT tolerance, or the
   internal-standard tolerance where that is wider, one inside the
   internal-standard tolerance before a farther one), then the standard's own
   text row as a reference match, then a hidden or below-cutoff text candidate or
   a plain search, which need the person's acceptance; the most intense spot over
   the sample and QC files within the first tier that has one. Every candidate is
   listed, and the person may take another valid one instead.
4. `msdial_lipidomics_normalize` writes the standards table with each chosen
   alignment ID (or the spot the person took, `accept_spot`) and runs
   `MSDIALCUI normalize --peak-ids-only`, so a standard the resolver rejected is
   never looked up by name. Every quantified cell is then recomputed from the
   Height matrix the same call wrote. A Console without `--peak-ids-only` is
   refused; one rebuilt since the contract is said, and its build recorded.
5. `msdial_lipidomics_merge` writes what the laboratory's own program
   (metaboservice) wrote, into `<root>/quant/lipidome`, the folder the
   laboratory's `metabolomics_service.R` is run in: `lipid_metabolome.csv`,
   `lipid_metabolome4stat.csv`, `lipid_metabolome4classsummary.csv`,
   `lipid_metabolome4summaryplot.csv` (always these names, which the R script
   reads; over the Sample and QC columns, never a Blank) and the
   `Summary_Dotplots`, `category_*` and `subclass_*` folders; ASCII, CRLF, comma
   separated and unquoted (UTF-8, warned about, where a cell is not ASCII and
   metaboservice wrote `?`), rows in the order .NET sorted them on Windows,
   numbers as .NET Framework printed them. Rows are chosen as metaboservice chose
   them: names, the Comment's `x` and the rule flag `TRUE` compared as written,
   untrimmed and case-sensitive. Before sheet 3 selects,
   the laboratory's positive/negative reconciliation renames a negative PG row
   that positive mode annotates as BMP, and a negative PE O- row that positive
   mode annotates as PE P- (same name after the class substitution, within
   0.1 min); the report lists every pair, every positive row left unmatched,
   every choice among several candidates, every positive row that lost all its
   candidates to closer ones (the claims are settled together, so a positive row
   that loses one candidate takes its next), and every renamed row whose own name
   positive mode also annotates. The dotplot categories are metaboservice's own
   (the converter it was built with, not current MS-DIAL's). Selected rows with no valid concentration go to
   `lipid_metabolome_not_quantified.csv`, and everything is reported in
   `merge-report.json`. It never writes over its own side file or report, or a
   file normalize wrote, and replaces only the dotplot files its last merge
   wrote.

Outside a contract, LC-MS lipidomics with the laboratory's own libraries is the
same profile: the salt, the LBM2 (an `.lbm2` with content), the annotation RT
decision, the output folder and the analysis purpose are required questions.
`sample_table` answers the Class confirmation with corrections; `false` alone no
longer does. The contract and its record name the LBM2 where it is kept and are
never part of a bundle. `msdial_save_workset` with `lab_lipidomics_contract_path`
saves the fixed inputs for the next batch's check.

## Publication reporting

The **7. Publication report** workspace generates an English Materials and
Methods draft, a separate QA Results draft, an Excel supplementary workbook, a
long-format supplementary TSV, and a machine-readable JSON audit file. The
complete reporting bundle contains all five files and, when the report is made
for a job with QA, the run's QA figures under `qa_figures/` (drawn first unless the
figures beside the run were drawn by this version from the same QA matrix and
RT-correction audit). The two manuscript drafts
remain editable in the browser and can be copied or downloaded after editing.

The Excel workbook is the primary human-readable supplementary output:

- **Data** reproduces the MS-DIAL analysis-file CSV as a sample-by-field matrix.
- **Guided setup** lists processing settings as sectioned Field/Value rows.
- **Annotation** groups MSP, text, and LBM annotators and presents selected
  adducts, lipid queries, and library provenance as compact tables.
- **Quality assurance** records observed metrics and prespecified criteria.

The long-format TSV remains available as an audit-friendly machine-readable
companion.

## Public repository reanalysis

An experimental command-line workflow can discover and stage lightweight,
untargeted GC-MS or LC-MS projects from Metabolomics Workbench, MetaboLights,
MB-POST, and MetaboBank. MB-POST (`MPST...`) and MetaboBank (`MTBKS...`) use
separate repository adapters. Candidate selection is reproducible with a recorded random seed.
Downloads are size-bounded, checksummed when the repository supplies a digest,
and extracted with archive traversal and expansion limits.

Raw data are managed as a temporary download lease. The cleanup command remains
locked until the retained mzTab-M output passes validation. Provenance,
workflow settings, QA, publication-report artifacts, and checksums are kept
after raw-data cleanup. See [Public repository reanalysis](docs/public_repository_reanalysis.md)
for commands, eligibility rules, and the pilot validation record.

MS-DIAL reads mzML but has no mzXML/mzData reader. Repository records that name
those legacy formats are blocked as `requires_conversion` before download and
must be converted to mzML through a separately reviewed conversion workflow.
Only in a campaign, where a campaign authorization covers the unit, is an
mzXML converted by the download lease itself: its `convert` stage writes each
of the unit's mzXML as mzML under `raw\converted` with Interactive's own
converter (`msdial_app.mzxml_conversion`, every inference flag off but one),
records each conversion in `input_conversions` and
`provenance\input-conversions.json`, and runs what converted. The one
inference: a scan whose mzXML records no polarity is given the unit's declared
ion mode, and only where the unit's Catalog handoff declares exactly one
polarity (`technical_settings.ion_mode` Positive or Negative); the declaration
and the field it was read from are recorded with the options, and each
imputation as an inference with its count. A unit declaring Both or Unknown
imputes nothing. A file some of whose scans record the other polarity and some
none contradicts the declaration: the converter refuses the imputation for it,
and the file is excluded unconverted with reason
`polarity_contradicts_declaration`, the refusal kept in `input_conversions`'
`polarity_contradictions`, and the rest of the unit runs. A file whose every
scan records the other polarity is converted as it records it, and the
raw-header preflight splits the unit by polarity. A file whose conversion
fails is excluded with reason `conversion_failed`. Either exclusion is listed
in the unit's `excluded_input_candidates` and `campaign_disposition`, and the
rest of the unit runs; a full disk, or a file another process holds, instead
stops the lease, so that the unit is retried. Where the unit's candidates hold
several encodings of one sample (copies in other folders, a shared archive's
encoding of a named sample and two declared copies of one row's file included),
the user's one encoding rule of 2026-10-09 uses exactly one readable file,
vendor -> mzML -> mzXML (`.cdf`, `.abf` and `.ibf` between vendor and mzML), a
tie to the first path without case, an unreadable one giving way to the next (an
undecodable mzML, a failed conversion, a vendor header the configured extractor
cannot read, a listed folder that did not arrive whole), as that sample's own
input; every other is on record in `encoding_choices` with its reason. Inside
one declared per-sample archive (MB-POST's per-sample zips) the folders are
copies too (the user's decision of 2026-10-15): one is used, and the others are
named in `declared_archive_inputs` (`copies_unused`) and warned of; two
polarities' folders, folders one of which may state a polarity by a mark the
rule does not read (`Posi/` and `Nega/`, `ESI+/` and `ESI-/`), or two stems, in
one such archive are still refused. A unit whose declared archives, seen on
disk, hold files of no format MS-DIAL opens ends its lease with an exclusion
(`download_failure.disposition`, `retryable` false), which a campaign does not
retry. mzData, which nothing converts, excludes its unit there too.
Raw-header results reported as `Mixed` can be split, after explicit confirmation,
into independent acquisition-mode units with `msdial_split_repository_unit`; the
Mixed parent is never sent to MS-DIAL.

The Data tab also includes a repository metadata handler. It normalizes
Metabolomics Workbench, MetaboLights, MB-POST, and MetaboBank sample metadata, previews an
ordered multi-field grouping hierarchy, and writes the resulting underscore-
separated value into MS-DIAL's single `Class` column. Reviewed source metadata
remain available as JSON/TSV, and the same workflow is callable from the CLI or
local agent API.

Repository downloads show declared size, a 100-Mbps planning estimate, measured
transfer speed, byte progress, and ETA. After recognition, repository Class,
file type, DDA/SWATH/AIF acquisition, batch, and analytical order can be applied
automatically or manually. Internal-standard statements are surfaced in the QA
tab. When an LLM endpoint is explicitly configured, the app can draft reviewable
m/z/adduct targets; unsupported retention times remain blank instead of being
invented.

When `workflow-settings.json` exists in the selected run/output directory, the
report uses those saved run settings instead of the current UI. New runs record
both the MS-DIAL Console version and MS-DIAL Interactive version in
`workflow-settings.json` and `run-manifest.json`. A version missing from an old
run is reported as `not recorded`; the current application version is not
silently assigned to historical processing.

For a selected analysis job, mzTab-M validation, QA, and publication reporting
use only files created or updated by that job. The GUI displays the job ID,
timestamp, output directory, and artifact counts. Archived results can still be
selected manually, but they are visibly identified as manual inputs.

LC-MS QA matrix export requires a Console build containing the QA exporter.
Guided setup checks detected Console candidates for this support and probes
top-level RT correction through the command's standard `--help` interface. It
does not depend on a separate, partial Console feature inventory. If export was
requested but no `*.qa.tsv` was produced, the completed job carries an explicit
warning even when the Console exit code is zero.

Supplementary Table S1 includes:

- analysis-file metadata from **1. Data**
- workflow and processing settings from **2. Guided setup**
- annotation rows, selected adducts/lipid queries, and library provenance from **3. Annotation**
- software versions and LC-MS QA metrics, criteria, and pass/review outcomes

The default QA reporting criteria are visible and editable before generation.
They are transparent report defaults, not universal acceptance criteria. The
Methods draft describes how QA was assessed and how many evaluable criteria
were met; the separate Results draft reports observed values and identifies
criteria requiring review.

### QA figures

QA for a job also draws the run's QA figures into `<run directory>/qa_figures/`,
with the Python standard library only: aligned features with a detected peak,
summed peak height and the peak-height distribution per injection, a PCA of the
study injections from the reference-matched features (log10(height + 1),
autoscaled, Blank excluded), the sample-to-Blank ratio per feature, and the
automatic RT-correction anchor offsets per injection. Each is an SVG file, and
`qa_report.html` shows them all on one self-contained page with a caption and a
note on how to read each. `figures.json` records the QA matrix they were drawn
from by name and sha256; the same inputs give the same bytes. A figure the run
cannot support (no Blank, no RT-correction audit, fewer than three study
injections) is skipped with its reason, and a figure that fails is a warning: the
QA report and its status are never changed by the figures. The tools return the
figures by path (`qa_figures`), never as image data, so an agent shows the page
rather than drawing its own. A laboratory lipidomics batch adds the internal
standards and the lipid class totals (see the tutorial).

Catalog libraries carry a Zenodo DOI, record URL, checksum, and license. For a
user-supplied library, the Publication workspace requests a version and DOI or
repository URL. Missing persistent identifiers produce a warning. Local
absolute paths remain in the supplementary TSV for auditability and must be
reviewed before public release.

## Common peak picking

Peak detection includes the MS-DIAL smoothing method used by all executable
project types. The default is `LinearWeightedMovingAverage`. Available values
are:

- `SimpleMovingAverage`
- `LinearWeightedMovingAverage`
- `SavitzkyGolayFilter`
- `BinomialFilter`
- `LowessFilter`
- `LoessFilter`
- `TimeBasedLinearWeightedMovingAverage`

The companion MS-DIAL Console parser should accept the same enum values. The
development checkout includes a parser fix for this.

## GC-MS retention index

The GC-MS workflow supports:

- RT-only processing
- RI calculation using alkanes or FAMEs
- RI use for MSP annotation scoring/filtering
- RI use for alignment
- RI use for both annotation and alignment

When RI is used, the UI can either reference an existing per-file RI dictionary
or generate one in the run folder from a single alkane/FAME carbon-number to
RT table. The generated file is named `ri_dictionary_paths.txt` and is included
in the reusable workflow ZIP.

## LLM and agent settings

The `LLM & agent settings` screen separates two connection directions:

- an in-app LLM connection lets web-page actions call Azure OpenAI, an
  OpenAI-compatible cloud endpoint, or a loopback OpenAI-compatible local model
  server such as Ollama or LM Studio;
- a desktop agent connection lets Claude Desktop, ChatGPT/Codex, or another MCP
  host operate MS-DIAL Interactive. Authentication is handled by the desktop
  host, so no model API key is entered in this web page.

A desktop subscription cannot be reused as an inference API by the browser
app, and the browser cannot call an existing desktop chat session. MCP solves
the inverse problem: the desktop agent calls MS-DIAL Interactive. API keys
entered in the UI remain in browser memory and are sent to the localhost Python
server only for the current request; they are not written to disk or included
in the workflow context. Keyless local-model connections are restricted to
`localhost`, `127.0.0.1`, or `::1`.

An API key is not used for repository metadata inspection, local QA-card
retrieval, MS-DIAL execution, QA, or mzTab-M processing. Repository metadata are
read directly from the public Metabolomics Workbench, MetaboLights, MB-POST, or MetaboBank
API. When a GPT/Claude desktop app uses the local MCP server, model
authentication remains the responsibility of that desktop app rather than this
web form.

## LC-MS parameter tuning

The Tune parameters screen runs one representative file with:

- `Minimum peak height: 0`
- the selected `Mass slice width`
- all five MSP cutoffs set to zero
- alignment disabled

The generated ASCII `.mdpeak` file is parsed locally. The peak-height and MSP
sliders then recalculate detected-peak and passing-annotation counts without
rerunning MS-DIAL. The summary separates precursor-mass MSP reference
candidates from candidates with non-negative MS/MS score fields; MS-DIAL
exports `-1` for matched-peak fields when no usable MS/MS comparison exists.
Each MSP slider is paired with a numeric input for exact threshold entry.
After a diagnostic run, **Auto-select for 3,000-6,000 peaks** chooses a stepped
threshold from the observed height distribution. It uses 100-unit steps for
QTOF-type data and 1,000-unit steps for Fourier-transform data (Orbitrap-class
and FT-ICR instruments, read from an mzML's header as well as from a Thermo
.raw); a zero-threshold count at or below 6,000 keeps the threshold at zero.
Within the range it takes the highest threshold whose estimated count is still
at least 3,000, the lower end of the range. Only when no such step lands in
3,000-6,000 does it fall back to 10-unit (QTOF-type) or 100-unit
(Fourier-transform) steps, and it never goes finer. The family step is always
searched first, whatever step is requested; a requested step is recorded
(`requested_threshold_step`) and never searched in its place, and a diagnostic
recorded by an earlier version has its family read again.
The estimate records the step used (`threshold_step`), the family step
(`coarse_threshold_step`), `step_fallback`, `fallback_reason`,
`within_target_range` and `selection_rule`; a threshold that misses the range
even at the fine step is the nearest one, with a warning. A repository run
records every file's actual peak count beside the estimate
(`production_peak_counts` in the unit manifest). Agent-driven repository
analysis selects a QC nearest the run midpoint, or a mid-run non-blank sample
when no QC is available. The agent-driven LC-MS diagnostic
(`msdial_start_peak_count_diagnostic`) loads no annotation library: only its
peaks and their heights are read, annotation changes neither, and its
provenance records annotation `skipped_for_peak_count`. Its `.mdpeak` Adduct,
Isotope and MS1 isotopes columns are not the production run's. The GUI
diagnostic above still annotates, because its MSP sliders read the match scores.
Suggested starting values are:

- Thermo RAW or FT-ICR: peak height `10000`, mass slice `0.05`
- QTOF including Waters, Agilent, and Bruker: peak height `100`, mass slice
  `0.1`

Public repository answer seeds use
`TimeBasedLinearWeightedMovingAverage`. It is designed to preserve the weighted
moving-average behavior while accounting for irregular scan intervals, at a
modest additional computational cost. Other Interactive workflows keep their
selected template method.

For large LC-MS Console jobs, the Peak detection and alignment panel can write
`Alignment light mode: True` to `method.txt`. This uses the experimental
MS-DIAL Console light alignment path for text-export workflows and skips GUI
project serialization.

The same panel can enable automatic alignment-only RT correction. MS-DIAL keeps
peak picking and annotation on the original RT axis, learns distributed anchor
features after those steps, chooses a reference file automatically or by file
ID, and applies a piecewise-linear map only during alignment. Blank models can
be interpolated by analytical order. This mode is mutually exclusive with the
user-defined RT-correction workspace and writes summary and anchor audit TSVs.
In **5. Validate & run**, the **Automatic alignment RT correction audit** panel
reads those TSVs, displays accepted/rejected anchors and per-file RT maps, and
can extract one original-raw MS1 EIC on demand. See
[the audit viewer guide](docs/automatic_alignment_rt_review.md) for interpretation
and the boundary between completed-run review and pre-alignment approval.

## LC-MS retention-time correction

The main app and the dedicated review workspace have separate roles. The main
app (`/`) configures and runs the complete MS-DIAL analysis. Its Guided setup
contains a launcher and the production-run enable switch. The dedicated
workspace (`/rt-correction`) performs anchor detection and review:

1. Select an anchor library in the MS-DIAL text-library format.
2. Browse reads the original anchor library into the editor without copying or
   renaming it. Edit the anchor name, target RT, RT tolerance, target m/z, m/z
   tolerance, minimum height, and inclusion flag when needed. Only **Save edited
   anchor library** creates a timestamped file such as
   `MTcorrection_anion_20260810-154230.txt`; the source remains unchanged.
3. Run **Extract EICs and detect anchors**. The app calls the top-level
   `rtcorrection` command in current MS-DIAL Console builds. For compatibility,
   it falls back to the earlier `eic rtcorrection` command when needed.
   The Console is given a copy of the parameter template,
   `rt_correction_method.txt` in the output folder, so the key record it writes
   beside its method file (`rt_correction_method.keys.json`) stays there too.
   Choose automatic peak selection by highest intensity, closest reference RT,
   or a weighted combination. The RT weight ranges from `0` (intensity only) to
   `1` (RT proximity only).
4. Review the overlaid Original and Corrected EICs. Each chart is limited to
   `Target RT +/- RT tolerance`. EIC intensity is smoothed with MS-DIAL's
   `LinearWeightedMovingAverage` setting (default level 3, +/-3 points).
5. Edit `Selected RT` or clear `Use` for an incorrect anchor, then use **Save
   approved peak selections**. This TSV is the explicit file-by-anchor decision
   table consumed by the Console. The optional existing-selection input is only
   for reopening a previous review and rechecking its corrected EICs.
6. Return to the main analysis. The app carries the data paths, Console paths,
   anchor library, and approved selection TSV back to `/`. The production-run
   RT-correction switch becomes available only after both files are present.

The Console detects anchors with the same Core process used by the GUI, writes
one `.rtc` warping file per analysis file, and applies corrected RT values before
normal peak detection. This requires a Console build containing the
`rtcorrection` and LC-MS RT-correction changes; older stable binaries do not
provide this command.

For a focused, cross-platform review tool, start the same backend directly in
RT correction mode:

```powershell
.\scripts\start-rt-correction-windows.ps1
```

```bash
./scripts/start-rt-correction-linux.sh
```

On macOS, open `scripts/start-rt-correction-macos.command`. These commands open
`http://127.0.0.1:8765/rt-correction`, which presents only Data, paths, and the
RT correction review workflow. It shares the same backend and file formats as
the main app, while keeping review and production-run responsibilities distinct.

## Reusable Console workflow

Prepare, Run, and Export reusable workflow generate a ZIP containing:

- `analysis_files.csv` with original raw-data paths and sample metadata
- the final `method.txt`, including values applied from Tune parameters
- `workflow-settings.json` and `run-manifest.json`
- `command.txt`
- `run-msdial.ps1` and `run-msdial.sh`
- `REPRODUCE.txt`, including `vim method.txt` and launch examples
- the RT-correction anchor library and reviewed selection TSV when enabled

The bundle contains no raw data.

## Agent Skill and MCP integration

MS-DIAL Interactive combines two complementary layers:

- the Agent Skills define the scientific interview, confirmation boundaries,
  QA interpretation, and publication workflow;
- the local MCP server inspects data paths, runs MS-DIAL, reports job state,
  validates mzTab-M, generates QA, and creates publication artifacts.

Two skills are bundled under `skills/`:

- `msdial-guided-analysis`: guided LC-MS and GC-MS analysis of a local folder or a
  public repository accession, through mzTab-M, QA and publication artifacts;
- `msdial-lipidomics-workflow`: a laboratory lipidomics batch with internal standards,
  from two raw folders to `lipid_metabolome.csv` (see
  [the tutorial](docs/tutorials/lab-lipidomics.md)).

Raw data stay on the user's PC. Guided execution currently supports LC-MS and
GC-MS. Five built-in worksets cover GC-MS metabolomics and positive/negative
LC-MS metabolomics/lipidomics; accepted choices can be saved as user worksets.

The one-command setup does everything below for Claude Code and Claude Desktop, and
asks before each change:

```bash
python3 scripts/setup_claude.py            # Windows: double-click "Setup MS-DIAL for Claude.cmd", or py -3
python3 scripts/setup_claude.py --dry-run  # the plan only; nothing is changed or started
```

It checks Python (3.10+), installs the MCP dependency when it is missing, registers the
MCP server with Claude Code at user scope (`claude mcp add --scope user`; a different
existing entry is saved to `~/.claude/mcp-backups` first) and in Claude Desktop's
`claude_desktop_config.json` (after a timestamped backup, other keys left as they are),
copies both skills into `~/.claude/skills` (a differing copy is backed up to
`~/.claude/skill-backups` first), builds the skill ZIPs for Claude Desktop, and says
whether the MS-DIAL Consoles it finds are new enough for laboratory lipidomics, by what
their assemblies hold.

Install the optional MCP dependency:

```bash
python -m pip install -e ".[mcp]"
```

Run the MCP server:

```bash
python scripts/msdial-interactive-mcp.py
```

Core MCP tools:

- `msdial_interactive_launch`: start the local web app if needed
- `msdial_interactive_restart`: replace a recognized incompatible local app
- `msdial_check_console_path` / `msdial_set_console_path`: discover, inspect, and persist the Console path
- `msdial_check_official_console_releases`: compare stable and prerelease Console channels
- `msdial_check_local_console_source`: fetch and compare the checkout with `origin/master`
- `msdial_build_console_from_local_source`: preview or explicitly confirm a local-source build with provenance
- `msdial_guided_analysis_plan`: inspect input and return the next question
- `msdial_list_worksets` / `msdial_save_workset`: reuse scientific choices
- `msdial_start_peak_count_diagnostic`: tune from one representative file
- `msdial_prepare_guided_analysis`: write and validate reproducible inputs
- `msdial_start_guided_analysis`: execute only after explicit confirmation
- `msdial_interactive_status`: check queued/running/completed jobs
- `msdial_interactive_wait_for_completion`: wait for one specified job to finish
- `msdial_cancel_job`: stop a queued or running run, diagnostic, or repository download
  (the two start tools also take `timeout_seconds` and `idle_timeout_seconds`; none by default)
- `msdial_interactive_validate_mztab`: validate mzTab-M outputs
- `msdial_generate_lcms_qa`: build the LC-MS QA report and, for a job, draw its QA
  figures (SVG and a self-contained `qa_report.html`) into `<run directory>/qa_figures`
- `msdial_generate_publication_report`: create text, Excel, audit, and ZIP files
- `msdial_complete_guided_analysis`: wait for one job and complete validation,
  QA, publication, and handoff without browser interaction
- `msdial_lipidomics_check_inputs` / `msdial_lipidomics_write_contract`: check a
  laboratory lipidomics batch's inputs (read-only) and fix them in a contract after
  explicit confirmation
- `msdial_lipidomics_resolve_standards` (read-only) / `msdial_lipidomics_normalize` /
  `msdial_lipidomics_merge`: find the internal standards in each run, normalize with
  `--peak-ids-only` and verify every cell, and write `lipid_metabolome.csv` with the
  tables and dotplot folders the laboratory's R script reads; normalize and merge
  preview by default and act only with `confirmed=true`
- `msdial_interactive_create_handoff`: create `datamining-handoff.json`
- `msdial_inspect_repository_metadata`: inspect public sample metadata and publication provenance
- `msdial_project_repository_classes`: project a user-selected metadata hierarchy into MS-DIAL `Class`
- `msdial_save_repository_metadata`: save reviewed JSON/TSV and optional analysis metadata CSV
- `msdial_repository_batch_plan`: split Catalog handoffs into independent analysis-unit runs
- `msdial_repository_reanalysis_plan`: plan an accession or Catalog analysis unit without downloading data
- `msdial_download_repository_raw`: start a bounded repository download after explicit confirmation
- `msdial_repository_raw_metadata_preflight`: cross-check every raw header by default with the local parser
- `msdial_check_raw_metadata_extractor` / `msdial_set_raw_metadata_extractor_path`: inspect and select the raw-metadata extractor
- `msdial_split_repository_unit`: split a Mixed acquisition unit after explicit confirmation
- `msdial_prepare_repository_reanalysis`: review Class matching and prepare `analysis_files.csv`
- `msdial_repository_qa_evidence`: expose internal-standard declarations for agent-reviewed QA targets
- `msdial_cleanup_repository_raw`: preview and explicitly confirm post-validation raw-data deletion, a split parent's release included
- `msdial_discard_repository_raw`: preview and explicitly confirm deletion of raw data that produced no validated output
- `msdial_download_store_status`: show the accession download stores (objects, claims, partial transfers), read-only
- `msdial_restore_project_for_viewing`: preview and explicitly confirm reopening a finalised campaign unit's project in the MS-DIAL GUI (containers copied back, library copy linked from the store, unit held)
- `msdial_undo_project_restore`: preview and explicitly confirm taking such a restore back

Agent API 0.5 binds mzTab-M, QA, publication, and handoff operations to the
production `job_id`. Files left by earlier runs in the same output directory are
excluded. Job summaries are compact by default; full details are opt-in.

Claude Desktop example:

```json
{
  "mcpServers": {
    "msdial-interactive": {
      "command": "C:\\path\\to\\python.exe",
      "args": [
        "C:\\path\\to\\msdial-interactive-app\\scripts\\msdial-interactive-mcp.py"
      ]
    }
  }
}
```

Use the actual Python path on that PC. Restart Claude Desktop after editing its
configuration. The setup script writes this entry for you; Claude Code's equivalent
is `claude mcp add --scope user msdial-interactive -- <python> <checkout>/scripts/msdial-interactive-mcp.py`.

If a newly added control reports `Unknown endpoint`, first refresh the browser
and confirm `/api/config` reports the current app version. An older local app
may still be using port 8765. Current builds use an exclusive port binding and
exit with a clear message instead of allowing old and new servers to share the
same port.

Package the cross-platform Agent Skills:

```bash
python scripts/package-agent-skill.py                                   # every skill
python scripts/package-agent-skill.py --skill msdial-lipidomics-workflow  # one skill
```

Each skill becomes `dist/<name>.skill.zip` (`dist/msdial-guided-analysis.skill.zip`
and `dist/msdial-lipidomics-workflow.skill.zip`), with the skill's folder at the
root. Upload them from Claude's `Customize > Skills` screen, or install the folders
under `skills/` in another Agent Skills-compatible host (Claude Code reads
`~/.claude/skills/<name>/SKILL.md`). The format follows the open Agent Skills
standard. See [`docs/agent_integration.md`](docs/agent_integration.md) for setup,
tool flow, and test prompts.

Example prompts:

```text
Analyze the mass-spectrometry data in <folder> with MS-DIAL, and guide me through
mzTab-M validation, QA and the Materials and Methods.
```

```text
Run the laboratory lipidomics workflow. Positive raw files: <folder>\pos.
Negative raw files: <folder>\neg. Look at the folders first, then ask me everything
you need in one message.
```

## Literature-based starting parameters

When an Azure OpenAI or OpenAI-compatible API configuration is active, the Ask
screen can search Crossref for explicitly licensed open-access MS-DIAL studies.
Candidates show citation count, direct parameter terms found in deposited
title/abstract metadata, and a confidence label. Citation counts are Crossref
`is-referenced-by-count` values. The LLM is instructed not to
invent numeric parameters; when detailed settings are absent, the app retains
its instrument-format defaults. Suggestions are never applied automatically.

## Local distribution model

The lab trial model described here is **one local instance per user PC**.
Each user downloads or receives the app ZIP, launches it on their own Windows,
macOS, or Linux machine, and opens `http://127.0.0.1:8765`.

This is different from a shared lab server. In local mode, raw data paths are
resolved on the same PC that owns the browser session, so users can select
their own local files without first uploading data to a server.

## Run on a user PC

Python 3.10 or newer is required.

### Windows

```powershell
cd C:\path\to\msdial-interactive-app
.\scripts\start-local-windows.ps1
```

Users can also double-click `scripts\start-local-windows.cmd`.

Optional Console path:

```powershell
.\scripts\start-local-windows.ps1 -ConsolePath "C:\MSDIAL\MSDIALCUI.exe"
```

### macOS

```bash
cd /Users/<user>/Apps/msdial_interactive_app
chmod +x scripts/start-local-macos.command scripts/start-local-linux.sh
./scripts/start-local-macos.command
```

### Linux

```bash
cd /home/<user>/apps/msdial_interactive_app
chmod +x scripts/start-local-linux.sh
./scripts/start-local-linux.sh
```

Advanced direct launch:

```bash
python app.py --host 127.0.0.1 --port 8765
```

The source ZIP also places launchers at its top level. On Windows, double-click
`Start MS-DIAL Interactive.cmd`; on macOS open
`Start MS-DIAL Interactive.command`; on Linux run or open
`start-msdial-interactive.sh` after granting execute permission once.

## Native packages for users without Python

The GitHub Actions workflow `Build native desktop packages` creates separate
artifacts for Windows, macOS, and Linux using PyInstaller. These are native
per-OS packages, not one universal binary:

- Windows: open `MS-DIAL-Interactive.exe`
- macOS: open `MS-DIAL-Interactive.app`
- Linux: run/open the executable `MS-DIAL-Interactive`

The user does not need to install Python for these artifacts. MS-DIAL Console
itself remains a separate OS-specific dependency selected from the Paths panel.
Build locally with `python -m pip install ".[desktop]"` followed by
`python scripts/build-native.py`.

The Windows artifact contains a ZIP. The macOS and Linux artifacts contain a
`tar.gz` archive so executable permissions survive GitHub artifact download.
Extract the inner archive once, then open the launcher listed above.

See the Japanese user tutorial:

```text
docs/local_user_tutorial_ja.md
```

## Build a distribution ZIP

From the development checkout:

```bash
python scripts/build-distribution.py
```

Output:

```text
dist/msdial-interactive-app-local.zip
```

The ZIP includes the app, resources, QA cards, launch scripts, README, and
docs. It excludes `.git`, `runs`, `work`, `dist`, caches, and raw data.

See the Japanese distribution memo:

```text
docs/distribution_ja.md
```

## License

This project is released under the GNU Lesser General Public License version
3.0. See `LICENSE`, `COPYING`, and `COPYING.LESSER`.

## Optional lab-server mode

Lab-server mode is still available for advanced cases:

```bash
python app.py --lab --port 8765
```

Use this only when raw data, libraries, and output folders are visible from
the server filesystem. It does not fit users who want to
process data stored on their own PCs. Do not expose it directly to the public
internet.

Optional environment variables:

```text
MSDIAL_CONSOLE_PATH       Default Console path shown in the UI
MSDIAL_INTERACTIVE_PORT   Linux helper script port, default 8765
PYTHON_BIN                Linux/macOS helper script Python executable
AZURE_OPENAI_ENDPOINT     Optional in-app LLM / literature evidence search
AZURE_OPENAI_API_KEY      Optional in-app LLM / literature evidence search
AZURE_OPENAI_DEPLOYMENT   Optional in-app LLM / literature evidence search
```

## Knowledge cards

`knowledge/qa_cards_ja.jsonl` and `knowledge/qa_cards_en.jsonl` contain only
small public-safe sample cards in this repository. They are included so that
Local MS-DIAL question retrieval works immediately after checkout. Labs can replace these files with
their own local Q&A cards; private or email-derived cards should not be
committed to a public repository.

The LLM and agent screen works in local retrieval mode without an API key.
Grounded Azure OpenAI answers are enabled when these variables are set:

```text
AZURE_OPENAI_ENDPOINT
AZURE_OPENAI_API_KEY
AZURE_OPENAI_DEPLOYMENT
AZURE_OPENAI_API_VERSION  (optional)
```

## Per-file acquisition type

The app writes `acquisition_type` for every CSV row. Mixed DDA/SWATH/AIF input
requires an MS-DIAL build containing the fix from branch:

```text
fix/console-per-file-acquisition-type
```

Older Console releases overwrite every row with the method-level acquisition
type. The preflight screen warns when multiple acquisition types are present.

## Tests

```bash
python -m unittest discover -s tests -v
```
