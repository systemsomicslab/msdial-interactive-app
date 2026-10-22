"""The laboratory LC-MS metabolomics agent's steps around MS-DIAL: check the inputs, fix them in a contract,
find the internal standards in each run, normalize by one of them, and merge the polarities.

A batch is one or two raw folders (positive, negative or both), an ordered list of annotation libraries
(lab_lcms_metabolomics.PRESETS), the salt, a text library of internal standards per polarity and the one
standard each polarity is normalized by. The sequence and what stops it follow the laboratory lipidomics
agent, whose helpers this module uses:

1. check_inputs reads everything and changes nothing; check_digest binds what it read. Each raw file's
   header gives its acquisition type and the analytical order (read_raw_headers); a type no header of the
   file's own settles, an AIF verdict included, is asked.
2. write_contract writes <analysis root>/inputs/: the contract, the internal-standard libraries in the
   layout MS-DIAL reads, and the record. Each polarity's guided plan is built from the contract.
3. resolve_standards finds every internal standard in each run (lab_lipidomics.resolve_standards, with the
   MSP libraries in the LBM library's place). Only the normalizing standard must be found.
4. normalize divides each feature by the normalizing standard's height and multiplies by its mean over the
   Sample and QC injections, in Python from the run's Height export; MS-DIAL's own internal-standard
   normalization averages over every file, Blank included, so it is not used. No Console is started.
5. merge writes one metabolome, one row per InChIKey first block (lab_lcms_metabolomics.merge_metabolome).

The contract and its record name the libraries and the raw data by their locations, so both are local
only (sharing.py). The Console is named per contract, never through Interactive's shared settings.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import math
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from . import __version__
from .agent_workflow import apply_sample_table
from .lab_lcms_metabolomics import (
    DUPLICATE_RULES,
    INCHIKEY_MATCHES,
    INTERNAL_STANDARD_PRIORITY,
    IS_TEXT_COLUMNS_METABOLOMICS,
    IS_TEXT_REQUIRED_METABOLOMICS,
    PAIRED_FIELDS,
    pair_by_shared_tokens,
    MERGE_REPORT_NAME,
    PRESET_PURPOSES,
    PRESETS,
    UNANNOTATED_NAMES,
    adduct_identities,
    annotation_summary,
    annotator_settings,
    export_annotator_ids,
    is_preferred,
    lbm_retention_time,
    library_identity,
    merge_metabolome,
    normalize_heights,
    normalized_text,
    preferred_adducts,
    read_annotation,
    retention_time_census,
    verify_normalized,
)
from .lab_lipidomics import (
    DEFAULT_INTERNAL_STANDARD_RT_TOLERANCE,
    INTERNAL_STANDARD_ANNOTATOR_IDS,
    NEGATIVE,
    POLARITIES,
    POSITIVE,
    InternalStandard,
    LabInputError,
    _is_zero_height,
    analysis_root_problems,
    chosen_candidate,
    file_sha256,
    folder_layout_problems,
    join_sample_metadata,
    normalize_salt,
    parse_number,
    raw_folder_overlaps,
    read_alignment_export,
    read_candidate_table,
    read_internal_standard_text,
    read_sample_metadata,
    representative_candidate,
    resolution_digest,
    resolve_standards,
    split_by_polarity,
    write_canonical_text_db,
    write_resolution_tsv,
)
from .lab_lipidomics_agent import CONTRACT_NAME as LIPIDOMICS_CONTRACT_NAME
from .lab_lipidomics_agent import (
    CONSOLE_BUILD_KEYS,
    LAB_LBM_QUERIES,
    LAB_PARAMETER_TEMPLATE,
    RAW_LOCATION_SENTENCE,
    RAW_LOCATIONS,
    _candidate_inputs,
    _confirmed_sample_table,
    _digest,
    _folder_inventory,
    _identity_sha256,
    _library_report,
    _path_key,
    _positive_number,
    _resolution_rows,
    _same_answer as _lipidomics_same_answer,
    _same_path,
    _sample_row,
    _sample_table_proposal as _paired_sample_table_proposal,
    _size,
    console_identity,
    polarity_sample_table,
    run_outputs,
)
from .lab_lipidomics import lab_method_answers
from .lipidome_merge import strip_polarity
from .raw_metadata_preflight import READ_OUTCOMES, file_key, input_format, input_signature, run_extractor
from .workflow import (
    ANNOTATION_CANDIDATES_CAPABILITY,
    AUTOMATIC_RT_LOCAL_SUPPORT_CAPABILITY,
    MULTI_ENERGY_AIF_CAPABILITY,
    _console_supports_folder_type_csv as console_supports_folder_type_csv,
    analysis_input_path,
    console_capabilities,
    console_safe_text,
)

CHECK_SCHEMA = "msdial-interactive.lab-lcms-metabolomics-check.v1"
CONTRACT_SCHEMA = "msdial-interactive.lab-lcms-metabolomics-contract.v1"
NORMALIZE_RECORD_SCHEMA = "msdial-interactive.lab-lcms-metabolomics-normalize.v1"
CONTRACT_NAME = "lab-lcms-metabolomics-contract.json"
RECORD_NAME = "lab-lcms-metabolomics-record.jsonl"
CONTRACT_KEY = "lab_lcms_metabolomics_contract_path"
PLAN_BLOCK = "lab_lcms_metabolomics"
INPUTS_FOLDER = "inputs"
QUANT_FOLDER = "quant"
FOLDERS = {POSITIVE: "pos", NEGATIVE: "neg"}
LABELS = {POSITIVE: "Pos", NEGATIVE: "Neg"}
# A sample-table row's file of each polarity.
FILE_KEYS = {POSITIVE: "positive_file", NEGATIVE: "negative_file"}
MERGE_FOLDER = "metabolome"
# Folder names inside quant that a merge may not take: normalize's, and the QA figures'.
RESERVED_QUANT_FOLDERS = frozenset({"pos", "neg", "qa_figures"})
RAW_LOCATION_QUESTION = (
    f"{RAW_LOCATION_SENTENCE} Are the raw folders the data as acquired (in_place: the files are written there) "
    "or a copy made for this analysis (copy)? A copy is made only on your word, before the contract."
)
ANNOTATION_QUESTION = (
    "Which libraries annotate the features, highest priority first? Choose a preset and name its files "
    "(annotation={'preset': ..., 'libraries': {slot: path or {'Positive': path, 'Negative': path}}}), or list "
    "the libraries yourself (annotation={'libraries': [{kind, path, thresholds, retention_time, rt_tolerance, "
    "label, library_kind}, ...]}). For each MSP: are its spectra acquired, or computed in silico (library_kind "
    "'predicted', which MS-DIAL then reports as in-silico evidence)? A preset slot may say so as "
    "{'path': ..., 'library_kind': 'predicted'}. Presets: "
    + "; ".join(
        f"{name}: {PRESET_PURPOSES[name]} (slots: {', '.join(dict.fromkeys(item['slot'] for item in PRESETS[name]))})"
        for name in PRESETS
    )
    + "."
)
DUPLICATE_RULE_QUESTION = (
    "When one compound is found more than once (several adducts, both polarities, or several spots), which row "
    "should the merge keep? evidence (recommended): the more reliable library match, one with your own RT and "
    "MS/MS before an MS/MS-only one, then the library's priority, then the preferred adduct and the intensity; "
    "intensity: the preferred adduct ([M+H]+, [M-H]-, the salt's adduct), then the larger intensity; keep_all: keep "
    "every row and only count the duplicates."
)
INCHIKEY_MATCH_QUESTION = (
    "What counts as one compound: the InChIKey's first block (the skeleton, so stereoisomers such as glucose and "
    "mannose 6-phosphate are one compound), or its first two blocks (stereoisomers apart)?"
)
# What a laboratory metabolomics run needs of its Console. Normalize runs in Python, so the Console's
# normalize --peak-ids-only is not among them.
LAB_CONSOLE_NEEDS = {
    ANNOTATION_CANDIDATES_CAPABILITY: "the annotation candidate table",
    AUTOMATIC_RT_LOCAL_SUPPORT_CAPABILITY: "automatic RT correction's local test (MsdialWorkbench#826)",
}
# Neither ending in a dot nor a space, which Windows strips from a folder name: "qa_figures." is qa_figures.
_FOLDER_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,62}[A-Za-z0-9_-])?$")
# The resolver's tiers for a spot a library or the standard's own text row annotated as that standard.
DECIDED_TIERS = ("A", "B")
WINDOWS_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{number}" for number in range(10)), *(f"lpt{number}" for number in range(10))}
)
# A project saved this long after the run's exports was saved again, as after curation in the GUI.
CURATION_SLACK_SECONDS = 30 * 60
# The acquisition types the Console reads (AnalysisFilesParser), in the case it writes them.
ACQUISITION_TYPES = ("DDA", "SWATH", "AIF")
# Up to this many raw files of a polarity have every header read: a Thermo or mzML header takes about half a
# second, a Waters one a few (raw_metadata_preflight). Past it only the first and the last file are read, and
# the type of the files between them is asked, with the type those two give as a suggestion only.
HEADER_READ_ALL_LIMIT = 200
# A check answers within the MCP request (600 s), write_contract's included. So its header reads stop starting
# extractor processes after HEADER_READ_BUDGET_SECONDS, and each input is given HEADER_TIMEOUT_POLICY's limit
# rather than the campaign's, which runs to hours for a large Waters folder. A file not read in time is asked
# about, and the next check reads it: the reads done are kept.
HEADER_READ_BUDGET_SECONDS = 240.0
HEADER_TIMEOUT_POLICY: dict[str, dict[str, float]] = {
    "metadata_reader": {"base_seconds": 30.0, "seconds_per_gb": 0.0},
    # About three times the worst measured Waters rate (42 s/GB) and five times the ion-mobility one.
    "full_spectrum": {"base_seconds": 30.0, "seconds_per_gb": 120.0},
    "ion_mobility": {"base_seconds": 30.0, "seconds_per_gb": 30.0},
}
HEADER_INPUT_TIMEOUT_CEILING_SECONDS = 180.0
# The extractor's Bruker BAF reader writes analysis.sqlite into a .d folder that has none, and the check
# writes nothing beside the raw data, so such a folder's header is not read.
WRITING_HEADER_FORMATS = frozenset({"bruker_baf"})
HEADER_ORDER_SOURCE = "raw_header_acquisition_start_time"
# What a check keeps of the campaign's per-file header summary (repository_reanalysis._summarize_raw_metadata).
HEADER_FIELDS = (
    "acquisition_mode", "method_source", "polarity", "separation", "isolation_window_count",
    "header_console_acquisition_type", "header_console_acquisition_basis", "instrument_model",
    "acquisition_start_time", "acquisition_start_time_evidence", "has_ms2",
    "ms2_collision_energies", "ms2_collision_energies_unresolved",
)
# The answer to acquisition_type_mixed_pos / _neg: the person keeps each file's own header type.
MIXED_AS_READ = "as_read"
# How a file's acquisition type was proposed. Only RAW_HEADER settles it; every other file is asked about
# (acquisition_type_pos / acquisition_type_neg), and the answer is ANSWERED.
RAW_HEADER = "raw_header"
ANSWERED = "answer"
OPEN_BASES = {
    "first_and_last_files": "inferred from the first and the last file, not read",
    "unread": "not read",
    "unresolved": "read, without a type the Console reads",
    "raw_header_aif": "read as AIF",
}
# A header polarity that is no single ion mode: MS-DIAL runs one per analysis.
SWITCHING_POLARITIES = frozenset({"PolaritySwitching", "MixedFunctions"})
# Header reads in this process, by the extractor build and the file as it was (path, size, modification time):
# {"entry": the summary, or None for a read that failed, "failure": its outcome}. A failed read is kept like a
# good one, so a check repeated does not wait for it again; a read the check's time limit cut short is not.
_HEADER_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}
# What a header read left in a folder input, or beside a file input, by that folder: kept for the process, so
# every later check, write_contract's included, says so again.
_HEADER_CREATED: dict[str, list[str]] = {}
# The inputs a check is reading now: a second check waits for that read rather than start another extractor on
# the same files.
_HEADER_IN_FLIGHT: dict[str, threading.Event] = {}
# The extractor build by the state of its folder (each file's path, size and modification time).
_EXTRACTOR_IDENTITIES: dict[tuple[Any, ...], dict[str, Any]] = {}
_HEADER_CACHE_LOCK = threading.Lock()


class LabMetabolomicsError(ValueError):
    """A contract or a step that cannot be used as it stands."""


def _polarities_given(args: dict[str, Any]) -> list[str]:
    return [polarity for polarity, key in ((POSITIVE, "pos_path"), (NEGATIVE, "neg_path"))
            if str(args.get(key) or "").strip()]


# ---------------------------------------------------------------------------------------------
# Step 1: look at the inputs
# ---------------------------------------------------------------------------------------------


def check_inputs(request: dict[str, Any]) -> dict[str, Any]:
    """Read every input of a laboratory LC-MS metabolomics batch and say what is wrong or still to be asked.

    Nothing is written. With only the raw folders it lists what is there; with the rest it checks each
    input and proposes the sample table. check_digest binds what was read.
    """
    args = {key: value for key, value in dict(request or {}).items() if value not in (None, "")}
    blockers: list[str] = []
    warnings: list[str] = []
    questions: list[dict[str, Any]] = []
    result: dict[str, Any] = {"schema": CHECK_SCHEMA, "interactive_version": __version__}

    def ask(identifier: str, prompt: str, choices: list[str] | None = None) -> None:
        questions.append({"id": identifier, "prompt": prompt, "choices": choices or [], "required": True})

    polarities = _polarities_given(args)
    if not polarities:
        blockers.append("Name pos_path, neg_path or both: the raw-data folder of each polarity acquired.")
    folders: dict[str, Path | None] = {}
    inventory: dict[str, Any] = {}
    for polarity in polarities:
        key = "pos_path" if polarity == POSITIVE else "neg_path"
        folder = Path(str(args[key]).strip()).expanduser().resolve()
        folders[polarity] = folder
        if not folder.is_dir():
            blockers.append(f"{key} {folder} is not a folder.")
            continue
        inventory[polarity] = _folder_inventory(folder, polarity)
        blockers.extend(inventory[polarity]["blockers"])
        warnings.extend(inventory[polarity]["warnings"])
        # Each raw file reaches the Console through analysis_files.csv, read as ASCII and split on commas. A
        # folder of such a path refuses every file; a file of its own may be left out of the sample table.
        unreadable = unreadable_raw_files(inventory[polarity]["files"])
        if unreadable and not console_safe_text(analysis_input_path(str(folder))):
            blockers.append(
                f"The Console cannot read the {polarity.lower()} raw folder {folder} by its path: analysis_files.csv "
                "carries printable ASCII without commas or quotes. Move or rename the raw data (a copy, raw_location "
                "'copy', is one way) before the contract."
            )
        elif unreadable:
            warnings.append(
                f"The Console cannot read the {polarity.lower()} raw files {', '.join(unreadable[:5])}"
                + (f" and {len(unreadable) - 5} more" if len(unreadable) > 5 else "")
                + " by their names (printable ASCII without commas or quotes): leave them out of the sample table "
                "(included false) or rename them; the contract refuses one that is included."
            )
    if folders.get(POSITIVE) and folders.get(NEGATIVE):
        blockers.extend(folder_layout_problems(folders[POSITIVE], folders[NEGATIVE]))

    # Each file's acquisition type and the order of acquisition, from its raw header where it can be read.
    # A type no header of the file's own settles is asked, never assumed: SWATH or AIF data run as DDA get
    # almost no MS/MS. A suggestion (the header's AIF, the two end files' type) is said, never filled in.
    headers = read_raw_headers(inventory, [polarity for polarity in polarities if polarity in inventory])
    warnings.extend(headers.pop("warnings"))
    blockers.extend(headers.pop("blockers"))
    proposed = headers.pop("_proposed")
    suggested = headers.pop("_suggested")
    header_entries = headers.pop("_entries")
    header_names = headers.pop("_names")
    header_orders = headers.pop("_orders")
    acquisition_answers: dict[str, str] = {}
    # Asked after the other questions, beside the sample table they belong to.
    acquisition_questions: list[tuple[str, str, list[str]]] = []
    for polarity in [polarity for polarity in polarities if polarity in inventory]:
        key = f"acquisition_type_{FOLDERS[polarity]}"
        report = headers["polarities"][polarity]
        items = proposed[polarity]
        names = header_names[polarity]
        text = str(args.get(key) or "").strip()
        answer = next((value for value in ACQUISITION_TYPES if value.casefold() == text.casefold()), "")
        if text and not answer:
            blockers.append(
                f"{key} '{text}' is none of DDA, SWATH or AIF, the words the Console reads; DIA data with isolation "
                "windows are SWATH, and without isolation AIF."
            )
        open_files = [name for name, (value, _basis) in items.items() if value is None]
        open_bases = _count(items[name][1] for name in open_files)
        open_text = ", ".join(f"{count} {OPEN_BASES.get(basis, basis)}" for basis, count in open_bases.items())
        settled = _count(value for value, basis in items.values() if basis == RAW_HEADER)
        hints = _count(suggested[polarity][name] for name in open_files if name in suggested[polarity])
        if open_files and answer:
            acquisition_answers[key] = answer
            for name in open_files:
                items[name] = (answer, ANSWERED)
            if set(settled) - {answer}:
                warnings.append(
                    f"{key} is {answer} for the {len(open_files)} {polarity.lower()} file(s) no header of their own "
                    f"settled ({open_text}); the {sum(settled.values())} file(s) read from their own headers keep their "
                    "headers' type (" + ", ".join(f"{name} {count}" for name, count in settled.items()) + "). Correct a "
                    "row in sample_table if a header is wrong."
                )
        elif open_files:
            acquisition_questions.append((
                key,
                f"Which acquisition type are the {polarity.lower()} raw files: DDA, SWATH (DIA with isolation windows) "
                "or AIF (DIA without isolation)? MS-DIAL deconvolutes SWATH and AIF spectra and takes DDA spectra as "
                "they are; data run as the wrong type get almost no MS/MS. "
                + (report["unresolved_why"] + " " if report["unresolved_why"] else "")
                + f"The answer is taken for the {len(open_files)} file(s) no header of their own settled ({open_text})."
                + (" The headers suggest " + ", ".join(f"{name} for {count}" for name, count in hints.items())
                   + " of them, a suggestion only." if hints else "")
                + (f" The {sum(settled.values())} file(s) read from their own headers keep their type ("
                   + ", ".join(f"{name} {count}" for name, count in settled.items()) + ")." if settled else ""),
                list(ACQUISITION_TYPES),
            ))
        elif answer:
            warnings.append(
                f"{key} is {answer}, but every {polarity.lower()} file's own header settled its type, so the answer "
                "is not used."
            )
        # Files whose own headers give different types: a misread header and a batch acquired so look alike.
        if len(settled) > 1:
            mixed_key = f"acquisition_type_mixed_{FOLDERS[polarity]}"
            mixed = str(args.get(mixed_key) or "").strip().casefold()
            if mixed and mixed != MIXED_AS_READ:
                blockers.append(f"{mixed_key} '{args.get(mixed_key)}' is not {MIXED_AS_READ}.")
            elif mixed:
                acquisition_answers[mixed_key] = MIXED_AS_READ
            else:
                listed = "; ".join(
                    f"{value} {count}: " + _names_text([names[name] for name, (kind, basis) in items.items()
                                                        if basis == RAW_HEADER and kind == value])
                    for value, count in sorted(settled.items(), key=lambda item: (item[1], item[0]))
                )
                acquisition_questions.append((
                    mixed_key,
                    f"The {polarity.lower()} files' own headers give more than one acquisition type ({listed}); "
                    "raw_headers.per_file gives each file's method and isolation targets. Were they acquired so? "
                    f"Answer {MIXED_AS_READ} to keep each file's header type. A row whose header is wrong is corrected "
                    "in sample_table (its acquisition_type), on the person's word.",
                    [MIXED_AS_READ],
                ))
        final = [value for value, _basis in items.values()]
        report["acquisition_types"] = _count(value or "unresolved" for value in final)
        report["proposed_by"] = _count(basis for _value, basis in items.values())
        if len({value for value in final if value}) > 1:
            warnings.append(
                f"The {polarity.lower()} files are of more than one acquisition type ("
                + ", ".join(f"{name} {count}" for name, count in report["acquisition_types"].items())
                + "); each row is proposed with its own, and raw_headers.per_file gives each file's method and "
                "isolation targets. Confirm them."
            )
        if report["coverage"] == "first_and_last":
            warnings.append(
                f"Only the first and the last of the {report['files']} {polarity.lower()} raw files were read (more "
                f"than {HEADER_READ_ALL_LIMIT}); the {open_bases.get('first_and_last_files', 0)} files between them "
                f"are asked about ({key}), and the type those two give is never filled in for them."
            )
    readable_orders = [polarity for polarity in polarities if polarity in inventory]
    order_used = bool(readable_orders) and all(header_orders.get(polarity) for polarity in readable_orders)
    order_reasons = {polarity: headers["polarities"][polarity]["order"]["reason"] for polarity in readable_orders}
    if order_used:
        tied = [name for polarity in readable_orders for name in headers["polarities"][polarity]["order"].get("tied", [])]
        headers["order"] = {
            "derived_from": HEADER_ORDER_SOURCE,
            "reason": "Ranked by the acquisition start time each file's raw header records"
                      + (f"; files of one time keep their listing order ({', '.join(tied)})" if tied else "") + ".",
            "agrees_with_file_listing": all(headers["polarities"][polarity]["order"].get("agrees_with_listing")
                                            for polarity in readable_orders),
            "confirmation_required": True,
        }
    else:
        headers["order"] = {
            "derived_from": None,
            "reason": "The order was not taken from the raw headers ("
                      + "; ".join(f"{polarity.lower()}: {reason}" for polarity, reason in order_reasons.items())
                      + "); the files keep the order proposed from their names or their listing.",
        }
    result["raw_headers"] = headers
    result["polarities"] = polarities
    result["folders"] = {
        polarity: {key: value for key, value in item.items() if key not in {"files", "blockers", "warnings"}}
        for polarity, item in inventory.items()
    }
    result["candidate_inputs"] = _metabolomics_candidate_inputs([folder for folder in folders.values() if folder])

    if not str(args.get("analysis_purpose") or "").strip():
        ask(
            "analysis_purpose",
            "What do you want to learn from this analysis: the question, the biological comparison, "
            "whether annotation or comparative profiling is central, and the outputs you need?",
        )

    # The annotation libraries, each with its own retention-time use and thresholds.
    annotation = read_annotation(args.get("annotation"), polarities)
    blockers.extend(annotation["blockers"])
    warnings.extend(annotation["warnings"])
    if annotation["missing"]:
        ask("annotation", ANNOTATION_QUESTION + (
            f" Still missing: {', '.join(annotation['missing'])}." if annotation["missing"] != ["annotation"] else ""
        ))
    identities: dict[str, dict[str, Any]] = {}
    census: dict[str, dict[str, Any]] = {}
    if not annotation["blockers"] and not annotation["missing"]:
        for entry in annotation["entries"]:
            for polarity, path in entry["paths"].items():
                identities.setdefault(_path_key(path), library_identity(path))
                if entry["kind"] != "msp" or entry["retention_time"] != "use":
                    continue
                # MS-DIAL keeps a record without an RT in every RT window and counts the RT as met: in a
                # library searched with RT, such a record is matched on MS/MS alone under the RT tier's label.
                counted = retention_time_census(path)
                census[f"{entry['label']} ({polarity.lower()})"] = counted
                if counted["records"] and not counted["with_retention_time"]:
                    blockers.append(
                        f"{entry['label']} is searched with retention time, but none of the {counted['records']} records "
                        f"of {Path(path).name} gives one; search it without RT (retention_time 'none')."
                    )
                elif counted["with_retention_time"] < counted["records"]:
                    warnings.append(
                        f"{counted['records'] - counted['with_retention_time']} of the {counted['records']} records of "
                        f"{Path(path).name} give no retention time; MS-DIAL matches those on MS/MS alone, under the "
                        f"label '{entry['label']}'."
                    )
    result["annotation"] = {
        "preset": annotation["preset"],
        "libraries": annotation_summary(annotation["entries"]),
        "internal_standard_priority": INTERNAL_STANDARD_PRIORITY,
        # One row per library and polarity: two libraries may share a file name and not their content.
        "files": [
            {"label": entry["label"], "polarity": polarity, "name": Path(path).name,
             **{key: identities.get(_path_key(path), {}).get(key) for key in ("sha256", "size_bytes")}}
            for entry in annotation["entries"] for polarity, path in entry["paths"].items()
        ],
        "retention_time_census": census,
    }

    # The salt: the LBM2's negative records, and the negative adduct the merge prefers.
    salt: dict[str, Any] = {}
    salt_text = str(args.get("solvent_type") or "").strip()
    if not salt_text:
        ask(
            "solvent_type",
            "Which salt or acid is in the mobile phase (ammonium acetate, ammonium formate or formic acid, or "
            "ammonium bicarbonate)? It sets the negative adduct the merge prefers besides [M-H]-, and the "
            "negative LBM2 records MS-DIAL searches.",
            ["CH3COONH4", "HCOONH4", "NH4HCO3"],
        )
    else:
        salt = normalize_salt(salt_text)
        if salt["problem"]:
            blockers.append(f"solvent_type: {salt['problem']}")
    result["salt"] = {**salt, "preferred_adducts": preferred_adducts(salt.get("salt")) if salt.get("salt") else []}

    # How the merge treats one compound found more than once: the person's choices, asked before anything runs.
    merge_choices: dict[str, str] = {}
    for key, choices, prompt in (
        ("duplicate_rule", DUPLICATE_RULES, DUPLICATE_RULE_QUESTION),
        ("inchikey_match", INCHIKEY_MATCHES, INCHIKEY_MATCH_QUESTION),
    ):
        answer = str(args.get(key) or "").strip().casefold()
        if not answer:
            ask(key, prompt, list(choices))
        elif answer not in choices:
            blockers.append(f"{key} '{args.get(key)}' is none of {', '.join(choices)}.")
        else:
            merge_choices[key] = answer
    result["merge_choices"] = merge_choices

    # The internal-standard text libraries and the normalizing standard of each polarity.
    libraries: dict[str, list[InternalStandard]] = {}
    library_reports: dict[str, Any] = {}
    texts = {POSITIVE: str(args.get("is_text_pos") or "").strip(), NEGATIVE: str(args.get("is_text_neg") or "").strip()}
    both = str(args.get("is_text") or "").strip()
    if both:
        texts = {polarity: texts[polarity] or both for polarity in POLARITIES}
    for polarity in polarities:
        text = texts[polarity]
        if not text:
            ask(
                f"is_text_{FOLDERS[polarity]}",
                f"Which text library lists the {polarity.lower()} internal standards (Name, MZ, RT, Adduct and "
                "Ontology, MZ being the adduct's m/z)? One file holding both polarities may be named for both.",
            )
            continue
        # MS-DIAL's own text library may be four columns (Name, m/z, RT, Adduct): no class is needed here.
        read = read_internal_standard_text(Path(text).expanduser(), columns=IS_TEXT_COLUMNS_METABOLOMICS,
                                           required=IS_TEXT_REQUIRED_METABOLOMICS)
        blockers.extend(read["blockers"])
        warnings.extend(read["warnings"])
        records = split_by_polarity(read["records"])[polarity] if not read["blockers"] else []
        libraries[polarity] = records
        shared = sum(1 for other in polarities if texts[other] and _same_path(texts[other], text)) > 1
        library_reports[polarity] = _library_report(read, records, polarity, shared=shared)
        if not records and not read["blockers"]:
            blockers.append(f"The {polarity.lower()} internal-standard library lists no standard of that polarity.")
    result["internal_standard_libraries"] = library_reports

    normalizing: dict[str, Any] = {}
    for polarity in polarities:
        key = f"normalizing_standard_{FOLDERS[polarity]}"
        name = str(args.get(key) or "").strip()
        records = libraries.get(polarity)
        if not name:
            if polarity in library_reports:
                ask(key, f"Which {polarity.lower()} internal standard normalizes every feature? "
                         f"The library lists {', '.join(record.name for record in records or []) or 'none'}.",
                    [record.name for record in records or []])
            else:
                ask(key, f"Which {polarity.lower()} internal standard normalizes every feature?")
            continue
        if records is not None:
            match = [record for record in records if record.name == name]
            if not match:
                blockers.append(
                    f"{key} '{name}' is not in the {polarity.lower()} internal-standard library "
                    f"({', '.join(record.name for record in records) or 'empty'})."
                )
            else:
                normalizing[polarity] = {"name": name, "mz": match[0].mz, "rt": match[0].rt, "adduct": match[0].adduct}
    result["normalizing_standards"] = normalizing

    is_rt_tolerance = _positive_number(args.get("internal_standard_rt_tolerance", DEFAULT_INTERNAL_STANDARD_RT_TOLERANCE))
    if is_rt_tolerance is None:
        blockers.append(
            f"internal_standard_rt_tolerance '{args.get('internal_standard_rt_tolerance')}' is not a positive number of minutes."
        )
    # Optional: the template's value stands unless the person gives one (CE migration times usually need more).
    alignment_rt_tolerance: float | None = None
    if args.get("alignment_rt_tolerance") not in (None, ""):
        alignment_rt_tolerance = _positive_number(args.get("alignment_rt_tolerance"))
        if alignment_rt_tolerance is None:
            blockers.append(
                f"alignment_rt_tolerance '{args.get('alignment_rt_tolerance')}' is not a positive number of minutes."
            )
    result["tolerances"] = {"internal_standard_rt": is_rt_tolerance, "alignment_rt": alignment_rt_tolerance}

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
        blockers.extend(other_contract_problems(analysis_root))
        if not all(" " <= character <= "~" for character in str(analysis_root)):
            # inputs/is_<pol>.txt reaches text_annotator_settings.tsv, which is written and read as ASCII.
            blockers.append(
                f"{analysis_root} is not an ASCII path, which the Console's annotator settings file cannot carry; choose "
                "an analysis root whose path is ASCII."
            )
        result["outputs"] = {
            "inputs": str(analysis_root / INPUTS_FOLDER), "quant": str(analysis_root / QUANT_FOLDER),
            **{polarity: str(analysis_root / FOLDERS[polarity]) for polarity in polarities},
        }

    raw_location = str(args.get("raw_location") or "").strip().casefold()
    if not raw_location:
        ask("raw_location", RAW_LOCATION_QUESTION, list(RAW_LOCATIONS))
    elif raw_location not in RAW_LOCATIONS:
        blockers.append(f"raw_location '{args.get('raw_location')}' is neither {' nor '.join(RAW_LOCATIONS)}.")
    result["raw_location"] = {"answer": raw_location, "writes_beside_raw": RAW_LOCATION_SENTENCE}

    console_text = str(args.get("console_path") or "").strip()
    console: dict[str, Any] = {}
    if not console_text:
        ask("console_path", "Which MS-DIAL Console (MSDIALCUI) runs this analysis?")
    else:
        console = _console_verdict(Path(console_text).expanduser())
        blockers.extend(console.pop("blockers"))
        # Agilent and Bruker .d or Waters .raw folders reach the Console as folder paths, which only a build
        # from MsdialWorkbench#739 reads; every plan would refuse another, after the contract fixed it.
        folder_inputs = any(Path(str(item.get("file_path", ""))).is_dir()
                            for entry in inventory.values() for item in entry["files"])
        if folder_inputs and Path(console_text).expanduser().is_file() and not console_supports_folder_type_csv(console_text):
            blockers.append(
                f"The raw data are folders (.d or .raw), and the Console {Path(console_text).name} cannot be shown to read "
                "them: a Console qualifies when its build record (msdial-console-build-provenance.json) verifies against "
                "the binary and records a commit descending from MsdialWorkbench#739. "
                "msdial_build_console_from_local_source writes that record."
            )
    result["console"] = console

    for item in acquisition_questions:
        ask(*item)
    header_types = {polarity: {name: value for name, (value, _basis) in items.items()} for polarity, items in proposed.items()}
    # What the Console does with the files to run as AIF: the repository campaign's collision-energy guards.
    aif = aif_problems(header_types, header_entries, header_names, console.get("capabilities"))
    blockers.extend(aif["blockers"])
    warnings.extend(aif["warnings"])
    proposal = _sample_table_proposal(inventory, polarities, args.get("sample_pairs"), {
        "types": header_types,
        "bases": {polarity: {name: basis for name, (_value, basis) in items.items()} for polarity, items in proposed.items()},
        "orders": {polarity: header_orders[polarity] for polarity in header_orders} if order_used else {},
        "order": headers["order"],
    })
    blockers.extend(proposal.pop("blockers"))
    warnings.extend(proposal.get("pairing_warnings", []))
    metadata_text = str(args.get("sample_metadata_path") or "").strip()
    metadata_report: dict[str, Any] = {}
    if metadata_text and inventory:
        # Compared with the table proposed, whose order and classes may be the headers' and the letters'.
        metadata_report = _sample_metadata_against_proposal(Path(metadata_text).expanduser(), inventory,
                                                            proposal.get("rows", []))
        blockers.extend(metadata_report.pop("blockers"))
        warnings.extend(metadata_report.pop("warnings"))
    result["paired_sample_table"] = proposal
    result["sample_metadata"] = metadata_report
    # The proposal as it stands, so that a class read from a name the Console cannot carry is said before it is confirmed.
    warnings.extend(analysis_csv_problems(proposal.get("rows", []), {}))
    if proposal.get("rows") and not any(str(row.get("file_type")) in {"Sample", "QC"} for row in proposal["rows"]):
        warnings.append("The proposed table has no Sample or QC injection; the normalizing standard has nothing to average over.")

    threads = args.get("number_of_threads")
    if threads not in (None, "") and _positive_number(threads) is None:
        blockers.append(f"number_of_threads '{threads}' is not a positive whole number.")
    result["stated_defaults"] = {
        **lab_method_answers(),
        "internal_standard_rt_tolerance": is_rt_tolerance,
        "alignment_rt_tolerance": (
            alignment_rt_tolerance if alignment_rt_tolerance is not None
            else f"the parameter template's {template_alignment_rt_tolerance()} min, unless alignment_rt_tolerance is given"
        ),
        "internal_standard_search": (
            f"each internal standard is looked for at its adduct's m/z within {is_rt_tolerance} min of its text RT: "
            "first a spot a library annotated as it, then one its own text row annotated, then one its row matched "
            "under another annotation, then any spot there; the last two only on the person's word"
        ),
        "normalization": (
            "normalized = height / height of the normalizing standard in that injection x the standard's mean "
            "height over the Sample and QC injections, x the injection's dilution factor; a Sample or QC without "
            "the standard blocks, and so does one where MS-DIAL did not detect it (a gap fill) unless the person "
            "accepts those heights; a Blank or Standard injection without it, with it undetected, or below 10% of "
            "the lowest Sample or QC height keeps its raw height"
        ),
        "merge": (
            "unannotated rows and internal standards are dropped; by default only reference-matched rows are kept "
            "(reference_matched_only) and every adduct is (preferred_adducts_only off); rows sharing the InChIKey's "
            "first block are one compound, within and across polarities, and the row kept is the preferred adduct, "
            "then the MS/MS-matched, then the larger mean over Sample and QC. The matrices are the exports the run "
            "wrote: curation in the GUI afterwards does not reach them"
        ),
        "method_files": {key: {"name": item["name"], "sha256": item["sha256"]} for key, item in lab_method_files().items()},
    }
    identity = _input_identity(args, inventory, identities, annotation, library_reports, metadata_report, normalizing)
    # What the headers proposed for every file, and what a read left beside the raw data, so a re-read that
    # proposes otherwise is a changed input.
    identity["raw_headers"] = _digest({
        polarity: sorted([name, value, basis, (header_orders.get(polarity) or {}).get(name) if order_used else None]
                         for name, (value, basis) in items.items())
        for polarity, items in sorted(proposed.items())
    } | {"reader_created_files": {polarity: report["reader_created_files"]
                                  for polarity, report in sorted(headers["polarities"].items())}})
    result.update({
        "questions": questions,
        "blockers": list(dict.fromkeys(blockers)),
        "warnings": list(dict.fromkeys(warnings)),
        "ready_for_contract": not questions and not blockers,
        "input_identity": identity,
        "check_digest": _digest(identity),
    })
    result["_parsed"] = {
        "args": args, "folders": folders, "inventory": inventory, "libraries": libraries, "salt": salt,
        "analysis_root": analysis_root, "is_rt_tolerance": is_rt_tolerance, "console": console,
        "metadata": metadata_report, "annotation": annotation, "identities": identities,
        "normalizing": normalizing, "polarities": polarities, "merge_choices": merge_choices,
        "alignment_rt_tolerance": alignment_rt_tolerance, "acquisition_answers": acquisition_answers,
        "header_entries": header_entries, "header_names": header_names,
    }
    return result


def public_check(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if not key.startswith("_")}


def _analysis_subfolders(root: Path) -> dict[str, Path]:
    return {
        **{f"{polarity.lower()} output folder": root / FOLDERS[polarity] for polarity in POLARITIES},
        "inputs folder": root / INPUTS_FOLDER, "quant folder": root / QUANT_FOLDER,
    }


def unreadable_raw_files(files: Iterable[dict[str, Any]]) -> list[str]:
    """The raw files analysis_files.csv cannot name: a path or file name outside printable ASCII, or with a comma or quote."""
    return [str(item.get("file_name")) for item in files
            if not console_safe_text(analysis_input_path(str(item.get("file_path", ""))))
            or not console_safe_text(item.get("file_name", ""))]


def analysis_csv_problems(rows: Iterable[dict[str, Any]], inventory: dict[str, Any]) -> list[str]:
    """What the included rows would write into analysis_files.csv that the Console reads otherwise.

    It is read as ASCII and split on commas without quoting: an unreadable file of an included row fails the
    run at prepare, and a class or acquisition type with a quote or outside ASCII is read as another word.
    """
    problems: list[str] = []
    rows = list(rows)
    for polarity, item in inventory.items():
        kept = apply_sample_table(item["files"], polarity_sample_table(rows, polarity))["files"]
        unreadable = unreadable_raw_files(kept)
        if unreadable:
            problems.append(
                f"The included {polarity.lower()} raw files {', '.join(unreadable[:5])} have names analysis_files.csv "
                "cannot carry (printable ASCII without commas or quotes); leave them out (included false) or rename them."
            )
    for row in rows:
        if not row_included(row):
            continue
        for key in ("class_id", "acquisition_type"):
            value = row.get(key)
            if value not in (None, "") and not console_safe_text(str(value).strip()):
                problems.append(
                    f"{row['stem']}: {key} '{value}' is not printable ASCII without commas or quotes, which "
                    "analysis_files.csv carries; give it in ASCII (a Japanese class name, for one, as a romanized word)."
                )
        acquisition = str(row.get("acquisition_type") or "").strip()
        if acquisition and acquisition.casefold() not in CONSOLE_ACQUISITION_TYPES:
            # AnalysisFilesParser parses the enum and reads any other word as DDA, without a word.
            problems.append(
                f"{row['stem']}: acquisition_type '{acquisition}' is none of DDA, SWATH or AIF, the words the Console "
                "reads; it would read it as DDA. Which of them the data are (DIA as SWATH or AIF) is the person's call."
            )
        for key in ("analytical_order", "batch_order"):
            number = parse_number(row.get(key)) if row.get(key) not in (None, "") else None
            if number is not None and number > CONSOLE_INT32_MAX:
                problems.append(
                    f"{row['stem']}: {key} {row.get(key)} is beyond what the Console reads as a number; it would use the "
                    "row's position instead. Give ranks from 1."
                )
    return problems


# What the Console's analysis-file parser reads: the AcquisitionType words, and an Int32.
CONSOLE_ACQUISITION_TYPES = frozenset({"dda", "swath", "aif"})
CONSOLE_INT32_MAX = 2147483647


def row_included(row: dict[str, Any]) -> bool:
    """Whether a sample-table row is in the run, read as apply_sample_table reads it."""
    value = row.get("included", True)
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"true", "1", "yes"}


def console_acquisition_type(value: Any) -> str | None:
    """The Console's word for an acquisition type given in any case (DDA, SWATH, AIF), or None."""
    text = str(value or "").strip().casefold()
    return next((word for word in ACQUISITION_TYPES if word.casefold() == text), None)


