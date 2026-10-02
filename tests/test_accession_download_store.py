"""The download lease through the accession download store: one fetch per object, one claim per unit.

download_store.py (0.5.13) could fetch an object once and link it into every unit, but no lease used it:
every unit downloaded its own copy of every object it lists, and the three units of ST001408 would each
have moved its 928 GB archive. These tests hold the lease, the deletions and the batch plan to the store:

- two units sharing a URL make one GET, and both units' files are links to the store's one file record;
- a unit's release of its raw tree releases its claims, and the store's collection, under the campaign
  approval that covered the deletion, deletes an object only when no live claim holds it: another unit's
  pending pre-claim keeps it, and a campaign that keeps raw data never deletes;
- a split parent's claims stand for its parts, and go only with the parent's own release;
- a Workbench study archive shared by a positive and a negative unit is fetched once and extracted once,
  and each unit's tree keeps only its own samples - with what the SCIEX reader opens beside each;
- a release asked for again keeps the record of the first, and finishes one a failure left unmade, a
  cleanup's included; a deletion's preview says which of its bytes the store keeps;
- a lease waiting for another lease's transfer says so (waiting_for_shared_download), and is heard;
- _dl and _campaigns are never units, and without a campaign or store_mode "always" nothing changes.

The batch plan, the download preview and the store status are test_download_store_planning's.

The network is a local HTTP server or an injected client. Every path and approval is synthetic.
"""

from __future__ import annotations

import hashlib
import http.server
import io
import json
import os
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

_CONFIG = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"LOCALAPPDATA": _CONFIG.name}):
    from msdial_app import mcp_server, repository_reanalysis, server
    from msdial_app.archives import ExtractionLimits
    from msdial_app.campaign_authorization import CampaignAuthorizationError, authorize
    from msdial_app.download_store import DownloadStore
    from msdial_app.repository_analysis_rows import _travelling_files
    from msdial_app.repository_reanalysis import (
        LEASE_STAGES,
        RepositoryFile,
        RepositoryHttpClient,
        RepositoryProject,
        cleanup_download_lease,
        cleanup_split_parent,
        create_download_lease,
        discard_download_lease,
        finalize_download_lease,
        plan_download_cleanup,
        pre_claim_downloads,
        project_from_dict,
        read_manifest,
        record_run_failure,
        run_raw_metadata_preflight,
        split_unit_by_acquisition,
        travels_with_sciex_file,
        unit_download_objects,
    )
    from msdial_app.user_settings import save_download_store_mode

from test_repository_reanalysis import _MixedUnitFixture

DELETE = "delete_after_validated_output"
VALID_MZTAB = "MTD\tmzTab-version\t2.0.0-M\nSMH\tSML_ID\nSML\t1\n"
# The default reserve is 20 GB of free space; these trees are a few kilobytes.
LIMITS = ExtractionLimits(reserve_bytes=0)
BASE = "https://repository.example.org/MTBLS-SHARED/FILES/"


def _mzml(tag: str) -> bytes:
    return f"<mzML>{tag}</mzML>".encode("ascii")


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        for name, data in entries.items():
            handle.writestr(name, data)
    return buffer.getvalue()


class _Client:
    """Stands in for the network: fixed bytes per URL, counted, and written where the store asks."""

    def __init__(self, payloads: dict[str, bytes], gate: threading.Event | None = None) -> None:
        self.payloads = payloads
        self.gate = gate
        self.started = threading.Event()
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def download(self, url, destination, _maximum_bytes, progress_callback=None):
        with self._lock:
            self.calls.append(url)
        self.started.set()
        if self.gate is not None and not self.gate.wait(30):
            raise TimeoutError("the test gate was never opened")
        data = self.payloads[url]
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        if progress_callback:
            progress_callback(len(data), len(data))
        return {
            "path": str(destination),
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "md5": hashlib.md5(data).hexdigest(),
            "resumed_from_bytes": 0,
            "attempts": [{"attempt": 1, "outcome": "completed"}],
        }

    def content_length(self, url) -> int:
        return len(self.payloads[url])


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # noqa: D102 - quiet
        pass

    def _answer(self, body: bool) -> None:
        data = self.server.objects.get(self.path)
        if data is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if body:
            self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - http.server's interface
        self.server.gets.append(self.path)
        self._answer(True)

    def do_HEAD(self) -> None:  # noqa: N802
        self._answer(False)


