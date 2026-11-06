"""A declared vendor raw file whose depositor deposited only its mzML is that mzML (converted_member_stem).

Metabolomics Workbench ST004806 declares Rahman_m25-L025_RIIBKO_10.raw in a sample row, and its study archive,
ST004806_mzml.zip, holds Rahman_m25-L025_RIIBKO_10.mzML beside blanks and pooled MS/MS runs no row names. No rule
paired the mzML with its row: a name a row records with an extension is never matched by its stem, and every name of
the study shares its leading identifier. In the Stage 3b campaign of 2026-11-06 the attribute stage admitted nothing,
and the unit failed after its download ("Refusing to fall back to accession-level inputs"), as did 17 other units of
eight such studies.

The pairing is one sample's on both sides and never crosses a polarity, and it records the conversion as the mzML's
own header states it. What the record cannot pair is still refused: ST004751's row that names two files in one field,
and ST004757's rows that name only ".RAW".
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_CONFIG = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"LOCALAPPDATA": _CONFIG.name}):
    from msdial_app.repository_analysis_rows import build_repository_analysis_rows
    from msdial_app.repository_reanalysis import (
        CONVERTED_MEMBER_STEM_PAIRING,
        INFERRED_PAIRING_WARNING,
        LEADING_IDENTIFIER_TOKEN_PAIRING,
        RepositoryFile,
        _file_key,
        _member_name_pairings,
        _path_on_disk,
        deposited_conversion,
    )

from test_replicate_injections import SHARED, _st001264, _Workspace


def _mzml(source: str = "", *, software: bool = True) -> bytes:
    """A deposited mzML whose header names ``source`` as the file it was converted from (none where empty)."""
    sources = (
        '<sourceFileList count="1">\n'
        f'<sourceFile id="RAW1" name="{source}" location="file:///D:/deposit">\n'
        '<cvParam cvRef="MS" accession="MS:1000768" name="Thermo nativeID format"/>\n'
        '<cvParam cvRef="MS" accession="MS:1000563" name="Thermo RAW format"/>\n'
        '<cvParam cvRef="MS" accession="MS:1000569" name="SHA-1" value="0123456789abcdef0123456789abcdef01234567"/>\n'
        "</sourceFile>\n</sourceFileList>\n"
        if source
        else ""
    )
    tools = (
        '<softwareList count="1">\n<software id="pwiz" version="3.0.21193">\n'
        '<cvParam cvRef="MS" accession="MS:1000615" name="ProteoWizard software"/>\n</software>\n</softwareList>\n'
        if software
        else ""
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<mzML xmlns="http://psi.hupo.org/ms/mzml" version="1.1.0">\n'
        f'<fileDescription><fileContent/>{sources}</fileDescription>\n{tools}'
        '<run id="r1">\n<spectrumList count="0">\n</spectrumList>\n</run>\n</mzML>\n'
    ).encode("utf-8")


RAHMAN = "Rahman_m25-L025_"
ST004806_SAMPLES = [
    ("S7", f"{RAHMAN}RIIBKO_07.raw"), ("S8", f"{RAHMAN}RIIBKO_08.raw"), ("S9", f"{RAHMAN}RIIBKO_09.raw"),
    ("RIIBKO_Unl", f"{RAHMAN}RIIBKO_unlabeled-FullMS-1.raw"),
]
ST004806_EXTRAS = [
    "Rahman_m25-L025-80MeOH-4ul-1.mzML", "Rahman_m25-L025-80MeOH-4ul-2.mzML",
    f"{RAHMAN}RIIBKO_unlabeled-NegMSMS-1.mzML", f"{RAHMAN}RIIBKO_unlabeled-PosMSMS-1.mzML",
]
ST004806_MEMBERS = [raw[: -len(".raw")] + ".mzML" for _sample, raw in ST004806_SAMPLES] + ST004806_EXTRAS


class _Pairing(_Workspace):
    def pairings(self, members: list[str], samples: list[tuple[str, str]], **project_changes) -> dict:
        """_member_name_pairings over these member names: {"paired": {member: (declared, rule)}, "refused": [...]}."""
        data_root = self.root / "raw" / "data"
        for name in members:
            path = data_root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_mzml(Path(name).stem + ".raw") if name.lower().endswith(".mzml") else b"raw bytes")
        project, _ = _st001264(members, samples)
        for key, value in project_changes.items():
            setattr(project, key, value)
        extracted = {_file_key(str(data_root / name)): {"path": name} for name in members}
        found = _member_name_pairings(project, extracted, data_root)
        names = {_file_key(str(data_root / name)): name for name in members}
        return {
            "paired": {names[key]: (item["declared_raw_file"], item["paired_by"]) for key, item in found["paired"].items()},
            "refused": found["refused"],
        }


class ADeclaredVendorFileIsItsDepositedMzml(_Pairing):
    def test_st004806s_rows_take_their_own_mzml_and_nothing_else(self) -> None:
        found = self.pairings(ST004806_MEMBERS, ST004806_SAMPLES)

        self.assertEqual(
            {raw[: -len(".raw")] + ".mzML": (raw, CONVERTED_MEMBER_STEM_PAIRING) for _sample, raw in ST004806_SAMPLES},
            found["paired"],
        )
        # The blanks and pooled MS/MS runs share the study's leading identifier. The token rule's refusals of them
        # for names their own mzML now carries say nothing, and are not on record.
        self.assertEqual([], found["refused"])

    def test_a_stem_never_crosses_a_polarity_token(self) -> None:
        """x_pos.raw takes x_pos.mzML and never x_neg.mzML; a name of the other polarity is refused, on record."""
        samples = [("p", "QC1_pos.raw"), ("n", "QC1_neg.raw")]
        found = self.pairings(["QC1_pos.mzML", "QC1_neg.mzML"], samples, ion_mode="Positive")

        self.assertEqual({"QC1_pos.mzML": ("QC1_pos.raw", CONVERTED_MEMBER_STEM_PAIRING)}, found["paired"])
        self.assertEqual(
            [("QC1_neg.mzML", "QC1_neg.raw", "polarity_token_contradicts_ion_mode")],
            [(entry["member_name"], entry["declared_raw_file"], entry["reason"]) for entry in found["refused"]],
        )

    def test_one_stem_in_two_polarity_folders_is_two_samples_and_pairs_neither(self) -> None:
        found = self.pairings(["POS/S1.mzML", "NEG/S1.mzML"], [("s", "S1.raw")])

        self.assertEqual({}, found["paired"])
        self.assertEqual(
            {"member_stem_not_one_sample"}, {entry["reason"] for entry in found["refused"]}
        )

    def test_a_stem_two_sample_rows_record_pairs_neither(self) -> None:
        found = self.pairings(["S1.mzML"], [("a", "S1.raw"), ("b", "S1.wiff")])

        self.assertEqual({}, found["paired"])
        self.assertEqual(["declared_stem_not_one_sample"], [entry["reason"] for entry in found["refused"]])

    def test_the_vendor_file_itself_wins_and_its_mzml_is_not_paired(self) -> None:
        """Where the declared file is there, its mzML is another encoding of it, for the one encoding rule."""
        self.assertEqual({}, self.pairings(["S1.raw", "S1.mzML"], [("s", "S1.raw")])["paired"])

    def test_a_declared_name_the_listing_names_itself_is_not_paired(self) -> None:
        listed = [RepositoryFile("S1.raw", 1, "https://example.org/S1.raw")]
        self.assertEqual({}, self.pairings(["S1.mzML"], [("s", "S1.raw")], files=listed)["paired"])

    def test_only_a_vendor_encoding_is_paired_with_an_mzml(self) -> None:
        """A row naming a netCDF or an mzXML is no vendor file whose mzML the depositor could have deposited."""
        # Names with no digit have no leading identifier, so the token rule pairs nothing here either.
        found = self.pairings(["Alpha.mzML", "Beta.mzML"], [("a", "Alpha.cdf"), ("b", "Beta.mzXML")])
        self.assertEqual({}, found["paired"])
        self.assertEqual({"Gamma.mzML": ("Gamma.raw", CONVERTED_MEMBER_STEM_PAIRING)},
                         self.pairings(["Gamma.mzML"], [("g", "Gamma.raw")])["paired"])

    def test_a_row_the_token_rule_pairs_keeps_its_pairing(self) -> None:
        """VV_13_HEpG2_C1_pos.raw's own-stem mzML, alone under its key, is paired by the key as it always was."""
        found = self.pairings(["VV_13_HEpG2_C1_pos.mzML"], [("c1", "VV_13_HEpG2_C1_pos.raw")])

        self.assertEqual(
            {"VV_13_HEpG2_C1_pos.mzML": ("VV_13_HEpG2_C1_pos.raw", LEADING_IDENTIFIER_TOKEN_PAIRING)}, found["paired"]
        )

    def test_st004751s_row_that_names_two_files_in_one_field_pairs_nothing(self) -> None:
        samples = [("QC1", "QC1_neg.raw QC1_pos.raw"), ("C1", "C1_neg.raw C1_pos.raw")]
        self.assertEqual({}, self.pairings(["QC1_pos.mzML"], samples, ion_mode="Positive")["paired"])

    def test_st004757s_rows_that_name_only_an_extension_pair_nothing(self) -> None:
        samples = [("b1_neg1", ".RAW"), ("b1_pos1", ".RAW")]
        self.assertEqual({}, self.pairings(["b1_neg1.mzML", "b1_pos1.mzML"], samples)["paired"])

    def test_a_unit_whose_catalog_declared_its_inputs_is_matched_by_path_and_never_by_stem(self) -> None:
        declared = [{"path": "S1.raw", "kind": "file", "sample_id": "s"}]
        self.assertEqual({}, self.pairings(["S1.mzML"], [("s", "S1.raw")], analysis_inputs=declared)["paired"])


