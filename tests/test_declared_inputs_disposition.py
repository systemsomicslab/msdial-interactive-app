"""A unit whose declared inputs are archives that hold no format MS-DIAL opens is excluded, not failed (0.5.40).

MB-POST MPST000213 (unit 334ab3e2f0e7600f4290): its three declared per-sample zips inside MPST000213.tar each held a
JEOL msAxel project (.xml, .pm, .dfa, .ba, .ppl ...), nothing MS-DIAL opens. Since 0.5.39 its lease says what they
held, and ends download_failed at the attribute stage; the campaign runner counted that as a failed download three
times and retried it, though every retry downloads the same objects into the same files.

THE CONTRACT WITH THE CAMPAIGN RUNNER (2026-10-15). When every missing declared input is a declared archive that the
extract stage expanded and that held nothing MS-DIAL opens, and no declared input is found more than once, the lease
raises DeclaredInputsUnsupportedError (a ValueError, with the same text), and the failed lease's download_failure
gains retryable false and disposition {decision "exclude", reason "declared_inputs_unsupported_format", inputs:
[{declared, archive, files, suffixes}]}. The unit manifest keeps it, the download job carries it verbatim
(download_failure, which the job's poll returns), and Interactive's campaign-authorized raw discard takes such a
unit. Any other combination - an input missing for another reason, an input found more than once - keeps the plain
failure, with no disposition.

AN EXCLUSION STANDS ONLY ON WHAT THE LEASE SAW (review of 0.5.40, 2026-10-15). It is never retried and deletes the
unit's raw data, so an archive holding a format MS-DIAL opens that the lease takes as no input (.ibf, .lrp, .imzML),
an archive the extract stage left packed, a record whose place is not on disk, and an archive that expanded to nothing
are each the plain failure; and a zip that expanded to a vendor folder is seen to hold one.

Everything here is synthetic: tars of zips built in memory, served by a stand-in client; nothing leaves the machine.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

_CONFIG = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"LOCALAPPDATA": _CONFIG.name}):
    from msdial_app import mcp_server, repository_reanalysis, server
    from msdial_app.agent_bridge import summarize_job
    from msdial_app.repository_reanalysis import (
        DeclaredInputsUnsupportedError,
        RepositoryFile,
        RepositoryProject,
        _filter_inputs_by_project_allowlist,
        download_exclusion,
        read_manifest,
    )

    from test_mzxml_conversion import dda_32
    from test_nested_archive_inputs import (
        ACCESSION,
        MZML,
        TAR_ROOT,
        TAR_URL,
        _Client,
        _handoff,
        _tar,
        _Workspace,
        _zip,
    )

# A JEOL msAxel project as MPST000213's zips hold one: no file of it is one MS-DIAL opens.
MSAXEL = {
    "Data/Data test/Adit_20260420_1/RD/MsData.RPS": b"rps",
    "Data/Data test/Adit_20260420_1/RD/MsData.RPC": b"rpc",
    "Data/Data test/Adit_20260420_1/MS/0/Profile.dfa": b"dfa",
    "Data/Data test/Adit_20260420_1/MS/1/Profile.dfa": b"dfa",
    "Settings/AccuTofLcMsMethod.xml": b"<xml/>",
}
UNIT = "nested-archive-unit"


def _msaxel(stem: str) -> bytes:
    """A per-sample zip holding exactly its own folder, as MPST000213's do (nested_rooted)."""
    return _zip({f"{stem}/{name}": data for name, data in MSAXEL.items()})


def _expected_input(stem: str) -> dict:
    return {
        "declared": f"{stem}.zip",
        "archive": f"{TAR_ROOT}/{stem}.zip",
        "files": 5,
        "suffixes": {".dfa": 2, ".rps": 1, ".rpc": 1, ".xml": 1},
    }


