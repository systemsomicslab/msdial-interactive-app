"""A restart that cannot load new source has to say so."""

from __future__ import annotations

import unittest
from unittest import mock

from msdial_app import mcp_server


class RestartCodeNoteTests(unittest.TestCase):
    def test_changed_source_is_named_and_warned_about(self):
        with mock.patch.object(mcp_server, "_PROCESS_STARTED_AT", 0.0):
            note = mcp_server._restart_code_note()

        self.assertFalse(note["code_reloaded"])
        self.assertIn("mcp_server.py", note["source_changed_since_process_start"])
        self.assertIn("Reconnect", note["warnings"][0])

    def test_unchanged_source_carries_no_warning(self):
        with mock.patch.object(mcp_server, "_PROCESS_STARTED_AT", float("inf")):
            note = mcp_server._restart_code_note()

        self.assertFalse(note["code_reloaded"])
        self.assertEqual([], note["source_changed_since_process_start"])
        self.assertNotIn("warnings", note)


if __name__ == "__main__":
    unittest.main()
