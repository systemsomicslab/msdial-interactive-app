import csv
import hashlib
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

from msdial_app import __version__
from msdial_app.agent_workflow import build_guided_plan
from msdial_app.annotation_pipeline import (
    TIERED_LCMS_PROFILE_ID,
    apply_tiered_lcms_annotation,
    tiered_lcms_annotation,
)
from msdial_app.materials_methods import generate_publication_report
from msdial_app.sharing import library_records
from msdial_app.workflow import _write_msp_annotator_settings, file_identity, prepare_run


ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "resources" / "msdial_console_param4lipidomics.txt"


def method_lines(path: str | Path) -> dict[str, str]:
    lines: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if ":" in line and not line.startswith("#"):
            key, _, value = line.partition(":")
            lines[key.strip()] = value.strip()
    return lines


@contextmanager
def working_directory(path: Path) -> Iterator[None]:
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


@contextmanager
def held_unreadable(path: Path) -> Iterator[None]:
    """The file as another process holds it while it copies, or as an ACL that denies reading leaves it: it
    exists and has a size, but no read of it succeeds. On Windows a byte-range lock does that within this
    process; elsewhere the file loses its permissions, which a superuser still reads through."""
    if os.name == "nt":
        import msvcrt

        size = path.stat().st_size
        with path.open("r+b") as handle:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, size)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, size)
    else:
        mode = path.stat().st_mode
        path.chmod(0)
        try:
            yield
        finally:
            path.chmod(mode)


@contextmanager
def held_in_part(path: Path, offset: int, length: int) -> Iterator[None]:
    """The file with one byte range held by a byte-range lock: its first read succeeds and a read through it
    fails, as when a network share or another process fails a read partway. Only Windows locks a range so."""
    import msvcrt

    with path.open("r+b") as handle:
        handle.seek(offset)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, length)
        try:
            yield
        finally:
            handle.seek(offset)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, length)


def write_synthetic_lbm2(path: Path, size: int) -> str:
    """A synthetic .lbm2 of the given size, zero-filled after a short header; its sha256."""
    with path.open("wb") as handle:
        handle.write(b"synthetic laboratory LBM2 library")
        handle.truncate(size)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 22), b""):
            digest.update(block)
    return digest.hexdigest()


# lab_lcms_metabolomics.library_identity hashes a file below this itself and one of this size or more through
# workflow.file_identity, whose digest is cached and which answers a failed read with an empty sha256.
LARGE_LIBRARY_BYTES = (64 << 20) + (1 << 20)


