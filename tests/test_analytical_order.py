"""The analytical order comes from the raw headers when every file records when it was acquired.

The analysis CSV carried the file listing, or a number read out of the names, even though every
raw header recorded its acquisition start time. For MTBLS2207 the listing put a file acquired in
December 2019 last and one acquired in September 2020 second, and the only QA criterion the run
could evaluate was a run-order drift computed against that listing.
"""
from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from msdial_app import mcp_server
from msdial_app.agent_workflow import _adopt_recorded_analytical_order
from msdial_app.repository_reanalysis import (
    ACQUISITION_ORDER_SOURCE,
    _summarize_raw_metadata,
    acquisition_start_order,
)


def _record(path: Path, start: str | None) -> dict:
    return {
        "source": {"filePath": str(path), "fileName": path.name},
        "acquisition": {"method": {"value": "DDA"}, "polarity": {"value": "Negative"}},
        "run": {"acquisitionStartTime": {"value": start, "source": "SpectrumHeader"}},
    }


def _manifest(root: Path, times: dict[str, str | None], in_summary: bool = True) -> dict:
    records = [_record(root / name, start) for name, start in times.items()]
    output = root / "raw-metadata-preflight.json"
    output.write_text(json.dumps(records), encoding="utf-8")
    summary = _summarize_raw_metadata(records)
    if not in_summary:
        for item in summary["per_file"]:
            item.pop("acquisition_start_time", None)
    return {"raw_metadata_preflight": {"summary": summary, "output": str(output)}}


