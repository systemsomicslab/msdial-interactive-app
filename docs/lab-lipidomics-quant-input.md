# Laboratory lipidomics quantification input, format 1

This file specifies the one input that a person prepares for the laboratory lipidomics
workflow besides the raw data and the LBM2 library. It says, for one batch:

- which internal standards were added, how much of each, and where each appears in
  positive and negative mode (adduct, m/z, retention time);
- which standard quantifies which lipid class;
- which class and adduct each polarity reports.

MS-DIAL Interactive reads it without a language model. Every problem it finds is reported
with the table, the row and the column, in words a person can act on. The format is
called `msdial-lipidomics-quant-input/1`.

What the input does not hold: the raw data, the LBM2, the mobile-phase salt, the annotation
retention-time tolerance and the sample table. The workflow asks for those separately
(the [tutorial](tutorials/lab-lipidomics.md), step 2).

## 1. Containers

Prepare the four tables in **one Excel workbook (`.xlsx`)**. This is the recommended form.

| Table | Sheet name in the workbook | File name in a folder |
|---|---|---|
| Settings | `Settings` | `settings.tsv` or `settings.csv` |
| Standards | `Standards` | `standards.tsv` or `standards.csv` |
| Class standards | `Class standards` | `class_standards.tsv` or `class_standards.csv` |
| Selection | `Selection` | `selection.tsv` or `selection.csv` |

- **Workbook.** Sheets are found by name, ignoring case, spaces, `_` and `-`, so
  `class_standards` is the `Class standards` sheet. Their order does not matter. Any
  other sheet, such as `Instructions` or `Class names` in the template, is ignored. A
  workbook saved as `.xlsm` is read the same way; an old `.xls` is not read (save it as
  `.xlsx`).
- **Folder of text files.** For people who do not use Excel, or who write the tables
  with a script: one folder holding the four files above. A `.tsv` file is
  tab-separated and a `.csv` file comma-separated. Both are UTF-8 (a byte-order mark is
  allowed), and a field may be quoted with `"`. A line that begins with `#` is a
  comment. One table must not be given twice (`standards.tsv` and `standards.csv`).
- **The laboratory's earlier form** is still read: `quant_utility_table.xlsx` with its
  amount column (F, G or H), plus the internal-standard text libraries of each polarity.
  Section 9 shows how to convert it.

The workflow tools take either container as `quant_table_path`. A format-1 input holds
the standards' peaks itself, so `is_text_pos`, `is_text_neg` and `amount_column` are not
given with it.

## 2. Rules for every table

- **Row 1 is the header.** Columns are found by their header, in any order. Headers are
  compared ignoring case, spaces, punctuation and any text in parentheses, so
  `Spiked amount (pmol)`, `spiked_amount` and `SpikedAmount` are the same column.
- **Missing required column:** a problem. **Unknown column:** listed and ignored, so a
  column of your own notes is allowed. **The same column twice:** a problem.
- **Rows** run from row 2 to the last row with any content. A blank row is skipped; it
  does not end the table.
- **Numbers** are written with a decimal point: `0.5`, not `0,5`. An Excel number cell
  is read as its value. A formula cell is read as the value Excel last saved. A formula
  that was never calculated (a file written by a program and never opened in Excel) is
  a problem: open the workbook in Excel and save it.
- **Text** is trimmed of leading and trailing spaces.
- **Names, adducts and formulas are ASCII.** MS-DIAL reads its internal-standard library
  as ASCII, so a `µ` or a full-width character in a standard's name would never match.
- A workbook that is open in Excel is read as last saved, with a warning.

## 3. Settings

Two columns: `Setting` and `Value`. One row per setting.

| Setting | Required | Value | Example |
|---|---|---|---|
| `Format` | yes | exactly `msdial-lipidomics-quant-input/1` | `msdial-lipidomics-quant-input/1` |
| `Sample amount` | yes | how much sample one extract was made from, a positive number, in the unit of `Sample basis` | `10` |
| `Sample basis` | yes | `uL plasma`, `mg tissue` or `10^6 cells` | `uL plasma` |
| `Spiked amount unit` | no | the unit of the Standards table's `Spiked amount`: `pmol` (the default), `nmol` or `fmol` | `pmol` |
| `Description` | no | free text, read back in the check and never interpreted | `EquiSPLASH 5 uL into 20 uL plasma` |