class _Server:
    """A repository on 127.0.0.1 that counts its GETs, reached through the real RepositoryHttpClient."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.objects = objects
        self.httpd.gets = []
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "_Server":
        self.thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def gets(self) -> list[str]:
        return self.httpd.gets

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}{path}"


def _project(unit: str, files: dict[str, str], *, accession: str = "MTBLS-SHARED", **fields) -> RepositoryProject:
    """A MetaboLights unit whose files are listed one by one: {listed name: URL}, each sample naming one."""
    listed = [RepositoryFile(name=name, size_bytes=10, url=url) for name, url in files.items()]
    return RepositoryProject(
        repository="metabolights",
        accession=accession,
        analysis_unit_id=unit,
        separation="LC-MS",
        acquisition_mode="DDA",
        ion_mode="Negative",
        untargeted=True,
        eligible=True,
        selection_status="eligible",
        files=listed,
        total_download_bytes=10 * len(listed),
        sample_metadata=[
            {"sample_id": Path(name).stem, "raw_file": name, "values": {}} for name in files
        ],
        **fields,
    )


class _Workspace(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.base = Path(self._temporary.name)
        self.root = self.base / "analysis"
        self.root.mkdir()
        limits = patch.object(repository_reanalysis, "LEASE_EXTRACTION_LIMITS", LIMITS)
        limits.start()
        self.addCleanup(limits.stop)
        # The saved settings of the person running the tests are not these tests' settings.
        settings = patch.dict(os.environ, {"LOCALAPPDATA": str(self.base / "config"), "APPDATA": str(self.base / "config")})
        settings.start()
        self.addCleanup(settings.stop)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def approval(self, units: list[str], retention: str = DELETE, name: str = "approval.json") -> Path:
        path = self.base / "campaign" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema": "msdial-campaign-authorization.v1",
                    "approval_id": f"approval-{name}",
                    "campaign_id": "synthetic-campaign",
                    "manifest_digest": "sha256:" + "ab" * 32,
                    "approved_by": "A. Person",
                    "approved_at": "2026-10-02T09:00:00+09:00",
                    "statement": "Run the listed units and delete their raw data as agreed.",
                    "covers": [1, 3, 4, 5, "split"],
                    "units": units,
                    "raw_retention_policy": retention,
                    "libraries": [{"name": "Synthetic.msp", "sha256": "cd" * 32}],
                    "revoked_at": None,
                }
            ),
            encoding="utf-8",
        )
        return path

    def campaign_lease(self, project: RepositoryProject, client, approval: Path, retention: str = DELETE) -> dict:
        crossing = authorize(
            approval, project.analysis_unit_id, 1, entry_point="test", raw_retention_policy=retention
        )
        return create_download_lease(
            project, self.root, 10**9, client=client, raw_retention_policy=retention, campaign_authorization=crossing
        )

    def validate(self, manifest_path: Path) -> None:
        output = Path(read_manifest(manifest_path)["output_directory"])
        (output / "AlignResult-2026100210.mzTab").write_text(VALID_MZTAB, encoding="ascii")
        self.assertEqual("mztab_validated", finalize_download_lease(manifest_path)["status"])

    def store(self, accession: str = "MTBLS-SHARED", repository: str = "metabolights") -> DownloadStore:
        return DownloadStore(self.root, repository, accession)


def _download(lease: dict, name: str) -> dict:
    return next(item for item in lease["downloads"] if Path(item["source_url"]).name == name)


def _stage(lease: dict, name: str) -> dict:
    return next(entry for entry in lease["lease_stages"] if entry["stage"] == name)


class TwoUnitsSharingAUrlMakeOneGet(_Workspace):
    def test_one_get_and_one_file_record_for_both_units(self) -> None:
        client = _Client({BASE + "shared.mzML": _mzml("shared"), BASE + "a.mzML": _mzml("a"), BASE + "b.mzML": _mzml("b")})
        first = create_download_lease(
            _project("unit-a", {"FILES/shared.mzML": BASE + "shared.mzML", "FILES/a.mzML": BASE + "a.mzML"}),
            self.root, 10**9, client=client, store_mode="always",
        )
        second = create_download_lease(
            _project("unit-b", {"FILES/shared.mzML": BASE + "shared.mzML", "FILES/b.mzML": BASE + "b.mzML"}),
            self.root, 10**9, client=client, store_mode="always",
        )

        self.assertEqual(1, client.calls.count(BASE + "shared.mzML"), "the second unit reuses the store's copy")
        links = [Path(lease["input_directory"]) / "shared.mzML" for lease in (first, second)]
        store_file = Path(_download(first, "shared.mzML")["cache_object_path"])
        inodes = {os.stat(path).st_ino for path in (*links, store_file)}
        self.assertEqual(1, len(inodes), "both units link the store's one file record")
        self.assertGreaterEqual(os.stat(store_file).st_nlink, 3)
        self.assertTrue(store_file.is_relative_to(self.root.resolve() / "metabolights" / "MTBLS-SHARED" / "_dl"))
        # Each downloads[] entry says where its bytes are, who fetched them and what vouched for them.
        mine, inherited = _download(first, "shared.mzML"), _download(second, "shared.mzML")
        self.assertEqual("fetched_by_this_unit", mine["sha256_origin"])
        self.assertEqual("inherited_from_cache", inherited["sha256_origin"])
        self.assertEqual(mine["cache_object_path"], inherited["cache_object_path"])
        self.assertEqual(str(links[1]), inherited["path"], "a per-file object's path is the unit's own link")
        self.assertIsNone(inherited["declared_checksum_verified"], "MetaboLights publishes no checksum")
        self.assertEqual(mine["sha256"], inherited["sha256"])
        self.assertEqual({str(path) for path in links[1:]} | {str(Path(second["input_directory"]) / "b.mzML")},
                         {str(Path(item)) for item in second["input_candidates"]})
        cache = second["download_cache"]
        self.assertEqual((1, 1, "store_mode"), (cache["cache_hits"], cache["objects_fetched"], cache["activated_by"]))
        self.assertEqual("hardlink", second["raw_storage"]["materialization"])
        self.assertEqual("completed", _stage(second, "materialise")["status"])
        # The lineage and the manifest on disk read the same, and the claims say who holds what.
        recorded = read_manifest(second["manifest_path"])
        self.assertEqual(second["download_cache"], recorded["download_cache"])
        claims = {claim["unit_id"]: claim["state"] for claim in self.store().all_claims()
                  if claim["url"] == BASE + "shared.mzML"}
        self.assertEqual({"unit-a": "materialized", "unit-b": "materialized"}, claims)

    def test_what_a_unit_writes_beside_its_link_never_reaches_the_store(self) -> None:
        client = _Client({BASE + "shared.mzML": _mzml("shared")})
        lease = create_download_lease(
            _project("unit-a", {"FILES/shared.mzML": BASE + "shared.mzML"}), self.root, 10**9, client=client,
            store_mode="always",
        )
        # A stand-in for the .dcl MS-DIAL writes beside the file it reads.
        (Path(lease["input_directory"]) / "shared_2026100210.dcl").write_bytes(b"intermediate")

        store_files = [path.name for path in self.store().root.rglob("*") if path.is_file()]
        self.assertNotIn("shared_2026100210.dcl", store_files)


class APendingClaimKeepsTheObjectThroughAnotherUnitsCleanup(_Workspace):
    def test_unit_b_pending_claim_keeps_the_object_and_its_own_release_collects_it(self) -> None:
        approval = self.approval(["unit-a", "unit-b"])
        client = _Client({BASE + "shared.mzML": _mzml("shared"), BASE + "a.mzML": _mzml("a"), BASE + "b.mzML": _mzml("b")})
        unit_b = {"FILES/shared.mzML": BASE + "shared.mzML", "FILES/b.mzML": BASE + "b.mzML"}
        # Pre-claimed at batch approval, before unit B's lease.
        pre_claim_downloads(self.root, [{
            "analysis_unit_id": "unit-b", "repository": "metabolights", "accession": "MTBLS-SHARED",
            "objects": unit_download_objects(_project("unit-b", unit_b).as_dict()),
        }])
        first = self.campaign_lease(
            _project("unit-a", {"FILES/shared.mzML": BASE + "shared.mzML", "FILES/a.mzML": BASE + "a.mzML"}),
            client, approval,
        )
        shared_object = _download(first, "shared.mzML")["cache_object_id"]
        own_object = _download(first, "a.mzML")["cache_object_id"]
        self.validate(Path(first["manifest_path"]))

        preview = plan_download_cleanup(Path(first["manifest_path"]))
        kept_for = {item["object_id"]: item["kept_for_units"] for item in preview["download_store"]["objects"]}
        self.assertEqual({shared_object: ["unit-b"], own_object: []}, kept_for)
        cleaned = cleanup_download_lease(Path(first["manifest_path"]), campaign_authorization_path=approval)

        self.assertTrue(cleaned["deleted"], cleaned.get("blockers"))
        gc = cleaned["download_store"]["gc"]
        self.assertTrue(gc["authorized"])
        self.assertEqual([{"object_id": shared_object, "live_claims": ["unit-b"]}], gc["kept"])
        self.assertEqual([own_object], [item["object_id"] for item in gc["collected"]])
        store = self.store()
        self.assertTrue((store.object_directory(shared_object) / "obj" / "shared.mzML").is_file())
        self.assertFalse((store.object_directory(own_object) / "obj").exists(), "no one else claimed it")
        self.assertEqual("collected", store.entry(own_object)["state"], "a tombstone stays")
        self.assertEqual({"released"}, {claim["state"] for claim in store.claims_for_unit("unit-a")})
        self.assertEqual("raw_cleaned", read_manifest(Path(first["manifest_path"]))["download_store_release"]["reason"])

        second = self.campaign_lease(_project("unit-b", unit_b), client, approval)
        self.assertEqual(1, client.calls.count(BASE + "shared.mzML"), "unit B reused what its claim kept")
        self.assertEqual("inherited_from_cache", _download(second, "shared.mzML")["sha256_origin"])

        self.validate(Path(second["manifest_path"]))
        last = cleanup_download_lease(Path(second["manifest_path"]), campaign_authorization_path=approval)
        self.assertIn(shared_object, [item["object_id"] for item in last["download_store"]["gc"]["collected"]])
        self.assertFalse((store.object_directory(shared_object) / "obj").exists())

    def test_a_discard_releases_with_its_reason_and_a_failed_unit_keeps_its_claim_until_then(self) -> None:
        approval = self.approval(["unit-a", "unit-b"])
        client = _Client({BASE + "shared.mzML": _mzml("shared")})
        lease = self.campaign_lease(_project("unit-a", {"FILES/shared.mzML": BASE + "shared.mzML"}), client, approval)
        manifest = Path(lease["manifest_path"])
        repository_reanalysis.record_run_failure(manifest, {"reason": "MS-DIAL Console exited with code 1.", "exit_code": 1})

        self.assertEqual({"materialized"}, {claim["state"] for claim in self.store().claims_for_unit("unit-a")},
                         "a failed run keeps its claim: the retries need the tree")
        discarded = discard_download_lease(manifest, campaign_authorization_path=approval)

        self.assertTrue(discarded["deleted"], discarded.get("blockers"))
        self.assertEqual("failed_terminal", discarded["download_store"]["reason"])
        self.assertEqual(1, len(discarded["download_store"]["gc"]["collected"]))
        # Asked again, the discard says it was made, and the release is made no further.
        again = discard_download_lease(manifest, campaign_authorization_path=approval)
        self.assertTrue(again["already_discarded"])
        self.assertEqual([], again["download_store"]["gc"]["collected"])

    def test_a_failed_transfer_keeps_its_partial_for_a_retry_and_goes_with_the_discard(self) -> None:
        approval = self.approval(["unit-a"])

        class Dropped(_Client):
            def download(self, url, destination, _maximum_bytes, progress_callback=None):
                self.calls.append(url)
                Path(destination).with_name(Path(destination).name + ".part").write_bytes(b"half")
                raise ConnectionResetError("the server hung up")

        with self.assertRaises(ConnectionResetError):
            self.campaign_lease(
                _project("unit-a", {"FILES/shared.mzML": BASE + "shared.mzML"}), Dropped({}), approval
            )
        store = self.store()
        manifest = self.root / "metabolights" / "MTBLS-SHARED" / "unit-a" / "provenance" / "run-manifest.json"
        partials = [path.name for path in (store.root / "partial").iterdir() if path.name.endswith(".part")]
        claim = store.read_claim(BASE + "shared.mzML", "unit-a")

        self.assertEqual(1, len(partials), "the bytes that arrived stay for the resume")
        self.assertEqual("pending", claim["state"])
        self.assertIn("ConnectionResetError", claim["last_error"]["message"])
        self.assertEqual("download_failed", read_manifest(manifest)["status"])
        discarded = discard_download_lease(manifest, campaign_authorization_path=approval)

        self.assertTrue(discarded["deleted"], discarded.get("blockers"))
        self.assertEqual("discarded", discarded["download_store"]["reason"])
        self.assertEqual(2, discarded["download_store"]["gc"]["partials_removed"], "the .part and its record")
        self.assertEqual([], [path.name for path in (store.root / "partial").iterdir() if path.name.endswith(".part")])


class RetentionKeepNeverDeletes(_Workspace):
    def test_a_campaign_that_keeps_raw_data_never_collects_a_store_object(self) -> None:
        keep = self.approval(["unit-a", "unit-b"], retention="keep")
        client = _Client({BASE + "shared.mzML": _mzml("shared")})
        leases = [
            self.campaign_lease(_project(unit, {"FILES/shared.mzML": BASE + "shared.mzML"}), client, keep, retention="keep")
            for unit in ("unit-a", "unit-b")
        ]
        object_id = _download(leases[0], "shared.mzML")["cache_object_id"]
        for lease in leases:
            self.validate(Path(lease["manifest_path"]))
            with self.assertRaises(CampaignAuthorizationError):
                cleanup_download_lease(Path(lease["manifest_path"]), campaign_authorization_path=keep)
            # A person's confirmation deletes the unit's tree and releases its claims; it is no store collection.
            cleaned = cleanup_download_lease(Path(lease["manifest_path"]), confirmed=True)
            self.assertTrue(cleaned["deleted"])
            self.assertFalse(cleaned["download_store"]["gc"]["authorized"])

        store = self.store()
        swept = store.gc(keep)
        self.assertFalse(swept["authorized"])
        self.assertIn("retention_keep", swept["refusal_codes"])
        self.assertEqual([], swept["collected"])
        self.assertTrue((store.object_directory(object_id) / "obj" / "shared.mzML").is_file())
        self.assertEqual({"released"}, {claim["state"] for claim in store.all_claims()})


class ARepeatedReleaseKeepsTheRecordOfTheFirst(_Workspace):
    """A repeated discard or release wrote a new download_store_release over the first: a person's confirmed
    repeat after a campaign's discard replaced the record of the store's collection with gc unauthorized."""

    def test_a_confirmed_repeat_of_a_campaign_discard_is_noted_and_replaces_nothing(self) -> None:
        approval = self.approval(["unit-a"])
        client = _Client({BASE + "shared.mzML": _mzml("shared")})
        lease = self.campaign_lease(_project("unit-a", {"FILES/shared.mzML": BASE + "shared.mzML"}), client, approval)
        manifest = Path(lease["manifest_path"])
        record_run_failure(manifest, {"reason": "MS-DIAL Console exited with code 1.", "exit_code": 1})
        discard_download_lease(manifest, campaign_authorization_path=approval)
        first = read_manifest(manifest)["download_store_release"]
        self.assertTrue(first["gc"]["authorized"])
        self.assertEqual(1, len(first["gc"]["collected"]))

        again = discard_download_lease(manifest, confirmed=True)

        self.assertTrue(again["already_discarded"])
        self.assertEqual("repeat", again["download_store"]["recorded_as"])
        self.assertFalse(again["download_store"]["gc"]["authorized"], "what the repeat itself did")
        after = read_manifest(manifest)["download_store_release"]
        self.assertEqual(first, {key: value for key, value in after.items() if key != "repeats"},
                         "the first release's record, its collection included, stands")
        self.assertEqual([{"at": again["download_store"]["released_at"], "reason": "failed_terminal",
                           "gc": {"authorized": False}}], after["repeats"])

    def test_a_later_release_that_collects_replaces_the_record_and_keeps_the_earlier(self) -> None:
        # store_mode "always" outside a campaign: a person's confirmation releases the claim and collects
        # nothing; a cleanup repeated under an approval that covers the unit collects what it left.
        client = _Client({BASE + "a.mzML": _mzml("a")})
        lease = create_download_lease(_project("unit-gui", {"FILES/a.mzML": BASE + "a.mzML"}), self.root, 10**9,
                                      client=client, store_mode="always", raw_retention_policy=DELETE)
        manifest = Path(lease["manifest_path"])
        object_id = _download(lease, "a.mzML")["cache_object_id"]
        self.validate(manifest)
        cleaned = cleanup_download_lease(manifest, confirmed=True)
        self.assertTrue(cleaned["deleted"])
        self.assertFalse(cleaned["download_store"]["gc"]["authorized"])
        store = self.store()
        self.assertTrue((store.object_directory(object_id) / "obj").is_dir(), "a confirmation deletes no store object")

        collected = cleanup_download_lease(manifest, campaign_authorization_path=self.approval(["unit-gui"]))

        self.assertTrue(collected["already_cleaned"])
        self.assertEqual("release", collected["download_store"]["recorded_as"])
        self.assertEqual([object_id], [item["object_id"] for item in collected["download_store"]["gc"]["collected"]])
        self.assertFalse((store.object_directory(object_id) / "obj").exists())
        record = read_manifest(manifest)["download_store_release"]
        self.assertTrue(record["gc"]["authorized"])
        (earlier,) = record["earlier"]
        self.assertEqual((cleaned["download_store"]["released_at"], False, 1),
                         (earlier["released_at"], earlier["gc"]["authorized"], earlier["claim_count"]))
        self.assertNotIn("claims", earlier)


class ACleanupRepeatedFinishesTheReleaseItLeftUnmade(_Workspace):
    """A cleanup whose claim release failed was never released again: raw_cleaned is no ready status, so
    a repeat under the approval was not ready and a confirmed one was refused, and the claims stayed live,
    keeping the store's object, for good."""

    def failed_release(self) -> tuple[Path, Path, str]:
        approval = self.approval(["unit-a"])
        client = _Client({BASE + "a.mzML": _mzml("a")})
        lease = self.campaign_lease(_project("unit-a", {"FILES/a.mzML": BASE + "a.mzML"}), client, approval)
        manifest = Path(lease["manifest_path"])
        self.validate(manifest)
        with patch.object(DownloadStore, "release_unit", side_effect=OSError("the claims could not be read")):
            cleaned = cleanup_download_lease(manifest, campaign_authorization_path=approval)
        self.assertTrue(cleaned["deleted"])
        self.assertIn("OSError", cleaned["download_store"]["error"])
        self.assertEqual({"materialized"}, {claim["state"] for claim in self.store().claims_for_unit("unit-a")})
        return manifest, approval, _download(lease, "a.mzML")["cache_object_id"]

    def test_a_repeat_under_the_approval_releases_and_collects(self) -> None:
        manifest, approval, object_id = self.failed_release()

        again = cleanup_download_lease(manifest, campaign_authorization_path=approval)

        self.assertEqual((True, True), (again["deleted"], again["already_cleaned"]))
        self.assertEqual("deleted", again["raw_deletion"]["state"], "the deletion's own record, unchanged")
        self.assertEqual({"released"}, {claim["state"] for claim in self.store().claims_for_unit("unit-a")})
        self.assertEqual([object_id], [item["object_id"] for item in again["download_store"]["gc"]["collected"]])
        record = read_manifest(manifest)["download_store_release"]
        self.assertNotIn("error", record)
        self.assertIn("OSError", record["earlier"][0]["error"])
        self.assertEqual([], [unit for item in repository_reanalysis.download_store_status(self.root)["stores"]
                              for unit in item["live_claims_of_released_units"]])

    def test_a_persons_repeat_releases_the_claims(self) -> None:
        manifest, _approval, object_id = self.failed_release()

        again = cleanup_download_lease(manifest, confirmed=True)

        self.assertTrue(again["already_cleaned"])
        self.assertEqual({"released"}, {claim["state"] for claim in self.store().claims_for_unit("unit-a")})
        self.assertFalse(again["download_store"]["gc"]["authorized"])
        self.assertTrue((self.store().object_directory(object_id) / "obj").is_dir())

    def test_a_unit_without_store_claims_is_refused_as_it_always_was(self) -> None:
        approval = self.approval(["unit-a"])
        lease = create_download_lease(_project("unit-a", {"FILES/a.mzML": BASE + "a.mzML"}), self.root, 10**9,
                                      client=_Client({BASE + "a.mzML": _mzml("a")}), raw_retention_policy=DELETE)
        manifest = Path(lease["manifest_path"])
        self.validate(manifest)
        self.assertTrue(cleanup_download_lease(manifest, confirmed=True)["deleted"])

        with self.assertRaisesRegex(ValueError, "requires a completed/validated manifest"):
            cleanup_download_lease(manifest, confirmed=True)
        again = cleanup_download_lease(manifest, campaign_authorization_path=approval)
        self.assertFalse(again["deleted"])
        self.assertNotIn("already_cleaned", again)


