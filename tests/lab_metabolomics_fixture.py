"""A synthetic laboratory LC-MS metabolomics batch: raw folders, three MSP libraries, internal-standard texts,
a Console stand-in, and the files a production run leaves behind. Nothing here runs MS-DIAL."""

from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import patch

from lab_batch_fixture import EVERY_CAPABILITY
from lab_export_fixture import candidate, spot, write_candidates, write_export

from msdial_app import lab_lcms_metabolomics_agent, lab_lipidomics
from msdial_app.lab_lcms_metabolomics_agent import CONTRACT_KEY, PLAN_BLOCK

STEMS = [("S1", "Sample"), ("S2", "Sample"), ("QC1", "QC"), ("Blank", "Blank")]
CLASSES = {"S1": "Case", "S2": "Control", "QC1": "QC", "Blank": "Blank"}
ALIGNMENT = "AlignResult_2026_10_22_12_00_00"
PROJECT = "Project-2610221200"
POSITIVE_STANDARDS = (
    "Name\tMZ\tRT\tAdduct\tInChIKey\tFormula\tSMILES\tOntology\r\n"
    "Methionine-d3\t153.07716\t0.896\t[M+H]+\t\tC5H8D3NO2S\t\tInternal standard\r\n"
    "CUDA\t341.27987\t2.87\t[M+H]+\t\tC19H36N2O3\t\tInternal standard\r\n"
)
NEGATIVE_STANDARDS = (
    "Name\tMZ\tRT\tAdduct\tInChIKey\tFormula\tSMILES\tOntology\r\n"
    "CUDA\t339.26531\t2.87\t[M-H]-\t\tC19H36N2O3\t\tInternal standard\r\n"
)
GLUTAMINE = "ZDXPYRJPNDTMRX-VKHMYHEASA-N"
CITRULLINE = "RHGKLRLOHDJJDR-BYPYZUCNSA-N"
TRYPTOPHAN = "QIVBCDIJIAJPQS-VIFPVBQESA-N"
GLUTAMATE = "WHUUTDBJXJRKMK-VKHMYHEASA-N"

# Cells are S1, S2, QC1, Blank. Met-d3 is in no Blank; CUDA is in every injection.
POSITIVE_SPOTS = [
    dict(alignment_id=1, mz=153.0772, rt=0.90, name="Methionine-d3", method="internal_standards_pos",
         cells=[1000, 2000, 1500, 0]),
    dict(alignment_id=2, mz=341.2799, rt=2.88, name="CUDA", method="internal_standards_pos",
         cells=[5000, 5200, 5100, 4900]),
    dict(alignment_id=10, mz=147.0764, rt=0.70, name="L-Glutamine", method="lib1_in_house", inchikey=GLUTAMINE,
         cells=[4000, 8000, 6000, 10]),
    # The same compound as a sodium adduct, more intense: the preferred [M+H]+ is kept all the same.
    dict(alignment_id=11, mz=169.0584, rt=0.70, name="L-Glutamine", adduct="[M+Na]+", method="lib1_in_house",
         inchikey=GLUTAMINE, cells=[40000, 80000, 60000, 100]),
    dict(alignment_id=12, mz=200.1000, rt=1.50, name="Unknown", cells=[300, 300, 300, 0]),
    # Suggested only: not reference matched.
    dict(alignment_id=13, mz=176.1030, rt=0.65, name="Citrulline", method="lib3_no_rt", inchikey=CITRULLINE,
         cells=[700, 900, 800, 0], matched=False),
    # Annotated, matched, but the library gives no InChIKey: kept as its own row.
    dict(alignment_id=14, mz=250.2000, rt=3.10, name="Compound X", method="lib2_predicted_rt", inchikey="null",
         cells=[100, 200, 150, 0]),
    dict(alignment_id=15, mz=205.0972, rt=3.40, name="L-Tryptophan", method="lib1_in_house", inchikey=TRYPTOPHAN,
         cells=[9000, 9000, 9000, 0]),
]
NEGATIVE_SPOTS = [
    dict(alignment_id=101, mz=339.2653, rt=2.87, name="CUDA", adduct="[M-H]-", method="internal_standards_neg",
         cells=[3000, 3000, 3000, 3000]),
    # Glutamine measured more strongly in negative mode: it wins across the polarities.
    dict(alignment_id=110, mz=145.0619, rt=0.70, name="L-Glutamine", adduct="[M-H]-", method="lib1_in_house",
         inchikey=GLUTAMINE, cells=[90000, 90000, 90000, 0]),
    # Tryptophan as a formate adduct, weaker than positive [M+H]+.
    dict(alignment_id=111, mz=249.0881, rt=3.40, name="L-Tryptophan", adduct="[M+FA-H]-", method="lib1_in_house",
         inchikey=TRYPTOPHAN, cells=[10, 10, 10, 0]),
    dict(alignment_id=112, mz=146.0459, rt=0.75, name="L-Glutamic acid", adduct="[M-H]-", method="lib2_predicted_rt",
         inchikey=GLUTAMATE, cells=[5000, 6000, 5500, 0]),
]


