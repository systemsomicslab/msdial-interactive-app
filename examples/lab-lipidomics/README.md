# Example inputs for the laboratory lipidomics demo

This folder holds the inputs the MS-DIAL laboratory uses with the public lipidomics demo
(mouse plasma, SCIEX, EquiSPLASH internal standards) that the demo archive lacks or holds
in a form the agent cannot use as it is:

| File | What it is |
| --- | --- |
| `quant_utility_table.xlsx` | the quant workbook: standards and amounts, class-to-standard lookup, class and adduct selection |
| `EquiSPLASH_Pos.txt`, `EquiSPLASH_Neg.txt` | the internal-standard texts, positive and negative, edited from the archive's (see [below](#the-internal-standard-texts)) |

**The demo data:** *Lecture files* (2024), Zenodo,
[doi:10.5281/zenodo.10957708](https://doi.org/10.5281/zenodo.10957708), licensed
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); the record names its creator.
The demo is `metabolomics_hands-on.zip` > `data2.zip` > `data2`. Credit the record this
way when you share the data, or results built on it. The three files here belong to that
demo and are offered under the same CC BY 4.0 licence, with that credit.

## The internal-standard texts

The archive's `data2` holds `20200121_MsdialTxtDB_Pos_EquiSPLASH.txt` and
`20200121_MsdialTxtDB_Neg_EquiSPLASH.txt`. Used as they are with this workbook, the
agent's input check stops: they write the sphingoid base of two standards as `;2O`, and
sheet 1 names them as current MS-DIAL does, with `;O2`, and a standard's name must match
character for character. `EquiSPLASH_Pos.txt` and `EquiSPLASH_Neg.txt` are those two
files as the MS-DIAL laboratory edited them for its demo run, with exactly these changes:

- in both, `SM 18:1;2O/18:1(d9)` is renamed `SM 18:1;O2/18:1(d9)`, and
  `Cer 18:1;2O/15:0(d7)` is renamed `Cer 18:1;O2/15:0(d7)`;
- in the negative text, the row for `PA 15:0_18:1(d7)`, which sheet 1 does not prepare,
  is removed.

Every other byte is the archive's (`tests/test_example_standards.py` undoes the changes
and compares the result with the record's checksums). With your own standards, write
the names in your texts and on sheet 1 the same way.

## The workbook, `quant_utility_table.xlsx`

The workbook is not in the record. Its document properties carry no author, and Excel's
record of the folder it was last saved in (`absPath` in `xl/workbook.xml`) was removed;
nothing else was changed (`tests/test_example_workbook.py` checks this).

The laboratory lipidomics agent (see
[`docs/tutorials/lab-lipidomics.md`](../../docs/tutorials/lab-lipidomics.md)) reads it
as Excel last saved it and never edits it. To use it, copy it next to your data and
name the copy when the agent asks for the workbook. To adapt it to your own standards,
edit your copy in Excel, save it, and close Excel before the agent checks it: an open
workbook's unsaved edits are invisible to the agent, which warns when it sees Excel's
lock file.

The three sheets are read by position (first, second, third), whatever they are
called.

## Sheet 1, `1. IS preparation`: the standards and their amounts

| What | Where | Used for |
| --- | --- | --- |
| Header row | the first row with `Name` in column B and `Class name` in column D (row 5) | finding the table |
| Standard name | column B, from the row below the header down to the first empty B (rows 6-20, 15 standards) | must equal the `Name` of that standard in your internal-standard text, character for character |
| Class name | column D | compared with the standard's `Ontology` in the text; a difference is a warning |
| Amount per sample unit | the column you choose: F `int (pmol/mg tissue)`, G `int (pmol/10^6 cells)` or H `int (pmol/ul plasma)` | the standard's amount, and the unit of every concentration; the header text names the unit |
| Sample amount | the chosen column, one row above the header (row 4), with its label two rows above (row 3) | read back to you before the contract, because every concentration scales with it |
| Notes | any text below the standards, after the empty row | read back to you, never used |

