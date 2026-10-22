# Agent Integration

MS-DIAL Interactive uses Agent Skills for procedure and a local MCP server for
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

## Set up in one command

From the source checkout, on Windows double-click `Setup MS-DIAL for Claude.cmd`, or
run (`python3` on macOS and Linux):

```powershell
py -3 scripts/setup_claude.py            # explains each step and asks before each change
py -3 scripts/setup_claude.py --dry-run  # shows the plan; changes nothing and starts no Console
py -3 scripts/setup_claude.py --yes      # answers yes to every question
```

The `.cmd` passes its arguments on, takes `py -3` or else `python` only when it runs
Python 3.10 or newer, and turns off the Python install manager's automatic install, so
that it never installs a Python itself.

It uses only the standard library and needs no administrator rights. Its steps:

1. Python 3.10 or newer.
2. The MCP dependency, `<this python> -m pip install -e .[mcp]` from the checkout, only
   when `mcp` or `psutil` is missing or outside the range `pyproject.toml` asks for.
3. Claude Code: `claude mcp add --scope user msdial-interactive -- <python>
   <checkout>/scripts/msdial-interactive-mcp.py`, when the `claude` CLI is on PATH. An
   existing user-scope registration is shown (command and arguments; environment
   variable names, never their values) and replaced only on a yes, after the whole entry
   is saved to `~/.claude/mcp-backups/msdial-interactive-<time>.json` (or
   `$CLAUDE_CONFIG_DIR/mcp-backups`). If the new entry cannot be added after the old one
   was removed, the saved one is put back with `claude mcp add-json`.