def aif_problems(
    types: dict[str, dict[str, Any]], entries: dict[str, dict[str, dict[str, Any]]], names: dict[str, dict[str, str]],
    capabilities: Iterable[str] | None,
) -> dict[str, list[str]]:
    """What the Console does with the files to run as AIF, from the MS2 collision energies their headers record.

    The repository campaign's guards (raw_metadata_preflight, AIF as SWATH): AIF of more than one MS2 collision
    energy needs a Console with MsdialWorkbench#825 to settle each spot's representative energy, and that Console
    stops on an AIF file whose MS2 scans carry none. types: {polarity: {file name casefolded: type}}; entries:
    the header summaries by the same keys; capabilities None while the Console is not named, when nothing is
    said yet. Whether AIF of one energy runs as SWATH, as the campaign runs it, is the person's answer.
    """
    blockers: list[str] = []
    warnings: list[str] = []
    if capabilities is None:
        return {"blockers": blockers, "warnings": warnings}
    ready = MULTI_ENERGY_AIF_CAPABILITY in set(capabilities)
    for polarity, items in types.items():
        multi: list[str] = []
        unrecorded: list[str] = []
        unread: list[str] = []
        for key, value in items.items():
            if console_acquisition_type(value) != "AIF":
                continue
            name = (names.get(polarity) or {}).get(key, key)
            entry = (entries.get(polarity) or {}).get(key)
            energies = (entry or {}).get("ms2_collision_energies")
            if entry is None:
                unread.append(name)
            elif not isinstance(energies, list) or not energies:
                unrecorded.append(name)
            elif len(set(energies)) > 1:
                multi.append(f"{name} ({_energies_text(entry)})")
        side = polarity.lower()
        if multi and not ready:
            blockers.append(
                f"The {side} files {_names_text(multi)} are to run as AIF, and their headers record more than one MS2 "
                "collision energy. The Console lacks MsdialWorkbench#825's multi-energy AIF processing, so nothing "
                "settles a spot's representative energy, and the repository campaign holds such data. Name a Console "
                "with #825, or another type on the person's word."
            )
        if unrecorded and ready:
            blockers.append(
                f"The {side} files {_names_text(unrecorded)} are to run as AIF, and their headers record no MS2 "
                "collision energy. The Console carries MsdialWorkbench#825, which stops on an AIF file whose MS2 scans "
                "carry none."
            )
        elif unrecorded:
            warnings.append(
                f"The {side} files {_names_text(unrecorded)} are to run as AIF, and their headers record no MS2 collision "
                "energy, so that they share one cannot be shown; the repository campaign holds such AIF data."
            )
        if unread:
            warnings.append(
                f"The {side} files {_names_text(unread)} are to run as AIF, and their headers were not read, so their MS2 "
                "collision energies are not known: AIF of more than one energy needs a Console with MsdialWorkbench#825, "
                "which stops on an AIF file that records none."
            )
    return {"blockers": blockers, "warnings": warnings}