Columns C (`mM in LC-MS vial`) and E (`pmol`) are what the amounts are calculated from
in Excel; the agent reads only the saved values of the chosen column. A formula Excel
never calculated is refused, and a workbook that asks Excel to recalculate on opening
is warned about: open it in Excel and save it once.

In the demo, the amount column is **H** (pmol per µL plasma) and its sample amount,
H4, is **10** (`Put uL in solvent`), as saved. Below the standards, the sheet keeps a
worked example and the extraction notes (`plasma 20 uL`, the solvent volumes); the
agent reads them out beside H4 so that you can judge whether H4 is right for your
samples.

`FA 16:0(d3)` is prepared on sheet 1 but used by no class on sheet 2: the agent notes
it, searches it, and quantifies nothing with it.

## Sheet 2, `2. Lookup table`: which standard quantifies which class

No header row. Every row down to the last is read, and blank rows are skipped.

| Column | Holds |
| --- | --- |
| A | the standard, exactly as on sheet 1 |
| B | Excel's lookup of its amount; the agent recomputes it from the column you chose and reports a cached value that differs |
| C | the lipid class, in the spelling MS-DIAL's normalize uses (`PC`, `EtherPE`, `Cer-NS`, `HexCer-NS`, ...) |
| D | old peak IDs, ignored |

Exactly one row must name the class `Any others`: its standard (`LPC 18:1(d7)` here)
quantifies every class without a row of its own, and any spot that is not a reference
match. A class named on two rows is refused, because normalize would silently use the
first. A class that MS-DIAL never assigns is refused, with the spelling MS-DIAL uses.

## Sheet 3, `3. Quant info`: which class and adduct each polarity keeps

Row 1 holds the headers `Class`, `Adduct`, `Ion mode` and `IsSelected`; each row below
is one class and adduct in one polarity:

- `Class` in the spelling of the export's Ontology column (`Cer_NS`, `EtherPE`, ...),
  which differs from sheet 2's for some classes;
- `Adduct` as MS-DIAL writes it (`[M+H]+`, `[M+CH3COO]-`, ...), of the polarity in
  `Ion mode` (`Positive` or `Negative`);
- `IsSelected` `TRUE` or `FALSE`.

The merge keeps a lipid only from a class and adduct marked `TRUE`. In this workbook,
360 rows select 158 pairs over 142 classes (63 positive, 95 negative). It already agrees
with the merge's positive/negative reconciliation, so the agent asks no question about
it:

- BMP and EtherPE are taken from negative `[M-H]-` only (`BMP [M-H]-` and
  `EtherPE [M-H]-` TRUE, `BMP [M+NH4]+` and `EtherPE [M+H]+` FALSE): the merge renames
  a negative PG that positive mode annotates as BMP, and a negative PE O- that positive
  mode annotates as PE P-, before it selects.
- Hex3Cer is taken from positive `[M+H]+`, its negative adducts FALSE.

With ammonium formate or bicarbonate instead of ammonium acetate in your mobile phase,
check the negative adducts sheet 3 selects (`[M+CH3COO]-` here): the agent blocks a
selected adduct that the salt you name makes MS-DIAL drop.

## The demo's choices

These are the answers the MS-DIAL team gave for the demo; the tutorial walks through
all of them. Every one is still yours to give.

| Question | Demo answer |
| --- | --- |
| Workbook | `quant_utility_table.xlsx` from this folder |
| Amount column | H (pmol/µL plasma), with H4 = 10 as saved |
| Sheet 3 | as it is (no reconciliation question arises) |
| Salt | ammonium acetate (`CH3COONH4`) |
| Annotation RT tolerance | 1.5 min (internal standards: 0.5 min, the default) |
| Internal standards | `EquiSPLASH_Pos.txt` and `EquiSPLASH_Neg.txt` from this folder, not the archive's two texts |
| LBM2 | `Msp20231101141520_conventional_converted_dev_custum.lbm2` in `data2` |
