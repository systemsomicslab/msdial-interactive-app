"""Nothing built to be shared carries this machine's paths or a private library's location.

The Console writes file://<library location> into every mzTab-M, and the publication report, its tables and
the workflow bundle carried the run's settings, locations included. The production library is kept outside any
user profile, so a check for profile paths never saw it. These tests put a fake private MSP on a drive that is
no profile's, in every encoding a writer produces, and require the gate's own SEC-1 to pass what is written.
"""

from __future__ import annotations

import csv
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from msdial_app import run_finalisation
from msdial_app.materials_methods import generate_publication_report
from msdial_app.mztab_validation import validate_mztab_file
from msdial_app.repository_reanalysis import _write_json
from msdial_app.run_finalisation import FinalisationHeld, finalise_console_run, redact_mztab
from msdial_app.sharing import PATH_POLICY, SHARED_PATHS_MEMBER, SharingContext, library_records
from msdial_app.workflow import _write_reproduction_files

# Synthetic locations. Nothing is read from them; they stand for a library kept on a data drive.
PRIVATE = "E:\\lab libs\\MSMS-Private-pos-VS21.msp"
PUBLIC = "E:/public libs/Public-LC25.lbm2"
CONSOLE = "F:\\tools\\MS-DIAL Console\\MSDIALCUI.exe"
TEMPLATE = "F:\\tools\\templates\\lcms-template.txt"
PRIVATE_SHA = "ab" * 32
PUBLIC_SHA = "cd" * 32
# The private location as each writer puts it: backslashes, doubled backslashes (JSON), forward slashes, the
# Console's file URI and a percent-encoded one.
PRIVATE_ENCODINGS = (
    "E:\\lab libs", "E:\\\\lab libs", "E:/lab libs", "file://E:/lab", "lab%20libs", "E%3A%5Clab",
)
GATE = Path(
    os.environ.get("MSDIAL_REANALYSIS_GATE")
    or "D:\\13_MSDIAL_Public_Reanalysis\\code\\scripts\\verify-run-invariants.py"
)
SHARED_FILES = (
    "MS_DIAL_publication_report.json", "MS_DIAL_publication_reporting_bundle.zip", "Supplementary_Table_MS_DIAL.tsv",
    "Supplementary_Table_MS_DIAL.xlsx", "MS_DIAL_Materials_and_Methods.txt", "MS_DIAL_QA_Results.txt",
    "msdial-workflow-bundle.zip",
)


