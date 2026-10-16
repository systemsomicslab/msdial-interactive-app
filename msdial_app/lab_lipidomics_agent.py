"""The laboratory lipidomics agent's steps around MS-DIAL: check the inputs, fix them in a contract,
find the internal standards in each run, normalize, and merge the two polarities.

A laboratory batch is two raw folders (positive and negative), an LBM2 library, the salt in the
mobile phase, a text library of internal standards per polarity, and quant_utility_table.xlsx.
lab_lipidomics reads and checks each of those; this module is the sequence a person walks through
with the agent, and what stops it:

1. check_inputs reads everything and changes nothing. It names what is still to be asked, what is
   wrong, what the run will be unable to quantify before it runs, and the sample table it proposes
   for both polarities. Its check_digest binds what it read.
2. write_contract writes <analysis root>/inputs/: the contract, the internal-standard libraries in
   the layout MS-DIAL reads, the standards template and the selection rules. It is refused if any
   input changed since it was checked. Both polarities' guided plans are built from the contract,
   and every later step hashes the inputs again and refuses on any difference.
3. resolve_standards reads each run's Height export and annotation candidate table and decides which
   aligned spot is each standard (lab_lipidomics.resolve_standards). It writes nothing; its digest
   binds what the person is shown.
4. plan_normalize / finish_normalize: the standards table and the exact MSDIALCUI normalize command,
   run as a job only once confirmed, and every quantified cell recomputed afterwards.
5. merge_lipidome: lipid_metabolome.csv from the two verified matrices and sheet 3, with the
   positive/negative reconciliation of BMP and PE P- before the selection, and the tables and
   dotplot folders the laboratory's R script reads, in <analysis root>/quant/lipidome.

The contract and its record name the LBM2 by its location, so both are local only (sharing.py). The
Console is named per contract, never through Interactive's shared settings.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
from pathlib import Path
from typing import Any, Iterable

from . import __version__
from .agent_workflow import RESOURCES, apply_sample_table, inspect_analysis_input
from .lab_lipidomics import (
    DEFAULT_INTERNAL_STANDARD_RT_TOLERANCE,
    INTERNAL_STANDARD_ANNOTATOR_IDS,
    NEGATIVE,
    POLARITIES,
    POSITIVE,
    InternalStandard,
    LabInputError,
    analysis_root_problems,
    chosen_candidate,
    compare_normalize_stdout,
    cross_check_standards,
    excel_lock_files,
    expected_resolution_matrix,
    file_sha256,
    folder_layout_problems,
    join_sample_metadata,
    lab_method_answers,
    normalize_salt,
    pair_sample_tables,
    parse_normalize_stdout,
    predict_peak_id_mismatches,
    raw_folder_overlaps,
    read_alignment_export,
    read_candidate_table,
    read_internal_standard_text,
    read_quant_workbook,
    read_sample_metadata,
    resolution_digest,
    resolve_standards,
    salt_evidence,
    split_by_polarity,
    standards_rows,
    verify_normalized,
    write_canonical_text_db,
    write_resolution_tsv,
    write_standards_tsv,
)
from .lipidome_merge import (
    DEFAULT_RECONCILIATION_RT_TOLERANCE,
    SAMPLE_FILE_TYPES,
    MergeRefused,
    companion_names,
    merge_pos_neg,
    reconciliation_parameters,
    reconciliation_selection_warnings,
    reference_matched_ids,
    strip_polarity,
    without_manifest,
)
from .workflow import (
    ANNOTATION_CANDIDATES_CAPABILITY,
    AUTOMATIC_RT_LOCAL_SUPPORT_CAPABILITY,
    NORMALIZE_PEAK_IDS_ONLY_CAPABILITY,
    NORMALIZE_PEAK_IDS_ONLY_OPTION,
    console_capabilities,
    file_identity,
    inspect_console_path,
)

CHECK_SCHEMA = "msdial-interactive.lab-lipidomics-check.v1"
CONTRACT_SCHEMA = "msdial-interactive.lab-lipidomics-contract.v1"
NORMALIZE_RECORD_SCHEMA = "msdial-interactive.lab-lipidomics-normalize.v1"
CONTRACT_NAME = "lab-lipidomics-contract.json"
RECORD_NAME = "lab-lipidomics-record.jsonl"
INPUTS_FOLDER = "inputs"
QUANT_FOLDER = "quant"
FOLDERS = {POSITIVE: "pos", NEGATIVE: "neg"}
LABELS = {POSITIVE: "Pos", NEGATIVE: "Neg"}
NOT_QUANTIFIED_NAME = "lipid_metabolome_not_quantified.csv"
MERGE_REPORT_NAME = "merge-report.json"
# The merge's own folder inside quant: metabolomics_service.R reads every subfolder of the folder it
# runs in as a dotplot folder, and quant holds normalize's pos and neg output folders.
MERGE_FOLDER = "lipidome"
# What normalize writes into the quant folder (plan_normalize's paths), by name prefix, folded.
QUANT_FILE_PREFIXES = ("normalizedheight_", "height_", "standards_", "is_resolution_", "normalize_")
RT_CORRECTION_FAILED = "Automatic alignment RT correction failed"
LAB_PARAMETER_TEMPLATE = RESOURCES / "msdial_console_param4lipidomics.txt"
LAB_LBM_QUERIES = RESOURCES / "LbmQueries.txt"
# R3: an annotation RT tolerance outside this is allowed and said.
ANNOTATION_RT_TOLERANCE_USUAL = (0.1, 3.0)
# Where MS-DIAL reads the raw data: the folders as acquired, or a copy made for this analysis.
RAW_LOCATIONS = ("in_place", "copy")
RAW_LOCATION_SENTENCE = (
    "MS-DIAL writes its per-file .dcl, .pai2 and tag files and the alignment container beside the raw files it "
    "reads, the one-file diagnostic included, and no output setting prevents it."
)
MERGE_SELECTION_QUESTION = (
    "Sheet 3 contradicts the merge's positive/negative reconciliation, which takes BMP and EtherPE from negative "
    "[M-H]- only. Either change sheet 3 (BMP and EtherPE positive adducts FALSE, BMP [M-H]- and EtherPE [M-H]- "
    "TRUE), save it and check again: once the contract is written it holds the workbook, and any change to it "
    "then needs a new contract and new runs of both polarities. Or keep sheet 3 as it is with "
    "merge_selection_accepted=true, which the contract records; the merge then says what the lipidome counts."
)
RAW_LOCATION_QUESTION = (
    f"{RAW_LOCATION_SENTENCE} Are pos_path and neg_path the data as acquired (in_place: the files are written "
    "there) or a copy made for this analysis (copy)? A copy is made only on your word, before the contract."
)


class LabContractError(ValueError):
    """A contract that cannot be used as it stands: unreadable, of another schema, or drifted."""


# ---------------------------------------------------------------------------------------------
# Step 1: look at the inputs
# ---------------------------------------------------------------------------------------------


def check_inputs(request: dict[str, Any]) -> dict[str, Any]:
    """Read every input of a laboratory lipidomics batch and say what is wrong or still to be asked.

    Nothing is written. With only the two folders it lists what is there (raw files, candidate
    input files, Excel lock files); with the rest it checks each input and the inputs against each
    other, and proposes one sample table for both polarities. check_digest binds what was read,
    so the contract can refuse an input that changed after the person saw this.
    """
    args = _arguments(request)
    blockers: list[str] = []
    warnings: list[str] = list(args.pop("_workset_warnings", []))
    notes: list[str] = []
    questions: list[dict[str, Any]] = []
    result: dict[str, Any] = {"schema": CHECK_SCHEMA, "interactive_version": __version__}
    # What a workset answered, every value of it: check_digest binds them, so write_contract must be
    # given the same workset_id or these values.
    filled = args.pop("_workset_filled", None)
    if filled is not None:
        result["filled_from_workset"] = filled

    def ask(identifier: str, prompt: str, choices: list[str] | None = None) -> None:
        questions.append({"id": identifier, "prompt": prompt, "choices": choices or [], "required": True})

    # The folders, read before anything else is asked: the person is shown what is there first.
    folders: dict[str, Path | None] = {}
    inventory: dict[str, Any] = {}
    for polarity, key in ((POSITIVE, "pos_path"), (NEGATIVE, "neg_path")):
        text = str(args.get(key) or "").strip()
        if not text:
            blockers.append(f"{key} names no folder; give the {polarity.lower()} raw-data folder.")
            folders[polarity] = None
            continue
        folder = Path(text).expanduser().resolve()
        folders[polarity] = folder
        if not folder.is_dir():
            blockers.append(f"{key} {folder} is not a folder.")
            continue
        inventory[polarity] = _folder_inventory(folder, polarity)
        blockers.extend(inventory[polarity]["blockers"])
        warnings.extend(inventory[polarity]["warnings"])
    if folders.get(POSITIVE) and folders.get(NEGATIVE):
        blockers.extend(folder_layout_problems(folders[POSITIVE], folders[NEGATIVE]))
    result["folders"] = {
        polarity: {key: value for key, value in item.items() if key not in {"files", "blockers", "warnings"}}
        for polarity, item in inventory.items()
    }
    result["candidate_inputs"] = _candidate_inputs([folder for folder in folders.values() if folder])

    if not str(args.get("analysis_purpose") or "").strip():
        ask(
            "analysis_purpose",
            "What do you want to learn from this analysis: the question, the biological comparison, "
            "whether annotation or comparative profiling is central, and the outputs you need?",
        )

    # R1: the laboratory's LBM2.
    lbm: dict[str, Any] = {}
    lbm_text = str(args.get("lbm_path") or "").strip()
    if not lbm_text:
        ask("lbm_path", "Which .lbm2 library (built for this chromatography) annotates the lipids?")
    else:
        lbm = _lbm_verdict(Path(lbm_text).expanduser())
        blockers.extend(lbm.pop("blockers"))
    result["lbm"] = lbm

    # R4: the internal-standard text libraries.
    libraries: dict[str, list[InternalStandard]] = {POSITIVE: [], NEGATIVE: []}
    library_reports: dict[str, Any] = {}
    texts = {POSITIVE: str(args.get("is_text_pos") or "").strip(), NEGATIVE: str(args.get("is_text_neg") or "").strip()}
    both = str(args.get("is_text") or "").strip()
    if both and not any(texts.values()):
        texts = {POSITIVE: both, NEGATIVE: both}
    if not texts[POSITIVE] or not texts[NEGATIVE]:
        ask(
            "is_text_pos/is_text_neg",
            "Which text library lists the internal standards of each polarity (Name, MZ, RT, Adduct and "
            "Ontology, MZ being the adduct's m/z)? One file holding both polarities may be given for both.",
        )
    elif Path(texts[POSITIVE]).expanduser().resolve() == Path(texts[NEGATIVE]).expanduser().resolve():
        read = read_internal_standard_text(Path(texts[POSITIVE]).expanduser())
        blockers.extend(read["blockers"])
        warnings.extend(read["warnings"])
        split = split_by_polarity(read["records"])
        libraries = split
        for polarity in POLARITIES:
            library_reports[polarity] = _library_report(read, split[polarity], polarity, shared=True)
    else:
        for polarity in POLARITIES:
            read = read_internal_standard_text(Path(texts[polarity]).expanduser(), polarity)
            blockers.extend(read["blockers"])
            warnings.extend(read["warnings"])
            libraries[polarity] = read["records"]
            library_reports[polarity] = _library_report(read, read["records"], polarity, shared=False)
    for polarity in POLARITIES:
        if polarity in library_reports and not libraries[polarity] and not blockers:
            blockers.append(f"The {polarity.lower()} internal-standard library lists no standard of that polarity.")
    result["internal_standard_libraries"] = library_reports

    # R5, R6: quant_utility_table.xlsx and the amount column.
    workbook: dict[str, Any] = {}
    quant_text = str(args.get("quant_table_path") or "").strip()
    amount_column = str(args.get("amount_column") or "").strip()
    if not quant_text:
        ask("quant_table_path", "Which quant_utility_table.xlsx gives the standards' amounts and the selection?")
    if not amount_column:
        ask(
            "amount_column",
            "Which sheet-1 column holds the amounts for these samples: F (per mg tissue), G (per 10^6 "
            "cells) or H (per uL plasma)?",
            ["F", "G", "H"],
        )
    if quant_text and amount_column:
        try:
            workbook = read_quant_workbook(Path(quant_text).expanduser(), amount_column)
        except LabInputError as error:
            blockers.append(str(error))
            workbook = {}
        blockers.extend(workbook.get("blockers", []))
        warnings.extend(workbook.get("warnings", []))
    elif quant_text:
        for lock in excel_lock_files(Path(quant_text).expanduser()):
            warnings.append(f"{Path(lock).name} shows the workbook is open in Excel; only what was last saved is read.")
    result["quant_table"] = _workbook_report(workbook) if workbook.get("preparation") else (
        {"path": workbook.get("path"), "sha256": workbook.get("sha256")} if workbook else {}
    )
    selection_accepted = False
    if (workbook.get("quant_info") or {}).get("selected"):
        # The merge reconciles BMP and PE P- between the polarities and takes both from negative
        # [M-H]-; a sheet 3 that says otherwise is the person's to change, before anything runs. After
        # the contract it cannot change without a new contract and new runs (the contract binds the
        # workbook), so a sheet 3 kept as it is is the person's explicit answer, recorded.
        selection = reconciliation_selection_warnings(workbook["quant_info"]["selected"], "Sheet 3")
        warnings.extend(selection)
        result["merge_selection_warnings"] = selection
        selection_accepted = bool(selection) and args.get("merge_selection_accepted") is True
        if selection and not selection_accepted:
            ask("merge_selection_accepted", MERGE_SELECTION_QUESTION + " " + " ".join(selection), ["change sheet 3", "true"])
        result["merge_selection_accepted"] = selection_accepted

    # R2: the salt, beside what the libraries and sheet 3 say about it.
    salt: dict[str, Any] = {}
    salt_text = str(args.get("solvent_type") or "").strip()
    if not salt_text:
        ask(
            "solvent_type",
            "Which salt is in the mobile phase (ammonium acetate, ammonium formate or ammonium bicarbonate)?",
            ["CH3COONH4", "HCOONH4", "NH4HCO3"],
        )
    else:
        salt = normalize_salt(salt_text)
        if salt["problem"]:
            blockers.append(f"solvent_type: {salt['problem']}")
    evidence = salt_evidence(salt.get("salt"), libraries[NEGATIVE], workbook.get("quant_info"))
    blockers.extend(evidence["blockers"])
    warnings.extend(evidence["warnings"])
    result["salt"] = salt
    result["salt_evidence"] = {key: value for key, value in evidence.items() if key not in {"blockers", "warnings"}}

    # R3 and P1: the two retention-time tolerances.
    rt_tolerance = _positive_number(args.get("retention_time_tolerance"))
    if args.get("retention_time_tolerance") in (None, ""):
        ask(
            "retention_time_tolerance",
            "Within how many minutes of the LBM library's retention time is an annotation accepted?",
        )
    elif rt_tolerance is None:
        blockers.append(f"retention_time_tolerance '{args.get('retention_time_tolerance')}' is not a positive number of minutes.")
    elif not ANNOTATION_RT_TOLERANCE_USUAL[0] <= rt_tolerance <= ANNOTATION_RT_TOLERANCE_USUAL[1]:
        warnings.append(f"An annotation RT tolerance of {rt_tolerance:g} min is outside the usual 0.1-3 min.")
    is_rt_tolerance = _positive_number(args.get("internal_standard_rt_tolerance", DEFAULT_INTERNAL_STANDARD_RT_TOLERANCE))
    if is_rt_tolerance is None:
        blockers.append(
            f"internal_standard_rt_tolerance '{args.get('internal_standard_rt_tolerance')}' is not a positive number of minutes."
        )
    result["tolerances"] = {"annotation_rt": rt_tolerance, "internal_standard_rt": is_rt_tolerance}

    # R7: where the analysis is written.
    root_text = str(args.get("analysis_root") or "").strip()
    analysis_root: Path | None = None
    if not root_text:
        ask(
            "analysis_root",
            "Where should the analysis be written (pos, neg, inputs and quant are made inside it)? Not a "
            "raw-data folder, a code checkout or a temporary folder.",
        )
    else:
        analysis_root = Path(root_text).expanduser().resolve()
        verdict = analysis_root_problems(analysis_root)
        blockers.extend(verdict["blockers"])
        warnings.extend(verdict["warnings"])
        blockers.extend(raw_folder_overlaps(analysis_root, _analysis_subfolders(analysis_root), folders))
        result["outputs"] = {
            "inputs": str(analysis_root / INPUTS_FOLDER), "quant": str(analysis_root / QUANT_FOLDER),
            **{polarity: str(analysis_root / FOLDERS[polarity]) for polarity in POLARITIES},
        }

    # Where MS-DIAL reads the raw data: it writes its intermediates beside the files it reads, and
    # only the person can say whether these folders are the data as acquired or a copy for this.
    raw_location = str(args.get("raw_location") or "").strip().casefold()
    if not raw_location:
        ask("raw_location", RAW_LOCATION_QUESTION, list(RAW_LOCATIONS))
    elif raw_location not in RAW_LOCATIONS:
        blockers.append(f"raw_location '{args.get('raw_location')}' is neither {' nor '.join(RAW_LOCATIONS)}.")
    result["raw_location"] = {"answer": raw_location, "writes_beside_raw": RAW_LOCATION_SENTENCE}

    # The Console that runs both polarities and normalize, named here rather than in the shared settings.
    console_text = str(args.get("console_path") or "").strip()
    console: dict[str, Any] = {}
    if not console_text:
        ask("console_path", "Which MS-DIAL Console (MSDIALCUI) runs this analysis?")
    else:
        console = _console_verdict(Path(console_text).expanduser())
        blockers.extend(console.pop("blockers"))
    result["console"] = console

    # Across the inputs: every prepared standard searchable, and what each selected class will be
    # quantified by. A class that can only come out NOT QUANTIFIED is said now, not after hours.
    if workbook.get("preparation") and library_reports:
        cross = cross_check_standards(workbook["preparation"], workbook["lookup"], libraries[POSITIVE], libraries[NEGATIVE])
        blockers.extend(cross["blockers"])
        warnings.extend(cross["warnings"])
        notes.extend(cross["notes"])
        matrix = expected_resolution_matrix(workbook["lookup"], workbook["quant_info"], libraries[POSITIVE], libraries[NEGATIVE])
        result["expected_resolution"] = {
            "by_polarity": matrix["by_polarity"], "any_others_standard": matrix["any_others_standard"],
        }
        for polarity, item in matrix["by_polarity"].items():
            if item["not_quantified"]:
                warnings.append(
                    f"{polarity}: {', '.join(item['not_quantified'])} will be NOT QUANTIFIED, because the standard "
                    f"sheet 2 names for them is not in the {polarity.lower()} internal-standard library."
                )

    # P3: one sample table for both polarities, from the file names; the metadata table only corroborates.
    proposal = _sample_table_proposal(inventory)
    blockers.extend(proposal.pop("blockers"))
    metadata_text = str(args.get("sample_metadata_path") or "").strip()
    metadata_report: dict[str, Any] = {}
    if metadata_text and inventory:
        metadata_report = _sample_metadata_report(Path(metadata_text).expanduser(), inventory)
        blockers.extend(metadata_report.pop("blockers"))
        warnings.extend(metadata_report.pop("warnings"))
    result["paired_sample_table"] = proposal
    result["sample_metadata"] = metadata_report

    threads = args.get("number_of_threads")
    if threads not in (None, "") and _positive_number(threads) is None:
        blockers.append(f"number_of_threads '{threads}' is not a positive whole number.")
    method = lab_method_answers()
    result["stated_defaults"] = {
        **method,
        "internal_standard_rt_tolerance": is_rt_tolerance,
        "internal_standard_search": (
            f"an LBM annotation of a standard counts out to the annotation RT tolerance "
            f"({rt_tolerance if rt_tolerance is not None else 'R3'} min), or {is_rt_tolerance} min where that is "
            f"wider, from its text RT: one within {is_rt_tolerance} min is taken first, and a farther one only on the "
            f"person's confirmation when another spot lies within {is_rt_tolerance} min. Its text row's annotation "
            f"and any other spot at its m/z count only within {is_rt_tolerance} min. The person may take any listed "
            "candidate instead"
        ),
        "method_files": {key: {"name": item["name"], "sha256": item["sha256"]} for key, item in lab_method_files().items()},
        "merge": (
            "rows whose representative annotation is not reference matched are dropped; a negative PG row that "
            "positive mode annotates as BMP, and a negative PE O- row positive mode annotates as PE P-, is renamed "
            f"before sheet 3 selects (same name after the class substitution, within "
            f"{DEFAULT_RECONCILIATION_RT_TOLERANCE} min); the statistics tables take the Sample and QC columns"
        ),
        "merge_method": lab_merge_method(),
        "dilution": "each file's factor in the sample table multiplies its concentrations (MS-DIAL -d)",
    }
    result["derived_preview"] = {
        "standards": len((workbook.get("preparation") or {}).get("standards", [])),
        "lookup_rows": len((workbook.get("lookup") or {}).get("rows", [])),
        "selected_rules": (workbook.get("quant_info") or {}).get("selected_by_ion_mode", {}),
        "internal_standards": {polarity: len(libraries[polarity]) for polarity in POLARITIES},
    }
    identity = _input_identity(args, inventory, lbm, library_reports, workbook, metadata_report)
    # The answer to the sheet-3 question as it applies: accepting a selection that contradicts nothing is no answer.
    identity["answers"]["merge_selection_accepted"] = selection_accepted
    result.update({
        "questions": questions,
        "blockers": list(dict.fromkeys(blockers)),
        "warnings": list(dict.fromkeys(warnings)),
        "notes": notes,
        "ready_for_contract": not questions and not blockers,
        "input_identity": identity,
        "check_digest": _digest(identity),
    })
    # Kept for write_contract, never returned: whole tables stay out of the model's context.
    result["_parsed"] = {
        "args": args, "folders": folders, "inventory": inventory, "libraries": libraries, "workbook": workbook,
        "salt": salt, "analysis_root": analysis_root, "rt_tolerance": rt_tolerance,
        "is_rt_tolerance": is_rt_tolerance, "console": console, "lbm": lbm, "metadata": metadata_report,
    }
    return result


def public_check(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if not key.startswith("_")}


def _analysis_subfolders(root: Path) -> dict[str, Path]:
    """The folders an analysis root holds, by what the overlap check calls them."""
    return {
        **{f"{polarity.lower()} output folder": root / FOLDERS[polarity] for polarity in POLARITIES},
        "inputs folder": root / INPUTS_FOLDER, "quant folder": root / QUANT_FOLDER,
    }


def _arguments(request: dict[str, Any]) -> dict[str, Any]:
    """The request's inputs, filled from a laboratory lipidomics workset where it names one."""
    args = {key: value for key, value in dict(request or {}).items() if value not in (None, "")}
    workset_id = str(args.pop("workset_id", "") or "").strip()
    if not workset_id:
        return args
    from .worksets import LAB_LIPIDOMICS_WORKSET_KIND, get_workset

    workset = get_workset(workset_id)
    if workset is None or workset.get("kind") != LAB_LIPIDOMICS_WORKSET_KIND:
        raise ValueError(f"Workset '{workset_id}' is not a laboratory lipidomics workset.")
    settings = dict(workset.get("lab_lipidomics") or {})
    warnings: list[str] = []
    filled: dict[str, Any] = {}
    for key, value in settings.get("answers", {}).items():
        if key not in args:
            args[key] = filled[key] = value
    for key, recorded in settings.get("files", {}).items():
        path = args.get(key)
        if path and recorded.get("sha256") and Path(str(path)).expanduser().is_file():
            current = _identity_sha256(Path(str(path)).expanduser())
            if current != recorded["sha256"] and _same_path(path, recorded.get("path", "")):
                warnings.append(
                    f"{Path(str(path)).name} has changed since workset '{workset_id}' was saved; it is checked as it is now."
                )
    args["_workset_warnings"] = warnings
    args["_workset_filled"] = {"workset_id": workset_id, "answers": filled}
    return args


