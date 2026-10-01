"""Which encoding of a sample is analysed is the Catalog's rule, decided here exactly as there.

The Catalog decides it wherever a repository lists its files; the lease decides it for what only an archive
showed (msdial_app.encoding_preference). The two are held to one specification by the Catalog's own test
vectors, every case of which is decided here.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from msdial_app import encoding_preference

# The Catalog's shared vectors, copied byte for byte from msdial_repository_catalog's
# tests/vectors/encoding_preference.v1.json (Catalog 0.6.1, commit 1fa064b; sha256 6ac2329ece64ea3c...). A
# change to the rule is a change to both copies.
VECTORS = Path(__file__).resolve().parent / "vectors" / "encoding_preference.v1.json"


class TheEncodingRuleIsTheCatalogs(unittest.TestCase):
    """encoding_preference decides every case of the vectors the Catalog decides, as the Catalog does."""

    def test_every_vector_case(self) -> None:
        vectors = json.loads(VECTORS.read_text(encoding="utf-8"))
        self.assertEqual("msdial-encoding-preference-vectors.v1", vectors["schema"])
        self.assertTrue(vectors["cases"])
        for case in vectors["cases"]:
            with self.subTest(case=case["name"]):
                self.assertEqual(case["roles"], encoding_preference.prefer_encodings(case["files"]))
                self.assertEqual(
                    sorted(case.get("requires_conversion") or []),
                    sorted(path for path in case["files"] if encoding_preference.requires_conversion(path)),
                )
                if "conversion_target" in case:
                    self.assertEqual(
                        case["conversion_target"],
                        {path: "mzML" for path in case["files"] if encoding_preference.is_convertible(path)},
                    )
                for packed, inner in (case.get("unpacks_to") or {}).items():
                    self.assertEqual(inner, encoding_preference.unpacked(packed))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
