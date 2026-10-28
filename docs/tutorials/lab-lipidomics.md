# Tutorial: laboratory lipidomics with Claude and MS-DIAL

This tutorial takes you from two folders of raw LC-MS/MS lipidomics data, one positive
and one negative, to a quantified lipidome, `lipid_metabolome.csv`, with Claude asking
the questions and MS-DIAL doing the processing on your own computer. It uses the
public MS-DIAL lipidomics demo (mouse plasma, SCIEX, EquiSPLASH internal standards), so
you can follow every step before you try your own data.

It is the workflow the MS-DIAL laboratory uses for its own lipidomics batches. Nothing
in it is specific to that laboratory's computers: you bring your own library,
standards and workbook, or use the public ones named here.

- [What you get](#what-you-get)
- [What you need](#what-you-need)
- [Set up, in one command](#set-up-in-one-command)
- [Get the demo data](#get-the-demo-data)
- [Choose where the analysis goes](#choose-where-the-analysis-goes)
- [The conversation, step by step](#the-conversation-step-by-step)
- [Every confirmation you will be asked for](#every-confirmation-you-will-be-asked-for)
- [How long it takes](#how-long-it-takes)
- [The outputs, and how to read them](#the-outputs-and-how-to-read-them)
- [Your own data](#your-own-data)
- [Troubleshooting](#troubleshooting)
- [Privacy](#privacy)

## What you get

- **A quantified lipidome.** `lipid_metabolome.csv` holds one row per lipid and one
  column per sample, in the unit your workbook gives (pmol per µL plasma for the
  demo). Each lipid class is taken from the polarity and adduct you chose for it, and
  divided by the internal standard you assigned to it.
- **The tables for statistics and plots**: per-sample tables of lipids and of class
  totals, a ranking for summary plots, and folders of per-class files, in the format
  the MS-DIAL laboratory's R script reads.
- **Two MS-DIAL projects**, one per polarity, which you can open in the MS-DIAL GUI, with
  their exports, a validated mzTab-M file, a QA report and a Materials and Methods text.
- **A record of every decision**: the inputs by sha256, the sample table you confirmed,
  the threshold, each internal standard and how it was found, and what the merge kept
  and why.

Claude never decides for you. It asks what it cannot read from your files, shows you
what it is about to do, and starts MS-DIAL only after your yes.

## What you need

- **A Windows 10 or 11 PC.** The setup also runs on macOS and Linux, but the demo's
  SCIEX `.wiff` files and the steps below were written for Windows.
- **About 4 GB of free disk space**: the download and its first unpacking (about
  1.5 GB, most of which you may delete afterwards), the unpacked demo (about 1.1 GB), a
  working copy of the raw data (about 1 GB) and the outputs (about 0.6 GB). Running on
  the data in place (see below) saves the copy.
- **Python 3.10 or newer** from [python.org](https://www.python.org/downloads/). On
  Windows it needs no administrator rights for a per-user installation and brings the
  `py` command the setup uses. With python.org's Python install manager (its default
  from 3.14 on), also install a Python with it: `py install 3.14` in a terminal.
- **Claude Code** ([claude.com/claude-code](https://claude.com/claude-code)) or
  **Claude Desktop** ([claude.ai/download](https://claude.ai/download)), signed in.
  The MCP server runs locally, so no API key is needed.
- **MS-DIAL Interactive**, this repository: clone it, or download it from GitHub with
  *Code > Download ZIP* and unpack it. A short path, such as
  `D:\msdial\msdial-interactive-app`, avoids Windows path-length problems later.
- **An MS-DIAL Console new enough for laboratory lipidomics**: one with
  `normalize --peak-ids-only` (MsdialWorkbench#830), the annotation candidate table and
  the automatic-RT local test (MsdialWorkbench#826). Releases up to 5.5.260820
  (2026-08-20) lack them, and Interactive refuses those for this workflow.

> [!NOTE]
> **The setup below installs the Console for you.** When it finds no Console new
> enough, it offers **the MS-DIAL Console prerelease 5.5.261018** (tag
> `MSDIAL-v5.5.261018`) from the
> [MsdialWorkbench releases page](https://github.com/systemsomicslab/MsdialWorkbench/releases).
> It checks the archive's size and SHA-256 against the values it pins, then unpacks it
> beside the MS-DIAL Interactive folder, for example
> `D:\msdial\MSDIAL.console.v5.5.261018-windows-net48\MSDIALCUI.exe`. To do it by hand,
> download the asset whose name contains `MSDIAL.console` and `windows-net48`, unpack it
> into a folder of your own, and name its `MSDIALCUI.exe`.
>
> The macOS and Linux archives (`osx-net8`, `linux-net8`) are x64 builds that read
> mzML, ABF and netCDF, not vendor formats, so the demo's `.wiff` files must be
> converted to mzML first (ProteoWizard msconvert). On Apple silicon the macOS Console
> runs under Rosetta 2. A Console can also be built from source with
> `msdial_check_local_console_source` and `msdial_build_console_from_local_source`; the
> setup says how when it has no archive for your system.

## Set up, in one command

Claude Code can do this section for you: open it in any folder and ask it to *set up
https://github.com/systemsomicslab/msdial-interactive-app so that you can run MS-DIAL
agent analyses in Claude Code*. It follows the README's instructions for Claude Code,
shows you the plan below and runs it after your yes.

In the MS-DIAL Interactive folder, double-click **`Setup MS-DIAL for Claude.cmd`**. On
macOS or Linux, run `python3 scripts/setup_claude.py` in a terminal in that folder.

The setup explains each step and asks before it changes anything. Options go on the
command line, in a terminal opened in the MS-DIAL Interactive folder: on Windows
`py -3 scripts\setup_claude.py --dry-run`, or the double-click file with the same
options (`"Setup MS-DIAL for Claude.cmd" --dry-run` in Command Prompt). `--dry-run`
shows the plan and changes nothing; `--yes` answers yes to every question;
`--desktop` sets up Claude Desktop even before it has written its configuration file
(otherwise start Claude Desktop once first); `--console <path>` names a Console to
check; `--help` lists the rest. Its eight steps:

1. **Python**: checks that it is 3.10 or newer.
2. **The MCP dependency**: if the `mcp` and `psutil` packages are missing, runs
   `<this python> -m pip install -e .[mcp]` in the MS-DIAL Interactive folder.
3. **Claude Code**: if the `claude` command is on your PATH, registers the
   `msdial-interactive` MCP server for your user
   (`claude mcp add --scope user msdial-interactive -- <python> <folder>\scripts\msdial-interactive-mcp.py`).
   An existing registration is shown first and replaced only if you say so, after it is
   saved to `%USERPROFILE%\.claude\mcp-backups`; if the new one cannot be added, the old
   one is put back.
4. **Claude Desktop**: if Claude Desktop's `claude_desktop_config.json` exists (or you
   pass `--desktop`), adds an `msdial-interactive` entry to its `mcpServers`. It writes a
   timestamped backup first and leaves every other server and setting as it was.
5. **Skills for Claude Code**: copies the two skills, `msdial-lipidomics-workflow` and
   `msdial-guided-analysis`, into `%USERPROFILE%\.claude\skills`. A differing copy that
   is already there is moved to `%USERPROFILE%\.claude\skill-backups` first; a link is
   only unlinked, never written through.
6. **Skill ZIPs for Claude Desktop**: builds `dist\msdial-lipidomics-workflow.skill.zip`
   and `dist\msdial-guided-analysis.skill.zip`.
7. **MS-DIAL Console**: looks for Consoles (Interactive's saved setting, `PATH`, and
   `MSDIAL*` folders beside the Interactive folder, in your home, Downloads, Desktop and
   Documents) and says for each whether it is new enough, by what its program file
   holds, without starting it. `--console <path to MSDIALCUI.exe>` checks one you name,
   and also starts it for its `--help`, as the agent's input check will; the Consoles it
   found are started that way only if you say yes. A dry run starts none. If none is
   new enough, it offers to download the official Console 5.5.261018 for your system,
   checks its SHA-256, unpacks it beside the Interactive folder and starts it for its
   `--help` (`--no-console-download` turns the offer off).
8. **Summary**, and a first prompt to try.

Then:

- **Claude Code**: open a new session (in any folder), run `/mcp` and check that
  `msdial-interactive` is connected.
- **Claude Desktop**: quit it completely (also from the system tray) and start it
  again. Upload both skill ZIPs under **Customize > Skills** (**+**, then upload), and
  turn them on. Skills must be available for your account; see Claude's
  [Skills help page](https://support.claude.com/en/articles/12512176-what-are-skills).

Try this first prompt:

```text
Check that MS-DIAL Interactive is ready for laboratory lipidomics: start its backend,
list the MS-DIAL Consoles you can find and say whether one is new enough. Do not run
any analysis yet.
```

Claude starts the local backend (`msdial_interactive_status`,
`msdial_interactive_launch`), lists the Consoles it finds (`msdial_check_console_path`)
and can compare the official stable and prerelease Console packages
(`msdial_check_official_console_releases`).

<details>
<summary>Setting up by hand instead</summary>

```powershell
cd D:\msdial\msdial-interactive-app
py -3 -m pip install -e ".[mcp]"
claude mcp add --scope user msdial-interactive -- "C:\path\to\python.exe" "D:\msdial\msdial-interactive-app\scripts\msdial-interactive-mcp.py"
py -3 scripts\package-agent-skill.py
```

For Claude Code, copy `skills\msdial-lipidomics-workflow` and
`skills\msdial-guided-analysis` into `%USERPROFILE%\.claude\skills\`. For Claude Desktop,
add to `%APPDATA%\Claude\claude_desktop_config.json` (a Microsoft Store installation
keeps it under `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\`):

```json
{
  "mcpServers": {
    "msdial-interactive": {
      "command": "C:\\path\\to\\python.exe",
      "args": ["D:\\msdial\\msdial-interactive-app\\scripts\\msdial-interactive-mcp.py"]
    }
  }
}
```

and upload the two ZIPs from `dist\`. Use the full path of the Python that has the
`mcp` package.

</details>

## Get the demo data

The demo data: *Lecture files* (2024), Zenodo,
[doi:10.5281/zenodo.10957708](https://doi.org/10.5281/zenodo.10957708), licensed
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); the record names its creator.
Credit it this way if you share the data, or results built on it. The record holds
several lecture archives, some of them large; only `metabolomics_hands-on.zip` is needed.

This tutorial puts everything in `D:\lipidomics-demo`:

1. Download **`metabolomics_hands-on.zip`** (773 MB) from the record.
2. Extract it anywhere, for example into your Downloads folder. Inside
   `metabolomics_hands-on` is **`data2.zip`** (695 MB).
3. Extract `data2.zip` into **`D:\lipidomics-demo`**, so that you have
   `D:\lipidomics-demo\data2\pos`. Windows' *Extract All* proposes a folder named after
   the ZIP: if you get `D:\lipidomics-demo\data2\data2\pos`, move the inner `data2` up
   one level.
4. Copy the three files of **`examples\lab-lipidomics`** in the MS-DIAL Interactive
   folder into `D:\lipidomics-demo\data2`: `quant_utility_table.xlsx`,
   `EquiSPLASH_Pos.txt` and `EquiSPLASH_Neg.txt`. The archive has no workbook in the
   format the agent reads (its `concentrations.xlsx` is an older format), and its two
   standard texts write two standards' names differently from the workbook, so that
   the check would stop on them. The example's
   [README](../../examples/lab-lipidomics/README.md) explains the three files and
   exactly what was changed in the texts.

```text
D:\lipidomics-demo\
  data2\
    pos\        7 SCIEX runs (.wiff with its .wiff.scan): 3 SPF, 3 GF, 1 Blank
    neg\        the same 7 samples in negative mode
    Msp20231101141520_conventional_converted_dev_custum.lbm2   the LBM2 library
    quant_utility_table.xlsx      copied from examples\lab-lipidomics\
    EquiSPLASH_Pos.txt            positive internal standards, copied from examples\lab-lipidomics\
    EquiSPLASH_Neg.txt            negative internal standards, copied from examples\lab-lipidomics\
    20200121_MsdialTxtDB_*_EquiSPLASH.txt   the archive's standard texts: not used
    metadata.xlsx, concentrations.xlsx, msdial_tutorial.docx, ...   not needed here
```

You may delete `metabolomics_hands-on.zip` and the extracted `metabolomics_hands-on`
folder afterwards. Keep `data2.zip` if you choose `in_place` below: it is then your only
copy of the data as published.

## Choose where the analysis goes

**The analysis root** is the folder the agent writes into: `inputs`, `pos`, `neg` and
`quant` are made inside it. This tutorial uses `D:\lipidomics-demo\analysis`.

- It must not be inside a raw-data folder, the MS-DIAL Interactive folder or a temporary
  folder. The agent refuses a raw-data folder, a temporary folder and any git clone, a
  clone of MS-DIAL Interactive included; an Interactive folder downloaded as a ZIP has
  no `.git`, so the agent cannot tell, and keeping out of it is up to you.
- On the system drive `C:` or inside your user profile is allowed; the agent says so
  and goes on.
- Keep the path short and out of cloud-synced folders such as OneDrive: MS-DIAL's
  intermediate file names are long, and sync clients lock files while they upload.

**MS-DIAL writes beside the raw files it reads.** Every run, the one-file diagnostic
included, writes `.dcl`, `.pai2` and tag files and the alignment container next to the
raw files, and no output setting prevents it. So the agent asks where MS-DIAL should
read the data (`raw_location`):

- **copy**: MS-DIAL reads a copy of the raw files (each `.wiff` with its `.wiff.scan`)
  in a folder you name, and writes there. The MS-DIAL laboratory chose this for the
  demo, with the copy in `D:\lipidomics-demo\run\pos` and `D:\lipidomics-demo\run\neg`.
  Who makes the copy depends on where you talk to Claude (step 3): in **Claude Code**,
  Claude copies the files after telling you what, where and how much, and only on your
  yes; **Claude Desktop** has no tool that copies files, so you make the copy yourself,
  for example in Explorer, and give Claude the copy's two folders.
- **in_place**: MS-DIAL reads `data2\pos` and `data2\neg` themselves and writes its
  files there. For the demo this is reasonable too, if you keep `data2.zip` as your
  original.

After the run, the demo folder looks like this:

```text
D:\lipidomics-demo\
  data2\                 unchanged (with copy)
  run\pos, run\neg       the copy, with MS-DIAL's intermediates beside the raw files
  analysis\
    inputs\              the contract and what was derived from your inputs
    pos\, neg\           each polarity's MS-DIAL project, exports, mzTab-M and QA
      qa_figures\        the run's QA figures and qa_report.html
    quant\               normalized matrices and the standards each one used
      qa_figures\        the batch's QA figures and qa_report.html
      lipidome\          lipid_metabolome.csv and the tables for statistics
```

## The conversation, step by step

The skill `msdial-lipidomics-workflow` guides Claude through the steps below; you only
answer. Claude may write in your language; the examples here are in English. Paths are
this tutorial's; use yours.

### 1. Start

```text
Run the laboratory lipidomics workflow on the MS-DIAL demo data.
Positive raw files: D:\lipidomics-demo\data2\pos
Negative raw files: D:\lipidomics-demo\data2\neg
Look at the folders first, then ask me everything you need in one message.
```

Claude makes sure the backend runs, then calls `msdial_lipidomics_check_inputs` with
the two folders only. That call reads and never writes. Claude reports what it found
before asking anything: seven SCIEX `.wiff` files per folder, the polarity the folder
and file names say, no MS-DIAL intermediates yet, and the files in `data2` that look
like inputs: four standard texts (the two you copied and the archive's two), three
workbooks (`quant_utility_table.xlsx`, and the archive's `metadata.xlsx` and
`concentrations.xlsx`) and the `.lbm2` library. It names them but proposes none: which
library fits your chromatography, and which texts and workbook belong to your batch,
are your call.

### 2. Answer the questions in one message

Claude asks every open question at once. The demo's answers:

| Question | What it decides | Demo answer |
| --- | --- | --- |
| What the analysis is for | recorded as the analysis purpose; steers the sample table and the report | e.g. *Compare the plasma lipidome of the SPF and GF groups; comparative profiling is central; I need lipid_metabolome.csv and the QA report.* |
| The LBM2 library | the lipid annotation rules and retention times | `D:\lipidomics-demo\data2\Msp20231101141520_conventional_converted_dev_custum.lbm2` |
| The salt in the mobile phase | which negative-mode adducts MS-DIAL searches | ammonium acetate |
| The annotation RT tolerance | how far from the library's retention time an annotation is accepted | 1.5 min |
| The internal-standard texts | the standards searched in each polarity | `EquiSPLASH_Pos.txt` and `EquiSPLASH_Neg.txt` in `data2`, the copies from `examples\lab-lipidomics` |
| The quant workbook | standards' amounts, class-to-standard lookup, which class and adduct each polarity keeps | `D:\lipidomics-demo\data2\quant_utility_table.xlsx` |
| Its amount column | the unit: F per mg tissue, G per 10^6 cells, H per µL plasma | H |
| The analysis root | where everything is written | `D:\lipidomics-demo\analysis` |
| Where MS-DIAL reads the raw data | `copy` or `in_place` (see above) | copy, into `D:\lipidomics-demo\run` |
| The Console | the `MSDIALCUI.exe` for both runs and normalize | e.g. `D:\msdial\MSDIAL.console.v5.5.261018-windows-net48\MSDIALCUI.exe` |

For example:

```text
Purpose: compare the plasma lipidome of the SPF and GF groups; comparative profiling
is central; I need lipid_metabolome.csv and the QA report.
LBM2: D:\lipidomics-demo\data2\Msp20231101141520_conventional_converted_dev_custum.lbm2
Salt: ammonium acetate
Annotation RT tolerance: 1.5 min
Internal standards: D:\lipidomics-demo\data2\EquiSPLASH_Pos.txt (positive) and
D:\lipidomics-demo\data2\EquiSPLASH_Neg.txt (negative)
Workbook: D:\lipidomics-demo\data2\quant_utility_table.xlsx, amount column H
Analysis root: D:\lipidomics-demo\analysis
Raw data: make a copy in D:\lipidomics-demo\run
Console: D:\msdial\MSDIAL.console.v5.5.261018-windows-net48\MSDIALCUI.exe
```

In Claude Desktop, make the copy yourself first (step 3) and answer instead
*Raw data: copy, which I made in D:\lipidomics-demo\run\pos and D:\lipidomics-demo\run\neg*.

> [!TIP]
> **The same inputs in one file.** `examples\lab-lipidomics\quant_input.xlsx` holds the
> workbook and both standard texts in format 1, the form the agent asks first-time users
> for ([specification](../lab-lipidomics-quant-input.md)). Copy it into `data2` as well
> and answer *Quantification input: D:\lipidomics-demo\data2\quant_input.xlsx* in place
> of the internal-standards and workbook lines. The result is the same. For your own
> data, ask Claude for a template to fill in: it writes one with
> `msdial_lipidomics_quant_input_template` and checks what you fill in with
> `msdial_lipidomics_check_quant_input`, cell by cell.

In the same message Claude tells you what is set without asking, so that you can object
in a word: the internal standards' own RT tolerance (0.5 min), the peak-height
diagnostic, `TimeBasedLinearWeightedMovingAverage` smoothing, automatic RT correction
(12 anchors, a 1.5 min local window), the LBM score cutoffs (0.1), how the merge
treats BMP and PE P-, and that each sample's dilution factor multiplies its
concentrations.

The salt is mapped to MS-DIAL's own name and read back: ammonium acetate is
`CH3COONH4`, ammonium formate `HCOONH4`, ammonium bicarbonate `NH4HCO3`. MS-DIAL
silently uses acetate for anything else, and the wrong salt loses PCs in negative mode
without a warning, so this one is worth checking against your method.

### 3. The copy

If you chose `copy`, the copy holds each `.wiff` with its `.wiff.scan` (about 0.9 GB for
the demo), and nothing else.

- **In Claude Code**, Claude states the source, the destination, the number of files and
  their size, and that the copy is kept afterwards. On your yes it copies them with its
  own file tools and compares names and sizes with the originals.
- **In Claude Desktop**, which cannot copy files, you copy them yourself: in Explorer,
  copy everything in `data2\pos` to `D:\lipidomics-demo\run\pos` and everything in
  `data2\neg` to `D:\lipidomics-demo\run\neg`, and compare the two folders' sizes
  (*Properties*). Then give Claude the copy's two folders.

Either way, Claude then checks the copy's folders with `msdial_lipidomics_check_inputs`:
the same seven files per polarity, by name, and each `.wiff` with its `.wiff.scan`. From
here on, MS-DIAL reads the copy.

### 4. The check, read back

Claude calls `msdial_lipidomics_check_inputs` again with every answer (still read-only)
and reads out:

- **blockers**, each of which stops the next step, and **warnings**;
- **the salt** as mapped, beside the adducts your negative standards and sheet 3 of the
  workbook use (here `[M+CH3COO]-`, which agrees with ammonium acetate);
- **the workbook**: the amount column's header (`int (pmol/ul plasma)`), the unit, and
  the **sample amount** it divides by, H4 = 10 (`Put uL in solvent`), with the notes
  below the standards. Every concentration scales with H4, so confirm it. If it is wrong
  for your samples, correct it in Excel, save, and ask Claude to check again; Claude
  never edits the workbook;
- **what cannot be quantified**: classes whose standard is not in that polarity's
  text. In the MS-DIAL laboratory's run of the demo, five classes (AHexBRS, AHexCAS,
  AHexCS, AHexSIS, AHexSTS) can only be NOT QUANTIFIED in negative mode, because their
  standard, CE 18:1(d7), is in the positive text only; `LPC 18:1(d7)` covers
  "Any others";
- prepared standards no class uses (`FA 16:0(d3)`).

The example workbook's sheet 3 already takes BMP and EtherPE from negative `[M-H]-`, as
the merge expects, so no question about it arises. With your own workbook, Claude asks
you to change sheet 3 or to keep it knowingly; that is settled before the contract,
because the contract binds the whole workbook.

### 5. Confirm the sample table

This step blocks until you confirm. Claude shows one table for both polarities, paired
by file stem (`..._Pos` and `..._Neg`): each sample's type (Sample, QC or Blank), class,
analytical order, batch, dilution factor and acquisition type, with where the classes
and the order came from. In the MS-DIAL laboratory's run, the proposal was SPF × 3,
GF × 3 and one Blank, in analytical order 1 to 7, all DDA, dilution 1, and it was
confirmed unchanged. Read yours: the order decides the injection order of every drift
plot and how the RT correction treats the blank, and a dilution factor other than 1
scales that sample's concentrations. Correct anything by stem (*set the class of
`190722_TT1_Hclass_Aging_Blank` to Blank*).

### 6. The contract

Claude previews the contract with `msdial_lipidomics_write_contract` and, on your yes,
writes it into `analysis\inputs\`: the contract itself, the internal-standard texts in
the column order MS-DIAL reads (`is_pos.txt`, `is_neg.txt`), the standards template, the
selection rules and a record of every later step. From here on the salt, the library,
both tolerances, the standards, the workbook, the sample table, the Console and the
method are fixed for both polarities. If any input file changes afterwards, every later
step refuses until you check and contract again.

### 7. The diagnostics: positive, then negative

To choose the minimum peak height, MS-DIAL first processes one file at zero threshold,
without annotation: a QC near the middle of the run, or, as here with no QC, the
non-blank sample nearest the middle (in the laboratory's run,
`190722_TT1_Hclass_Aging_023_15_9w_GF_M_Plasma_3`). It writes intermediates beside that
one raw file. Claude asks before each diagnostic, then proposes the threshold
(`msdial_estimate_peak_height`): the highest multiple of the instrument's step (100 for
this QTOF-type data) that still keeps at least 3,000 peaks. The laboratory's run
accepted 200 for positive and 100 for negative mode. Accept the proposal with Claude, or
discuss it.

### 8. The production runs, one after the other

For each polarity Claude prepares the run (`msdial_prepare_guided_analysis`), shows the
exact command and the lines of the method file that matter (the salt line, the library,
both RT tolerances, the internal-standard search, the threshold, the smoothing, the RT
correction), and starts MS-DIAL only on your yes. The negative run starts after the
positive one has finished. `msdial_complete_guided_analysis` then waits for the run,
validates the mzTab-M file and writes the QA report, the QA figures
(`analysis\pos\qa_figures\qa_report.html`) and the Materials and Methods text.
The laboratory's runs aligned about 2,200 spots in positive and about 2,450 in negative
mode; yours will differ somewhat.

If the log says *Automatic alignment RT correction failed*, Claude stops and tells you.
Running again without the correction is your decision, and it is recorded.

### 9. The internal standards

Every concentration is a lipid's height divided by its standard's height, so the wrong
peak taken as a standard makes a whole class wrong by one factor. Claude calls
`msdial_lipidomics_resolve_standards` (read-only) and reads out each standard: how it
was found (A: the LBM2 annotated it; B: its own text row matched; C: its row matched but
lost the spot, or matched below the cutoff; D: only a spot at its m/z and RT), its
alignment ID, m/z and RT error, and files where it has no height. Standards that belong
to the other polarity are expected to be missing; in the laboratory's run these were
`FA 18:0(d3)` in positive mode, and the CE, DG, MG and TG standards in negative mode.

You decide every standard listed as needing confirmation: accept it, decline it (its
classes become NOT QUANTIFIED), or take another listed spot instead. In the
laboratory's run no standard needed a decision; with the public LBM2 one may.

### 10. Normalize each polarity

`msdial_lipidomics_normalize` previews the standards table (name, class, amount, the
alignment ID chosen), the unit and the exact `MSDIALCUI normalize` command, with why each
flag is there (`--peak-ids-only` always: a standard is taken by its alignment ID, never
looked up by name). Each polarity starts on its own yes. Afterwards the job compares
MS-DIAL's report with the table and recomputes every quantified cell from the heights
the same call wrote; Claude reports whether every cell was verified. The confirmed call
also draws each standard's response across the samples
(`analysis\quant\qa_figures\04_internal_standards_positive.svg` and `..._negative.svg`).

### 11. Merge the polarities

`msdial_lipidomics_merge` previews what it will keep and why: the rows kept from each
polarity, those dropped (unannotated, an internal standard, not a reference match, or
marked by you), NOT QUANTIFIED rows, lipids kept twice under two adducts or polarities,
and the reconciliation of BMP and PE P- between the polarities (a negative PG or PE O-
row that positive mode names BMP or PE P-, by exact name within 0.1 min). In the
laboratory's run six PE P- rows were matched this way. On your yes it writes
`analysis\quant\lipidome\`. The laboratory's lipidome held about 390 lipids in about 46
classes. It then writes the batch's QA page, `analysis\quant\qa_figures\qa_report.html`,
and Claude gives you its path.

### 12. Save the method, and the report

Claude offers to save a workset (`msdial_save_workset`): the library, the salt, both
tolerances, the standards texts, the workbook and its amount column, the Console and the
method, each file with its sha256. The next batch starts from it and is still asked
the purpose, the analysis root and where MS-DIAL reads, and still confirms its sample
table. Claude ends with a report of what ran, on which files, with which library (by
name and sha256) and settings, what was assumed rather than confirmed, and where
everything is. The raw data, or its copy, is kept.

## Every confirmation you will be asked for

| # | Confirmation | Why it is asked |
| --- | --- | --- |
| 1 | The copy of the raw data (only with `copy`, in Claude Code; in Claude Desktop you make it) | it writes about 1 GB and decides where MS-DIAL writes its intermediates |
| 2 | The sample table | it decides groups, order and dilution for both polarities |
| 3 | Writing the contract | it fixes every input and answer for the runs that follow |
| 4, 5 | The positive and the negative diagnostic | each starts MS-DIAL and writes beside one raw file |
| 6, 7 | Accepting each threshold | a scientific choice that sets how many peaks are kept |
| 8, 9 | The positive and the negative production run | each starts MS-DIAL on every file |
| 10 | Each internal standard that needs a decision (if any) | the standard sets the scale of its classes |
| 11, 12 | Normalize, positive and negative | each starts MS-DIAL's normalize |
| 13 | The merge | it writes the lipidome |

Also asked when they arise: restarting an older MS-DIAL Interactive backend that holds
the port, replacing a contract already at the analysis root (which makes everything done
under it unusable), downloading the official LBM2 for your own data, saving a workset,
and building a Console from source. A yes covers only the action it was given for.
Nothing in this workflow deletes your data.

## How long it takes

- The setup: a few minutes.
- The download: 773 MB, depending on your connection, and a few minutes to unpack.
- MS-DIAL, on the MS-DIAL laboratory's workstation for the demo: one to two minutes per
  diagnostic, under ten minutes per production run including the QA, and about a minute
  per normalize. The merge takes seconds.
- The conversation: plan on about an hour the first time, most of it reading what Claude
  reads out and answering.

## The outputs, and how to read them

`analysis\quant\lipidome\`, the folder to run the R script in:

| File or folder | What it holds |
| --- | --- |
| `lipid_metabolome.csv` | the lipidome: MS-DIAL's alignment-table layout, with rows `Class`, `File type`, `Injection order` and `Batch ID` above the sample columns, then one row per lipid (alignment ID, average RT and m/z, name, adduct, ontology, comment, ...) and one concentration per file, in the workbook's unit |
| `lipid_metabolome4stat.csv` | one row per Sample or QC file (never a Blank), named class and injection order (`SPF_1`), one column per lipid (`name_alignmentID`) |
| `lipid_metabolome4classsummary.csv` | the same files, one column per lipid class: the sum of its lipids' concentrations |
| `lipid_metabolome4summaryplot.csv` | each lipid's log10 mean concentration over the Sample and QC files, ranked |
| `Summary_Dotplots\`, `category_*\`, `subclass_*\` | per-category and per-class files for dot plots |
| `lipid_metabolome_not_quantified.csv` | rows sheet 3 selects that have no valid concentration, for example a class whose standard was not found |
| `merge-report.json` | everything the merge did: counts, every row set aside and why, the reconciliation pairs, warnings |

The tables are comma separated and unquoted, as the MS-DIAL laboratory's earlier merge
program (metaboservice) wrote them, so its R script, `metabolomics_service.R`, reads
them unchanged; any spreadsheet or R/Python reads them too.

**Statistics with the R script.** The script is in the same Zenodo record:
`metabolomics_hands-on.zip` holds `metabolomics_hands-on\inhouse_programs.zip`, which
holds `inhouse_programs\statistics\metabolomics_service.R` and, beside it, the folder
`metabolomics_service_function` it loads its functions from. Claude does not run it,
and it is not set up for you: open it in R or RStudio and set by hand what its comments
ask for, such as the folder of `metabolomics_service_function`, the folder to analyse
(`analysis\quant\lipidome`), the groups to compare (`my_comparisons`; for the demo, SPF
against GF) and the unit, then run it. It needs the R packages it loads at its top.

`analysis\quant\` holds each polarity's normalized matrix (`NormalizedHeight_Pos.txt`,
`NormalizedHeight_Neg.txt`) beside the raw heights it was divided from
(`Height_Pos.txt`, `Height_Neg.txt`), and the standards table normalize used
(`standards_pos.tsv`, `standards_neg.tsv`, with the resolution behind it in
`is_resolution_*.tsv`). Keeping both sides lets anyone check a concentration.

`analysis\pos\` and `analysis\neg\` each hold one MS-DIAL run:

- `Project-*.mdproject`: open it in the MS-DIAL GUI to review spectra and annotations;
- `AlignResult-*.mzTab`: the alignment as mzTab-M, validated after the run;
- `AlignResult-*_Height.txt` and `AlignResult-*.mdcandidate.tsv`: the height export and
  the annotation candidates the standards were resolved from;
- `MS_DIAL_QA_Results.txt`, `AlignResult-*.qa.tsv` and `Supplementary_Table_MS_DIAL.xlsx`:
  the QA report, its matrix and the supplementary tables;
- `qa_figures\`: the run's QA figures (01 to 03, 05 to 07) and `qa_report.html`;
- `MS_DIAL_Materials_and_Methods.txt`: a methods paragraph for this run;
- `method.txt`, `command.txt`, `run-manifest.json`, `REPRODUCE.txt`: how to run it again.

**The QA figures.** You do not need to draw anything: the tools write the figures and
Claude gives you their paths. Open `analysis\quant\qa_figures\qa_report.html` in a
browser for the whole batch on one page, both polarities side by side; each figure is
also an SVG file beside it, and each run has its own page in its `qa_figures` folder.

| Figure | What it shows |
| --- | --- |
| 01 detected features per injection | aligned features with a peak (height > 0, gap-filled included) in each injection, in injection order; the Blank far below the samples |
| 02 summed peak height per injection | run-order drift of the total signal |
| 03 peak-height distribution | log10 height boxplots per injection |
| 04 internal standards, per polarity | each standard normalize used, its height relative to its mean over the non-Blank injections, a +/-20% band and its CV; the subtitle names the standards not drawn (not resolved in that polarity, or resolved but not used by normalize) |
| 05 PCA of the injections | PC1 and PC2 of the study injections (Blank excluded) from the reference-matched features, log10(height + 1), autoscaled |
| 06 sample-to-Blank ratio | per aligned feature, log10 of the mean sample height over the Blank's, reference-matched features in blue, the share at 3x or more, and beside it the QA report's criterion of the same name |
| 07 RT-correction anchors | each anchor's observed minus reference RT, per injection, used and not used |
| 08 lipid class totals | each class's summed concentration per sample, from `lipid_metabolome4classsummary.csv`, one lane per group |

A figure the data cannot support is not drawn, and the page says why: figure 07 needs
the run's RT-correction audit, and a PCA at least three study injections. The figures
are kept out of
`analysis\quant\lipidome\`, so `metabolomics_service.R` never reads them.

**If you compare them with figures drawn earlier by hand.** The QA matrix marks
`Reference matched` per injection, not per feature, and a Console up to MS-DIAL
103c3b535 marks an in-silico (predicted-spectrum) match there too, which the QA never
counts as one (it reads the run's peak tables, or the matrix's own `In-silico matched`
column). Here a feature counts as
reference-matched when the peak of any study injection in it is, and then all of its
study heights enter the PCA; figure 06 counts each feature once; figure 04 draws the
standards normalize used, not every standard the resolution found. A plotting script that
set a height to 0 wherever that injection's own peak was not matched, or counted a
feature once per flag value, gives other numbers: for the demo, PC1/PC2 52%/15%
(positive) and 37%/20% (negative) instead of 66%/14% and 52%/18%, and 63%/59% at 3x or
more instead of 55%/55%. Figure 06's share also differs from the QA report's
`sample_blank_ratio_ge_3` (45% and 23% for the demo): the figure uses means and counts a
feature with no Blank signal as clean, while the criterion uses medians over the features
detected in a Blank only. The figure prints both.

**Reading the QA report.** The QA report evaluates prespecified criteria from QC
injections, blanks and run order, and says which it could not assess and why. The demo
has no QC injection and a single blank, so the four QC criteria are reported as not
assessed, and in the laboratory's run none of the three that could be evaluated was met.
That is the QA describing this small demo honestly, not a failed run; with your own
batch, include at least three QC injections to have precision assessed.

## Your own data

- **Raw data**: two folders, one per polarity, in a format MS-DIAL reads (vendor files on
  Windows, or mzML). Name each sample's files `<sample>_Pos` and `<sample>_Neg`: the two
  polarities are paired by the name without that ending, and every sample needs both.
  ASCII file and folder names without commas are safest, because MS-DIAL's file list is
  read as ASCII.
- **The LBM2**: your laboratory's, built for your chromatography. If you have none,
  Interactive's library catalog has the official one, `lipidomics` (Zenodo record
  [21904324](https://doi.org/10.5281/zenodo.21904324), about 785 MB): ask Claude to
  download it with `msdial_download_official_library`; it asks you first. Whether its
  retention times fit your gradient is yours to judge, since a library built for another
  gradient annotates at the wrong retention times without a warning.
- **The internal-standard texts**: tab-separated text with a header row and one row per
  standard. Required columns are **Name**, **MZ** (the m/z of the adduct you name, written
  with a decimal point, not the neutral mass), **RT** (minutes), **Adduct** (as MS-DIAL
  writes it: `[M+H]+`, `[M+NH4]+`, `[M-H]-`, `[M+CH3COO]-`, ...) and **Ontology** (the
  class); InChIKey, Formula, SMILES and CCS may follow. Header spellings such as
  `Metabolite name`, `Precursor m/z`, `RT (min)` or `Adduct type` are recognised, in any
  column order. Use ASCII only (a non-ASCII character becomes `?` in MS-DIAL), one row
  per standard and polarity, and the same Name as on sheet 1 of your workbook. One file
  holding both polarities may be named for both; it is split by the adducts' charge.
  When a Formula is given, an MZ more than 5 mDa away from it is warned about.
- **The workbook**: start from
  [`examples/lab-lipidomics/quant_utility_table.xlsx`](../../examples/lab-lipidomics/quant_utility_table.xlsx)
  and its [README](../../examples/lab-lipidomics/README.md): your standards and amounts on
  sheet 1, your class-to-standard lookup on sheet 2, your class and adduct selection on
  sheet 3.
- **A sample metadata table** (optional): a table with MS-DIAL's sample-table headers
  (`File name`, `Type`, `Class ID`, `Analytical order`, ...) as `.xlsx`, `.tsv` or `.csv`
  only corroborates the proposed sample table; Claude reports where they disagree.
- **Next batches**: start from the workset you saved after the first one.

## Troubleshooting

**The Console is refused as too old.** The check says the Console *lacks normalize
--peak-ids-only, the annotation candidate table or automatic RT correction's local test*.
Get the prerelease or build from source (see [What you need](#what-you-need)), then check
it with `py -3 scripts\setup_claude.py --console <path to MSDIALCUI.exe>` in a terminal in
the MS-DIAL Interactive folder (with `--dry-run`, it is only read, not started).

**Claude does not see the MS-DIAL tools.** In Claude Code, run `/mcp` or
`claude mcp list` and start a new session after the setup; in Claude Desktop, quit it
completely and start it again, and check that `claude_desktop_config.json` still holds
the `msdial-interactive` entry (the setup's backup is next to it). The registered command
must be a Python that has the `mcp` package: run the setup with that Python. After
updating MS-DIAL Interactive, reconnect the server (`/mcp` in Claude Code, or restart
Claude Desktop): its backend runs inside the MCP server process, so new code is loaded
only by a new process.

**Port 8765 is in use.** The backend listens on `127.0.0.1:8765`. If an older MS-DIAL
Interactive holds it, `msdial_interactive_status` reports it as incompatible, and Claude
can replace it with `msdial_interactive_restart` after your yes. If another program holds
the port, close that program, or ask Claude to use another port (for example 8766) for
every MS-DIAL tool call: each tool takes a `port` argument.

**Windows path length.** MS-DIAL's per-file intermediates have long names, and a deep
folder can exceed Windows' 260-character path limit; a run then fails with a message
that a part of the path could not be found. Use a short analysis root and a short copy
folder (`D:\lipidomics-demo\run`), out of deep or synced folders.

**The wrong salt.** It does not fail loudly: in negative mode, classes measured as acetate
or formate adducts (PC, SM, LPC, ceramides) come out few or none. The check reads the salt
back beside the adducts your standards and sheet 3 use, and blocks when sheet 3 selects
an adduct your salt would drop. The salt is in the contract: correcting it means a new
contract and both polarities run again.

**The workbook is open in Excel.** The check warns when it sees Excel's lock file; it reads
only what was last saved. Save and close the workbook, then check again.

**A contract is already at the analysis root.** Even the preview is refused, with what was
done under the existing contract. Choose another analysis root, or replace it knowingly:
replacing makes every run under it unusable.

**Double-clicking the setup says Python was not found.** It looks for `py -3`, then
`python`, and takes one only if it runs Python 3.10 or newer; it never installs a Python
itself, and it skips the Microsoft Store's `python` alias. Install Python from
python.org. If you installed python.org's Python install manager (its default from
3.14 on), it also needs a Python runtime: run `py install 3.14` in a terminal. Then run
the setup again.

**pip refuses to install (macOS or Linux).** An "externally managed" Python cannot take
packages. Make a virtual environment (`python3 -m venv .venv`) and run the setup with its
Python (`.venv/bin/python scripts/setup_claude.py`).

## Privacy

- **Your data stay on your computer.** The MCP server and the MS-DIAL Interactive backend
  run locally and listen only on `127.0.0.1`; MS-DIAL reads the raw files where they are.
  Nothing in this workflow uploads raw data, libraries or results.
- **What Claude sees** is what the tools return: file and folder names, counts, settings,
  standard names and the numbers Claude reads out to you, such as a standard's retention
  time or a class count. It does not receive the raw files or the libraries. What it
  receives is part of your conversation with Claude.
- **The contract and its record name your library by where it is kept**, so they are
  marked local only. Do not share them; when you share results, name a library by its
  file name and sha256.
- **Private libraries stay private.** A laboratory's own LBM2 or MSP files may be used
  locally; never put them, or their paths, into a shared report, a repository or an
  issue.