class TheConversionIsOnRecord(_Workspace):
    def test_the_header_names_the_source_file_and_the_software(self) -> None:
        path = self.root / "S1.mzML"
        path.write_bytes(_mzml("S1.raw"))
        record = deposited_conversion(path, "S1.raw")

        self.assertEqual("depositor", record["converted_by"])
        self.assertEqual((".raw", "mzML", True, True),
                         (record["declared_encoding"], record["input_encoding"], record["header_read"],
                          record["source_file_agrees"]))
        self.assertEqual(["S1.raw"], [source["name"] for source in record["source_files"]])
        self.assertIn({"name": "SHA-1", "value": "0123456789abcdef0123456789abcdef01234567"},
                      record["source_files"][0]["cv_params"])
        self.assertEqual([{"id": "pwiz", "version": "3.0.21193", "names": ["ProteoWizard software"]}], record["software"])

    def test_a_header_that_names_another_file_is_recorded_and_decides_nothing(self) -> None:
        path = self.root / "S1.mzML"
        path.write_bytes(_mzml("20190101_run12.raw"))
        self.assertIs(False, deposited_conversion(path, "S1.raw")["source_file_agrees"])

    def test_a_folder_of_the_location_may_name_the_source(self) -> None:
        """A Waters .raw is a folder: ProteoWizard names its _FUNC001.DAT, in the folder's location."""
        path = self.root / "S1.mzML"
        path.write_bytes(_mzml("_FUNC001.DAT").replace(b"file:///D:/deposit", b"file:///D:/deposit/S1.raw"))
        self.assertIs(True, deposited_conversion(path, "S1.raw")["source_file_agrees"])

    def test_a_casefolded_key_is_found_in_its_case_on_a_case_sensitive_file_system(self) -> None:
        data_root = self.root / "raw" / "data"
        path = data_root / "Sub" / "Rahman_S1.mzML"
        path.parent.mkdir(parents=True)
        path.write_bytes(_mzml("Rahman_S1.raw"))
        # As on Linux: the casefolded key itself names no file.
        with patch.object(Path, "exists", return_value=False):
            found = _path_on_disk(_file_key(str(path)), data_root)
        self.assertEqual(["Sub", "Rahman_S1.mzML"], list(found.relative_to(data_root).parts))

    def test_a_header_with_no_source_file_or_no_mzml_says_so(self) -> None:
        path = self.root / "S1.mzML"
        path.write_bytes(_mzml("", software=False))
        self.assertEqual((True, None), (deposited_conversion(path, "S1.raw")["header_read"],
                                        deposited_conversion(path, "S1.raw")["source_file_agrees"]))
        path.write_bytes(b"not an mzML")
        self.assertEqual((False, None), (deposited_conversion(path, "S1.raw")["header_read"],
                                         deposited_conversion(path, "S1.raw")["source_file_agrees"]))


