"""What the batch plan, the download preview and the store status say about the accession download store.

The text a person approves for a campaign showed only distinct bytes, while every unit downloaded its own
copies: the actual transfer was the per-unit figure, 12.82 TB against 6.99 TB for the declared pool, and
nothing printed it. With the lease fetching through the store, the batch plan reports both, the distinct
objects with the units that consume each, and an order that keeps units sharing an object together; it can
pre-claim each approved unit's objects; the download preview says what the store already holds; and a
read-only status shows every store without taking _dl or _campaigns for units.

Every path and approval is synthetic.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from test_accession_download_store import BASE, DELETE, _Client, _mzml, _project, _Workspace

from msdial_app import mcp_server
from msdial_app.download_store import DownloadStore
from msdial_app.repository_reanalysis import create_download_lease, unit_workspaces, update_manifest


def _handoff(unit: str, files: list[tuple[str, str, int]], accession: str = "ST000009") -> dict:
    """A Workbench-shaped handoff: (listed path, URL, listed size) per file, one sample."""
    return {
        "schema": "msdial-repository-reanalysis-handoff.v1",
        "repository": "metabolomics_workbench",
        "accession": accession,
        "analysis_unit_id": unit,
        "technical_settings": {
            "separation": "LC-MS", "ion_mode": "Negative", "acquisition_mode": "DDA",
            "target_omics": "Metabolomics", "untargeted": True,
        },
        "files": [
            {"path": name, "role": "raw_archive" if name.endswith(".zip") else "raw", "size_bytes": size,
             "download_url": url, "checksum": ""}
            for name, url, size in files
        ],
        "sample_metadata": [{"sample_id": unit, "raw_file": files[-1][0], "attributes": {}}],
        "class_proposal": {"proposal_id": "class-1", "assignments": []},
        "blocking_reasons": [],
        "download_scope": {"file_count": len(files), "bundle_bytes": sum(size for *_rest, size in files)},
        "sample_count": 1,
    }


ARCHIVE = ("ST000009_Raw.zip", "https://example.org/ST000009_Raw.zip", 100)


class TheBatchPlanCountsEachObjectOnce(_Workspace):
    def plan(self, handoffs: list[dict] | None = None, **options) -> dict:
        return mcp_server.msdial_repository_batch_plan(
            analysis_unit_handoffs=handoffs or [
                _handoff("unit-a", [ARCHIVE, ("a.mzML", "https://example.org/a.mzML", 10)]),
                _handoff("unit-c", [("c.mzML", "https://example.org/c.mzML", 7)]),
                _handoff("unit-b", [ARCHIVE, ("b.mzML", "https://example.org/b.mzML", 20)]),
            ],
            workspace_root=str(self.root),
            analysis_purpose="Annotate every experimental spectrum.",
            **options,
        )

    def test_distinct_bytes_against_per_unit_bytes_and_a_sharing_group_order(self) -> None:
        plan = self.plan()
        download = plan["download_plan"]

        self.assertEqual(110 + 7 + 120, download["per_unit_known_bytes"])
        self.assertEqual(100 + 10 + 7 + 20, download["distinct_bytes"])
        self.assertEqual(137, download["distinct_bytes_to_transfer"])
        self.assertEqual((4, 1), (download["object_count"], download["shared_objects"]))
        shared = next(item for item in download["objects"] if item["url"].endswith("ST000009_Raw.zip"))
        self.assertEqual(["unit-a", "unit-b"], shared["consumers"])
        self.assertEqual(["unit-a", "unit-b", "unit-c"], plan["run_order"], "units sharing an object run together")
        self.assertEqual([["unit-a", "unit-b"], ["unit-c"]], [group["unit_ids"] for group in download["groups"]])
        runs = {run["analysis_unit_id"]: run for run in plan["runs"]}
        self.assertEqual((110, 1, 1), (runs["unit-a"]["unit_object_bytes"], runs["unit-a"]["shared_object_count"],
                                       runs["unit-a"]["run_position"]))
        self.assertEqual(runs["unit-a"]["sharing_group"], runs["unit-b"]["sharing_group"])
        self.assertFalse((self.root / "metabolomics_workbench").exists(), "planning writes nothing")

    def test_an_object_of_unknown_size_is_counted_and_never_priced_at_zero(self) -> None:
        plan = self.plan([
            _handoff("unit-a", [("ST000009_Raw.zip", "https://example.org/ST000009_Raw.zip", 0),
                                ("a.mzML", "https://example.org/a.mzML", 10)]),
        ])
        download = plan["download_plan"]

        self.assertEqual((10, 10, 1), (download["distinct_bytes"], download["distinct_bytes_lower_bound"],
                                       download["unknown_size_objects"]))
        self.assertIsNone(plan["runs"][0]["unit_object_bytes"])
        self.assertEqual(1, download["units_of_unknown_size"])

    def test_the_catalogs_download_objects_are_read_where_the_handoff_lists_them(self) -> None:
        handoff = _handoff("unit-a", [ARCHIVE])
        handoff["download_scope"]["objects"] = [
            {"url": ARCHIVE[1], "name": "ST000009_Raw.zip", "kind": "archive", "bytes": 4000, "known_bytes": 4000,
             "size_known": True, "consumer_unit_ids": ["unit-a", "unit-z"]}
        ]
        handoff["download_scope"]["bundle_bytes"] = 4000
        plan = self.plan([handoff])
        item = plan["download_plan"]["objects"][0]

        self.assertEqual((4000, "catalog_download_objects", ["unit-a", "unit-z"]),
                         (item["bytes"], item["declared_by"], item["catalog_consumer_unit_ids"]))

    def test_an_object_already_in_the_store_transfers_nothing(self) -> None:
        client = _Client({"https://example.org/a.mzML": _mzml("a")})
        project = _project("unit-a", {"a.mzML": "https://example.org/a.mzML"}, accession="ST000009")
        project.repository = "metabolomics_workbench"
        create_download_lease(project, self.root, 10**9, client=client, store_mode="always")
        plan = self.plan([_handoff("unit-b", [("a.mzML", "https://example.org/a.mzML", 10)])])

        download = plan["download_plan"]
        self.assertEqual((1, 0), (download["objects_in_store"], download["distinct_bytes_to_transfer"]))
        self.assertTrue(download["objects"][0]["in_store"])

    def test_pre_claim_needs_an_approval_and_claims_only_what_it_covers(self) -> None:
        refused = self.plan(pre_claim=True)
        self.assertFalse(refused.get("ok", True))
        approval = self.approval(["unit-a", "unit-b"])
        plan = self.plan(pre_claim=True, campaign_authorization_path=str(approval), raw_retention_policy=DELETE)

        store = DownloadStore(self.root, "metabolomics_workbench", "ST000009")
        self.assertEqual(
            {"unit-a": 2, "unit-b": 2}, {item["analysis_unit_id"]: item["live"] for item in plan["pre_claimed"]}
        )
        self.assertEqual(["unit-c"], [item["analysis_unit_id"] for item in plan["not_pre_claimed"]])
        self.assertEqual({"pending"}, {claim["state"] for claim in store.all_claims()})
        self.assertEqual({"batch_plan"}, {claim["claimed_by"]["source"] for claim in store.all_claims()})
        # Asked again, the plan keeps the claims as they are.
        again = self.plan(pre_claim=True, campaign_authorization_path=str(approval), raw_retention_policy=DELETE)
        self.assertEqual(plan["pre_claimed"], again["pre_claimed"])
        self.assertEqual(4, len(store.all_claims()))

    def test_a_unit_named_like_the_store_is_blocked(self) -> None:
        plan = self.plan([_handoff("_dl", [("a.mzML", "https://example.org/a.mzML", 10)])])

        self.assertIn("workspace_name:reserved", plan["runs"][0]["blocking_reasons"])
        self.assertFalse(plan["runs"][0]["ready"])


class TheDownloadPreviewSaysWhatTheStoreHolds(_Workspace):
    def test_a_campaign_preview_reports_the_bytes_left_to_transfer(self) -> None:
        client = _Client({"https://example.org/a.mzML": _mzml("a")})
        project = _project("unit-a", {"a.mzML": "https://example.org/a.mzML"}, accession="ST000009")
        project.repository = "metabolomics_workbench"
        create_download_lease(project, self.root, 10**9, client=client, store_mode="always")
        approval = self.approval(["unit-b"])

        # The approval stands in for confirmed=true, so the tool would start the backend's download.
        with patch.object(mcp_server, "_request_json", return_value={"job_id": "job-b"}) as started:
            preview = mcp_server.msdial_download_repository_raw(
                "metabolomics_workbench", "ST000009", str(self.root),
                raw_retention_policy=DELETE,
                analysis_unit_handoff=_handoff("unit-b", [("a.mzML", "https://example.org/a.mzML", 10),
                                                          ("b.mzML", "https://example.org/b.mzML", 20)]),
                analysis_purpose="Annotate every experimental spectrum.",
                campaign_authorization_path=str(approval),
                port=1,
            )

        store = preview["preview"]["download_store"]
        self.assertEqual((2, 1, 30, 20), (store["object_count"], store["objects_in_store"], store["distinct_bytes"],
                                          store["distinct_bytes_to_transfer"]))
        self.assertEqual(30, preview["preview"]["required_download_bytes"], "the approval quantity is unchanged")
        self.assertEqual(1, started.call_count)

    def test_without_a_campaign_or_the_setting_the_preview_is_as_before(self) -> None:
        preview = mcp_server.msdial_download_repository_raw(
            "metabolomics_workbench", "ST000009", str(self.root),
            analysis_unit_handoff=_handoff("unit-b", [("b.mzML", "https://example.org/b.mzML", 20)]),
            analysis_purpose="Annotate every experimental spectrum.",
            port=1,
        )

        self.assertNotIn("download_store", preview["preview"])


class TheStoreStatusNeverTakesTheStoreForAUnit(_Workspace):
    def test_the_status_reads_the_stores_and_never_takes_them_for_units(self) -> None:
        client = _Client({BASE + "shared.mzML": _mzml("shared")})
        create_download_lease(
            _project("unit-a", {"FILES/shared.mzML": BASE + "shared.mzML"}), self.root, 10**9, client=client,
            store_mode="always",
        )
        (self.root / "_campaigns" / "c1").mkdir(parents=True)
        accession = self.root / "metabolights" / "MTBLS-SHARED"
        # A stray manifest inside the store directory is still no unit.
        (accession / "_dl" / "provenance").mkdir()
        (accession / "_dl" / "provenance" / "run-manifest.json").write_text("{}", encoding="utf-8")

        self.assertEqual(["unit-a"], [path.name for path in unit_workspaces(accession)])
        status = mcp_server.msdial_download_store_status(str(self.root))
        self.assertEqual(1, status["store_count"])
        store = status["stores"][0]
        self.assertEqual(("metabolights", "MTBLS-SHARED"), (store["repository"], store["accession"]))
        self.assertEqual({"unit-a"}, set(store["units"]))
        self.assertEqual("prepared", store["units"]["unit-a"]["status"])
        self.assertEqual(["unit-a"], store["objects"][0]["live_claims"])
        self.assertEqual([], store["unclaimed_objects"])
        self.assertEqual("campaign", status["store_mode"])

    def test_a_released_unit_whose_claim_is_still_live_is_flagged(self) -> None:
        client = _Client({BASE + "shared.mzML": _mzml("shared")})
        lease = create_download_lease(
            _project("unit-a", {"FILES/shared.mzML": BASE + "shared.mzML"}), self.root, 10**9, client=client,
            store_mode="always",
        )
        update_manifest(Path(lease["manifest_path"]), lambda current: current.update(status="raw_cleaned"))
        status = mcp_server.msdial_download_store_status(str(self.root), "metabolights", "MTBLS-SHARED")

        self.assertEqual(["unit-a"], status["stores"][0]["live_claims_of_released_units"])
        narrowed = mcp_server.msdial_download_store_status(str(self.root), analysis_unit_id="unit-z")
        self.assertEqual({}, narrowed["stores"][0]["units"])


if __name__ == "__main__":
    unittest.main()