def _folder_inventory(folder: Path, polarity: str) -> dict[str, Any]:
    inspection = inspect_analysis_input(str(folder))
    files = inspection.get("files", [])
    blockers: list[str] = []
    warnings: list[str] = list(inspection.get("warnings", []))
    if not files:
        blockers.append(f"{folder} holds no raw-data file MS-DIAL reads.")
    for item in files:
        path = Path(str(item.get("file_path", "")))
        if path.suffix.casefold() == ".wiff" and not Path(str(path) + ".scan").is_file():
            blockers.append(f"{path.name} has no {path.name}.scan beside it; MS-DIAL cannot read it.")
    tokens = {POSITIVE: 0, NEGATIVE: 0}
    for item in files:
        found = _polarity_tokens(str(item.get("file_name", "")))
        for key in found:
            tokens[key] += 1
    other = NEGATIVE if polarity == POSITIVE else POSITIVE
    if tokens[other] and tokens[other] >= tokens[polarity]:
        blockers.append(
            f"{folder} is given as the {polarity.lower()} folder, but {tokens[other]} of its file names say "
            f"{other.lower()}."
        )
    intermediates = sorted(
        path.name for path in folder.iterdir()
        if path.is_file() and path.suffix.casefold() in {".dcl", ".pai2", ".pai", ".tag"}
    )
    if intermediates:
        warnings.append(
            f"{folder} already holds {len(intermediates)} MS-DIAL intermediate file(s) (.dcl, .pai2); an earlier "
            "run read the data here."
        )
    folder_name = folder.name.casefold()
    return {
        "path": str(folder),
        "file_count": len(files),
        "formats": inspection.get("formats", []),
        "vendors": inspection.get("vendors", []),
        "polarity_in_names": tokens,
        "folder_name_says": (
            POSITIVE if re.search(r"(^|[^a-z])pos", folder_name) else NEGATIVE if re.search(r"(^|[^a-z])neg", folder_name) else ""
        ),
        "existing_intermediates": len(intermediates),
        "class_proposal": inspection.get("class_proposal", {}),
        "sample_table_proposal": inspection.get("sample_table_proposal", {}),
        "files": files,
        "blockers": blockers,
        "warnings": warnings,
    }