4. Claude Desktop: an `msdial-interactive` entry merged into `mcpServers` of
   `claude_desktop_config.json` (Windows `%APPDATA%\Claude\`, or a Microsoft Store
   installation's `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\`; macOS
   `~/Library/Application Support/Claude/`), after a timestamped backup. Every other key
   is kept. Without a configuration file it is skipped unless `--desktop` is given.
5. Both skills copied into Claude Code's `~/.claude/skills/<name>/` (or
   `$CLAUDE_CONFIG_DIR/skills`). A differing copy is moved to `~/.claude/skill-backups/`
   first, outside the folder Claude Code loads skills from; a link there is removed
   (what it points at is untouched), never written through.
6. The skill ZIPs for Claude Desktop, `dist/<name>.skill.zip`.
7. The MS-DIAL Consoles it finds (Interactive's saved setting, `MSDIAL_CONSOLE_PATH`,
   `PATH`, `MSDIAL*` folders nearby, or `--console <path>`; a folder stands for the
   `MSDIALCUI` in it, and a named one that is not there is said). Every one is judged
   without starting it, by what its assembly holds: the `--peak-ids-only` option and
   the annotation-candidate and automatic-RT local-test markers that
   `msdial_lipidomics_check_inputs` requires, in its words
   (`lab_lipidomics_agent.LAB_CONSOLE_NEEDS`). One that qualifies is then started as the
   check starts it (Interactive's `console_capabilities`): one named with `--console`
   without a question, the others after one; a dry run starts none. It saves no Console
   setting.
8. A summary and a first prompt.

## Install the local MCP server by hand

From the source checkout:

```powershell
cd C:\path\to\msdial-interactive-app
python -m pip install -e ".[mcp]"
```

Claude Code:

```powershell
claude mcp add --scope user msdial-interactive -- "C:\path\to\python.exe" "C:\path\to\msdial-interactive-app\scripts\msdial-interactive-mcp.py"
```

Claude Desktop, with the full Python path, in `claude_desktop_config.json`:

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

Replace the paths as needed and restart Claude Desktop. The MCP process
uses stdio and can launch the app backend on `http://127.0.0.1:8765` without
opening a browser. If an older app occupies the port,
`msdial_interactive_status` reports `compatible: false` and
`msdial_interactive_restart` can replace only a recognized local MS-DIAL
Interactive process after explicit confirmation.

## Install the Agent Skills by hand

Two skills are bundled under `skills/`:

- `msdial-guided-analysis`: guided LC-MS and GC-MS analysis and public repository
  reanalysis;
- `msdial-lipidomics-workflow`: the laboratory lipidomics flow below, from two raw
  folders to `lipid_metabolome.csv`. Its tutorial is
  [`docs/tutorials/lab-lipidomics.md`](tutorials/lab-lipidomics.md), and the demo's
  workbook is [`examples/lab-lipidomics`](../examples/lab-lipidomics/README.md).

Build the upload ZIPs:

```powershell
python scripts/package-agent-skill.py                                   # every skill
python scripts/package-agent-skill.py --skill msdial-lipidomics-workflow  # one skill
python scripts/package-agent-skill.py --list                            # the names
```

In Claude Desktop, open `Customize > Skills`, choose `+`, and upload each of:

```text
dist/msdial-guided-analysis.skill.zip
dist/msdial-lipidomics-workflow.skill.zip
```

Claude Code users can instead place the folders under `~/.claude/skills/` (every
project) or a project's `.claude/skills/`; other compatible agents can use the same
Agent Skills packages.

## Guided flow

1. Inspect the input folder or analysis metadata CSV.
2. Ask LC-MS versus GC-MS. Other project types are reported as not yet supported.
3. For LC-MS, ask positive/negative and metabolomics/lipidomics.
4. Use template defaults, an exact peak-count target, or run the automatic
   3,000-6,000 peak diagnostic. Automatic selection prefers a mid-run QC and
   uses instrument-specific threshold steps (100 for QTOF-type data, 1,000 for
   Fourier-transform data, including Orbitrap and FT-ICR mzML), taking the
   highest threshold still estimated to keep 3,000 peaks, and falling back to 10
   (QTOF-type) or 100 (FT), and no finer, only when no coarse step lands in
   range. A repository run records its actual per-file peak counts as
   `production_peak_counts`.
5. Optionally choose reviewed user-defined RT correction or automatic
   alignment-only correction learned from detected features. Never enable both.
6. Use official versioned, existing, tiered lipid/MSP, or no annotation libraries.
7. Optionally configure LC-MS QA and internal standards.
8. Prepare the reproducible workflow, show the command, and request confirmation.
9. Run MS-DIAL and call `msdial_complete_guided_analysis` to wait for the exact
   job, validate and preview mzTab-M, generate requested QA/publication files
   (QA draws its figures into `<run directory>/qa_figures`, returned by path),
   and create the data-mining handoff.
10. Optionally save the accepted scientific choices as a reusable workset.

For public accessions, the agent can run the same workflow without browser
interaction: plan the accession, perform a confirmed bounded download, inspect
every raw header by default, review and project repository metadata into
MS-DIAL `Class`, prepare `analysis_files.csv`, and then enter the normal guided
flow. Internal-standard declarations are exposed as evidence so the desktop
agent can draft reviewable LC-MS QA targets without requiring an in-app LLM API
key.

Downloads and production runs require a separate explicit confirmation. A
workset omits raw-data paths and output paths. An accepted diagnostic threshold
is retained as part of the reusable scientific method.

Public repository answer seeds use `TimeBasedLinearWeightedMovingAverage` and
automatic peak-count tuning. This smoother accounts for irregular scan
intervals while adding a small computational cost.

## Laboratory lipidomics flow

The skill `msdial-lipidomics-workflow` carries this flow; the tutorial
[`docs/tutorials/lab-lipidomics.md`](tutorials/lab-lipidomics.md) walks through it
with the public demo.

1. `msdial_lipidomics_check_inputs(pos_path, neg_path)` with the folders only:
   show the person what is there, then ask every open question in one message
   (analysis purpose, LBM2, salt, annotation RT tolerance, internal-standard
   libraries, `quant_utility_table.xlsx` and its amount column, analysis root,
   `raw_location`, Console). Call it again with the answers. Nothing is written.
   When sheet 3 contradicts the merge's reconciliation (`merge_selection_warnings`),
   `merge_selection_accepted` is asked too: the person changes sheet 3, saves it
   and the check runs again, or keeps it (`merge_selection_accepted=true`, recorded
   in the contract). After the contract, changing the workbook needs a new
   contract and new runs of both polarities.
2. `raw_location` is asked with the sentence that MS-DIAL writes `.dcl`, `.pai2`
   and tag files beside the raw files it reads, the one-file diagnostic included,
   and no output setting prevents it. Whether to run on the data in place
   (`in_place`) or on a copy (`copy`) is the person's decision; a copy is made
   only on their word, and the check is then repeated on the copy's folders. The
   contract records the answer.
3. Show `paired_sample_table`; the person confirms it or corrects it by stem.
   `msdial_lipidomics_write_contract(..., check_digest, sample_table_confirmed=true,
   sample_table=<corrections>)` previews, and with `confirmed=true` writes
   `<root>/inputs/`. A check filled from a workset (`filled_from_workset`) is
   written with the same `workset_id`. A contract already at the root refuses
   even the preview and shows `existing_contract` (the steps recorded under it,
   and that replacing it leaves them unusable); preview again with
   `replace_existing=true` only if the person wants it replaced.
4. For each polarity: `msdial_guided_analysis_plan(input_path=<raw folder>,
   answers={"lab_lipidomics_contract_path": ..., "ion_mode": ...})`, the
   diagnostic (confirmed), `msdial_estimate_peak_height`, then the production run
   (confirmed, with `minimum_peak_height` added). Positive and negative run one
   after the other. The diagnostic is refused while the plan has any blocker but
   the missing threshold (`diagnostic_blockers`). The diagnostic's estimate and
   each start are recorded in `<root>/inputs/lab-lipidomics-record.jsonl`, and
   each run's `guided-answers.json` keeps the contract's sha256.
   A run whose automatic RT correction failed is reported; running it again
   uncorrected is the person's decision, and the plan reports it as a deviation.
   The contract fixes the parameter template, the LBM query list and the export
   folder too; any answer it does not state is reported as a deviation, so
   `lab_lipidomics.deviations` is empty only for a plan that is the contract's.
5. `msdial_lipidomics_resolve_standards(contract_path, pos_job_id, neg_job_id)`:
   read each standard's tier, its candidates and `valid_alternatives`, and the
   classes that would be NOT QUANTIFIED to the person. Standards awaiting
   acceptance (`needs_confirmation`: a text candidate, a search, or an LBM
   annotation outside the internal-standard RT tolerance while another spot lies
   inside it), and standards not found or invalid, each need their decision.
6. `msdial_lipidomics_normalize(contract_path, job_id, resolution_digest,
   accept_searched, accept_unresolved, accept_spot)` previews the standards table
   and the exact command; each polarity is confirmed separately. `accept_spot`
   takes another listed candidate for a standard (`{"LPE 18:1(d7)": 11}`). The
   preview warns when the Console is not the build the contract recorded. Poll
   the job with `msdial_interactive_job`; its result says whether every cell was
   verified.
7. `msdial_lipidomics_merge(contract_path)` previews the counts, the
   positive/negative reconciliation (BMP from a negative PG, PE P- from a
   negative PE O-, by name and within 0.1 min: `polarity_reconciliation`) and
   the warnings; `confirmed=true` writes `lipid_metabolome.csv`, the three
   statistics tables (always under metaboservice's names, which
   `metabolomics_service.R` reads), the dotplot folders,
   `lipid_metabolome_not_quantified.csv` and `merge-report.json` into
   `<root>/quant/lipidome`.
   `reconcile_polarities=false` or another `reconciliation_rt_tolerance` is
   recorded as a deviation from the contract. The flags are JSON booleans and the
   tolerance a JSON number; text is refused. A sheet-3 warning at this step says
   what the lipidome counts: sheet 3 can no longer change under this contract.
8. Report the LBM2 by name and sha256, the salt, every setting, what was assumed
   (the workbook's sample amount, the sample table), and any step skipped. Offer
   `msdial_save_workset(name, lab_lipidomics_contract_path=...)`.

The Console must offer `MSDIALCUI normalize --peak-ids-only` and the annotation
candidate table; the check, the plan and normalize each refuse one that does not.
It is named per contract, never through the shared settings.

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
- `msdial_check_official_console_releases`
- `msdial_check_local_console_source`
- `msdial_build_console_from_local_source` (preview by default; execution requires `confirmed=true`)
- `msdial_guided_analysis_plan`
- `msdial_repository_reanalysis_plan`
- `msdial_repository_batch_plan`
- `msdial_download_repository_raw`
- `msdial_check_raw_metadata_extractor` / `msdial_set_raw_metadata_extractor_path`
- `msdial_repository_raw_metadata_preflight`
- `msdial_split_repository_unit`
- `msdial_prepare_repository_reanalysis`
- `msdial_repository_qa_evidence`
- `msdial_cleanup_repository_raw`
- `msdial_discard_repository_raw`
- `msdial_download_store_status` (read-only)
- `msdial_restore_project_for_viewing` / `msdial_undo_project_restore` (preview by default; changes require `confirmed=true`)
- `msdial_list_worksets`
- `msdial_download_official_library`
- `msdial_start_peak_count_diagnostic`
- `msdial_estimate_peak_height`
- `msdial_prepare_guided_analysis`
- `msdial_start_guided_analysis`
- `msdial_interactive_job`
- `msdial_cancel_job`
- `msdial_interactive_validate_mztab`
- `msdial_interactive_preview_mztab`
- `msdial_generate_lcms_qa`
- `msdial_generate_publication_report`
- `msdial_complete_guided_analysis`
- `msdial_save_workset` (with `lab_lipidomics_contract_path`, a laboratory lipidomics workset)
- `msdial_interactive_create_handoff`
- `msdial_lipidomics_quant_input_template` (preview by default; writing requires `confirmed=true`; never overwrites)
- `msdial_lipidomics_check_quant_input` (read-only; no raw data needed)
- `msdial_lipidomics_check_inputs` (read-only)
- `msdial_lipidomics_write_contract` (preview by default; writing requires `confirmed=true`)
- `msdial_lipidomics_resolve_standards` (read-only)
- `msdial_lipidomics_normalize` (preview by default; the Console starts only with `confirmed=true`)
- `msdial_lipidomics_merge` (preview by default; writing requires `confirmed=true`)

Run completion, mzTab-M validation/preview, LC-MS QA, publication generation,
and handoff should receive the exact `job_id` returned by the production run.
Agent API 0.5 tracks files created or updated by that job and does not silently
reuse older mzTab-M or `*.qa.tsv` files from the same directory. Publication can
be generated without QA by setting `run_qa=false`.

Catalog-driven repository runs should pass local `handoff_path` values via
`analysis_unit_handoff_path` or `analysis_unit_handoff_paths`. This keeps full
sample and raw-file manifests out of the model context while Interactive reloads
and validates the complete documents. The default status response is bounded to
five jobs and artifact counts; request one job explicitly for full artifacts.

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
- `POST /api/lipidomics/check`
- `POST /api/lipidomics/write-contract`
- `POST /api/lipidomics/resolve`
- `POST /api/lipidomics/normalize`
- `POST /api/lipidomics/merge`

## Test prompts

Check the setup:

```text
Check that MS-DIAL Interactive is ready for laboratory lipidomics: start its backend,
list the MS-DIAL Consoles you can find and say whether one is new enough. Do not run
any analysis yet.
```

Start from scratch:

```text
Analyze the mass-spectrometry data in <folder>. Ask me the MS-DIAL conditions one at
a time, and confirm with me before running.
```

Start from a workset:

```text
Analyze this folder with the lcms-negative-lipidomics workset. Tune the peak count to
about 10,000 on one representative file, and make the QA report and the Methods too.
```

A laboratory lipidomics batch (the tutorial's demo):

```text
Run the laboratory lipidomics workflow on the MS-DIAL demo data.
Positive raw files: <demo>\data2\pos
Negative raw files: <demo>\data2\neg
Look at the folders first, then ask me everything you need in one message.
```

The final data-mining handoff is written as
`<run_directory>/datamining-handoff.json`. Downstream PCA, UMAP, HCA, summary,
and visualization MCP servers should normally start from `primary_mztab_file`.
