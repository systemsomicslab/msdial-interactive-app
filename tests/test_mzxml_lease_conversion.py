"""A unit whose samples exist only as mzXML is converted in its download lease and run, not excluded.

MS-DIAL has no mzXML reader, so one mzXML anywhere in a unit used to exclude the whole unit before a byte
was fetched: MetaboLights MTBLS417 (four units, 60 files each), MTBLS1572 and MTBLS1842, and the 35
Workbench units whose study archives hold mzXML, about 280 GB of the declared pool. The user decided on
2026-09-30 that such data are converted with Interactive's own converter and run, with every inference flag
off; that a file which fails its conversion is excluded with a reason while the rest run; and that mzData,
which nothing converts, is still excluded. The lease's convert stage (a placeholder since 0.5.16) does it,
after extract and before discover, into raw\\converted.

The fixtures are the converter's own synthetic mzXML (test_mzxml_conversion), served by a stand-in for the
network (test_download_lease_record._Client). The raw-metadata extractor is a stand-in too. The gate tests
run the reanalysis gate's own script, where it is on this machine, on a workspace these leases wrote.
"""

from __future__ import annotations

import errno
import hashlib
import io
import json
import lzma
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from msdial_app import mcp_server, mzxml_conversion
from msdial_app.mzxml_conversion import ConversionOptions
from msdial_app.repository_analysis_rows import build_repository_analysis_rows, write_analysis_csv
from msdial_app.repository_reanalysis import (
    CONVERSION_FAILED,
    CONVERSION_PLAN_SCHEMA,
    INPUT_CONVERSIONS_SCHEMA,
    LEASE_STAGES,
    NO_CONVERTED_INPUT_REASON,
    UNCONVERTIBLE_INPUT_REASON,
    RepositoryFile,
    RepositoryProject,
    _file_key,
    _sample_locus,
    create_download_lease,
    declared_analysis_inputs,
    lineage_stands_for,
    project_from_dict,
    read_manifest,
    run_raw_metadata_preflight,
    split_unit_by_acquisition,
)

from test_download_lease_record import _Client
from test_mzml_encoding import _numpress_mzml, dda_spectra, mzml
from test_mzxml_conversion import dda_32, swath_32
from test_raw_metadata_preflight import _APPROVAL, _Extractor, _PinnedExtractor

GATE = Path(
    os.environ.get("MSDIAL_REANALYSIS_GATE")
    or "D:\\13_MSDIAL_Public_Reanalysis\\code\\scripts\\verify-run-invariants.py"
)
OFF = asdict(ConversionOptions())


class _Cancelled(Exception):
    """What a cancelled job's progress callback raises (the backend's RepositoryDownloadCancelled)."""


def _truncated() -> bytes:
    """An mzXML cut off mid-scan, as a download that lost its tail would leave it."""
    return dda_32()[:-300]


def _project(
    payloads: dict[str, bytes],
    *,
    declared: bool = False,
    checksums: bool = True,
    acquisition: str = "DDA",
    unit: str = "mtbls417-pos",
) -> RepositoryProject:
    """An MTBLS417-shaped unit: every sample one FILES/<name>.mzXML, listed for analysis, as the Catalog lists it."""
    files = [
        RepositoryFile(
            f"FILES/{name}", len(data), f"https://example.org/{name}", role="requires_conversion",
            checksum=hashlib.md5(data).hexdigest() if checksums else "",
        )
        for name, data in payloads.items()
    ]
    project = RepositoryProject(
        repository="metabolights",
        accession="MTBLS417",
        analysis_unit_id=unit,
        eligible=True,
        selection_status="eligible",
        separation="LC-MS",
        acquisition_mode=acquisition,
        ion_mode="Positive",
        untargeted=True,
        files=files,
        total_download_bytes=sum(item.size_bytes for item in files),
        sample_count=len(files),
        sample_metadata=[
            {"sample_id": Path(name).stem, "raw_file": f"FILES/{name}", "values": {}} for name in payloads
        ],
    )
    if declared:
        project.analysis_inputs = [
            {
                "path": f"FILES/{name}", "kind": "file", "suffix": ".mzxml", "format": "", "member_count": 1,
                "size_bytes": len(data), "sample_id": Path(name).stem, "requires_conversion": True,
                "conversion_target": "mzML",
            }
            for name, data in payloads.items()
        ]
        project.analysis_inputs_declared = True
    return project


