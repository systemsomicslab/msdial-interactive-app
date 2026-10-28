"""The QA's Reference matched never counts an in-silico match (the MS-DIAL author's decision of 2026-10-28).

A Console before the change marks a peak an in-silico match annotates "Reference matched" in its QA matrix; the
QA finds it from the per-file peak tables through the peak-ID matrix. A Console with the change writes it in its
own "In-silico matched" column. Both must give the same counts, and acquired and rule-based matches are unchanged.
"""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from msdial_app.qa_figure_outputs import QaMatrixReader, read_qa_matrix
from msdial_app.quality_assurance import IN_SILICO_MATCHED_COLUMN, build_lcms_qa_report_from_file

FILES = [("s1", "Case", "Sample", 1), ("s2", "Case", "Sample", 2), ("s3", "Control", "Sample", 3)]
QA_HEADER = ["ID", "File", "Class", "File type", "Injection order", "Batch ID", "Height", "RT", "MZ", "SN", "MSMS",
             "Reference matched"]
# Per spot and file: (peak ID or -1, evidence source, annotator) of the file's own peak.
# Spot 0: acquired in every file. Spot 1: in-silico in s1 and s2, absent in s3. Spot 2: rule-based in s1,
# in-silico in s2 (another file's acquired match represents it), unknown in s3. Spot 3: unannotated.
PEAKS = {
    0: {"s1": (10, "ReferenceSpectrum", "msp_high_quality"), "s2": (20, "ReferenceSpectrum", "msp_high_quality"),
        "s3": (30, "ReferenceSpectrum", "msp_high_quality")},
    1: {"s1": (11, "InSilico", "msp_in_silico"), "s2": (21, "InSilico", "msp_in_silico"), "s3": (-1, "", "")},
    2: {"s1": (12, "RuleBased", "lbm"), "s2": (22, "InSilico", "msp_in_silico"), "s3": (32, "", "")},
    3: {"s1": (13, "", ""), "s2": (23, "", ""), "s3": (33, "", "")},
}