def _sample_metadata_against_proposal(path: Path, inventory: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The person's sample metadata, compared with the table proposed (lab_lipidomics_agent._sample_metadata_report
    compares it with the names): the headers may set the order, and the letters of shared words the classes."""
    if not path.is_file():
        return {"path": str(path), "blockers": [f"sample_metadata_path {path} is not a file."], "warnings": []}
    try:
        metadata = read_sample_metadata(path)
    except (LabInputError, OSError, UnicodeDecodeError) as error:
        return {"path": str(path), "blockers": [f"{path.name} cannot be read: {error}"], "warnings": []}
    warnings: list[str] = list(metadata["warnings"])
    joined = {}
    for polarity in inventory:
        report = join_sample_metadata(metadata, polarity_sample_table(rows, polarity))
        joined[polarity] = {
            "matched": sum(1 for entry in report["files"] if entry["metadata"]),
            "unmatched": report["unmatched"],
            "disagreements": report["disagreements"],
        }
        for disagreement in report["disagreements"]:
            warnings.append(
                f"{path.name} gives {disagreement['field']} {disagreement['metadata']!r} for "
                f"{disagreement['file_name']}; the proposed table gives {disagreement['proposed']!r}."
            )
    rows_read = metadata["rows"]
    return {
        "path": metadata["path"], "sha256": file_sha256(path), "rows": len(rows_read), "joined": joined,
        "excluded": [row["file_name"] for row in rows_read if not row["included"]],
        "factors": sorted({row["factor"] for row in rows_read if row.get("factor")}),
        "acquisition_types": sorted({row["acquisition_type"] for row in rows_read if row.get("acquisition_type")}),
        "blockers": list(metadata["blockers"]), "warnings": warnings,
    }


def other_contract_problems(root: Path) -> list[str]:
    """A laboratory lipidomics analysis in this root: the two agents write the same file names there."""
    other = root / INPUTS_FOLDER / LIPIDOMICS_CONTRACT_NAME
    if other.exists():
        return [
            f"{root} holds a laboratory lipidomics analysis ({other.name}); its internal-standard libraries, run "
            "folders and normalized matrices have the names this analysis would write. Give the metabolomics "
            "analysis its own root."
        ]
    return []


def _metabolomics_candidate_inputs(folders: list[Path]) -> dict[str, Any]:
    """The lipidomics listing, and the MSP libraries beside the data: listed, never chosen."""
    found = _candidate_inputs(folders)
    found["msp_libraries"] = []
    seen: set[Path] = set()
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
                if path.is_file() and path.suffix.casefold() == ".msp":
                    found["msp_libraries"].append({"path": str(path), "size_bytes": path.stat().st_size, "proposed": False})
    return found


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
        blockers.append(f"The Console {path.name} lacks {', '.join(missing)}, which a laboratory metabolomics run needs.")
    verdict["blockers"] = blockers
    return verdict


# ---------------------------------------------------------------------------------------------
# The raw headers: each file's acquisition type, and the order the files were acquired in
# ---------------------------------------------------------------------------------------------


def _configured_extractor() -> dict[str, Any]:
    """The raw-metadata extractor a preflight outside a campaign runs: {"path", "source"}, path "" when none."""
    from .raw_metadata_extractor import select_raw_metadata_extractor

    try:
        selected = select_raw_metadata_extractor("")
    except Exception as error:  # noqa: BLE001 - no extractor is a question to the person, never a failed check
        return {"path": "", "source": "", "error": str(error)}
    return {"path": str(selected.get("path") or ""), "source": str(selected.get("source") or "")}


def _extractor_identity(path: str | Path) -> dict[str, Any]:
    """The extractor build headers are read with: its exe and every file beside it (the vendor readers), hashed.

    inspect_raw_metadata_extractor's hashes and provenance, worked out once per state of the extractor's
    folder in this process: a reader assembly replaced beside an unchanged exe is another build.
    """
    from .raw_metadata_extractor import inspect_raw_metadata_extractor

    binary = Path(str(path))
    try:
        state: tuple[Any, ...] | None = tuple(sorted(
            (item.relative_to(binary.parent).as_posix(), stat.st_size, stat.st_mtime_ns)
            for item in binary.parent.rglob("*") if item.is_file() for stat in (item.stat(),)
        ))
    except (OSError, ValueError):
        state = None
    key = (file_key(binary), state)
    with _HEADER_CACHE_LOCK:
        cached = _EXTRACTOR_IDENTITIES.get(key) if state is not None else None
    if cached is not None:
        return dict(cached)
    try:
        inspection = inspect_raw_metadata_extractor(binary)
    except Exception:  # noqa: BLE001 - an extractor that cannot be hashed is recorded without hashes
        inspection = {}
    identity = {name: inspection.get(name) for name in (
        "binary_sha256", "inventory_sha256", "file_count", "product_version", "msrawdataworkbench_commit",
        "msdialworkbench_commit", "pin_state", "provenance_status",
    )}
    if state is not None:
        with _HEADER_CACHE_LOCK:
            _EXTRACTOR_IDENTITIES[key] = dict(identity)
    return identity


def _extractor_key(path: Path, identity: dict[str, Any]) -> tuple[Any, ...]:
    """What a cached header read is keyed by: the extractor's build, or its exe as it was when it has no hash."""
    if identity.get("inventory_sha256"):
        return ("inventory", identity["inventory_sha256"])
    own = input_signature(path) or {}
    return ("exe", file_key(path), own.get("size_bytes"), own.get("mtime_ns"))


def _folder_names(folder: Path) -> set[str] | None:
    try:
        return {item.name for item in folder.iterdir()}
    except OSError:
        return None


def _record_created(key: str, paths: Iterable[str]) -> None:
    found = [str(item) for item in paths if str(item).strip()]
    if not found:
        return
    with _HEADER_CACHE_LOCK:
        _HEADER_CREATED[key] = list(dict.fromkeys([*_HEADER_CREATED.get(key, []), *found]))


def created_beside_raw(paths: Iterable[Path]) -> list[str]:
    """The files header reads of this process left in these inputs (folders) or beside them (files)."""
    keys = list(dict.fromkeys(file_key(path) if path.is_dir() else file_key(path.parent) for path in paths))
    with _HEADER_CACHE_LOCK:
        return list(dict.fromkeys(item for key in keys for item in _HEADER_CREATED.get(key, [])))