- `Sample basis` also accepts `µL plasma`, `ul plasma` and `10e6 cells`.
- MS-DIAL names its volume unit after plasma. For serum or another fluid measured by
  volume, write `uL plasma`: the arithmetic is the same, and the output unit reads
  "per µL plasma".
- **The sample amount is one value for the batch.** A sample made from another amount,
  for example a tissue piece of another weight, is corrected by its dilution factor in
  the sample table. That factor multiplies the sample's concentrations, so it is
  nominal amount / actual amount (10 mg nominal, 8 mg weighed: 1.25).
- An unknown setting is listed and ignored. A setting given twice is a problem.

## 4. Standards

One row per internal standard. Its amount and its peaks in both polarities are on the
same row.

| Column | Required | Meaning | Example |
|---|---|---|---|
| `Name` | yes | the standard's name, as the results will show it; unique | `PC 15:0_18:1(d7)` |
| `Class` | yes | its lipid class (section 7) | `PC` |
| `Spiked amount` | yes | the amount of this standard in the extract of one sample, in `Spiked amount unit` | `663.917` |
| `Formula` | no | the neutral molecule's elemental formula; `D` is deuterium | `C41H73D7NO8P` |
| `InChIKey` | no | copied into MS-DIAL's library | |
| `SMILES` | no | copied into MS-DIAL's library | |
| `Pos adduct` | per polarity | the adduct MS-DIAL finds it as in positive mode | `[M+H]+` |
| `Pos m/z` | per polarity | that adduct's m/z | `753.6134` |
| `Pos RT` | per polarity | its retention time in minutes, on your chromatography | `9.5` |
| `Pos CCS` | no | its collision cross-section, for ion-mobility data | |
| `Neg adduct`, `Neg m/z`, `Neg RT`, `Neg CCS` | per polarity | the same in negative mode | `[M+CH3COO]-`, `811.6199`, `9.5` |

- **The amount you write is what you added.** The amount per sample unit is computed as
  `Spiked amount / Sample amount`, for example 663.917 pmol / 10 µL = 66.3917 pmol/µL
  plasma, and that becomes the concentration unit of the results.
- **Each polarity is optional per standard.** Leave all four cells of a polarity blank
  where the standard is not used in it. FA standards, for example, are often negative
  only. Once any cell of a polarity is filled, its adduct and RT are required, and so is
  its m/z unless `Formula` is given.
- **m/z from the formula.** With `Formula` given and the m/z blank, the m/z is computed
  from the monoisotopic masses and the adduct. With both given, a difference of more
  than 5 mDa is reported, because it usually means a mistyped m/z or the wrong adduct.
- **The RT is yours to measure.** The internal standard is looked for near this RT
  (within 0.5 min unless the workflow is told otherwise). An RT copied from another
  laboratory's gradient finds nothing, or the wrong peak.
- **Adducts are written as MS-DIAL writes them**: `[M+H]+`, `[M+NH4]+`, `[M+Na]+`,
  `[M-H]-`, `[M+CH3COO]-`, `[M+HCOO]-` and so on. An adduct of the wrong charge for its
  polarity is a problem.
- Every standard needs at least one polarity, and each polarity needs at least one
  standard.

## 5. Class standards

Which standard quantifies which lipid class. Two columns:

| Column | Required | Meaning | Example |
|---|---|---|---|
| `Class` | yes | a lipid class (section 7), or `Any others` | `EtherPC` |
| `Standard` | yes | a `Name` from the Standards table | `PC 15:0_18:1(d7)` |

- **`Any others` is required, exactly once.** It names the standard for every class not
  listed. A class listed twice is a problem: normalization would use the first row and
  ignore the second.
- A standard may quantify several classes. A standard that quantifies none is listed.
- **A class is quantified only in a polarity where its standard has a peak.** The check
  lists, before anything runs, the classes that will come out NOT QUANTIFIED because of
  that.

