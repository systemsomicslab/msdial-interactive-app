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
It remains editable, and both diagnostic and production result folders are
created below that selected location.

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

## Reusable Console workflow

Prepare, Run, and Export reusable workflow generate a ZIP containing:

- `analysis_files.csv` with original raw-data paths and sample metadata
- the final `method.txt`, including values applied from Tune parameters
- `workflow-settings.json` and `run-manifest.json`
- `command.txt`
- `run-msdial.ps1` and `run-msdial.sh`
- `REPRODUCE.txt`, including `vim method.txt` and launch examples

The bundle contains no raw data.

## Agent and MCP integration

MS-DIAL Interactive exposes local agent endpoints and an optional MCP server so
Codex, Claude Desktop, or another MCP host can observe an analysis without
guessing what this app is.

Install the optional MCP dependency:

```bash
python -m pip install -e ".[mcp]"
```

Run the MCP server:

```bash
python scripts/msdial-interactive-mcp.py
```

Typical MCP tools:

- `msdial_interactive_launch`: start the local web app if needed
- `msdial_interactive_status`: check queued/running/completed jobs
- `msdial_interactive_wait_for_completion`: wait for a run to finish
- `msdial_interactive_create_handoff`: create `datamining-handoff.json`
- `msdial_interactive_validate_mztab`: validate mzTab-M outputs
- `msdial_interactive_preview_mztab`: inspect mzTab-M sections, first rows, and
  numeric columns

Claude Desktop example:

```json
{
  "mcpServers": {
    "msdial-interactive": {
      "command": "python",
      "args": [
        "D:\\0_SourceCode\\msdial_interactive_app\\scripts\\msdial-interactive-mcp.py"
      ]
    }
  }
}
```

The MCP server is intentionally a local adapter. Raw data remain on the user PC,
the web UI remains user-in-the-loop, and downstream MCP servers should consume
the generated `primary_mztab_file` or `datamining-handoff.json`.

## Literature-based starting parameters

When an Azure OpenAI or OpenAI-compatible API configuration is active, the Ask
screen can search Crossref for explicitly licensed open-access MS-DIAL studies.
Candidates show citation count, direct parameter terms found in deposited
title/abstract metadata, and a confidence label. Citation counts are Crossref
`is-referenced-by-count` values. The LLM is instructed not to
invent numeric parameters; when detailed settings are absent, the app retains
its instrument-format defaults. Suggestions are never applied automatically.

## Recommended distribution model

The recommended lab trial model is **one local instance per user PC**.
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
the server filesystem. It is not the recommended mode for users who want to
process data stored on their own PCs. Do not expose it directly to the public
internet.

Optional environment variables:

```text
MSDIAL_CONSOLE_PATH       Default Console path shown in the UI
MSDIAL_INTERACTIVE_PORT   Linux helper script port, default 8765
PYTHON_BIN                Linux/macOS helper script Python executable
AZURE_OPENAI_ENDPOINT     Optional Ask MS-DIAL / literature recommendation
AZURE_OPENAI_API_KEY      Optional Ask MS-DIAL / literature recommendation
AZURE_OPENAI_DEPLOYMENT   Optional Ask MS-DIAL / literature recommendation
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
