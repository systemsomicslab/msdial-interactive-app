"""The QA figures: their arithmetic, their SVG, their pages, and the steps that write them.

Every input is synthetic and written under a temporary folder; nothing here runs MS-DIAL.
"""

from __future__ import annotations

import csv
import json
import math
import os
import tempfile
import threading
import unittest
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

_CONFIG = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"LOCALAPPDATA": _CONFIG.name}):
    from msdial_app import mcp_server, qa_figure_outputs, server
    from msdial_app import qa_figures as qf
    from msdial_app.materials_methods import generate_publication_report, qa_report_for_run
    from msdial_app.quality_assurance import build_lcms_qa_report_from_file


QA_COLUMNS = ["ID", "File", "Class", "File type", "Injection order", "Batch ID", "Height", "RT", "MZ", "SN",
              "MSMS", "Reference matched"]
# (stem, class, file type, injection order): two groups, a QC between them, a Blank last.
INJECTIONS = [
    ("S1", "SPF", "Sample", 1), ("S2", "GF", "Sample", 2), ("QC1", "QC", "QC", 3),
    ("S3", "SPF", "Sample", 4), ("S4", "GF", "Sample", 5), ("Blank", "Blank", "Blank", 6),
]


def file_name(stem: str, suffix: str = "Pos") -> str:
    return f"20261018_demo_{stem}_{suffix}"


def synthetic_heights(spot: int, index: int, stem: str) -> float:
    """A deterministic height: groups differ on the even spots, the Blank is low on most."""
    base = 1000.0 + 150.0 * spot
    if stem == "Blank":
        return 0.0 if spot % 3 else base * 0.9
    group = {"S1": 1.0, "S3": 1.1, "S2": 1.8, "S4": 1.7, "QC1": 1.4}.get(stem, 1.3)
    factor = group if spot % 2 == 0 else 1.0 + 0.05 * index
    return round(base * factor + 7 * ((spot * 7 + index * 3) % 5), 1)


