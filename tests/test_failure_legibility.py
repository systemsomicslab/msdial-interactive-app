"""A failure an unattended caller cannot read is a failure it cannot act on.

Two guards live here. The backend already answers a rejected call with an actionable sentence, and
the MCP boundary used to discard it, so a caller saw only "Error executing tool <name>". And the run
job used to declare success on the Console's exit code alone, while the list of exports it had
planned was computed and read by nothing -- so a run that silently skipped inputs published a matrix
describing more samples than it contained.
"""

from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from io import BytesIO
from pathlib import Path
from unittest import mock

from msdial_app import mcp_server
from msdial_app.server import _verify_expected_exports


def _http_error(status: int, payload: object) -> urllib.error.HTTPError:
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    return urllib.error.HTTPError(
        url="http://127.0.0.1:8765/api/agent/tuning/run",
        code=status,
        msg="error",
        hdrs=None,
        fp=BytesIO(raw),
    )


class BackendFailureDetailTests(unittest.TestCase):
    def test_the_backend_message_survives_the_boundary(self):
        # The exact case from the smoke run: the actionable instruction existed server-side and was
        # only recovered by bypassing MCP and calling the HTTP endpoint directly.
        message = "Complete the guided questions and choose target_peak_count before diagnostic tuning."
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=_http_error(400, {"error": message, "trace": "ValueError: ..."}),
        ):
            with self.assertRaises(mcp_server.MsdialRequestError) as caught:
                mcp_server._request_json("POST", "/api/agent/tuning/run", body={})

        self.assertEqual(message, caught.exception.detail)
        self.assertEqual(400, caught.exception.status)
        self.assertEqual("validation_error", caught.exception.reason)
        self.assertIn("ValueError", caught.exception.trace)

    def test_a_non_json_body_is_still_reported_verbatim(self):
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(500, b"<html>boom</html>")):
            with self.assertRaises(mcp_server.MsdialRequestError) as caught:
                mcp_server._request_json("GET", "/api/agent/status")

        self.assertIn("boom", caught.exception.detail)
        self.assertEqual("server_error", caught.exception.reason)

    def test_an_unreachable_backend_is_named_as_such(self):
        with mock.patch(
            "urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")
        ):
            with self.assertRaises(mcp_server.MsdialRequestError) as caught:
                mcp_server._request_json("GET", "/api/agent/status")

        self.assertEqual("backend_unavailable", caught.exception.reason)
        self.assertIsNone(caught.exception.status)

    def test_a_wrapped_tool_returns_the_reason_instead_of_raising(self):
        message = "Complete the guided questions and choose target_peak_count before diagnostic tuning."

        @mcp_server._structured_validation_errors
        def tool():
            raise mcp_server.MsdialRequestError(
                message, status=400, endpoint="POST /api/agent/tuning/run", trace="ValueError: ..."
            )

        result = tool()

        self.assertEqual(False, result["ok"])
        self.assertEqual("validation_error", result["reason"])
        self.assertEqual(message, result["detail"])
        self.assertEqual(400, result["http_status"])
        self.assertEqual("POST /api/agent/tuning/run", result["endpoint"])

    def test_every_tool_carries_the_wrapper(self):
        # Two of thirty-five were wrapped. The reason is equally unreachable from all of them, and a
        # caller cannot know which tools answer with a reason and which raise past the boundary.
        import re

        source = Path(mcp_server.__file__).read_text(encoding="utf-8")
        tools = re.findall(
            r"@mcp\.tool\(\)\s*\n(@_structured_validation_errors\s*\n)?def (\w+)", source
        )
        bare = [name for decorator, name in tools if not decorator]

        self.assertGreater(len(tools), 30, "the tool surface should not have shrunk")
        self.assertEqual([], bare)


class ExpectedExportVerificationTests(unittest.TestCase):
    def test_a_complete_run_reports_no_shortfall(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = []
            for index in range(3):
                path = root / f"sample_{index + 1}.mdpeak"
                path.write_text("Peak ID\n", encoding="ascii")
                expected.append(str(path))

            result = _verify_expected_exports({"expected_analysis_exports": expected})

            self.assertEqual({"expected": 3, "produced": 3, "missing": []}, result)

    def test_a_skipped_input_is_visible_as_a_shortfall(self):
        # MS-DIAL Console exits 0 having skipped a file it could not read, so nothing else in the
        # pipeline would notice that thirty inputs became twenty-nine outputs.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = []
            for index in range(3):
                path = root / f"sample_{index + 1}.mdpeak"
                if index != 2:
                    path.write_text("Peak ID\n", encoding="ascii")
                expected.append(str(path))

            result = _verify_expected_exports({"expected_analysis_exports": expected})

            self.assertEqual(3, result["expected"])
            self.assertEqual(2, result["produced"])
            self.assertEqual([expected[2]], result["missing"])

    def test_a_run_with_no_expected_exports_is_not_a_shortfall(self):
        self.assertEqual(
            {"expected": 0, "produced": 0, "missing": []}, _verify_expected_exports({})
        )


if __name__ == "__main__":
    unittest.main()
