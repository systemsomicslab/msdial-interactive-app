"""A run-order criterion is not assessed against the file listing (decided 2026-09-29).

The run-order/intensity correlation is computed against whatever analytical_order the analysis CSV
carries. When the raw headers do not give the order, a repository unit's CSV carries the order its
sample table declares, a number read out of the file names, or the file listing, and the unit
manifest said only that the headers were not used. It now records which, and the publication report
withholds the correlation where the order is the file listing and the CSV still carries it.
"""
from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from msdial_app import mcp_server
from msdial_app.materials_methods import generate_publication_report
from msdial_app.quality_assurance import (
    NOT_ASSESSED_REASON_PHRASES,
    UNRECORDED_ORDER_REASON,
    with_recorded_order,
)
from msdial_app.repository_reanalysis import (
    ACQUISITION_ORDER_SOURCE,
    DECLARED_ORDER_SOURCE,
    EMBEDDED_ORDER_SOURCE,
    LISTING_ORDER_SOURCE,
    recorded_order_source,
)
from msdial_app.sample_grouping import propose_injection_order

from test_analytical_order import _manifest

METRIC = "run_order_intensity_correlation"


def _prepare(root: Path, names: list[str], *, times: dict[str, str] | None = None,
             declared: dict[str, int] | None = None, recognised_orders: list[int] | None = None):
    """Prepare a repository unit as msdial_prepare_repository_reanalysis does; return its CSV rows,
    the unit manifest's order record, the manifest path and the preview's record."""
    raw = root / "raw"
    raw.mkdir()
    for name in names:
        (raw / name).write_text("raw", encoding="ascii")
    body = _manifest(raw, times) if times is not None else {}
    body.update({
        "project": {
            "repository": "metabolights",
            "accession": "MTBLS0",
            "sample_metadata": [
                {
                    "sample_id": Path(name).stem,
                    "raw_file": name,
                    "values": {"Cell line": "A" if index % 2 else "B",
                               **({"Injection order": str(declared[name])} if declared and name in declared else {})},
                }
                for index, name in enumerate(names)
            ],
        },
        "analysis_input_path": str(raw),
        "output_directory": str(root / "output"),
        "input_candidates": [str(raw / name) for name in names],
    })
    manifest_path = root / "run-manifest.json"
    manifest_path.write_text(json.dumps(body), encoding="utf-8")
    # The order recognition gives: propose_injection_order over the stems, in listing order.
    proposal = propose_injection_order([Path(name).stem for name in names])
    orders = recognised_orders or [proposal["orders"][Path(name).stem] for name in names]
    job = {
        "id": "download-job",
        "kind": "repository_download",
        "status": "completed",
        "raw_retention_policy": "keep",
        "result": {
            "manifest_path": str(manifest_path),
            "recognized": {"files": [
                {"file_path": str(raw / name), "file_name": Path(name).stem, "file_type": "Sample",
                 "class_id": "Sample", "acquisition_type": "DDA", "batch_order": 1,
                 "analytical_order": order, "factor": 1}
                for name, order in zip(names, orders)
            ]},
        },
    }
    with patch.object(mcp_server, "_request_json", return_value=job):
        preview = mcp_server.msdial_prepare_repository_reanalysis("download-job", hierarchy=["Cell line"], confirmed=False)
        prepared = mcp_server.msdial_prepare_repository_reanalysis("download-job", hierarchy=["Cell line"], confirmed=True)
    with open(prepared["input_path"], encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    record = json.loads(manifest_path.read_text(encoding="utf-8"))["analytical_order"]
    return rows, record, manifest_path, preview["preview"]["analytical_order"]


def _files(rows: list[dict]) -> list[dict]:
    return [{"file_name": row["file_name"], "file_path": row["file_path"], "analytical_order": int(row["analytical_order"])}
            for row in rows]


class RecordedSourceTests(unittest.TestCase):
    """The preparation records where the analysis CSV's order came from."""

    def test_each_source_is_recorded(self) -> None:
        cases = {
            ACQUISITION_ORDER_SOURCE: dict(
                names=["b.mzML", "a.mzML", "c.mzML"],
                times={"b.mzML": "2020-02-01T00:00:00+00:00", "a.mzML": "2020-01-01T00:00:00+00:00",
                       "c.mzML": "2020-03-01T00:00:00+00:00"}),
            DECLARED_ORDER_SOURCE: dict(names=["a.mzML", "b.mzML", "c.mzML"],
                                        declared={"a.mzML": 3, "b.mzML": 1, "c.mzML": 2}),
            EMBEDDED_ORDER_SOURCE: dict(names=["run_03.mzML", "run_01.mzML", "run_02.mzML"]),
            LISTING_ORDER_SOURCE: dict(names=["alpha.mzML", "beta.mzML", "gamma.mzML"]),
        }
        for source, options in cases.items():
            with self.subTest(source), tempfile.TemporaryDirectory() as temporary:
                rows, record, manifest_path, previewed = _prepare(Path(temporary), **options)
                self.assertEqual(source, record["order_source"])
                self.assertEqual(source, previewed["order_source"])
                self.assertEqual(source, recorded_order_source(str(manifest_path), _files(rows)))
                self.assertNotIn("declared_files", record)

    def test_a_partly_declared_order_names_what_filled_the_rest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, record, _, _ = _prepare(Path(temporary), ["alpha.mzML", "beta.mzML", "gamma.mzML"],
                                       declared={"alpha.mzML": 2, "beta.mzML": 1})

        self.assertEqual(LISTING_ORDER_SOURCE, record["order_source"])
        self.assertEqual(2, record["declared_files"])

    def test_an_order_recognition_did_not_give_is_not_named(self) -> None:
        # The recognised orders are not what the names give, so what decided them cannot be told.
        with tempfile.TemporaryDirectory() as temporary:
            _, record, _, _ = _prepare(Path(temporary), ["alpha.mzML", "beta.mzML", "gamma.mzML"],
                                       recognised_orders=[3, 1, 2])

        self.assertIsNone(record["order_source"])

    def test_a_csv_that_no_longer_carries_the_order_has_no_recorded_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            rows, _, manifest_path, _ = _prepare(Path(temporary), ["alpha.mzML", "beta.mzML", "gamma.mzML"])
            files = _files(rows)
            files[0]["analytical_order"], files[1]["analytical_order"] = files[1]["analytical_order"], files[0]["analytical_order"]
            edited = recorded_order_source(str(manifest_path), files)
            other = recorded_order_source(str(manifest_path), [dict(item, file_path=f"D:/elsewhere/{item['file_name']}.mzML")
                                                               for item in _files(rows)])

        self.assertIsNone(edited)
        self.assertIsNone(other)

    def test_a_record_from_before_the_source_was_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = [{"file_name": "a", "file_path": str(root / "a.mzML"), "analytical_order": 1},
                     {"file_name": "b", "file_path": str(root / "b.mzML"), "analytical_order": 2}]
            recorded_files = [{"file": "a.mzML", "analytical_order": 1}, {"file": "b.mzML", "analytical_order": 2}]
            header = root / "header.json"
            header.write_text(json.dumps({"input_candidates": [item["file_path"] for item in files], "analytical_order": {
                "derived_from": ACQUISITION_ORDER_SOURCE, "files": recorded_files}}), encoding="utf-8")
            unknown = root / "unknown.json"
            unknown.write_text(json.dumps({"input_candidates": [item["file_path"] for item in files], "analytical_order": {
                "derived_from": None, "reason": "no preflight", "files": recorded_files}}), encoding="utf-8")

            self.assertEqual(ACQUISITION_ORDER_SOURCE, recorded_order_source(str(header), files))
            self.assertIsNone(recorded_order_source(str(unknown), files))
            self.assertIsNone(recorded_order_source("", files))
            self.assertIsNone(recorded_order_source(str(root / "missing.json"), files))


def _qa(value: float | None = 0.07, reasons: dict | None = None, injections: int = 6) -> dict:
    summary = {
        "sample_count": injections,
        "alignment_spot_count": 500,
        "category_counts": {"Sample": injections, "QC": 0, "Blank": 0},
        METRIC: value,
    }
    if reasons is not None:
        summary["not_assessed_reasons"] = reasons
    return {"summary": summary, "internal_standards": []}


def _publish(root: Path, source: str | None, qa: dict, *, carry: bool = True) -> dict:
    names = ["alpha", "beta", "gamma"]
    files = [{"file_name": name, "file_path": str(root / f"{name}.mzML"), "file_type": "Sample", "class_id": "All",
              "analytical_order": index + 1} for index, name in enumerate(names)]
    manifest = root / "run-manifest.json"
    manifest.write_text(json.dumps({
        "input_candidates": [item["file_path"] for item in files],
        "analytical_order": {
            "derived_from": ACQUISITION_ORDER_SOURCE if source == ACQUISITION_ORDER_SOURCE else None,
            "order_source": source,
            "files": [{"file": f"{item['file_name']}.mzML",
                       "analytical_order": item["analytical_order"] if carry else 9} for item in files],
        },
    }), encoding="utf-8")
    workflow = {"project_type": "lcms", "ion_mode": "Negative", "files": files,
                "repository_run_manifest": str(manifest)}
    output = root / "output"
    return generate_publication_report(workflow, qa, output, app_version="0.5.4", console_version="5.5.260929")


def _check(result: dict) -> dict:
    return next(item for item in result["qa_assessment"]["checks"] if item["metric"] == METRIC)


class PublicationTests(unittest.TestCase):
    """The publication report withholds a run-order criterion computed against the file listing."""

    def test_the_listing_is_no_injection_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = _publish(Path(temporary), LISTING_ORDER_SOURCE, _qa())
            with open(result["supplementary_table"], encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle, delimiter="\t"))
            audit = json.loads(Path(result["audit_file"]).read_text(encoding="utf-8"))

        check = _check(result)
        self.assertEqual("not_assessed", check["status"])
        self.assertIsNone(check["value"])
        self.assertEqual(UNRECORDED_ORDER_REASON, check["reason"])
        self.assertEqual(0, result["qa_assessment"]["evaluated"])
        self.assertIn("because the injection order was not recorded for every file", result["methods_text"])
        self.assertIn("because the injection order was not recorded for every file", result["qa_results_text"])
        self.assertNotIn("0.07", result["qa_results_text"])
        self.assertIn(UNRECORDED_ORDER_REASON, {row["Value"] for row in rows if row["Parameter"] == "Not assessed because"})
        observed = [row for row in rows if row["Record"] == "Observed metric" and row["Parameter"] == METRIC]
        self.assertEqual(["not recorded"], [row["Value"] for row in observed])
        self.assertEqual(LISTING_ORDER_SOURCE, audit["analytical_order_source"])
        self.assertEqual(UNRECORDED_ORDER_REASON, audit["qa_report"]["summary"]["not_assessed_reasons"][METRIC])

    def test_a_recorded_or_read_order_is_assessed(self) -> None:
        for source in (ACQUISITION_ORDER_SOURCE, DECLARED_ORDER_SOURCE, EMBEDDED_ORDER_SOURCE, None):
            with self.subTest(source), tempfile.TemporaryDirectory() as temporary:
                result = _publish(Path(temporary), source, _qa())
                self.assertEqual("pass", _check(result)["status"])
                self.assertEqual(0.07, _check(result)["value"])

    def test_a_listing_the_csv_no_longer_carries_is_not_known_to_be_the_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = _publish(Path(temporary), LISTING_ORDER_SOURCE, _qa(), carry=False)

        self.assertEqual("pass", _check(result)["status"])

    def test_a_criterion_already_withheld_keeps_its_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = _publish(Path(temporary), LISTING_ORDER_SOURCE, _qa(None, injections=2))

        self.assertEqual("not_assessed", _check(result)["status"])
        self.assertEqual("the run had 2 injection(s), and at least three are needed", _check(result)["reason"])

    def test_the_reason_is_one_of_the_fixed_phrases_and_the_summary_is_not_changed(self) -> None:
        summary = _qa()["summary"]
        withheld = with_recorded_order(summary, LISTING_ORDER_SOURCE)

        self.assertIn(UNRECORDED_ORDER_REASON, NOT_ASSESSED_REASON_PHRASES)
        self.assertEqual(0.07, summary[METRIC])
        self.assertNotIn("not_assessed_reasons", summary)
        self.assertIsNone(withheld[METRIC])
        self.assertIs(summary, with_recorded_order(summary, EMBEDDED_ORDER_SOURCE))


if __name__ == "__main__":
    unittest.main()
