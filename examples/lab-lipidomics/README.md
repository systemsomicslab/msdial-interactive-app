# Example `quant_utility_table.xlsx`

`quant_utility_table.xlsx` is the workbook the MS-DIAL laboratory uses with the public
lipidomics demo (mouse plasma, SCIEX, EquiSPLASH internal standards; Zenodo
[10.5281/zenodo.10957708](https://doi.org/10.5281/zenodo.10957708), `data2`). The demo
archive does not include it, so it is shipped here. Its document properties carry no
author, and nothing else about it was changed.

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
| Workbook | this file |
| Amount column | H (pmol/µL plasma), with H4 = 10 as saved |
| Sheet 3 | as it is (no reconciliation question arises) |
| Salt | ammonium acetate (`CH3COONH4`) |
| Annotation RT tolerance | 1.5 min (internal standards: 0.5 min, the default) |
| Internal standards | the two `20200121_MsdialTxtDB_*_EquiSPLASH.txt` files in `data2` |
| LBM2 | `Msp20231101141520_conventional_converted_dev_custum.lbm2` in `data2` |
