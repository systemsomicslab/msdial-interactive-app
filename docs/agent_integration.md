# Agent Integration

MS-DIAL Interactive uses an Agent Skill for procedure and a local MCP server for
execution. This matches the intended division: Skills teach a repeatable
workflow, while MCP connects the model to tools and local data. See the official
[Claude Skills overview](https://support.claude.com/en/articles/12512176-what-are-skills)
and [Anthropic MCP documentation](https://docs.anthropic.com/en/docs/mcp). For
ChatGPT and Codex, see OpenAI's
[MCP server documentation](https://developers.openai.com/plugins/concepts/mcp-server)
and [Secure MCP Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).

This connection does not expose Claude or ChatGPT as an API to the browser.
Instead, the authenticated desktop agent calls MS-DIAL Interactive. Therefore a
separate model API key is not needed for agent-driven execution. In-app model
generation remains a separate optional connection under `LLM & agent settings`.
Claude Desktop supports local MCP servers and desktop extensions. ChatGPT custom
MCP support is plan-dependent and may require a remote MCP endpoint or secure
tunnel rather than a direct localhost connection.

## Install the local MCP server

From the source checkout:

```powershell
cd D:\0_SourceCode\msdial_interactive_app
python -m pip install -e ".[mcp]"
```

Use the full Python path in Claude Desktop's `claude_desktop_config.json`:

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

Replace the Python path as needed and restart Claude Desktop. The MCP process
uses stdio and can launch the app backend on `http://127.0.0.1:8765` without
opening a browser. If an older app occupies the port,
`msdial_interactive_status` reports `compatible: false` and
`msdial_interactive_restart` can replace only a recognized local MS-DIAL
Interactive process after explicit confirmation.

## Install the Agent Skill

Build the upload ZIP:

```powershell
python scripts/package-agent-skill.py
```

In Claude Desktop, open `Customize > Skills`, choose `+`, and upload:

```text
dist/msdial-guided-analysis.skill.zip
```

The source skill is in `skills/msdial-guided-analysis`. Claude Code users can
also place that folder under `.claude/skills/`; other compatible agents can use
the same Agent Skills package.

## Guided flow

1. Inspect the input folder or analysis metadata CSV.
2. Ask LC-MS versus GC-MS. Other project types are reported as not yet supported.
3. For LC-MS, ask positive/negative and metabolomics/lipidomics.
4. Use template defaults or run a confirmed single-file peak-count diagnostic.
5. Optionally configure RT correction.
6. Use official versioned, existing, or no annotation libraries.
7. Optionally configure LC-MS QA and internal standards.
8. Prepare the reproducible workflow, show the command, and request confirmation.
9. Run MS-DIAL and call `msdial_complete_guided_analysis` to wait for the exact
   job, validate and preview mzTab-M, generate requested QA/publication files,
   and create the data-mining handoff.
10. Optionally save the accepted scientific choices as a reusable workset.

Downloads and production runs require a separate explicit confirmation. A
workset omits raw-data paths and output paths. An accepted diagnostic threshold
is retained as part of the reusable scientific method.

## Built-in worksets

- `gcms-metabolomics`
- `lcms-positive-metabolomics`
- `lcms-negative-metabolomics`
- `lcms-positive-lipidomics`
- `lcms-negative-lipidomics`

User worksets are stored in the per-user MS-DIAL Interactive data directory.

## Main MCP tools

- `msdial_interactive_launch`
- `msdial_interactive_restart`
- `msdial_check_console_path` / `msdial_set_console_path`
- `msdial_guided_analysis_plan`
- `msdial_list_worksets`
- `msdial_download_official_library`
- `msdial_start_peak_count_diagnostic`
- `msdial_estimate_peak_height`
- `msdial_prepare_guided_analysis`
- `msdial_start_guided_analysis`
- `msdial_interactive_job`
- `msdial_interactive_validate_mztab`
- `msdial_interactive_preview_mztab`
- `msdial_generate_lcms_qa`
- `msdial_generate_publication_report`
- `msdial_complete_guided_analysis`
- `msdial_save_workset`
- `msdial_interactive_create_handoff`

Run completion, mzTab-M validation/preview, LC-MS QA, publication generation,
and handoff should receive the exact `job_id` returned by the production run.
Agent API 0.3 tracks files created or updated by that job and does not silently
reuse older mzTab-M or `*.qa.tsv` files from the same directory. Publication can
be generated without QA by setting `run_qa=false`.

The browser UI uses the same provenance model. Its job history can select a
completed run, after which mzTab-M preview, LC-MS QA, and publication generation
are scoped to that exact job. Guided setup probes
`MSDIALCUI rtcorrection --help` when it needs the top-level RT correction
workflow. LC-MS QA support is checked independently because QA matrix export is
not a standalone Console command.

## Agent endpoints

- `GET /api/agent/status`
- `GET /api/agent/worksets`
- `POST /api/agent/plan`
- `POST /api/agent/worksets/save`
- `POST /api/agent/prepare`
- `POST /api/agent/run`
- `POST /api/agent/tuning/run`
- `POST /api/agent/tuning/estimate`
- `GET|POST /api/agent/handoff`

## Test prompts

Start from scratch:

```text
D:\0_SourceCode\MsdialWorkbenchDemo\console_fastlc_demo にある質量分析データを
解析して。MS-DIALの条件を順番に質問し、実行前に最終確認して。
```

Start from a workset:

```text
lcms-negative-lipidomics worksetを使ってこのフォルダーを解析して。
ピーク数は代表1ファイルで約10000になるよう調整し、QAとMethodsも作って。
```

The final data-mining handoff is written as
`<run_directory>/datamining-handoff.json`. Downstream PCA, UMAP, HCA, summary,
and visualization MCP servers should normally start from `primary_mztab_file`.
