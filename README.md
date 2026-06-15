# MS-DIAL Interactive App

Cross-platform local web application for building and running MS-DIAL Console
LC-MS workflows.

## Why Python + local web UI

- The same application code runs on Windows, macOS, and Linux.
- MS-DIAL Console executables can be selected per operating system.
- Large raw files stay local. Nothing is sent to an external service.
- The browser UI supports drag-and-drop uploads, native file dialogs, and
  server-side paths.
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

Use drag-and-drop, Browser folder picker, Native folder picker, or a server-side
path. Browser folder uploads preserve the directory tree instead of flattening
the vendor package.

## SCIEX data

Only `.wiff` and `.wiff2` are added as SCIEX analysis files. A `.wiff.scan`
file is transported as the required companion of its matching `.wiff`, but is
never added as a separate analysis row. Other SCIEX sidecars are rejected.
When both `.wiff` and `.wiff2` exist for the same sample, the app asks the user
to choose one and does not add the ambiguous pair.

A `.wiff` file is accepted without displaying a missing-sidecar warning. Native
file selection and local paths retain the original path, so an adjacent
`.wiff.scan` is used implicitly. A web browser does not reveal the original
absolute path or unselected sibling files during a single-file drop. In that
case, the app asks the user to confirm the original WIFF path once instead of
uploading an incomplete copy. Folder drops/uploads already include the sibling
and need no confirmation.

The Local folder picker references one server-visible folder directly. The
Browser folder picker uploads one folder into the app. Both can be used
repeatedly to accumulate multiple folders.

## Project types and adducts

The UI separates LC-MS, GC-MS, DI-MS, LC-IM-MS, IM-MS, and Imaging-MS
parameter modes. LC-MS is the currently executable workflow. Other modes show
their capability boundary and are blocked at preflight until their
mode-specific parameter backend is implemented.

GC-MS hides precursor-adduct, LBM, Text DB, and lipid-query controls. Other
project types provide searched-adduct selection from the MS-DIAL positive and
negative adduct resource tables.

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
rerunning MS-DIAL. Suggested starting values are:

- Thermo RAW or FT-ICR: peak height `10000`, mass slice `0.05`
- QTOF including Waters, Agilent, and Bruker: peak height `100`, mass slice
  `0.1`

## Run

Python 3.10 or newer:

```bash
cd D:/0_SourceCode/msdial_interactive_app
python app.py
```

Open `http://127.0.0.1:8765`.

Options:

```bash
python app.py --host 127.0.0.1 --port 8765 --no-browser
```

## Knowledge reuse

`knowledge/qa_cards_ja.jsonl` and `knowledge/qa_cards_en.jsonl` are the
privacy-scrubbed, distilled public Q&A cards created by the prior
"MS-DIAL email extraction" project. Raw emails are not copied into this app.

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