def _write(path: Path, header: list[str], rows: list[list[object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\r\n")
        writer.writerow(header)
        writer.writerows(rows)


def _named(evidence: str) -> bool:
    return bool(evidence)


def write_run(root: Path, *, new_console: bool = False, peak_tables: bool = True, evidence_column: bool = True,
              settings: bool = True) -> Path:
    """A run folder as the Console writes it: QA matrix, peak-ID matrix, per-file .mdpeak and MSP settings."""
    header = QA_HEADER + ([IN_SILICO_MATCHED_COLUMN] if new_console else [])
    rows = []
    for spot, by_file in PEAKS.items():
        for name, class_name, file_type, order in FILES:
            peak, evidence, _annotator = by_file[name]
            accepted = peak >= 0 and _named(evidence)
            in_silico = accepted and evidence == "InSilico"
            # A Console before the change marks every accepted name Reference matched, in-silico ones included.
            reference = accepted and not (new_console and in_silico)
            row = [spot, name, class_name, file_type, order, 1, 0 if peak < 0 else 1000 * (spot + 1), 5.0 + spot,
                   100.0 + spot, 10, "TRUE", "TRUE" if reference else "FALSE"]
            if new_console:
                row.append("TRUE" if in_silico else "FALSE")
            rows.append(row)
    matrix = root / "AlignResult-1.qa.tsv"
    _write(matrix, header, rows)
    if not peak_tables:
        return matrix
    _write(root / "AlignResult-1.mdpeakid.tsv", ["alignment_master_id"] + [name for name, *_ in FILES],
           [[spot] + [by_file[name][0] for name, *_ in FILES] for spot, by_file in PEAKS.items()])
    for name, *_ in FILES:
        columns = ["Peak ID", "Name", "Comment", "Annotation tag (VS1.0)"] + (["Evidence source"] if evidence_column else [])
        table = []
        for spot, by_file in PEAKS.items():
            peak, evidence, annotator = by_file[name]
            if peak < 0:
                continue
            code = {"ReferenceSpectrum": 430, "RuleBased": 410, "InSilico": 430}.get(evidence, 999)
            row = [peak, f"compound {spot}" if evidence else "Unknown",
                   f"Annotation method: {annotator}" if annotator else "", code]
            if evidence_column:
                row.append(evidence or "null")
            table.append(row)
        _write(root / f"{name}.mdpeak", columns, table)
    if settings:
        _write(root / "msp_annotator_settings.tsv", ["annotator_id", "msp_file_path", "library_kind"],
               [["msp_high_quality", "acquired.msp", "acquired"], ["msp_in_silico", "predicted.msp", "predicted"]])
    return matrix


def counts(report: dict) -> dict[str, tuple[int, int]]:
    return {item["file"]: (item["reference_matched_count"], item["in_silico_matched_count"])
            for item in report["samples"]}


# s1: spot 0 acquired and spot 2 rule-based; s2: spot 0 only; s3: spot 0 only.
EXPECTED = {"s1": (2, 1), "s2": (1, 2), "s3": (1, 0)}


class QaReferenceMatchLeavesOutInSilicoTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_a_console_before_the_change_is_read_through_its_peak_tables(self) -> None:
        matrix = write_run(self.root)
        reader = QaMatrixReader()
        report = build_lcms_qa_report_from_file(matrix, spot_observer=reader.add_qa_spot)

        self.assertEqual(EXPECTED, counts(report))
        record = report["reference_matching"]
        self.assertEqual("peak_tables", record["source"])
        self.assertTrue(record["in_silico_excluded"])
        self.assertEqual(3, record["in_silico_matched_cells"])
        self.assertEqual(3, record["excluded_from_reference_matched_cells"])
        self.assertIn("AlignResult-1.mdpeakid.tsv", record["detail"])
        self.assertNotIn(str(self.root), record["detail"], "the record names files, never this machine's paths")
        self.assertFalse(any("in-silico" in item for item in report["warnings"]))
        self.assertIn("in-silico", report["method"]["reference_match"])
        # The figures count a feature reference matched where any study injection is: spots 0 and 2, not 1.
        self.assertEqual(2, reader.finish("run").pca_reference_matched)
        self.assertEqual(2, read_qa_matrix(matrix).pca_reference_matched)

    def test_a_console_with_the_change_gives_the_same_counts_from_its_own_column(self) -> None:
        matrix = write_run(self.root, new_console=True, peak_tables=False)
        report = build_lcms_qa_report_from_file(matrix)

        self.assertEqual(EXPECTED, counts(report))
        self.assertEqual("qa_matrix", report["reference_matching"]["source"])
        self.assertEqual(3, report["reference_matching"]["in_silico_matched_cells"])
        self.assertEqual(0, report["reference_matching"]["excluded_from_reference_matched_cells"])
        self.assertEqual(2, read_qa_matrix(matrix).pca_reference_matched)

    def test_acquired_and_rule_based_matches_are_counted_as_before(self) -> None:
        matrix = write_run(self.root)
        report = build_lcms_qa_report_from_file(matrix)
        # A run without the in-silico tier: the same matrix minus its in-silico cells.
        acquired_only = {name: (reference, 0) for name, (reference, _in_silico) in EXPECTED.items()}
        self.assertEqual(acquired_only, {name: (value[0], 0) for name, value in counts(report).items()})
        self.assertEqual(1, counts(report)["s1"][1])

    def test_the_annotator_settings_tell_an_in_silico_peak_where_the_table_records_no_evidence(self) -> None:
        matrix = write_run(self.root, evidence_column=False)
        report = build_lcms_qa_report_from_file(matrix)

        self.assertEqual(EXPECTED, counts(report))
        self.assertIn("msp_annotator_settings.tsv", report["reference_matching"]["detail"])

    def test_without_evidence_or_settings_it_is_not_established_and_says_so(self) -> None:
        matrix = write_run(self.root, evidence_column=False, settings=False)
        report = build_lcms_qa_report_from_file(matrix)

        self.assertEqual("not_established", report["reference_matching"]["source"])
        self.assertFalse(report["reference_matching"]["in_silico_excluded"])
        self.assertIn("records no evidence source", report["reference_matching"]["detail"])
        self.assertTrue(any("could not be established" in item for item in report["warnings"]))

    def test_a_matrix_with_nothing_beside_it_is_counted_as_written_with_a_warning(self) -> None:
        matrix = write_run(self.root, peak_tables=False)
        report = build_lcms_qa_report_from_file(matrix)

        self.assertEqual({"s1": (3, 0), "s2": (3, 0), "s3": (1, 0)}, counts(report))
        self.assertEqual("not_established", report["reference_matching"]["source"])
        self.assertIn("AlignResult-1.mdpeakid.tsv", report["reference_matching"]["detail"])
        warning = next(item for item in report["warnings"] if "could not be established" in item)
        self.assertIn("not identifications", warning)

    def test_a_missing_peak_table_is_not_read_as_no_in_silico_match(self) -> None:
        matrix = write_run(self.root)
        (self.root / "s2.mdpeak").unlink()
        report = build_lcms_qa_report_from_file(matrix)

        self.assertEqual("not_established", report["reference_matching"]["source"])
        self.assertIn("s2.mdpeak", report["reference_matching"]["detail"])

    def test_an_in_silico_peak_the_matrix_does_not_mark_stays_unmatched_and_uncounted(self) -> None:
        matrix = write_run(self.root)
        # As if the in-silico name of spot 1 in s1 were a suggestion: the matrix marks it FALSE.
        rows = list(csv.reader(matrix.open(encoding="utf-8", newline=""), delimiter="\t"))
        for row in rows[1:]:
            if row[0] == "1" and row[1] == "s1":
                row[11] = "FALSE"
        _write(matrix, rows[0], rows[1:])
        report = build_lcms_qa_report_from_file(matrix)

        self.assertEqual((2, 0), counts(report)["s1"])
        self.assertEqual(2, report["reference_matching"]["in_silico_matched_cells"])


if __name__ == "__main__":
    unittest.main()