def _records_of(*names: str, file_count: int = 1) -> list[dict]:
    """The extraction record of MPST-NEST.tar, each named zip inside it expanded as one holding exactly its own folder
    (nested_rooted): its place is TAR_ROOT/<the zip's name less .zip>. Nothing is written to disk."""
    return [
        {
            "archive_name": f"{ACCESSION}.tar",
            "placement": "",
            "destination_rule": "archive_root",
            "nested": [
                {
                    "format": "zip", "file_count": file_count, "depth": 2, "archive_name": name,
                    "archive_path": f"{TAR_ROOT}/{name}", "destination_relative": TAR_ROOT,
                    "destination_rule": "nested_rooted", "container_stem": "", "container_root": "",
                    "nested": [], "nested_skipped": [],
                }
                for name in names
            ],
        }
    ]


def _declared_project(names: tuple[str, ...]) -> RepositoryProject:
    """A positive unit declaring each named zip as its sample's one input (kind file), as MPST000213's handoff does."""
    stems = [name[: -len(".zip")] for name in names]
    return RepositoryProject(
        repository="mb_post", accession=ACCESSION, analysis_unit_id=UNIT, eligible=True,
        selection_status="eligible", ion_mode="Positive",
        files=[RepositoryFile(name, 10, TAR_URL) for name in names],
        total_download_bytes=10,
        sample_metadata=[{"sample_id": stem, "raw_file": name} for name, stem in zip(names, stems)],
        analysis_inputs=[{"path": name, "kind": "file", "sample_id": stem} for name, stem in zip(names, stems)],
        analysis_inputs_declared=True,
    )