class TheDeletionPreviewSaysWhatTheStoreKeeps(_Workspace):
    """The preview's deletion_bytes counted the tree's links to the store's files as freed, and its
    bytes_collectable_after_release left out an archive's extraction tree."""

    def test_a_confirmation_frees_none_of_the_bytes_linked_from_the_store(self) -> None:
        payload = _mzml("a" * 1000)
        lease = create_download_lease(_project("unit-gui", {"FILES/a.mzML": BASE + "a.mzML"}), self.root, 10**9,
                                      client=_Client({BASE + "a.mzML": payload}), store_mode="always",
                                      raw_retention_policy=DELETE)
        manifest = Path(lease["manifest_path"])
        self.validate(manifest)
        (Path(lease["input_directory"]) / "a_2026100210.dcl").write_bytes(b"intermediate")

        preview = plan_download_cleanup(manifest)

        store = preview["download_store"]
        self.assertEqual(len(payload) + len(b"intermediate"), preview["deletion_bytes"])
        self.assertEqual(len(payload), store["tree_bytes_kept_by_store"], "the link's bytes are the store's")
        self.assertEqual(0, store["store_bytes_freed_by_a_confirmation"])
        self.assertEqual(len(payload), store["bytes_collectable_after_release"])
        self.assertIn("frees none of them", store["collection"])

    def test_an_archives_collectable_bytes_count_its_extraction_tree(self) -> None:
        data = _zip({f"st/S{index}.mzML": _mzml("x" * 4000 + str(index)) for index in range(3)})
        url = "https://example.org/st.zip"
        project = RepositoryProject(
            repository="metabolomics_workbench", accession="ST000077", analysis_unit_id="an000077",
            eligible=True, selection_status="eligible", separation="LC-MS", acquisition_mode="DDA",
            ion_mode="Positive", untargeted=True,
            files=[RepositoryFile(name="st.zip", size_bytes=len(data), url=url, role="shared_raw_archive",
                                  checksum=hashlib.md5(data).hexdigest())],
            total_download_bytes=len(data),
            sample_metadata=[{"sample_id": f"S{index}", "raw_file": f"S{index}.mzML"} for index in range(3)],
        )
        approval = self.approval(["an000077"])
        manifest = Path(self.campaign_lease(project, _Client({url: data}), approval)["manifest_path"])
        self.validate(manifest)

        preview = plan_download_cleanup(manifest)["download_store"]
        cleaned = cleanup_download_lease(manifest, campaign_authorization_path=approval)

        removed = sum(item["removed_bytes"] for item in cleaned["download_store"]["gc"]["collected"])
        self.assertGreater(removed, len(data))
        self.assertEqual(removed, preview["bytes_collectable_after_release"])
        self.assertEqual(removed - len(data), preview["tree_bytes_kept_by_store"], "the three members' links")