def header_record(path: str | Path, method: str = "DDA", targets: list[float] | None = None,
                  start: str | None = None, polarity: str | None = None,
                  energies: list[float] | None = None) -> dict[str, Any]:
    """One record as RawMetadataConsoleApp writes it for a file, with only what the campaign's summary reads.

    energies: the MS2 collision energies it records (acquisition.collisionEnergies); None records none."""
    record: dict[str, Any] = {
        "source": {"filePath": str(path), "fileName": Path(path).name, "readerName": "ThermoMetadataReader"},
        "instrument": {"model": {"value": "Q Exactive Plus Orbitrap"}},
        "acquisition": {
            "method": {"value": method, "source": "SpectrumStatistics"},
            "separation": {"value": "LiquidChromatography"},
            "msLevels": {"value": [1, 2]},
            "isolationWindowTargets": list(targets or []),
        },
        "run": {},
    }
    if energies is not None:
        record["acquisition"]["collisionEnergies"] = list(energies)
    if polarity:
        record["acquisition"]["polarity"] = {"value": polarity}
    if start:
        record["run"]["acquisitionStartTime"] = {"value": start, "evidence": "Thermo FileHeader.CreationDate"}
    return record


class FakeExtractor:
    """The configured raw-metadata extractor and its reads, stood in for.

    records(path) gives a file's record, None for a file the extractor cannot read, or "late" for one the
    check's deadline leaves unread. calls lists the files each read was given, options the keyword arguments
    and work_directories the folder it was given to work in. during(path, work_directory) runs as each file
    is read, and created(path) names the files a reader left in a folder input.
    """

    def __init__(self, records: Any = None, configured: bool = True, during: Any = None, created: Any = None,
                 inventory: str = "a" * 64) -> None:
        self.records = records or (lambda path: header_record(path))
        self.configured = configured
        self.during = during
        self.created = created
        self.inventory = inventory
        self.calls: list[list[str]] = []
        self.options: list[dict[str, Any]] = []
        self.work_directories: list[Path] = []

    def run(self, extractor: Path, inputs: list[Path], work_directory: Path, **options: Any) -> dict[str, Any]:
        from msdial_app.raw_metadata_preflight import file_key, input_signature

        self.calls.append([Path(path).name for path in inputs])
        self.options.append(dict(options))
        self.work_directories.append(Path(work_directory))
        records, outcomes = [], {}
        for path in inputs:
            if self.during is not None:
                self.during(Path(path), Path(work_directory))
            record = self.records(path)
            created = [str(item) for item in (self.created(Path(path)) if self.created else [])]
            if record == "late":
                outcomes[file_key(path)] = {"outcome": "timed_out", "exit_code": None, "deadline": True,
                                            "not_started": True, "reader_created_files": created}
            elif record is None:
                outcomes[file_key(path)] = {"outcome": "failed", "exit_code": 1, "reader_created_files": created}
            else:
                records.append(record)
                outcomes[file_key(path)] = {"outcome": "ok", "exit_code": 0, "reader_created_files": created,
                                            "input_signature": input_signature(path)}
        return {"records": records, "outcomes": outcomes}

    def identity(self, path: Any) -> dict[str, Any]:
        return {"binary_sha256": "b" * 64, "inventory_sha256": self.inventory, "file_count": 3,
                "product_version": "1.0.0+test", "provenance_status": "verified"}

    def patches(self) -> ExitStack:
        stack = ExitStack()
        stack.enter_context(patch.object(
            lab_lcms_metabolomics_agent, "_configured_extractor",
            return_value={"path": "C:/extractor/RawMetadataConsoleApp.exe", "source": "setting"}
            if self.configured else {"path": "", "source": ""},
        ))
        stack.enter_context(patch.object(lab_lcms_metabolomics_agent, "run_extractor", side_effect=self.run))
        stack.enter_context(patch.object(lab_lcms_metabolomics_agent, "_extractor_identity", side_effect=self.identity))
        for cache in ("_HEADER_CACHE", "_HEADER_CREATED", "_HEADER_DEFERRED", "_HEADER_IN_FLIGHT", "_EXTRACTOR_IDENTITIES"):
            stack.enter_context(patch.dict(getattr(lab_lcms_metabolomics_agent, cache), clear=True))
        return stack


