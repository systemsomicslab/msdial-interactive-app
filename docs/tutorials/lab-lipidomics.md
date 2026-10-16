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
  1.5 GB, which you may delete afterwards), the unpacked demo (about 1.1 GB), a working
  copy of the raw data (about 1 GB) and the outputs (about 0.6 GB). Running on the data
  in place (see below) saves the copy.
- **Python 3.10 or newer** from [python.org](https://www.python.org/downloads/). On
  Windows the python.org installer needs no administrator rights for a per-user
  installation; it also installs the `py` launcher the setup uses.
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

<!-- PLACEHOLDER(console-prerelease): replace "5.5.2610xx" and "MSDIAL-v5.5.2610xx" in this
file, and CONSOLE_PRERELEASE_TAG in scripts/setup_claude.py, with the published tag. -->
> [!IMPORTANT]
> **The Console prerelease is not published yet.** Use **the MS-DIAL Console
> prerelease 5.5.2610xx or later** from the
> [MsdialWorkbench releases page](https://github.com/systemsomicslab/MsdialWorkbench/releases)
> (tag `MSDIAL-v5.5.2610xx`, *placeholder: the exact tag will be filled in here once
> the prerelease is cut*). Download its Windows Console ZIP (the asset whose name
> contains `MSDIAL.console`) and unpack it into a folder of your own, for example
> `D:\msdial\MSDIAL.console.v5.5.2610xx`. The file you will name is `MSDIALCUI.exe`
> inside it.
>
> Until the prerelease exists, build the Console from source: clone
> [MsdialWorkbench](https://github.com/systemsomicslab/MsdialWorkbench), install the
> [.NET SDK](https://dotnet.microsoft.com/download), and, once the setup below is done,
> ask Claude:
>
> ```text
> My MsdialWorkbench clone is in D:\msdial\MsdialWorkbench. Check it against
> origin/master with msdial_check_local_console_source, then show me the build
> command of msdial_build_console_from_local_source with select_after_build=false.
> ```
>
> Claude shows the exact `dotnet build` command and runs it only after your yes. The
> Console it builds is `tests\MSDIAL5\MsdialCoreTestApp\bin\Release\net48\MSDIALCUI.exe`
> inside the clone.

## Set up, in one command

In the MS-DIAL Interactive folder, double-click **`Setup MS-DIAL for Claude.cmd`**. On
macOS or Linux, run `python3 scripts/setup_claude.py` in a terminal in that folder.

The setup explains each step and asks before it changes anything. To see the plan
first without changing anything, run it with `--dry-run`
(`python scripts\setup_claude.py --dry-run`); `--yes` answers yes to every question,
and `--help` lists the other options. Its eight steps:

1. **Python**: checks that it is 3.10 or newer.
2. **The MCP dependency**: if the `mcp` and `psutil` packages are missing, runs
   `<this python> -m pip install -e .[mcp]` in the MS-DIAL Interactive folder.
3. **Claude Code**: if the `claude` command is on your PATH, registers the
   `msdial-interactive` MCP server for your user
   (`claude mcp add --scope user msdial-interactive -- <python> <folder>\scripts\msdial-interactive-mcp.py`).
   An existing registration is shown first and replaced only if you say so.
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
   Documents) and says for each whether it is new enough, by asking it for
   `normalize --help`. `--console <path to MSDIALCUI.exe>` checks one you name.
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

The demo is Zenodo record
[10.5281/zenodo.10957708](https://doi.org/10.5281/zenodo.10957708) (CC BY 4.0).

1. Download **`metabolomics_hands-on.zip`** (773 MB) from the record.
2. Unpack it. Inside `metabolomics_hands-on` is **`data2.zip`** (695 MB).
3. Unpack `data2.zip`. Check that you get `...\data2\pos`, not `...\data2\data2\pos`
   (Windows' *Extract All* adds a folder of its own; move the inner `data2` up if so).
4. Copy **`examples\lab-lipidomics\quant_utility_table.xlsx`** from the MS-DIAL
   Interactive folder next to `data2`. The demo archive has no workbook in the format
   the agent reads; its `concentrations.xlsx` is an older format and is not used. The
   example's [README](../../examples/lab-lipidomics/README.md) explains its three
   sheets.

This tutorial uses `D:\lipidomics-demo` for all of it:

```text
D:\lipidomics-demo\
  data2\
    pos\        7 SCIEX runs (.wiff with its .wiff.scan): 3 SPF, 3 GF, 1 Blank
    neg\        the same 7 samples in negative mode
    20200121_MsdialTxtDB_Pos_EquiSPLASH.txt     positive internal standards
    20200121_MsdialTxtDB_Neg_EquiSPLASH.txt     negative internal standards
    Msp20231101141520_conventional_converted_dev_custum.lbm2   the LBM2 library
    metadata.xlsx, concentrations.xlsx, msdial_tutorial.docx, ...   not needed here
  quant_utility_table.xlsx      copied from examples\lab-lipidomics\
```

You may delete the two ZIP files once they are unpacked; `data2.zip` is your untouched
original if you later want the data as published.

## Choose where the analysis goes

**The analysis root** is the folder the agent writes into: `inputs`, `pos`, `neg` and
`quant` are made inside it. This tutorial uses `D:\lipidomics-demo\analysis`.

- It must not be inside a raw-data folder, inside a code checkout (such as the MS-DIAL
  Interactive folder) or inside a temporary folder; the agent refuses all three.
- On the system drive `C:` or inside your user profile is allowed; the agent says so
  and goes on.
- Keep the path short and out of cloud-synced folders such as OneDrive: MS-DIAL's
  intermediate file names are long, and sync clients lock files while they upload.

**MS-DIAL writes beside the raw files it reads.** Every run, the one-file diagnostic
included, writes `.dcl`, `.pai2` and tag files and the alignment container next to the
raw files, and no output setting prevents it. So the agent asks where MS-DIAL should
read the data (`raw_location`):

- **copy**: it copies the raw files (each `.wiff` with its `.wiff.scan`) to a folder you
  name, after telling you what, where and how much, and only on your yes. MS-DIAL then
  reads and writes there. The MS-DIAL laboratory chose this for the demo, with the copy
  in `D:\lipidomics-demo\run\pos` and `D:\lipidomics-demo\run\neg`.
- **in_place**: MS-DIAL reads `data2\pos` and `data2\neg` themselves and writes its
  files there. For the demo this is reasonable too, since `data2.zip` is your original.

After the run, the demo folder looks like this:

```text
D:\lipidomics-demo\
  data2\                 unchanged (with copy)
  run\pos, run\neg       the copy, with MS-DIAL's intermediates beside the raw files
  analysis\
    inputs\              the contract and what was derived from your inputs
    pos\, neg\           each polarity's MS-DIAL project, exports, mzTab-M and QA
    quant\               normalized matrices and the standards each one used
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
and file names say, no MS-DIAL intermediates yet, and the files beside the data that
look like inputs (the two EquiSPLASH texts, the workbooks, the `.lbm2` library). It
names the library but does not propose it: which library fits your chromatography is
your call.

### 2. Answer the questions in one message

Claude asks every open question at once. The demo's answers:

| Question | What it decides | Demo answer |
| --- | --- | --- |
| What the analysis is for | recorded as the analysis purpose; steers the sample table and the report | e.g. *Compare the plasma lipidome of the SPF and GF groups; comparative profiling is central; I need lipid_metabolome.csv and the QA report.* |
| The LBM2 library | the lipid annotation rules and retention times | `D:\lipidomics-demo\data2\Msp20231101141520_conventional_converted_dev_custum.lbm2` |
| The salt in the mobile phase | which negative-mode adducts MS-DIAL searches | ammonium acetate |
| The annotation RT tolerance | how far from the library's retention time an annotation is accepted | 1.5 min |
| The internal-standard texts | the standards searched in each polarity | the two `20200121_MsdialTxtDB_*_EquiSPLASH.txt` files |
| The quant workbook | standards' amounts, class-to-standard lookup, which class and adduct each polarity keeps | `D:\lipidomics-demo\quant_utility_table.xlsx` |
| Its amount column | the unit: F per mg tissue, G per 10^6 cells, H per µL plasma | H |
| The analysis root | where everything is written | `D:\lipidomics-demo\analysis` |
| Where MS-DIAL reads the raw data | `copy` or `in_place` (see above) | copy, into `D:\lipidomics-demo\run` |
| The Console | the `MSDIALCUI.exe` for both runs and normalize | e.g. `D:\msdial\MSDIAL.console.v5.5.2610xx\MSDIALCUI.exe` |

For example:

```text
Purpose: compare the plasma lipidome of the SPF and GF groups; comparative profiling
is central; I need lipid_metabolome.csv and the QA report.
LBM2: D:\lipidomics-demo\data2\Msp20231101141520_conventional_converted_dev_custum.lbm2
Salt: ammonium acetate
Annotation RT tolerance: 1.5 min
Internal standards: D:\lipidomics-demo\data2\20200121_MsdialTxtDB_Pos_EquiSPLASH.txt
(positive) and D:\lipidomics-demo\data2\20200121_MsdialTxtDB_Neg_EquiSPLASH.txt (negative)
Workbook: D:\lipidomics-demo\quant_utility_table.xlsx, amount column H
Analysis root: D:\lipidomics-demo\analysis
Raw data: make a copy in D:\lipidomics-demo\run
Console: D:\msdial\MSDIAL.console.v5.5.2610xx\MSDIALCUI.exe
```

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

If you chose `copy`, Claude states the source, the destination, the number of files and
their size (about 0.9 GB for the demo) and that the copy is kept afterwards. On your yes
it copies each `.wiff` with its `.wiff.scan`, compares names and sizes, and checks the
copy's folders again: from here on, MS-DIAL reads the copy.

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
validates the mzTab-M file and writes the QA report and the Materials and Methods text.
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
the same call wrote; Claude reports whether every cell was verified.

### 11. Merge the polarities

`msdial_lipidomics_merge` previews what it will keep and why: the rows kept from each
polarity, those dropped (unannotated, an internal standard, not a reference match, or
marked by you), NOT QUANTIFIED rows, lipids kept twice under two adducts or polarities,
and the reconciliation of BMP and PE P- between the polarities (a negative PG or PE O-
row that positive mode names BMP or PE P-, by exact name within 0.1 min). In the
laboratory's run six PE P- rows were matched this way. On your yes it writes
`analysis\quant\lipidome\`. The laboratory's lipidome held about 390 lipids in about 46
classes.

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
| 1 | The copy of the raw data (only with `copy`) | it writes about 1 GB and decides where MS-DIAL writes its intermediates |
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
- `MS_DIAL_Materials_and_Methods.txt`: a methods paragraph for this run;
- `method.txt`, `command.txt`, `run-manifest.json`, `REPRODUCE.txt`: how to run it again.

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
it with `python scripts\setup_claude.py --dry-run --console <path to MSDIALCUI.exe>`.

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

**Double-clicking the setup says Python was not found.** Install Python from python.org
(it brings the `py` launcher), or turn off the Microsoft Store `python` alias under
*Settings > Apps > Advanced app settings > App execution aliases*, then run it again.

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