class ASplitParentKeepsItsClaimWhileItsPartsAreLive(_Workspace, _MixedUnitFixture):
    def split(self) -> tuple[Path, dict[str, Path], dict]:
        payloads = {BASE + name: _mzml(name) for name in self.MODES}
        project = _project(
            "unit-mixed",
            {f"FILES/{name}": BASE + name for name in self.MODES},
            accession="MTBLS-MIXED",
            class_proposal={
                "proposal_id": "p1", "status": "accepted", "selected_fields": ["origin"],
                "assignments": [{"sample_id": Path(name).stem, "class_label": "Bio"} for name in self.MODES],
            },
        )
        lease = create_download_lease(
            project, self.root, 10**9, client=_Client(payloads), store_mode="always", raw_retention_policy=DELETE
        )
        parent = Path(lease["manifest_path"])
        extractor = self.base / "RawMetadataConsoleApp.exe"
        extractor.write_bytes(b"stub")
        with patch("msdial_app.repository_reanalysis.subprocess.run", side_effect=self._extractor(self.MODES)):
            run_raw_metadata_preflight(parent, extractor)
        result = split_unit_by_acquisition(parent, confirmed=True)
        self.assertTrue(result["written"], result["blockers"])
        return parent, {part["acquisition_mode"]: Path(part["manifest_path"]) for part in result["parts"]}, lease

    def test_the_parent_holds_its_claims_until_its_own_release(self) -> None:
        approval = self.approval(["unit-mixed"])
        parent, parts, lease = self.split()
        store = DownloadStore(self.root, "metabolights", "MTBLS-MIXED")
        objects = [item["cache_object_id"] for item in lease["downloads"]]

        self.assertEqual([], [claim for part in parts.values()
                              for claim in store.claims_for_unit(read_manifest(part)["project"]["analysis_unit_id"])],
                         "parts never claim")
        self.validate(parts["DDA"])
        waiting = cleanup_split_parent(parent, campaign_authorization_path=approval)

        self.assertFalse(waiting["deleted"])
        self.assertEqual({"materialized"}, {claim["state"] for claim in store.claims_for_unit("unit-mixed")})
        self.assertTrue(all((store.object_directory(item) / "obj").is_dir() for item in objects))
        # A part's own cleanup is the parent's to make; it releases nothing.
        self.validate(parts["DIA"])
        released = cleanup_split_parent(parent, campaign_authorization_path=approval)

        self.assertTrue(released["deleted"], released.get("blockers"))
        self.assertEqual("raw_cleaned", released["download_store"]["reason"])
        self.assertEqual({"released"}, {claim["state"] for claim in store.claims_for_unit("unit-mixed")})
        self.assertEqual(sorted(objects), sorted(item["object_id"] for item in released["download_store"]["gc"]["collected"]))
        self.assertEqual("split_by_acquisition", read_manifest(parent)["status"])