def capabilities_patches(found: dict[str, Any] | None = None, extractor: FakeExtractor | None = None) -> ExitStack:
    stack = ExitStack()
    value = found if found is not None else EVERY_CAPABILITY
    stack.enter_context(patch("msdial_app.workflow.console_capabilities", return_value=value))
    stack.enter_context(patch.object(lab_lcms_metabolomics_agent, "console_capabilities", return_value=value))
    real = lab_lipidomics.analysis_root_problems
    stack.enter_context(patch.object(
        lab_lcms_metabolomics_agent, "analysis_root_problems", side_effect=lambda root: real(root, temporary=[])
    ))
    # Every header reads as DDA unless a test gives its own extractor.
    stack.enter_context((extractor or FakeExtractor()).patches())
    return stack


class MetabolomicsBatch:
    def __init__(self, root: Path, polarities: tuple[str, ...] = ("Positive",), stems: list[tuple[str, str]] = STEMS) -> None:
        self.root = root
        self.stems = list(stems)
        self.polarities = polarities
        data = root / "data"
        self.folders: dict[str, Path] = {}
        for polarity in polarities:
            label = "Pos" if polarity == "Positive" else "Neg"
            folder = data / label.lower()
            folder.mkdir(parents=True)
            for stem, _ in self.stems:
                (folder / f"{stem}_{label}.mzML").write_text("", encoding="ascii")
            self.folders[polarity] = folder
        self.is_pos = data / "IS_POS.txt"
        self.is_neg = data / "IS_NEG.txt"
        self.is_pos.write_bytes(POSITIVE_STANDARDS.encode("ascii"))
        self.is_neg.write_bytes(NEGATIVE_STANDARDS.encode("ascii"))
        library = root / "lib"
        library.mkdir()
        self.libraries = {}
        for slot in ("in_house", "predicted_rt", "no_rt"):
            path = library / f"{slot}.msp"
            # The libraries searched with retention time give one; the third gives none.
            rt = "" if slot == "no_rt" else "1.25"
            path.write_text(f"NAME: a {slot} record\nPRECURSORMZ: 100\nRETENTIONTIME: {rt}\nNum Peaks: 0\n",
                            encoding="ascii")
            self.libraries[slot] = path
        self.console = root / "console" / "MSDIALCUI.exe"
        self.console.parent.mkdir()
        self.console.write_bytes(b"not a console")
        self.analysis = root / "analysis"

    def request(self, **extra: Any) -> dict[str, Any]:
        request: dict[str, Any] = {
            "annotation": {"preset": "lab_rt_tiers", "libraries": {slot: str(path) for slot, path in self.libraries.items()}},
            "solvent_type": "formic acid",
            "analysis_root": str(self.analysis), "analysis_purpose": "case versus control breath metabolome",
            "console_path": str(self.console), "raw_location": "copy",
            "duplicate_rule": "intensity", "inchikey_match": "first_block",
        }
        if "Positive" in self.polarities:
            request.update(pos_path=str(self.folders["Positive"]), is_text_pos=str(self.is_pos),
                           normalizing_standard_pos="Methionine-d3")
        if "Negative" in self.polarities:
            request.update(neg_path=str(self.folders["Negative"]), is_text_neg=str(self.is_neg),
                           normalizing_standard_neg="CUDA")
        request.update(extra)
        return {key: value for key, value in request.items() if value is not None}

    def contract(self, **extra: Any) -> str:
        check = lab_lcms_metabolomics_agent.check_inputs(self.request(**extra))
        assert check["ready_for_contract"], (check["questions"], check["blockers"])
        written = lab_lcms_metabolomics_agent.write_contract({
            **self.request(**extra), "check_digest": check["check_digest"], "sample_table_confirmed": True,
            "sample_table": [
                {"stem": stem, "class_id": CLASSES[stem], **({} if kind == "Sample" else {"file_type": kind})}
                for stem, kind in self.stems
            ],
            "confirmed_by": "the analyst", "confirmed": True,
        })
        assert written["written"], written
        return written["contract_path"]