## 6. Selection

Which class and adduct each polarity reports in the final lipidome:

| Column | Required | Meaning | Example |
|---|---|---|---|
| `Class` | yes | a lipid class (section 7) | `PC` |
| `Adduct` | yes | the adduct that class is reported by | `[M+CH3COO]-` |
| `Polarity` | yes | `Positive` or `Negative` (`Pos`, `Neg`, `+` and `-` also work) | `Negative` |
| `Use` | no | `TRUE` or `FALSE`; without the column, or in a blank cell, `TRUE` | `TRUE` |

- List only the rows you report, or every row with `Use` saying which. Rows with
  `FALSE` are kept as a record and never reach the lipidome.
- An adduct of the wrong charge for its polarity is a problem in a used row. In a row
  marked `FALSE` it is only reported.
- **One class reported by two adducts counts each lipid twice.** This is reported and
  not resolved for you.
- **BMP and EtherPE (PE P-) are taken from negative `[M-H]-`.** Before the selection is
  applied, the merge renames negative PG rows that positive mode annotates as BMP, and
  negative PE O- rows that positive mode annotates as PE P-. A selection that takes
  either class from positive mode, or not from negative `[M-H]-`, is asked about before
  the contract.
- The salt must allow the selected negative adducts: ammonium acetate gives
  `[M+CH3COO]-`, ammonium formate `[M+HCOO]-`. The check compares the selection with the
  salt you name.

## 7. Lipid class names

MS-DIAL has two spellings for some classes:

- the LBM spelling (`Cer_NS`), which the LBM2 library and the exported results carry;
- the class-definition spelling (`Cer-NS`), which normalization looks a standard up by.

**Either spelling is accepted in every table of format 1.** Each name is converted to
the one its use needs, and the check lists every conversion, for example "Class
standards row 18: Cer_NS read as Cer-NS". A name MS-DIAL knows in neither spelling is a
problem in Class standards and Selection, because no lipid would ever carry it. In
Standards it is only reported, because there it only labels the standard.

The template's `Class names` sheet lists every class with both spellings.

## 8. What the check reports

`msdial_lipidomics_check_quant_input` reads the input on its own, so a person can
prepare it before any data are at hand. `msdial_lipidomics_check_inputs` reads it with
everything else. Both are read-only.

- **Problems (`blockers`)** stop the contract. Each names its place: `Standards row 7,
  Pos m/z: '753,6134' is not a number written with a decimal point`.
- **Warnings** do not stop anything: an m/z that differs from the formula, a class
  quantified against `Any others`, a class reported by two adducts.
- **Notes** list what was read differently from how it was written: converted class
  spellings, computed m/z values, ignored columns and sheets.
- **The tables as read**, for the person to look over: each standard with its amount per
  sample unit and its peaks; the class standards; the number of classes selected per
  polarity; the classes that will not be quantified in each polarity.

## 9. Getting started

`msdial_lipidomics_quant_input_template` writes a new input. It shows what it would
write first, and writes only when the call is repeated with `confirmed=true`. It never
overwrites a file. It writes:

- an empty template (the default): the tables with their headers, an `Instructions`
  sheet and a `Class names` sheet. Class standards and Selection hold the laboratory's
  defaults for the EquiSPLASH mixture: review them, then fill in Settings and one
  Standards row per standard you added.
  - With `salt="HCOONH4"`, the default selection takes negative-mode formate adducts in
    place of acetate where the defaults list both.
- a converted input: given the earlier `quant_utility_table.xlsx`, its amount column and
  both internal-standard text libraries, the same content in format 1. The spiked
  amount is sheet 1's `pmol` column where there is one, and otherwise the amount per
  sample unit times the sample amount (sheet 1, row 4).

`layout="tsv"` writes the folder form instead of a workbook.

A complete example is in [`examples/lab-lipidomics`](../examples/lab-lipidomics/README.md):
`quant_input.xlsx`, and the same tables as text files in `quant_input_tsv/`. Both are the
public demo's inputs in format 1.

## 10. Versions

The `Format` setting names the version. A later version will have another number. A
reader refuses a version it does not know rather than guess at it.