def _workbench_handoff(unit: str, ion_mode: str, url: str, size: int, md5: str, samples: list[str]) -> dict:
    """A Catalog handoff of one polarity of a Workbench study, whose one file is the study archive."""
    return {
        "schema": "msdial-repository-reanalysis-handoff.v1",
        "repository": "metabolomics_workbench",
        "accession": "ST000001",
        "analysis_unit_id": unit,
        "technical_settings": {
            "separation": "LC-MS", "ion_mode": ion_mode, "acquisition_mode": "DDA",
            "target_omics": "Metabolomics", "untargeted": True,
        },
        "files": [
            {"path": "ST000001_Rawdata.zip", "role": "shared_raw_archive", "size_bytes": size,
             "download_url": url, "checksum": md5}
        ],
        "sample_metadata": [{"sample_id": sample, "raw_file": sample, "attributes": {}} for sample in samples],
        "class_proposal": {"proposal_id": "class-1", "assignments": []},
        "blocking_reasons": [],
        "download_scope": {
            "file_count": 1,
            "bundle_bytes": size,
            "objects": [
                {"url": url, "name": "ST000001_Rawdata.zip", "kind": "archive", "bytes": size, "known_bytes": size,
                 "size_known": True, "consumer_unit_ids": ["an000001-neg", "an000001-pos"]}
            ],
        },
        "sample_count": len(samples),
    }