class TheLeaseRunsTheDepositedMzml(_Workspace):
    def unit(self) -> dict:
        contents = {name: _mzml(Path(name).stem + ".raw") for name in ST004806_MEMBERS}
        project, payloads = _st001264(ST004806_MEMBERS, ST004806_SAMPLES, contents)
        project.download_scope = SHARED
        return self.lease(project, payloads)

    def test_an_st004806_shaped_unit_leases_each_rows_mzml_with_its_conversion(self) -> None:
        manifest = self.unit()

        self.assertEqual(sorted(raw[: -len(".raw")] + ".mzML" for _sample, raw in ST004806_SAMPLES),
                         sorted(Path(item).name for item in manifest["input_candidates"]))
        rows = {Path(row["path"]).name: row for row in manifest["input_lineage"]["rows"]}
        row = rows[f"{RAHMAN}RIIBKO_07.mzML"]
        self.assertEqual("S7", row["sample_id"])
        pairing = row["name_pairing"]
        self.assertEqual((f"{RAHMAN}RIIBKO_07.raw", f"{RAHMAN}RIIBKO_07.mzML", CONVERTED_MEMBER_STEM_PAIRING),
                         (pairing["declared_raw_file"], pairing["member_name"], pairing["paired_by"]))
        self.assertEqual(("depositor", True), (pairing["conversion"]["converted_by"],
                                               pairing["conversion"]["source_file_agrees"]))
        attribute = next(entry for entry in manifest["lease_stages"] if entry["stage"] == "attribute")
        self.assertEqual(4, attribute["converted_member_stem_pairings"])
        self.assertIn(INFERRED_PAIRING_WARNING, manifest["warnings"])
        listed = {entry["member_name"]: entry for entry in manifest["input_name_pairings"]["paired"]}
        self.assertEqual("depositor", listed[f"{RAHMAN}RIIBKO_07.mzML"]["conversion"]["converted_by"])
        # The blanks and pooled MS/MS runs no row names are another unit's, or none's: a shared archive takes none.
        self.assertEqual((False, "shared_archive"), (manifest["unattributed_members"]["applied"],
                                                    manifest["unattributed_members"]["reason"]))

    def test_the_csv_gives_every_row_its_mzml_and_says_how(self) -> None:
        built = build_repository_analysis_rows(self.unit())

        self.assertEqual([], built["failures"])
        self.assertEqual([], built["samples_without_input"])
        self.assertEqual(
            sorted((sample, raw, CONVERTED_MEMBER_STEM_PAIRING) for sample, raw in ST004806_SAMPLES),
            sorted((row["sample_id"], row["sample_raw_file"], row["raw_file_paired_by"]) for row in built["rows"]),
        )

    def test_without_a_member_of_any_rows_stem_the_lease_still_refuses_to_fall_back(self) -> None:
        samples = [("QC1", "QC1_neg.raw QC1_pos.raw"), ("C1", "C1_neg.raw C1_pos.raw")]
        project, payloads = _st001264(["QC1_pos.mzML"], samples, {"QC1_pos.mzML": _mzml("QC1_pos.raw")})

        with self.assertRaisesRegex(ValueError, "Refusing to fall back to accession-level inputs"):
            self.lease(project, payloads)


if __name__ == "__main__":
    unittest.main()
