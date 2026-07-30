import unittest

from msdial_app.literature import (
    build_search_query,
    confidence_label,
    parse_crossref_works,
)


class LiteratureTests(unittest.TestCase):
    def test_build_search_query_uses_workflow_context(self) -> None:
        query = build_search_query(
            {
                "target_omics": "Lipidomics",
                "ion_mode": "Negative",
                "files": [
                    {
                        "vendor": "SCIEX",
                        "instrument_family": "QTOF",
                    }
                ],
            }
        )
        self.assertIn('"MS-DIAL"', query)
        self.assertIn("SCIEX", query)
        self.assertIn("QTOF", query)
        self.assertIn("Lipidomics", query)

    def test_crossref_parser_keeps_explicit_open_access_msdial_records(self) -> None:
        works = parse_crossref_works(
            [
                {
                    "DOI": "10.1000/example",
                    "title": ["MS-DIAL lipidomics with mass slice width 0.1"],
                    "abstract": "<jats:p>Minimum peak height was 100.</jats:p>",
                    "published": {"date-parts": [[2024]]},
                    "is-referenced-by-count": 25,
                    "license": [
                        {"URL": "https://creativecommons.org/licenses/by/4.0/"}
                    ],
                },
                {
                    "DOI": "10.1000/closed",
                    "title": ["MS-DIAL closed record"],
                    "is-referenced-by-count": 100,
                    "license": [],
                },
            ]
        )
        self.assertEqual(1, len(works))
        self.assertEqual("https://doi.org/10.1000/example", works[0]["url"])
        self.assertIn("mass slice width", works[0]["direct_parameter_terms"])
        self.assertEqual("high", works[0]["confidence"])

    def test_confidence_requires_direct_parameter_evidence_for_high(self) -> None:
        self.assertNotEqual("high", confidence_label(1000, True, 0))
        self.assertEqual("high", confidence_label(100, True, 2))


if __name__ == "__main__":
    unittest.main()