class AWorkbenchStudyArchiveIsFetchedOnceAndExtractedOnce(_Workspace):
    MEMBERS = {
        "ST000001/NEG/S1_neg.mzML": _mzml("negative one"),
        "ST000001/NEG/S2_neg.mzML": _mzml("negative two"),
        "ST000001/POS/S1_pos.mzML": _mzml("positive one"),
        "ST000001/POS/S2_pos.mzML": _mzml("positive two"),
        "ST000001/README.txt": b"study notes",
    }
    UNITS = {"an000001-neg": ("Negative", ["S1_neg", "S2_neg"]), "an000001-pos": ("Positive", ["S1_pos", "S2_pos"])}

    def test_a_shared_study_archive_serves_both_polarities(self) -> None:
        data = _zip(self.MEMBERS)
        md5 = hashlib.md5(data).hexdigest()
        approval = self.approval(list(self.UNITS))
        leases = {}
        with _Server({"/studydata/ST000001_Rawdata.zip": data}) as repository:
            url = repository.url("/studydata/ST000001_Rawdata.zip")
            for unit, (ion_mode, samples) in self.UNITS.items():
                handoff = _workbench_handoff(unit, ion_mode, url, len(data), md5, samples)
                project, _ = mcp_server._project_from_analysis_unit_handoff(handoff)
                leases[unit] = self.campaign_lease(
                    project_from_dict(project), RepositoryHttpClient(timeout=10), approval
                )
            gets = list(repository.gets)

        self.assertEqual(["/studydata/ST000001_Rawdata.zip"], gets, "one GET for both units")
        neg, pos = leases["an000001-neg"], leases["an000001-pos"]
        store = DownloadStore(self.root, "metabolomics_workbench", "ST000001")
        object_id = neg["downloads"][0]["cache_object_id"]
        entry = store.entry(object_id)
        self.assertEqual("an000001-neg", entry["tree"]["extracted_by"]["unit_id"], "extracted once, by the first")
        self.assertEqual(
            ("extracted", "reused"),
            tuple(lease["archive_extractions"][0]["store_extraction"]["action"] for lease in (neg, pos)),
        )
        for unit, lease in leases.items():
            with self.subTest(unit=unit):
                polarity = "neg" if unit.endswith("neg") else "pos"
                other = "pos" if polarity == "neg" else "neg"
                self.assertEqual(
                    [f"S1_{polarity}.mzML", f"S2_{polarity}.mzML"],
                    sorted(Path(item).name for item in lease["input_candidates"]),
                )
                download = lease["downloads"][0]
                self.assertTrue(download["declared_checksum_verified"])
                self.assertEqual(download["cache_object_path"], download["path"], "an archive's path is the store's")
                self.assertEqual(1, lease["allowlist_checksum_validation"]["archives_verified_at_download"])
                # The member listing is the unit's own, in its provenance, as the per-unit extraction wrote it.
                extraction = lease["archive_extractions"][0]
                listing = Path(extraction["members_tsv"]["path"])
                self.assertEqual(Path(lease["workspace"]) / "provenance", listing.parent)
                self.assertEqual(hashlib.sha256(listing.read_bytes()).hexdigest(), extraction["members_tsv"]["sha256"])
                self.assertEqual(lease["input_directory"], extraction["destination"])
                row = next(item for item in lease["input_lineage"]["rows"] if Path(item["path"]).name == f"S1_{polarity}.mzML")
                self.assertEqual("archive_declared_checksum", row["basis"]["kind"])
                # Only the unit's own samples are left in its tree; the store keeps the rest.
                data_root = Path(lease["input_directory"])
                self.assertTrue(os.path.samefile(
                    data_root / "ST000001" / polarity.upper() / f"S1_{polarity}.mzML",
                    store.object_directory(object_id) / "t" / "ST000001" / polarity.upper() / f"S1_{polarity}.mzML",
                ))
                self.assertFalse((data_root / "ST000001" / other.upper()).exists())
                self.assertFalse((data_root / "ST000001" / "README.txt").exists())
                self.assertEqual(3, lease["raw_storage"]["pruned"]["removed_files"])
                self.assertEqual(list(LEASE_STAGES), [stage["stage"] for stage in lease["lease_stages"]])
        self.assertTrue((store.object_directory(object_id) / "t" / "ST000001" / "POS" / "S1_pos.mzML").is_file())

    def test_two_archives_carrying_one_identical_file_merge_as_the_per_unit_lease_did(self) -> None:
        first = _zip({"study/S1.mzML": _mzml("one"), "study/README.txt": b"same notes"})
        second = _zip({"study/S2.mzML": _mzml("two"), "study/README.txt": b"same notes"})
        project = RepositoryProject(
            repository="metabolomics_workbench", accession="ST000002", analysis_unit_id="an000002-neg",
            eligible=True, selection_status="eligible",
            files=[
                RepositoryFile(name="part1.zip", size_bytes=len(first), url="https://example.org/part1.zip", role="raw_archive"),
                RepositoryFile(name="part2.zip", size_bytes=len(second), url="https://example.org/part2.zip", role="raw_archive"),
            ],
            total_download_bytes=len(first) + len(second),
            sample_metadata=[{"sample_id": "S1", "raw_file": "S1.mzML"}, {"sample_id": "S2", "raw_file": "S2.mzML"}],
        )
        lease = create_download_lease(
            project, self.root, 10**9, client=_Client({"https://example.org/part1.zip": first,
                                                       "https://example.org/part2.zip": second}),
            store_mode="always",
        )

        self.assertEqual(["S1.mzML", "S2.mzML"], sorted(Path(item).name for item in lease["input_candidates"]))
        self.assertEqual(1, lease["archive_extractions"][1]["merge"]["already_present_files"])
        self.assertEqual(["study/README.txt"], lease["archive_extractions"][1]["merge"]["already_present"])
        self.assertEqual(1, lease["raw_storage"]["duplicate_source_files"])