def _approval(path: Path, units: list[str]) -> Path:
    """A recorded campaign approval covering boundary 5 for the units, with a deleting retention."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "msdial-campaign-authorization.v1",
                "approval_id": "approval-0540",
                "campaign_id": "declared-pool",
                "manifest_digest": "sha256:" + "ab" * 32,
                "approved_by": "A. Person",
                "approved_at": "2026-10-15T10:00:00+09:00",
                "statement": "Run the listed units as planned.",
                "covers": [1, 3, 4, 5, "split"],
                "units": units,
                "raw_retention_policy": "delete_after_validated_output",
                "libraries": [{"name": "Synthetic-Private-Pos.msp", "sha256": "cd" * 32}],
                "revoked_at": None,
            }
        ),
        encoding="utf-8",
    )
    return path


class _Lease(_Workspace):
    def manifest_path(self) -> Path:
        return self.root / "mb_post" / ACCESSION / UNIT / "provenance" / "run-manifest.json"

    def failed(self, zips: dict[str, bytes], *, tar: bytes | None = None, **lease_options) -> tuple[Exception, dict]:
        """Lease the unit, which must fail; the error and the manifest it left. ``tar`` serves other bytes than the
        declared zips (a declared zip the download does not hold)."""
        typed, full = self.project(zips)
        with self.assertRaises(ValueError) as caught:
            repository_reanalysis.create_download_lease(
                typed, self.root, 10**9, client=_Client({TAR_URL: tar if tar is not None else full}), **lease_options
            )
        return caught.exception, read_manifest(self.manifest_path())


class EveryMissingInputHoldsNoFormatMsDialOpens(_Lease):
    """THE REAL CASE: three declared zips, each a JEOL msAxel project."""

    ZIPS = {f"Borneo_ID_0{index}.zip": _msaxel(f"Borneo_ID_0{index}") for index in (1, 2, 3)}

    def test_the_lease_raises_the_subclass_with_the_plain_failures_text(self) -> None:
        error, _manifest = self.failed(self.ZIPS)

        self.assertIsInstance(error, DeclaredInputsUnsupportedError)
        self.assertIn("3 of its 3 declared analysis inputs are not in the download", str(error))
        self.assertIn(f"3 of them were archives inside {ACCESSION}.tar that the extract stage expanded", str(error))
        self.assertTrue(str(error).endswith("Refusing to run it without them."))

    def test_the_failed_lease_records_an_exclusion_that_is_not_retried(self) -> None:
        _error, manifest = self.failed(self.ZIPS)
        failure = manifest["download_failure"]

        self.assertEqual("download_failed", manifest["status"])
        self.assertEqual(("attribute", "DeclaredInputsUnsupportedError"), (failure["stage"], failure["error_type"]))
        self.assertEqual((1, 1), (failure["objects_completed"], failure["objects_declared"]))
        self.assertIs(False, failure["retryable"])
        self.assertEqual(
            {
                "decision": "exclude",
                "reason": "declared_inputs_unsupported_format",
                "inputs": [_expected_input(f"Borneo_ID_0{index}") for index in (1, 2, 3)],
            },
            failure["disposition"],
        )
        self.assertEqual(failure["disposition"], download_exclusion(manifest))

    def test_the_suffixes_are_the_commonest_ten(self) -> None:
        many = {f"S1/file.x{index:02d}": b"x" * 1 for index in range(14)}
        many.update({"S1/a.dfa": b"a", "S1/b.dfa": b"b"})
        _error, manifest = self.failed({"S1.zip": _zip(many)})

        (entry,) = manifest["download_failure"]["disposition"]["inputs"]
        self.assertEqual(16, entry["files"])
        self.assertEqual(10, len(entry["suffixes"]))
        self.assertEqual((".dfa", 2), next(iter(entry["suffixes"].items())), "the commonest first")


class AnyOtherFailureKeepsThePlainFailure(_Lease):
    def test_an_input_missing_for_another_reason_gives_no_disposition(self) -> None:
        """S1.zip held nothing MS-DIAL opens, but S2.zip holds an mzXML, which a campaign converts and which outside
        one is no input: S2.zip is missing for another reason than an unsupported format, so the unit stays a
        failure."""
        error, manifest = self.failed({"S1.zip": _msaxel("S1"), "S2.zip": _zip({"S2/S2.mzXML": dda_32()})})

        self.assertIs(ValueError, type(error))
        self.assertIn("2 of its 2 declared analysis inputs are not in the download", str(error))
        failure = manifest["download_failure"]
        self.assertEqual(("attribute", "ValueError"), (failure["stage"], failure["error_type"]))
        self.assertNotIn("disposition", failure)
        self.assertNotIn("retryable", failure)
        self.assertIsNone(download_exclusion(manifest))

    def test_a_declared_archive_no_record_says_was_expanded_gives_no_disposition(self) -> None:
        """The attribute stage's own rule, on the records alone: S1.zip expanded (its nested record names its
        place) and holds a JEOL file only; S2.zip, declared too, has no record of having been expanded, so it is
        missing for another reason, and only where S1.zip is the one declared input is the unit one to exclude."""
        data_root = self.root / "data"
        place = data_root / TAR_ROOT / "S1" / "Data" / "RD"
        place.mkdir(parents=True)
        (place / "MsData.RPS").write_bytes(b"rps")
        records = _records_of("S1.zip")

        with self.assertRaises(ValueError) as both:
            _filter_inputs_by_project_allowlist(
                [], data_root, _declared_project(("S1.zip", "S2.zip")), archive_extractions=records
            )
        with self.assertRaises(DeclaredInputsUnsupportedError) as alone:
            _filter_inputs_by_project_allowlist([], data_root, _declared_project(("S1.zip",)), archive_extractions=records)

        self.assertIs(ValueError, type(both.exception))
        self.assertIn("2 of its 2 declared analysis inputs are not in the download", str(both.exception))
        self.assertIn("1 of them was an archive inside", str(both.exception))
        self.assertEqual(
            [{"declared": "S1.zip", "archive": f"{TAR_ROOT}/S1.zip", "files": 1, "suffixes": {".rps": 1}}],
            alone.exception.disposition()["inputs"],
        )

    def test_an_archive_holding_a_format_ms_dial_opens_gives_no_disposition(self) -> None:
        """REVIEW OF 0.5.40 (2026-10-15). The check asked only for the lease's RAW_SUFFIXES and mzXML, so a zip
        holding S1.ibf or S1.lrp - formats MS-DIAL opens (SupportFormat.cs; encoding_preference.VENDOR_SUFFIXES),
        which the lease's discovery takes as no input - was recorded as declared_inputs_unsupported_format: a false
        scientific record, never retried, and the raw data deleted under boundary 5. So were an imzML (imaging,
        outside the campaign, but a format MS-DIAL opens) and a zip inside it that the extract stage left packed,
        whose contents no one saw. Each is the plain failure, and its message names what lies there."""
        cases = (
            ({"S1/S1.ibf": b"ibf"}, ".ibf 1"),
            ({"S1/S1.lrp": b"lrp"}, ".lrp 1"),
            ({"S1/S1.imzML": b"<imzML/>", "S1/S1.ibd": b"ibd"}, ".imzml 1"),
            # Not an archive at all, though named as one: the extract stage leaves it packed (nested_skipped).
            ({"S1/inner.zip": b"not a zip at all"}, ".zip 1"),
            # Beside a JEOL project, one such file is enough.
            ({**{f"S1/{name}": data for name, data in MSAXEL.items()}, "S1/extra/S1.ibf": b"ibf"}, ".ibf 1"),
        )
        for index, (files, held) in enumerate(cases):
            self.fresh_root(f"case-{index}")
            with self.subTest(files=sorted(files)):
                error, manifest = self.failed({"S1.zip": _zip(files)})

                self.assertIs(ValueError, type(error))
                self.assertIn("1 of its 1 declared analysis inputs are not in the download: S1.zip", str(error))
                self.assertIn("none of the files it held is an input the lease takes", str(error))
                self.assertIn(
                    f"among them {held} of a format MS-DIAL opens or a campaign converts, or an archive left packed",
                    str(error),
                )
                failure = manifest["download_failure"]
                self.assertEqual(("attribute", "ValueError"), (failure["stage"], failure["error_type"]))
                self.assertNotIn("disposition", failure)
                self.assertNotIn("retryable", failure)
                self.assertIsNone(download_exclusion(manifest))

    def test_a_declared_archive_the_download_does_not_hold_is_refused_before_the_attribute_stage(self) -> None:
        """S2.zip is declared and not in the tar the repository served: the declared checksums' stage refuses the
        unit before the attribute stage, as it always did, with no disposition."""
        error, manifest = self.failed(
            {"S1.zip": _msaxel("S1"), "S2.zip": _zip({"S2/S2.mzML": MZML})}, tar=_tar({"S1.zip": _msaxel("S1")})
        )

        self.assertIs(ValueError, type(error))
        self.assertNotIn("disposition", manifest["download_failure"])
        self.assertIsNone(download_exclusion(manifest))

    def test_an_input_found_more_than_once_gives_no_disposition(self) -> None:
        zips = {"S1.zip": _msaxel("S1"), "S2.zip": _zip({"S2/run_1.mzML": MZML, "S2/run_2.mzML": MZML})}
        error, manifest = self.failed(zips)

        self.assertIs(ValueError, type(error))
        self.assertIn("1 of its 2 declared analysis inputs are not in the download: S1.zip", str(error))
        self.assertIn("found more than once: S2.zip (2 inputs in what the archive held)", str(error))
        self.assertNotIn("disposition", manifest["download_failure"])
        self.assertIsNone(download_exclusion(manifest))


class AnExclusionStandsOnlyOnWhatTheLeaseSaw(unittest.TestCase):
    """REVIEW OF 0.5.40 (2026-10-15): the attribute stage on the extraction records alone. An exclusion is never
    retried and deletes the unit's raw data, so _expanded_without_input gives a declared archive an entry only where
    one of its places is on disk, was walked (the place itself included), and held at least one file, none of a format
    MS-DIAL opens. Before the review a record whose place was not on disk gave an entry of the record's file_count and
    no suffix, and a zip that expanded to the vendor folder S1.d gave one, since the walk named only what lies inside
    the folder."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.data_root = Path(self._temporary.name).resolve() / "data"
        self.data_root.mkdir()

    def refused(self, name: str, **record_options) -> ValueError:
        with self.assertRaises(ValueError) as caught:
            _filter_inputs_by_project_allowlist(
                [], self.data_root, _declared_project((name,)), archive_extractions=_records_of(name, **record_options)
            )
        return caught.exception

    def test_a_record_whose_place_is_not_on_disk_gives_no_disposition(self) -> None:
        """The record names TAR_ROOT/S1, and nothing lies there: what the zip held was not seen, and a record at odds
        with the disk is the lease's own failure, never a format to exclude."""
        error = self.refused("S1.zip", file_count=7)

        self.assertIs(ValueError, type(error))
        self.assertIn(f"S1.zip: 7 file(s) under {TAR_ROOT}/S1 (not on disk)", str(error))
        self.assertIn("none of the files it held is an input the lease takes", str(error))

    def test_an_archive_that_expanded_to_nothing_gives_no_disposition(self) -> None:
        (self.data_root / TAR_ROOT / "S1").mkdir(parents=True)

        error = self.refused("S1.zip", file_count=0)

        self.assertIs(ValueError, type(error))
        self.assertIn(f"S1.zip: 0 file(s) under {TAR_ROOT}/S1", str(error))

    def test_a_zip_that_expanded_to_a_vendor_folder_gives_no_disposition(self) -> None:
        """S1.d.zip holding exactly its own folder expands to TAR_ROOT/S1.d (nested_rooted), whose walk names
        AcqData/MSScan.bin and not the folder itself: the place is walked with it."""
        acquisition = self.data_root / TAR_ROOT / "S1.d" / "AcqData"
        acquisition.mkdir(parents=True)
        (acquisition / "MSScan.bin").write_bytes(b"scan")

        error = self.refused("S1.d.zip")

        self.assertIs(ValueError, type(error))
        self.assertIn(f"S1.d.zip: 1 file(s) under {TAR_ROOT}/S1.d, by suffix .bin 1, among them .d 1", str(error))

    def test_a_place_on_disk_holding_files_ms_dial_does_not_open_is_still_an_exclusion(self) -> None:
        place = self.data_root / TAR_ROOT / "S1" / "Data" / "RD"
        place.mkdir(parents=True)
        (place / "MsData.RPS").write_bytes(b"rps")

        error = self.refused("S1.zip", file_count=7)

        self.assertIsInstance(error, DeclaredInputsUnsupportedError)
        self.assertIn("none of the files it held is one MS-DIAL opens", str(error))
        self.assertEqual(
            [{"declared": "S1.zip", "archive": f"{TAR_ROOT}/S1.zip", "files": 1, "suffixes": {".rps": 1}}],
            error.disposition()["inputs"],
            "files counts what was seen, not the record's file_count",
        )


