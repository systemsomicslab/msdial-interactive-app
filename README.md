# MS-DIAL Interactive App

Cross-platform local web application for building and running MS-DIAL Console
LC-MS and GC-MS workflows.

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

## Publication reporting

The **7. Publication report** workspace generates an English Materials and
Methods draft, a separate QA Results draft, an Excel supplementary workbook, a
long-format supplementary TSV, and a machine-readable JSON audit file. The
complete reporting bundle contains all five files. The two manuscript drafts
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

The Data tab also includes a repository metadata handler. It normalizes
Metabolomics Workbench, MetaboLights, MB-POST, and MetaboBank sample metadata, previews an
ordered multi-field grouping hierarchy, and writes the resulting underscore-
separated value into MS-DIAL's single `Class` column. Reviewed source metadata
remain available as JSON/TSV, and the same workflow is callable from the CLI or
local agent API.

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

## LLM settings

The Ask MS-DIAL screen can use local retrieval, Azure OpenAI, or an
OpenAI-compatible chat-completions endpoint. API keys entered in the UI remain
in browser memory and are sent to the localhost Python server only for the
current request; they are not written to disk or included in the workflow
context.

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
Suggested starting values are:

- Thermo RAW or FT-ICR: peak height `10000`, mass slice `0.05`
- QTOF including Waters, Agilent, and Bruker: peak height `100`, mass slice
  `0.1`

For large LC-MS Console jobs, the Peak detection and alignment panel can write
`Alignment light mode: True` to `method.txt`. This uses the experimental
MS-DIAL Console light alignment path for text-export workflows and skips GUI
project serialization.

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

- the Agent Skill defines the scientific interview, confirmation boundaries,
  QA interpretation, and publication workflow;
- the local MCP server inspects data paths, runs MS-DIAL, reports job state,
  validates mzTab-M, generates QA, and creates publication artifacts.

Raw data stay on the user's PC. Guided execution currently supports LC-MS and
GC-MS. Five built-in worksets cover GC-MS metabolomics and positive/negative
LC-MS metabolomics/lipidomics; accepted choices can be saved as user worksets.

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
- `msdial_check_console_path` / `msdial_set_console_path`: discover and persist the Console path
- `msdial_guided_analysis_plan`: inspect input and return the next question
- `msdial_list_worksets` / `msdial_save_workset`: reuse scientific choices
- `msdial_start_peak_count_diagnostic`: tune from one representative file
- `msdial_prepare_guided_analysis`: write and validate reproducible inputs
- `msdial_start_guided_analysis`: execute only after explicit confirmation
- `msdial_interactive_status`: check queued/running/completed jobs
- `msdial_interactive_wait_for_completion`: wait for one specified job to finish
- `msdial_interactive_validate_mztab`: validate mzTab-M outputs
- `msdial_generate_lcms_qa`: build the LC-MS QA report
- `msdial_generate_publication_report`: create text, Excel, audit, and ZIP files
- `msdial_complete_guided_analysis`: wait for one job and complete validation,
  QA, publication, and handoff without browser interaction
- `msdial_interactive_create_handoff`: create `datamining-handoff.json`
- `msdial_inspect_repository_metadata`: inspect public sample metadata and publication provenance
- `msdial_project_repository_classes`: project a user-selected metadata hierarchy into MS-DIAL `Class`
- `msdial_save_repository_metadata`: save reviewed JSON/TSV and optional analysis metadata CSV

Agent API 0.4 binds mzTab-M, QA, publication, and handoff operations to the
production `job_id`. Files left by earlier runs in the same output directory are
excluded. Job summaries are compact by default; full details are opt-in.

Claude Desktop example:

```json
{
  "mcpServers": {
    "msdial-interactive": {
      "command": "C:\\Users\\<user>\\AppData\\Local\\Python\\pythoncore-3.14-64\\python.exe",
      "args": [
        "D:\\0_SourceCode\\msdial_interactive_app\\scripts\\msdial-interactive-mcp.py"
      ]
    }
  }
}
```

Use the actual Python path on that PC. Restart Claude Desktop after editing its
configuration.

If a newly added control reports `Unknown endpoint`, first refresh the browser
and confirm `/api/config` reports the current app version. An older local app
may still be using port 8765. Current builds use an exclusive port binding and
exit with a clear message instead of allowing old and new servers to share the
same port.

Package the cross-platform Agent Skill:

```bash
python scripts/package-agent-skill.py
```

Upload `dist/msdial-guided-analysis.skill.zip` from Claude's
`Customize > Skills` screen, or install the folder
`skills/msdial-guided-analysis` in another Agent Skills-compatible host. The
format follows the open Agent Skills standard. See
[`docs/agent_integration.md`](docs/agent_integration.md) for setup, tool flow,
and test prompts.

Example prompt:

```text
D:\0_SourceCode\MsdialWorkbenchDemo\console_fastlc_demo の質量分析データを
MS-DIALで解析し、mzTab-M検証、QA、Materials and Methods生成まで案内して。
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
cd D:\0_SourceCode\msdial_interactive_app
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
AZURE_OPENAI_ENDPOINT     Optional Ask MS-DIAL / literature evidence search
AZURE_OPENAI_API_KEY      Optional Ask MS-DIAL / literature evidence search
AZURE_OPENAI_DEPLOYMENT   Optional Ask MS-DIAL / literature evidence search
```

## Knowledge cards

`knowledge/qa_cards_ja.jsonl` and `knowledge/qa_cards_en.jsonl` contain only
small public-safe sample cards in this repository. They are included so that
Ask MS-DIAL works immediately after checkout. Labs can replace these files with
their own local Q&A cards; private or email-derived cards should not be
committed to a public repository.

The Ask MS-DIAL screen works in local retrieval mode without an API key.
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