def _samples(label: str, stems: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    return [(f"{stem}_{label}", CLASSES[stem], kind) for stem, kind in stems]


def export_spot(item: dict[str, Any]) -> dict[str, Any]:
    keys = {key: value for key, value in item.items() if key not in {"inchikey", "matched", "rt_matched", "ms2_matched"}}
    keys.setdefault("adduct", "[M+H]+")
    keys["unit"] = "Height"
    exported = spot(**keys)
    exported["INCHIKEY"] = item.get("inchikey", "null")
    exported["Total score"] = "0.9"
    # As the Console writes them (bool.ToString()): an MS/MS match unless said, RT only where said.
    exported["RT matched"] = str(bool(item.get("rt_matched", False)))
    exported["MS/MS matched"] = str(bool(item.get("ms2_matched", item.get("matched", True))))
    return exported


def write_run(
    batch: MetabolomicsBatch, contract_path: str, polarity: str, *, salt: str = "HCOONH4",
    spots: list[dict[str, Any]] | None = None, gap_filled: dict[int, list[str]] | None = None,
    peak_ids: bool = True,
) -> dict[str, Any]:
    """What a production run of the contract leaves in its output folder, and the job that made it.

    The peak-ID matrix marks a zero height and every injection gap_filled names (by stem) as -1.
    """
    label = "Pos" if polarity == "Positive" else "Neg"
    output = batch.analysis / label.lower()
    output.mkdir(parents=True, exist_ok=True)
    items = spots if spots is not None else (POSITIVE_SPOTS if polarity == "Positive" else NEGATIVE_SPOTS)
    samples = _samples(label, batch.stems)
    height = write_export(output / f"{ALIGNMENT}_Height.txt", [export_spot(item) for item in items], samples)
    matrix = output / f"{ALIGNMENT}.mdpeakid.tsv"
    if peak_ids:
        lines = ["\t".join(["alignment_master_id", *(name for name, _, _ in samples)])]
        for item in items:
            filled = set((gap_filled or {}).get(item["alignment_id"], []))
            lines.append("\t".join([str(item["alignment_id"]), *(
                "-1" if float(cell) == 0 or stem in filled else str(index)
                for index, (cell, (stem, _)) in enumerate(zip(item["cells"], batch.stems))
            )]))
        matrix.write_text("\n".join(lines) + "\n", encoding="utf-8")
    elif matrix.exists():
        matrix.unlink()
    priorities = {"lib1_in_house": "4", "lib2_predicted_rt": "3", "lib3_no_rt": "2"}
    rows = []
    for item in items:
        if item["name"] == "Unknown" or not item.get("method"):
            continue
        source = "TextDB" if item["method"].startswith("internal_standards") else "MspDB"
        row = candidate(item["alignment_id"], item["name"], annotator=item["method"], source=source,
                        matched=item.get("matched", True), suggested=not item.get("matched", True))
        row["priority"] = priorities.get(item["method"], "1")
        rows.append(row)
    candidates = write_candidates(output / f"{ALIGNMENT}.mdcandidate.tsv", rows)
    project = output / f"{PROJECT}.mddata"
    project.write_bytes(b"project")
    (output / "method.txt").write_text(f"Ion mode: {polarity}\nSolvent type: {salt}\n", encoding="utf-8")
    (output / "guided-answers.json").write_text(json.dumps({
        "answers": {CONTRACT_KEY: contract_path},
        PLAN_BLOCK: {"contract_path": contract_path, "polarity": polarity,
                     "contract_sha256": lab_lipidomics.file_sha256(contract_path)},
    }), encoding="utf-8")
    return {
        "id": f"run-{label.lower()}", "kind": "run", "status": "completed", "logs": [],
        "preparation": {"run_directory": str(output), "export_folder_path": str(output)},
        "artifacts": {"msdial": [], "other": [str(height), str(candidates), str(project)], "qa": [], "mztab": []},
    }