class WhatTheSciexReaderOpensStaysWithItsInput(_Workspace):
    """The prune kept only <input>.scan, so x.wiff2 lost the x.wiff.scan and x.timeseries.data SCIEX OS writes
    beside it, and x.wiff its x.wiff.<n>.scan: the per-unit lease had kept them all, the analysis CSV's alias
    found none to carry, and the reader could not open the input."""

    def lease(self, members: dict[str, bytes], raw_file: str, *, store: bool) -> dict:
        data = _zip(members)
        url = "https://example.org/study.zip"
        accession = "ST000009" if store else "ST000010"
        project = RepositoryProject(
            repository="metabolomics_workbench", accession=accession, analysis_unit_id=f"an-{accession}",
            eligible=True, selection_status="eligible", separation="LC-MS", acquisition_mode="DDA",
            ion_mode="Positive", untargeted=True,
            files=[RepositoryFile(name="study.zip", size_bytes=len(data), url=url, role="shared_raw_archive")],
            total_download_bytes=len(data),
            sample_metadata=[{"sample_id": "S1", "raw_file": raw_file}],
        )
        return create_download_lease(
            project, self.root, 10**9, client=_Client({url: data}), store_mode="always" if store else "campaign"
        )

    def trees(self, members: dict[str, bytes], raw_file: str) -> tuple[list[str], list[str], dict]:
        per_unit, stored = (self.lease(members, raw_file, store=store) for store in (False, True))
        self.assertNotIn("download_cache", per_unit)
        names = [sorted(path.name for path in (Path(lease["input_directory"]) / "study").iterdir())
                 for lease in (per_unit, stored)]
        return names[0], names[1], stored

    def test_a_wiff2_keeps_the_wiff_scan_and_timeseries_the_per_unit_lease_kept(self) -> None:
        members = {
            f"study/{sample}{suffix}": f"{sample}{suffix}".encode("ascii")
            for sample in ("S1", "S2") for suffix in (".wiff2", ".wiff.scan", ".timeseries.data")
        }
        per_unit, stored, lease = self.trees(members, "S1.wiff2")

        self.assertEqual(["S1.timeseries.data", "S1.wiff.scan", "S1.wiff2"], stored)
        self.assertEqual([name for name in per_unit if name.startswith("S1.")], stored,
                         "the store's tree is the per-unit tree less the other samples")
        self.assertEqual(3, lease["raw_storage"]["pruned"]["removed_files"], "S2's three files")
        (input_path,) = [Path(item) for item in lease["input_candidates"]]
        # What the analysis CSV's alias carries beside the input, by the same rule.
        self.assertEqual([".timeseries.data", ".wiff.scan"], [rest for _path, rest in _travelling_files(input_path)])

    def test_an_analyst_wiff_keeps_every_part_of_its_scan(self) -> None:
        members = {
            f"study/{name}": name.encode("ascii")
            for name in ("S1.wiff", "S1.wiff.scan", "S1.wiff.1.scan", "S1.wiff.2.scan", "S2.wiff", "S2.wiff.scan")
        }
        per_unit, stored, _lease = self.trees(members, "S1.wiff")

        self.assertEqual(["S1.wiff", "S1.wiff.1.scan", "S1.wiff.2.scan", "S1.wiff.scan"], stored)
        self.assertEqual([name for name in per_unit if name.startswith("S1.")], stored)

    def test_what_travels_with_a_sciex_file(self) -> None:
        for name, primary, expected in (
            ("x.wiff.scan", "x.wiff2", True), ("X.TIMESERIES.DATA", "x.wiff2", True), ("x.wiff2.scan", "x.wiff2", True),
            ("x.wiff.scan", "x.wiff", True), ("x.wiff.3.scan", "x.wiff", True), ("x.wiff2", "x.wiff2", False),
            ("y.wiff.scan", "x.wiff2", False), ("x.timeseries.data", "x.mzML", False), ("x.wiff.scan.bak", "x.wiff", False),
        ):
            with self.subTest(name=name, primary=primary):
                self.assertIs(expected, travels_with_sciex_file(name, primary))