class AcquisitionStartOrderTests(unittest.TestCase):
    def test_the_preflight_summary_carries_the_start_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            summary = _summarize_raw_metadata([_record(root / "a.mzML", "2020-09-16T05:51:20+00:00")])

        self.assertEqual("2020-09-16T05:51:20+00:00", summary["per_file"][0]["acquisition_start_time"])

    def test_files_are_ranked_by_header_time_not_by_listing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            times = {
                "IROA.mzML": "2020-01-17T08:04:21+00:00",
                "Ecoli.mzML": "2020-09-17T00:50:21+00:00",
                "Plasma.mzML": "2020-09-16T07:00:44+00:00",
                "NIST.mzML": "2019-12-18T21:49:13Z",
            }
            manifest = _manifest(root, times)
            order = acquisition_start_order(manifest, [str(root / name) for name in times])

        self.assertEqual(ACQUISITION_ORDER_SOURCE, order["derived_from"])
        self.assertEqual(
            ["NIST.mzML", "IROA.mzML", "Plasma.mzML", "Ecoli.mzML"],
            [item["file"] for item in order["files"]],
        )
        self.assertEqual([1, 2, 3, 4], [item["analytical_order"] for item in order["files"]])
        self.assertFalse(order["agrees_with_listing"])

    def test_an_older_summary_falls_back_to_the_extractor_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            times = {"b.mzML": "2020-02-01T00:00:00+00:00", "a.mzML": "2020-01-01T00:00:00+00:00"}
            manifest = _manifest(root, times, in_summary=False)
            order = acquisition_start_order(manifest, [str(root / name) for name in times])

        self.assertEqual(["a.mzML", "b.mzML"], [item["file"] for item in order["files"]])

    def test_one_file_without_a_time_leaves_the_order_alone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            times = {"a.mzML": "2020-01-01T00:00:00+00:00", "b.mzML": None}
            manifest = _manifest(root, times)
            order = acquisition_start_order(manifest, [str(root / name) for name in times])

        self.assertIsNone(order["derived_from"])
        self.assertEqual(["b.mzML"], order["missing"])
        self.assertNotIn("orders", order)

    def test_equal_times_keep_listing_order_and_are_named(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            times = {"b.mzML": "2020-01-01T00:00:00+00:00", "a.mzML": "2020-01-01T00:00:00+00:00"}
            manifest = _manifest(root, times)
            order = acquisition_start_order(manifest, [str(root / name) for name in times])

        self.assertEqual(["b.mzML", "a.mzML"], [item["file"] for item in order["files"]])
        self.assertEqual(["a.mzML", "b.mzML"], order["tied"])

    def test_mixed_timezone_awareness_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            times = {"a.mzML": "2020-01-01T00:00:00+00:00", "b.mzML": "2020-01-02T00:00:00"}
            manifest = _manifest(root, times)
            order = acquisition_start_order(manifest, [str(root / name) for name in times])

        self.assertIsNone(order["derived_from"])


class PreparedAnalysisCsvTests(unittest.TestCase):
    def test_the_prepared_csv_carries_the_header_order_and_the_manifest_says_so(self) -> None:
        # The repository sample table declares an injection order too; the measured one wins.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "raw").mkdir()
            output = root / "output"
            names = ["sample_a.mzML", "sample_b.mzML", "sample_c.mzML"]
            for name in names:
                (root / "raw" / name).write_text("raw", encoding="ascii")
            times = {
                "sample_a.mzML": "2020-03-01T00:00:00+00:00",
                "sample_b.mzML": "2020-01-01T00:00:00+00:00",
                "sample_c.mzML": "2020-02-01T00:00:00+00:00",
            }
            manifest_body = _manifest(root / "raw", times)
            manifest_body.update(
                {
                    "project": {
                        "repository": "metabolights",
                        "accession": "MTBLS0",
                        "sample_metadata": [
                            {
                                "sample_id": name.split(".")[0],
                                "raw_file": name,
                                "values": {"Cell line": "A" if index < 2 else "B", "Injection order": str(index + 1)},
                            }
                            for index, name in enumerate(names)
                        ],
                    },
                    "analysis_input_path": str(root / "raw"),
                    "output_directory": str(output),
                }
            )
            manifest_path = root / "run-manifest.json"
            manifest_path.write_text(json.dumps(manifest_body), encoding="utf-8")
            job = {
                "id": "download-job",
                "kind": "repository_download",
                "status": "completed",
                "raw_retention_policy": "keep",
                "result": {
                    "manifest_path": str(manifest_path),
                    "recognized": {
                        "files": [
                            {
                                "file_path": str(root / "raw" / name),
                                "file_name": name.split(".")[0],
                                "file_type": "Sample",
                                "class_id": "Sample",
                                "acquisition_type": "DDA",
                                "batch_order": 1,
                                "analytical_order": index + 1,
                                "factor": 1,
                            }
                            for index, name in enumerate(names)
                        ]
                    },
                },
            }

            with patch.object(mcp_server, "_request_json", return_value=job):
                preview = mcp_server.msdial_prepare_repository_reanalysis(
                    "download-job", hierarchy=["Cell line"], confirmed=False
                )
                prepared = mcp_server.msdial_prepare_repository_reanalysis(
                    "download-job", hierarchy=["Cell line"], confirmed=True
                )
            with open(prepared["input_path"], encoding="utf-8-sig", newline="") as handle:
                rows = {row["file_name"]: row["analytical_order"] for row in csv.DictReader(handle)}
            recorded = json.loads(manifest_path.read_text(encoding="utf-8"))["analytical_order"]

            state = {
                "repository_run_manifest": str(manifest_path),
                "files": [{"file_name": name, "analytical_order": int(order)} for name, order in rows.items()],
                "sample_table_proposal": {"analytical_order": {"derived_from": "listing"}},
            }
            _adopt_recorded_analytical_order(state)

        self.assertEqual(ACQUISITION_ORDER_SOURCE, preview["preview"]["analytical_order"]["derived_from"])
        self.assertEqual({"sample_b": "1", "sample_c": "2", "sample_a": "3"}, rows)
        self.assertEqual(ACQUISITION_ORDER_SOURCE, recorded["derived_from"])
        self.assertNotIn("orders", recorded)
        proposal = state["sample_table_proposal"]["analytical_order"]
        self.assertEqual(ACQUISITION_ORDER_SOURCE, proposal["derived_from"])
        self.assertTrue(proposal["matches_analysis_csv"])


if __name__ == "__main__":
    unittest.main()
