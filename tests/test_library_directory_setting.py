"""Where the spectral libraries live must be something a site can choose.

The location was computed and never read from anywhere: user_data_directory() / "libraries",
which on Windows is under LOCALAPPDATA and therefore on the system drive. The public MS/MS
libraries alone come to 1.2 GB there -- 378 MB positive, 48 MB negative, 749 MB lipid -- before
any laboratory library is added, and a site whose data drive is not C: had no way to say so.

Raised on 2026-09-20 by the analyst, whose instruction was simply: do not do this on C.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from msdial_app import library_catalog
from msdial_app.user_settings import PATH_SETTING_KEYS


class LibraryDirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.environment = mock.patch.dict(os.environ, {}, clear=False)
        self.environment.start()
        os.environ.pop("MSDIAL_LIBRARY_DIRECTORY", None)

    def tearDown(self) -> None:
        self.environment.stop()
        self.directory.cleanup()

    def _with_settings(self, settings: dict):
        return mock.patch("msdial_app.user_settings.load_user_settings", return_value=settings)

    def test_the_default_is_unchanged_when_nobody_says_otherwise(self) -> None:
        """Existing installations must keep finding the libraries they already downloaded."""
        with self._with_settings({}):
            with mock.patch.object(library_catalog, "user_data_directory", return_value=self.root):
                self.assertEqual(self.root / "libraries", library_catalog.library_directory())

    def test_a_saved_setting_moves_the_libraries_off_the_system_drive(self) -> None:
        """THE POINT. A site with a data drive can name it."""
        chosen = self.root / "data" / "libraries"

        with self._with_settings({"library_directory": str(chosen)}):
            self.assertEqual(chosen, library_catalog.library_directory())
        self.assertTrue(chosen.is_dir(), "and the directory is created rather than merely returned")

    def test_an_environment_variable_works_without_touching_the_settings_file(self) -> None:
        """For a launcher or a scheduled job that should not rewrite a person's settings."""
        chosen = self.root / "from-environment"
        os.environ["MSDIAL_LIBRARY_DIRECTORY"] = str(chosen)

        with self._with_settings({}):
            self.assertEqual(chosen, library_catalog.library_directory())

    def test_the_saved_setting_outranks_the_environment(self) -> None:
        """What a person chose through the application wins over what a launcher exported."""
        saved = self.root / "chosen-by-a-person"
        os.environ["MSDIAL_LIBRARY_DIRECTORY"] = str(self.root / "exported-by-a-script")

        with self._with_settings({"library_directory": str(saved)}):
            self.assertEqual(saved, library_catalog.library_directory())

    def test_an_unusable_setting_falls_back_rather_than_failing(self) -> None:
        """A disconnected network share must not stop an analysis whose libraries are elsewhere.

        The libraries may already be downloaded under the default, so refusing to resolve a path
        would break a run that had everything it needed.
        """
        blocking_file = self.root / "not-a-directory"
        blocking_file.write_text("this is a file", encoding="utf-8")

        with self._with_settings({"library_directory": str(blocking_file / "libraries")}):
            with mock.patch.object(library_catalog, "user_data_directory", return_value=self.root):
                self.assertEqual(self.root / "libraries", library_catalog.library_directory())

    def test_blank_and_whitespace_are_not_a_choice(self) -> None:
        with mock.patch.object(library_catalog, "user_data_directory", return_value=self.root):
            for empty in ("", "   ", None):
                with self._with_settings({"library_directory": empty}):
                    self.assertEqual(self.root / "libraries", library_catalog.library_directory())

    def test_the_setting_is_one_the_application_will_persist(self) -> None:
        """A key save_path_settings does not know about would be silently dropped on save."""
        self.assertIn("library_directory", PATH_SETTING_KEYS)

    def test_a_library_path_is_built_under_whatever_was_chosen(self) -> None:
        """The record-id subdirectory layout has to follow the setting, not the default."""
        chosen = self.root / "elsewhere"

        with self._with_settings({"library_directory": str(chosen)}):
            path = library_catalog.library_path(
                {"record_id": 21901200, "filename": "MSMS-Public_all-pos-VS20.msp"}
            )

        self.assertEqual(chosen / "21901200" / "MSMS-Public_all-pos-VS20.msp", path)


class SavingTheSettingTests(unittest.TestCase):
    def test_it_round_trips_through_the_settings_file(self) -> None:
        from msdial_app import user_settings

        with tempfile.TemporaryDirectory() as temporary:
            settings_file = Path(temporary) / "settings.json"
            with mock.patch.object(user_settings, "settings_path", return_value=settings_file):
                saved = user_settings.save_path_settings(
                    {"library_directory": r"D:\13_MSDIAL_Public_Reanalysis\libraries"}
                )
                written = json.loads(settings_file.read_text(encoding="utf-8"))

        self.assertEqual(r"D:\13_MSDIAL_Public_Reanalysis\libraries", saved["library_directory"])
        self.assertEqual(r"D:\13_MSDIAL_Public_Reanalysis\libraries", written["library_directory"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