def _polarity_tokens(name: str) -> set[str]:
    tokens = {token for token in re.split(r"[_\-.\s]+", name.casefold()) if token}
    found = set()
    if tokens & {"pos", "positive"}:
        found.add(POSITIVE)
    if tokens & {"neg", "negative"}:
        found.add(NEGATIVE)
    return found


def _candidate_inputs(folders: list[Path]) -> dict[str, Any]:
    """Files beside the raw data that look like this batch's other inputs: listed, never chosen.

    The folders themselves and their parent, without descending: an LBM2 found there is listed but
    not proposed, because which library matches this chromatography is the person's call.
    """
    seen: set[Path] = set()
    found: dict[str, list[dict[str, Any]]] = {
        "internal_standard_texts": [], "workbooks": [], "excel_lock_files": [], "lbm2_libraries": [],
    }
    for folder in folders:
        for directory in (folder, folder.parent):
            if directory in seen or not directory.is_dir():
                continue
            seen.add(directory)
            try:
                entries = sorted(directory.iterdir())
            except OSError:
                continue
            for path in entries:
                if not path.is_file():
                    continue
                name = path.name
                suffix = path.suffix.casefold()
                if name.startswith("~$"):
                    found["excel_lock_files"].append({"path": str(path)})
                elif suffix == ".xlsx":
                    found["workbooks"].append({"path": str(path)})
                elif suffix == ".lbm2":
                    found["lbm2_libraries"].append({"path": str(path), "size_bytes": path.stat().st_size,
                                                    "proposed": False})
                elif suffix == ".txt" and path.stat().st_size < (1 << 20):
                    read = read_internal_standard_text(path)
                    if read["records"]:
                        split = split_by_polarity(read["records"])
                        found["internal_standard_texts"].append({
                            "path": str(path), "positive": len(split[POSITIVE]), "negative": len(split[NEGATIVE]),
                        })
    return found


def _lbm_verdict(path: Path) -> dict[str, Any]:
    blockers: list[str] = []
    verdict: dict[str, Any] = {"path": str(path.resolve()) if path.exists() else str(path), "name": path.name}
    if not path.is_file():
        blockers.append(f"lbm_path {path} is not a file.")
    elif path.suffix.casefold() != ".lbm2":
        blockers.append(f"{path.name} is not an .lbm2 library.")
    elif path.stat().st_size == 0:
        # A missing or empty LBM library is skipped by the Console without a word.
        blockers.append(f"{path.name} is empty.")
    else:
        identity = file_identity(path)
        verdict.update({"sha256": identity.get("sha256", ""), "size_bytes": path.stat().st_size})
        verdict["sibling_libraries"] = sorted(
            item.name for item in path.parent.glob("*.lbm2") if item.name != path.name
        )
    verdict["blockers"] = blockers
    return verdict


# What a laboratory lipidomics run needs of its Console, each with the words a refusal uses for it.
# scripts/setup_claude.py reads this too, so that the setup and the input check judge a Console alike.
LAB_CONSOLE_NEEDS = {
    NORMALIZE_PEAK_IDS_ONLY_CAPABILITY: f"normalize {NORMALIZE_PEAK_IDS_ONLY_OPTION}",
    ANNOTATION_CANDIDATES_CAPABILITY: "the annotation candidate table",
    AUTOMATIC_RT_LOCAL_SUPPORT_CAPABILITY: "automatic RT correction's local test (MsdialWorkbench#826)",
}


def _console_verdict(path: Path) -> dict[str, Any]:
    blockers: list[str] = []
    verdict: dict[str, Any] = {"path": str(path.resolve()) if path.exists() else str(path)}
    if not path.is_file():
        blockers.append(f"console_path {path} is not a file.")
        verdict["blockers"] = blockers
        return verdict
    found = console_capabilities(str(path))["capabilities"]
    verdict["capabilities"] = found
    missing = [label for capability, label in LAB_CONSOLE_NEEDS.items() if capability not in found]
    if missing:
        blockers.append(f"The Console {path.name} lacks {', '.join(missing)}, which a laboratory lipidomics run needs.")
    verdict["blockers"] = blockers
    return verdict


# What identifies a Console build: its assembly, and every file of its folder by one digest (a
# rebuild of MsdialCore alone leaves MSDIALCUI.dll as it was).
CONSOLE_BUILD_KEYS = ("assembly_sha256", "inventory_sha256")


def console_identity(path: str | Path) -> dict[str, Any]:
    """The Console by the build it is, not by where it sits: Console folders are rebuilt in place."""
    found = inspect_console_path(path)
    return {key: found.get(key, "") for key in ("path", "version", *CONSOLE_BUILD_KEYS, "provenance_status")}


def _library_report(read: dict[str, Any], records: list[InternalStandard], polarity: str, *, shared: bool) -> dict[str, Any]:
    return {
        "path": read["path"], "sha256": read["sha256"], "encoding": read["encoding"], "shared_by_polarities": shared,
        "polarity": polarity, "standards": [record.name for record in records],
        "adducts": sorted({record.adduct for record in records}),
    }


def _workbook_report(workbook: dict[str, Any]) -> dict[str, Any]:
    preparation = workbook["preparation"]
    return {
        "path": workbook["path"], "sha256": workbook["sha256"], "sheet_names": workbook["sheet_names"],
        "amount_column": preparation.get("amount_column"), "amount_header": preparation.get("amount_header"),
        "unit": workbook.get("unit"),
        # Read back for the person, never interpreted: the sample amount the chosen column divides by,
        # and any note below the standards (a plasma volume, for one).
        "sample_parameter": preparation.get("sample_parameter"), "notes": preparation.get("notes", []),
        "unused_standards": workbook["lookup"].get("unused_standards", []),
        "multi_adduct_classes": workbook["quant_info"].get("multi_adduct_classes", []),
        "undefined_classes": workbook["quant_info"].get("undefined_classes", []),
    }


def _sample_table_proposal(inventory: dict[str, Any]) -> dict[str, Any]:
    if set(inventory) != set(POLARITIES):
        return {"rows": [], "problems": [], "matched": False, "blockers": []}
    rows = {polarity: [_sample_row(item) for item in inventory[polarity]["files"]] for polarity in POLARITIES}
    paired = pair_sample_tables(rows[POSITIVE], rows[NEGATIVE])
    blockers = [f"The two polarities do not pair: {problem}" for problem in paired["problems"]]
    for row in paired["rows"]:
        row["included"] = True
        row["acquisition_type"] = next(
            (item.get("acquisition_type") for item in rows[POSITIVE] + rows[NEGATIVE]
             if strip_polarity(item["file_name"]).casefold() == row["stem"].casefold()), "DDA",
        )
    return {
        **paired,
        "class_proposal": inventory[POSITIVE].get("class_proposal", {}),
        "order_proposal": inventory[POSITIVE].get("sample_table_proposal", {}).get("analytical_order", {}),
        "confirmation_required": True,
        "blockers": blockers,
    }


def _sample_row(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "file_name": str(item.get("file_name", "")),
        "file_type": item.get("file_type", "Sample"),
        "class_id": item.get("class_id", ""),
        "analytical_order": item.get("analytical_order"),
        "batch_order": item.get("batch_order", 1),
        "factor": float(item.get("factor", 1) or 1),
        "acquisition_type": item.get("acquisition_type", "DDA"),
    }


def _sample_metadata_report(path: Path, inventory: dict[str, Any]) -> dict[str, Any]:
    blockers: list[str] = []
    warnings: list[str] = []
    if not path.is_file():
        return {"path": str(path), "blockers": [f"sample_metadata_path {path} is not a file."], "warnings": []}
    try:
        metadata = read_sample_metadata(path)
    except (LabInputError, OSError, UnicodeDecodeError) as error:
        return {"path": str(path), "blockers": [f"{path.name} cannot be read: {error}"], "warnings": []}
    blockers.extend(metadata["blockers"])
    warnings.extend(metadata["warnings"])
    joined = {}
    for polarity, item in inventory.items():
        report = join_sample_metadata(metadata, [_sample_row(entry) for entry in item["files"]])
        joined[polarity] = {
            "matched": sum(1 for entry in report["files"] if entry["metadata"]),
            "unmatched": report["unmatched"],
            "disagreements": report["disagreements"],
        }
        for disagreement in report["disagreements"]:
            warnings.append(
                f"{path.name} gives {disagreement['field']} {disagreement['metadata']!r} for "
                f"{disagreement['file_name']}; the file names suggest {disagreement['proposed']!r}."
            )
    rows = metadata["rows"]
    return {
        "path": metadata["path"], "sha256": file_sha256(path), "rows": len(rows), "joined": joined,
        "excluded": [row["file_name"] for row in rows if not row["included"]],
        "factors": sorted({row["factor"] for row in rows if row.get("factor")}),
        "acquisition_types": sorted({row["acquisition_type"] for row in rows if row.get("acquisition_type")}),
        "blockers": blockers, "warnings": warnings,
    }


def _input_identity(
    args: dict[str, Any], inventory: dict[str, Any], lbm: dict[str, Any], libraries: dict[str, Any],
    workbook: dict[str, Any], metadata: dict[str, Any],
) -> dict[str, Any]:
    """What the check read, by content where it is small and by name and size for raw data."""
    return {
        "raw": {
            polarity: sorted(
                [str(item.get("file_name")), _size(item.get("file_path"))] for item in item_inventory["files"]
            )
            for polarity, item_inventory in sorted(inventory.items())
        },
        "lbm": {"path": lbm.get("path"), "sha256": lbm.get("sha256")},
        "internal_standard_texts": {
            polarity: {"path": item.get("path"), "sha256": item.get("sha256")} for polarity, item in sorted(libraries.items())
        },
        "quant_table": {"path": workbook.get("path"), "sha256": workbook.get("sha256")},
        "sample_metadata": {"path": metadata.get("path"), "sha256": metadata.get("sha256")},
        # As read, so that 2 and 2.0, or a path spelled two ways, do not count as a change.
        "answers": {
            **{key: str(args.get(key, "")).strip() for key in ("solvent_type", "amount_column", "analysis_purpose")},
            "raw_location": str(args.get("raw_location", "")).strip().casefold(),
            **{key: repr(_positive_number(args.get(key))) for key in ("retention_time_tolerance", "number_of_threads")},
            "internal_standard_rt_tolerance": repr(_positive_number(
                args.get("internal_standard_rt_tolerance", DEFAULT_INTERNAL_STANDARD_RT_TOLERANCE)
            )),
            # The raw folders by location too: two copies of one batch have the same names and sizes,
            # and the one the person was shown is the one MS-DIAL writes beside.
            **{key: _path_key(args.get(key)) for key in ("analysis_root", "console_path", "pos_path", "neg_path")},
        },
    }


def _path_key(value: Any) -> str:
    text = str(value or "").strip()
    return os.path.normcase(str(Path(text).expanduser().resolve())) if text else ""


def _size(path: Any) -> int:
    try:
        target = Path(str(path))
        if target.is_dir():
            return sum(item.stat().st_size for item in target.rglob("*") if item.is_file())
        return target.stat().st_size
    except OSError:
        return -1


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _positive_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 and number == number and number != float("inf") else None


def _identity_sha256(path: Path) -> str:
    return file_identity(path).get("sha256", "") if path.stat().st_size >= (64 << 20) else file_sha256(path)


# ---------------------------------------------------------------------------------------------
# Step 2: the contract
# ---------------------------------------------------------------------------------------------


