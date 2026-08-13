import hashlib
import os
import tempfile
import unittest
from unittest.mock import patch

from msdial_app.library_catalog import LIBRARY_CATALOG, catalog_status, download_library, library_path
from msdial_app.user_settings import load_user_settings, save_path_settings, settings_path


class UserResourceTests(unittest.TestCase):
    def test_path_settings_round_trip_in_user_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"LOCALAPPDATA": temporary}
        ):
            saved = save_path_settings(
                {
                    "console_path": "C:/MSDIAL/MSDIALCUI.exe",
                    "template_path": "C:/MSDIAL/method.txt",
                    "queries_path": "C:/MSDIAL/LbmQueries.txt",
                    "ignored": "do not persist",
                }
            )

            self.assertTrue(settings_path().is_file())
            self.assertEqual("C:/MSDIAL/MSDIALCUI.exe", saved["console_path"])
            self.assertNotIn("ignored", load_user_settings())

    def test_official_library_catalog_has_verified_zenodo_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"LOCALAPPDATA": temporary}
        ):
            status = catalog_status()

            self.assertEqual(5, len(LIBRARY_CATALOG))
            self.assertEqual(5, len(status))
            self.assertTrue(all(item["record_url"].startswith("https://zenodo.org/records/") for item in status))
            self.assertTrue(all(item["doi"].startswith("10.5281/zenodo.") for item in status))
            self.assertTrue(all(len(item["md5"]) == 32 for item in status))
            self.assertTrue(all(item["license"] == "CC BY 4.0" for item in status))

    def test_verified_existing_library_gets_persistent_metadata(self) -> None:
        content = b"verified library"
        item = {
            "id": "test-library",
            "kind": "msp",
            "record_id": 123,
            "filename": "test.msp",
            "size": len(content),
            "md5": hashlib.md5(content).hexdigest(),
        }
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"LOCALAPPDATA": temporary}
        ), patch("msdial_app.library_catalog.LIBRARY_CATALOG", (item,)):
            target = library_path(item)
            target.parent.mkdir(parents=True)
            target.write_bytes(content)

            result = download_library("test-library")

            self.assertTrue(result["reused"])
            self.assertEqual(item["md5"], result["md5"])
            self.assertTrue((target.parent / "zenodo-record.json").is_file())
            self.assertTrue(catalog_status()[0]["downloaded"])


if __name__ == "__main__":
    unittest.main()