class AnnotationPipelineTests(unittest.TestCase):
    def test_tiered_profile_has_auditable_priority_and_thresholds(self) -> None:
        profile = tiered_lcms_annotation("lipids.lbm2", "private.msp")

        self.assertEqual(TIERED_LCMS_PROFILE_ID, profile["profile_id"])
        self.assertEqual(3, profile["lbm_annotator"]["priority"])
        high, low = profile["msp_annotators"]
        self.assertEqual(("msp_high_quality", 2, 0.05, 0.8, 3), (
            high["annotator_id"], high["priority"], high["ms2_tolerance"],
            high["reverse_dot_product_cutoff"], high["minimum_spectrum_match"],
        ))
        self.assertEqual(("msp_low_quality", 1, 0.25, 0.5, 1), (
            low["annotator_id"], low["priority"], low["ms2_tolerance"],
            low["reverse_dot_product_cutoff"], low["minimum_spectrum_match"],
        ))
        self.assertTrue(all(row["target_omics"] == "Metabolomics" for row in (high, low)))
        self.assertTrue(all(row["matched_peaks_percentage_cutoff"] == 0 for row in (high, low)))

    def test_settings_tsv_preserves_mode_tier_and_reuses_one_msp_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            msp = root / "private.msp"
            msp.write_text("", encoding="ascii")
            state = {"project_type": "lcms", "lbm_annotator": {}}
            apply_tiered_lcms_annotation(state, str(root / "lipids.lbm2"), str(msp))

            path = _write_msp_annotator_settings(root, state)
            with path.open(encoding="ascii", newline="") as handle:
                rows = list(csv.DictReader(handle, delimiter="\t"))

            self.assertEqual(2, len(rows))
            self.assertEqual(rows[0]["msp_file_path"], rows[1]["msp_file_path"])
            self.assertEqual("Metabolomics", rows[0]["target_omics"])
            self.assertEqual("MSP low quality candidate", rows[1]["evidence_tier"])

    def test_agent_can_build_tiered_pipeline_for_metabolomics_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "sample.mzML").write_text("", encoding="ascii")
            console = root / "MSDIALCUI.exe"
            console.write_text("", encoding="ascii")
            msp = root / "private-neg-vs20.msp"
            msp.write_text("", encoding="ascii")
            lbm = root / "lipids.lbm2"
            lbm.write_text("", encoding="ascii")
            catalog = [{
                "id": "lipidomics",
                "kind": "lbm",
                "downloaded": True,
                "local_path": str(lbm),
                "record_id": 21904324,
                "record_url": "https://zenodo.org/records/21904324",
                "doi": "10.5281/zenodo.21904324",
                "license": "CC BY 4.0",
            }]

            with patch("msdial_app.agent_workflow.catalog_status", return_value=catalog):
                plan = build_guided_plan(
                    str(root),
                    {
                        "project_type": "lcms",
                        "ion_mode": "Negative",
                        "target_omics": "Metabolomics",
                        "parameter_strategy": "default",
                        "execute_rt_correction": False,
                        "library_strategy": "tiered_lipid_msp",
                        "libraries": {"msp_paths": [str(msp)], "msp_version": "VS20"},
                        "run_qa": False,
                        "generate_materials_methods": False,
                        "console_path": str(console),
                        "template_path": str(ROOT / "resources" / "msdial_console_param4lipidomics.txt"),
                        "class_assignment_confirmed": True,
                    },
                )

            self.assertTrue(plan["ready_to_prepare"], plan["blockers"])
            workflow = plan["workflow"]
            self.assertEqual(TIERED_LCMS_PROFILE_ID, workflow["annotation_pipeline_profile"])
            self.assertEqual(2, len(workflow["msp_annotators"]))
            self.assertTrue(workflow["selected_lipids"])
            self.assertEqual(3, workflow["lbm_annotator"]["priority"])
            # Without libraries.lbm_path, the official catalog LBM2 is the lipid-rule tier and its record.
            self.assertEqual("lipidomics", plan["official_library"]["id"])
            self.assertEqual(str(lbm), workflow["lbm_path"])
            self.assertEqual(str(lbm), workflow["lbm_annotator"]["lbm_file_path"])
            self.assertEqual(
                {
                    "path": str(lbm),
                    "version": "21904324",
                    "source": "https://zenodo.org/records/21904324",
                    "doi": "10.5281/zenodo.21904324",
                    "license": "CC BY 4.0",
                },
                workflow["library_provenance"][0],
            )
            self.assertEqual(str(msp), workflow["library_provenance"][1]["path"])


