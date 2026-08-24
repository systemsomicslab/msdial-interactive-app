import os
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


_CONFIG = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"LOCALAPPDATA": _CONFIG.name}):
    from msdial_app.server import (
        Handler,
        ExclusiveThreadingHTTPServer,
        JOBS,
        JOBS_LOCK,
        _changed_run_artifacts,
        _snapshot_run_artifacts,
    )


class JobArtifactTests(unittest.TestCase):
    def _post(self, port: int, path: str, body: dict) -> dict:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_local_server_rejects_duplicate_port(self) -> None:
        first = ExclusiveThreadingHTTPServer(("127.0.0.1", 0), Handler)
        try:
            with self.assertRaises(OSError):
                ExclusiveThreadingHTTPServer(("127.0.0.1", first.server_port), Handler)
        finally:
            first.server_close()

    def test_old_qa_and_mztab_are_not_attributed_to_new_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_qa = root / "old.qa.tsv"
            old_mztab = root / "old.mzTab"
            old_qa.write_text("old qa", encoding="ascii")
            old_mztab.write_text("old mztab", encoding="ascii")
            preparation = {
                "run_directory": str(root),
                "export_folder_path": str(root),
            }
            baseline = _snapshot_run_artifacts(preparation)

            current_mztab = root / "current.mzTab"
            current_mztab.write_text("current mztab", encoding="ascii")
            artifacts = _changed_run_artifacts(preparation, baseline)

            self.assertEqual([str(current_mztab.resolve())], artifacts["mztab"])
            self.assertEqual([], artifacts["qa"])
            self.assertNotIn(str(old_mztab.resolve()), artifacts["mztab"])
            self.assertNotIn(str(old_qa.resolve()), artifacts["qa"])

    def test_updated_existing_qa_is_attributed_to_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            qa = root / "alignment.qa.tsv"
            qa.write_text("before", encoding="ascii")
            preparation = {
                "run_directory": str(root),
                "export_folder_path": str(root),
            }
            baseline = _snapshot_run_artifacts(preparation)

            qa.write_text("after with a different size", encoding="ascii")
            artifacts = _changed_run_artifacts(preparation, baseline)

            self.assertEqual([str(qa.resolve())], artifacts["qa"])
            record = next(item for item in artifacts["records"] if item["path"] == str(qa.resolve()))
            self.assertEqual("updated", record["change"])

    def test_job_scoped_api_does_not_fall_back_to_old_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_mztab = root / "old.mzTab"
            current_mztab = root / "current.mzTab"
            old_qa = root / "old.qa.tsv"
            content = "MTD\tmzTab-version\t2.0.0-M\nSMH\tidentifier\nSML\tfeature\n"
            old_mztab.write_text(content, encoding="ascii")
            current_mztab.write_text(content, encoding="ascii")
            old_qa.write_text("unrelated", encoding="ascii")
            (root / "workflow-settings.json").write_text(
                json.dumps({"project_type": "lcms", "files": [], "output_root": str(root)}),
                encoding="utf-8",
            )
            job_id = uuid.uuid4().hex
            with JOBS_LOCK:
                JOBS[job_id] = {
                    "id": job_id,
                    "kind": "run",
                    "status": "completed",
                    "preparation": {"run_directory": str(root), "analysis_type": "lcms"},
                    "artifacts": {"mztab": [str(current_mztab)], "qa": [], "msdial": []},
                    "logs": [],
                    "exit_code": 0,
                }
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                validation = self._post(
                    server.server_port, "/api/mztab/validate", {"job_id": job_id}
                )["validation"]
                self.assertEqual(1, validation["summary"]["file_count"])
                self.assertEqual(str(current_mztab.resolve()), validation["files"][0]["file"])

                preview = self._post(
                    server.server_port, "/api/mztab/preview", {"job_id": job_id}
                )["preview"]
                self.assertEqual(1, len(preview["files"]))
                self.assertTrue(Path(preview["files"][0]).samefile(current_mztab))

                with self.assertRaises(urllib.error.HTTPError) as error:
                    self._post(server.server_port, "/api/qa/report", {"job_id": job_id})
                payload = json.loads(error.exception.read().decode("utf-8"))
                error.exception.close()
                self.assertIn("did not create or update", payload["error"])

                with self.assertRaises(urllib.error.HTTPError) as error:
                    self._post(
                        server.server_port,
                        "/api/publication/report",
                        {
                            "job_id": job_id,
                            "run_qa": True,
                            "qa_file_path": str(old_qa),
                            "use_saved_run": True,
                        },
                    )
                payload = json.loads(error.exception.read().decode("utf-8"))
                error.exception.close()
                self.assertIn("was not created or updated by job", payload["error"])

                publication = self._post(
                    server.server_port,
                    "/api/publication/report",
                    {"job_id": job_id, "run_qa": False, "use_saved_run": True},
                )
                self.assertFalse(publication["qa_included"])
                self.assertEqual("", publication["qa_file"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                with JOBS_LOCK:
                    JOBS.pop(job_id, None)


if __name__ == "__main__":
    unittest.main()