def _read_headers(extractor: Path, extractor_key: tuple[Any, ...], paths: list[Path], deadline: float) -> dict[str, Any]:
    """Each path's header, summarized as the repository campaign summarizes it (by file_key).

    A file read before by the same extractor build, at the same size and modification time, is taken from
    this process's cache, a read that failed included. A file another check is reading now is waited for, up
    to the deadline. The rest are read in one run_extractor call: bounded chunks, HEADER_TIMEOUT_POLICY per
    input, the deadline over all of them, into a temporary folder that is removed afterwards. The folder of
    each file input is listed before and after, so a reader that writes beside a file is seen as one that
    writes into a folder is (created_beside_raw).

    Returns entries and failures (the outcome of each read that failed) by file_key, and deferred: the files
    the deadline left unread, which the next check reads.
    """
    from .repository_reanalysis import _summarize_raw_metadata

    entries: dict[str, dict[str, Any]] = {}
    failures: dict[str, str] = {}
    deferred: list[str] = []
    cache_keys: dict[str, tuple[Any, ...]] = {}
    claimed: list[Path] = []
    waiting: list[tuple[Path, threading.Event]] = []

    def take(key: str, cached: dict[str, Any]) -> None:
        if cached.get("entry") is not None:
            entries[key] = dict(cached["entry"])
        else:
            failures[key] = str(cached.get("failure") or "failed")

    for path in paths:
        key = file_key(path)
        signature = input_signature(path)
        if signature is None:
            failures[key] = "missing"
            continue
        cache_keys[key] = (*extractor_key, key, json.dumps(signature, sort_keys=True))
        with _HEADER_CACHE_LOCK:
            cached = _HEADER_CACHE.get(cache_keys[key])
            if cached is None:
                running = _HEADER_IN_FLIGHT.get(key)
                if running is None:
                    _HEADER_IN_FLIGHT[key] = threading.Event()
                    claimed.append(path)
                else:
                    waiting.append((path, running))
        if cached is not None:
            take(key, cached)
    try:
        if claimed and time.monotonic() < deadline:
            beside = {file_key(path.parent): path.parent for path in claimed if not path.is_dir()}
            before = {key: _folder_names(folder) for key, folder in beside.items()}
            with _HEADER_CACHE_LOCK:
                earlier = {file_key(path): list(_HEADER_CREATED.get(file_key(path), [])) for path in claimed if path.is_dir()}
            work = Path(tempfile.mkdtemp(prefix="msdial-lab-headers-"))
            try:
                execution = run_extractor(
                    extractor, claimed, work, created_before=earlier, timeout_policy=HEADER_TIMEOUT_POLICY,
                    timeout_ceiling_seconds=HEADER_INPUT_TIMEOUT_CEILING_SECONDS, deadline=deadline,
                )
            finally:
                shutil.rmtree(work, ignore_errors=True)
            for key, folder in beside.items():
                after = _folder_names(folder)
                if before[key] is not None and after is not None:
                    _record_created(key, (str(folder / name) for name in sorted(after - before[key])))
            summary = _summarize_raw_metadata(list(execution.get("records") or []))
            by_file = {file_key(item["file"]): item for item in summary.get("per_file", []) if item.get("file")}
            for path in claimed:
                key = file_key(path)
                outcome = (execution.get("outcomes") or {}).get(key) or {}
                _record_created(key, outcome.get("reader_created_files") or [])
                entry = by_file.get(key)
                if outcome.get("outcome") in READ_OUTCOMES and entry is not None:
                    value: dict[str, Any] = {"entry": {name: entry.get(name) for name in HEADER_FIELDS}, "failure": ""}
                elif outcome.get("deadline"):
                    deferred.append(key)
                    continue
                else:
                    value = {"entry": None, "failure": str(outcome.get("outcome") or "no_record")}
                keys = {cache_keys[key]}
                if isinstance(outcome.get("input_signature"), dict):
                    # As the read left it: a reader that wrote into a folder changed its signature.
                    keys.add((*extractor_key, key, json.dumps(outcome["input_signature"], sort_keys=True)))
                with _HEADER_CACHE_LOCK:
                    for cache_key in keys:
                        _HEADER_CACHE[cache_key] = dict(value)
                take(key, value)
        else:
            deferred.extend(file_key(path) for path in claimed)
    finally:
        with _HEADER_CACHE_LOCK:
            for path in claimed:
                event = _HEADER_IN_FLIGHT.pop(file_key(path), None)
                if event is not None:
                    event.set()
    for path, running in waiting:
        key = file_key(path)
        running.wait(max(0.0, deadline - time.monotonic()))
        with _HEADER_CACHE_LOCK:
            cached = _HEADER_CACHE.get(cache_keys[key])
        if cached is not None:
            take(key, cached)
        else:
            deferred.append(key)
    return {"entries": entries, "failures": failures, "deferred": deferred}


def _parse_start_time(value: Any) -> dt.datetime | None:
    from .repository_reanalysis import _parse_acquisition_time

    return _parse_acquisition_time(value)


def _header_order(names: list[str], times: list[str | None]) -> dict[str, Any]:
    """The files ranked by the acquisition start time their headers record, or why not (as the campaign ranks them).

    Every file needs a readable time, and the times must order something; files of one time keep their
    listing order and are named.
    """
    missing = [name for name, value in zip(names, times) if value is None]
    if missing:
        return {"derived_from": None, "reason": "the header of " + ", ".join(missing[:5])
                + (f" and {len(missing) - 5} more" if len(missing) > 5 else "") + " was not read"}
    parsed = [_parse_start_time(value) for value in times]
    silent = [name for name, value, read in zip(names, times, parsed) if not str(value or "").strip() or read is None]
    if silent:
        return {"derived_from": None, "reason": "no readable acquisition start time was recorded for "
                + ", ".join(silent[:5]) + (f" and {len(silent) - 5} more" if len(silent) > 5 else "")}
    if len({value.tzinfo is None for value in parsed}) > 1:
        return {"derived_from": None, "reason": "some start times carry a timezone and some do not"}
    if len(names) > 1 and len(set(parsed)) == 1:
        return {"derived_from": None, "reason": "every header records the same start time"}
    ordered = sorted(range(len(names)), key=lambda index: (parsed[index], index))
    ranks = {names[index].casefold(): rank for rank, index in enumerate(ordered, start=1)}
    tied = sorted(name for name, value in zip(names, parsed) if parsed.count(value) > 1)
    return {
        "derived_from": HEADER_ORDER_SOURCE,
        "reason": "ranked by the acquisition start time each file's raw header records"
                  + (f"; files of one time keep their listing order ({', '.join(tied)})" if tied else ""),
        "agrees_with_listing": ordered == list(range(len(names))),
        "tied": tied,
        "ranks": ranks,
    }


def _count(values: Iterable[Any]) -> dict[str, int]:
    counted: dict[str, int] = {}
    for value in values:
        counted[str(value)] = counted.get(str(value), 0) + 1
    return dict(sorted(counted.items()))


def _names_text(names: list[str], limit: int = 5) -> str:
    return ", ".join(names[:limit]) + (f" and {len(names) - limit} more" if len(names) > limit else "")


def _energies_text(entry: dict[str, Any]) -> str:
    energies = entry.get("ms2_collision_energies")
    if isinstance(energies, list) and energies:
        return "MS2 collision energies " + ", ".join(f"{float(value):g}" for value in energies) + " eV"
    return "no MS2 collision energy recorded"


def read_raw_headers(inventory: dict[str, Any], polarities: list[str]) -> dict[str, Any]:
    """What each polarity's raw headers say, read with the configured extractor as the campaign reads them.

    For each file read: the method, the MS2 isolation targets and collision energies, the acquisition type
    the header gives the Console (raw_metadata_preflight.header_console_acquisition_type: DIA with two or
    more isolation targets is SWATH, with none AIF, with one unresolved), the polarity, the instrument and
    the acquisition start time. Up to HEADER_READ_ALL_LIMIT files of a polarity are all read; past it the
    first and the last, and the files between them are asked about, with the type those two give as a
    suggestion only. Every read of one check stops at HEADER_READ_BUDGET_SECONDS; a file left unread is
    asked about too. Only a DDA or SWATH verdict of a file's own header settles its type: an AIF verdict is
    asked (a SWATH file whose sampled headers show no isolation reads as AIF, and the campaign runs
    single-energy AIF as SWATH). A Bruker BAF folder is never read, because its reader writes into it; what
    any other reader leaves in or beside the raw data is detected afterwards, not prevented, and said.

    Returns the public report ("polarities", "extractor", "warnings", "blockers"), and under "_proposed"
    each file's (acquisition type or None, basis) by casefolded file name, under "_suggested" the type an
    unsettled file's header or the two end files give, under "_entries" each file's header summary, under
    "_names" the file names, and under "_orders" the header ranks where the times order the files.
    """
    extractor = _configured_extractor()
    identity = _extractor_identity(extractor["path"]) if extractor.get("path") else {}
    report: dict[str, Any] = {
        "extractor": (
            {"path": extractor["path"], "source": extractor["source"], **identity} if extractor.get("path") else None
        ),
        "polarities": {}, "warnings": [], "blockers": [],
        "_proposed": {}, "_suggested": {}, "_entries": {}, "_names": {}, "_orders": {},
    }
    plans: dict[str, dict[str, Any]] = {}
    for polarity in polarities:
        files = list((inventory.get(polarity) or {}).get("files") or [])
        paths = [Path(str(item.get("file_path", ""))) for item in files]
        partial = len(files) > HEADER_READ_ALL_LIMIT
        chosen = sorted({0, len(files) - 1}) if partial else list(range(len(files)))
        skipped = [index for index in chosen if input_format(paths[index]) in WRITING_HEADER_FORMATS]
        plans[polarity] = {
            "names": [str(item.get("file_name", "")) for item in files], "paths": paths, "partial": partial,
            "chosen": chosen, "skipped": skipped, "to_read": [index for index in chosen if index not in skipped],
        }
    # One read for every polarity, under one time limit: what it leaves unread is asked, and read next time.
    read: dict[str, Any] = {"entries": {}, "failures": {}, "deferred": []}
    wanted = [plan["paths"][index] for plan in plans.values() for index in plan["to_read"]]
    if extractor.get("path") and wanted:
        path = Path(extractor["path"])
        read = _read_headers(path, _extractor_key(path, identity), wanted,
                             time.monotonic() + HEADER_READ_BUDGET_SECONDS)
    deferred = set(read["deferred"])
    for polarity, plan in plans.items():
        names, paths, chosen, skipped = plan["names"], plan["paths"], plan["chosen"], plan["skipped"]
        partial = plan["partial"]
        entries = {index: read["entries"].get(file_key(paths[index])) for index in chosen}
        proposed: dict[str, tuple[str | None, str]] = {}
        suggested: dict[str, str] = {}
        per_file: list[dict[str, Any]] = []
        unreadable: list[str] = []
        late: list[str] = []
        unresolved: list[str] = []
        aif: list[str] = []
        for index in chosen:
            entry = entries[index]
            name = names[index]
            if not extractor.get("path"):
                continue
            if index in skipped:
                per_file.append({"file": name, "read": "not_read_reader_writes_into_folder"})
                continue
            if entry is None:
                if file_key(paths[index]) in deferred:
                    late.append(name)
                    per_file.append({"file": name, "read": "not_read_time_limit"})
                else:
                    unreadable.append(name)
                    per_file.append({"file": name, "read": read["failures"].get(file_key(paths[index]), "failed")})
                continue
            console = entry.get("header_console_acquisition_type")
            if console == "AIF":
                proposed[name.casefold()] = (None, "raw_header_aif")
                suggested[name.casefold()] = console
                aif.append(f"{name} ({_energies_text(entry)})")
            elif console:
                proposed[name.casefold()] = (console, RAW_HEADER)
            else:
                proposed[name.casefold()] = (None, "unresolved")
                unresolved.append(
                    f"{name} ({entry.get('acquisition_mode') or 'no method'}"
                    + (f", {entry.get('isolation_window_count')} isolation target"
                       if entry.get("isolation_window_count") == 1 else "") + ")"
                )
            per_file.append({
                "file": name, "read": "ok", "method": entry.get("acquisition_mode") or None,
                "method_source": entry.get("method_source") or None,
                "isolation_targets": entry.get("isolation_window_count"),
                "acquisition_type": console, "basis": entry.get("header_console_acquisition_basis") or None,
                "ms2_collision_energies": entry.get("ms2_collision_energies"),
                "ms2_collision_energies_unresolved": entry.get("ms2_collision_energies_unresolved") or None,
                "polarity": entry.get("polarity") or None,
                "acquisition_start_time": entry.get("acquisition_start_time") or None,
            })
        end_types = {(proposed.get(names[index].casefold()) or (None, ""))[0] for index in chosen}
        # The files between the first and the last are never read: the type both ends give is a suggestion.
        ends_agree = partial and len(end_types) == 1 and None not in end_types
        between = 0
        for index, name in enumerate(names):
            if name.casefold() in proposed:
                continue
            if partial and index not in chosen and extractor.get("path"):
                between += 1
                proposed[name.casefold()] = (None, "first_and_last_files")
                if ends_agree:
                    suggested[name.casefold()] = next(iter(end_types))
            else:
                proposed[name.casefold()] = (None, "unread")
        if extractor.get("error"):
            why = f"The raw-metadata extractor could not be chosen ({extractor['error']}), so the headers were not read."
        elif not extractor.get("path"):
            why = ("No raw-metadata extractor is configured (msdial_set_raw_metadata_extractor_path), so the headers "
                   "were not read.")
        else:
            parts = []
            if skipped:
                parts.append(f"{', '.join(names[index] for index in skipped)} were not read: the extractor's reader "
                             "writes analysis.sqlite into such a .d folder, and the check writes nothing beside the raw data")
            if late:
                parts.append(f"the headers of {_names_text(late)} were not read within the check's "
                             f"{HEADER_READ_BUDGET_SECONDS:g} s; checking again reads them (the headers read are kept)")
            if unreadable:
                parts.append(f"the headers of {_names_text(unreadable)} could not be read")
            if unresolved:
                parts.append(f"the headers of {_names_text(unresolved)} give no type the Console reads (DIA with one "
                             "isolation target is one wide window or one targeted precursor)")
            if aif:
                parts.append(
                    f"the headers of {_names_text(aif)} give AIF (DIA without isolation), which is asked: a SWATH file "
                    "whose sampled headers show no isolation reads as AIF too, the repository campaign runs AIF of one "
                    "MS2 collision energy as SWATH (the Console deconvolutes it alike), and AIF of more than one energy "
                    "needs a Console with MsdialWorkbench#825, which stops on an AIF file that records none"
                )
            if partial and ends_agree:
                parts.append(f"only the first and the last of {len(names)} files were read (more "
                             f"than {HEADER_READ_ALL_LIMIT}), and both give {next(iter(end_types))}: the {between} files "
                             "between them were not read, so that type is only a suggestion for them")
            elif partial:
                parts.append(f"only the first and the last of {len(names)} files were read (more "
                             f"than {HEADER_READ_ALL_LIMIT}), and they settle no type for the {between} files between them")
            why = "; ".join(parts)
            why = (why[:1].upper() + why[1:] + ".") if why else ""
        if not extractor.get("path"):
            order: dict[str, Any] = {"derived_from": None, "reason": "no raw-metadata extractor is configured"}
        elif partial:
            order = {"derived_from": None, "reason": f"only the first and the last of {len(names)} files were read"}
        elif not names:
            order = {"derived_from": None, "reason": "the folder holds no raw file"}
        else:
            # None: the header was not read; "": it was, and records no time.
            times = [(entries[index].get("acquisition_start_time") or "") if entries.get(index) else None
                     for index in range(len(names))]
            order = _header_order(names, times)
        report["_orders"][polarity] = order.pop("ranks", None)
        report["_proposed"][polarity] = proposed
        report["_suggested"][polarity] = suggested
        report["_entries"][polarity] = {names[index].casefold(): entry for index, entry in entries.items() if entry}
        report["_names"][polarity] = {name.casefold(): name for name in names}
        read_entries = [entry for entry in entries.values() if entry]
        # A header is stronger evidence of a file's polarity than its name, which already refuses the folder.
        other = [names[index] for index in chosen if entries.get(index)
                 and entries[index].get("polarity") in POLARITIES and entries[index]["polarity"] != polarity]
        if other:
            report["blockers"].append(
                f"The headers of {_names_text(other)} in the {polarity.lower()} folder record the other polarity. "
                "Move such files to the folder of their polarity, or out of the batch, before the contract; if the "
                "folders are the wrong way round, give them the other way."
            )
        switching = [f"{names[index]} ({entries[index]['polarity']})" for index in chosen if entries.get(index)
                     and entries[index].get("polarity") in SWITCHING_POLARITIES]
        if switching:
            report["blockers"].append(
                f"The headers of {_names_text(switching)} in the {polarity.lower()} folder record both polarities. "
                "MS-DIAL runs one ion mode per analysis, and a polarity-switching file is outside what this agent "
                "runs: take such files out of the batch (the repository campaign excludes them too)."
            )
        created = created_beside_raw(paths)
        if created:
            report["warnings"].append(
                "Reading the headers left files in or beside the raw data (a reader wrote them; the check does not): "
                + _names_text(created) + "."
            )
        report["polarities"][polarity] = {
            "files": len(names),
            "coverage": ("none" if not extractor.get("path") else "first_and_last" if partial else "all"),
            "files_read": len(read_entries),
            "methods": _count(entry.get("acquisition_mode") or "none" for entry in read_entries),
            "isolation_targets": sorted({entry["isolation_window_count"] for entry in read_entries
                                         if isinstance(entry.get("isolation_window_count"), int)}),
            "ms2_collision_energies": sorted({float(value) for entry in read_entries
                                              for value in entry.get("ms2_collision_energies") or []}),
            "instruments": sorted({str(entry["instrument_model"]) for entry in read_entries
                                   if entry.get("instrument_model")}),
            "separations": sorted({str(entry["separation"]) for entry in read_entries if entry.get("separation")}),
            "unreadable": unreadable,
            "not_read_in_time": late,
            "reader_created_files": created,
            "unresolved_why": why,
            "per_file": per_file,
            "order": order,
        }
    return report