class GivenLbmPathTests(unittest.TestCase):
    """The tiered profile loads the LBM2 its answers name, and every record names the one it loads.

    Until 0.5.47 the profile loaded the official catalog LBM2 whatever libraries.lbm_path said, while
    the answers, and a campaign's records built from them, named the given one.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.data = self.root / "data"
        self.data.mkdir()
        (self.data / "sample.mzML").write_text("", encoding="ascii")
        self.libraries = self.root / "libraries"
        self.libraries.mkdir()
        self.console = self.root / "MSDIALCUI.exe"
        self.console.write_text("", encoding="ascii")
        self.msp = self.libraries / "synthetic-neg.msp"
        self.msp.write_text("NAME: A\n", encoding="ascii")
        self.lbm = self.libraries / "synthetic-lab-lipids.lbm2"
        self.lbm.write_bytes(b"synthetic laboratory LBM2 library")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def answers(self, **libraries) -> dict:
        return {
            "project_type": "lcms", "ion_mode": "Negative", "target_omics": "Lipidomics",
            "parameter_strategy": "default", "execute_rt_correction": False,
            "library_strategy": "tiered_lipid_msp",
            "libraries": {"lbm_path": str(self.lbm), "msp_paths": [str(self.msp)], "msp_version": "VS-test",
                          **libraries},
            "run_qa": False, "generate_materials_methods": False, "class_assignment_confirmed": True,
            "console_path": str(self.console), "template_path": str(TEMPLATE),
            "output_root": str(self.root / "out"),
        }

    def plan(self, answers: dict) -> dict:
        # The catalog is the official LBM2's; a plan with an LBM2 of its own never asks it.
        with patch("msdial_app.agent_workflow.catalog_status", side_effect=AssertionError("catalog consulted")):
            return build_guided_plan(str(self.data), answers)

    def test_the_given_lbm2_reaches_the_method_file_and_every_record_by_its_sha256(self) -> None:
        digest = hashlib.sha256(self.lbm.read_bytes()).hexdigest()
        plan = self.plan(self.answers(lbm_version="synthetic-1"))

        self.assertTrue(plan["ready_to_prepare"], plan["blockers"])
        self.assertIsNone(plan["official_library"])
        workflow = plan["workflow"]
        self.assertEqual(str(self.lbm), workflow["lbm_path"])
        self.assertEqual(str(self.lbm), workflow["lbm_annotator"]["lbm_file_path"])
        self.assertEqual(3, workflow["lbm_annotator"]["priority"])
        self.assertEqual(
            {
                "path": str(self.lbm), "name": self.lbm.name, "size_bytes": self.lbm.stat().st_size,
                "sha256": digest, "version": "synthetic-1", "source": "", "doi": "",
                "license": "institutional/private",
            },
            workflow["library_provenance"][0],
        )
        self.assertEqual([str(self.msp)] * 2, [row["msp_file_path"] for row in workflow["msp_annotators"]])

        prepared = prepare_run(workflow)
        self.assertEqual(str(self.lbm), method_lines(prepared["method_file"])["Lbm file path"])
        settings = json.loads(Path(prepared["settings_file"]).read_text(encoding="utf-8"))
        self.assertEqual(str(self.lbm), settings["lbm_path"])
        self.assertEqual(digest, settings["library_provenance"][0]["sha256"])
        manifest = json.loads(Path(prepared["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(
            [(str(self.lbm), digest)],
            [(item["path"], item["sha256"]) for item in manifest["libraries"] if item["path"].endswith(".lbm2")],
        )
        # The identities a repository unit's run manifest, its finalisation and its reports name libraries by.
        identities = [library.identity for library in library_records(workflow)]
        self.assertEqual(
            [(self.lbm.name, digest, "private")],
            [(item["name"], item["sha256"], item["distribution"]) for item in identities if item["role"] == "lbm"],
        )
        report = generate_publication_report(
            workflow, None, prepared["run_directory"], app_version=__version__, console_version="test",
        )
        self.assertIn("one LBM annotator", report["methods_text"])
        self.assertIn(f"{self.lbm.name} (SHA-256 {digest})", report["methods_text"])
        recorded = json.dumps([workflow, settings, manifest, identities]) + report["methods_text"]
        self.assertNotIn("zenodo", recorded.casefold())

    def test_an_lbm_path_the_console_could_not_load_is_a_blocker(self) -> None:
        missing = self.libraries / "missing.lbm2"
        empty = self.libraries / "empty.lbm2"
        empty.write_bytes(b"")
        other = self.libraries / "lipids.msp"
        other.write_text("NAME: A\n", encoding="ascii")
        folder = self.libraries / "folder.lbm2"
        folder.mkdir()
        for path, message in (
            (missing, f"LBM library not found: {missing}"),
            (empty, "libraries.lbm_path: empty.lbm2 is empty"),
            (other, "libraries.lbm_path: The tiered_lipid_msp profile reads an .lbm2 library; lipids.msp is not one."),
            (folder, "libraries.lbm_path: folder.lbm2 is a folder"),
        ):
            with self.subTest(path=path.name):
                plan = self.plan(self.answers(lbm_path=str(path)))
                self.assertFalse(plan["ready_to_prepare"])
                self.assertIsNone(plan["official_library"])
                self.assertTrue(any(message in item for item in plan["blockers"]), plan["blockers"])

    def test_a_relative_lbm_path_is_written_and_recorded_as_the_file_the_plan_hashed(self) -> None:
        # The plan checks and hashes a relative path against the backend's working directory. The Console
        # reads one against the method file's folder and then its own (ConfigParser.ResolvePathFromMethodFile),
        # and skips an LBM it does not find without a word; neither folder holds this one.
        digest = hashlib.sha256(self.lbm.read_bytes()).hexdigest()
        with working_directory(self.libraries):
            plan = self.plan(self.answers(lbm_path=self.lbm.name))
            self.assertTrue(plan["ready_to_prepare"], plan["blockers"])
            workflow = plan["workflow"]
            prepared = prepare_run(workflow)

        written = method_lines(prepared["method_file"])["Lbm file path"]
        self.assertTrue(Path(written).is_absolute(), written)
        self.assertTrue(Path(written).samefile(self.lbm))
        self.assertFalse((Path(prepared["method_file"]).parent / self.lbm.name).exists())
        self.assertFalse((self.console.parent / self.lbm.name).exists())
        provenance = workflow["library_provenance"][0]
        settings = json.loads(Path(prepared["settings_file"]).read_text(encoding="utf-8"))
        self.assertEqual(
            [written] * 4,
            [workflow["lbm_path"], workflow["lbm_annotator"]["lbm_file_path"], provenance["path"],
             settings["lbm_path"]],
        )
        self.assertEqual((self.lbm.name, digest), (provenance["name"], provenance["sha256"]))

    def test_a_relative_lbm_path_of_existing_libraries_reaches_the_method_file_absolute(self) -> None:
        # The generic strategy keeps the path as given in its state, as it keeps its MSP paths; the method
        # file names it absolute, as msp_annotator_settings.tsv names the MSP.
        answers = self.answers(lbm_path=self.lbm.name)
        answers.update({"library_strategy": "existing", "target_omics": "Metabolomics"})
        with working_directory(self.libraries):
            plan = self.plan(answers)
            self.assertTrue(plan["ready_to_prepare"], plan["blockers"])
            prepared = prepare_run(plan["workflow"])

        written = method_lines(prepared["method_file"])["Lbm file path"]
        self.assertTrue(Path(written).is_absolute(), written)
        self.assertTrue(Path(written).samefile(self.lbm))

    def test_an_lbm2_the_plan_cannot_read_is_a_blocker_not_an_exception(self) -> None:
        # The plan reads the given LBM2 in full for its sha256. Held by another process, or denied by an ACL,
        # it raised PermissionError out of build_guided_plan, so /api/agent/plan answered with a traceback.
        with held_unreadable(self.lbm):
            try:
                self.lbm.read_bytes()
            except OSError:
                pass
            else:
                self.skipTest("this account reads a file whatever its permissions")
            plan = self.plan(self.answers())

        self.assertFalse(plan["ready_to_prepare"])
        self.assertTrue(
            any(f"libraries.lbm_path: {self.lbm.name} cannot be read" in item for item in plan["blockers"]),
            plan["blockers"],
        )
        provenance = plan["workflow"]["library_provenance"][0]
        self.assertEqual(self.lbm.name, provenance["name"])
        self.assertNotIn("sha256", provenance)
        self.assertNotIn("size_bytes", provenance)

    def test_an_lbm2_that_cannot_be_hashed_is_a_blocker_and_recorded_by_name_only(self) -> None:
        # A read fails while it is hashed: the plan's check hashes it as the record does (_lbm_identity).
        with patch("msdial_app.lab_lipidomics.file_sha256", side_effect=PermissionError(13, "Permission denied")):
            plan = self.plan(self.answers())

        self.assertFalse(plan["ready_to_prepare"])
        self.assertIn(
            f"libraries.lbm_path: {self.lbm.name} cannot be read (Permission denied), so its sha256 cannot be "
            "recorded. Plan again once it can.",
            plan["blockers"],
        )
        provenance = plan["workflow"]["library_provenance"][0]
        self.assertEqual(self.lbm.name, provenance["name"])
        self.assertNotIn("sha256", provenance)
        self.assertNotIn("size_bytes", provenance)

    @unittest.skipUnless(os.name == "nt", "only Windows holds a byte range of a file")
    def test_an_lbm2_that_fails_partway_through_is_a_blocker_and_recorded_by_name_only_at_any_size(self) -> None:
        # A campaign LBM2 is hundreds of MB, which the plan hashed through workflow.file_identity: that answers a
        # failed read with an empty sha256, not an OSError, and the record carried it with a size.
        # The range held lies past the first read, so only a read through the file meets it. The cache is
        # empty here; test_a_large_lbm2_that_fails_partway_through_is_a_blocker_though_its_digest_is_cached
        # holds the same range with the digest cached.
        for size, label in ((2 << 20, "below 64 MiB"), (LARGE_LIBRARY_BYTES, "64 MiB or more")):
            with self.subTest(label), tempfile.TemporaryDirectory() as user_data:
                lbm = self.libraries / f"synthetic-{size}.lbm2"
                write_synthetic_lbm2(lbm, size)
                with patch("msdial_app.workflow.user_data_directory", return_value=Path(user_data)):
                    with held_in_part(lbm, 1 << 20, 1 << 10):
                        with lbm.open("rb") as handle:
                            self.assertTrue(handle.read(1))
                        plan = self.plan(self.answers(lbm_path=str(lbm)))

                self.assertFalse(plan["ready_to_prepare"])
                self.assertTrue(
                    any(f"libraries.lbm_path: {lbm.name} cannot be read (" in item for item in plan["blockers"]),
                    plan["blockers"],
                )
                provenance = plan["workflow"]["library_provenance"][0]
                self.assertEqual(lbm.name, provenance["name"])
                self.assertNotIn("sha256", provenance)
                self.assertNotIn("size_bytes", provenance)
                lbm.unlink()

    def test_a_large_lbm2_is_recorded_by_its_sha256_and_never_by_a_digest_cached_while_it_is_unreadable(self) -> None:
        # workflow.file_identity caches a large file's digest against its size and time and returns it without
        # reading the file, so a held LBM2 the plan blocked on was still recorded with a sha256.
        lbm = self.libraries / "synthetic-large.lbm2"
        digest = write_synthetic_lbm2(lbm, LARGE_LIBRARY_BYTES)
        with tempfile.TemporaryDirectory() as user_data:
            with patch("msdial_app.workflow.user_data_directory", return_value=Path(user_data)):
                readable = self.plan(self.answers(lbm_path=str(lbm)))
                # Cached as a laboratory run's manifest records it (workflow._manifest_libraries).
                self.assertEqual(digest, file_identity(lbm)["sha256"])
                self.assertIn(digest, (Path(user_data) / "file-identity-cache.json").read_text(encoding="utf-8"))
                with held_unreadable(lbm):
                    try:
                        with lbm.open("rb") as handle:
                            handle.read(1)
                    except OSError:
                        pass
                    else:
                        self.skipTest("this account reads a file whatever its permissions")
                    held = self.plan(self.answers(lbm_path=str(lbm)))

        self.assertTrue(readable["ready_to_prepare"], readable["blockers"])
        self.assertEqual(
            (lbm.name, LARGE_LIBRARY_BYTES, digest),
            tuple(readable["workflow"]["library_provenance"][0][key] for key in ("name", "size_bytes", "sha256")),
        )
        self.assertFalse(held["ready_to_prepare"])
        self.assertTrue(
            any(f"libraries.lbm_path: {lbm.name} cannot be read" in item for item in held["blockers"]),
            held["blockers"],
        )
        provenance = held["workflow"]["library_provenance"][0]
        self.assertEqual(lbm.name, provenance["name"])
        self.assertNotIn("sha256", provenance)
        self.assertNotIn("size_bytes", provenance)

    @unittest.skipUnless(os.name == "nt", "only Windows holds a byte range of a file")
    def test_a_large_lbm2_that_fails_partway_through_is_a_blocker_though_its_digest_is_cached(self) -> None:
        # The plan read the first byte and then took file_identity's digest, which the cache answers without
        # reading the file. In a campaign every unit names the same LBM2, so once its digest was cached, one
        # failing past its first byte planned as ready and was recorded by its sha256.
        lbm = self.libraries / "synthetic-large.lbm2"
        digest = write_synthetic_lbm2(lbm, LARGE_LIBRARY_BYTES)
        with tempfile.TemporaryDirectory() as user_data:
            with patch("msdial_app.workflow.user_data_directory", return_value=Path(user_data)):
                self.assertEqual(digest, file_identity(lbm)["sha256"])
                with held_in_part(lbm, 1 << 20, 1 << 10):
                    with lbm.open("rb") as handle:
                        self.assertTrue(handle.read(1))
                    with self.assertRaises(OSError):
                        lbm.read_bytes()
                    # The cache still answers for the file, though it no longer reads through.
                    self.assertEqual(digest, file_identity(lbm)["sha256"])
                    plan = self.plan(self.answers(lbm_path=str(lbm)))

        self.assertFalse(plan["ready_to_prepare"])
        self.assertTrue(
            any(f"libraries.lbm_path: {lbm.name} cannot be read (" in item for item in plan["blockers"]),
            plan["blockers"],
        )
        provenance = plan["workflow"]["library_provenance"][0]
        self.assertEqual(lbm.name, provenance["name"])
        self.assertNotIn("sha256", provenance)
        self.assertNotIn("size_bytes", provenance)

    def test_a_large_lbm2_is_recorded_by_the_sha256_the_plan_reads_not_the_one_cached_for_it(self) -> None:
        # A digest cached for content the file no longer holds, at the same size and time, shows which one the
        # record takes: the plan reads the LBM2 through and records what it read.
        lbm = self.libraries / "synthetic-large.lbm2"
        cached = write_synthetic_lbm2(lbm, LARGE_LIBRARY_BYTES)
        with tempfile.TemporaryDirectory() as user_data:
            with patch("msdial_app.workflow.user_data_directory", return_value=Path(user_data)):
                self.assertEqual(cached, file_identity(lbm)["sha256"])
                stat = lbm.stat()
                with lbm.open("r+b") as handle:
                    handle.write(b"rewritten")
                os.utime(lbm, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                self.assertEqual(cached, file_identity(lbm)["sha256"])
                plan = self.plan(self.answers(lbm_path=str(lbm)))

        digest = hashlib.sha256(lbm.read_bytes()).hexdigest()
        self.assertNotEqual(cached, digest)
        self.assertTrue(plan["ready_to_prepare"], plan["blockers"])
        self.assertEqual(
            (LARGE_LIBRARY_BYTES, digest),
            tuple(plan["workflow"]["library_provenance"][0][key] for key in ("size_bytes", "sha256")),
        )

    def test_a_workflow_override_of_a_library_is_refused_rather_than_run_unchecked(self) -> None:
        # workflow_overrides reach the state after the profile, so an lbm_path there was the LBM2 method.txt
        # named, unchecked, while lbm_annotator, library_provenance and every record named the given one.
        other = self.libraries / "synthetic-other.lbm2"
        other.write_bytes(b"another synthetic LBM2 library")
        for key, value in (
            ("lbm_path", str(other)),
            ("lbm_annotator", {"lbm_file_path": str(other), "priority": 3}),
            ("msp_path", str(self.msp)),
            ("msp_annotators", []),
            ("text_db_path", str(self.msp)),
            ("text_annotators", []),
            ("library_provenance", []),
            ("annotation_pipeline_profile", "another-profile"),
        ):
            with self.subTest(key):
                answers = self.answers()
                answers["workflow_overrides"] = {key: value, "number_of_threads": 2}
                plan = self.plan(answers)

                self.assertFalse(plan["ready_to_prepare"])
                self.assertEqual(
                    [
                        f"workflow_overrides.{key} would replace what the tiered_lipid_msp profile sets from "
                        "libraries, after the plan has checked it, so the run could load a library its records do "
                        "not name. Give the libraries in libraries."
                    ],
                    [item for item in plan["blockers"] if item.startswith("workflow_overrides.")],
                )

        answers = self.answers()
        answers["workflow_overrides"] = {"number_of_threads": 2}
        plan = self.plan(answers)
        self.assertTrue(plan["ready_to_prepare"], plan["blockers"])
        self.assertEqual(2, plan["workflow"]["number_of_threads"])

    def test_a_second_msp_path_is_refused_rather_than_left_out(self) -> None:
        second = self.libraries / "second.msp"
        second.write_text("NAME: B\n", encoding="ascii")
        plan = self.plan(self.answers(msp_paths=[str(self.msp), str(second)]))

        self.assertFalse(plan["ready_to_prepare"])
        self.assertTrue(
            any("libraries.msp_paths names 2" in item for item in plan["blockers"]), plan["blockers"]
        )

    def test_a_text_library_is_refused_rather_than_left_out(self) -> None:
        # The profile has no text tier: a text DB given beside it reached neither method.txt nor any record but
        # the answers.
        text = self.libraries / "synthetic-standards.txt"
        text.write_text("Name\tMZ\nA\t100.0\n", encoding="ascii")
        plan = self.plan(self.answers(text_paths=[str(text)]))

        self.assertFalse(plan["ready_to_prepare"])
        self.assertIn(
            "tiered_lipid_msp searches no text library; libraries.text_paths names 1. Leave it out, or search it "
            "with library_strategy 'existing'.",
            plan["blockers"],
        )
        self.assertEqual([], plan["workflow"]["text_annotators"])

    def test_without_an_lbm_path_the_official_lbm2_is_still_asked_for(self) -> None:
        answers = self.answers()
        del answers["libraries"]["lbm_path"]
        missing = [{
            "id": "lipidomics", "kind": "lbm", "downloaded": False, "local_path": "", "record_id": 21904324,
            "record_url": "https://zenodo.org/records/21904324", "doi": "10.5281/zenodo.21904324",
            "license": "CC BY 4.0",
        }]
        with patch("msdial_app.agent_workflow.catalog_status", return_value=missing):
            plan = build_guided_plan(str(self.data), answers)
            unanswered = build_guided_plan(str(self.data), {**answers, "libraries": {}})

        self.assertFalse(plan["ready_to_prepare"])
        self.assertEqual("lipidomics", plan["official_library"]["id"])
        self.assertIn(
            "The official lipidomics LBM library is not downloaded. Ask for confirmation, then download it.",
            plan["blockers"],
        )
        question = next(item for item in unanswered["remaining_questions"] if item["id"] == "libraries")
        self.assertIn("libraries.lbm_path", question["prompt"])
        self.assertNotIn("The official LBM library is used as tier 1", question["prompt"])


if __name__ == "__main__":
    unittest.main()