class TheDownloadJobCarriesTheDisposition(_Lease):
    """The runner polls the download job; the job carries the lease's download_failure verbatim, so its poll says
    what the unit manifest says (and the runner, which reads the manifest when a job is lost, finds the same)."""

    def setUp(self) -> None:
        super().setUp()
        self.jobs: dict = {}
        for item in (
            patch.object(server, "JOBS", self.jobs),
            patch.object(server, "PROCESSES", {}),
            patch.object(server, "_persist_jobs_locked", lambda: None),
        ):
            item.start()
            self.addCleanup(item.stop)

    def run_job(self, job_id: str, zips: dict[str, bytes]) -> dict:
        typed, tar = self.project(zips)
        with server.JOBS_LOCK:
            self.jobs[job_id] = {"id": job_id, "status": "queued", "kind": "repository_download", "logs": []}
        with patch.object(repository_reanalysis, "RepositoryHttpClient", lambda: _Client({TAR_URL: tar})):
            server._run_repository_download_job(job_id, typed, self.root, 10**9, False, "keep")
        return self.jobs[job_id]

    def test_the_failed_job_carries_the_leases_record_and_its_poll_returns_it(self) -> None:
        job = self.run_job("dl-exclude", {"S1.zip": _msaxel("S1")})
        manifest = read_manifest(self.manifest_path())

        self.assertEqual("failed", job["status"])
        self.assertEqual(manifest["download_failure"], job["download_failure"])
        self.assertEqual("exclude", job["download_failure"]["disposition"]["decision"])
        self.assertIs(False, job["download_failure"]["retryable"])
        backend = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=backend.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{backend.server_port}/api/jobs/dl-exclude") as response:
                polled = json.loads(response.read().decode("utf-8"))
        finally:
            backend.shutdown()
            backend.server_close()
            thread.join(timeout=5)
        self.assertEqual(manifest["download_failure"], polled["download_failure"])

    def test_a_plain_failure_is_carried_without_a_disposition(self) -> None:
        job = self.run_job("dl-plain", {"S1.zip": _zip({"S1/run_1.mzML": MZML, "S1/run_2.mzML": MZML})})

        self.assertEqual("ValueError", job["download_failure"]["error_type"])
        self.assertNotIn("disposition", job["download_failure"])
        self.assertNotIn("disposition", summarize_job(job)["download_failure"])

    def test_a_job_whose_lease_wrote_no_record_has_none(self) -> None:
        self.assertNotIn("download_failure", summarize_job({"id": "x", "status": "failed", "error": "refused"}))