def _load_gate():
    if not GATE.is_file():
        return None
    spec = importlib.util.spec_from_file_location("verify_run_invariants_sec1", GATE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    written = sys.dont_write_bytecode
    sys.dont_write_bytecode = True  # the gate's checkout is not ours to write into
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = written
    return module


def _texts(where: str, data: bytes, depth: int = 0) -> list[tuple[str, str]]:
    """Every text in a file, a zip read member by member (a bundle, and the workbook inside it)."""
    if data[:4] == b"PK\x03\x04" and depth < 3:
        found = []
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for name in archive.namelist():
                found.extend(_texts(f"{where}:{name}", archive.read(name), depth + 1))
        return found
    return [(where, data.decode("utf-8-sig", errors="replace"))]


def _mztab(workspace: Path) -> str:
    raw = workspace.as_posix() + "/raw/data"
    lines = [
        "MTD\tmzTab-version\t2.0.0-M",
        "MTD\tmzTab-ID\tAlignResult-20269301200",
        "MTD\tsoftware[1]\t[MS, MS:1003082, MS-DIAL, 5.5.260926+31dea2b3]",
        f"MTD\tms_run[1]-location\tfile://{raw}/Sample A.mzML",
        "MTD\tms_run[1]-format\t[MS, MS:1000584, mzML format, ]",
        f"MTD\tms_run[2]-location\tfile://{raw}/QC_01.mzML",
        "MTD\tms_run[2]-format\t[MS, MS:1000584, mzML format, ]",
        "MTD\tassay[1]\tSample A",
        "MTD\tassay[1]-ms_run_ref\tms_run[1]",
        "MTD\tassay[2]\tQC_01",
        "MTD\tassay[2]-ms_run_ref\tms_run[2]",
        "MTD\tdatabase[1]\t[,, User-defined MSP library file, ]",
        "MTD\tdatabase[1]-prefix\tMspDB_1_MSMS-Private-pos-VS21",
        "MTD\tdatabase[1]-version\tMSMS-Private-pos-VS21.msp",
        "MTD\tdatabase[1]-uri\tfile://E:/lab libs/MSMS-Private-pos-VS21.msp",
        "MTD\tdatabase[2]\t[,, MS-DIAL LipidsMsMs database, ]",
        "MTD\tdatabase[2]-prefix\tLbmDB",
        "MTD\tdatabase[2]-version\tPublic-LC25.lbm2",
        "MTD\tdatabase[2]-uri\tfile://E:/public libs/Public-LC25.lbm2",
        "MTD\tcustom[1]\t[,, MS-DIAL library statistics database[1], 44353 records; 10365 compounds; sha256:f0179b4cd4fe6c0c]",
        "MTD\tcustom[2]\t[,, MS-DIAL library statistics database[2], 1446226 records; 962387 compounds; sha256:90bf146b6112beff]",
        "MTD\tsmall_molecule-quantification_unit\t[,,precursor intensity (peak height), ]",
        "",
        "SMH\tSML_ID\tSMF_ID_REFS\tdatabase_identifier\tchemical_formula\tsmiles\tinchi\tchemical_name\turi",
        "SML\t1\t1\tnull\tC5H8\tC/C=C\\C(\\C)CC\tnull\tisoprene\tnull",
        "",
    ]
    return "\r\n".join(lines)


def build_unit(root: Path) -> dict:
    """A repository unit as Interactive lays one out, prepared, with a Console mzTab-M in its output."""
    workspace = root / "metabolights" / "MTBLS9999" / "unit-a"
    raw = workspace / "raw" / "data"
    output = workspace / "output"
    provenance = workspace / "provenance"
    for directory in (raw, output, provenance):
        directory.mkdir(parents=True, exist_ok=True)
    inputs = [raw / "Sample A.mzML", raw / "QC_01.mzML"]
    for path in inputs:
        path.write_text("<mzML/>", encoding="ascii")
    manifest = provenance / "run-manifest.json"
    _write_json(manifest, {
        "schema": "msdial-public-reanalysis-run.v1",
        "status": "prepared",
        "workspace": str(workspace),
        "raw_directory": str(workspace / "raw"),
        "output_directory": str(output),
        "input_candidates": [str(path) for path in inputs],
        "raw_retention_policy": "keep",
        "project": {"repository": "metabolights", "accession": "MTBLS9999", "analysis_unit_id": "unit-a"},
    })
    files = [
        {"file_path": str(path), "file_name": path.stem, "file_type": "QC" if "QC" in path.name else "Sample",
         "class_id": "All", "acquisition_type": "DDA", "batch_order": 1, "analytical_order": index + 1, "factor": 1}
        for index, path in enumerate(inputs)
    ]
    state = {
        "project_type": "lcms",
        "ion_mode": "Positive",
        "target_omics": "Metabolomics",
        "files": files,
        "repository_run_manifest": str(manifest),
        "console_path": CONSOLE,
        "template_path": TEMPLATE,
        "output_root": str(output),
        "export_folder_path": str(output),
        "msp_annotators": [{"annotator_id": "msp_high_quality", "msp_file_path": PRIVATE, "priority": 1}],
        "lbm_path": PUBLIC,
        "library_provenance": [
            {"path": PRIVATE, "version": "VS21", "source": "", "doi": "", "license": "institutional/private"},
            {"path": PUBLIC, "version": "21904324", "source": "https://zenodo.org/records/21904324",
             "doi": "10.5281/zenodo.21904324", "license": "CC BY 4.0"},
        ],
        "msp_annotator_settings_file_path": str(output / "msp_annotator_settings.tsv"),
        # The private location in the other encodings a writer produces, and its directory alone.
        "annotation_note": "loaded from file://E:/lab%20libs/MSMS-Private-pos-VS21.msp",
        "library_directory": "E:\\lab libs",
    }
    with (output / "analysis_files.csv").open("w", encoding="ascii", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(files[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(files)
    (output / "method.txt").write_text(
        f"Msp file path: \nLbm file path: {PUBLIC}\n"
        f"MSP annotator settings file path: {output / 'msp_annotator_settings.tsv'}\n"
        f"Export folder path: {output}\n",
        encoding="utf-8",
    )
    (output / "msp_annotator_settings.tsv").write_text(
        f"annotator_id\tmsp_file_path\tpriority\nmsp_high_quality\t{PRIVATE}\t1\n", encoding="ascii"
    )
    libraries = [
        {"name": "MSMS-Private-pos-VS21.msp", "filename": "MSMS-Private-pos-VS21.msp", "role": "msp",
         "sha256": PRIVATE_SHA, "bytes": 2090000000, "size_bytes": 2090000000, "private": True,
         "distribution": "private"},
        {"name": "Public-LC25.lbm2", "filename": "Public-LC25.lbm2", "role": "lbm", "sha256": PUBLIC_SHA,
         "bytes": 700, "size_bytes": 700, "private": False, "distribution": "public",
         "doi": "10.5281/zenodo.21904324"},
    ]
    (output / "run-manifest.json").write_text(json.dumps({
        "console": {"path": CONSOLE, "assembly_path": CONSOLE.replace(".exe", ".dll"),
                    "provenance": {"source_root": "F:\\src\\MsdialWorkbench"}},
        "libraries": libraries,
        "input_csv": str(output / "analysis_files.csv"),
        "method_file": str(output / "method.txt"),
        "source_files": [str(path) for path in inputs],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    command = [CONSOLE, "lcms", "-i", str(output / "analysis_files.csv"), "-o", str(output),
               "-m", str(output / "method.txt"), "-p"]
    _write_reproduction_files(output, state, command)
    mztab = output / "AlignResult-20269301200.mzTab"
    mztab.write_bytes(_mztab(workspace).encode("utf-8"))
    return {
        "workspace": workspace, "output": output, "manifest": manifest, "state": state, "mztab": mztab,
        "preparation": {
            "run_directory": str(output),
            "repository_run_manifest": str(manifest),
            "input_csv": str(output / "analysis_files.csv"),
            "settings_file": str(output / "workflow-settings.json"),
            "manifest": str(output / "run-manifest.json"),
        },
    }


def publish(unit: dict) -> dict:
    settings = json.loads((unit["output"] / "workflow-settings.json").read_text(encoding="utf-8"))
    return generate_publication_report(settings, None, unit["output"], app_version="0.5.14",
                                       console_version="5.5.260926+31dea2b3")


class SharingContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.unit = build_unit(Path(self.directory.name))
        self.context = SharingContext.for_state(self.unit["state"], run_directory=self.unit["output"],
                                                recorded=json.loads((self.unit["output"] / "run-manifest.json")
                                                                    .read_text(encoding="utf-8"))["libraries"])

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_a_private_library_location_in_every_encoding_becomes_its_file_name(self) -> None:
        for text in (PRIVATE, PRIVATE.replace("\\", "/"), PRIVATE.replace("\\", "\\\\"),
                     "file://E:/lab libs/MSMS-Private-pos-VS21.msp", "file:///E:/lab%20libs/MSMS-Private-pos-VS21.msp",
                     "E%3A%5Clab%20libs%5CMSMS-Private-pos-VS21.msp", "libs\\MSMS-Private-pos-VS21.msp"):
            with self.subTest(text=text):
                self.assertEqual("MSMS-Private-pos-VS21.msp", self.context.text(text))

    def test_a_repository_units_paths_become_workspace_relative_and_the_rest_is_withheld(self) -> None:
        workspace = self.unit["workspace"]
        self.assertEqual("raw/data/Sample A.mzML", self.context.view(str(workspace / "raw" / "data" / "Sample A.mzML")))
        self.assertEqual("output/method.txt", self.context.view(str(workspace / "output" / "method.txt")))
        self.assertEqual("MSDIALCUI.exe", self.context.text(CONSOLE))
        self.assertEqual("<local path withheld: a.mzML>", self.context.text("G:\\elsewhere\\a.mzML"))
        self.assertEqual("<local path withheld>", self.context.text("\\\\labnas\\share"))
        row = f"{workspace / 'raw' / 'data' / 'QC_01.mzML'},QC_01,QC"
        self.assertEqual("raw\\data\\QC_01.mzML,QC_01,QC", self.context.text(row))

    def test_a_laboratory_run_keeps_every_path_but_a_private_librarys(self) -> None:
        state = {key: value for key, value in self.unit["state"].items() if key != "repository_run_manifest"}
        laboratory = SharingContext.for_state(state)
        self.assertFalse(laboratory.full)
        self.assertEqual("MSMS-Private-pos-VS21.msp", laboratory.text(PRIVATE))
        self.assertEqual(PUBLIC, laboratory.text(PUBLIC))
        self.assertEqual("D:\\data\\sample.raw", laboratory.text("D:\\data\\sample.raw"))

    def test_structures_identifiers_and_urls_are_left_alone(self) -> None:
        for text in ("C/C=C\\C(\\C)CC", "https://doi.org/10.5281/zenodo.21904103", "MS:1003082",
                     "MSMS-Private-pos-VS21.msp  sha256:" + PRIVATE_SHA, "2026-09-30T14:12:00+09:00"):
            with self.subTest(text=text):
                self.assertEqual(text, self.context.text(text))
                self.assertEqual([], self.context.residual(text))

    def test_a_library_is_public_only_with_a_doi_or_https_source_and_no_private_licence(self) -> None:
        records = {item.name: item for item in library_records({
            "msp_annotators": [{"msp_file_path": "E:\\a\\none.msp"}, {"msp_file_path": "E:\\a\\http.msp"},
                               {"msp_file_path": "E:\\a\\licensed.msp"}, {"msp_file_path": "E:\\a\\open.msp"}],
            "library_provenance": [
                {"path": "E:\\a\\http.msp", "source": "http://example.org/x"},
                {"path": "E:\\a\\licensed.msp", "doi": "10.1/x", "license": "in-house use only"},
                {"path": "E:\\a\\open.msp", "doi": "10.1/y", "license": "CC BY 4.0"},
            ],
        })}
        self.assertEqual({"none.msp": True, "http.msp": True, "licensed.msp": True, "open.msp": False},
                         {name: item.private for name, item in records.items()})
        self.assertNotIn("source", records["http.msp"].identity)  # a private library's source may be a location


class MztabRedactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.unit = build_unit(Path(self.directory.name))

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _metadata(self) -> dict[str, str]:
        text = self.unit["mztab"].read_text(encoding="utf-8")
        return {line.split("\t")[1]: line.split("\t", 2)[2]
                for line in text.splitlines() if line.startswith("MTD\t")}

    def test_the_console_mztab_names_libraries_by_identity_and_raw_files_by_workspace_path(self) -> None:
        before = self.unit["mztab"].read_bytes()
        record = finalise_console_run("job1", self.unit["preparation"], {"mztab": [str(self.unit["mztab"])]},
                                      0, {"missing": []}, lambda _line: None)
        metadata = self._metadata()

        self.assertEqual([], record["errors"])
        self.assertEqual("null", metadata["database[1]-uri"])
        self.assertEqual("https://doi.org/10.5281/zenodo.21904324", metadata["database[2]-uri"])
        self.assertEqual("raw/data/Sample%20A.mzML", metadata["ms_run[1]-location"])
        self.assertEqual("raw/data/QC_01.mzML", metadata["ms_run[2]-location"])
        # What identifies each library is kept: the file name, the prefix, the Console's record counts.
        self.assertEqual("MSMS-Private-pos-VS21.msp", metadata["database[1]-version"])
        self.assertEqual("MspDB_1_MSMS-Private-pos-VS21", metadata["database[1]-prefix"])
        self.assertIn("44353 records; 10365 compounds", metadata["custom[1]"])
        self.assertIn("MSMS-Private-pos-VS21.msp; sha256:" + PRIVATE_SHA, metadata["custom[3]"])
        self.assertIn("database[1]", metadata["custom[3]"])
        # Only the metadata changed: the tables, the SMILES and the line endings are as the Console wrote them.
        after = self.unit["mztab"].read_bytes()
        self.assertEqual(before[before.index(b"\r\nSMH"):], after[after.index(b"\r\nSMH"):])
        self.assertEqual(before.count(b"\r\n") + 1, after.count(b"\r\n"))
        self.assertEqual(2, sum(line.startswith(b"MTD\tms_run[") and b"-location\t" in line
                                for line in after.splitlines()))
        self.assertEqual("passed", validate_mztab_file(self.unit["mztab"])["status"])
        # The originals are kept in a local-only record, and the manifest names it.
        local = self.unit["manifest"].parent / "mztab-redaction.local.json"
        kept = json.loads(local.read_text(encoding="utf-8"))
        self.assertEqual("local_only", kept["sharing"])
        self.assertIn("file://E:/lab libs/MSMS-Private-pos-VS21.msp",
                      [change["original"] for change in kept["files"][0]["changes"]])
        manifest = json.loads(self.unit["manifest"].read_text(encoding="utf-8"))
        self.assertEqual([local.resolve()], [Path(item) for item in manifest["console_run_finalisation"]["local_only"]])
        self.assertNotIn("changes", json.dumps(manifest["console_run_finalisation"]["mztab_redaction"]))

    def test_a_laboratory_mztab_loses_only_its_private_library_location(self) -> None:
        state = {key: value for key, value in self.unit["state"].items() if key != "repository_run_manifest"}
        result = redact_mztab(self.unit["mztab"], SharingContext.for_state(state))
        metadata = self._metadata()

        self.assertTrue(result["changed"])
        self.assertEqual("null", metadata["database[1]-uri"])
        self.assertEqual("file://E:/public libs/Public-LC25.lbm2", metadata["database[2]-uri"])
        self.assertTrue(metadata["ms_run[1]-location"].startswith("file://"))

    def test_an_mztab_with_nothing_to_redact_is_left_untouched(self) -> None:
        path = self.unit["output"] / "clean.mzTab"
        path.write_bytes(b"MTD\tmzTab-version\t2.0.0-M\r\nMTD\tdatabase[1]-uri\tnull\r\n\r\nSMH\tSML_ID\r\nSML\t1\r\n")
        stamp = path.stat().st_mtime_ns
        result = redact_mztab(path, SharingContext([], [], full=True))
        self.assertFalse(result["changed"])
        self.assertEqual(stamp, path.stat().st_mtime_ns)

    def test_every_custom_param_has_four_fields_as_a_strict_reader_splits_them(self) -> None:
        # [label, accession, name, value]: a comma inside a name or value must be quoted, or jmzTab-M reads a
        # fifth field. The identity line used to end "private, not distributed".
        finalise_console_run("job1", self.unit["preparation"], {"mztab": [str(self.unit["mztab"])]}, 0,
                             {"missing": []}, lambda _line: None)
        customs = {key: value for key, value in self._metadata().items() if key.startswith("custom[")}

        self.assertEqual(["custom[1]", "custom[2]", "custom[3]"], sorted(customs))
        for key, value in customs.items():
            with self.subTest(param=key):
                self.assertEqual(4, len(_param_fields(value)), value)
        self.assertEqual("MS-DIAL library file database[1]", _param_fields(customs["custom[3]"])[2])

    def test_a_library_name_with_a_comma_is_quoted_in_its_identity_line(self) -> None:
        library = SimpleNamespace(name="MSMS, Private-pos.msp", identity={"sha256": PRIVATE_SHA, "bytes": 5})
        line = run_finalisation._identity_line(4, "1", library)
        fields = _param_fields(line.split("\t", 2)[2])

        self.assertEqual(4, len(fields), line)
        self.assertEqual(f"MSMS, Private-pos.msp; sha256:{PRIVATE_SHA}; 5 bytes; private; not distributed", fields[3])


def _param_fields(value: str) -> list[str]:
    """An mzTab-M Param's fields, as a reader that honours quoting splits them."""
    text = value.strip()
    assert text.startswith("[") and text.endswith("]"), text
    return next(csv.reader([text[1:-1]], skipinitialspace=True))


@unittest.skipUnless(os.name == "nt", "Windows refuses to replace a file another handle holds open; POSIX does not")
class HeldMztabTests(unittest.TestCase):
    """The Console's mzTab-M held open by another reader - a viewer, the indexer, antivirus - while the job
    finalises. The redaction used to be skipped with a warning, and the unit kept the private location."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.unit = build_unit(Path(self.directory.name))
        self.quick = patch.multiple(run_finalisation, RETRY_DELAYS_SECONDS=(0.05,) * 40, RETRY_BUDGET_SECONDS=0.3)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_a_reader_that_lets_go_within_the_budget_costs_nothing(self) -> None:
        handle = self.unit["mztab"].open("rb")
        timer = threading.Timer(0.3, handle.close)
        timer.start()
        try:
            with patch.multiple(run_finalisation, RETRY_DELAYS_SECONDS=(0.05,) * 400, RETRY_BUDGET_SECONDS=10.0):
                record = finalise_console_run("job1", self.unit["preparation"], {"mztab": [str(self.unit["mztab"])]},
                                              0, {"missing": []}, lambda _line: None)
        finally:
            timer.join()

        self.assertEqual([], record["errors"])
        self.assertEqual([], record["holds"])
        self.assertIn("MTD\tdatabase[1]-uri\tnull", self.unit["mztab"].read_text(encoding="utf-8"))

    def test_a_laboratory_run_with_no_manifest_carries_its_hold_in_the_job_record(self) -> None:
        preparation = {**self.unit["preparation"], "repository_run_manifest": ""}
        before = self.unit["manifest"].read_bytes()
        with self.unit["mztab"].open("rb"), self.quick:
            record = finalise_console_run("job1", preparation, {"mztab": [str(self.unit["mztab"])]}, 0,
                                          {"missing": []}, lambda _line: None)

        self.assertEqual("laboratory", record["scope"])
        self.assertEqual([(["sharing"], "mztab_redaction")], [(item["blocks"], item["step"]) for item in record["holds"]])
        self.assertEqual(before, self.unit["manifest"].read_bytes())

    def test_an_mztab_held_past_the_budget_is_held_from_publication_until_a_retry_redacts_it(self) -> None:
        logs: list[str] = []
        with self.unit["mztab"].open("rb"), self.quick:
            record = finalise_console_run("job1", self.unit["preparation"], {"mztab": [str(self.unit["mztab"])]},
                                          0, {"missing": []}, logs.append)
            with self.assertRaisesRegex(FinalisationHeld, r"^finalisation_held \[sharing\]: .*held from sharing"):
                publish(self.unit)

        self.assertEqual(["mztab_redaction"], [item["step"] for item in record["holds"]])
        self.assertTrue(any("held" in line for line in logs if line.startswith("WARNING")))
        [hold] = json.loads(self.unit["manifest"].read_text(encoding="utf-8"))["finalisation_holds"]
        self.assertEqual(["sharing"], hold["blocks"])
        self.assertIn("file://E:/lab libs/MSMS-Private-pos-VS21.msp", self.unit["mztab"].read_text(encoding="utf-8"))
        self.assertFalse((self.unit["output"] / "MS_DIAL_publication_report.json").exists())
        self.assertEqual([], [path.name for path in self.unit["output"].iterdir() if path.name.startswith(".redact-")])

        # Let go: the publication retries the redaction first, then the unit's shared artifacts pass SEC-1.
        publish(self.unit)
        text = self.unit["mztab"].read_text(encoding="utf-8")
        self.assertIn("MTD\tdatabase[1]-uri\tnull", text)
        self.assertNotIn("lab libs", text)
        manifest = json.loads(self.unit["manifest"].read_text(encoding="utf-8"))
        self.assertEqual([], manifest["finalisation_holds"])
        self.assertEqual(["mztab_redaction"], [item["step"] for item in manifest["finalisation_hold_resolutions"]])
        kept = json.loads((self.unit["manifest"].parent / "mztab-redaction.local.json").read_text(encoding="utf-8"))
        self.assertIn("file://E:/lab libs/MSMS-Private-pos-VS21.msp",
                      [change["original"] for entry in kept["files"] for change in entry["changes"]])
        gate = _load_gate()
        if gate is not None:  # the gate is the reanalysis checkout's; it is read, never written
            report = gate.Report(self.unit["workspace"])
            gate.check_no_private_path_in_a_shared_artifact(report, self.unit["output"], "before-publish")
            check = next(item for item in report.checks if item.check_id == "SEC-1")
            self.assertEqual(gate.PASS, check.status, check.detail)


class WorkflowBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.unit = build_unit(Path(self.directory.name))

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_the_bundle_is_portable_while_the_files_the_console_reads_keep_their_paths(self) -> None:
        output = self.unit["output"]
        with zipfile.ZipFile(output / "msdial-workflow-bundle.zip") as archive:
            members = {name: archive.read(name).decode("utf-8-sig") for name in archive.namelist()}

        self.assertIn(PRIVATE, (output / "msp_annotator_settings.tsv").read_text(encoding="ascii"))
        self.assertIn(str(output), (output / "method.txt").read_text(encoding="utf-8"))
        self.assertIn("msp_high_quality\tMSMS-Private-pos-VS21.msp\t1", members["msp_annotator_settings.tsv"])
        self.assertIn("Lbm file path: Public-LC25.lbm2", members["method.txt"])
        self.assertIn("MSP annotator settings file path: msp_annotator_settings.tsv", members["method.txt"])
        self.assertIn("raw\\data\\Sample A.mzML,Sample A", members["analysis_files.csv"])
        self.assertIn("'MSDIALCUI.exe'", members["run-msdial.ps1"])
        self.assertNotIn("C:\\path\\to", members["REPRODUCE.txt"])
        declaration = json.loads(members[SHARED_PATHS_MEMBER])
        self.assertEqual(PATH_POLICY, declaration["policy"])
        self.assertEqual({"MSMS-Private-pos-VS21.msp": PRIVATE_SHA, "Public-LC25.lbm2": PUBLIC_SHA},
                         {item["name"]: item["sha256"] for item in declaration["libraries"]})
        self.assertNotIn("guided-answers.json", members)

    def test_a_library_file_is_never_a_member(self) -> None:
        output = self.unit["output"]
        anchor = output / "rt-anchors.msp"
        anchor.write_text("NAME: anchor\n", encoding="ascii")
        state = dict(self.unit["state"], execute_rt_correction=True, rt_correction_anchor_path=str(anchor))
        result = _write_reproduction_files(output, state, ["MSDIALCUI.exe"])
        with zipfile.ZipFile(output / "msdial-workflow-bundle.zip") as archive:
            names = archive.namelist()
        self.assertNotIn("rt-anchors.msp", names)
        self.assertTrue(any(item.startswith("rt-anchors.msp") for item in result["bundle_withheld_members"]))

    def test_a_laboratory_bundle_with_no_private_library_is_unchanged(self) -> None:
        output = self.unit["output"]
        state = {key: value for key, value in self.unit["state"].items()
                 if key not in {"repository_run_manifest", "msp_annotators", "library_provenance"}}
        state["library_provenance"] = [self.unit["state"]["library_provenance"][1]]
        (output / "msp_annotator_settings.tsv").unlink()
        state["msp_annotator_settings_file_path"] = ""
        _write_reproduction_files(output, state, [CONSOLE, "lcms"])
        with zipfile.ZipFile(output / "msdial-workflow-bundle.zip") as archive:
            for name in archive.namelist():
                with self.subTest(member=name):
                    self.assertEqual((output / name).read_bytes(), archive.read(name))


class PublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.unit = build_unit(Path(self.directory.name))

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_the_report_declares_the_policy_and_names_libraries_by_file_name_and_sha256(self) -> None:
        result = publish(self.unit)
        audit = json.loads(Path(result["audit_file"]).read_text(encoding="utf-8"))

        self.assertEqual(PATH_POLICY, audit["shared_path_policy"])
        self.assertEqual({"MSMS-Private-pos-VS21.msp": (PRIVATE_SHA, True), "Public-LC25.lbm2": (PUBLIC_SHA, False)},
                         {item["name"]: (item["sha256"], item["private"]) for item in audit["libraries"]})
        self.assertEqual("raw/data/Sample A.mzML", audit["workflow"]["files"][0]["file_path"])
        self.assertEqual("MSMS-Private-pos-VS21.msp", audit["workflow"]["msp_annotators"][0]["msp_file_path"])
        # The directory the private library is kept in is withheld whole, its space included.
        self.assertEqual("<local path withheld>", audit["workflow"]["library_directory"])
        self.assertEqual("MSDIALCUI.exe", audit["workflow"]["console_path"])
        self.assertIn(f"MSMS-Private-pos-VS21.msp (SHA-256 {PRIVATE_SHA})", result["methods_text"])
        self.assertIn("Public-LC25.lbm2 (10.5281/zenodo.21904324)", result["methods_text"])
        self.assertNotRegex(result["methods_text"], r"(?i)verified|validated|checked")
        # A private library with a recorded checksum is identified; it is not reported as lacking one.
        self.assertEqual([], result["warnings"])

    def test_a_laboratory_report_redacts_only_the_private_library(self) -> None:
        state = {key: value for key, value in self.unit["state"].items() if key != "repository_run_manifest"}
        result = generate_publication_report(state, None, self.unit["output"], app_version="0.5.14",
                                             console_version="5.5")
        audit = json.loads(Path(result["audit_file"]).read_text(encoding="utf-8"))

        self.assertNotIn("shared_path_policy", audit)
        self.assertEqual("MSMS-Private-pos-VS21.msp", audit["workflow"]["msp_annotators"][0]["msp_file_path"])
        self.assertEqual(PUBLIC, audit["workflow"]["lbm_path"])
        self.assertEqual(self.unit["state"]["files"][0]["file_path"], audit["workflow"]["files"][0]["file_path"])
        for where, text in _texts("bundle", Path(result["bundle"]).read_bytes()):
            for encoding in PRIVATE_ENCODINGS:
                self.assertNotIn(encoding.casefold(), text.casefold(), where)


class SyntheticUnitSec1Tests(unittest.TestCase):
    """The whole unit, redacted as a production run redacts it, read by the gate."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.unit = build_unit(Path(self.directory.name))
        finalise_console_run("job1", self.unit["preparation"], {"mztab": [str(self.unit["mztab"])]}, 0,
                             {"missing": []}, lambda _line: None)
        publish(self.unit)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_no_shared_artifact_carries_the_private_location_or_the_workspace_in_any_encoding(self) -> None:
        output = self.unit["output"]
        workspace = str(self.unit["workspace"])
        needles = [*PRIVATE_ENCODINGS, workspace, workspace.replace("\\", "/"), workspace.replace("\\", "\\\\"),
                   "F:\\tools", "F:/tools"]
        for name in (*SHARED_FILES, self.unit["mztab"].name):
            for where, text in _texts(name, (output / name).read_bytes()):
                for needle in needles:
                    with self.subTest(where=where, needle=needle):
                        self.assertNotIn(needle.casefold(), text.casefold())

    @unittest.skipUnless(GATE.is_file(), "the reanalysis gate (verify-run-invariants.py) is not on this machine")
    def test_the_gate_passes_sec1(self) -> None:
        gate = _load_gate()
        report = gate.Report(self.unit["workspace"])
        gate.check_no_private_path_in_a_shared_artifact(report, self.unit["output"], "before-publish")
        check = next(item for item in report.checks if item.check_id == "SEC-1")

        self.assertEqual(gate.PASS, check.status, check.detail)
        self.assertEqual("applied", check.evidence["private_library_rules"])
        self.assertIn({"name": "MSMS-Private-pos-VS21.msp", "distribution": "private"}, check.evidence["libraries"])
        self.assertTrue({"MS_DIAL_publication_report.json", "msdial-workflow-bundle.zip"} <= set(check.evidence["declared"]))

    @unittest.skipUnless(GATE.is_file(), "the reanalysis gate (verify-run-invariants.py) is not on this machine")
    def test_the_gate_still_fails_the_unredacted_console_mztab(self) -> None:
        # The same unit before this change: the Console's own mzTab-M, with its library URIs.
        (self.unit["output"] / "AlignResult-20269301200.mzTab").write_bytes(_mztab(self.unit["workspace"]).encode())
        gate = _load_gate()
        report = gate.Report(self.unit["workspace"])
        gate.check_no_private_path_in_a_shared_artifact(report, self.unit["output"], "before-publish")
        check = next(item for item in report.checks if item.check_id == "SEC-1")

        self.assertEqual(gate.FAIL, check.status, check.detail)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