class ALeaseWaitsForAnotherLeasesTransfer(_Workspace):
    def test_the_waiter_says_so_reuses_the_bytes_and_makes_no_second_get(self) -> None:
        gate = threading.Event()
        client = _Client({BASE + "shared.mzML": _mzml("shared")}, gate=gate)
        events: list[tuple[str, dict]] = []
        results: dict[str, dict] = {}

        def lease(unit: str, **options) -> None:
            results[unit] = create_download_lease(
                _project(unit, {"FILES/shared.mzML": BASE + "shared.mzML"}), self.root, 10**9, client=client,
                store_mode="always", **options,
            )

        with patch.object(repository_reanalysis, "STORE_WAIT_POLL_SECONDS", 0.2):
            first = threading.Thread(target=lease, args=("unit-a",))
            first.start()
            self.assertTrue(client.started.wait(10))
            second = threading.Thread(
                target=lease, args=("unit-b",),
                kwargs={"shared_download_callback": lambda event, detail: events.append((event, detail))},
            )
            second.start()
            for _ in range(100):
                if events:
                    break
                threading.Event().wait(0.05)
            gate.set()
            first.join(30)
            second.join(30)

        self.assertEqual([BASE + "shared.mzML"], client.calls)
        self.assertEqual(["waiting_for_shared_download", "shared_download_ready"], [event for event, _ in events])
        self.assertEqual("unit-b", events[0][1]["unit_id"])
        self.assertEqual("inherited_from_cache", results["unit-b"]["downloads"][0]["sha256_origin"])
        self.assertEqual(1, results["unit-b"]["download_cache"]["waited_for_shared_download"])

    def test_the_backend_job_shows_the_wait_and_runs_on(self) -> None:
        seen: list[str] = []

        def lease(project, workspace_root, maximum_bytes, **options):
            options["shared_download_callback"]("waiting_for_shared_download", {"object": "shared.mzML", "pid": 1})
            seen.append(server.JOBS["job-1"]["status"])
            seen.append(server.JOBS["job-1"]["waiting_for"]["object"])
            options["shared_download_callback"]("shared_download_ready", {"object": "shared.mzML"})
            seen.append(server.JOBS["job-1"]["status"])
            return {
                "manifest_path": "m", "workspace": "w", "raw_directory": "r", "input_directory": "i",
                "output_directory": "o", "analysis_input_path": "i", "input_candidates": [],
            }

        jobs = {"job-1": {"id": "job-1", "status": "queued", "kind": "repository_download", "logs": []}}
        with patch.object(server, "JOBS", jobs), patch.object(server, "PROCESSES", {}), \
                patch.object(server, "_persist_jobs_locked", lambda: None), \
                patch.object(server, "create_download_lease", lease):
            server._run_repository_download_job("job-1", _project("unit-a", {}), self.root, 10**9, False, "keep")

        self.assertEqual(["waiting_for_shared_download", "shared.mzML", "running"], seen)
        self.assertEqual("completed", jobs["job-1"]["status"])
        self.assertIn("waiting_for_shared_download", server.LIVE_JOB_STATUSES, "a waiting job can be cancelled")

    def test_a_cancel_is_heard_while_the_lease_waits(self) -> None:
        gate = threading.Event()
        client = _Client({BASE + "shared.mzML": _mzml("shared")}, gate=gate)
        waiting = threading.Event()
        outcome: dict[str, BaseException] = {}

        class Cancelled(Exception):
            pass

        def first() -> None:
            create_download_lease(
                _project("unit-a", {"FILES/shared.mzML": BASE + "shared.mzML"}), self.root, 10**9, client=client,
                store_mode="always",
            )

        def progress(*_values) -> None:
            if waiting.is_set():
                raise Cancelled("cancelled on request")

        def second() -> None:
            try:
                create_download_lease(
                    _project("unit-b", {"FILES/shared.mzML": BASE + "shared.mzML"}), self.root, 10**9, client=client,
                    store_mode="always", progress_callback=progress,
                    shared_download_callback=lambda event, _detail: waiting.set(),
                )
            except BaseException as error:  # noqa: BLE001 - the test records what stopped it
                outcome["error"] = error

        with patch.object(repository_reanalysis, "STORE_WAIT_POLL_SECONDS", 0.2):
            holder = threading.Thread(target=first)
            holder.start()
            self.assertTrue(client.started.wait(10))
            waiter = threading.Thread(target=second)
            waiter.start()
            waiter.join(30)
            gate.set()
            holder.join(30)

        self.assertIsInstance(outcome.get("error"), Cancelled)
        manifest = read_manifest(self.root / "metabolights" / "MTBLS-SHARED" / "unit-b" / "provenance" / "run-manifest.json")
        self.assertEqual(("download_failed", "fetch"), (manifest["status"], manifest["download_failure"]["stage"]))
        self.assertEqual(str(self.store().root), manifest["download_cache"]["store"])
        self.assertEqual("pending", self.store().read_claim(BASE + "shared.mzML", "unit-b")["state"],
                         "a stopped lease keeps its claim for its retry")
        self.assertEqual([BASE + "shared.mzML"], client.calls)


class AFileItsReaderRewritesIsCopiedNotLinked(_Workspace):
    def test_a_baf_folder_keeps_its_sqlite_as_the_units_own_copy(self) -> None:
        archive = _zip({"S1.d/analysis.baf": b"baf bytes", "S1.d/analysis.sqlite": b"sqlite bytes"})
        project = RepositoryProject(
            repository="metabolomics_workbench", accession="ST000003", analysis_unit_id="an000003-pos",
            eligible=True, selection_status="eligible",
            files=[RepositoryFile(name="ST000003.zip", size_bytes=len(archive), url="https://example.org/ST000003.zip",
                                  role="raw_archive")],
            total_download_bytes=len(archive),
            sample_metadata=[{"sample_id": "S1", "raw_file": "S1.d"}],
        )
        lease = create_download_lease(
            project, self.root, 10**9, client=_Client({"https://example.org/ST000003.zip": archive}),
            store_mode="always",
        )
        folder = Path(lease["input_directory"]) / "S1.d"
        tree = DownloadStore(self.root, "metabolomics_workbench", "ST000003").object_directory(
            lease["downloads"][0]["cache_object_id"]
        ) / "t" / "S1.d"

        self.assertEqual([str(folder.resolve())], [str(Path(item).resolve()) for item in lease["input_candidates"]])
        self.assertTrue(os.path.samefile(folder / "analysis.baf", tree / "analysis.baf"))
        self.assertFalse(os.path.samefile(folder / "analysis.sqlite", tree / "analysis.sqlite"),
                         "baf2sql may rewrite it in place, which through a link would change the store's file")
        self.assertEqual(b"sqlite bytes", (folder / "analysis.sqlite").read_bytes())
        self.assertEqual((1, ["S1.d/analysis.sqlite"]),
                         (lease["raw_storage"]["protected_copies"], lease["raw_storage"]["protected_copy_paths"]))
        self.assertEqual("mixed", lease["raw_storage"]["materialization"])


class ReservedNamesAreNeverUnits(_Workspace):
    def test_a_unit_named_like_the_store_is_refused_before_anything_is_written(self) -> None:
        for name in ("_dl", "_DL", "_campaigns"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "keeps for itself"):
                create_download_lease(
                    _project(name, {"FILES/a.mzML": BASE + "a.mzML"}), self.root, 10**9,
                    client=_Client({BASE + "a.mzML": _mzml("a")}),
                )
        self.assertEqual([], list(self.root.iterdir()))


class WithoutACampaignOrTheSettingNothingChanges(_Workspace):
    def test_the_default_lease_downloads_into_the_unit_as_before(self) -> None:
        client = _Client({BASE + "a.mzML": _mzml("a")})
        lease = create_download_lease(_project("unit-a", {"FILES/a.mzML": BASE + "a.mzML"}), self.root, 10**9, client=client)

        self.assertNotIn("download_cache", lease)
        self.assertNotIn("raw_storage", lease)
        self.assertNotIn("cache_object_path", lease["downloads"][0])
        self.assertEqual("not_used", _stage(lease, "materialise")["status"])
        self.assertFalse((self.root / "metabolights" / "MTBLS-SHARED" / "_dl").exists())
        self.assertEqual(1, os.stat(Path(lease["input_directory"]) / "a.mzML").st_nlink)

    def test_the_saved_store_mode_always_turns_the_store_on(self) -> None:
        save_download_store_mode("always")
        client = _Client({BASE + "a.mzML": _mzml("a")})
        lease = create_download_lease(_project("unit-a", {"FILES/a.mzML": BASE + "a.mzML"}), self.root, 10**9, client=client)

        self.assertEqual("store_mode", lease["download_cache"]["activated_by"])
        self.assertTrue((self.root / "metabolights" / "MTBLS-SHARED" / "_dl").is_dir())
        with self.assertRaises(ValueError):
            save_download_store_mode("sometimes")


if __name__ == "__main__":
    unittest.main()
