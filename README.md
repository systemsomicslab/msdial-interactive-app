# MS-DIAL Interactive App

Cross-platform local web application for building and running MS-DIAL Console
LC-MS workflows.

## Why Python + local web UI

- The same application code runs on Windows, macOS, and Linux.
- MS-DIAL Console executables can be selected per operating system.
- Large raw files stay local. Nothing is sent to an external service.
- The browser UI supports drag-and-drop uploads, native file dialogs, and
  server-side paths.
- The app has no mandatory third-party Python dependencies.

Proprietary vendor formats may still depend on vendor readers supported by the
selected MS-DIAL Console build. For Linux/macOS workflows, mzML is generally the
most portable input format.

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