def write_qa_matrix(path: Path, *, spots: int = 24, injections=INJECTIONS, suffix: str = "Pos",
                    cell=None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(QA_COLUMNS)
        for spot in range(spots):
            for index, (stem, klass, kind, order) in enumerate(injections):
                height = synthetic_heights(spot, index, stem)
                text = cell(spot, stem, height) if cell else f"{height:g}"
                matched = "TRUE" if spot % 4 != 3 and stem != "Blank" else "FALSE"
                writer.writerow([spot, file_name(stem, suffix), klass, kind, order, 1, text, 5.0 + spot * 0.3,
                                 400.0 + spot, 50.0, "TRUE", matched])
    return path


def write_anchor_audit(root: Path, injections=INJECTIONS, suffix: str = "Pos") -> None:
    rows = ["File ID\tFile name\tAnchor ID\tm/z\tReference RT (min)\tOriginal RT (min)\tOffset (min)\tQuality score"
            "\tNon-Blank sample coverage\tUsed\tStatus"]
    summary = ["File ID\tFile name\tFile type\tAnalytical order\tModel source"]
    for file_id, (stem, _, kind, order) in enumerate(injections):
        source = "Reference" if stem == "S2" else ("NearestBlank" if kind == "Blank" else "DetectedAnchors")
        summary.append(f"{file_id}\t{file_name(stem, suffix)}\t{kind}\t{order}\t{source}")
        for anchor in range(5):
            offset = 0.0 if source == "Reference" else 0.01 * ((anchor + file_id) % 3 - 1)
            used = source != "NearestBlank" and anchor != 4
            rows.append(f"{file_id}\t{file_name(stem, suffix)}\t{anchor}\t500\t{2.0 + 3 * anchor}\t"
                        f"{2.0 + 3 * anchor + offset}\t{offset}\t0.8\t1\t{'True' if used else 'False'}\tUsed")
    (root / qa_figure_outputs.ANCHORS_NAME).write_text("\n".join(rows) + "\n", encoding="utf-8")
    (root / qa_figure_outputs.SUMMARY_NAME).write_text("\n".join(summary) + "\n", encoding="utf-8")


def run_from(root: Path, **options) -> qf.RunQa:
    qa = write_qa_matrix(root / "AlignResult-1.qa.tsv", **options)
    run = qa_figure_outputs.read_qa_matrix(qa, "Positive")
    return run


def parse(svg: str) -> ET.Element:
    return ET.fromstring(svg)


SVG = "{http://www.w3.org/2000/svg}"


def svg_text(svg: str) -> str:
    """Every text element's text, joined by spaces (a wrapped subtitle reads as one sentence)."""
    return " ".join("".join(node.itertext()) for node in parse(svg).iter(f"{SVG}text"))


def tooltip_of(node: ET.Element) -> str:
    title = node.find(f"{SVG}title")
    return title.text if title is not None and title.text else ""


class TheArithmetic(unittest.TestCase):
    def test_pca_of_a_hand_checked_matrix(self) -> None:
        # Two features over four samples. Autoscaled, their correlation r gives eigenvalues 1 +/- r of
        # the correlation matrix, and the scores are (z1 + z2)/sqrt(2) and (z1 - z2)/sqrt(2).
        f1, f2 = [2.0, 4.0, 6.0, 8.0], [1.0, 1.0, 2.0, 2.0]
        result = qf.principal_components([f1, f2])

        def z(values):
            mean = sum(values) / len(values)
            sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))
            return [(v - mean) / sd for v in values]

        z1, z2 = z(f1), z(f2)
        r = sum(a * b for a, b in zip(z1, z2)) / (len(f1) - 1)
        self.assertAlmostEqual((1 + r) / 2, result["explained"][0], places=10)
        self.assertAlmostEqual((1 - r) / 2, result["explained"][1], places=10)
        expected = [[(a + b) / math.sqrt(2) for a, b in zip(z1, z2)], [(a - b) / math.sqrt(2) for a, b in zip(z1, z2)]]
        for component in range(2):
            got = [row[component] for row in result["scores"]]
            sign = 1.0 if got[0] * expected[component][0] > 0 else -1.0  # signs normalised here
            for left, right in zip(got, expected[component]):
                self.assertAlmostEqual(right, sign * left, places=9)
        self.assertEqual((2, 4, "jacobi"), (result["features"], result["samples"], result["method"]))

    def test_pca_fixes_the_sign_and_drops_features_without_variance(self) -> None:
        result = qf.principal_components([[1.0, 2.0, 3.0], [3.0, 2.0, 1.0], [5.0, 5.0, 5.0]])
        self.assertEqual(2, result["features"])  # the constant row carries no variance
        pc1 = [row[0] for row in result["scores"]]
        self.assertAlmostEqual(1.0, result["explained"][0], places=12)
        self.assertAlmostEqual(math.sqrt(2), max(pc1, key=abs), places=9)  # the largest score is positive
        self.assertAlmostEqual(0.0, pc1[1], places=9)

    def test_pca_refuses_too_few_samples_or_features(self) -> None:
        with self.assertRaisesRegex(qf.FigureSkipped, "at least three study injections; there are 2"):
            qf.principal_components([[1.0, 2.0], [2.0, 1.0]])
        with self.assertRaisesRegex(qf.FigureSkipped, "at least two features that vary"):
            qf.principal_components([[1.0, 2.0, 3.0], [4.0, 4.0, 4.0]])

    def test_subspace_iteration_agrees_with_jacobi(self) -> None:
        matrix = [[math.sin(0.37 * row * (column + 1)) + 0.1 * column for column in range(12)] for row in range(30)]
        exact = qf.principal_components(matrix)
        iterated = qf.principal_components(matrix, jacobi_limit=2)
        self.assertEqual(("jacobi", "subspace_iteration"), (exact["method"], iterated["method"]))
        for left, right in zip(exact["explained"], iterated["explained"]):
            self.assertAlmostEqual(left, right, places=9)
        for left, right in zip(exact["scores"], iterated["scores"]):
            for a, b in zip(left, right):
                self.assertAlmostEqual(a, b, places=6)

    def test_jacobi_eigenvalues_of_a_known_matrix(self) -> None:
        values, vectors = qf.jacobi_eigen([[2.0, 1.0], [1.0, 2.0]])
        self.assertEqual([1.0, 3.0], sorted(round(value, 12) for value in values))
        for value, vector in zip(values, vectors):
            product = [2 * vector[0] + vector[1], vector[0] + 2 * vector[1]]
            self.assertAlmostEqual(value * vector[0], product[0], places=12)

    def test_relative_response_and_cv(self) -> None:
        response = qf.relative_response([90.0, 100.0, None, 110.0])
        self.assertEqual(100.0, response["mean"])
        self.assertEqual([90.0, 100.0, None, 110.0], [None if v is None else round(v, 10) for v in response["relative"]])
        self.assertAlmostEqual(10.0, response["cv_percent"], places=10)  # sd (n - 1) = 10 over a mean of 100
        self.assertIsNone(qf.relative_response([5.0])["cv_percent"])
        self.assertIsNone(qf.relative_response([None, None])["mean"])

    def test_ratio_bins_and_the_share_at_three_times(self) -> None:
        self.assertEqual([0, 1, 5, -2, 0], [qf.ratio_bin(v) for v in (0.04, 0.06, 0.477, -0.2, -0.04)])
        values = [(qf.log_ratio(2.0, 0.0), True), (qf.log_ratio(10.0, 10.0), False), (qf.log_ratio(99.0, 0.0), False),
                  (qf.log_ratio(0.0, 99.0), True)]
        self.assertEqual(math.log10(3), values[0][0])  # (2 + 1) / (0 + 1) is exactly 3x and counts
        self.assertEqual(0.5, qf.share_at_least(values))
        histogram = qf.ratio_histogram(values)
        self.assertEqual({-20: [1, 0], 0: [0, 1], 5: [1, 0], 20: [0, 1]}, histogram)

    def test_box_statistics_on_the_bin_grid(self) -> None:
        bins: dict[int, int] = {}
        for value in (1.0, 2.0, 3.0, 4.0, 10.0):
            key = round(value / qf.HEIGHT_BIN_WIDTH)
            bins[key] = bins.get(key, 0) + 1
        stats = qf.box_statistics(bins)
        self.assertEqual((2.0, 3.0, 4.0), (stats["q1"], stats["median"], stats["q3"]))
        self.assertEqual((1.0, 4.0), (stats["whisker_low"], stats["whisker_high"]))
        self.assertEqual([10.0], stats["outliers"])
        self.assertIsNone(qf.box_statistics({}))

    def test_short_labels_drop_what_every_name_shares(self) -> None:
        names = ["190722_TT1_Hclass_Aging_004_2_9w_SPF_M_Plasma_2_Pos", "190722_TT1_Hclass_Aging_010_13_9w_GF_M_Plasma_1_Pos",
                 "190722_TT1_Hclass_Aging_Blank_Pos"]
        injections = [qf.Injection(names[0], "SPF", "Sample", "Sample", 1), qf.Injection(names[1], "GF", "Sample", "Sample", 2),
                      qf.Injection(names[2], "Blank", "Blank", "Blank", 3)]
        self.assertEqual(["SPF 004", "GF 010", "Blank"], qf.short_labels(injections))
        twins = [qf.Injection("a_1", "X", "Sample", "Sample", 1), qf.Injection("a_1", "X", "Sample", "Sample", 2)]
        self.assertEqual(["X #1", "X #2"], qf.short_labels(twins))  # nothing but the order tells them apart

    def test_text_is_shortened_to_fit(self) -> None:
        long = "PC 15:0_18:1(d7) with a very long description that cannot fit in one narrow panel title"
        shown = qf.fit_text(long, 120, qf.STRIP_SIZE, bold=True)
        self.assertTrue(shown.endswith("\u2026"))
        self.assertLessEqual(qf.text_width(shown, qf.STRIP_SIZE, bold=True), 120)
        self.assertEqual("short", qf.fit_text("short", 120, qf.STRIP_SIZE))

    def test_group_colours_are_stable_whatever_else_is_present(self) -> None:
        one = qf.group_colors(["SPF", "GF", "Blank"])
        two = qf.group_colors(["GF", "SPF", "QC"])
        self.assertEqual((one["GF"], one["SPF"]), (two["GF"], two["SPF"]))
        self.assertEqual((qf.SERIES[0], qf.SERIES[1], qf.BLANK_COLOR, qf.QC_COLOR),
                         (one["GF"], one["SPF"], one["Blank"], two["QC"]))
        many = qf.group_colors([f"G{index:02d}" for index in range(10)])
        # No generated hue: the ninth and tenth groups reuse the palette with squares.
        self.assertEqual((qf.SERIES[0], "square"), (many["G08"], qf.group_shape(many, "G08")))
        self.assertEqual((qf.SERIES[1], "square"), (many["G09"], qf.group_shape(many, "G09")))
        self.assertEqual("dot", qf.group_shape(many, "G00"))
        self.assertEqual("cross", qf.group_shape(many, "Blank"))

    def test_every_group_keeps_its_own_colour_and_shape(self) -> None:
        names = [f"G{index:02d}" for index in range(len(qf.SERIES) * len(qf.SHAPES))]
        colors = qf.group_colors(names + ["QC", "Blank"])
        styles = {(colors[name], qf.group_shape(colors, name)) for name in names}
        self.assertEqual(len(names), len(styles))
        legend = qf.group_legend(names, colors)
        self.assertEqual(len(names), len({(color, shape) for _, color, shape in legend}))
        # Beyond the palette's styles a group is "other", and the legend says so once.
        more = qf.group_colors(names + ["Z1", "Z2"])
        self.assertEqual((qf.OTHER_COLOR, qf.OTHER_COLOR), (more["Z1"], more["Z2"]))
        self.assertEqual(("2 other group(s), not told apart", qf.OTHER_COLOR, "dot"),
                         qf.group_legend(names + ["Z1", "Z2"], more)[-1])

    def test_a_box_median_stands_out_on_every_group_colour(self) -> None:
        for fill in (*qf.SERIES, qf.QC_COLOR, qf.BLANK_COLOR, qf.OTHER_COLOR):
            ink = qf.ink_on(fill)
            self.assertNotEqual(fill, ink)
            self.assertGreaterEqual(qf.contrast_ratio(fill, ink), 3.0, fill)
        self.assertEqual(qf.SURFACE, qf.ink_on(qf.QC_COLOR))  # light on the QC box, which is near-black

    def test_a_nice_axis_ends_on_ticks(self) -> None:
        low, high, ticks, step = qf.nice_axis(877.0, 1510.0, 6)
        self.assertEqual((800.0, 1600.0, 200.0), (low, high, step))
        self.assertEqual([800.0, 1000.0, 1200.0, 1400.0, 1600.0], ticks)