def _sample_table_proposal(
    inventory: dict[str, Any], polarities: list[str], pairs: Any = None, headers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One table for the polarities given: paired for two, the one polarity's files for one.

    Two polarities pair by stem (S1_Pos with S1_Neg) where they can; otherwise by the words that set each
    polarity's files apart (pair_by_shared_tokens), which the person confirms, or by the pairs they give.
    Each row's acquisition type and, where the raw headers order the files, its analytical order are then
    the headers' (_apply_headers).
    """
    headers = headers or {}
    if headers.get("orders"):
        # The headers set each row's order after the pairing; the order read from the names must not decide it.
        inventory = {
            polarity: {**item, "files": [{**entry, "analytical_order": None} for entry in item["files"]]}
            for polarity, item in inventory.items()
        }
    if len(polarities) == 2:
        if set(inventory) != set(POLARITIES):
            return {"rows": [], "problems": [], "matched": False, "blockers": []}
        rows = {polarity: [_sample_row(item) for item in inventory[polarity]["files"]] for polarity in POLARITIES}
        by_name: dict[str, Any] = {}
        if pairs not in (None, "", []):
            paired = _given_pairs(pairs, rows)
            how = "as the person gave them (sample_pairs)"
        else:
            by_name = _paired_sample_table_proposal(inventory)
            paired = pair_by_shared_tokens(rows[POSITIVE], rows[NEGATIVE]) if by_name.get("blockers") else {}
            how = "by the words that set each polarity's files apart; confirm every pair"
        if by_name and not by_name.get("blockers"):
            proposal = {**by_name, "pairing": "by file name (the same name but for _Pos/_Neg)", "pairing_warnings": []}
        else:
            for row in paired["rows"]:
                row["included"] = True
            proposal = {
                "rows": paired["rows"], "problems": paired["problems"], "matched": not paired["problems"],
                "pairing": how, "pairing_warnings": list(paired.get("warnings", [])),
                "class_proposal": paired.get("class_proposal") or inventory[POSITIVE].get("class_proposal", {}),
                "order_proposal": inventory[POSITIVE].get("sample_table_proposal", {}).get("analytical_order", {}),
                "confirmation_required": True,
                "blockers": [f"The two polarities do not pair: {problem}" for problem in paired["problems"]]
                            + (["Give the pairs as sample_pairs: [{sample, positive_file, negative_file}, ...]."]
                               if paired["problems"] else []),
            }
        return _apply_headers(proposal, headers)
    if not polarities or polarities[0] not in inventory:
        return {"rows": [], "problems": [], "matched": False, "blockers": []}
    polarity = polarities[0]
    key = "positive_file" if polarity == POSITIVE else "negative_file"
    other = "negative_file" if polarity == POSITIVE else "positive_file"
    rows = []
    for item in sorted((_sample_row(entry) for entry in inventory[polarity]["files"]),
                       key=lambda row: (row.get("analytical_order") or 0, row["file_name"].casefold())):
        rows.append({
            "stem": strip_polarity(item["file_name"]), key: item["file_name"], other: None,
            **{name: item.get(name) for name in ("file_type", "class_id", "analytical_order", "batch_order", "factor",
                                                  "acquisition_type")},
            "included": True,
        })
    stems = [row["stem"].casefold() for row in rows]
    problems = [f"{row['stem']} is the stem of more than one file." for row in rows if stems.count(row["stem"].casefold()) > 1]
    return _apply_headers({
        "rows": rows, "problems": problems, "matched": not problems,
        "class_proposal": inventory[polarity].get("class_proposal", {}),
        "order_proposal": inventory[polarity].get("sample_table_proposal", {}).get("analytical_order", {}),
        "confirmation_required": True,
        "blockers": [f"The sample table cannot be proposed: {problem}" for problem in problems],
    }, headers)


def _apply_headers(proposal: dict[str, Any], headers: dict[str, Any]) -> dict[str, Any]:
    """Each row's acquisition type from its files' headers (or the person's answer), and its order from them.

    headers: {"types": {polarity: {file name casefolded: type or None}}, "bases": the same keys' bases,
    "orders": {polarity: {name: rank}}, "order": the order proposal to show}. A row carries a type only when
    every file of it has one and they agree: one file still asked about leaves the row without one, and so
    does a pair whose two files' types differ, which acquisition_type_conflicts names with both. The
    contract refuses a row without a type until the person states it in sample_table, so a table sent back
    as proposed can never carry a type nobody gave. A paired row carries one order for both polarities: the
    positive file's, else the negative's; orders that differ are a warning.
    """
    types = headers.get("types") or {}
    bases = headers.get("bases") or {}
    orders = headers.get("orders") or {}
    if not types and not orders:
        return proposal
    differing_types: list[str] = []
    differing_orders: list[str] = []
    conflicts: dict[str, dict[str, str]] = {}
    for row in proposal.get("rows", []):
        files = {POSITIVE: row.get("positive_file"), NEGATIVE: row.get("negative_file")}
        if types:
            found = {polarity: types.get(polarity, {}).get(str(name).casefold()) for polarity, name in files.items() if name}
            how = {polarity: bases.get(polarity, {}).get(str(name).casefold(), "unread")
                   for polarity, name in files.items() if name}
            known = [value for value in found.values() if value]
            if len(set(known)) > 1:
                conflicts[row["stem"]] = {POSITIVE: found[POSITIVE], NEGATIVE: found[NEGATIVE]}
                differing_types.append(f"{row['stem']} ({found[POSITIVE]} positive, {found[NEGATIVE]} negative)")
                row["acquisition_type"] = None
                row["acquisition_type_basis"] = "polarities_disagree"
            else:
                row["acquisition_type"] = known[0] if known and len(known) == len(found) else None
                row["acquisition_type_basis"] = (
                    next(iter(how.values())) if len(set(how.values())) == 1
                    else "; ".join(f"{polarity.lower()} {basis}" for polarity, basis in how.items())
                )
        if orders:
            ranks = {polarity: orders.get(polarity, {}).get(str(name).casefold()) for polarity, name in files.items() if name}
            row["analytical_order"] = ranks.get(POSITIVE) or ranks.get(NEGATIVE)
            if ranks.get(POSITIVE) and ranks.get(NEGATIVE) and ranks[POSITIVE] != ranks[NEGATIVE]:
                differing_orders.append(f"{row['stem']} ({ranks[POSITIVE]} positive, {ranks[NEGATIVE]} negative)")
    if orders:
        proposal["rows"].sort(key=lambda row: (row.get("analytical_order") or 0, row["stem"].casefold()))
        proposal["order_proposal"] = headers.get("order") or proposal.get("order_proposal", {})
    elif headers.get("order"):
        proposal["order_proposal"] = {**proposal.get("order_proposal", {}), "raw_headers": headers["order"]}
    proposal["acquisition_type_conflicts"] = conflicts
    # The rows the person still states a type for in sample_table before the contract (the files' questions
    # answered): ready_for_contract does not cover them, since a check never reads the sample table.
    proposal["acquisition_type_required"] = [
        row["stem"] for row in proposal.get("rows", []) if row_included(row) and not row.get("acquisition_type")
    ] if types else []
    if differing_types:
        proposal["pairing_warnings"] = [*proposal.get("pairing_warnings", []), (
            "The positive and negative files of " + _names_text(differing_types)
            + " are of different acquisition types (acquisition_type_conflicts), and a paired row carries one type "
            "for both, so such a row is proposed with none. Ask the person which it is and give the row's "
            "acquisition_type in sample_table; the contract refuses the row until then. If the polarities really "
            "were acquired as different types, check each polarity on its own (one contract each)."
        )]
    if differing_orders:
        proposal["pairing_warnings"] = [*proposal.get("pairing_warnings", []), (
            "The negative files were acquired in another order than the positive ones ("
            + ", ".join(differing_orders[:5]) + (f" and {len(differing_orders) - 5} more" if len(differing_orders) > 5 else "")
            + "); a paired row carries one analytical order for both polarities, so the negative run is ordered as "
            "the positive one was acquired. Correct analytical_order in sample_table to change it for both."
        )]
    return proposal


def _given_pairs(pairs: Any, rows: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """The pairs the person gave: [{sample, positive_file, negative_file}], every file of each folder once."""
    problems: list[str] = []
    if not isinstance(pairs, list) or not all(isinstance(item, dict) for item in pairs):
        return {"rows": [], "problems": ["sample_pairs is a list of {sample, positive_file, negative_file}."]}
    by_name = {polarity: {row["file_name"].casefold(): row for row in items} for polarity, items in rows.items()}
    used: dict[str, set[str]] = {POSITIVE: set(), NEGATIVE: set()}
    result: list[dict[str, Any]] = []
    for number, item in enumerate(pairs, start=1):
        sample = str(item.get("sample") or "").strip()
        files = {POSITIVE: str(item.get("positive_file") or "").strip(), NEGATIVE: str(item.get("negative_file") or "").strip()}
        if not sample:
            problems.append(f"sample_pairs[{number}] names no sample.")
            continue
        found = {}
        for polarity, name in files.items():
            row = by_name[polarity].get(name.casefold())
            if row is None:
                problems.append(f"sample_pairs[{number}] names {name or 'no file'}, which is not in the {polarity.lower()} folder.")
            elif name.casefold() in used[polarity]:
                problems.append(f"sample_pairs names {name} twice.")
            else:
                used[polarity].add(name.casefold())
                found[polarity] = row
        if len(found) == 2:
            row = {"stem": sample, "positive_file": found[POSITIVE]["file_name"],
                   "negative_file": found[NEGATIVE]["file_name"], "paired_by": "the person"}
            row.update({key: found[POSITIVE].get(key) for key in PAIRED_FIELDS})
            result.append(row)
    for polarity, items in rows.items():
        for row in items:
            if row["file_name"].casefold() not in used[polarity]:
                problems.append(f"sample_pairs has no pair for {row['file_name']}.")
    names = [row["stem"] for row in result]
    problems.extend(f"sample_pairs names the sample {name} twice." for name in sorted({n for n in names if names.count(n) > 1}))
    result.sort(key=lambda row: (row.get("analytical_order") or 0, row["stem"].casefold()))
    return {"rows": result, "problems": problems}


def _input_identity(
    args: dict[str, Any], inventory: dict[str, Any], identities: dict[str, Any], annotation: dict[str, Any],
    libraries: dict[str, Any], metadata: dict[str, Any], normalizing: dict[str, Any],
) -> dict[str, Any]:
    return {
        "raw": {
            polarity: sorted([str(item.get("file_name")), _size(item.get("file_path"))] for item in item_inventory["files"])
            for polarity, item_inventory in sorted(inventory.items())
        },
        "annotation": {
            "preset": annotation.get("preset"),
            "libraries": [
                {key: entry.get(key) for key in ("kind", "thresholds", "retention_time", "rt_tolerance", "label",
                                                 "priority", "annotator_id", "library_kind")}
                | {"files": {polarity: identities.get(_path_key(path), {}).get("sha256") or _path_key(path)
                             for polarity, path in sorted(entry["paths"].items())},
                   "paths": {polarity: _path_key(path) for polarity, path in sorted(entry["paths"].items())}}
                for entry in annotation.get("entries", [])
            ],
        },
        "internal_standard_texts": {
            polarity: {"path": item.get("path"), "sha256": item.get("sha256")} for polarity, item in sorted(libraries.items())
        },
        "normalizing_standards": {polarity: item["name"] for polarity, item in sorted(normalizing.items())},
        "sample_pairs": json.dumps(args.get("sample_pairs"), sort_keys=True, default=str),
        "merge_choices": {key: str(args.get(key, "")).strip().casefold() for key in ("duplicate_rule", "inchikey_match")},
        "sample_metadata": {"path": metadata.get("path"), "sha256": metadata.get("sha256")},
        "answers": {
            **{key: str(args.get(key, "")).strip() for key in ("solvent_type", "analysis_purpose")},
            "raw_location": str(args.get("raw_location", "")).strip().casefold(),
            "number_of_threads": repr(_positive_number(args.get("number_of_threads"))),
            "internal_standard_rt_tolerance": repr(_positive_number(
                args.get("internal_standard_rt_tolerance", DEFAULT_INTERNAL_STANDARD_RT_TOLERANCE)
            )),
            "alignment_rt_tolerance": repr(_positive_number(args.get("alignment_rt_tolerance"))),
            **{key: str(args.get(key, "")).strip().casefold() for key in (
                "acquisition_type_pos", "acquisition_type_neg", "acquisition_type_mixed_pos", "acquisition_type_mixed_neg",
            )},
            **{key: _path_key(args.get(key)) for key in ("analysis_root", "console_path", "pos_path", "neg_path")},
        },
    }


# ---------------------------------------------------------------------------------------------
# Step 2: the contract
# ---------------------------------------------------------------------------------------------


def lab_method_files() -> dict[str, dict[str, Any]]:
    """The laboratory LC-MS template and LBM query list, by path and sha256, as lipidomics fixes them."""
    return {
        key: {"path": str(path.resolve()), "name": path.name, "sha256": file_sha256(path)}
        for key, path in (("parameter_template", LAB_PARAMETER_TEMPLATE), ("lbm_queries", LAB_LBM_QUERIES))
    }


def template_alignment_rt_tolerance(template: str | Path = LAB_PARAMETER_TEMPLATE) -> float | None:
    """The alignment RT tolerance a parameter template states (the laboratory one unless named), which every
    run of a contract keeps unless the contract gives one."""
    try:
        lines = Path(template).read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return None
    for line in lines:
        name, separator, value = line.partition(":")
        if separator and name.strip().casefold() == "retention time tolerance for alignment":
            return parse_number(value.strip())
    return None


def write_contract(request: dict[str, Any]) -> dict[str, Any]:
    """Fix the checked inputs and the confirmed sample table in <analysis root>/inputs/.

    A question still open, a blocker, or any input that differs from what check_digest bound refuses
    the write. confirmed=false previews what would be written.
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
        return {**refusal, "error": "Pass the check_digest msdial_lcms_metabolomics_check_inputs returned for the inputs the person saw."}
    if given_digest != check["check_digest"]:
        return {**refusal, "error": (
            "An input changed after it was checked (a file's content, a raw file, or an answer); check the inputs "
            "again and show the person the result before writing the contract."
        )}
    if request.get("sample_table_confirmed") is not True:
        return {**refusal, "error": (
            "The sample table is the person's to confirm: show paired_sample_table, then pass "
            "sample_table_confirmed=true, with sample_table holding their corrections if any."
        )}
    table = _confirmed_sample_table(check["paired_sample_table"], request.get("sample_table"), parsed["inventory"])
    if table["problems"]:
        return {**refusal, "error": "The sample table cannot be used: " + " ".join(table["problems"])}
    # Every included row runs as the type it carries, and a row proposed without one (a pair whose files'
    # types differ) carries only the type the person states: an empty one would reach the Console as DDA.
    conflicts = check["paired_sample_table"].get("acquisition_type_conflicts", {})
    untyped = [row["stem"] for row in table["rows"] if row_included(row) and not str(row.get("acquisition_type") or "").strip()]
    if untyped:
        stated = [stem for stem in untyped if stem in conflicts]
        return {**refusal, "error": " ".join(part for part in (
            ("The two files of " + ", ".join(
                f"{stem} ({conflicts[stem][POSITIVE]} positive, {conflicts[stem][NEGATIVE]} negative)" for stem in stated
            ) + " are of different acquisition types, and a paired row carries one: give each such row's "
             "acquisition_type in sample_table, as the person states it.") if stated else "",
            ("The sample table gives no acquisition type for " + ", ".join(stem for stem in untyped if stem not in conflicts)
             + "; give it in sample_table, as the person states it.") if len(stated) < len(untyped) else "",
        ) if part)}
    unsafe = analysis_csv_problems(table["rows"], parsed["inventory"])
    if unsafe:
        return {**refusal, "error": "The sample table cannot reach the Console as it stands: " + " ".join(unsafe)}
    proposed_types = {row["stem"].casefold(): row.get("acquisition_type") for row in check["paired_sample_table"].get("rows", [])}
    for row in table["rows"]:
        if row.get("acquisition_type") not in (None, ""):
            row["acquisition_type"] = console_acquisition_type(row["acquisition_type"])
        if row.get("acquisition_type") != proposed_types.get(row["stem"].casefold()):
            row["acquisition_type_basis"] = "sample_table"
    # A type stated in sample_table meets the same collision-energy guards as one the check proposed.
    stated_types = {
        polarity: {item["file_name"].casefold(): item.get("acquisition_type")
                   for item in polarity_sample_table(table["rows"], polarity) if row_included(item)}
        for polarity in parsed["polarities"]
    }
    aif = aif_problems(stated_types, parsed["header_entries"], parsed["header_names"],
                       parsed["console"].get("capabilities"))
    if aif["blockers"]:
        return {**refusal, "error": "The sample table cannot run as it stands: " + " ".join(aif["blockers"])}

    polarities: list[str] = parsed["polarities"]
    root: Path = parsed["analysis_root"]
    inputs = root / INPUTS_FOLDER
    contract_path = inputs / CONTRACT_NAME
    derived_paths = {polarity: inputs / f"is_{FOLDERS[polarity]}.txt" for polarity in polarities}
    method_files = lab_method_files()
    existing = _existing_contract(contract_path) if contract_path.exists() else None
    if existing is not None and request.get("replace_existing") is not True:
        return {**refusal, "existing_contract": existing, "error": (
            f"{contract_path} already exists. Show the person existing_contract (what was done under it, and what "
            "replacing it does), then preview again with replace_existing=true if they want it replaced."
        )}
    annotation = parsed["annotation"]
    preview = {
        "contract_path": str(contract_path),
        "existing_contract": existing,
        "would_write": [str(contract_path), *(str(path) for path in derived_paths.values()), str(inputs / RECORD_NAME)],
        "polarities": polarities,
        "raw_folders": {polarity: str(parsed["folders"][polarity]) for polarity in polarities},
        "raw_location": check["raw_location"],
        "output_roots": {polarity: str(root / FOLDERS[polarity]) for polarity in polarities},
        "sample_table": table["rows"],
        "raw_headers": check["raw_headers"],
        "salt": parsed["salt"]["salt"],
        "annotation": check["annotation"],
        "normalizing_standards": parsed["normalizing"],
        "alignment_rt_tolerance": parsed["alignment_rt_tolerance"],
        "method_files": {key: {"name": item["name"], "sha256": item["sha256"]} for key, item in method_files.items()},
        "check_digest": check["check_digest"],
    }
    if request.get("confirmed") is not True:
        return {"written": False, "confirmation_required": True, **preview,
                "message": "Show the person what would be written, then call again with confirmed=true."}

    inputs.mkdir(parents=True, exist_ok=True)
    replaced = ""
    if contract_path.exists():
        stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
        kept = contract_path.with_name(f"lab-lcms-metabolomics-contract.{stamp}.json")
        contract_path.replace(kept)
        replaced = str(kept)
    # A standard given without a class is written as one, which the canonical layout and the plan's check expect.
    for polarity in polarities:
        for record in parsed["libraries"][polarity]:
            record.ontology = record.ontology or "Internal standard"
    derived = {polarity: write_canonical_text_db(parsed["libraries"][polarity], derived_paths[polarity])
               for polarity in polarities}
    identities = parsed["identities"]
    args = parsed["args"]
    library_reports = check["internal_standard_libraries"]
    contract = {
        "schema": CONTRACT_SCHEMA,
        # It names the libraries and the raw data by their locations: never part of a bundle or a shared report.
        "sharing": "local_only",
        "created_at": dt.datetime.now().astimezone().isoformat(),
        "interactive_version": __version__,
        "analysis_purpose": str(args.get("analysis_purpose")).strip(),
        "analysis_root": str(root),
        "polarities": polarities,
        "raw_folders": {polarity: str(parsed["folders"][polarity]) for polarity in polarities},
        "raw_location": check["raw_location"],
        "output_roots": {polarity: str(root / FOLDERS[polarity]) for polarity in polarities},
        "quant_folder": str(root / QUANT_FOLDER),
        "console_path": parsed["console"]["path"],
        "console": console_identity(parsed["console"]["path"]),
        "annotation": {
            "preset": annotation["preset"],
            "libraries": [
                {key: entry.get(key) for key in ("id", "slot", "kind", "thresholds", "retention_time", "rt_tolerance",
                                                 "label", "priority", "annotator_id", "library_kind")}
                | {"files": {polarity: identities[_path_key(path)] for polarity, path in entry["paths"].items()}}
                for entry in annotation["entries"]
            ],
        },
        "inputs": {
            **{key: {"path": item["path"], "sha256": item["sha256"]} for key, item in method_files.items()},
            "internal_standard_texts": {
                polarity: {"path": library_reports[polarity]["path"], "sha256": library_reports[polarity]["sha256"]}
                for polarity in polarities
            },
            "sample_metadata": (
                {"path": parsed["metadata"]["path"], "sha256": parsed["metadata"]["sha256"]}
                if parsed["metadata"].get("sha256") else None
            ),
        },
        "derived": {
            "internal_standard_libraries": {
                polarity: {"path": item["path"], "sha256": item["sha256"], "records": item["records"]}
                for polarity, item in derived.items()
            },
        },
        "salt": {key: parsed["salt"].get(key) for key in ("salt", "answer", "matched")},
        "internal_standard_rt_tolerance": parsed["is_rt_tolerance"],
        # None: the parameter template's value, which every run then keeps.
        "alignment_rt_tolerance": parsed["alignment_rt_tolerance"],
        # What each file's acquisition type and the analytical order were proposed from; the rows carry the types.
        "raw_headers": {**check["raw_headers"], "answers": parsed["acquisition_answers"]},
        "normalizing_standards": parsed["normalizing"],
        "sample_table": {
            "rows": table["rows"],
            "confirmed": True,
            "confirmed_by": str(request.get("confirmed_by") or "").strip(),
            "confirmed_at": dt.datetime.now().astimezone().isoformat(),
            "corrected": table["corrected"],
        },
        "method": lab_method_answers(),
        "merge": {"defaults": {"reference_matched_only": True, "preferred_adducts_only": False,
                               "duplicate_rule": parsed["merge_choices"]["duplicate_rule"],
                               "inchikey_match": parsed["merge_choices"]["inchikey_match"]},
                  "preferred_adducts": preferred_adducts(parsed["salt"]["salt"])},
        "pairing": check["paired_sample_table"].get("pairing"),
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
            f"answers={{'{CONTRACT_KEY}': <contract_path>, 'ion_mode': ...}}); the diagnostic and each production "
            "run are confirmed separately."
        ),
    }


RECORDED_STEPS = ("peak_height_diagnostic", "production_run_started", "normalized", "merged")


def _existing_contract(contract_path: Path) -> dict[str, Any]:
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
            "keeps it under a dated name and rewrites the internal-standard libraries; every diagnostic, run and "
            "normalize made under it can then no longer be resolved, normalized or merged, and is made again"
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
        raise LabMetabolomicsError(f"The laboratory metabolomics contract {path} cannot be read: {error}") from None
    if not isinstance(contract, dict) or contract.get("schema") != CONTRACT_SCHEMA:
        raise LabMetabolomicsError(f"{path} is not a laboratory metabolomics contract ({CONTRACT_SCHEMA}).")
    contract["_path"] = str(path)
    contract["_sha256"] = file_sha256(path)
    if verify:
        drift = contract_drift(contract)
        if drift:
            raise LabMetabolomicsError(
                "The contract's inputs changed after it was written, so nothing planned from it can be trusted: "
                + "; ".join(drift) + ". Check the inputs again and write a new contract."
            )
    return contract


def contract_drift(contract: dict[str, Any]) -> list[str]:
    drift: list[str] = []
    checked: list[tuple[str, Any]] = []
    for entry in contract.get("annotation", {}).get("libraries", []):
        checked += [(f"{polarity} library '{entry.get('label')}'", item) for polarity, item in entry.get("files", {}).items()]
    inputs = contract.get("inputs", {})
    checked += [(f"{polarity} internal-standard text", item) for polarity, item in inputs.get("internal_standard_texts", {}).items()]
    checked += [("sample metadata", inputs.get("sample_metadata"))]
    checked += [(f"{polarity} canonical internal-standard library", item)
                for polarity, item in contract.get("derived", {}).get("internal_standard_libraries", {}).items()]
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


CONTRACT_PLAN_KEYS = frozenset({CONTRACT_KEY, "ion_mode", "minimum_peak_height", "workflow_overrides"})


def contract_plan_answers(contract_path: str, supplied: dict[str, Any], input_path: str) -> dict[str, Any]:
    """The guided answers a contract gives one polarity, and the blockers in using it (as for lipidomics)."""
    outcome: dict[str, Any] = {
        "answers": {}, "fixed": {}, "blockers": [], "contract_path": str(contract_path), "contract_sha256": "",
        "polarity": "", "deviations": [], "fixed_keys": [],
    }
    try:
        contract = load_contract(contract_path)
    except LabMetabolomicsError as error:
        outcome["blockers"].append(str(error))
        return outcome
    outcome["contract_path"] = contract["_path"]
    outcome["contract_sha256"] = contract["_sha256"]
    polarity = str(supplied.get("ion_mode") or "")
    if polarity not in contract.get("polarities", []):
        outcome["blockers"].append(
            f"Name the ion mode this plan is for: {' or '.join(contract.get('polarities', []))}, the polarities the "
            "contract covers."
        )
        return outcome
    outcome["polarity"] = polarity
    if "raw_headers" not in contract:
        # Written before the acquisition types were read from the raw headers: its rows took DDA by default.
        outcome["blockers"].append(
            f"The contract {contract['_path']} was written before each file's acquisition type was read from its raw "
            "header, so its rows may carry DDA that nobody stated (SWATH or AIF data run as DDA get almost no MS/MS). "
            "Check the inputs again and write the contract again."
        )
        return outcome
    raw = contract["raw_folders"][polarity]
    if not input_path or not _same_path(input_path, raw):
        outcome["blockers"].append(f"input_path must be the contract's {polarity.lower()} raw-data folder, {raw}.")
    untyped = [row.get("stem") for row in contract["sample_table"]["rows"]
               if row.get(FILE_KEYS[polarity]) and row_included(row) and console_acquisition_type(row.get("acquisition_type")) is None]
    if untyped:
        outcome["blockers"].append(
            f"The contract gives no acquisition type the Console reads for {_names_text([str(stem) for stem in untyped])}; "
            "write a new contract."
        )
    root = Path(contract["analysis_root"])
    outcome["blockers"].extend(raw_folder_overlaps(root, _analysis_subfolders(root), contract["raw_folders"]))
    # A lipidomics contract written into this root afterwards would share every file name with this one.
    outcome["blockers"].extend(other_contract_problems(root))
    method_files = {key: contract["inputs"].get(key) or {} for key in ("parameter_template", "lbm_queries")}
    for key, item in method_files.items():
        label = key.replace("_", " ")
        if not item.get("sha256"):
            outcome["blockers"].append(f"The contract names no {label}. Write a new contract.")
        elif not Path(str(item.get("path"))).is_file():
            outcome["blockers"].append(f"The contract's {label} {item.get('path')} is gone.")
        elif file_sha256(item["path"]) != item["sha256"]:
            outcome["blockers"].append(
                f"The contract's {label} {Path(str(item['path'])).name} has changed since the contract was written, so "
                "this polarity would run another method than the contract states. Write a new contract."
            )
    entries = [
        {**entry, "paths": {name: item["path"] for name, item in entry.get("files", {}).items()}}
        for entry in contract["annotation"]["libraries"]
    ]
    use_rt, tolerance = lbm_retention_time(entries)
    fixed = {
        "project_type": "lcms",
        "target_omics": "Metabolomics",
        "library_strategy": "annotators",
        "libraries": annotator_settings(entries, polarity),
        # The libraries' own RT settings are fixed per row; this answer reaches the LBM2 only.
        "use_retention_time_for_annotation": use_rt,
        **({"retention_time_tolerance": tolerance} if use_rt else {}),
        "solvent_type": contract["salt"]["salt"],
        "internal_standard_text_path": contract["derived"]["internal_standard_libraries"][polarity]["path"],
        "internal_standard_rt_tolerance": contract["internal_standard_rt_tolerance"],
        "sample_table": polarity_sample_table(contract["sample_table"]["rows"], polarity),
        # Each file's factor is the sample table's; a run-wide one would be overridden by it, yet reported.
        "dilution_factor": None,
        "class_assignment_confirmed": True,
        "analysis_purpose": contract["analysis_purpose"],
        "output_root": contract["output_roots"][polarity],
        "export_folder_path": contract["output_roots"][polarity],
        "console_path": contract["console_path"],
        "template_path": method_files["parameter_template"].get("path", ""),
        "queries_path": method_files["lbm_queries"].get("path", ""),
    }
    # The person's value, for both polarities; without one the template's, fixed alike, so that neither a plan's
    # answer nor a workset's changes one polarity (workflow_overrides is refused for the same key).
    alignment = contract.get("alignment_rt_tolerance")
    if alignment is None:
        alignment = template_alignment_rt_tolerance(method_files["parameter_template"].get("path") or LAB_PARAMETER_TEMPLATE)
    if alignment is not None:
        fixed["alignment_rt_tolerance"] = alignment
    given = {key: value for key, value in supplied.items() if value is not None and value != ""}
    for key, value in fixed.items():
        if key in given and not _same_answer(key, given[key], value):
            outcome["blockers"].append(
                f"{key} is fixed by the contract {contract['_path']} for every polarity; write a new contract to change it."
            )
    answers = dict(contract["method"]["answers"])
    if contract.get("number_of_threads"):
        answers["number_of_threads"] = contract["number_of_threads"]
    for key, value in answers.items():
        if key in given and not _same_answer(key, given[key], value):
            outcome["deviations"].append({"key": key, "contract": value, "given": given[key]})
    for key in sorted(set(given) - set(fixed) - set(answers) - CONTRACT_PLAN_KEYS):
        outcome["deviations"].append({"key": key, "contract": None, "given": given[key]})
    outcome.update({"answers": answers, "fixed": {**fixed, CONTRACT_KEY: contract["_path"]}, "fixed_keys": sorted(fixed)})
    return outcome


CONTRACT_FIXED_STATE_KEYS = frozenset({
    "solvent", "files", "sample_table_proposal", "output_root", "export_folder_path", "console_path", "ion_mode",
    "project_type", "target_omics", "template_path", "selected_lipids", "selected_adducts",
    "internal_standard_rt_tolerance", "alignment_rt_tolerance", CONTRACT_KEY, "analysis_purpose", "repository_run_manifest",
    "repository_metadata", "repository_metadata_source_path", "annotation_pipeline_profile", "library_provenance",
})
CONTRACT_FIXED_STATE_PREFIXES = ("lbm_", "msp_", "text_")


def contract_override_problems(overrides: dict[str, Any]) -> dict[str, list[Any]]:
    blockers: list[str] = []
    deviations: list[dict[str, Any]] = []
    for key, value in sorted((overrides or {}).items()):
        if key in CONTRACT_FIXED_STATE_KEYS or key.startswith(CONTRACT_FIXED_STATE_PREFIXES):
            blockers.append(
                f"workflow_overrides.{key} would replace what the laboratory metabolomics contract decides; write a "
                "new contract to change it."
            )
        else:
            deviations.append({"key": f"workflow_overrides.{key}", "contract": None, "given": value})
    return {"blockers": blockers, "deviations": deviations}


def _same_answer(key: str, left: Any, right: Any) -> bool:
    if key == "libraries":
        def canonical(value: Any) -> Any:
            rows = (value or {}).get("annotators") if isinstance(value, dict) else None
            if not isinstance(rows, list):
                return value
            return [{**row, "path": _path_key(row.get("path"))} if isinstance(row, dict) else row for row in rows]
        return json.dumps(canonical(left), sort_keys=True, default=str) == json.dumps(canonical(right), sort_keys=True, default=str)
    return _lipidomics_same_answer(key, left, right)


# ---------------------------------------------------------------------------------------------
# Step 3: the internal standards in each run
# ---------------------------------------------------------------------------------------------


def _outputs(contract: dict[str, Any], polarity: str, job: dict[str, Any] | None) -> dict[str, Any]:
    return run_outputs(contract, polarity, job, contract_key=CONTRACT_KEY, plan_block=PLAN_BLOCK,
                       require_library_copy=False)


def read_peak_ids(path: str | Path) -> dict[str, Any]:
    """The run's <alignment>.mdpeakid.tsv: each spot's peak ID per injection, -1 where MS-DIAL filled the gap."""
    target = Path(path)
    with target.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle, delimiter="\t")
        header = next(reader, [])
        files = [name.strip() for name in header[1:]]
        by_spot: dict[str, list[int]] = {}
        for row in reader:
            if not row or not row[0].strip():
                continue
            by_spot[row[0].strip()] = [int(value) if re.fullmatch(r"-?\d+", value.strip()) else -1 for value in row[1:]]
    return {"path": str(target.resolve()), "sha256": file_sha256(target), "files": files, "by_spot": by_spot}