class TheCampaignDiscardTakesSuchAUnit(_Lease):
    """The runner ends the unit as excluded and its discarding state calls Interactive's campaign-authorized discard
    (msdial_discard_repository_raw, boundary 5), as it does for every exclusion. It already took a download_failed
    unit; this pins that it takes one whose failure is an exclusion, and says why the raw data went."""

    def test_the_approval_discards_the_raw_data_and_keeps_the_exclusion_on_record(self) -> None:
        self.failed(
            {"S1.zip": _msaxel("S1"), "S2.zip": _msaxel("S2")},
            raw_retention_policy="delete_after_validated_output",
        )
        manifest_path = self.manifest_path()
        raw = manifest_path.parents[1] / "raw"
        self.assertTrue(any(raw.rglob("MsData.RPS")), "the expanded zips are on disk before the discard")
        preview = mcp_server.msdial_discard_repository_raw(manifest_path=str(manifest_path))
        self.assertEqual(([], False), (preview["plan"]["blockers"], preview["deleted"]))

        result = mcp_server.msdial_discard_repository_raw(
            manifest_path=str(manifest_path),
            campaign_authorization_path=str(_approval(self.root / "campaign" / "approval.json", [UNIT])),
        )
        recorded = read_manifest(manifest_path)

        self.assertTrue(result["deleted"], result)
        self.assertFalse(raw.exists() and any(raw.rglob("*")), "the raw tree is gone")
        self.assertEqual("discarded", recorded["status"])
        self.assertIn("declared_inputs_unsupported_format", recorded["discard_reason"])
        self.assertEqual("exclude", recorded["download_failure"]["disposition"]["decision"])
        self.assertEqual([5], [item["boundary"] for item in recorded["campaign_authorizations"]])
        self.assertEqual("deleted", recorded["raw_deletion"]["state"])
        self.assertIsNone(download_exclusion(recorded), "a discarded unit is no longer a failed download")

    def test_a_unit_that_keeps_its_raw_data_is_not_discarded_by_the_campaign(self) -> None:
        self.failed({"S1.zip": _msaxel("S1")}, raw_retention_policy="keep")
        manifest_path = self.manifest_path()

        result = mcp_server.msdial_discard_repository_raw(
            manifest_path=str(manifest_path),
            campaign_authorization_path=str(_approval(self.root / "campaign" / "approval.json", [UNIT])),
        )

        self.assertIs(False, result.get("ok"), result)
        self.assertEqual("download_failed", read_manifest(manifest_path)["status"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