class TheFigures(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_the_run_figures_are_well_formed_and_byte_stable(self) -> None:
        write_anchor_audit(self.root)
        run = run_from(self.root)
        qa_figure_outputs.attach_rt_anchors(run, self.root)
        first = qf.run_figures([run])
        second = qf.run_figures([run])
        self.assertEqual(["01", "02", "03", "05", "06", "07"], [figure.key for figure in first])
        for left, right in zip(first, second):
            self.assertIsNotNone(left.svg, (left.key, left.skipped))
            self.assertEqual(left.svg, right.svg)
            element = parse(left.svg)
            self.assertEqual("{http://www.w3.org/2000/svg}svg", element.tag)
            self.assertNotIn(str(self.root), left.svg)

    def test_the_quantities_read_from_the_matrix(self) -> None:
        run = run_from(self.root)
        self.assertEqual(24, run.feature_count)
        self.assertEqual(["S1", "S2", "QC1", "S3", "S4", "Blank"], [item.label.split()[-1] for item in run.injections])
        blank = run.injections[-1]
        self.assertEqual(("Blank", "Blank"), (blank.category, blank.group))
        self.assertEqual(8, run.detected[-1])  # the Blank has a peak on every third spot
        self.assertEqual([24] * 5, run.detected[:5])
        self.assertEqual(18, run.pca_reference_matched)  # every fourth spot is matched nowhere
        self.assertEqual([0, 1, 2, 3, 4], run.pca_injections)  # the QC with the samples, the Blank not
        self.assertEqual(24, len(run.blank_ratios))
        spot = 1
        sample_mean = sum(synthetic_heights(spot, i, stem) for i, (stem, _, kind, _) in enumerate(INJECTIONS)
                          if kind == "Sample") / 4
        self.assertAlmostEqual(qf.log_ratio(sample_mean, 0.0), run.blank_ratios[spot][0], places=12)

    def test_empty_and_nan_heights_count_as_not_detected(self) -> None:
        def cell(spot, stem, height):
            return "NaN" if (spot, stem) == (0, "S1") else ("" if (spot, stem) == (1, "S2") else f"{height:g}")

        run = run_from(self.root, cell=cell)
        self.assertEqual(2, run.non_numeric_cells)
        self.assertEqual([23, 23], run.detected[:2])
        for figure in qf.run_figures([run]):
            if figure.svg:
                parse(figure.svg)
        _, page = qa_figure_outputs.run_page(run, "AlignResult-1.qa.tsv")
        self.assertIn("2 cell(s) had no numeric height and count as not detected", page)

    def test_figures_are_skipped_with_their_reason(self) -> None:
        no_blank = [item for item in INJECTIONS if item[2] != "Blank"]
        run = run_from(self.root, injections=no_blank)
        figures = {figure.key: figure for figure in qf.run_figures([run])}
        self.assertEqual("the run has no Blank injection", figures["06"].skipped)
        self.assertIn("automatic RT correction audit", figures["07"].skipped)
        self.assertIsNotNone(figures["05"].svg)
        two = run_from(self.root / "two", injections=[INJECTIONS[0], INJECTIONS[1], INJECTIONS[5]])
        figures = {figure.key: figure for figure in qf.run_figures([two])}
        self.assertEqual("PCA needs at least three study injections; there are 2", figures["05"].skipped)
        self.assertIsNotNone(figures["01"].svg)

    def test_a_long_run_stays_legible(self) -> None:
        injections = [(f"P{order:03d}", "Case" if order % 2 else "Control", "Sample", order) for order in range(1, 60)]
        injections.append(("Blank", "Blank", "Blank", 60))
        write_anchor_audit(self.root, injections)
        run = run_from(self.root, injections=injections, spots=8)
        qa_figure_outputs.attach_rt_anchors(run, self.root)
        figures = {figure.key: figure for figure in qf.run_figures([run])}
        rt_texts = [node.text for node in parse(figures["07"].svg).iter("{http://www.w3.org/2000/svg}text")]
        self.assertIn("Positive: 48 of 60 injections, evenly spaced", rt_texts)
        pca = parse(figures["05"].svg)
        labels = [node.text for node in pca.iter("{http://www.w3.org/2000/svg}text") if (node.text or "").count("#")]
        self.assertEqual([], labels)  # 59 study injections: points name themselves on hover only
        points = [node for node in pca.iter("{http://www.w3.org/2000/svg}circle")
                  if node.find("{http://www.w3.org/2000/svg}title") is not None]  # the legend's have no tooltip
        self.assertEqual(59, len(points))

    def test_a_qc_box_shows_its_median(self) -> None:
        run = run_from(self.root)
        children = list(parse(qf.height_distribution_figure([run]).svg))
        boxes = [index for index, node in enumerate(children)
                 if node.tag == f"{SVG}rect" and tooltip_of(node).startswith(("QC ", "SPF ", "GF ", "Blank"))]
        self.assertEqual(6, len(boxes))
        for index in boxes:
            box, median = children[index], children[index + 1]
            self.assertEqual(f"{SVG}line", median.tag)
            self.assertNotEqual(box.get("fill"), median.get("stroke"), tooltip_of(box))
            if tooltip_of(box).startswith("QC "):
                self.assertEqual((qf.QC_COLOR, qf.SURFACE), (box.get("fill"), median.get("stroke")))

    def test_counts_and_sums_are_read_between_labelled_lines_and_join_study_injections_only(self) -> None:
        injections = [INJECTIONS[0], ("Blank", "Blank", "Blank", 2), *INJECTIONS[1:5]]
        injections = [(stem, klass, kind, order + 1) for order, (stem, klass, kind, _) in enumerate(injections)]
        run = run_from(self.root, injections=injections)
        for figure in (qf.detected_features_figure([run]), qf.summed_height_figure([run])):
            element = parse(figure.svg)
            labels = [float(node.text.replace(",", "")) for node in element.iter(f"{SVG}text")
                      if node.get("text-anchor") == "end" and node.text and node.text.replace(",", "").replace(".", "").isdigit()]
            values = run.detected if figure.key == "01" else [value / 1e3 for value in run.summed]
            self.assertLessEqual(max(values), max(labels), figure.key)
            self.assertGreaterEqual(min(values), min(labels), figure.key)
            lines = list(element.iter(f"{SVG}polyline"))
            self.assertEqual(1, len(lines))
            self.assertEqual(5, len(lines[0].get("points").split()))  # the Blank between them is not joined

    def test_the_ratio_figure_states_the_qa_criterion_beside_its_own_share(self) -> None:
        qa = write_qa_matrix(self.root / "AlignResult-1.qa.tsv")
        run = qa_figure_outputs.read_qa_matrix(qa, "Positive")
        criterion = build_lcms_qa_report_from_file(qa)["summary"]["sample_blank_ratio_ge_3"]
        at_least, counted = run.blank_criterion
        self.assertEqual(8, counted)  # the Blank has a peak on every third spot only
        self.assertEqual(criterion, at_least / counted)
        text = svg_text(qf.sample_blank_ratio_figure([run]).svg)
        self.assertIn("% of features at >= 3x by mean (no Blank signal counts as >= 3x)", text)
        self.assertIn(f"QA criterion (median, 8 Blank-detected features): {criterion * 100:.0f}%", text)
        self.assertIn("counts at or above 3x here", qf.FIGURES["06"]["how"] + text)

    def test_the_standards_figure_names_the_standards_it_does_not_draw(self) -> None:
        rows = [{"StandardName": "PC d7", "PeakID": "5"}, {"StandardName": "PC d7", "PeakID": "5"},
                {"StandardName": "FA 18:0(d3)", "PeakID": "-1"}, {"StandardName": "CE d7", "PeakID": "-1"}]
        resolution = [{"standard": "PC d7", "status": "resolved_lbm"}, {"standard": "FA 16:0(d3)", "status": "resolved_text"},
                      {"standard": "TG d7", "status": "unresolved_expected"}, {"name": "LPC d7", "status": "resolved_search"}]
        self.assertEqual(["FA 18:0(d3)", "CE d7"], qa_figure_outputs.unresolved_standards(rows))
        self.assertEqual(["FA 16:0(d3)", "LPC d7"], qa_figure_outputs.resolved_but_unused(resolution, rows))
        notes = qa_figure_outputs.standard_notes(rows, resolution)
        injections = [qf.Injection(file_name(stem), klass, kind, "Blank" if kind == "Blank" else "Sample", order)
                      for stem, klass, kind, order in INJECTIONS if kind != "QC"]
        one = [qf.StandardResponse("PC d7", "5", [90.0, 100.0, 110.0, 100.0, 5.0])]
        text = svg_text(qf.internal_standard_figure("Positive", injections, one, notes=notes).svg)
        self.assertIn("1 standard used by normalize", text)
        self.assertIn("Not resolved in this polarity, so the classes they serve are not quantified from it: "
                      "FA 18:0(d3), CE d7", text)
        self.assertIn("Resolved but not used by normalize (the quantification rules do not name them): "
                      "FA 16:0(d3), LPC d7", text)
        two = one + [qf.StandardResponse("PE d7", "6", [100.0, 100.0, 100.0, 100.0, 1.0])]
        self.assertIn("2 standards used by normalize", svg_text(qf.internal_standard_figure("Positive", injections, two).svg))
        none = qf.internal_standard_figure("Positive", injections, [], notes=notes)
        self.assertIn("FA 18:0(d3), CE d7", none.skipped)

    def test_samples_of_one_group_with_equal_totals_stay_apart(self) -> None:
        samples = ["GF_1", "GF_2", "GF_3", "SPF_1", "SPF_2", "SPF_3"]
        groups = ["GF", "GF", "GF", "SPF", "SPF", "SPF"]
        values = [[18.817, 5.0], [18.806, 5.0], [18.968, 5.01], [12.0, 5.0], [12.0, 5.0], [12.01, 5.0]]
        table = qf.ClassTotals(samples=samples, groups=groups, classes=["EtherPE", "CE"], values=values)
        element = parse(qf.lipid_class_totals_figure(table).svg)
        points = [(float(node.get("cx")), float(node.get("cy")), tooltip_of(node)) for node in element.iter(f"{SVG}circle")
                  if tooltip_of(node)]
        self.assertEqual(12, len(points))
        for first in range(len(points)):
            for second in range(first + 1, len(points)):
                (x1, y1, a), (x2, y2, b) = points[first], points[second]
                if a.split(",")[0] == b.split(",")[0] and a.split("(")[-1] == b.split("(")[-1]:
                    self.assertGreater(math.hypot(x1 - x2, y1 - y2), 3.5, (a, b))  # more than a marker radius

    def test_a_long_run_draws_one_outlier_per_side_of_a_box(self) -> None:
        injections = [(f"L{order:04d}", ("Case", "Control")[order % 2], "Sample", order) for order in range(1, 1001)]

        def cell(spot, stem, height):
            extreme = {26: 10 ** 7.5, 27: 10 ** 8, 28: 10 ** 0.3, 29: 10 ** 0.5}
            return f"{extreme.get(spot, 10 ** (3 + 0.01 * spot)):g}"

        run = run_from(self.root, injections=injections, spots=30, cell=cell)
        figure = qf.height_distribution_figure([run])
        size = len(figure.svg.encode("utf-8"))
        self.assertLess(size, 700_000)
        self.assertEqual(2 * 1000, figure.svg.count("<circle"))  # four outliers each, the most extreme of a side drawn
        self.assertIn("only its most extreme outlier on each side is drawn", svg_text(figure.svg))
        short = run_from(self.root / "short", injections=injections[:6], spots=30, cell=cell)
        self.assertEqual(4 * 6, qf.height_distribution_figure([short]).svg.count("<circle"))  # wide boxes: all of them

    def test_the_pca_bounds_choose_spread_injections_and_the_most_variable_features(self) -> None:
        injections = [(f"S{order:02d}", ("A", "B")[order % 2], "Sample", order) for order in range(1, 14) if order != 7]
        injections.insert(6, ("Blank", "Blank", "Blank", 7))

        def cell(spot, stem, height):
            if stem == "Blank":
                return "0"
            order = int(stem[1:])
            return f"{1000 * (1 + (spot + 1) * 0.01 * order) + 3 * ((spot * order) % 5):g}"

        with patch.object(qa_figure_outputs, "PCA_SAMPLE_LIMIT", 5), patch.object(qa_figure_outputs, "PCA_FEATURE_LIMIT", 10):
            run = run_from(self.root, injections=injections, spots=30, cell=cell)
        study = [position for position, item in enumerate(run.injections) if item.category != "Blank"]
        self.assertEqual([study[index] for index in (0, 3, 6, 8, 11)], run.pca_injections)  # first, last and between
        self.assertEqual([1, 4, 8, 10, 13], [run.injections[position].order for position in run.pca_injections])
        self.assertTrue(run.pca_sample_limited and run.pca_feature_limited)
        # The ten matched features of greatest variance over those injections, in matrix order.
        matched = [spot for spot in range(30) if spot % 4 != 3]
        vectors = {spot: [math.log10(float(cell(spot, run.injections[position].file.split("_")[2], 0)) + 1.0)
                          for position in run.pca_injections] for spot in matched}

        def variance(values):
            mean = sum(values) / len(values)
            return sum((value - mean) ** 2 for value in values)

        chosen = sorted(sorted(matched, key=lambda spot: (-variance(vectors[spot]), spot))[:10])
        self.assertEqual([vectors[spot] for spot in chosen], run.pca_matrix)
        self.assertEqual(len(matched), run.pca_reference_matched)
        text = svg_text(qf.pca_figure([run]).svg)
        self.assertIn(f"Positive: the 10 most variable of {len(matched)} features", text)
        self.assertIn("Positive: 5 study injections spread over the run order", text)

    def test_an_anchor_file_of_another_run_is_not_drawn(self) -> None:
        write_anchor_audit(self.root, suffix="Neg")
        run = run_from(self.root)
        qa_figure_outputs.attach_rt_anchors(run, self.root)
        figure = qf.rt_correction_figure([run])
        self.assertIn("names none of this alignment's files", figure.skipped)

    def test_names_that_xml_would_misread_are_escaped(self) -> None:
        injections = [("A&B<1>", "G\"1", "Sample", 1), ("C\x01D", "G'2", "Sample", 2), ("E", "G\"1", "Sample", 3),
                      ("Blank", "Blank", "Blank", 4)]
        run = run_from(self.root, injections=injections)
        for figure in qf.run_figures([run]):
            if figure.svg:
                parse(figure.svg)

    def test_internal_standard_figure(self) -> None:
        injections = [qf.Injection(file_name(stem), klass, kind, "Blank" if kind == "Blank" else "Sample", order)
                      for stem, klass, kind, order in INJECTIONS if kind != "QC"]
        for item, label in zip(injections, qf.short_labels(injections)):
            item.label = label
        long_name = "TG 15:0_18:1(d7)_15:0 " + "x" * 120
        standards = [qf.StandardResponse("PC 15:0_18:1(d7)", "1", [90.0, 100.0, 110.0, 100.0, 5.0]),
                     qf.StandardResponse(long_name, "2", [100.0, 0.0, 100.0, None, 0.0])]
        figure = qf.internal_standard_figure("Positive", injections, standards)
        element = parse(figure.svg)
        texts = ["".join(node.itertext()) for node in element.iter("{http://www.w3.org/2000/svg}text")]
        self.assertIn("CV 8%", texts)  # 90, 100, 110, 100: sd 8.16 over a mean of 100
        self.assertTrue(any(text.startswith("TG 15:0_18:1(d7)_15:0") and "\u2026" in text for text in texts))
        self.assertEqual("04_internal_standards_positive.svg", figure.file_name)
        one = qf.internal_standard_figure("Negative", injections[:1] + injections[-1:], standards)
        self.assertIn("at least two non-Blank injections; there are 1", one.skipped)
        none = qf.internal_standard_figure("Negative", injections, [])
        self.assertIn("no resolved internal standard", none.skipped)

    def test_lipid_class_totals_figure(self) -> None:
        table = qf.ClassTotals(samples=["SPF_1", "GF_2"], groups=["SPF", "GF"], classes=["PC", "TG", "BileAcid"],
                               values=[[900.0, 200.0, 0.01], [800.0, 600.0, None]])
        figure = qf.lipid_class_totals_figure(table, "pmol_per_microL_plasma")
        element = parse(figure.svg)
        texts = ["".join(node.itertext()) for node in element.iter("{http://www.w3.org/2000/svg}text")]
        classes = [text for text in texts if text in ("PC", "TG", "BileAcid")]
        self.assertEqual(["PC", "TG", "BileAcid"], classes)  # largest median first, at the top
        self.assertIn("pmol/uL plasma", texts)
        empty = qf.ClassTotals(samples=["S"], groups=["G"], classes=["PC"], values=[[0.0]])
        self.assertIn("no positive concentration", qf.lipid_class_totals_figure(empty).skipped)

    def test_the_page_inlines_each_figure_with_its_caption(self) -> None:
        run = run_from(self.root)
        figures, page = qa_figure_outputs.run_page(run, "AlignResult-1.qa.tsv")
        self.assertEqual(sum(1 for figure in figures if figure.svg), page.count("<svg "))
        for figure in figures:
            self.assertIn(figure.title.replace("'", "&#x27;"), page)
            if figure.svg:
                self.assertIn(qf.escape(figure.how_to_read), page)
            else:
                self.assertIn(f"Not drawn: {qf.escape(figure.skipped)}", page)
        self.assertNotIn(str(self.root), page)

    def test_both_polarities_and_a_missing_one(self) -> None:
        positive = run_from(self.root / "pos")
        negative = qa_figure_outputs.read_qa_matrix(
            write_qa_matrix(self.root / "neg" / "AlignResult-2.qa.tsv", suffix="Neg"), "Negative")
        for figure in qf.run_figures([positive, negative]):
            if figure.svg:
                element = parse(figure.svg)
                texts = "".join("".join(node.itertext()) for node in element.iter("{http://www.w3.org/2000/svg}text"))
                self.assertIn("Positive", texts, figure.key)
                self.assertIn("Negative", texts, figure.key)
        standards = {"Positive": ([], [], [])}
        figures, page, notes = qa_figure_outputs.lipidomics_page([positive], standards, None, "")
        keys = {figure.key: figure for figure in figures}
        self.assertIn("standards table or Height matrix normalize wrote is missing", keys["04_negative"].skipped)
        self.assertIn("no class summary", keys["08"].skipped)
        self.assertIsNotNone(keys["01"].svg)


class TheWrittenFolders(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_a_run_folder_is_written_the_same_twice(self) -> None:
        qa = write_qa_matrix(self.root / "AlignResult-1.qa.tsv")
        (self.root / "method.txt").write_text("Ion mode: Negative\n", encoding="utf-8")
        write_anchor_audit(self.root)
        first = qa_figure_outputs.write_run_qa_figures(qa, self.root)
        folder = self.root / "qa_figures"
        snapshot = {path.name: path.read_bytes() for path in folder.iterdir()}
        second = qa_figure_outputs.write_run_qa_figures(qa, self.root)
        self.assertEqual(first, second)
        self.assertEqual(snapshot, {path.name: path.read_bytes() for path in folder.iterdir()})
        self.assertEqual("written", first["status"])
        self.assertEqual((folder / "qa_report.html").resolve(), Path(first["html"]).resolve())
        self.assertEqual(6, len(first["figures"]))
        self.assertEqual({"file", "title"}, set(first["figures"][0]))
        self.assertNotIn("<svg", json.dumps(first))
        manifest = json.loads((folder / "figures.json").read_text(encoding="utf-8"))
        self.assertEqual(qa_figure_outputs.file_sha256(qa), manifest["sources"]["qa_matrix"]["sha256"])
        self.assertNotIn(str(self.root), (folder / "figures.json").read_text(encoding="utf-8"))
        self.assertIn("Negative", (folder / "01_detected_features_by_injection.svg").read_text(encoding="utf-8"))

    def test_what_is_written_passes_a_shared_bundles_scan(self) -> None:
        from msdial_app.sharing import SharingContext

        qa = write_qa_matrix(self.root / "AlignResult-1.qa.tsv")
        write_anchor_audit(self.root)
        qa_figure_outputs.write_run_qa_figures(qa, self.root)
        context = SharingContext([], [], full=True)  # a repository unit's: any path of this machine is refused
        for path in sorted((self.root / "qa_figures").iterdir()):
            self.assertEqual([], context.scan(path.name, path.read_bytes()), path.name)

    def test_a_figure_no_longer_drawn_leaves_no_stale_file(self) -> None:
        qa = write_qa_matrix(self.root / "AlignResult-1.qa.tsv")
        write_anchor_audit(self.root)
        qa_figure_outputs.write_run_qa_figures(qa, self.root)
        stale = self.root / "qa_figures" / "07_rt_correction_offsets.svg"
        self.assertTrue(stale.is_file())
        (self.root / qa_figure_outputs.ANCHORS_NAME).unlink()
        result = qa_figure_outputs.write_run_qa_figures(qa, self.root)
        self.assertFalse(stale.exists())
        self.assertEqual(["07_rt_correction_offsets.svg"], [item["file"] for item in result["skipped"]])

    def test_current_figures_are_reused_and_stale_ones_redrawn(self) -> None:
        qa = write_qa_matrix(self.root / "AlignResult-1.qa.tsv")
        result, files = qa_figure_outputs.current_run_figures(qa, self.root)
        self.assertEqual("written", result["status"])
        self.assertIn("qa_report.html", [path.name for path in files])
        result, _ = qa_figure_outputs.current_run_figures(qa, self.root)
        self.assertEqual("current", result["status"])
        write_qa_matrix(qa, spots=12)
        result, _ = qa_figure_outputs.current_run_figures(qa, self.root)
        self.assertEqual("written", result["status"])

    def test_figures_of_another_version_audit_or_with_a_file_missing_are_redrawn(self) -> None:
        qa = write_qa_matrix(self.root / "AlignResult-1.qa.tsv")
        write_anchor_audit(self.root)
        folder = self.root / "qa_figures"
        qa_figure_outputs.write_run_qa_figures(qa, self.root)
        self.assertTrue(qa_figure_outputs.figures_are_current(qa, self.root))
        manifest = folder / "figures.json"
        record = json.loads(manifest.read_text(encoding="utf-8"))
        manifest.write_text(json.dumps({**record, "msdial_interactive_version": "0.0.1"}), encoding="utf-8")
        self.assertFalse(qa_figure_outputs.figures_are_current(qa, self.root))
        result, _ = qa_figure_outputs.current_run_figures(qa, self.root)
        self.assertEqual("written", result["status"])  # drawn by an older version: drawn again
        self.assertEqual(qa_figure_outputs.__version__,
                         json.loads(manifest.read_text(encoding="utf-8"))["msdial_interactive_version"])
        anchors = self.root / qa_figure_outputs.ANCHORS_NAME
        anchors.write_text(anchors.read_text(encoding="utf-8").replace("\t0.8\t", "\t0.7\t"), encoding="utf-8")
        result, _ = qa_figure_outputs.current_run_figures(qa, self.root)
        self.assertEqual("written", result["status"])  # the anchor audit changed
        (folder / "05_pca_injections.svg").unlink()
        result, files = qa_figure_outputs.current_run_figures(qa, self.root)
        self.assertEqual("written", result["status"])  # a listed figure is missing
        self.assertIn("05_pca_injections.svg", [path.name for path in files])
        result, files = qa_figure_outputs.current_run_figures(qa, self.root)
        self.assertEqual("current", result["status"])
        self.assertEqual(sorted(path.name for path in files if path.suffix == ".svg"),
                         sorted(Path(item["file"]).name for item in result["figures"]))

    def test_an_unreadable_anchor_audit_loses_only_its_own_figure(self) -> None:
        qa = write_qa_matrix(self.root / "AlignResult-1.qa.tsv")
        write_anchor_audit(self.root)
        locked = PermissionError(13, "Permission denied", str(self.root / qa_figure_outputs.ANCHORS_NAME))
        with patch.object(qa_figure_outputs, "attach_rt_anchors", side_effect=locked):
            result = qa_figure_outputs.write_run_qa_figures(qa, self.root)
        self.assertEqual(5, len(result["figures"]))
        self.assertEqual(["07_rt_correction_offsets.svg"], [item["file"] for item in result["skipped"]])
        self.assertIn("could not be read (PermissionError: Permission denied)", result["skipped"][0]["reason"])
        self.assertTrue(any("07_rt_correction_offsets.svg" in warning for warning in result["warnings"]))
        self.assertNotIn(str(self.root), (self.root / "qa_figures" / "qa_report.html").read_text(encoding="utf-8"))

    def test_writers_of_one_folder_at_once_do_not_collide(self) -> None:
        qa = write_qa_matrix(self.root / "AlignResult-1.qa.tsv")
        write_anchor_audit(self.root)
        errors: list[BaseException] = []

        def draw(index: int) -> None:
            try:
                if index % 2:
                    qa_figure_outputs.write_run_qa_figures(qa, self.root)
                else:
                    qa_figure_outputs.current_run_figures(qa, self.root)
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=draw, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual([], errors)
        names = sorted(path.name for path in (self.root / "qa_figures").iterdir())
        self.assertEqual([], [name for name in names if name.endswith(".writing")])
        self.assertTrue(qa_figure_outputs.figures_are_current(qa, self.root))

    def test_a_link_to_a_folder_with_reserved_characters_is_encoded(self) -> None:
        page = self.root / "run #1 50%?" / "qa_figures" / "qa_report.html"
        href = qa_figure_outputs.relative_link(page, self.root / "quant" / "qa_figures")
        self.assertEqual("../../run%20%231%2050%25%3F/qa_figures/qa_report.html", href)
        html = qf.report_html("t", "l", [], [("Positive", href)])
        self.assertIn('href="../../run%20%231%2050%25%3F/qa_figures/qa_report.html"', html)

    def test_the_figures_fed_by_the_qa_read_equal_a_read_of_their_own(self) -> None:
        def cell(spot, stem, height):
            return "NaN" if (spot, stem) == (0, "S1") else ("" if (spot, stem) == (1, "S2") else f"{height:g}")

        qa = write_qa_matrix(self.root / "AlignResult-1.qa.tsv", cell=cell)
        reader = qa_figure_outputs.QaMatrixReader()
        report = build_lcms_qa_report_from_file(qa, spot_observer=reader.add_qa_spot)
        self.assertEqual(build_lcms_qa_report_from_file(qa), report)  # observing changes nothing
        self.assertEqual(qa_figure_outputs.read_qa_matrix(qa, "Positive"), reader.finish("Positive"))
        self.assertEqual(2, reader.non_numeric)

    def test_a_failing_figure_reader_changes_no_qa_and_the_figures_read_the_matrix_themselves(self) -> None:
        qa = write_qa_matrix(self.root / "AlignResult-1.qa.tsv")
        reader = qa_figure_outputs.QaMatrixReader()
        with patch.object(reader, "add_spot", side_effect=RuntimeError("broken reader")):
            report = build_lcms_qa_report_from_file(qa, spot_observer=reader.add_qa_spot)
        self.assertEqual(build_lcms_qa_report_from_file(qa), report)
        self.assertIsInstance(reader.error, RuntimeError)
        result = qa_figure_outputs.write_run_qa_figures(qa, self.root, reader=reader)
        self.assertEqual("written", result["status"])
        self.assertEqual(5, len(result["figures"]))  # 07 has no audit here


def _post(port: int, path: str, body: dict) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


class TheQaEndpoint(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.qa = write_qa_matrix(self.root / "run" / "AlignResult-1.qa.tsv")
        write_anchor_audit(self.root / "run")
        self.jobs = {"job-1": {
            "id": "job-1", "kind": "run", "status": "completed", "logs": [], "exit_code": 0,
            "preparation": {"run_directory": str(self.root / "run"), "analysis_type": "lcms"},
            "artifacts": {"qa": [str(self.qa)], "mztab": [], "msdial": []},
        }}
        for item in (patch.object(server, "JOBS", self.jobs), patch.object(server, "_persist_jobs_locked", lambda: None)):
            item.start()
            self.addCleanup(item.stop)
        self.backend = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.backend.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.backend.shutdown()
        self.backend.server_close()
        self.thread.join(timeout=5)
        self.directory.cleanup()

    def test_a_jobs_qa_carries_its_figures_by_path(self) -> None:
        response = _post(self.backend.server_port, "/api/qa/report", {"job_id": "job-1"})
        figures = response["qa_figures"]
        self.assertEqual("written", figures["status"])
        self.assertEqual(str((self.root / "run" / "qa_figures").resolve()), figures["folder"])
        self.assertTrue(Path(figures["html"]).is_file())
        self.assertEqual(6, len(figures["figures"]))
        for item in figures["figures"]:
            self.assertTrue(Path(item["file"]).is_file())
            self.assertTrue(item["title"])
        self.assertNotIn("<svg", json.dumps(response))
        expected = qa_report_for_run(build_lcms_qa_report_from_file(self.qa), str(self.root / "run"))
        self.assertEqual(json.loads(json.dumps(expected)), response["report"])

    def test_a_jobs_qa_reads_its_matrix_once(self) -> None:
        parsed = []
        original = server.build_lcms_qa_report_from_file

        def counted(*arguments, **options):
            parsed.append(arguments[0])
            return original(*arguments, **options)

        second_read = AssertionError("the QA figures read the matrix a second time")
        with patch.object(qa_figure_outputs, "read_qa_matrix", side_effect=second_read), \
                patch.object(server, "build_lcms_qa_report_from_file", side_effect=counted):
            response = _post(self.backend.server_port, "/api/qa/report", {"job_id": "job-1"})
        self.assertEqual([str(self.qa)], [str(item) for item in parsed])
        self.assertEqual("written", response["qa_figures"]["status"])
        self.assertEqual(6, len(response["qa_figures"]["figures"]))

    def test_a_publication_reuses_current_figures_and_feeds_stale_ones_from_its_qa_read(self) -> None:
        _post(self.backend.server_port, "/api/qa/report", {"job_id": "job-1"})
        workflow = {"project_type": "lcms", "files": []}
        second_read = AssertionError("the QA figures read the matrix a second time")
        with patch.object(qa_figure_outputs, "read_qa_matrix", side_effect=second_read):
            current = _post(self.backend.server_port, "/api/publication/report",
                            {"job_id": "job-1", "workflow": workflow, "use_saved_run": False})
            self.assertEqual("current", current["qa_figures"]["status"])
            manifest = self.root / "run" / "qa_figures" / "figures.json"
            record = json.loads(manifest.read_text(encoding="utf-8"))
            manifest.write_text(json.dumps({**record, "msdial_interactive_version": "0.0.1"}), encoding="utf-8")
            redrawn = _post(self.backend.server_port, "/api/publication/report",
                            {"job_id": "job-1", "workflow": workflow, "use_saved_run": False})
        self.assertEqual("written", redrawn["qa_figures"]["status"])
        with zipfile.ZipFile(redrawn["report"]["bundle"]) as archive:
            self.assertIn("qa_figures/05_pca_injections.svg", archive.namelist())

    def test_a_figure_failure_leaves_the_qa_as_it_was(self) -> None:
        normal = _post(self.backend.server_port, "/api/qa/report", {"job_id": "job-1"})
        with patch.object(server, "write_run_qa_figures", side_effect=RuntimeError("no figures today")):
            failed = _post(self.backend.server_port, "/api/qa/report", {"job_id": "job-1"})
        self.assertEqual(normal["report"], failed["report"])
        self.assertEqual("failed", failed["qa_figures"]["status"])
        self.assertIn("no figures today", failed["qa_figures"]["warnings"][0])

    def test_a_matrix_named_by_path_alone_gets_no_figures(self) -> None:
        loose = write_qa_matrix(self.root / "loose" / "AlignResult-9.qa.tsv")
        response = _post(self.backend.server_port, "/api/qa/report", {"file_path": str(loose)})
        self.assertEqual("not_written", response["qa_figures"]["status"])
        self.assertFalse((self.root / "loose" / "qa_figures").exists())

    def test_the_guided_completion_passes_the_figures_on(self) -> None:
        port = self.backend.server_port

        def request(method, path, **options):
            if path.startswith("/api/jobs/"):
                return {**self.jobs["job-1"], "status": "completed"}
            if path == "/api/qa/report":
                return _post(port, path, options.get("body") or {})
            return {}

        with patch.object(mcp_server, "_request_json", side_effect=request):
            result = mcp_server.msdial_complete_guided_analysis("job-1", generate_materials_methods=False)
        self.assertEqual("written", result["qa_figures"]["status"])
        self.assertIn("summary", result["quality_assurance"])


class ThePublicationBundle(unittest.TestCase):
    def test_the_bundle_carries_the_figures_and_leaves_out_one_that_names_a_private_library(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            qa = write_qa_matrix(root / "AlignResult-1.qa.tsv")
            _, files = qa_figure_outputs.current_run_figures(qa, root)
            leaky = root / "qa_figures" / "leaky.svg"
            leaky.write_text("<svg xmlns='http://www.w3.org/2000/svg'><text>D:/private/inhouse.msp</text></svg>",
                             encoding="utf-8")
            workflow = {"project_type": "lcms", "files": [], "msp_annotators": [{"msp_file_path": "D:/private/inhouse.msp"}]}
            result = generate_publication_report(
                workflow, build_lcms_qa_report_from_file(qa), root, app_version="test", console_version="",
                supplementary_files=[(path, f"qa_figures/{path.name}") for path in [*files, leaky]],
            )
            with zipfile.ZipFile(result["bundle"]) as archive:
                names = set(archive.namelist())
            self.assertIn("qa_figures/qa_report.html", names)
            self.assertIn("qa_figures/01_detected_features_by_injection.svg", names)
            self.assertNotIn("qa_figures/leaky.svg", names)
            self.assertIn("qa_figures/leaky.svg", result["bundle_warnings"][0])
            self.assertEqual(len(files), len(result["bundle_supplementary_files"]))

    def test_without_figures_the_report_is_as_it_was(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = generate_publication_report({"project_type": "lcms", "files": []}, None, temporary,
                                                 app_version="test", console_version="")
            self.assertNotIn("bundle_supplementary_files", result)
            audit = json.loads(Path(result["audit_file"]).read_text(encoding="utf-8"))
            self.assertNotIn("bundle_supplementary_files", audit)


class TheLaboratorySteps(unittest.TestCase):
    """Figure 04 with a confirmed normalize, the batch's page with a confirmed merge; previews write nothing."""

    def setUp(self) -> None:
        from lab_batch_fixture import Batch, capabilities_patches, write_run

        self.directory = tempfile.TemporaryDirectory()
        self.batch = Batch(Path(self.directory.name))
        self.patches = capabilities_patches()
        self.patches.__enter__()
        self.contract = self.batch.contract()
        self.runs = {polarity: write_run(self.batch, self.contract, polarity) for polarity in ("Positive", "Negative")}
        self.jobs: dict = {run["id"]: run for run in self.runs.values()}
        for item in (patch.object(server, "JOBS", self.jobs), patch.object(server, "PROCESSES", {}),
                     patch.object(server, "_persist_jobs_locked", lambda: None)):
            item.start()
            self.addCleanup(item.stop)
        self.backend = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.backend.serve_forever, daemon=True)
        self.thread.start()
        self.figures = self.batch.analysis / "quant" / "qa_figures"

    def tearDown(self) -> None:
        self.backend.shutdown()
        self.backend.server_close()
        self.thread.join(timeout=5)
        self.patches.__exit__(None, None, None)
        self.directory.cleanup()

    def normalize(self, polarity: str, accepted: list[str]) -> None:
        from lab_batch_fixture import normalize_outputs

        from msdial_app.lab_lipidomics_agent import (
            finish_normalize,
            plan_normalize,
            public_normalize_plan,
            resolve_standards_for_contract,
            write_normalize_inputs,
        )

        resolved = resolve_standards_for_contract(self.contract, {polarity: self.runs[polarity]})
        plan = plan_normalize(self.contract, self.runs[polarity], resolved["polarities"][polarity]["resolution_digest"],
                              accept_searched=accepted)
        write_normalize_inputs(plan)
        _, _, report = normalize_outputs(public_normalize_plan(plan), polarity)
        self.assertTrue(finish_normalize(plan, 0, report.splitlines())["completed"])

    def test_a_confirmed_normalize_draws_figure_04_and_its_preview_nothing(self) -> None:
        resolved = _post(self.backend.server_port, "/api/lipidomics/resolve",
                         {"contract_path": self.contract, "neg_job_id": "run-neg"})
        self.assertFalse(self.figures.exists())  # resolving stays read-only
        body = {"contract_path": self.contract, "job_id": "run-neg",
                "resolution_digest": resolved["polarities"]["Negative"]["resolution_digest"]}
        preview = _post(self.backend.server_port, "/api/lipidomics/normalize", body)
        self.assertFalse(preview["started"])
        self.assertNotIn("qa_figures", preview)
        self.assertFalse(self.figures.exists())
        with patch.object(server, "_run_normalize_job", lambda *arguments: None):
            started = _post(self.backend.server_port, "/api/lipidomics/normalize", {**body, "confirmed": True})
        self.assertTrue(started["started"])
        target = self.figures / "04_internal_standards_negative.svg"
        listed = started["qa_figures"]["figures"]
        self.assertEqual(["Internal-standard response across samples (Negative)"], [item["title"] for item in listed])
        self.assertEqual(target.resolve(), Path(listed[0]["file"]).resolve())
        element = parse(target.read_text(encoding="utf-8"))
        texts = [node.text for node in element.iter("{http://www.w3.org/2000/svg}text")]
        # The negative run's standards normalize uses: PC, LPC, Cer and FA d3 resolve; CE stays unresolved.
        self.assertIn("PC 15:0_18:1(d7)", texts)
        self.assertNotIn("CE 18:1(d7)", texts)

    def test_a_confirmed_merge_writes_the_batch_page_and_its_preview_nothing(self) -> None:
        self.normalize("Positive", ["Cer 18:1;O2/15:0(d7)"])
        self.normalize("Negative", [])
        preview = _post(self.backend.server_port, "/api/lipidomics/merge", {"contract_path": self.contract})
        self.assertFalse(preview["written"])
        self.assertNotIn("qa_figures", preview)
        self.assertFalse(self.figures.exists())
        written = _post(self.backend.server_port, "/api/lipidomics/merge", {"contract_path": self.contract, "confirmed": True})
        figures = written["qa_figures"]
        self.assertEqual((self.figures / "qa_report.html").resolve(), Path(figures["html"]).resolve())
        drawn = {Path(item["file"]).name for item in figures["figures"]}
        self.assertTrue({"04_internal_standards_positive.svg", "04_internal_standards_negative.svg",
                         "08_lipid_class_totals.svg"} <= drawn)
        # The fixture's runs wrote no QA matrix: their figures are skipped, and the page says why.
        self.assertTrue(any("QA matrix" in warning for warning in figures["warnings"]))
        self.assertNotIn("<svg", json.dumps(figures))

        # With the positive run's matrix beside its candidate table, its figures are drawn alone.
        from lab_batch_fixture import ALIGNMENT

        positive = self.batch.analysis / "pos"
        stems = [("S1_Pos", "SPF", "Sample", 1), ("S2_Pos", "GF", "Sample", 2), ("Blank_Pos", "Blank", "Blank", 3)]
        with (positive / f"{ALIGNMENT}.qa.tsv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
            writer.writerow(QA_COLUMNS)
            for spot in range(10):
                for index, (name, klass, kind, order) in enumerate(stems):
                    writer.writerow([spot, name, klass, kind, order, 1, 0 if kind == "Blank" and spot % 2 else 1000 + 100 * spot + index,
                                     5, 400, 10, "TRUE", "TRUE"])
        again = _post(self.backend.server_port, "/api/lipidomics/merge", {"contract_path": self.contract, "confirmed": True})
        drawn = {Path(item["file"]).name for item in again["qa_figures"]["figures"]}
        self.assertIn("01_detected_features_by_injection.svg", drawn)
        page = (self.figures / "qa_report.html").read_text(encoding="utf-8")
        self.assertIn("PCA needs at least three study injections; there are 2", page)
        self.assertIn("The negative run&#x27;s QA matrix", page)

    def test_an_unreadable_input_of_the_batch_page_loses_only_its_own_figures(self) -> None:
        self.normalize("Positive", ["Cer 18:1;O2/15:0(d7)"])
        self.normalize("Negative", [])
        empty = self.batch.analysis / "empty_classsummary.csv"
        empty.write_text("Metabolite,Class\n", encoding="utf-8")
        result = qa_figure_outputs.write_lipidomics_qa_figures(
            self.contract, {"outputs": {"class_summary": {"path": str(empty)}}})
        drawn = {Path(item["file"]).name for item in result["figures"]}
        self.assertEqual({"04_internal_standards_positive.svg", "04_internal_standards_negative.svg"}, drawn)
        reasons = {item["file"]: item["reason"] for item in result["skipped"]}
        self.assertIn("the class summary empty_classsummary.csv could not be read (ValueError", reasons["08_lipid_class_totals.svg"])
        self.assertTrue(any("08_lipid_class_totals.svg" in warning for warning in result["warnings"]))
        self.assertTrue((self.figures / "qa_report.html").is_file())
        # A standards table that cannot be read leaves out that polarity's figure 04 alone.
        original = qa_figure_outputs.read_height_export

        def torn(path):
            if "pos" in Path(path).name.casefold():
                raise ValueError("torn file")
            return original(path)

        with patch.object(qa_figure_outputs, "read_height_export", side_effect=torn):
            again = qa_figure_outputs.write_lipidomics_qa_figures(
                self.contract, {"outputs": {"class_summary": {"path": str(empty)}}})
        drawn = {Path(item["file"]).name for item in again["figures"]}
        self.assertEqual({"04_internal_standards_negative.svg"}, drawn)
        reasons = {item["file"]: item["reason"] for item in again["skipped"]}
        self.assertIn("the positive standards table or Height matrix could not be read (ValueError: torn file)",
                      reasons["04_internal_standards_positive.svg"])


if __name__ == "__main__":
    unittest.main()