def gap_filled_injections(peak_ids: dict[str, Any], alignment_id: Any) -> list[str]:
    """The injections in which MS-DIAL did not detect this spot: its height there is a gap fill."""
    row = (peak_ids or {}).get("by_spot", {}).get(str(alignment_id))
    if row is None:
        return []
    return [name for name, value in zip(peak_ids["files"], row) if value < 0]


def standard_profile(export: dict[str, Any], alignment_id: str) -> dict[str, Any]:
    """A spot's height in every injection, and its CV over the QC and over the Sample and QC injections."""
    spot = next((item for item in export["spots"] if item["id"] == str(alignment_id)), None)
    if spot is None:
        return {}
    heights = {name: (0.0 if _is_zero_height(cell) else float(parse_number(cell)))
               for name, cell in zip(export["sample_names"], spot["cells"])}

    def cv(kinds: set[str]) -> float | None:
        values = [heights[name] for name, kind in zip(export["sample_names"], export["file_types"])
                  if kind.casefold() in kinds]
        if len(values) < 2:
            return None
        mean = sum(values) / len(values)
        if mean <= 0:
            return None
        variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
        return round(math.sqrt(variance) / mean * 100, 2)

    return {"heights": heights, "cv_percent_qc": cv({"qc"}), "cv_percent_sample_qc": cv({"sample", "qc"})}