def write_contract(request: dict[str, Any]) -> dict[str, Any]:
    """Fix the checked inputs and the confirmed sample table in <analysis root>/inputs/.

    The inputs are checked again; a question still open, a blocker, or any input that differs from
    what check_digest bound refuses the write. confirmed=false previews what would be written.
    """
    check = check_inputs(request)
    parsed = check.pop("_parsed")
    refusal: dict[str, Any] = {"written": False, "check": {
        key: check[key] for key in ("questions", "blockers", "warnings", "check_digest")
    }}
    if check["questions"] or check["blockers"]:
        return {**refusal, "error": "The inputs are not ready for a contract; answer the questions and resolve the blockers."}
    given_digest = str(request.get("check_digest") or "").strip()
    if not given_digest:
        return {**refusal, "error": "Pass the check_digest msdial_lipidomics_check_inputs returned for the inputs the person saw."}
    if given_digest != check["check_digest"]:
        return {**refusal, "error": (
            "An input changed after it was checked (a file's content, a raw file, or an answer); check the "
            "inputs again and show the person the result before writing the contract. A check filled from a "
            "workset binds the workset's answers too (filled_from_workset): pass the same workset_id here."
        )}
    if request.get("sample_table_confirmed") is not True:
        return {**refusal, "error": (
            "The sample table is the person's to confirm: show paired_sample_table, then pass "
            "sample_table_confirmed=true, with sample_table holding their corrections if any."
        )}
    table = _confirmed_sample_table(check["paired_sample_table"], request.get("sample_table"), parsed["inventory"])
    if table["problems"]:
        return {**refusal, "error": "The sample table cannot be used: " + " ".join(table["problems"])}

    root: Path = parsed["analysis_root"]
    inputs = root / INPUTS_FOLDER
    contract_path = inputs / CONTRACT_NAME
    derived_paths = {
        "internal_standard_libraries": {polarity: inputs / f"is_{FOLDERS[polarity]}.txt" for polarity in POLARITIES},
        "standards_template": inputs / "standards_template.tsv",
        "quant_rules": inputs / "quant_rules.tsv",
    }
    workbook = parsed["workbook"]
    libraries = parsed["libraries"]
    method_files = lab_method_files()
    # A contract already there has runs planned from it, which a new one leaves unusable: the person
    # is shown it and asked about replacing it before confirming anything.
    existing = _existing_contract(contract_path) if contract_path.exists() else None
    # The sheet-3 contradictions of the reconciliation the person kept (merge_selection_accepted), or none.
    accepted_selection = list(check.get("merge_selection_warnings") or []) if check.get("merge_selection_accepted") else []
    if existing is not None and request.get("replace_existing") is not True:
        return {**refusal, "existing_contract": existing, "error": (
            f"{contract_path} already exists. Show the person existing_contract (what was done under it, and what "
            "replacing it does), then preview again with replace_existing=true if they want it replaced."
        )}
    preview = {
        "contract_path": str(contract_path),
        "existing_contract": existing,
        "would_write": [
            str(contract_path), *(str(path) for path in derived_paths["internal_standard_libraries"].values()),
            str(derived_paths["standards_template"]), str(derived_paths["quant_rules"]), str(inputs / RECORD_NAME),
        ],
        "raw_folders": {polarity: str(parsed["folders"][polarity]) for polarity in POLARITIES},
        "raw_location": check["raw_location"],
        "output_roots": {polarity: str(root / FOLDERS[polarity]) for polarity in POLARITIES},
        "sample_table": table["rows"],
        "salt": parsed["salt"]["salt"],
        "unit": workbook.get("unit"),
        "method_files": {key: {"name": item["name"], "sha256": item["sha256"]} for key, item in method_files.items()},
        "merge": {**lab_merge_method(), "selection_warnings_accepted": accepted_selection},
        "check_digest": check["check_digest"],
    }
    if request.get("confirmed") is not True:
        return {"written": False, "confirmation_required": True, **preview,
                "message": "Show the person what would be written, then call again with confirmed=true."}

    inputs.mkdir(parents=True, exist_ok=True)
    replaced = ""
    if contract_path.exists():
        stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
        kept = contract_path.with_name(f"lab-lipidomics-contract.{stamp}.json")
        contract_path.replace(kept)
        replaced = str(kept)
    derived: dict[str, Any] = {"internal_standard_libraries": {}}
    for polarity in POLARITIES:
        written = write_canonical_text_db(libraries[polarity], derived_paths["internal_standard_libraries"][polarity])
        derived["internal_standard_libraries"][polarity] = written
    template_rows = [
        {"StandardName": row["standard"], "TargetClass": row["target_class"],
         "Concentration": repr(float(row["amount"]))}
        for row in workbook["lookup"]["rows"]
    ]
    derived["standards_template"] = _write_tsv(
        derived_paths["standards_template"], ("StandardName", "TargetClass", "Concentration"), template_rows
    )
    rule_rows = [
        {"Class": row["class"], "Adduct": row["adduct"], "Ion mode": row["ion_mode"],
         "IsSelected": "TRUE" if row["selected"] else "FALSE"}
        for row in workbook["quant_info"]["rows"]
    ]
    derived["quant_rules"] = _write_tsv(derived_paths["quant_rules"], ("Class", "Adduct", "Ion mode", "IsSelected"), rule_rows)

    args = parsed["args"]
    library_reports = check["internal_standard_libraries"]
    contract = {
        "schema": CONTRACT_SCHEMA,
        # It names the LBM2 and the raw data by their locations here: never part of a bundle or a shared report.
        "sharing": "local_only",
        "created_at": dt.datetime.now().astimezone().isoformat(),
        "interactive_version": __version__,
        "analysis_purpose": str(args.get("analysis_purpose")).strip(),
        "analysis_root": str(root),
        "raw_folders": {polarity: str(parsed["folders"][polarity]) for polarity in POLARITIES},
        # The person's answer, read back with the sentence they were given.
        "raw_location": check["raw_location"],
        "output_roots": {polarity: str(root / FOLDERS[polarity]) for polarity in POLARITIES},
        "quant_folder": str(root / QUANT_FOLDER),
        "console_path": parsed["console"]["path"],
        # The build as well as the path: normalize says when it runs another build than this.
        "console": console_identity(parsed["console"]["path"]),
        "inputs": {
            "lbm": {key: parsed["lbm"].get(key) for key in ("path", "name", "sha256", "size_bytes")},
            # The method template and the LBM query list every plan from the contract uses, whatever
            # the shared settings or a workset name: both polarities run the same method.
            **{key: {"path": item["path"], "sha256": item["sha256"]} for key, item in method_files.items()},
            "internal_standard_texts": {
                polarity: {"path": library_reports[polarity]["path"], "sha256": library_reports[polarity]["sha256"]}
                for polarity in POLARITIES
            },
            "quant_table": check["quant_table"],
            "sample_metadata": (
                {"path": parsed["metadata"]["path"], "sha256": parsed["metadata"]["sha256"]}
                if parsed["metadata"].get("sha256") else None
            ),
        },
        "derived": {
            "internal_standard_libraries": {
                polarity: {"path": item["path"], "sha256": item["sha256"], "records": item["records"]}
                for polarity, item in derived["internal_standard_libraries"].items()
            },
            "standards_template": derived["standards_template"],
            "quant_rules": derived["quant_rules"],
        },
        "salt": {key: parsed["salt"].get(key) for key in ("salt", "answer", "matched")},
        "retention_time_tolerance": parsed["rt_tolerance"],
        "internal_standard_rt_tolerance": parsed["is_rt_tolerance"],
        "amount_column": workbook["preparation"]["amount_column"],
        "unit": workbook["unit"],
        "standards": [
            {"name": item["name"], "class_name": item["class_name"], "amount": item["amount"]}
            for item in workbook["preparation"]["standards"]
        ],
        "lookup": [
            {"standard": row["standard"], "target_class": row["target_class"], "amount": row["amount"]}
            for row in workbook["lookup"]["rows"]
        ],
        "expected_resolution": check.get("expected_resolution", {}),
        "sample_table": {
            "rows": table["rows"],
            "confirmed": True,
            "confirmed_by": str(request.get("confirmed_by") or "").strip(),
            "confirmed_at": dt.datetime.now().astimezone().isoformat(),
            "corrected": table["corrected"],
        },
        "method": lab_method_answers(),
        # How the merge reads the two matrices, unless the merge is told otherwise (and records it), and the
        # sheet-3 selection the person kept although it contradicts the reconciliation.
        "merge": {**lab_merge_method(), "selection_warnings_accepted": accepted_selection},
        "number_of_threads": int(float(args["number_of_threads"])) if args.get("number_of_threads") else None,
        "check_digest": check["check_digest"],
        "replaced_contract": replaced,
    }
    temporary = contract_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(contract, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(contract_path)
    contract_sha256 = file_sha256(contract_path)
    record = record_event(contract_path, {
        "event": "contract_written", "contract_sha256": contract_sha256, "check_digest": check["check_digest"],
        "sample_table_confirmed_by": contract["sample_table"]["confirmed_by"], "replaced_contract": replaced,
        "raw_folders": contract["raw_folders"], "raw_location": contract["raw_location"]["answer"],
        "merge_selection_accepted": accepted_selection,
    })
    return {
        "written": True,
        "contract_path": str(contract_path),
        "contract_sha256": contract_sha256,
        "record": record,
        "files": preview["would_write"],
        "output_roots": contract["output_roots"],
        "next": (
            "Plan each polarity with msdial_guided_analysis_plan(input_path=<its raw folder>, "
            "answers={'lab_lipidomics_contract_path': <contract_path>, 'ion_mode': ...}); the diagnostic and "
            "each production run are confirmed separately."
        ),
    }


def _confirmed_sample_table(
    proposal: dict[str, Any], given: Any, inventory: dict[str, Any]
) -> dict[str, Any]:
    """The table the person confirmed: the proposal, with their corrections by stem."""
    problems: list[str] = []
    rows = [dict(row) for row in proposal.get("rows", [])]
    corrected = False
    if given not in (None, "", []):
        if not isinstance(given, list) or not all(isinstance(row, dict) for row in given):
            return {"rows": [], "problems": ["sample_table must be a list of rows."], "corrected": False}
        by_stem = {row["stem"].casefold(): row for row in rows}
        seen: set[str] = set()
        for number, item in enumerate(given, start=1):
            stem = str(
                item.get("stem")
                or strip_polarity(str(item.get("positive_file") or item.get("negative_file") or item.get("file_name") or ""))
            ).strip()
            if not stem or stem.casefold() not in by_stem:
                problems.append(f"sample_table row {number} names {stem or 'no sample'}, which is in neither folder.")
                continue
            if stem.casefold() in seen:
                problems.append(f"sample_table names {stem} twice.")
                continue
            seen.add(stem.casefold())
            target = by_stem[stem.casefold()]
            for key in ("file_type", "class_id", "analytical_order", "batch_order", "factor", "included", "acquisition_type"):
                if key in item and item[key] not in (None, ""):
                    if target.get(key) != item[key]:
                        corrected = True
                    target[key] = item[key]
        missing = [row["stem"] for row in rows if row["stem"].casefold() not in seen]
        if missing:
            problems.append(f"sample_table has no row for {', '.join(missing)}; give every sample, unchanged or not.")
    for polarity in POLARITIES:
        if polarity not in inventory:
            continue
        check = apply_sample_table(inventory[polarity]["files"], polarity_sample_table(rows, polarity))
        problems.extend(f"{polarity}: {problem}" for problem in check["problems"])
    return {"rows": rows, "problems": list(dict.fromkeys(problems)), "corrected": corrected}


def polarity_sample_table(rows: Iterable[dict[str, Any]], polarity: str) -> list[dict[str, Any]]:
    """The paired table as one polarity's guided sample_table answer."""
    key = "positive_file" if polarity == POSITIVE else "negative_file"
    return [
        {
            "file_name": row[key],
            **{name: row[name] for name in ("file_type", "class_id", "analytical_order", "batch_order", "factor",
                                             "included", "acquisition_type") if row.get(name) not in (None, "")},
        }
        for row in rows
        if row.get(key)
    ]


def _write_tsv(path: Path, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> dict[str, Any]:
    lines = ["\t".join(columns)] + ["\t".join(str(row[column]) for column in columns) for row in rows]
    data = ("\r\n".join(lines) + "\r\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "rows": len(rows)}


def lab_method_files() -> dict[str, dict[str, Any]]:
    """The parameter template and LBM query list a laboratory contract fixes, by path and sha256.

    Interactive's own laboratory template (LBM cutoffs 0.1 / 0.1 / 0.1) and query list, never what
    the shared settings name: a template saved from the GUI would otherwise replace the method of
    one polarity and not the other without a word.
    """
    return {
        key: {"path": str(path.resolve()), "name": path.name, "sha256": file_sha256(path)}
        for key, path in (("parameter_template", LAB_PARAMETER_TEMPLATE), ("lbm_queries", LAB_LBM_QUERIES))
    }


# The steps a contract's record holds that a replacement leaves unusable.
RECORDED_STEPS = ("peak_height_diagnostic", "production_run_started", "normalized", "merged")


def _existing_contract(contract_path: Path) -> dict[str, Any]:
    """A contract already at the path, and what was done under it: what replacing it would orphan."""
    sha256 = file_sha256(contract_path)
    try:
        created_at = json.loads(contract_path.read_text(encoding="utf-8-sig")).get("created_at")
    except (OSError, ValueError, AttributeError):
        created_at = None
    steps: list[dict[str, Any]] = []
    current = False
    try:
        lines = (contract_path.parent / RECORD_NAME).read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("event") == "contract_written":
            current = event.get("contract_sha256") == sha256
        elif current and event.get("event") in RECORDED_STEPS:
            steps.append({key: event.get(key) for key in ("event", "at", "polarity", "job_id") if event.get(key)})
    return {
        "path": str(contract_path), "sha256": sha256, "created_at": created_at, "recorded_steps": steps,
        "replacing_it": (
            "keeps it under a dated name and rewrites is_pos.txt and is_neg.txt; every diagnostic, run and "
            "normalize made under it can then no longer be resolved, normalized or merged, and is made again "
            "under the new contract"
        ),
    }


def record_event(contract_path: str | Path, event: dict[str, Any]) -> str:
    """Append one decision or step to the contract's record, beside it."""
    path = Path(contract_path).expanduser().resolve().parent / RECORD_NAME
    entry = {"at": dt.datetime.now().astimezone().isoformat(), **event}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    return str(path)


def load_contract(contract_path: str | Path, *, verify: bool = True) -> dict[str, Any]:
    """The contract, with every input and derived file hashed again; any difference refuses it."""
    path = Path(str(contract_path)).expanduser().resolve()
    try:
        contract = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise LabContractError(f"The laboratory lipidomics contract {path} cannot be read: {error}") from None
    if not isinstance(contract, dict) or contract.get("schema") != CONTRACT_SCHEMA:
        raise LabContractError(f"{path} is not a laboratory lipidomics contract ({CONTRACT_SCHEMA}).")
    contract["_path"] = str(path)
    contract["_sha256"] = file_sha256(path)
    if verify:
        drift = contract_drift(contract)
        if drift:
            raise LabContractError(
                "The contract's inputs changed after it was written, so nothing planned from it can be trusted: "
                + "; ".join(drift) + ". Check the inputs again and write a new contract."
            )
    return contract


def contract_drift(contract: dict[str, Any]) -> list[str]:
    drift: list[str] = []
    inputs = contract.get("inputs", {})
    checked = [("LBM2 library", inputs.get("lbm"))]
    checked += [(f"{polarity} internal-standard text", item) for polarity, item in inputs.get("internal_standard_texts", {}).items()]
    checked += [("quant table", inputs.get("quant_table")), ("sample metadata", inputs.get("sample_metadata"))]
    derived = contract.get("derived", {})
    checked += [(f"{polarity} canonical internal-standard library", item)
                for polarity, item in derived.get("internal_standard_libraries", {}).items()]
    checked += [("standards template", derived.get("standards_template")), ("quant rules", derived.get("quant_rules"))]
    for label, item in checked:
        if not item or not item.get("sha256"):
            continue
        path = Path(str(item.get("path")))
        if not path.is_file():
            drift.append(f"the {label} {path} is gone")
            continue
        if _identity_sha256(path) != item["sha256"]:
            drift.append(f"the {label} {path.name} has changed")
    return drift


# What a plan from a contract is given besides the contract's own answers: the contract, the
# polarity and the accepted threshold (and workflow_overrides, judged on their own).
CONTRACT_PLAN_KEYS = frozenset({"lab_lipidomics_contract_path", "ion_mode", "minimum_peak_height", "workflow_overrides"})


def contract_plan_answers(contract_path: str, supplied: dict[str, Any], input_path: str) -> dict[str, Any]:
    """The guided answers a contract gives one polarity, and the blockers in using it.

    supplied is everything the plan was given, the caller's answers over a workset's. "fixed"
    holds what the contract decides for both polarities; a plan given one of them otherwise is
    refused. "answers" holds the method the contract states (S1-S7); a plan may change one, and
    the change is reported as a deviation from the contract. Any other answer (an RT-correction
    tuning value, say) is applied and reported as a deviation too, so that a plan reported as the
    contract's is the contract's.
    """
    outcome: dict[str, Any] = {
        "answers": {}, "fixed": {}, "blockers": [], "contract_path": str(contract_path), "contract_sha256": "",
        "polarity": "", "deviations": [], "fixed_keys": [],
    }
    try:
        contract = load_contract(contract_path)
    except LabContractError as error:
        outcome["blockers"].append(str(error))
        return outcome
    outcome["contract_path"] = contract["_path"]
    outcome["contract_sha256"] = contract["_sha256"]
    polarity = str(supplied.get("ion_mode") or "")
    if polarity not in POLARITIES:
        outcome["blockers"].append("Name the ion mode (Positive or Negative) this plan is for; the contract covers both.")
        return outcome
    outcome["polarity"] = polarity
    raw = contract["raw_folders"][polarity]
    if not input_path or not _same_path(input_path, raw):
        outcome["blockers"].append(
            f"input_path must be the contract's {polarity.lower()} raw-data folder, {raw}."
        )
    # Checked when the contract was written, and again here for a contract written by hand or
    # before the check existed.
    root = Path(contract["analysis_root"])
    outcome["blockers"].extend(raw_folder_overlaps(root, _analysis_subfolders(root), contract["raw_folders"]))
    # The method files are Interactive's own, so an upgrade may change them under a contract: the
    # runs already made stand, and a plan made now would run another method.
    method_files = {key: contract["inputs"].get(key) or {} for key in ("parameter_template", "lbm_queries")}
    for key, item in method_files.items():
        label = key.replace("_", " ")
        if not item.get("sha256"):
            outcome["blockers"].append(f"The contract names no {label}; it was written by an earlier version. Write a new contract.")
        elif not Path(str(item.get("path"))).is_file():
            outcome["blockers"].append(f"The contract's {label} {item.get('path')} is gone.")
        elif file_sha256(item["path"]) != item["sha256"]:
            outcome["blockers"].append(
                f"The contract's {label} {Path(str(item['path'])).name} has changed since the contract was written, so "
                "this polarity would run another method than the contract states. Write a new contract."
            )
    fixed = {
        "project_type": "lcms",
        "target_omics": "Lipidomics",
        "library_strategy": "existing",
        "libraries": {"lbm_path": contract["inputs"]["lbm"]["path"]},
        "use_retention_time_for_annotation": True,
        "retention_time_tolerance": contract["retention_time_tolerance"],
        "solvent_type": contract["salt"]["salt"],
        "internal_standard_text_path": contract["derived"]["internal_standard_libraries"][polarity]["path"],
        "internal_standard_rt_tolerance": contract["internal_standard_rt_tolerance"],
        "sample_table": polarity_sample_table(contract["sample_table"]["rows"], polarity),
        "class_assignment_confirmed": True,
        "analysis_purpose": contract["analysis_purpose"],
        "output_root": contract["output_roots"][polarity],
        # The exports land in the output folder, never in or over a raw folder.
        "export_folder_path": contract["output_roots"][polarity],
        "console_path": contract["console_path"],
        "template_path": method_files["parameter_template"].get("path", ""),
        "queries_path": method_files["lbm_queries"].get("path", ""),
    }
    # An empty answer is no answer: the contract's stands.
    given = {key: value for key, value in supplied.items() if value is not None and value != ""}
    for key, value in fixed.items():
        if key in given and not _same_answer(key, given[key], value):
            outcome["blockers"].append(
                f"{key} is fixed by the contract {contract['_path']} for both polarities; write a new contract to change it."
            )
    answers = dict(contract["method"]["answers"])
    if contract.get("number_of_threads"):
        answers["number_of_threads"] = contract["number_of_threads"]
    for key, value in answers.items():
        if key in given and not _same_answer(key, given[key], value):
            outcome["deviations"].append({"key": key, "contract": value, "given": given[key]})
    for key in sorted(set(given) - set(fixed) - set(answers) - CONTRACT_PLAN_KEYS):
        outcome["deviations"].append({"key": key, "contract": None, "given": given[key]})
    outcome.update({"answers": answers, "fixed": {**fixed, "lab_lipidomics_contract_path": contract["_path"]},
                    "fixed_keys": sorted(fixed)})
    return outcome


# The workflow state a contract decides, by key or by prefix: the libraries and their scoring, the
# salt, the files and sample table, where the run writes, the Console, the internal-standard search.
# workflow_overrides are applied after everything else, so one of these would replace the contract's
# choice in the run without a word.
CONTRACT_FIXED_STATE_KEYS = frozenset({
    "solvent", "files", "sample_table_proposal",
    "output_root", "export_folder_path", "console_path", "ion_mode", "project_type", "target_omics",
    "template_path", "selected_lipids", "selected_adducts", "internal_standard_rt_tolerance",
    "lab_lipidomics_contract_path", "lab_lipidomics_profile", "analysis_purpose", "repository_run_manifest",
    "repository_metadata", "repository_metadata_source_path",
})
CONTRACT_FIXED_STATE_PREFIXES = ("lbm_", "msp_", "text_")


def contract_override_problems(overrides: dict[str, Any]) -> dict[str, list[Any]]:
    """workflow_overrides on a plan from a contract: refused where the contract decides, reported otherwise."""
    blockers: list[str] = []
    deviations: list[dict[str, Any]] = []
    for key, value in sorted((overrides or {}).items()):
        if key in CONTRACT_FIXED_STATE_KEYS or key.startswith(CONTRACT_FIXED_STATE_PREFIXES):
            blockers.append(
                f"workflow_overrides.{key} would replace what the laboratory lipidomics contract decides; write a "
                "new contract to change it."
            )
        else:
            deviations.append({"key": f"workflow_overrides.{key}", "contract": None, "given": value})
    return {"blockers": blockers, "deviations": deviations}


def _same_answer(key: str, left: Any, right: Any) -> bool:
    if key in {"output_root", "export_folder_path", "console_path", "internal_standard_text_path", "template_path",
               "queries_path"}:
        return _same_path(left, right)
    if key == "libraries":
        return isinstance(left, dict) and _same_path(left.get("lbm_path", ""), (right or {}).get("lbm_path", "")) and not (
            set(left) - {"lbm_path"}
        )
    if isinstance(right, bool):
        return str(left).strip().casefold() in ({"true", "1", "yes"} if right else {"false", "0", "no"})
    if isinstance(right, (int, float)) and not isinstance(right, bool):
        try:
            return float(left) == float(right)
        except (TypeError, ValueError):
            return False
    return json.dumps(left, sort_keys=True, default=str) == json.dumps(right, sort_keys=True, default=str)


def _same_path(left: Any, right: Any) -> bool:
    try:
        return os.path.normcase(str(Path(str(left)).expanduser().resolve())) == os.path.normcase(
            str(Path(str(right)).expanduser().resolve())
        )
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------------------------
# Step 3: the internal standards in each run
# ---------------------------------------------------------------------------------------------


def run_outputs(contract: dict[str, Any], polarity: str, job: dict[str, Any] | None) -> dict[str, Any]:
    """The run's Height export, candidate table and project, found from its job or its folder.

    The run must be the contract's: written into the contract's output folder, planned from this
    contract, with the contract's salt in its method. A rerun into the same folder leaves several
    exports there; only the job's own files are taken, and without the job an ambiguous folder is
    refused rather than guessed.
    """
    output_root = Path(contract["output_roots"][polarity])
    problems: list[str] = []
    found_by = "job"
    logs = [str(line) for line in (job or {}).get("logs", [])]
    if job is not None:
        preparation = job.get("preparation") or {}
        if job.get("kind") != "run":
            problems.append(f"Job {job.get('id')} is a {job.get('kind')} job, not a production run.")
        if job.get("status") != "completed":
            problems.append(f"Job {job.get('id')} is {job.get('status')}, not completed.")
            if any(RT_CORRECTION_FAILED in line for line in logs):
                problems.append(
                    "Its log says automatic alignment RT correction failed. Whether to run it again without the "
                    "correction is the person's decision (execute_automatic_rt_correction=false, which the plan "
                    "reports as a deviation from the contract); never retry it unasked."
                )
        if not _same_path(preparation.get("run_directory", ""), output_root):
            problems.append(
                f"Job {job.get('id')} wrote into {preparation.get('run_directory')}, not the contract's "
                f"{polarity.lower()} output folder {output_root}."
            )
        # As the files stand now: a job lists what the run wrote, and one deleted since is not there.
        artifacts = [Path(path) for kind, paths in (job.get("artifacts") or {}).items() if kind != "records"
                     for path in paths if Path(path).is_file()]
        if not artifacts:
            found_by = "output folder"
            artifacts = _folder_files(output_root, preparation.get("export_folder_path"))
    else:
        found_by = "output folder"
        artifacts = _folder_files(output_root, None)

    def named(test: Any) -> list[Path]:
        return sorted({path for path in artifacts if test(path.name)}, key=str)

    heights = named(lambda name: name.endswith("_Height.txt") and not name.endswith("_NormalizedHeight.txt"))
    projects = named(lambda name: name.casefold().endswith(".mddata"))
    candidates = named(lambda name: name.endswith(".mdcandidate.tsv"))
    outcome: dict[str, Any] = {
        "polarity": polarity, "job_id": (job or {}).get("id"), "found_by": found_by, "run_directory": str(output_root),
        "height_export": None, "candidate_table": None, "project": None, "alignment_name": None,
        "loaded_library_copy": None, "problems": problems, "warnings": [],
    }
    if len(heights) != 1:
        problems.append(
            f"{'No' if not heights else len(heights)} alignment Height export(s) (*_Height.txt) found for the "
            f"{polarity.lower()} run; exactly one is needed."
        )
    if len(projects) != 1:
        problems.append(f"{'No' if not projects else len(projects)} project file(s) (*.mddata) found; exactly one is needed.")
    if heights:
        alignment = heights[0].name[: -len("_Height.txt")]
        outcome["alignment_name"] = alignment
        outcome["height_export"] = str(heights[0])
        match = [path for path in candidates if path.name == alignment + ".mdcandidate.tsv"]
        if not match:
            problems.append(
                f"{alignment}.mdcandidate.tsv is missing; the run must export annotation candidates "
                "(export_annotation_candidates), which tell a reference match from a suggestion."
            )
        else:
            outcome["candidate_table"] = str(match[0])
    if projects:
        outcome["project"] = str(projects[0])
        copy = projects[0].with_name(projects[0].stem + "_Loaded.msp2.dbs")
        if copy.is_file():
            outcome["loaded_library_copy"] = str(copy)
        else:
            problems.append(
                f"{copy.name} is not beside the project; normalize loads the run's library copy from there. "
                "Do not move the run's intermediates before normalize."
            )
    answers_file = output_root / "guided-answers.json"
    try:
        recorded = json.loads(answers_file.read_text(encoding="utf-8-sig"))
        answers = recorded.get("answers", {})
    except (OSError, ValueError, AttributeError):
        recorded, answers = {}, {}
        problems.append(f"{answers_file} cannot be read, so this run cannot be shown to come from the contract.")
    if answers and not _same_path(answers.get("lab_lipidomics_contract_path", ""), contract["_path"]):
        problems.append(f"The run in {output_root} was not planned from this contract.")
    elif answers and (recorded.get("lab_lipidomics") or {}).get("contract_sha256") != contract["_sha256"]:
        # A contract replaced at the same path rewrites the internal-standard libraries in place;
        # the run was annotated with the earlier ones.
        problems.append(
            f"The run in {output_root} was planned from another version of this contract (its sha256 differs); "
            "run it again from the current contract."
        )
    method_file = output_root / "method.txt"
    if method_file.is_file():
        lines = method_file.read_text(encoding="utf-8", errors="replace").splitlines()
        salt = contract["salt"]["salt"]
        if f"Solvent type: {salt}" not in lines:
            problems.append(f"The run's method.txt does not say 'Solvent type: {salt}'.")
    else:
        problems.append(f"{method_file} is missing.")
    return outcome


def _folder_files(root: Path, export_folder: Any) -> list[Path]:
    folders = [root] + ([Path(str(export_folder))] if export_folder else [])
    files: list[Path] = []
    for folder in folders:
        if folder.is_dir():
            files.extend(path.resolve() for path in folder.iterdir() if path.is_file())
    return files


def resolve_polarity(contract: dict[str, Any], polarity: str, job: dict[str, Any] | None) -> dict[str, Any]:
    """One polarity's internal standards, decided from its run, and the digest the person confirms."""
    outputs = run_outputs(contract, polarity, job)
    result: dict[str, Any] = {"polarity": polarity, "outputs": outputs, "problems": list(outputs["problems"])}
    if result["problems"]:
        return result
    export = read_alignment_export(outputs["height_export"])
    candidates = read_candidate_table(outputs["candidate_table"])
    library = read_internal_standard_text(
        contract["derived"]["internal_standard_libraries"][polarity]["path"], polarity
    )
    used = list(dict.fromkeys(row["standard"] for row in contract["lookup"]))
    resolution = resolve_standards(
        export, candidates, library["records"], polarity=polarity,
        annotation_rt_tolerance=float(contract["retention_time_tolerance"]),
        internal_standard_rt_tolerance=float(contract["internal_standard_rt_tolerance"]),
        standard_names=used,
    )
    lbm_annotated = sum(1 for spot in export["spots"] if spot["method"].startswith("LbmDB"))
    annotator = INTERNAL_STANDARD_ANNOTATOR_IDS[polarity]
    text_annotated = sum(1 for spot in export["spots"] if spot["method"] == annotator)
    if not lbm_annotated:
        # A missing or unreadable LBM2 is skipped by the Console without a word.
        result["problems"].append(
            "No spot in the run is annotated by the LBM library (no 'LbmDB:' annotation method); the LBM2 was "
            "not loaded, and nothing in the run is a lipid annotation."
        )
    if not text_annotated:
        resolution["warnings"].append(
            f"No spot is annotated by the internal-standard library ({annotator}); check that MS-DIAL loaded it."
        )
    rows = standards_rows({"rows": [{"standard": row["standard"], "target_class": row["target_class"],
                                     "amount": row["amount"]} for row in contract["lookup"]]}, resolution)
    by_name = {entry["name"]: entry for entry in resolution["standards"]}
    unexpected = [
        name for name in used
        if by_name.get(name, {}).get("status") in {"unresolved_missing", "invalid", "conflict"}
    ]
    to_confirm = [name for name in resolution["needs_confirmation"] if name in used]
    not_quantified = sorted({row["TargetClass"] for row in rows if int(row["PeakID"]) < 0})
    digest = resolution_digest({"contract_sha256": contract["_sha256"], "resolution": resolution})
    result.update({
        "resolution": resolution,
        "resolution_digest": digest,
        "summary": {
            "standards": [
                {key: entry.get(key) for key in ("name", "tier", "status", "alignment_id", "needs_confirmation",
                                                 "delta_mz_mda", "delta_rt", "label_disagreement", "reason")}
                | {"ms_dial_name": (entry.get("spot") or {}).get("name"),
                   "zero_height_files": entry.get("zero_height_files", []),
                   "tolerated_zero_height_files": entry.get("tolerated_zero_height_files", []),
                   "candidates": len(entry.get("candidates", [])),
                   "valid_alternatives": entry.get("valid_alternatives", []),
                   "other_observations": len(entry.get("other_observations", []))}
                for entry in resolution["standards"]
            ],
            "unresolved_expected": [name for name in resolution["unresolved_expected"] if name in used],
            "unexpected_unresolved": unexpected,
            "needs_confirmation": to_confirm,
            "classes_not_quantified_if_unchanged": not_quantified,
            "lbm_annotated_spots": lbm_annotated,
            "internal_standard_library_spots": text_annotated,
        },
    })
    return result


def resolve_standards_for_contract(contract_path: str, jobs: dict[str, dict[str, Any] | None]) -> dict[str, Any]:
    """Each named polarity's standards; read-only. A polarity with problems says why, the other stands."""
    contract = load_contract(contract_path)
    polarities: dict[str, Any] = {}
    for polarity, job in jobs.items():
        resolved = resolve_polarity(contract, polarity, job)
        if resolved.get("resolution") is not None:
            resolved["resolution_table"] = _resolution_rows(resolved["resolution"])
            del resolved["resolution"]
        polarities[polarity] = resolved
    return {
        "contract_path": contract["_path"], "contract_sha256": contract["_sha256"], "polarities": polarities,
        "decisions": (
            "For each polarity, read the tiers to the person. A standard found only by its text candidate or by "
            "search, or by an LBM annotation outside the internal-standard RT tolerance while another spot lies "
            "inside it (needs_confirmation), is used only if they accept it (accept_searched); a standard that was "
            "not found, or whose spot is invalid, is left unresolved only if they accept that (accept_unresolved), "
            "and the classes it quantifies are then NOT QUANTIFIED. For any standard they may instead take another "
            "of its listed candidates (valid_alternatives; accept_spot, by standard)."
        ),
    }


def _resolution_rows(resolution: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"name": entry["name"], "candidates": [
            {key: item.get(key) for key in ("alignment_id", "tier", "name", "adduct", "mz", "rt", "intensity", "method",
                                            "valid", "zero_height_files")}
            for item in entry.get("candidates", [])
        ], "other_observations": entry.get("other_observations", []), "warnings": entry.get("warnings", [])}
        for entry in resolution["standards"]
        if entry.get("candidates") or entry.get("other_observations")
    ]


# ---------------------------------------------------------------------------------------------
# Step 4: normalize
# ---------------------------------------------------------------------------------------------


def polarity_of_job(contract: dict[str, Any], job: dict[str, Any]) -> str:
    run_directory = (job.get("preparation") or {}).get("run_directory", "")
    for polarity in POLARITIES:
        if _same_path(run_directory, contract["output_roots"][polarity]):
            return polarity
    raise LabContractError(
        f"Job {job.get('id')} wrote into {run_directory}, which is neither of the contract's output folders."
    )


def plan_normalize(
    contract_path: str,
    job: dict[str, Any],
    resolution_digest_text: str,
    accept_searched: Iterable[str] = (),
    accept_unresolved: Iterable[str] = (),
    accept_spot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The standards table and the exact normalize command for one polarity, or why not.

    The resolution is computed again and must carry the digest the person was shown. Every
    standard sheet 2 uses must be resolved, or the person must have decided about it: a searched
    spot accepted (it is then named by AssignName in MS-DIAL's exports), another of its listed
    candidates taken instead (accept_spot, by standard), or the standard left unresolved, which
    empties the classes it quantifies. --peak-ids-only always: a standard left at -1 is never
    looked up by name.
    """
    contract = load_contract(contract_path)
    polarity = polarity_of_job(contract, job)
    resolved = resolve_polarity(contract, polarity, job)
    if resolved["problems"]:
        raise LabContractError(f"The {polarity.lower()} run cannot be normalized: " + " ".join(resolved["problems"]))
    if str(resolution_digest_text or "").strip() != resolved["resolution_digest"]:
        raise LabContractError(
            "The resolution is not the one the person confirmed (the run's exports or the contract changed, or "
            "another digest was passed); resolve the standards again and show them."
        )
    resolution = resolved["resolution"]
    used = set(row["standard"] for row in contract["lookup"])
    by_name = {entry["name"]: entry for entry in resolution["standards"]}
    searched = {name for name in resolution["needs_confirmation"] if name in used}
    accepted = {str(name) for name in accept_searched}
    left = {str(name) for name in accept_unresolved}
    if accept_spot is not None and not isinstance(accept_spot, dict):
        raise LabContractError("accept_spot maps a standard's name to the alignment ID the person chose for it.")
    chosen = {str(name): value for name, value in (accept_spot or {}).items()}
    unknown = sorted(accepted - searched)
    if unknown:
        raise LabContractError(
            f"accept_searched names {', '.join(unknown)}, which this resolution does not ask the person to accept."
        )
    for name, alignment_id in sorted(chosen.items()):
        if name not in used:
            raise LabContractError(f"accept_spot names {name}, which sheet 2 does not use.")
        if name in accepted or name in left:
            raise LabContractError(
                f"{name} is given more than one decision; accept_spot, accept_searched and accept_unresolved exclude "
                "one another."
            )
        try:
            chosen_candidate(by_name.get(name, {"name": name}), alignment_id)
        except LabInputError as error:
            raise LabContractError(f"accept_spot: {error}") from None
    unresolvable = {
        name for name in used
        if by_name.get(name, {}).get("status") in {"unresolved_missing", "invalid", "conflict"}
    }
    stray = sorted(left - unresolvable - (searched - accepted))
    if stray:
        raise LabContractError(f"accept_unresolved names {', '.join(stray)}, which is not a standard awaiting that decision.")
    undecided = sorted((unresolvable | (searched - accepted)) - left - set(chosen))
    if undecided:
        raise LabContractError(
            "Each of these standards needs the person's decision before normalize: "
            + "; ".join(f"{name} ({by_name[name]['status']}: {by_name[name].get('reason', '')})" for name in undecided)
            + ". Accept a searched spot with accept_searched, take another listed candidate with accept_spot, or "
            "leave the standard unresolved with accept_unresolved."
        )
    lookup = {"rows": [dict(row) for row in contract["lookup"]]}
    rows = standards_rows(lookup, resolution, accepted, chosen)
    owners: dict[int, set[str]] = {}
    for row in rows:
        if int(row["PeakID"]) >= 0:
            owners.setdefault(int(row["PeakID"]), set()).add(row["StandardName"])
    shared = {peak_id: names for peak_id, names in owners.items() if len(names) > 1}
    if shared:
        raise LabContractError(
            "One spot would be more than one standard: "
            + "; ".join(f"alignment ID {peak_id} for {', '.join(sorted(names))}" for peak_id, names in sorted(shared.items()))
            + "."
        )
    console = Path(contract["console_path"])
    if NORMALIZE_PEAK_IDS_ONLY_CAPABILITY not in console_capabilities(str(console))["capabilities"]:
        raise LabContractError(
            f"The contract's Console {console} offers no normalize {NORMALIZE_PEAK_IDS_ONLY_OPTION}; without it a "
            "standard the resolver rejected would be looked up by name. Normalize is refused."
        )
    identity = console_identity(console)
    recorded_console = contract.get("console") or {}
    changed = [key for key in CONSOLE_BUILD_KEYS if recorded_console.get(key) and recorded_console[key] != identity.get(key)]
    plan_warnings: list[str] = []
    if changed:
        # Redaction and AssignName differ between normalize builds; which one ran is said and recorded.
        plan_warnings.append(
            f"The Console {console} is not the build the contract recorded ({', '.join(changed)} differ): it was "
            "rebuilt or replaced since, and normalize runs the build there now. Both are recorded with the result."
        )
    outputs = resolved["outputs"]
    candidates = read_candidate_table(outputs["candidate_table"])
    predicted = predict_peak_id_mismatches(rows, candidates)
    sample_rows = polarity_sample_table(contract["sample_table"]["rows"], polarity)
    factors = {
        row["file_name"]: float(row.get("factor", 1) or 1)
        for row in sample_rows if str(row.get("included", True)).casefold() not in {"false", "0", "no"}
    }
    apply_dilution = any(value != 1.0 for value in factors.values())
    quant = Path(contract["quant_folder"])
    folder = FOLDERS[polarity]
    paths = {
        "standards": quant / f"standards_{folder}.tsv",
        "resolution": quant / f"is_resolution_{folder}.tsv",
        "output_folder": quant / folder,
        "record": quant / f"normalize_{folder}.local.json",
        "normalized_copy": quant / f"NormalizedHeight_{LABELS[polarity]}.txt",
        "height_copy": quant / f"Height_{LABELS[polarity]}.txt",
    }
    prefix = ["dotnet", str(console)] if console.suffix.casefold() == ".dll" else [str(console)]
    command = prefix + [
        "normalize", "-i", outputs["project"], "-s", str(paths["standards"]), "-o", str(paths["output_folder"]),
        "-u", contract["unit"], NORMALIZE_PEAK_IDS_ONLY_OPTION,
    ]
    flags: dict[str, str] = {NORMALIZE_PEAK_IDS_ONLY_OPTION: "a standard is taken from its PeakID or not at all"}
    unresolved_names = sorted({row["StandardName"] for row in rows if int(row["PeakID"]) < 0})
    if unresolved_names:
        command.append("--allow-unresolved-standards")
        flags["--allow-unresolved-standards"] = (
            f"{', '.join(unresolved_names)} stay unresolved, as expected or as decided; their classes are emptied"
        )
    if predicted:
        # Every alignment ID was written by the resolver from this run, bound by the export's sha256;
        # the Console's check exists for a stale table. Its warnings must equal this prediction.
        command.append("--allow-mismatched-peak-ids")
        flags["--allow-mismatched-peak-ids"] = (
            "MS-DIAL's stored name differs from the standard's for "
            + ", ".join(f"{item['standard']} (alignment ID {item['alignment_id']}, '{item['ms_dial_name']}')"
                        for item in predicted)
        )
    if apply_dilution:
        command.append("-d")
        flags["-d"] = "the sample table gives dilution factors other than 1, which MS-DIAL multiplies by"
    return {
        "contract_path": contract["_path"],
        "contract_sha256": contract["_sha256"],
        "polarity": polarity,
        "job_id": job.get("id"),
        "resolution_digest": resolved["resolution_digest"],
        "unit": contract["unit"],
        "command": command,
        "flags": flags,
        "rows": rows,
        "accepted_searched": sorted(accepted),
        "accepted_unresolved": sorted(left),
        "accepted_spots": {name: int(chosen_candidate(by_name[name], value)["alignment_id"])
                           for name, value in sorted(chosen.items())},
        "console": {"contract": recorded_console, "now": identity, "changed_since_contract": bool(changed)},
        "warnings": plan_warnings,
        "predicted_mismatches": predicted,
        "not_quantified_classes": sorted({row["TargetClass"] for row in rows if int(row["PeakID"]) < 0}),
        "dilution_factors": factors if apply_dilution else {},
        "outputs": {key: outputs[key] for key in ("height_export", "candidate_table", "project", "alignment_name")},
        "candidate_table_sha256": candidates["sha256"],
        "paths": {key: str(value) for key, value in paths.items()},
        "resolution": resolution,
        "computes": (
            "Each standard's own spot is its amount x the file's dilution factor (its amount when -d is not "
            "passed). Every other spot is amount x its height / the standard's height in that file, x the file's "
            "dilution factor with -d, the standard being its class's own or 'Any others'; a spot not reference "
            "matched is quantified by 'Any others'. A class whose standard did not resolve is emptied with a NOT "
            "QUANTIFIED comment, and a file whose standard has no height is empty for that class. Blanks are "
            "normalized like samples; nothing is subtracted."
        ),
    }


def public_normalize_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """The plan as shown to the person: the table's decisions, not the resolution's every candidate."""
    shown = {key: value for key, value in plan.items() if key not in {"resolution", "rows"}}
    shown["standards_table"] = [
        {key: row[key] for key in ("StandardName", "TargetClass", "Concentration", "PeakID", "AssignName", "status")}
        for row in plan["rows"]
    ]
    return shown


def write_normalize_inputs(plan: dict[str, Any]) -> dict[str, Any]:
    """The standards table normalize reads and the resolution it was made from, beside the outputs."""
    paths = plan["paths"]
    standards = write_standards_tsv(plan["rows"], paths["standards"])
    resolution = write_resolution_tsv(plan["resolution"], paths["resolution"])
    Path(paths["output_folder"]).mkdir(parents=True, exist_ok=True)
    return {"standards": standards, "resolution": resolution}


def finish_normalize(plan: dict[str, Any], exit_code: int | None, log_lines: Iterable[str]) -> dict[str, Any]:
    """What normalize did, checked against what it was given, and every quantified cell recomputed.

    The record (normalize_{pos,neg}.local.json) is written whatever the outcome; the merge reads it
    and refuses a polarity whose normalize did not complete as planned.
    """
    paths = plan["paths"]
    text = "\n".join(str(line) for line in log_lines)
    parsed = parse_normalize_stdout(text)
    problems: list[str] = []
    record: dict[str, Any] = {
        "schema": NORMALIZE_RECORD_SCHEMA,
        "sharing": "local_only",
        "recorded_at": dt.datetime.now().astimezone().isoformat(),
        "contract_path": plan["contract_path"], "contract_sha256": plan["contract_sha256"],
        "polarity": plan["polarity"], "run_job_id": plan["job_id"], "resolution_digest": plan["resolution_digest"],
        "unit": plan["unit"], "command": plan["command"], "flags": plan["flags"], "exit_code": exit_code,
        "accepted_searched": plan["accepted_searched"], "accepted_unresolved": plan["accepted_unresolved"],
        "accepted_spots": plan.get("accepted_spots", {}),
        # The build that normalized, beside the one the contract recorded: Console folders are rebuilt in place.
        "console": plan.get("console", {}),
        "internal_standard_ids": sorted({str(int(row["PeakID"])) for row in plan["rows"] if int(row["PeakID"]) >= 0}),
        "not_quantified_classes": plan["not_quantified_classes"],
        "candidate_table": plan["outputs"]["candidate_table"], "candidate_table_sha256": plan["candidate_table_sha256"],
        "normalize_report": {key: parsed[key] for key in ("resolved", "unresolved", "mismatched", "warnings", "unit",
                                                          "dilution_factor_applied", "redacted_rows")},
    }
    if exit_code != 0:
        problems.append(f"MSDIALCUI normalize exited with code {exit_code}.")
    else:
        problems.extend(compare_normalize_stdout(parsed, plan["rows"]))
        reported = {(item.get("standard"), item.get("alignment_id")) for item in parsed["mismatched"]}
        predicted = {(item["standard"], item["alignment_id"]) for item in plan["predicted_mismatches"]}
        if reported != predicted:
            problems.append(
                "normalize reported other mismatched standards than were predicted: "
                f"reported {sorted(map(str, reported))}, predicted {sorted(map(str, predicted))}."
            )
    alignment = plan["outputs"]["alignment_name"]
    height = Path(paths["output_folder"]) / f"{alignment}_Height.txt"
    normalized = Path(paths["output_folder"]) / f"{alignment}_NormalizedHeight.txt"
    verification: dict[str, Any] | None = None
    if exit_code == 0:
        if not height.is_file() or not normalized.is_file():
            problems.append(f"normalize did not write {height.name} and {normalized.name} into {paths['output_folder']}.")
        else:
            verification = verify_normalized(
                height, normalized, plan["rows"], read_candidate_table(plan["outputs"]["candidate_table"]),
                unit=plan["unit"], dilution_factors=plan["dilution_factors"] or None,
            )
            # A failed row is set aside by the merge; a matrix that does not match its Height matrix
            # at all (other samples) cannot be set aside row by row.
            problems.extend(verification["problems"])
            shutil.copyfile(normalized, paths["normalized_copy"])
            shutil.copyfile(height, paths["height_copy"])
            record["outputs"] = {
                "normalized": str(normalized), "height": str(height),
                "normalized_copy": paths["normalized_copy"], "normalized_sha256": file_sha256(paths["normalized_copy"]),
                "height_copy": paths["height_copy"], "height_sha256": file_sha256(paths["height_copy"]),
            }
    record["problems"] = problems
    record["verification"] = verification
    record["completed"] = exit_code == 0 and not problems
    record["verified"] = bool(record["completed"] and verification and verification["passed"])
    target = Path(paths["record"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    record["record"] = str(target)
    record_event(plan["contract_path"], {
        "event": "normalized", "polarity": plan["polarity"], "exit_code": exit_code,
        "completed": record["completed"], "verified": record["verified"],
        "failed_rows": len((verification or {}).get("failed_rows", [])), "record": str(target),
        "accepted_searched": plan["accepted_searched"], "accepted_unresolved": plan["accepted_unresolved"],
        "accepted_spots": plan.get("accepted_spots", {}),
        "console_changed_since_contract": bool((plan.get("console") or {}).get("changed_since_contract")),
    })
    return record


# ---------------------------------------------------------------------------------------------
# Step 5: merge
# ---------------------------------------------------------------------------------------------


def lab_merge_method() -> dict[str, Any]:
    """How the laboratory merge reads the two normalized matrices, as the contract states it."""
    return {
        "sample_file_types": list(SAMPLE_FILE_TYPES),
        "polarity_reconciliation": reconciliation_parameters(True, DEFAULT_RECONCILIATION_RT_TOLERANCE),
        "outputs": (
            f"<quant>/{MERGE_FOLDER}: lipid_metabolome.csv (or output_name) and, under these names whatever it is "
            "called, lipid_metabolome4stat.csv, lipid_metabolome4classsummary.csv and lipid_metabolome4summaryplot.csv; "
            "the Summary_Dotplots, "
            f"category_<category> and subclass_<ontology> folders; {NOT_QUANTIFIED_NAME} and {MERGE_REPORT_NAME}"
        ),
    }


def merge_lipidome(
    contract_path: str,
    output_name: str = "lipid_metabolome.csv",
    *,
    write: bool = False,
    reconcile_polarities: bool | None = None,
    reconciliation_rt_tolerance: float | None = None,
) -> dict[str, Any]:
    """lipid_metabolome.csv and its statistics tables and dotplot folders, as the laboratory's program wrote them.

    Each polarity's normalize must have completed as planned, and its matrix must be the one its
    record names. Rows that failed verification go to the not-quantified file with the NOT QUANTIFIED
    ones. Before sheet 3 selects, the negative rows positive mode identifies as BMP or PE P- are
    renamed (the contract's merge method: on, 0.1 min); reconcile_polarities=False or another
    reconciliation_rt_tolerance is applied and recorded as a deviation from the contract;
    reconcile_polarities is true, false or None (the contract), never another value. Everything is
    written into <quant>/lipidome, the folder metabolomics_service.R is run in; output_name names the
    lipidome only, and the three tables keep metaboservice's names, which the R script reads.
    """
    contract = load_contract(contract_path)
    name = str(output_name or "").strip()
    if not name or Path(name).name != name or Path(name).suffix.casefold() not in {".csv", ".txt", ".tsv"}:
        raise LabContractError("output_name is a file name ending in .csv, .tsv or .txt, without a folder.")
    folded = name.casefold()
    tables = {table.casefold() for table in companion_names().values()}
    if folded in {NOT_QUANTIFIED_NAME.casefold(), MERGE_REPORT_NAME.casefold(), *tables} or folded.startswith(QUANT_FILE_PREFIXES):
        # The merge writes its side file and report beside the lipidome, and normalize's records bind
        # its matrices by sha256: a name of theirs is refused wherever the lipidome is written.
        raise LabContractError(
            f"output_name {name} is a file the merge or normalize writes under {contract['quant_folder']}; choose another name."
        )
    if reconcile_polarities is not None and not isinstance(reconcile_polarities, bool):
        # A JSON "false" is text, and text is no answer: neither on nor off is assumed from it.
        raise LabContractError(f"reconcile_polarities is true or false, not {reconcile_polarities!r}.")
    stated = (contract.get("merge") or {}).get("polarity_reconciliation") or {}
    contract_reconcile = stated.get("enabled", True) is not False
    contract_tolerance = float(stated.get("rt_tolerance_min", DEFAULT_RECONCILIATION_RT_TOLERANCE))
    reconcile = contract_reconcile if reconcile_polarities is None else reconcile_polarities
    tolerance = contract_tolerance if reconciliation_rt_tolerance is None else reconciliation_rt_tolerance
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or not math.isfinite(tolerance) or tolerance <= 0:
        raise LabContractError(
            f"reconciliation_rt_tolerance must be a positive number of minutes, not {reconciliation_rt_tolerance!r}."
        )
    tolerance = float(tolerance)
    deviations: list[str] = []
    if reconcile != contract_reconcile:
        deviations.append(
            f"the positive/negative reconciliation is {'on' if reconcile else 'off'}; the contract says "
            f"{'on' if contract_reconcile else 'off'}"
        )
    if reconcile and tolerance != contract_tolerance:
        deviations.append(f"the reconciliation RT tolerance is {tolerance:g} min; the contract says {contract_tolerance:g}")
    sample_file_types = tuple((contract.get("merge") or {}).get("sample_file_types") or SAMPLE_FILE_TYPES)

    quant = Path(contract["quant_folder"])
    folder = quant / MERGE_FOLDER
    records: dict[str, dict[str, Any]] = {}
    for polarity in POLARITIES:
        path = quant / f"normalize_{FOLDERS[polarity]}.local.json"
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise LabContractError(f"The {polarity.lower()} polarity has not been normalized ({path.name} is missing).") from None
        if record.get("contract_sha256") != contract["_sha256"]:
            raise LabContractError(f"The {polarity.lower()} normalize was made under another contract; normalize it again.")
        if not record.get("completed"):
            raise LabContractError(
                f"The {polarity.lower()} normalize did not complete as planned: " + " ".join(record.get("problems", []))
            )
        outputs = record.get("outputs") or {}
        for key in ("normalized", "height"):
            copy = Path(outputs.get(f"{key}_copy", ""))
            if not copy.is_file() or file_sha256(copy) != outputs.get(f"{key}_sha256"):
                raise LabContractError(f"{copy.name} is not the matrix the {polarity.lower()} normalize wrote.")
        if file_sha256(record["candidate_table"]) != record["candidate_table_sha256"]:
            raise LabContractError(f"The {polarity.lower()} run's candidate table has changed since normalize.")
        records[polarity] = record
    workbook = read_quant_workbook(contract["inputs"]["quant_table"]["path"], contract["amount_column"])
    labels = {POSITIVE: "positive", NEGATIVE: "negative"}
    # The dotplot files the last merge here wrote, which this one may replace, and its lipidome.
    try:
        previous = json.loads((folder / MERGE_REPORT_NAME).read_text(encoding="utf-8"))
        replaceable = dict(((previous.get("outputs") or {}).get("dotplots") or {}).get("manifest") or {})
        previous_output = Path(str(previous.get("output") or ""))
    except (OSError, ValueError, AttributeError, TypeError):
        replaceable, previous_output = {}, None
    try:
        report = merge_pos_neg(
            records[POSITIVE]["outputs"]["normalized_copy"], records[NEGATIVE]["outputs"]["normalized_copy"],
            None, folder / name,
            rules=workbook["quant_info"],
            internal_standard_ids={labels[polarity]: records[polarity]["internal_standard_ids"] for polarity in POLARITIES},
            reference_matched_ids={
                labels[polarity]: reference_matched_ids(records[polarity]["candidate_table"]) for polarity in POLARITIES
            },
            expected_unit=contract["unit"],
            unquantified_ids={
                labels[polarity]: (records[polarity].get("verification") or {}).get("failed_ids", [])
                for polarity in POLARITIES
            },
            not_quantified_path=folder / NOT_QUANTIFIED_NAME,
            # Written below, once what this step adds (verification, unit, Console) is in it.
            report_path=None,
            reconcile=reconcile,
            reconciliation_rt_tolerance=tolerance,
            sample_file_types=sample_file_types,
            replaceable_dotplot_files=replaceable,
            rules_label="Sheet 3",
            selection_bound=_selection_bound(contract),
            write=write,
        )
    except MergeRefused as error:
        raise LabContractError(str(error)) from None
    if (previous_output is not None and previous_output.name and previous_output.name.casefold() != name.casefold()
            and (folder / previous_output.name).is_file()):
        # The three tables and the dotplots are rewritten under their own names; a lipidome named otherwise is not.
        report["warnings"].append(
            f"{previous_output.name}, the lipidome the earlier merge here wrote, stays beside this merge's {name}; the "
            "statistics tables and the dotplot folders are this merge's. Say which lipidome is current, or move the "
            "other out of the folder."
        )
    reconciliation = report["polarity_reconciliation"]
    reconciliation["deviations_from_contract"] = deviations
    report["warnings"].extend(f"Not as the contract states: {item}." for item in deviations)
    if reconcile:
        reconciliation["normalized_as"] = _reconciled_standards(contract)
        bmp = reconciliation["normalized_as"]["BMP"]
        if bmp["differs"] and (reconciliation["counts"].get("BMP") or {}).get("matched"):
            gives = (
                f"sheet 2 names {bmp['bmp_standard'] or 'no standard'} for BMP" if bmp["bmp_standard_from"] == "BMP"
                else f"sheet 2 names no standard for BMP, so BMP falls to Any others ({bmp['bmp_standard'] or 'no standard'})"
            )
            report["warnings"].append(
                f"The negative rows renamed BMP carry the concentration normalize computed for them as PG, against "
                f"{bmp['pg_standard'] or 'no standard'}; {gives}."
            )
    report["verification"] = {
        polarity: {"verified": records[polarity]["verified"],
                   "failed_rows": len((records[polarity].get("verification") or {}).get("failed_rows", []))}
        for polarity in POLARITIES
    }
    report["sample_parameter"] = contract["inputs"]["quant_table"].get("sample_parameter")
    report["unit"] = contract["unit"]
    # Which normalize build made each matrix, by its build and not its path.
    report["normalize_console"] = {
        polarity: {
            **{key: ((records[polarity].get("console") or {}).get("now") or {}).get(key, "")
               for key in ("version", *CONSOLE_BUILD_KEYS)},
            "changed_since_contract": bool((records[polarity].get("console") or {}).get("changed_since_contract")),
        }
        for polarity in POLARITIES
    }
    if write:
        target = folder / MERGE_REPORT_NAME
        target.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
        report["report"] = str(target.resolve())
        outputs = report["outputs"]
        record_event(contract["_path"], {
            "event": "merged", "output": report["output"], "output_sha256": report["output_sha256"],
            "output_folder": report["output_folder"],
            "tables": {
                Path(outputs[key]["path"]).name: outputs[key]["sha256"] for key in companion_names()
            },
            "dotplot_files": outputs["dotplots"]["files"],
            "kept_rows": report["kept_rows"], "not_quantified_rows": report["not_quantified"]["rows"],
            "sample_file_types": list(sample_file_types),
            "polarity_reconciliation": {
                "enabled": reconcile, "rt_tolerance_min": tolerance, "counts": reconciliation.get("counts", {}),
                "renamed_rows": reconciliation.get("renamed_rows", {}), "deviations_from_contract": deviations,
            },
        })
    return without_manifest(report)


def _selection_bound(contract: dict[str, Any]) -> str:
    """Why sheet 3 can no longer change at the merge: the contract holds the workbook."""
    accepted = (contract.get("merge") or {}).get("selection_warnings_accepted") or []
    return (
        ("The contract accepted this sheet 3 as it is (merge_selection_accepted). " if accepted else "")
        + "Sheet 3 is part of the contract: changing it now needs a new contract and new runs of both polarities."
    )


def _reconciled_standards(contract: dict[str, Any]) -> dict[str, Any]:
    """Which standard normalize quantified a renamed row against: its negative class's, not its new one's.

    A PG row renamed BMP was normalized as PG. Said beside the standard sheet 2 gives BMP (its own row,
    or 'Any others' when it names none), so a difference between the two is visible; EtherPE keeps its
    class, and so its standard.
    """
    lookup = {row["target_class"]: row["standard"] for row in contract.get("lookup", [])}
    fallback = lookup.get("Any others", "")

    def standard(klass: str) -> tuple[str, str]:
        return (lookup[klass], klass) if klass in lookup else (fallback, "Any others")

    pg_standard, pg_from = standard("PG")
    bmp_standard, bmp_from = standard("BMP")
    ether_standard, ether_from = standard("EtherPE")
    return {
        "BMP": {"normalized_as": "PG", "pg_standard": pg_standard, "pg_standard_from": pg_from,
                "bmp_standard": bmp_standard, "bmp_standard_from": bmp_from, "differs": pg_standard != bmp_standard},
        "EtherPE": {"normalized_as": "EtherPE", "standard": ether_standard, "standard_from": ether_from},
    }


# ---------------------------------------------------------------------------------------------
# Worksets
# ---------------------------------------------------------------------------------------------


def lab_workset_settings(contract_path: str) -> dict[str, Any]:
    """What a laboratory lipidomics workset carries from a contract to the next batch.

    The library, the salt, the tolerances, the internal-standard libraries, the workbook, the amount
    column and the method: each by path with its sha256, so the next check says when one changed.
    """
    contract = load_contract(contract_path)
    inputs = contract["inputs"]
    texts = inputs["internal_standard_texts"]
    return {
        "answers": {
            "lbm_path": inputs["lbm"]["path"],
            "solvent_type": contract["salt"]["salt"],
            "retention_time_tolerance": contract["retention_time_tolerance"],
            "internal_standard_rt_tolerance": contract["internal_standard_rt_tolerance"],
            "is_text_pos": texts[POSITIVE]["path"],
            "is_text_neg": texts[NEGATIVE]["path"],
            "quant_table_path": inputs["quant_table"]["path"],
            "amount_column": contract["amount_column"],
            "console_path": contract["console_path"],
            **({"number_of_threads": contract["number_of_threads"]} if contract.get("number_of_threads") else {}),
        },
        "files": {
            "lbm_path": {"path": inputs["lbm"]["path"], "sha256": inputs["lbm"]["sha256"]},
            "is_text_pos": dict(texts[POSITIVE]),
            "is_text_neg": dict(texts[NEGATIVE]),
            "quant_table_path": {"path": inputs["quant_table"]["path"], "sha256": inputs["quant_table"]["sha256"]},
        },
        "method": contract["method"],
        "source_contract_sha256": contract["_sha256"],
    }