class _Scratch(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()

    def tearDown(self) -> None:
        self._directory.cleanup()

    def lease(self, payloads: dict[str, bytes], project: RepositoryProject | None = None, **options) -> dict:
        project = project or _project(payloads)
        client = _Client({f"https://example.org/{name}": data for name, data in payloads.items()})
        lease = create_download_lease(project, self.root, 10**9, client=client, **options)
        return read_manifest(lease["manifest_path"])


def _stage(manifest: dict, name: str) -> dict:
    return next(entry for entry in manifest["lease_stages"] if entry["stage"] == name)


def _rows(manifest: dict) -> dict[str, dict]:
    return {Path(row["path"]).name: row for row in manifest["input_lineage"]["rows"]}


# ---- before any byte: eligibility -------------------------------------------------------------------------


class AnMzxmlUnitIsEligibleWithAConversionPlan(unittest.TestCase):
    """evaluate_eligibility used to exclude every unit holding an mzXML; it now plans the conversion."""

    @staticmethod
    def _handoff(paths: list[str], *, convertible: bool = True) -> dict:
        """A Catalog 0.6.1 handoff: each file of role raw marked requires_conversion, mzXML for conversion to mzML."""
        files = [
            {
                "path": path, "role": "raw", "size_bytes": 100, "download_url": f"https://example.org/{path}",
                "requires_conversion": "MS-DIAL has no reader for this format",
                **({"conversion_target": "mzML"} if convertible else {}),
            }
            for path in paths
        ]
        return {
            "schema": "msdial-repository-reanalysis-handoff.v1",
            "repository": "metabolights",
            "accession": "MTBLS417",
            "analysis_unit_id": "mtbls417-pos-dda",
            "technical_settings": {
                "separation": "LC-MS", "ion_mode": "Positive", "acquisition_mode": "DDA", "untargeted": True,
            },
            "files": files,
            "sample_metadata": [
                {"sample_id": Path(path).name.split(".")[0], "raw_file": path, "attributes": {}} for path in paths
            ],
            "analysis_input_model": "one-input-per-sample.v1",
            "analysis_inputs_declared": True,
            "analysis_input_count": len(paths),
            "analysis_inputs": [
                {
                    "path": path, "kind": "file", "suffix": ".mzxml", "format": "", "member_count": 1,
                    "size_bytes": 100, "sample_id": Path(path).name.split(".")[0], "requires_conversion": True,
                    "conversion_target": "mzML" if convertible else "",
                }
                for path in paths
            ],
            "class_proposal": {"proposal_id": "class-1", "assignments": []},
            "blocking_reasons": [],
            "download_scope": {"file_count": len(paths), "analysis_file_count": len(paths), "bundle_bytes": 100 * len(paths)},
            "sample_count": len(paths),
        }

    def test_an_mtbls417_shaped_unit_is_eligible_with_a_conversion_plan(self) -> None:
        paths = [f"FILES/MTBLS417_POS_{index:02d}.mzXML" for index in range(1, 5)]

        project, _workspace = mcp_server._project_from_analysis_unit_handoff(self._handoff(paths))

        self.assertTrue(project["eligible"], project["exclusion_reasons"])
        self.assertEqual("eligible", project["selection_status"])
        self.assertEqual([], project["exclusion_reasons"])
        self.assertEqual({"requires_conversion"}, {item["role"] for item in project["files"]})
        plan = project["conversion_plan"]
        self.assertEqual(CONVERSION_PLAN_SCHEMA, plan["schema"])
        self.assertEqual("mzML", plan["target"])
        self.assertEqual((4, paths), (plan["named_inputs"], plan["names"]))
        self.assertEqual(OFF, plan["options"])
        self.assertIsNone(plan["options"]["impute_polarity"])
        self.assertFalse(any(plan["options"][name] for name in (
            "infer_dia_windows", "synthesize_all_ion_windows", "spectrum_level_collision_energy")))
        # The declared mzXML stay the unit's declared inputs: what is converted from them stands for them.
        self.assertEqual(
            {path.casefold()[6:] for path in paths}, set(declared_analysis_inputs(project_from_dict(project)))
        )

    def test_a_packed_mzxml_plans_its_conversion(self) -> None:
        """MTBLS688 publishes its LC-MS units only as x.mzXML.lzma, which unpack to mzXML."""
        project, _ = mcp_server._project_from_analysis_unit_handoff(self._handoff(["FILES/NEG1/s_Seg1Ev2.mzXML.lzma"]))

        self.assertTrue(project["eligible"], project["exclusion_reasons"])
        self.assertEqual(["FILES/NEG1/s_Seg1Ev2.mzXML.lzma"], project["conversion_plan"]["names"])

    def test_a_unit_with_only_mzdata_stays_excluded(self) -> None:
        project, _ = mcp_server._project_from_analysis_unit_handoff(
            self._handoff(["FILES/a.mzData", "FILES/b.mzData.xml"], convertible=False)
        )

        self.assertFalse(project["eligible"])
        self.assertEqual("excluded", project["selection_status"])
        self.assertEqual(1, len(project["exclusion_reasons"]))
        self.assertTrue(project["exclusion_reasons"][0].startswith(UNCONVERTIBLE_INPUT_REASON), project["exclusion_reasons"])
        self.assertNotIn("conversion_plan", project)
        self.assertEqual({}, declared_analysis_inputs(project_from_dict(project)))

    def test_mzdata_beside_mzxml_still_excludes_the_unit(self) -> None:
        handoff = self._handoff(["FILES/a.mzXML"])
        handoff["files"].append({**handoff["files"][0], "path": "FILES/b.mzData", "download_url": "https://x/b"})
        handoff["files"][-1].pop("conversion_target")
        handoff["sample_metadata"].append({"sample_id": "b", "raw_file": "FILES/b.mzData", "attributes": {}})
        handoff["download_scope"].update(file_count=2, analysis_file_count=2, bundle_bytes=200)
        handoff["analysis_inputs"].append({**handoff["analysis_inputs"][0], "path": "FILES/b.mzData", "suffix": ".mzdata",
                                           "sample_id": "b", "conversion_target": ""})
        handoff.update(sample_count=2, analysis_input_count=2)

        project, _ = mcp_server._project_from_analysis_unit_handoff(handoff)

        self.assertFalse(project["eligible"])
        self.assertTrue(any(item.startswith(UNCONVERTIBLE_INPUT_REASON) for item in project["exclusion_reasons"]))
        self.assertIn("b.mzData", next(item for item in project["exclusion_reasons"] if "mzData" in item))

    def test_a_unit_with_nothing_to_convert_records_no_plan(self) -> None:
        project = RepositoryProject(repository="metabolights", accession="MTBLS2207", files=[
            RepositoryFile("FILES/a.mzML", 1, "https://x/a", role="converted")])

        self.assertNotIn("conversion_plan", project.as_dict())


# ---- the lease's convert stage ------------------------------------------------------------------------------


class TheLeaseConvertsTheUnitsMzxml(_Scratch):
    PAYLOADS = {"S01.mzXML": dda_32(), "S02.mzXML": dda_32()}

    def test_each_mzxml_becomes_an_mzml_input_under_raw_converted(self) -> None:
        manifest = self.lease(self.PAYLOADS)
        raw = Path(manifest["raw_directory"])

        self.assertEqual(
            [str((raw / "converted" / "S01.mzML").resolve()), str((raw / "converted" / "S02.mzML").resolve())],
            manifest["input_candidates"],
        )
        self.assertTrue((Path(manifest["input_directory"]) / "S01.mzXML").is_file(), "the mzXML stays as it arrived")
        self.assertEqual(list(LEASE_STAGES), [entry["stage"] for entry in manifest["lease_stages"]])
        convert = _stage(manifest, "convert")
        self.assertEqual(
            ("completed", 2, 2, 0, 0),
            (convert["status"], convert["mzxml_found"], convert["converted"], convert["failed"], convert["reused"]),
        )
        block = manifest["input_conversions"]
        self.assertEqual(INPUT_CONVERSIONS_SCHEMA, block["schema"])
        self.assertEqual(block, json.loads((Path(manifest["workspace"]) / "provenance" / "input-conversions.json")
                                           .read_text(encoding="utf-8")))
        self.assertEqual(["converted", "converted"], [record["status"] for record in block["records"]])
        self.assertEqual([OFF, OFF], [record["options"] for record in block["records"]], "every inference flag off")
        self.assertEqual(OFF, block["options"])
        self.assertTrue(all(record["validation"]["status"] == "passed" for record in block["records"]))
        self.assertTrue(manifest["execution_allowed"])
        self.assertTrue(manifest["project"]["eligible"])
        self.assertEqual(
            {"converted": 2, "reused": 0, "failed": 0, "not_converted_readable_encoding": 0, "analysis_inputs": 2},
            manifest["project"]["conversion_plan"]["outcome"],
        )

    def test_a_converted_mzml_is_attributed_to_its_mzxml(self) -> None:
        manifest = self.lease(self.PAYLOADS, _project(self.PAYLOADS, declared=True))
        record = manifest["input_conversions"]["records"][0]
        row = _rows(manifest)["S01.mzML"]
        source = str((Path(manifest["input_directory"]) / "S01.mzXML").resolve())

        self.assertEqual("converted", row["kind"])
        self.assertEqual(("S01", ["FILES/S01.mzXML"]), (row["sample_id"], row["declared_names"]))
        self.assertEqual(record["output"]["sha256"], row["checksums"]["sha256"])
        conversion = row["source"]["conversion"]
        self.assertEqual(source, conversion["source_path"])
        self.assertEqual(
            (record["source"]["sha256"], record["source"]["md5"], hashlib.md5(dda_32()).hexdigest()),
            (conversion["source_sha256"], conversion["source_md5"], conversion["source_md5"]),
        )
        self.assertEqual(record["output"]["sha256"], conversion["output_sha256"])
        self.assertEqual(
            {key: record["converter"][key] for key in ("name", "version", "module_sha256")}, conversion["converter"]
        )
        self.assertEqual(("passed", 0), (conversion["validation"]["status"], conversion["validation"]["problem_count"]))
        # The mzXML's own row, as an input's would be: the download, and its declared MD5 verified as it arrived.
        carried = conversion["source_row"]
        self.assertEqual(("file", source), (carried["kind"], carried["path"]))
        self.assertEqual(record["source"]["sha256"], carried["checksums"]["sha256"])
        self.assertTrue(carried["checksums"]["declared_verified"])
        # The mzXML is no input, so it has no row among the rows; it is listed with what it became.
        self.assertNotIn("S01.mzXML", _rows(manifest))
        listed = {Path(item["path"]).name: item for item in manifest["input_lineage"]["conversion_sources"]}
        self.assertEqual(str(Path(record["output"]["path"])), listed["S01.mzXML"]["converted_to"])
        self.assertEqual(source, lineage_stands_for(manifest)[_file_key(row["path"])])

        built = build_repository_analysis_rows(manifest)
        self.assertEqual([], built["failures"])
        self.assertEqual(
            [(row["path"], "S01"), (_rows(manifest)["S02.mzML"]["path"], "S02")],
            [(item["file_path"], item["sample_id"]) for item in built["rows"]],
        )

    def test_a_truncated_mzxml_is_excluded_and_the_rest_run(self) -> None:
        payloads = {**self.PAYLOADS, "S03.mzXML": _truncated()}
        for declared in (False, True):
            with self.subTest(declared=declared), tempfile.TemporaryDirectory() as temporary:
                self.root = Path(temporary).resolve()
                manifest = self.lease(payloads, _project(payloads, declared=declared))
                source = str((Path(manifest["input_directory"]) / "S03.mzXML").resolve())

                self.assertEqual(["S01.mzML", "S02.mzML"], [Path(item).name for item in manifest["input_candidates"]])
                excluded = manifest["excluded_input_candidates"]
                self.assertEqual([(source, CONVERSION_FAILED)], [(item["path"], item["reason"]) for item in excluded])
                self.assertTrue(excluded[0]["problems"][0])
                row = manifest["input_lineage"]["excluded"][0]
                self.assertEqual((source, "S03", CONVERSION_FAILED), (row["path"], row["sample_id"], row["exclusion"]["reason"]))
                failed = manifest["input_conversions"]["records"][2]
                self.assertEqual("failed", failed["status"])
                self.assertFalse(Path(failed["output"]["path"]).exists(), "no mzML is left for a failed conversion")
                self.assertTrue(manifest["execution_allowed"])
                self.assertTrue(manifest["project"]["eligible"])
                self.assertEqual(1, manifest["project"]["conversion_plan"]["outcome"]["failed"])

                built = build_repository_analysis_rows(manifest)
                self.assertEqual([], built["failures"])
                self.assertEqual(["S01", "S02"], [item["sample_id"] for item in built["rows"]])
                self.assertEqual(
                    [(source, CONVERSION_FAILED, "S03")],
                    [(item["path"], item["reason"], item["sample_id"]) for item in built["excluded_inputs"]],
                )

    def test_a_second_lease_reuses_what_the_first_converted(self) -> None:
        first = self.lease(self.PAYLOADS)
        second = self.lease(self.PAYLOADS)

        self.assertEqual(2, _stage(second, "convert")["reused"])
        self.assertEqual([True, True], [item["reused_previous_record"] for item in second["input_conversions"]["records"]])
        self.assertEqual(
            [item["output"]["sha256"] for item in first["input_conversions"]["records"]],
            [item["output"]["sha256"] for item in second["input_conversions"]["records"]],
        )
        self.assertEqual(first["input_candidates"], second["input_candidates"])

    def test_a_lease_stopped_mid_conversion_resumes_from_its_records(self) -> None:
        """Each record is written as its conversion completes, so a re-lease converts only what was not done."""
        payloads = {"S01.mzXML": dda_32(), "S02.mzXML": dda_32(), "S03.mzXML": dda_32()}
        calls: list[str] = []

        def cancel_at_the_second(_index, _total, name, _received, _bytes) -> None:
            calls.append(name)
            if name == "converting S02.mzXML":
                raise _Cancelled("cancelled")

        project = _project(payloads)
        client = _Client({f"https://example.org/{name}": data for name, data in payloads.items()})
        with self.assertRaises(_Cancelled):
            create_download_lease(project, self.root, 10**9, client=client, progress_callback=cancel_at_the_second)
        stopped = read_manifest(self.root / "metabolights" / "MTBLS417" / "mtbls417-pos" / "provenance" / "run-manifest.json")
        self.assertEqual(("download_failed", "convert"), (stopped["status"], stopped["download_failure"]["stage"]))
        record_path = Path(stopped["workspace"]) / "provenance" / "input-conversions.json"
        self.assertEqual(["converted"], [item["status"] for item in json.loads(record_path.read_text(encoding="utf-8"))["records"]])

        manifest = self.lease(payloads)

        self.assertEqual([True, False, False],
                         [item["reused_previous_record"] for item in manifest["input_conversions"]["records"]])
        self.assertEqual(3, len(manifest["input_candidates"]))

    def test_an_mzxml_the_catalog_demoted_is_not_converted(self) -> None:
        """MetaboBank MTBKS157 publishes each sample as .RAW and as .mzXML; the Catalog keeps the vendor file."""
        payloads = {"01026_Bread_nega.RAW": b"thermo raw bytes", "01026_Bread_nega.mzXML": dda_32()}
        project = _project(payloads, checksums=False)
        project.files[1].role = "raw_alternate"
        project.files[0].role = "raw"
        project.sample_metadata = [{"sample_id": "bread", "raw_file": "raw/01026_Bread_nega.RAW", "values": {}}]

        manifest = self.lease(payloads, project)

        self.assertEqual(["01026_Bread_nega.RAW"], [Path(item).name for item in manifest["input_candidates"]])
        self.assertEqual("not_used", _stage(manifest, "convert")["status"])
        self.assertNotIn("input_conversions", manifest)
        self.assertFalse((Path(manifest["raw_directory"]) / "converted").exists())

    def test_a_packed_mzxml_is_converted_once_extracted(self) -> None:
        """MTBLS688's x.mzXML.lzma unpacks to x.mzXML in the extract stage, and is converted after it."""
        packed = lzma.compress(dda_32(), format=lzma.FORMAT_ALONE)
        url = "https://example.org/FILES/x.mzXML.lzma"
        project = RepositoryProject(
            repository="metabolights", accession="MTBLS688", analysis_unit_id="mtbls688-neg", eligible=True,
            selection_status="eligible", files=[RepositoryFile("FILES/x.mzXML.lzma", len(packed), url)],
            total_download_bytes=len(packed), sample_metadata=[{"sample_id": "a", "raw_file": "FILES/x.mzXML.lzma"}],
        )

        lease = create_download_lease(project, self.root, 10**9, client=_Client({url: packed}))

        self.assertEqual(["x.mzML"], [Path(item).name for item in lease["input_candidates"]])
        self.assertEqual("x.mzXML", lease["input_conversions"]["records"][0]["source"]["relative_path"])
        self.assertEqual(("converted", "a"), (_rows(lease)["x.mzML"]["kind"], _rows(lease)["x.mzML"]["sample_id"]))

    SAMPLES = ["211210_NEG_S01", "211210_NEG_S02"]

    def _st003038(self, mzml_bytes: bytes) -> dict:
        """Workbench ST003038's shape: an mzML archive and an mzXML archive of the same samples, which name .mzXML."""

        def archive(suffix: str, folder: str) -> bytes:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as handle:
                for name in self.SAMPLES:
                    handle.writestr(f"{folder}/{name}.{suffix}", dda_32() if suffix == "mzXML" else mzml_bytes)
            return buffer.getvalue()

        payloads = {"ST003038_rawdata_mzML.zip": archive("mzML", "mzML"),
                    "ST003038_rawdata_mzXML.zip": archive("mzXML", "mzXML")}
        files = [
            RepositoryFile(name, len(data), f"https://example.org/{name}", role="shared_raw_archive",
                           checksum=hashlib.md5(data).hexdigest())
            for name, data in payloads.items()
        ]
        project = RepositoryProject(
            repository="metabolomics_workbench", accession="ST003038", analysis_unit_id="an004990-neg",
            eligible=True, selection_status="eligible", files=files, acquisition_mode="DDA",
            total_download_bytes=sum(item.size_bytes for item in files),
            sample_metadata=[{"sample_id": name, "raw_file": f"{name}.mzXML"} for name in self.SAMPLES],
        )
        return self.lease(payloads, project)

    def test_an_mzml_archive_beside_an_mzxml_archive_is_analysed_instead(self) -> None:
        """Only extraction shows ST003038's pairs, so the lease applies the Catalog's encoding rule there: the
        mzML is analysed, attributed to the sample that names the mzXML, and the mzXML is not converted."""
        samples = self.SAMPLES
        manifest = self._st003038(mzml(dda_spectra(6)))
        data = Path(manifest["input_directory"])

        self.assertEqual([str((data / "mzML" / f"{name}.mzML").resolve()) for name in samples], manifest["input_candidates"])
        convert = _stage(manifest, "convert")
        self.assertEqual((0, 2), (convert["converted"], convert["not_converted_readable_encoding"]))
        self.assertEqual([], manifest["input_conversions"]["records"])
        choices = manifest["input_conversions"]["encoding_choices"]
        self.assertEqual([str((data / "mzXML" / f"{name}.mzXML").resolve()) for name in samples],
                         [item["mzxml"] for item in choices])
        rows = _rows(manifest)
        for name in samples:
            row = rows[f"{name}.mzML"]
            self.assertEqual(("extracted_member", name), (row["kind"], row["sample_id"]))
            self.assertEqual(str((data / "mzXML" / f"{name}.mzXML").resolve()), row["encoding_choice"]["stands_for"])
        self.assertFalse((Path(manifest["raw_directory"]) / "converted").exists())
        built = build_repository_analysis_rows(manifest)
        self.assertEqual([], built["failures"])
        self.assertEqual(samples, [item["sample_id"] for item in built["rows"]])

    def test_another_folders_mzml_of_the_same_name_does_not_displace_the_mzxml(self) -> None:
        """A study archive with a folder per polarity holds a QC_01 in each. The positive unit's POS/QC_01.mzML is
        no encoding of the negative unit's NEG/QC_01.mzXML, which is converted and analysed as its own sample."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as handle:
            handle.writestr("ST009999/NEG/QC_01.mzXML", dda_32())
            handle.writestr("ST009999/NEG/S_01.mzXML", dda_32())
            handle.writestr("ST009999/POS/QC_01.mzML", mzml(dda_spectra(6)))
        data = buffer.getvalue()
        name = "ST009999_Rawdata.zip"
        project = RepositoryProject(
            repository="metabolomics_workbench", accession="ST009999", analysis_unit_id="an009999-neg",
            eligible=True, selection_status="eligible", separation="LC-MS", acquisition_mode="DDA",
            ion_mode="Negative", untargeted=True, total_download_bytes=len(data),
            files=[RepositoryFile(name, len(data), f"https://example.org/{name}", role="shared_raw_archive",
                                  checksum=hashlib.md5(data).hexdigest())],
            sample_metadata=[{"sample_id": stem, "raw_file": f"{stem}.mzXML"} for stem in ("QC_01", "S_01")],
        )

        manifest = self.lease({name: data}, project)
        converted = Path(manifest["raw_directory"]) / "converted" / "ST009999" / "NEG"

        self.assertEqual([str((converted / f"{stem}.mzML").resolve()) for stem in ("QC_01", "S_01")],
                         manifest["input_candidates"])
        self.assertEqual([], manifest["input_conversions"]["encoding_choices"])
        self.assertEqual((2, 0), (_stage(manifest, "convert")["converted"],
                                  _stage(manifest, "convert")["not_converted_readable_encoding"]))
        built = build_repository_analysis_rows(manifest)
        self.assertEqual([], built["failures"])
        self.assertEqual(manifest["input_candidates"], sorted(item["file_path"] for item in built["rows"]))

    def test_one_samples_encodings_are_told_by_their_folders_less_the_words_naming_an_encoding(self) -> None:
        root = self.root / "raw" / "data"

        def one_sample(first: str, second: str) -> bool:
            return _sample_locus(str(root / first), root) == _sample_locus(str(root / second), root)

        self.assertTrue(one_sample("mzML/x.mzML", "mzXML/x.mzXML"), "ST003038's archives")
        self.assertTrue(one_sample("ST1/NEG_mzML/x.mzML", "ST1/neg-mzXML/x.mzXML"))
        self.assertTrue(one_sample("x.mzML", "mzXML/x.mzXML"))
        self.assertTrue(one_sample("RAW/x.raw", "mzXML/x.mzXML"))
        self.assertFalse(one_sample("POS/x.mzML", "NEG/x.mzXML"))
        self.assertFalse(one_sample("HILIC_POS_mzML/x.mzML", "HILIC_NEG_mzXML/x.mzXML"))
        self.assertFalse(one_sample("Batch_D/x.mzML", "Batch_E/x.mzXML"), "a one-letter word is not set aside")

    def test_a_full_disk_stops_the_lease_and_a_retry_converts_the_rest(self) -> None:
        """A full disk is not the mzXML's fault: no sample is excluded for it, and the lease fails so that the unit
        is retried, reusing what was converted."""
        payloads = {"S01.mzXML": dda_32(), "S02.mzXML": dda_32(), "S03.mzXML": dda_32()}
        write = mzxml_conversion._write_mzml
        calls: list[str] = []

        def fill_at_the_second(*args, **kwargs):
            calls.append("write")
            if len(calls) == 2:
                raise OSError(errno.ENOSPC, "No space left on device")
            return write(*args, **kwargs)

        with patch.object(mzxml_conversion, "_write_mzml", side_effect=fill_at_the_second):
            with self.assertRaises(OSError) as raised:
                self.lease(payloads)

        self.assertEqual(errno.ENOSPC, raised.exception.errno)
        stopped = read_manifest(self.root / "metabolights" / "MTBLS417" / "mtbls417-pos" / "provenance" / "run-manifest.json")
        self.assertEqual(("download_failed", "convert"), (stopped["status"], stopped["download_failure"]["stage"]))
        self.assertFalse(stopped["execution_allowed"])
        self.assertNotIn("excluded_input_candidates", stopped)
        block = json.loads((Path(stopped["workspace"]) / "provenance" / "input-conversions.json").read_text(encoding="utf-8"))
        self.assertEqual(("stopped", ["converted", "failed"]), (block["status"], [item["status"] for item in block["records"]]))
        self.assertEqual(errno.ENOSPC, block["records"][1]["error_errno"])

        manifest = self.lease(payloads)

        self.assertEqual(["S01.mzML", "S02.mzML", "S03.mzML"], [Path(item).name for item in manifest["input_candidates"]])
        self.assertEqual([True, False, False],
                         [item["reused_previous_record"] for item in manifest["input_conversions"]["records"]])
        self.assertNotIn("excluded_input_candidates", manifest)
        self.assertTrue(manifest["execution_allowed"])

    def test_an_mzml_twin_nothing_can_decode_does_not_outrank_the_mzxml(self) -> None:
        """A convertible mzXML outranks an unreadable twin (2026-09-30): a Numpress mzML is one."""
        manifest = self._st003038(_numpress_mzml())
        raw = Path(manifest["raw_directory"])

        self.assertEqual([str((raw / "converted" / "mzXML" / f"{name}.mzML").resolve()) for name in self.SAMPLES],
                         manifest["input_candidates"])
        convert = _stage(manifest, "convert")
        self.assertEqual((2, 0), (convert["converted"], convert["not_converted_readable_encoding"]))
        self.assertEqual(self.SAMPLES, [_rows(manifest)[f"{name}.mzML"]["sample_id"] for name in self.SAMPLES])


class AUnitLeftWithNoInputIsSkipped(_Scratch):
    def test_a_unit_whose_every_mzxml_fails_is_recorded_and_its_preflight_skips_it(self) -> None:
        payloads = {"S01.mzXML": _truncated()}

        manifest = self.lease(payloads, campaign_authorization=dict(_APPROVAL))

        self.assertEqual("prepared", manifest["status"])
        self.assertEqual([], manifest["input_candidates"])
        self.assertFalse(manifest["execution_allowed"])
        project = manifest["project"]
        self.assertEqual(("excluded", False), (project["selection_status"], project["eligible"]))
        self.assertTrue(any(item.startswith(NO_CONVERTED_INPUT_REASON) for item in project["exclusion_reasons"]))

        # The preflight has nothing to read, and says so with a disposition instead of raising.
        extractor = _PinnedExtractor.make(self.root / "build")
        fake = _Extractor({})
        with patch("msdial_app.repository_reanalysis.subprocess.run", side_effect=fake):
            result = run_raw_metadata_preflight(Path(manifest["workspace"]) / "provenance" / "run-manifest.json", extractor)

        self.assertEqual([], fake.commands)
        disposition = result["campaign_disposition"]
        self.assertEqual(("skip", ["no_inputs", CONVERSION_FAILED], True),
                         (disposition["disposition"], disposition["reasons"], disposition["applied"]))
        self.assertEqual("skipped_by_preflight", result["status"])
        self.assertFalse(result["execution_allowed"])

    def test_a_unit_with_no_input_for_another_reason_still_raises_at_preflight(self) -> None:
        manifest = self.lease({"S01.mzXML": dda_32()})
        path = Path(manifest["workspace"]) / "provenance" / "run-manifest.json"
        current = read_manifest(path)
        current["input_candidates"] = []
        path.write_text(json.dumps(current), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "No extracted MS-DIAL input candidate"):
            run_raw_metadata_preflight(path, _PinnedExtractor.make(self.root / "build"))


class AConvertedUnitStaysEligible(_Scratch):
    """The preflight lane's open issue: a converted unit ran under a campaign while its project said excluded."""

    def test_its_preflight_reads_the_mzml_and_its_disposition_keeps_it_eligible(self) -> None:
        payloads = {"S01.mzXML": dda_32(), "S02.mzXML": dda_32()}
        manifest = self.lease(payloads, campaign_authorization=dict(_APPROVAL))
        extractor = _PinnedExtractor.make(self.root / "build")
        fake = _Extractor({name: {"polarity": "Positive"} for name in ("S01.mzML", "S02.mzML")})

        with patch("msdial_app.repository_reanalysis.subprocess.run", side_effect=fake):
            result = run_raw_metadata_preflight(Path(manifest["workspace"]) / "provenance" / "run-manifest.json", extractor)

        self.assertEqual([["S01.mzML", "S02.mzML"]], fake.inputs_read(), "the extractor reads the converted mzML")
        self.assertEqual(("run", True), (result["campaign_disposition"]["disposition"], result["campaign_disposition"]["applied"]))
        self.assertEqual(("preflight_passed", True), (result["status"], result["execution_allowed"]))
        project = result["project"]
        self.assertEqual((True, "eligible", []), (project["eligible"], project["selection_status"], project["exclusion_reasons"]))


class AMixedConvertedUnitSplits(_Scratch):
    """MetaboLights MTBLS1572 is mzXML only, and its headers mix DDA and DIA files: it splits after conversion."""

    MODES = {"a_DDA_1.mzXML": "DDA", "b_DIA_1.mzXML": "DIA", "c_DDA_2.mzXML": "DDA", "d_DIA_2.mzXML": "DIA"}

    def test_the_parts_hold_the_converted_inputs_and_the_samples_that_name_their_mzxml(self) -> None:
        payloads = {name: dda_32() if mode == "DDA" else swath_32() for name, mode in self.MODES.items()}
        manifest = self.lease(payloads, _project(payloads, acquisition="DIA", unit="mtbls1572-pos"))
        path = Path(manifest["workspace"]) / "provenance" / "run-manifest.json"
        verdicts = {Path(name).with_suffix(".mzML").name: {"method": mode, "polarity": "Positive"}
                    for name, mode in self.MODES.items()}
        fake = _Extractor(verdicts)
        extractor = self.root / "RawMetadataConsoleApp.exe"
        extractor.write_bytes(b"stub")
        with patch("msdial_app.repository_reanalysis.subprocess.run", side_effect=fake):
            preflighted = run_raw_metadata_preflight(path, extractor)
        self.assertEqual("Mixed", preflighted["raw_metadata_preflight"]["summary"]["acquisition_mode"])

        result = split_unit_by_acquisition(path, confirmed=True)

        self.assertTrue(result["written"], result["blockers"])
        self.assertEqual([], result["unclaimed_samples"])
        parts = {item["acquisition_mode"]: read_manifest(item["manifest_path"]) for item in result["parts"]}
        self.assertEqual({"DDA", "DIA"}, set(parts))
        for mode, part in parts.items():
            mine = sorted(name for name, value in self.MODES.items() if value == mode)
            self.assertEqual([Path(name).with_suffix(".mzML").name for name in mine],
                             [Path(item).name for item in part["input_candidates"]])
            self.assertEqual([Path(name).stem for name in mine],
                             sorted(item["sample_id"] for item in part["project"]["sample_metadata"]))
            self.assertEqual([f"FILES/{name}" for name in mine], sorted(item["name"] for item in part["project"]["files"]))
            self.assertEqual([], part["project"]["exclusion_reasons"])
            self.assertEqual({"converted"}, {row["kind"] for row in part["input_lineage"]["rows"]})
            self.assertTrue(all(row["source"]["conversion"]["source_row"] for row in part["input_lineage"]["rows"]))
            self.assertNotIn("conversion_sources", part["input_lineage"])


# ---- what the gate reads ---------------------------------------------------------------------------------------


@unittest.skipUnless(GATE.is_file(), "the reanalysis gate (verify-run-invariants.py) is not on this machine")
class TheGateFollowsEachConversion(_Scratch):
    """The gate's SUM-1 follows a converted input to the mzXML it was read from, and its CONV-1 holds the mzML to
    its record. Both are run here, by the gate's own script, on a workspace this lease and CSV builder wrote."""

    def gate(self, payloads: dict[str, bytes], project: RepositoryProject) -> dict[str, dict]:
        manifest = self.lease(payloads, project)
        built = build_repository_analysis_rows(manifest)
        self.assertEqual([], built["failures"])
        write_analysis_csv(built, Path(manifest["output_directory"]) / "analysis_files.csv")
        completed = subprocess.run(
            [sys.executable, str(GATE), manifest["workspace"], "--stage", "before-production", "--json"],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )
        report = json.loads(completed.stdout)
        return {check["check_id"]: check for check in report["checks"]}

    def test_conv1_and_sum1_pass_on_a_unit_this_lease_converted(self) -> None:
        payloads = {f"S{index:02d}.mzXML": dda_32() for index in range(1, 4)}

        checks = self.gate(payloads, _project(payloads, declared=True))

        for check_id in ("CONV-1", "SUM-1", "INP-1", "CNT-1"):
            self.assertEqual("pass", checks[check_id]["status"], f"{check_id}: {checks[check_id]['detail']}")
        self.assertEqual(3, checks["CONV-1"]["evidence"]["converted_inputs"])
        self.assertEqual(3, checks["CONV-1"]["evidence"]["rehashed"])
        self.assertEqual(("verified", 3), (checks["SUM-1"]["evidence"]["basis"], checks["SUM-1"]["evidence"]["converted_inputs"]))

    def test_an_mzxml_a_verified_study_archive_held_rests_on_that_archive(self) -> None:
        """35 Workbench units hold their mzXML only inside a study archive: input <- conversion <- the archive's
        member listing <- the archive's published MD5, one lineage the gate follows (SUM-1 archive_verified)."""
        samples = ["S1_neg", "S2_neg"]
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as handle:
            for name in samples:
                handle.writestr(f"ST000001/NEG/{name}.mzXML", dda_32())
            handle.writestr("ST000001/POS/S1_pos.mzXML", dda_32())
        data = buffer.getvalue()
        project = RepositoryProject(
            repository="metabolomics_workbench", accession="ST000001", analysis_unit_id="an000001-neg",
            eligible=True, selection_status="eligible", separation="LC-MS", acquisition_mode="DDA",
            ion_mode="Negative", untargeted=True, total_download_bytes=len(data),
            files=[RepositoryFile("ST000001_Rawdata.zip", len(data), "https://example.org/ST000001_Rawdata.zip",
                                  role="raw_archive", checksum=hashlib.md5(data).hexdigest())],
            sample_metadata=[{"sample_id": name, "raw_file": f"{name}.mzXML"} for name in samples],
        )

        checks = self.gate({"ST000001_Rawdata.zip": data}, project)

        self.assertEqual("pass", checks["CONV-1"]["status"], checks["CONV-1"]["detail"])
        self.assertEqual(2, checks["CONV-1"]["evidence"]["converted_inputs"], "the other unit's mzXML is not converted")
        evidence = checks["SUM-1"]["evidence"]
        self.assertEqual(("archive_verified", 2, []), (evidence["basis"], evidence["converted_inputs"], evidence["uncovered"]))

    def test_a_failed_conversion_is_a_warning_and_the_rest_pass(self) -> None:
        payloads = {"S01.mzXML": dda_32(), "S02.mzXML": dda_32(), "S03.mzXML": _truncated()}

        checks = self.gate(payloads, _project(payloads))

        self.assertEqual("warn", checks["CONV-1"]["status"], checks["CONV-1"]["detail"])
        self.assertEqual(1, checks["CONV-1"]["evidence"]["failed"])
        self.assertEqual("pass", checks["SUM-1"]["status"], checks["SUM-1"]["detail"])
        self.assertEqual("pass", checks["CNT-1"]["status"], checks["CNT-1"]["detail"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