def resolve_polarity(contract: dict[str, Any], polarity: str, job: dict[str, Any] | None) -> dict[str, Any]:
    """One polarity's internal standards, decided from its run, and the digest the person confirms."""
    outputs = _outputs(contract, polarity, job)
    result: dict[str, Any] = {"polarity": polarity, "outputs": outputs, "problems": list(outputs["problems"])}
    if result["problems"]:
        return result
    export = read_alignment_export(outputs["height_export"])
    candidates = read_candidate_table(outputs["candidate_table"])
    library = read_internal_standard_text(contract["derived"]["internal_standard_libraries"][polarity]["path"], polarity)
    annotator = INTERNAL_STANDARD_ANNOTATOR_IDS[polarity]
    library_ids = {entry["annotator_id"] for entry in contract["annotation"]["libraries"] if entry["kind"] == "msp"}

    def library_annotated(spot: dict[str, Any]) -> bool:
        return spot["method"] in library_ids or spot["method"].startswith("LbmDB")

    tolerance = float(contract["internal_standard_rt_tolerance"])
    resolution = resolve_standards(
        export, candidates, library["records"], polarity=polarity,
        annotation_rt_tolerance=tolerance, internal_standard_rt_tolerance=tolerance,
        library_annotated=library_annotated, library_label="annotation library",
        # No class is quantified here: a standard not found is only absent from figure 04, unless it normalizes.
        not_found="{name} was not found at its adduct's m/z within " + f"{tolerance:g}" + " min of its text RT.",
    )
    normalizing_name = contract["normalizing_standards"][polarity]["name"]
    for item in resolution["standards"]:
        if item["name"] == normalizing_name and item["status"] == "unresolved_missing":
            item["warnings"] = [
                warning + " It is the normalizing standard: nothing can be normalized by it as found."
                if warning.startswith(f"{normalizing_name} was not found") else warning
                for warning in item["warnings"]
            ]
    resolution["warnings"] = [warning for item in resolution["standards"] for warning in item.get("warnings", [])]
    normalizing = contract["normalizing_standards"][polarity]["name"]
    annotated = sum(1 for spot in export["spots"] if spot["method"] in library_ids or spot["method"].startswith("LbmDB"))
    text_annotated = sum(1 for spot in export["spots"] if spot["method"] == annotator)
    if not annotated:
        result["problems"].append(
            "No spot in the run is annotated by any of the contract's libraries; they were not loaded, and nothing "
            "in the run is a library annotation."
        )
    if not text_annotated:
        resolution["warnings"].append(
            f"No spot is annotated by the internal-standard library ({annotator}); check that MS-DIAL loaded it."
        )
    # Each library on its own, over every candidate MS-DIAL kept: the Console skips a library that parses to
    # no record without a word, and a lower tier that always loses still leaves candidates.
    candidate_counts: dict[str, int] = {}
    for rows in candidates["by_spot"].values():
        for row in rows:
            name = str(row.get("annotator_id") or "")
            candidate_counts[name] = candidate_counts.get(name, 0) + 1
    by_library: dict[str, int] = {}
    for entry in contract["annotation"]["libraries"]:
        if polarity not in entry.get("files", {}):
            continue
        ids = set(export_annotator_ids({**entry, "paths": {polarity: entry["files"][polarity]["path"]}}).values())
        by_library[entry["label"]] = sum(candidate_counts.get(identifier, 0) for identifier in ids)
        if annotated and not by_library[entry["label"]]:
            resolution["warnings"].append(
                f"The library '{entry['label']}' ({entry['files'][polarity]['name']}) left no annotation candidate in "
                "this run; MS-DIAL skips a library it reads as empty without a word."
            )
    by_name = {entry["name"]: entry for entry in resolution["standards"]}
    profiles = {
        name: standard_profile(export, entry["alignment_id"])
        for name, entry in by_name.items() if entry.get("alignment_id")
    }
    # Which heights MS-DIAL detected and which it filled: written beside the exports by every Console run.
    peak_ids: dict[str, Any] = {}
    alignment = outputs.get("alignment_name") or ""
    for folder in (Path(outputs["height_export"]).parent, Path(contract["output_roots"][polarity])):
        candidate = folder / f"{alignment}.mdpeakid.tsv"
        if alignment and candidate.is_file():
            peak_ids = read_peak_ids(candidate)
            break
    if not peak_ids:
        resolution["warnings"].append(
            f"{alignment}.mdpeakid.tsv is not beside the exports, so a gap-filled standard height cannot be told from a "
            "detected one."
        )
    digest = resolution_digest({"contract_sha256": contract["_sha256"], "resolution": resolution,
                                "peak_id_matrix_sha256": peak_ids.get("sha256")})
    entry = by_name.get(normalizing, {})
    profile = profiles.get(normalizing, {})
    result.update({
        "resolution": resolution,
        "resolution_digest": digest,
        "peak_ids": peak_ids,
        # Kept when the resolution itself is left out of what is shown.
        "warnings": list(resolution["warnings"]),
        "summary": {
            "normalizing_standard": {
                "name": normalizing, "status": entry.get("status"), "tier": entry.get("tier"),
                "alignment_id": entry.get("alignment_id"), "needs_confirmation": bool(entry.get("needs_confirmation")),
                "reason": entry.get("reason"), "valid_alternatives": entry.get("valid_alternatives", []),
                "ms_dial_name": (entry.get("spot") or {}).get("name"),
                **{key: entry.get(key) for key in ("delta_mz_mda", "delta_mz_ppm", "delta_rt", "label_disagreement")},
                "zero_height_files": entry.get("zero_height_files", []),
                "tolerated_zero_height_files": entry.get("tolerated_zero_height_files", []),
                "gap_filled_files": gap_filled_injections(peak_ids, entry.get("alignment_id")) if entry.get("alignment_id") else [],
                "cv_percent_qc": profile.get("cv_percent_qc"),
                "cv_percent_sample_qc": profile.get("cv_percent_sample_qc"),
            },
            "standards": [
                {key: item.get(key) for key in ("name", "tier", "status", "alignment_id", "needs_confirmation",
                                                "delta_mz_mda", "delta_rt", "label_disagreement", "reason")}
                | {"ms_dial_name": (item.get("spot") or {}).get("name"),
                   "zero_height_files": item.get("zero_height_files", []),
                   "candidates": len(item.get("candidates", [])),
                   "valid_alternatives": item.get("valid_alternatives", []),
                   "cv_percent_qc": profiles.get(item["name"], {}).get("cv_percent_qc"),
                   "cv_percent_sample_qc": profiles.get(item["name"], {}).get("cv_percent_sample_qc")}
                for item in resolution["standards"]
            ],
            "unresolved": [item["name"] for item in resolution["standards"]
                           if item["status"] in {"unresolved_missing", "invalid", "conflict"}],
            "library_annotated_spots": annotated,
            "candidates_by_library": by_library,
            "internal_standard_library_spots": text_annotated,
        },
    })
    return result


def resolve_standards_for_contract(contract_path: str, jobs: dict[str, dict[str, Any] | None]) -> dict[str, Any]:
    """Each named polarity's standards; read-only."""
    contract = load_contract(contract_path)
    polarities: dict[str, Any] = {}
    for polarity, job in jobs.items():
        if polarity not in contract["polarities"]:
            if job is None:
                # A folder read asked for every polarity; this contract has no run of this one.
                continue
            raise LabMetabolomicsError(f"The contract covers {', '.join(contract['polarities'])}, not {polarity}.")
        resolved = resolve_polarity(contract, polarity, job)
        if resolved.get("resolution") is not None:
            resolved["resolution_table"] = _resolution_rows(resolved["resolution"])
            del resolved["resolution"]
        # Every spot's peak IDs: kept for normalize, not shown.
        resolved.pop("peak_ids", None)
        polarities[polarity] = resolved
    return {
        "contract_path": contract["_path"], "contract_sha256": contract["_sha256"], "polarities": polarities,
        "decisions": (
            "Read each polarity's normalizing standard to the person first: its tier, alignment ID, m/z and RT error and "
            "files without a height. It is used as found when its own text row or a library annotated it within the "
            "tolerance; found only by m/z and RT, or with another annotation, it is used only if they accept it "
            "(accept_searched); they may take another listed spot instead (accept_spot). Without it there is nothing "
            "to normalize by. Then read the other standards with their CVs over the QC injections."
        ),
    }


def polarity_of_job(contract: dict[str, Any], job: dict[str, Any]) -> str:
    run_directory = (job.get("preparation") or {}).get("run_directory", "")
    for polarity in contract["polarities"]:
        if _same_path(run_directory, contract["output_roots"][polarity]):
            return polarity
    raise LabMetabolomicsError(f"Job {job.get('id')} wrote into {run_directory}, which is none of the contract's output folders.")


# ---------------------------------------------------------------------------------------------
# Step 4: normalize
# ---------------------------------------------------------------------------------------------


