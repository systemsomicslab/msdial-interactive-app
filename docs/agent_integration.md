# Agent Integration

MS-DIAL Interactive can be treated as a local, user-in-the-loop analysis
service. A natural-language agent should not bypass the human parameter setup
UI. Instead, it should launch or connect to the local app, observe job state,
and hand completed outputs to downstream MCP tools.

## Recommended Flow

1. User says they want to start a new MS-DIAL analysis.
2. Agent launches `MS-DIAL Interactive` on `127.0.0.1:8765`.
3. User imports files and completes `Validate & Run` in the UI.
4. Agent polls `GET /api/agent/status`.
5. When a run job is `completed`, agent calls `GET /api/agent/handoff?job_id=<id>`.
6. Agent passes `primary_mztab_file` or `mztab_files` to the downstream
   data-mining MCP server.

## MCP Server

The app also provides a thin local MCP server. It does not replace the web UI;
it exposes the UI-backed workflow state as agent tools.

Install the optional MCP dependency:

```bash
python -m pip install -e ".[mcp]"
```

Or, without editable install:

```bash
python -m pip install "mcp[cli]"
```

Run the MCP server from the checkout:

```bash
python scripts/msdial-interactive-mcp.py
```

Available MCP tools:

- `msdial_interactive_status`
- `msdial_interactive_launch`
- `msdial_interactive_open`
- `msdial_interactive_wait_for_completion`
- `msdial_interactive_create_handoff`
- `msdial_interactive_validate_mztab`
- `msdial_interactive_preview_mztab`

Example Claude Desktop MCP configuration:

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

For a managed lab PC, replace `python` with the full path to the intended
Python executable. The MCP server expects the web app at `127.0.0.1:8765` by
default, and the `msdial_interactive_launch` tool can start it when needed.

### Trying From Codex Desktop

In the current Codex task, a newly created local MCP server is not automatically
added to the tool palette while the task is already running. You can still test
the exact MCP server with the SDK client:

```powershell
cd D:\0_SourceCode\msdial_interactive_app
C:\Users\Hiroshi Tsugawa\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pip install -e ".[mcp]"
```

Then run a small MCP client or register this server in a future Codex/Claude
session:

```json
{
  "mcpServers": {
    "msdial-interactive": {
      "command": "C:\\Users\\Hiroshi Tsugawa\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\python\\python.exe",
      "args": [
        "D:\\0_SourceCode\\msdial_interactive_app\\scripts\\msdial-interactive-mcp.py"
      ]
    }
  }
}
```

For persistent Codex Desktop use, package this server as a local Codex plugin
or register the MCP server through the app's MCP/plugin configuration surface
available in that installation. After registration, prompts such as
“MS-DIAL Interactive の解析状態を確認して” can resolve to
`msdial_interactive_status` instead of relying on conversation memory.

For a quick visual/data sanity check, the agent can call
`msdial_interactive_preview_mztab` and summarize the MTD/SML/SMF/SME sections,
abundance columns, annotation columns, and first rows before handing the file to
downstream mining tools.

## Agent Endpoints

### `GET /api/agent/status`

Returns machine-readable app status and recent jobs.

Important fields:

- `latest_job`
- `latest_completed_job`
- `jobs[].status`
- `jobs[].run_directory`
- `jobs[].mztab_status`
- `jobs[].handoff_file`

### `GET /api/agent/handoff?job_id=<id>`

Creates and returns a data-mining handoff document for a completed job. The
same JSON is written to:

```text
<run_directory>/datamining-handoff.json
```

### `POST /api/agent/handoff`

Request body:

```json
{
  "job_id": "optional-job-id",
  "run_directory": "optional-output-folder-or-mzTab-path"
}
```

Use `run_directory` when the agent wants to hand off an already completed
MS-DIAL output folder that was not created in the current app session.

## Handoff JSON

The handoff schema is:

```text
msdial-interactive.datamining-handoff.v1
```

Key fields:

- `analysis_type`
- `run_directory`
- `input_csv`
- `method_file`
- `manifest`
- `command`
- `mztab_validation`
- `primary_mztab_file`
- `mztab_files`
- `msdial_output_files`
- `downstream_mcp_hint`

The downstream MCP server should generally start from `primary_mztab_file`.
When multiple mzTab-M files exist, the agent should ask the user which one to
mine or use the newest file according to the downstream policy.

## Design Principle

The app remains local-first: raw data are not uploaded to a remote service.
The agent reads status and output paths; MS-DIAL Console still runs against the
original local file paths prepared by the UI.