def plan_normalize(
    contract_path: str,
    job: dict[str, Any],
    resolution_digest_text: str,
    accept_searched: Iterable[str] = (),
    accept_spot: dict[str, Any] | None = None,
    accept_gap_filled: bool = False,
) -> dict[str, Any]:
    """The normalizing standard's spot, the normalized matrix and where it is written, or why not.

    A Sample or QC in which MS-DIAL did not detect the standard (its peak ID is -1, the height a gap fill)
    refuses unless the person accepts those heights (accept_gap_filled).
    """
    contract = load_contract(contract_path)
    polarity = polarity_of_job(contract, job)
    resolved = resolve_polarity(contract, polarity, job)
    if resolved["problems"]:
        raise LabMetabolomicsError(f"The {polarity.lower()} run cannot be normalized: " + " ".join(resolved["problems"]))
    if str(resolution_digest_text or "").strip() != resolved["resolution_digest"]:
        raise LabMetabolomicsError(
            "The resolution is not the one the person confirmed (the run's exports or the contract changed, or another "
            "digest was passed); resolve the standards again and show them."
        )
    resolution = resolved["resolution"]
    name = contract["normalizing_standards"][polarity]["name"]
    by_name = {entry["name"]: entry for entry in resolution["standards"]}
    entry = by_name.get(name, {"name": name, "status": "unresolved_missing"})
    accepted = {str(item) for item in accept_searched}
    if accept_spot is not None and not isinstance(accept_spot, dict):
        raise LabMetabolomicsError("accept_spot maps the normalizing standard's name to the alignment ID the person chose.")
    chosen = {str(key): value for key, value in (accept_spot or {}).items()}
    stray = sorted((accepted | set(chosen)) - {name})
    if stray:
        raise LabMetabolomicsError(
            f"Only the normalizing standard {name} takes a decision here; {', '.join(stray)} is not it."
        )
    if accepted and chosen:
        raise LabMetabolomicsError(f"{name} is given two decisions; accept_searched and accept_spot exclude one another.")
    decision = "as resolved"
    tier, status = entry.get("tier"), entry.get("status")
    if name in chosen:
        try:
            candidate = chosen_candidate(entry, chosen[name])
        except LabInputError as error:
            raise LabMetabolomicsError(f"accept_spot: {error}") from None
        alignment_id = str(candidate["alignment_id"])
        decision = "taken by the person (accept_spot)"
        tier, status = candidate.get("tier"), "taken_by_person"
    elif entry.get("status") in {"unresolved_missing", "unresolved_expected", "invalid", "conflict"}:
        alternatives = entry.get("valid_alternatives") or []
        raise LabMetabolomicsError(
            f"The normalizing standard {name} is {entry.get('status')}: {entry.get('reason', '')}. Nothing can be "
            "normalized by it as found"
            + (f"; the person may take one of spots {', '.join(alternatives)} instead (accept_spot)." if alternatives else
               "; write a new contract with another normalizing standard.")
        )
    elif entry.get("needs_confirmation"):
        if name not in accepted:
            raise LabMetabolomicsError(
                f"The normalizing standard {name} was found only as tier {entry.get('tier')} ({entry.get('reason', '')}); "
                "it is used only if the person accepts it (accept_searched) or takes another listed spot (accept_spot)."
            )
        alignment_id = str(entry["alignment_id"])
        decision = "accepted by the person (accept_searched)"
        status = "accepted_by_person"
    else:
        if name in accepted:
            raise LabMetabolomicsError(f"accept_searched names {name}, which this resolution does not ask the person to accept.")
        alignment_id = str(entry["alignment_id"])
    # What is an internal standard from here on: the normalizing standard's spot as decided, and every spot a
    # library or a standard's own text row annotated as that standard (tiers A and B), whatever else the
    # resolver said of it (invalid, contested, outside the window). A spot only found by m/z and RT, or matched
    # under another annotation (C, D), nobody confirmed: it stays a feature, which the merge keeps on its own.
    annotator = INTERNAL_STANDARD_ANNOTATOR_IDS[polarity]
    decided_ids: set[str] = {alignment_id}
    figure_spots: dict[str, str] = {}
    for item in resolution["standards"]:
        decided_ids.update(str(candidate["alignment_id"]) for candidate in item.get("candidates", [])
                           if candidate.get("tier") in DECIDED_TIERS)
        if item.get("alignment_id"):
            figure_spots[item["name"]] = str(item["alignment_id"])
    figure_spots[name] = alignment_id
    unconfirmed: list[dict[str, Any]] = []
    for item in resolution["standards"]:
        spot_id = str(item.get("alignment_id") or "")
        if not spot_id or spot_id in decided_ids or item.get("tier") in DECIDED_TIERS:
            continue
        if item["name"] == name and spot_id == alignment_id:
            continue
        found = next((candidate for candidate in item.get("candidates", []) if str(candidate["alignment_id"]) == spot_id), {})
        unconfirmed.append({
            "standard": item["name"], "alignment_id": spot_id, "tier": item.get("tier"), "status": item["status"],
            "ms_dial_name": (item.get("spot") or {}).get("name"), "method": found.get("method") or (item.get("spot") or {}).get("method"),
            "passed_over": item["name"] == name,
        })
    figure_rows = [{"StandardName": standard, "PeakID": int(spot_id)} for standard, spot_id in figure_spots.items()]
    sample_rows = polarity_sample_table(contract["sample_table"]["rows"], polarity)
    factors = {
        row["file_name"]: float(row.get("factor", 1) or 1)
        for row in sample_rows if row_included(row)
    }
    height_export = read_alignment_export(resolved["outputs"]["height_export"])
    export_names = height_export["sample_names"]
    by_column = _factors_by_column(export_names, factors)
    # MS-DIAL fills a gap with the signal there: a standard it did not detect still has a height.
    peak_ids = resolved.get("peak_ids") or {}
    not_detected = gap_filled_injections(peak_ids, alignment_id)
    kinds = dict(zip(export_names, (kind.casefold() for kind in height_export["file_types"])))
    gap_filled_required = [item for item in not_detected if kinds.get(item) in {"sample", "qc"}]
    if gap_filled_required and accept_gap_filled is not True:
        heights = {item: (next(spot for spot in height_export["spots"] if spot["id"] == alignment_id)["cells"][export_names.index(item)])
                   for item in gap_filled_required}
        raise LabMetabolomicsError(
            f"MS-DIAL did not detect {name} (alignment ID {alignment_id}) in "
            + ", ".join(f"{item} (gap-filled height {heights[item]})" for item in gap_filled_required)
            + ": the height there is the signal where the peak should be, and dividing by it scales every feature of "
            "that injection. Normalize only if the person accepts these heights (accept_gap_filled=true)."
        )
    normalized = normalize_heights(resolved["outputs"]["height_export"], alignment_id, by_column, not_detected)
    if normalized["problems"]:
        raise LabMetabolomicsError(f"The {polarity.lower()} run cannot be normalized by {name}: " + " ".join(normalized["problems"]))
    quant = Path(contract["quant_folder"])
    folder = FOLDERS[polarity]
    paths = {
        "normalized": quant / f"NormalizedHeight_{LABELS[polarity]}.txt",
        "height_copy": quant / f"Height_{LABELS[polarity]}.txt",
        "resolution": quant / f"is_resolution_{folder}.tsv",
        "record": quant / f"normalize_{folder}.local.json",
    }
    warnings = []
    for injection, reason in normalized["raw_height_reasons"].items():
        warnings.append(f"{injection} keeps its raw heights: {reason} ({name}).")
    if gap_filled_required:
        warnings.append(
            f"The person accepted the gap-filled {name} heights of {', '.join(gap_filled_required)} (accept_gap_filled)."
        )
    # What the merge will do with each other standard's spot, by the rules it applies (lab_lcms_metabolomics).
    candidates = read_candidate_table(resolved["outputs"]["candidate_table"])
    defaults = (contract.get("merge") or {}).get("defaults") or {}
    identities_preferred = adduct_identities(preferred_adducts(contract["salt"]["salt"]))
    spots_by_id = {spot["id"]: spot for spot in height_export["spots"]}
    # Every spot the merge drops as an internal standard besides the one normalized by, once each: each
    # standard's A or B candidates, the one chosen for it or not, the normalizing standard's included.
    named: set[str] = set()
    for item in resolution["standards"]:
        for candidate in item.get("candidates", []):
            spot_id = str(candidate.get("alignment_id") or "")
            if candidate.get("tier") not in DECIDED_TIERS or spot_id == alignment_id or spot_id in named:
                continue
            named.add(spot_id)
            chosen_here = spot_id == str(item.get("alignment_id") or "")
            role = ("the spot chosen for it" if chosen_here and item["name"] != name
                    else "a spot the person passed over" if item["name"] == name and chosen_here
                    else "not the spot chosen for it")
            warnings.append(
                f"Spot {spot_id} ('{candidate.get('name')}') was annotated as {item['name']} (tier {candidate['tier']}, "
                f"{role}): the merge drops it as an internal standard."
            )
    # The merge also drops every row the internal-standard text row annotated, whatever its tier.
    unconfirmed_ids = {item["alignment_id"] for item in unconfirmed}
    for item in resolution["standards"]:
        for candidate in item.get("candidates", []):
            spot_id = str(candidate.get("alignment_id") or "")
            if (candidate.get("method") != annotator or spot_id == alignment_id or spot_id in named
                    or spot_id in unconfirmed_ids):
                continue
            named.add(spot_id)
            warnings.append(
                f"Spot {spot_id} ('{candidate.get('name')}'): the internal-standard text row annotated it as {item['name']} "
                f"(tier {candidate.get('tier')}, not the spot chosen for it); the merge drops it as an internal standard."
            )
    for item in unconfirmed:
        how = ("its text row matched it, but another annotation was kept for the spot" if item["tier"] == "C"
               else "it was found only by m/z and RT")
        spot = spots_by_id.get(item["alignment_id"], {})
        representative = representative_candidate(candidates, item["alignment_id"]) or {}
        if str(spot.get("name", "")).strip().casefold() in UNANNOTATED_NAMES:
            fate = "the merge drops it as unannotated"
        elif item["method"] == annotator:
            fate = "the merge drops it as an internal standard, since the internal-standard text row annotated it"
        elif not representative.get("is_reference_matched") and defaults.get("reference_matched_only", True):
            fate = "the merge drops it as not reference matched, unless it runs with reference_matched_only=false"
        else:
            fate = "nobody confirmed it as the standard, so the merge keeps it as its own row, outside the InChIKey groups"
            if not is_preferred(str(spot.get("adduct", "")), identities_preferred):
                fate += f", unless preferred_adducts_only drops its adduct {spot.get('adduct')}"
        passed = " The person took another spot for it." if item["passed_over"] else ""
        warnings.append(
            f"Spot {item['alignment_id']} ('{item['ms_dial_name']}') is where {item['standard']} was looked for: {how} "
            f"(tier {item['tier']}, {item['status']}); {fate}.{passed}"
        )
    project = Path(str(resolved["outputs"].get("project") or ""))
    height = Path(resolved["outputs"]["height_export"])
    if project.is_file() and project.stat().st_mtime - height.stat().st_mtime > CURATION_SLACK_SECONDS:
        warnings.append(
            f"{project.name} was saved {int((project.stat().st_mtime - height.stat().st_mtime) // 60)} min after the "
            "run's exports, as after curation in the GUI. This normalizes the exports the run wrote; names, Unknowns "
            "and comments set in the GUI afterwards do not reach it."
        )
    return {
        "contract_path": contract["_path"], "contract_sha256": contract["_sha256"], "polarity": polarity,
        "job_id": job.get("id"), "resolution_digest": resolved["resolution_digest"],
        "normalizing_standard": {"name": name, "alignment_id": alignment_id, "decision": decision,
                                 "tier": tier, "status": status},
        "internal_standard_ids": sorted(decided_ids, key=lambda value: (len(value), value)),
        "unconfirmed_standard_spots": unconfirmed,
        "figure_rows": figure_rows,
        "export_column_factors": by_column,
        "not_detected": not_detected,
        "accepted_gap_filled": gap_filled_required,
        "peak_id_matrix": {key: peak_ids.get(key) for key in ("path", "sha256")} if peak_ids else None,
        "standard_mean": normalized["standard_mean"],
        "mean_over": normalized["mean_over"],
        "standard_heights": normalized["standard_heights"],
        "multipliers": normalized["multipliers"],
        "raw_height_kept": normalized["raw_height_kept"],
        "raw_height_reasons": normalized["raw_height_reasons"],
        "dilution_factors": normalized["dilution_factors"],
        "features": len(normalized["values"]),
        "outputs": {key: resolved["outputs"][key] for key in ("height_export", "candidate_table", "project", "alignment_name")},
        "paths": {key: str(value) for key, value in paths.items()},
        "warnings": warnings,
        "computes": (
            f"normalized = height / height of {name} (alignment ID {alignment_id}) in that injection x "
            f"{normalized['standard_mean']:.6g}, its mean height over the {len(normalized['mean_over'])} Sample and QC "
            "injections, x the injection's dilution factor"
        ),
        "resolution": resolution,
        "_normalized": normalized,
    }


def _factors_by_column(export_names: list[str], factors: dict[str, float]) -> dict[str, float]:
    """The sample table's dilution factors by the export's column names, which drop the file extension."""
    by_stem = {Path(name).stem.casefold(): value for name, value in factors.items()}
    by_stem.update({name.casefold(): value for name, value in factors.items()})
    return {name: by_stem.get(name.casefold(), 1.0) for name in export_names}


def public_normalize_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in plan.items() if key not in {"resolution", "_normalized"}}


def finish_normalize(plan: dict[str, Any]) -> dict[str, Any]:
    """Write the normalized matrix, check every cell again from the Height export, and record it."""
    paths = plan["paths"]
    normalized = plan["_normalized"]
    target = Path(paths["normalized"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(normalized_text(normalized).encode("utf-8"))
    shutil.copyfile(plan["outputs"]["height_export"], paths["height_copy"])
    resolution_file = write_resolution_tsv(plan["resolution"], paths["resolution"])
    # Recomputed on its own from the copy just taken, which must be the export normalize read.
    verification = verify_normalized(
        target, paths["height_copy"], plan["normalizing_standard"]["alignment_id"], plan["export_column_factors"],
        expected_height_sha256=normalized["height_export_sha256"], not_detected=plan["not_detected"],
    )
    record: dict[str, Any] = {
        "schema": NORMALIZE_RECORD_SCHEMA,
        "sharing": "local_only",
        "recorded_at": dt.datetime.now().astimezone().isoformat(),
        "contract_path": plan["contract_path"], "contract_sha256": plan["contract_sha256"],
        "polarity": plan["polarity"], "run_job_id": plan["job_id"], "resolution_digest": plan["resolution_digest"],
        "normalizing_standard": plan["normalizing_standard"],
        "standard_mean": plan["standard_mean"], "mean_over": plan["mean_over"],
        "raw_height_kept": plan["raw_height_kept"], "raw_height_reasons": plan["raw_height_reasons"],
        "dilution_factors": plan["dilution_factors"],
        "not_detected": plan["not_detected"], "accepted_gap_filled": plan["accepted_gap_filled"],
        "peak_id_matrix": plan["peak_id_matrix"],
        "internal_standard_ids": plan["internal_standard_ids"],
        "unconfirmed_standard_spots": plan["unconfirmed_standard_spots"],
        "candidate_table": plan["outputs"]["candidate_table"],
        "candidate_table_sha256": file_sha256(plan["outputs"]["candidate_table"]),
        "height_export": plan["outputs"]["height_export"],
        "height_export_sha256": normalized["height_export_sha256"],
        "outputs": {
            "normalized": str(target), "normalized_sha256": file_sha256(target),
            "height_copy": paths["height_copy"], "height_sha256": file_sha256(paths["height_copy"]),
            "resolution": resolution_file["path"],
        },
        "verification": verification,
    }
    record["completed"] = True
    record["verified"] = bool(verification["passed"])
    Path(paths["record"]).write_text(json.dumps(record, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    record["record"] = paths["record"]
    record_event(plan["contract_path"], {
        "event": "normalized", "polarity": plan["polarity"], "verified": record["verified"], "record": paths["record"],
        "normalizing_standard": plan["normalizing_standard"],
    })
    return record


# ---------------------------------------------------------------------------------------------
# Step 5: merge
# ---------------------------------------------------------------------------------------------


def merge(
    contract_path: str,
    *,
    reference_matched_only: bool | None = None,
    preferred_adducts_only: bool | None = None,
    duplicate_rule: str | None = None,
    inchikey_match: str | None = None,
    output_folder: str = MERGE_FOLDER,
    write: bool = False,
) -> dict[str, Any]:
    """The metabolome of every polarity the contract covers, from the matrices normalize wrote.

    Each option follows the contract unless given; one given otherwise is applied and said as a deviation.
    """
    contract = load_contract(contract_path)
    for key, value in (("reference_matched_only", reference_matched_only), ("preferred_adducts_only", preferred_adducts_only)):
        if value is not None and not isinstance(value, bool):
            raise LabMetabolomicsError(f"{key} is true or false, not {value!r}.")
    for key, value, choices in (("duplicate_rule", duplicate_rule, DUPLICATE_RULES),
                                ("inchikey_match", inchikey_match, INCHIKEY_MATCHES)):
        if value is not None and value not in choices:
            raise LabMetabolomicsError(f"{key} is one of {', '.join(choices)}, not {value!r}.")
    name = str(output_folder or "").strip()
    if (not _FOLDER_NAME.match(name) or name.casefold() in RESERVED_QUANT_FOLDERS
            or name.split(".")[0].casefold() in WINDOWS_DEVICE_NAMES):
        raise LabMetabolomicsError(
            "output_folder is one folder name inside quant (letters, digits, _ . -, not ending in a dot), not pos, "
            "neg, qa_figures or a Windows device name."
        )
    target_folder = Path(contract["quant_folder"]) / name
    if target_folder.exists() and not target_folder.is_dir():
        raise LabMetabolomicsError(f"{target_folder} is a file normalize or an earlier step wrote; choose another output_folder.")
    defaults = (contract.get("merge") or {}).get("defaults") or {}
    contract_options = {
        "reference_matched_only": defaults.get("reference_matched_only", True),
        "preferred_adducts_only": defaults.get("preferred_adducts_only", False),
        "duplicate_rule": defaults.get("duplicate_rule") or "intensity",
        "inchikey_match": defaults.get("inchikey_match") or "first_block",
    }
    given = {"reference_matched_only": reference_matched_only, "preferred_adducts_only": preferred_adducts_only,
             "duplicate_rule": duplicate_rule, "inchikey_match": inchikey_match}
    options = {key: contract_options[key] if given[key] is None else given[key] for key in contract_options}
    deviations = [f"{key} is {options[key]}; the contract says {contract_options[key]}"
                  for key in options if options[key] != contract_options[key]]
    # The confirmed table names each export column's sample, which pairs the polarities whatever their file names.
    sample_map: dict[str, dict[str, str]] = {}
    for row in contract["sample_table"]["rows"]:
        if not row_included(row):
            continue
        for polarity, key in ((POSITIVE, "positive_file"), (NEGATIVE, "negative_file")):
            if row.get(key):
                sample_map.setdefault(polarity, {})[Path(str(row[key])).stem] = row["stem"]
    quant = Path(contract["quant_folder"])
    inputs = []
    for polarity in contract["polarities"]:
        path = quant / f"normalize_{FOLDERS[polarity]}.local.json"
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise LabMetabolomicsError(f"The {polarity.lower()} polarity has not been normalized ({path.name} is missing).") from None
        if record.get("contract_sha256") != contract["_sha256"]:
            raise LabMetabolomicsError(f"The {polarity.lower()} normalize was made under another contract; normalize it again.")
        if not record.get("verified"):
            raise LabMetabolomicsError(
                f"The {polarity.lower()} normalized matrix did not pass its check: "
                + " ".join((record.get("verification") or {}).get("problems", []))
            )
        outputs = record.get("outputs") or {}
        if not Path(outputs.get("normalized", "")).is_file() or file_sha256(outputs["normalized"]) != outputs.get("normalized_sha256"):
            raise LabMetabolomicsError(f"The {polarity.lower()} normalized matrix is not the one normalize wrote.")
        if file_sha256(record["candidate_table"]) != record["candidate_table_sha256"]:
            raise LabMetabolomicsError(f"The {polarity.lower()} run's candidate table has changed since normalize.")
        inputs.append({
            "polarity": polarity, "normalized": outputs["normalized"], "candidate_table": record["candidate_table"],
            "internal_standard_ids": record.get("internal_standard_ids", []),
            "internal_standard_annotator": INTERNAL_STANDARD_ANNOTATOR_IDS[polarity],
            "unconfirmed_standard_spots": record.get("unconfirmed_standard_spots", []),
        })
    # By the annotator ID an export's Comment gives each library: the LBM2's is the Console's own.
    labels: dict[str, str] = {}
    for entry in contract["annotation"]["libraries"]:
        paths = {polarity: item["path"] for polarity, item in entry.get("files", {}).items()}
        for identifier in export_annotator_ids({**entry, "paths": paths}).values():
            labels[identifier] = entry["label"]
    try:
        report = merge_metabolome(
            inputs, quant / name, salt=contract["salt"]["salt"], labels=labels, write=write, sample_map=sample_map,
            **options,
        )
    except LabInputError as error:
        raise LabMetabolomicsError(str(error)) from None
    except OSError as error:
        raise LabMetabolomicsError(f"The metabolome could not be written into {quant / name}: {error}") from None
    report["contract_defaults"] = contract_options
    report["deviations_from_contract"] = deviations
    report.setdefault("warnings", []).extend(f"Not as the contract states: {item}." for item in deviations)
    if write:
        # The report again, with what this step adds: the contract's options and any deviation from them.
        target = quant / name / MERGE_REPORT_NAME
        target.write_text(json.dumps({key: value for key, value in report.items() if key != "report"}, indent=2,
                                     ensure_ascii=False, default=str) + "\n", encoding="utf-8")
        record_event(contract["_path"], {
            "event": "merged", "output_folder": str(quant / name), "options": options, "deviations": deviations,
            "outputs": {key: item["sha256"] for key, item in report["outputs"].items()},
            "kept": report["counts"]["kept"], "removed": report["counts"]["removed"],
        })
        report["merge_report"] = str(target.resolve())
    return report
