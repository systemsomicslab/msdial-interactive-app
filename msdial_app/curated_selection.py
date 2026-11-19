"""A triaged unit's file list, held by Interactive: the curated selection (0.5.69).

WHY THIS EXISTS. Campaign stage3b was stopped on 2026-11-15: every unit of a multi-unit accession ran on the whole
study's files, because the Catalog's one shared archive admitted everything, and 79 done unit/parts over 29 accessions
were contaminated. The author approved triage before download: an agent with the msdial-unit-triage skill reads the
Catalog rows, the mwTab text and the archive listing, and writes one file per unit (schema msdial-unit-triage.v1)
saying which rows and archive members the unit keeps, and why. scripts/validate-triage.py checks those files as a set,
and a person approves the campaign manifest digest that carries their sha256.

This module is what makes Interactive hold a unit to that file list, and nothing else:

- ``apply_curated_selection`` cuts the Catalog handoff's sample rows to the triage's kept rows, declares one analysis
  input per kept row by its archive member path (so the lease matches by path and never admits a whole archive by
  name), fixes the unit's polarity and acquisition from the triage, and records the provenance block
  ``repository_metadata.curated_selection``. Every refusal is a ``CuratedSelectionError`` raised before download, so
  no byte moves for a unit whose triage does not hold.
- ``reconcile_curated_selection`` compares each admitted input's raw header (polarity, acquisition) with what the
  triage said of it. A disagreement holds the unit for a person (``triage_header_disagreement``); nothing reruns it.
- ``cross_unit_attribution`` compares the Sample inputs of sibling units after their preflights: one polarity
  contradiction (P), a set shared across chromatographies (C), a set mostly inside another's at one polarity and
  chromatography (S) or an identical set (D) is refused.

WHAT IT RECORDS, AND WHY IT IS ALL HASHES. The author's requirement of 2026-11-16 is that LLM use in data management
must not leave a black box in the paper. The triage is a model inference until a person approves the digest that
carries it; the provenance says which (``standing``), names the model, skill and skill version the triage file
records, and carries the sha256 of the triage, of its decision record when one sits beside it, of the Catalog handoff
it was made from, and of the selection itself in a canonical form (``selection_sha256``) a third party can recompute
from the released triage without any model. Nothing here calls a model.

WHAT IT NEVER DOES. It never chooses a file. It never widens a selection: a row the triage does not keep is not an
input, and a kept row whose archive member is unresolved refuses the unit rather than falling back to names. Archive
members no Catalog row names (``added_members``: a pooled QC, a blank) are recorded and not admitted beside kept rows,
since they have no sample row for the approved Class to assign. A selection that keeps no Catalog row and keeps added
members (0.5.70: Metabolomics Workbench ST004700-ST004811, where only the pooled DDA runs carry MS/MS and every Catalog
row is MS1-only) runs on those members alone: each is one input and one sample row of its own, typed by its role, in
the run's one Class (``All`` for an abstention).

POLARITY-SPLIT CHILDREN (0.5.70). A triage's optional ``polarity_split`` (polarity_split_of) makes the selection one of
two runs over the same files, each file acquired with polarity switching: the author's decision is that MS-DIAL 5
reads only the scans whose polarity matches the project's ion mode, so each such file is analysed once as Positive and
once as Negative. The run is keyed ``<unit_id>-pos`` or ``<unit_id>-neg`` (run_key), so the siblings never share a
campaign entry, a store claim or a batch row; a switching header is that child's polarity at the preflight, and the
cross-unit check does not refuse the siblings for holding the same files.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

TRIAGE_SCHEMAS = ("msdial-unit-triage.v1", "msdial-unit-triage.v2")
PROVENANCE_SCHEMA = "msdial-curated-selection.v1"
RECONCILIATION_SCHEMA = "msdial-curated-selection-reconciliation.v1"
CROSS_UNIT_SCHEMA = "msdial-cross-unit-attribution.v1"
CROSS_UNIT_RULE = "cross_unit_attribution_2026_11_16b"
SELECTION_CANONICAL_FORM = "msdial-selection-canonical.v1"

# Refusals before download (CuratedSelectionError.code). Each is final for the triage as it is: the same triage and
# handoff give the same refusal, so none is retried.
CURATED_SELECTION_INVALID = "curated_selection_invalid"
CURATED_SELECTION_NOT_READY = "curated_selection_not_ready"
CURATED_SELECTION_HANDOFF_MISMATCH = "curated_selection_handoff_mismatch"
CURATED_SELECTION_NOT_APPROVED = "curated_selection_not_approved"
CURATED_SELECTION_RECORD_MISMATCH = "curated_selection_record_mismatch"
CURATED_SELECTION_CHILD_UNKNOWN = "curated_selection_child_unknown"
CURATED_SELECTION_MIXED_POLARITY = "curated_selection_mixed_polarity"
CURATED_SELECTION_MEMBER_UNRESOLVED = "curated_selection_member_unresolved"
CURATED_SELECTION_EMPTY = "curated_selection_empty"
CURATED_SELECTION_CLASS_CONTRAST_LOST = "curated_selection_class_contrast_lost"
CURATED_SELECTION_SELECTION_MISMATCH = "curated_selection_selection_mismatch"
# A polarity_split object that is not {ion_mode, sibling_ion_mode (the opposite), reason polarity_switching,
# parent_unit_id (the triage's unit)} with ion_mode equal to unit_technical.ion_mode (0.5.70).
CURATED_SELECTION_POLARITY_SPLIT_INVALID = "curated_selection_polarity_split_invalid"
# After the preflight: the raw headers say otherwise than the triage. A hold for a person, never a rerun.
TRIAGE_HEADER_DISAGREEMENT = "triage_header_disagreement"
# After every sibling's preflight (cross_unit_attribution).
CROSS_UNIT_POLARITY_CONTRADICTION = "cross_unit_polarity_contradiction"
CROSS_UNIT_INDISTINGUISHABLE_CHROMATOGRAPHY = "cross_unit_inputs_indistinguishable_chromatography"
CROSS_UNIT_DUPLICATE_INPUTS = "cross_unit_duplicate_inputs"
CROSS_UNIT_CONTAINED_INPUTS = "cross_unit_inputs_contained"
CROSS_UNIT_PARTLY_SHARED_WARNING = "cross_unit_inputs_partly_shared"
CROSS_UNIT_HEADER_OVERRIDES_INFERENCE_WARNING = "cross_unit_header_polarity_overrides_inferred_polarity"

CURATED_SELECTION_REFUSALS = frozenset(
    {
        CURATED_SELECTION_INVALID,
        CURATED_SELECTION_NOT_READY,
        CURATED_SELECTION_HANDOFF_MISMATCH,
        CURATED_SELECTION_NOT_APPROVED,
        CURATED_SELECTION_RECORD_MISMATCH,
        CURATED_SELECTION_CHILD_UNKNOWN,
        CURATED_SELECTION_MIXED_POLARITY,
        CURATED_SELECTION_MEMBER_UNRESOLVED,
        CURATED_SELECTION_EMPTY,
        CURATED_SELECTION_CLASS_CONTRAST_LOST,
        CURATED_SELECTION_SELECTION_MISMATCH,
        CURATED_SELECTION_POLARITY_SPLIT_INVALID,
    }
)
CROSS_UNIT_REFUSALS = frozenset(
    {
        CROSS_UNIT_POLARITY_CONTRADICTION,
        CROSS_UNIT_INDISTINGUISHABLE_CHROMATOGRAPHY,
        CROSS_UNIT_CONTAINED_INPUTS,
        CROSS_UNIT_DUPLICATE_INPUTS,
    }
)
# Containment at or above which two units' Sample inputs are one set (the coverage gate's minimum, 2026-11-15).
CROSS_UNIT_CONTAINMENT_MINIMUM = 0.5

_POLARITY = {"positive": "Positive", "negative": "Negative"}
_MIXED_POLARITY = {"both", "switching", "unknown", ""}
_TRIAGE_ACQUISITION = {"DDA": "DDA", "DIA": "DIA", "SWATH": "SWATH", "AIF": "AIF"}
_DIA_FAMILY = {"DIA", "SWATH", "AIF"}
_HEADER_POLARITIES = {"Positive", "Negative"}
_HEADER_SWITCHING = {"PolaritySwitching", "MixedFunctions"}
_READ_OUTCOMES = {"ok", "reused"}
# A triage row role that states nothing: only such a row is read by its file and sample names (_unit_view); any other
# stated role decides by itself whether the input is a Sample input of the cross-unit check.
_UNKNOWN_ROLES = {"unknown", ""}
# polarity_split (0.5.70): the reasons a unit's files are analysed once per polarity, the opposite of each polarity,
# and the run key's suffix for each (as a split part's polarity is keyed: raw_metadata_preflight's part_key).
POLARITY_SPLIT_REASONS = ("polarity_switching",)
_OPPOSITE_POLARITY = {"positive": "negative", "negative": "positive"}
_POLARITY_SPLIT_SUFFIX = {"positive": "pos", "negative": "neg"}
# An added member's role as the analysis CSV's file type, where it states one (the others are read by name).
_ROLE_FILE_TYPE = {"qc": "QC", "blank": "Blank", "sample": "Sample"}


class CuratedSelectionError(ValueError):
    """A curated selection was passed and does not hold. Raised before download; the unit is excluded.

    A ValueError, so every structured-error path reports it; the message starts with a fixed token and the code, so an
    unattended caller tells it from any other validation failure without parsing prose. ``retryable`` is always
    False: the same triage and handoff give the same refusal.
    """

    retryable = False

    def __init__(self, code: str, message: str, **detail: Any) -> None:
        self.code = code
        self.detail = dict(detail)
        super().__init__(f"curated_selection_refused [{code}]: {message}")

    def failure(self) -> dict[str, Any]:
        """The failure a tool returns and a campaign runner reads (reason_code, retryable false)."""
        return {
            "ok": False,
            "reason": self.code,
            "reason_code": self.code,
            "codes": [self.code],
            "retryable": False,
            "detail": str(self),
            **({"refusal": dict(self.detail)} if self.detail else {}),
            "error_type": type(self).__name__,
        }


# --------------------------------------------------------------------------------------------------------------------
# Canonical forms. Kept free of everything else in the package, so a replay script can import or copy them.


def canonical_json(value: Any) -> bytes:
    """Sorted keys, no spaces, UTF-8, no ASCII escaping: the one form every hash here is taken over."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _member_key(text: Any) -> str:
    return str(text or "").replace("\\", "/").strip().strip("/").casefold()


def _selection_member(path: Any) -> str:
    return str(path or "").replace("\\", "/").strip().strip("/")


def selection_canonical(
    triage: Mapping[str, Any],
    listing: Mapping[str, Mapping[str, Any]] | None = None,
    child: Mapping[str, Any] | str | None = None,
) -> list[dict[str, Any]]:
    """The selection in SELECTION_CANONICAL_FORM (msdial-selection-canonical.v1): the normative definition of
    msdial-public-reanalysis docs/llm-assisted-curation.md section 3.1, which the plan, the campaign runner and
    replay-campaign.py import from here (or vendor unchanged) so that all give one value for one unit.

    Every item the triage keeps, its ``rows[]`` and its ``added_members[]`` (whether or not this version admits added
    members), one entry each: {unit_id (the Catalog unit id, bare, also for a child), child_key (or null), member_path
    (the triage's, ``\\`` turned into ``/``, leading and trailing ``/`` removed, case kept), archive_name (the item's
    ``archive``, else the listing's), crc32 (8 upper-case hex digits) and size, both from ``listing`` only, never from
    the triage or a download (null without a match), sample_row_index (the row's ``row_index``, null for an added
    member)}. Sorted by unit_id, child_key (null as ''), member_path, sample_row_index (null as -1), then canonical
    JSON.

    ``listing`` is {case-folded member path: {archive, crc32, size}} parsed from the archive listing(s) the decision
    record binds (read_decision_record, parse_archive_listing); {} or None when it binds none. ``child`` is one item of
    ``triage["children"]``, or its child_key (an unknown key raises ValueError), or None for the whole unit.
    """
    if isinstance(listing, str):
        raise TypeError("selection_canonical(triage, listing, child): listing is a mapping, not a child key.")
    listing = listing or {}
    if isinstance(child, str):
        if not child:
            child = None
        else:
            found = _child(triage, child)
            if found is None:
                raise ValueError(f"The triage has no child {child!r}.")
            child = found
    child_key = None if child is None else str(child["child_key"])
    allowed = None if child is None else {_selection_member(p).casefold() for p in child.get("member_paths") or []}
    kept = [(item, item.get("row_index")) for item in triage.get("rows") or []]
    kept += [(item, None) for item in triage.get("added_members") or []]
    entries = []
    for item, row_index in kept:
        member = _selection_member(item.get("member_path"))
        if allowed is not None and member.casefold() not in allowed:
            continue
        listed = (listing.get(member.casefold()) or {}) if member else {}
        crc32, size = listed.get("crc32"), listed.get("size")
        entries.append({
            "unit_id": str(triage["unit_id"]),
            "child_key": child_key,
            "member_path": member or None,
            "archive_name": item.get("archive") or listed.get("archive") or None,
            "crc32": None if crc32 in (None, "") else str(crc32).upper().zfill(8),
            "size": None if size in (None, "") else int(size),
            "sample_row_index": None if row_index is None else int(row_index),
        })
    return sorted(entries, key=lambda e: (e["unit_id"], e["child_key"] or "", e["member_path"] or "",
                                          -1 if e["sample_row_index"] is None else e["sample_row_index"],
                                          canonical_json(e)))


# The name the reference implementation of section 3.1 uses.
selection_entries = selection_canonical


def selection_sha256(
    triage: Mapping[str, Any],
    listing: Mapping[str, Mapping[str, Any]] | None = None,
    child: Mapping[str, Any] | str | None = None,
) -> str:
    """sha256 of the canonical JSON of selection_canonical(triage, listing, child): ``selection_sha256``."""
    return sha256_bytes(canonical_json(selection_canonical(triage, listing, child)))


ARCHIVE_LISTING_COLUMNS = ("archive", "path", "size", "crc32")


def parse_archive_listing(data: bytes, listing: dict[str, dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    """An archive listing TSV (columns archive, path, size, crc32; others ignored) as selection_canonical reads it:
    {case-folded member path: {archive, crc32, size}}. The first row of a path wins; ``listing`` is extended in place
    when given (a record binding one listing per archive). Raises ValueError for a size that is not a whole number."""
    found: dict[str, dict[str, Any]] = listing if listing is not None else {}
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")), delimiter="\t")
    for record in reader:
        member = _selection_member(record.get("path"))
        if not member:
            continue
        size = str(record.get("size") or "").strip()
        if size and not size.isdigit():
            raise ValueError(f"The archive listing gives size {size!r} for {member}.")
        found.setdefault(member.casefold(), {
            "archive": str(record.get("archive") or "").strip() or None,
            "crc32": str(record.get("crc32") or "").strip() or None,
            "size": int(size) if size else None,
        })
    return found


ANALYSIS_CSV_CANONICAL_FORM = "msdial-analysis-csv-canonical.v1"
REPLAY_PROJECTION_SCHEMA = "msdial-replay-projection.v1"
_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")


def _canonical_cell(cell: str, raw_directory: str) -> str:
    text = cell.strip()
    if not _ABSOLUTE.match(text) or not raw_directory:
        return cell
    path = text.replace("\\", "/")
    raw = str(raw_directory).replace("\\", "/").rstrip("/")
    unit = raw.rsplit("/", 1)[0] if "/" in raw else raw
    if path.casefold() == raw.casefold():
        return "."
    if path.casefold().startswith(raw.casefold() + "/"):
        return path[len(raw) + 1:]
    if path.casefold().startswith(unit.casefold() + "/"):
        return "../" + path[len(unit) + 1:]
    return "<external>/" + path.rsplit("/", 1)[-1]


def canonical_analysis_csv(data: bytes, raw_directory: str) -> bytes:
    """An analysis CSV in the form the transparency framework hashes (ANALYSIS_CSV_CANONICAL_FORM).

    Every absolute path is rewritten relative to the unit's raw directory with forward slashes (a path elsewhere in
    the unit's workspace as ../<path>, one outside it as <external>/<name>), line endings are LF, the text is UTF-8
    without a byte-order mark. The cells are split on ',' only, as the pinned Console reads the file.
    """
    text = data.decode("utf-8-sig")
    lines = [
        ",".join(_canonical_cell(cell, raw_directory) for cell in line.split(","))
        for line in text.splitlines()
        if line.strip()
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def canonical_analysis_csv_sha256(csv_path: str | Path, raw_directory: str) -> str:
    return sha256_bytes(canonical_analysis_csv(Path(csv_path).read_bytes(), raw_directory))


def replay_projection(manifest: Mapping[str, Any], interactive_version: str = "") -> dict[str, Any]:
    """The whitelist of a run manifest a replay compares (REPLAY_PROJECTION_SCHEMA). Pure; no timestamps, job ids,
    host names or absolute paths, by construction: every value is copied from a named field or made portable
    (_portable_path: relative to the data root, else raw:/<path under the raw directory>, else <external>/<name>).

    Fields: repository, accession, analysis unit id; the Catalog handoff sha256 (the curated selection's, else the
    canonical JSON of the handoff the manifest carries); the curated selection's hashes; ion mode, acquisition mode and
    separation; the inputs (member path, identity, sample id, acquisition type, header polarity), sorted; the analysis
    CSV's canonical sha256; the accepted minimum peak height with its diagnostic count and step; the Interactive
    version. Libraries, the Console identity and the parameter file are not in v1.
    """
    project = manifest.get("project") if isinstance(manifest.get("project"), dict) else {}
    metadata = project.get("repository_metadata") if isinstance(project.get("repository_metadata"), dict) else {}
    block = metadata.get("curated_selection") if isinstance(metadata.get("curated_selection"), dict) else None
    handoff = metadata.get("catalog_handoff") if isinstance(metadata.get("catalog_handoff"), dict) else None
    data_root = str(manifest.get("input_directory") or "")
    preflight = manifest.get("raw_metadata_preflight") if isinstance(manifest.get("raw_metadata_preflight"), dict) else {}
    headers = {
        str(item.get("file") or "").replace("\\", "/").casefold(): str(item.get("polarity") or "")
        for item in ((preflight.get("summary") or {}).get("per_file") or [])
        if isinstance(item, dict)
    }
    inputs = []
    raw_directory = _raw_directory(manifest)
    lineage = manifest.get("input_lineage") if isinstance(manifest.get("input_lineage"), dict) else {}
    for row in lineage.get("rows") or []:
        if not isinstance(row, dict) or not str(row.get("path") or "").strip():
            continue
        path = str(row["path"])
        identity, basis = _input_identity(row, data_root)
        source = row.get("source") if isinstance(row.get("source"), dict) else {}
        conversion = source.get("conversion") if isinstance(source.get("conversion"), dict) else {}
        origin = str(conversion.get("path") or "")
        # The member the input is: the triage's, else (for an mzML converted from an mzXML member) its source's, else
        # its path made portable (_portable_path). Never absolute (review of 0.5.69: a converted input under
        # raw/converted kept its absolute path).
        member = str(
            row.get("triage_member_path")
            or (_portable_path(origin, data_root, raw_directory) if origin else "")
            or _portable_path(path, data_root, raw_directory)
        ).replace("\\", "/")
        inputs.append(
            {
                "member": member,
                **({"converted_to": "mzML"} if origin else {}),
                "identity": identity,
                "identity_basis": basis,
                "sample_id": str(row.get("sample_id") or ""),
                "acquisition_type": str(row.get("acquisition_type") or ""),
                "header_polarity": headers.get(path.replace("\\", "/").casefold(), ""),
            }
        )
    inputs.sort(key=lambda item: (item["member"].casefold(), item["identity"]))
    diagnostics = [item for item in manifest.get("peak_height_diagnostics") or [] if isinstance(item, dict)]
    latest = diagnostics[-1] if diagnostics else {}
    return {
        "schema": REPLAY_PROJECTION_SCHEMA,
        "repository": str(project.get("repository") or ""),
        "accession": str(project.get("accession") or ""),
        "analysis_unit_id": str(project.get("analysis_unit_id") or ""),
        "catalog_handoff_sha256": (
            str(block.get("catalog_handoff_sha256") or "") if block else
            (sha256_bytes(canonical_json(handoff)) if handoff else "")
        ),
        "curated_selection": (
            {key: block.get(key) or "" for key in (
                "triage_sha256", "triage_record_sha256", "selection_sha256", "manifest_digest", "child_key"
            )} if block else None
        ),
        "ion_mode": str(project.get("ion_mode") or ""),
        "acquisition_mode": str(project.get("acquisition_mode") or ""),
        "separation": str(project.get("separation") or ""),
        "inputs": inputs,
        "analysis_csv_canonical_sha256": str((manifest.get("analysis_csv") or {}).get("canonical_sha256") or "")
        if isinstance(manifest.get("analysis_csv"), dict) else "",
        "peak_height": {
            key: latest.get(key) for key in ("minimum_peak_height", "diagnostic_peak_count", "threshold_step", "method")
        } if latest else None,
        "msdial_interactive_version": str(manifest.get("msdial_interactive_version") or interactive_version or ""),
    }


def replay_projection_sha256(manifest: Mapping[str, Any], interactive_version: str = "") -> str:
    return sha256_bytes(canonical_json(replay_projection(manifest, interactive_version)))


# --------------------------------------------------------------------------------------------------------------------
# Loading and checking a triage.


def load_triage(path: str | Path) -> tuple[dict[str, Any], str, Path]:
    """(the triage, the sha256 of its bytes as read, its resolved path). Refuses what is not one JSON object."""
    location = Path(str(path)).expanduser()
    try:
        data = location.read_bytes()
    except OSError as error:
        raise CuratedSelectionError(
            CURATED_SELECTION_INVALID, f"The curated selection could not be read: {error}.", path=str(location)
        ) from error
    try:
        triage = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as error:
        raise CuratedSelectionError(
            CURATED_SELECTION_INVALID, f"The curated selection is not JSON: {error}.", path=str(location)
        ) from error
    if not isinstance(triage, dict):
        raise CuratedSelectionError(CURATED_SELECTION_INVALID, "The curated selection is not one JSON object.")
    return triage, sha256_bytes(data), location.resolve()


def decision_record_candidates(triage_path: Path, unit_id: str) -> list[Path]:
    """Where a triage's decision record is looked for when no decision_record_path is given, in order.

    ``<triage file stem>.record.json`` beside the triage: <unit_id>.json has <unit_id>.record.json, a revision
    <unit_id>.r<N>.json its own <unit_id>.r<N>.record.json (doc section 2.1), and a campaign runner's copy
    handoffs/<unit_key>/<unit_id>.json the copy <unit_id>.record.json beside it. Then, for a triage file named
    otherwise, <unit_id>.record.json; never for a revision, whose record is its own.
    """
    location = Path(triage_path)
    candidates = [location.parent / f"{location.stem}.record.json"]
    revision = bool(unit_id) and re.fullmatch(rf"{re.escape(unit_id)}\.r\d+", location.stem) is not None
    if unit_id and location.stem != unit_id and not revision:
        candidates.append(location.parent / f"{unit_id}.record.json")
    return candidates


def _record_input(record_directory: Path, item: Mapping[str, Any], name: str) -> tuple[Path, bytes]:
    """One ``inputs[]`` item of a decision record: its file (relative to the record's directory, as doc section 2.1
    keeps it under ``inputs\\``), read and checked against the sha256 the record names. Raises
    CuratedSelectionError(CURATED_SELECTION_RECORD_MISMATCH) for a file that is missing or holds other bytes."""
    text = str(item.get("path") or "").strip()
    expected = str(item.get("sha256") or "").strip().casefold()
    role = str(item.get("role") or "")
    if not text or not expected:
        raise CuratedSelectionError(
            CURATED_SELECTION_RECORD_MISMATCH,
            f"The decision record {name} binds a {role} input without a path and sha256.",
            role=role,
        )
    path = Path(text)
    location = path if path.is_absolute() else record_directory / path
    try:
        data = location.read_bytes()
    except OSError as error:
        raise CuratedSelectionError(
            CURATED_SELECTION_RECORD_MISMATCH,
            f"The {role} input {text} the decision record {name} binds cannot be read beside it ({error}). Pass "
            "decision_record_path naming the record where its inputs\\ directory is, or copy that directory with it.",
            role=role,
            input_path=text,
        ) from error
    found = sha256_bytes(data)
    if found != expected:
        raise CuratedSelectionError(
            CURATED_SELECTION_RECORD_MISMATCH,
            f"The {role} input {text} hashes to {found}; the decision record {name} names {expected}.",
            role=role,
            input_path=text,
        )
    return location, data


def read_decision_record(
    triage_path: Path,
    triage: Mapping[str, Any],
    triage_sha256: str,
    record_path: str | Path = "",
) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]], Path | None]:
    """(the record's summary, the archive listing it binds, the Catalog handoff input it binds), or (None, {}, None).

    The record (msdial-llm-decision-record.v1) is ``record_path`` where given (it must exist), else the first of
    decision_record_candidates that exists. Its payload.sha256 must be the triage's, else the triage and its record
    describe different decisions (CURATED_SELECTION_RECORD_MISMATCH). Every ``inputs[]`` item with role
    ``archive_listing`` is read beside the record and must hash to the sha256 the record names (doc section 3.1: a
    reader holding a listing whose sha256 differs refuses; a listing it cannot read is refused too, since a selection
    computed without it would differ silently); the listings are merged for selection_canonical. The
    ``catalog_handoff`` input is resolved but not read here (verify_handoff_copy reads it where a copy needs it).

    The summary {name, path, sha256, decision_id, located (explicit or beside_triage), archive_listings [{path,
    sha256, entries}], catalog_handoff_input {path, sha256} or null} is what the provenance records.
    """
    unit_id = str(triage.get("unit_id") or "").strip()
    if str(record_path or "").strip():
        location = Path(str(record_path)).expanduser()
        if not location.is_file():
            raise CuratedSelectionError(
                CURATED_SELECTION_RECORD_MISMATCH, f"decision_record_path {location} is not a file."
            )
        located = "explicit"
    else:
        location = next((path for path in decision_record_candidates(triage_path, unit_id) if path.is_file()), None)
        if location is None:
            return None, {}, None
        located = "beside_triage"
    location = location.resolve()
    data = location.read_bytes()
    try:
        record = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as error:
        raise CuratedSelectionError(
            CURATED_SELECTION_RECORD_MISMATCH, f"The decision record {location.name} is not JSON: {error}."
        ) from error
    if not isinstance(record, dict):
        raise CuratedSelectionError(CURATED_SELECTION_RECORD_MISMATCH, "The decision record is not one JSON object.")
    payload = record.get("payload")
    payload_sha = str((payload or {}).get("sha256") or "").strip().casefold() if isinstance(payload, dict) else ""
    if payload_sha != triage_sha256:
        raise CuratedSelectionError(
            CURATED_SELECTION_RECORD_MISMATCH,
            f"The decision record {location.name} names payload sha256 {payload_sha or '(none)'}, not the triage's "
            f"{triage_sha256}.",
        )
    inputs = [item for item in record.get("inputs") or [] if isinstance(item, dict)]
    listing: dict[str, dict[str, Any]] = {}
    listings = []
    for item in inputs:
        if str(item.get("role") or "") != "archive_listing":
            continue
        _path, content = _record_input(location.parent, item, location.name)
        before = len(listing)
        try:
            parse_archive_listing(content, listing)
        except (UnicodeDecodeError, ValueError) as error:
            raise CuratedSelectionError(
                CURATED_SELECTION_RECORD_MISMATCH, f"The archive listing {item.get('path')} cannot be read: {error}."
            ) from error
        listings.append({"path": str(item["path"]), "sha256": str(item["sha256"]).casefold(),
                         "entries": len(listing) - before})
    handoff_item = next((item for item in inputs if str(item.get("role") or "") == "catalog_handoff"), None)
    handoff_path: Path | None = None
    if handoff_item is not None and str(handoff_item.get("path") or "").strip():
        candidate = Path(str(handoff_item["path"]))
        handoff_path = candidate if candidate.is_absolute() else location.parent / candidate
    summary = {
        "name": location.name,
        "path": str(location),
        "sha256": sha256_bytes(data),
        "decision_id": str(record.get("decision_id") or ""),
        "located": located,
        "archive_listings": listings,
        "catalog_handoff_input": (
            {"path": str(handoff_item.get("path") or ""), "sha256": str(handoff_item.get("sha256") or "").casefold()}
            if handoff_item is not None else None
        ),
    }
    return summary, listing, handoff_path


def decision_record(
    triage_path: Path, triage: Mapping[str, Any], triage_sha256: str, record_path: str | Path = ""
) -> dict[str, Any] | None:
    """read_decision_record's summary alone."""
    return read_decision_record(triage_path, triage, triage_sha256, record_path)[0]


# The keys a campaign runner's copy of a Catalog handoff rewrites (scripts/campaign/ports.py copy_handoff): each points
# at the copy of a sidecar. Everything else in the copy must be the original's.
HANDOFF_COPY_REWRITTEN_KEYS = ("file_manifest_path", "sample_table_path", "analysis_input_manifest_path")

# What of a handoff the Class decides, and so what the triage does not bind (WS0 decision F, 2026-11-17, superseding
# decision A's ratification-only scope). The triage runs before any Class is saved: it reads the Catalog's rows, files
# and listing, never the Class, so the handoff it names normally carries no Class, or an unratified one, and the
# campaign saves and ratifies the Class afterwards. The triage binds the evidence it read; the Class binds through its
# own ratification (the Catalog's, which the runner writes under the campaign approval and checks against the approved
# class digest). So between the triaged handoff and the copy the runner passes, these are left out, exactly as
# msdial_repository_catalog's msdial_catalog_reanalysis_handoff derives them (4ffa0ff):
#
# - "keys": the whole class_proposal object (the proposal, its assignments, contrast, status, ratification and
#   ratified_by).
# - "blocking_reason_codes": the blocking_reasons codes the builder adds from the Class alone (no proposal; a proposal
#   saved for another unit; a proposal not accepted). blocking_reasons is compared with only these codes removed: any
#   other code (technical_metadata:*, analysis_input:*) appearing or disappearing is still a difference.
# - "readiness_keys": next_action and ready_for_download_planning, which the builder derives from blocking_reasons,
#   required_review, analysis_input_issues and whether a proposal exists. They are left out only where the remaining
#   (non-Class) blocking_reasons are equal on both sides; otherwise they are compared like any key.
#
# Everything else is compared: the files, the sample rows, the technical settings, the sidecars' contents, the unit and
# accession, every other top-level key. msdial-public-reanalysis's scripts/campaign/curated.py holds the same value
# (its integration test compares the two).
HANDOFF_CLASS_KEYS: dict[str, tuple[str, ...]] = {
    "keys": ("class_proposal",),
    "blocking_reason_codes": ("class_proposal:missing", "class_proposal:not_accepted", "class_proposal:other_unit"),
    "readiness_keys": ("next_action", "ready_for_download_planning"),
}


def handoff_class_split(blocking_reasons: Any) -> tuple[Any, list[str]]:
    """blocking_reasons without the Class-derived codes (HANDOFF_CLASS_KEYS), and those codes in their order.

    A value that is not a list is returned as it is, with no Class code: it is compared whole."""
    if not isinstance(blocking_reasons, list):
        return blocking_reasons, []
    codes = set(HANDOFF_CLASS_KEYS["blocking_reason_codes"])
    return ([item for item in blocking_reasons if str(item) not in codes],
            [str(item) for item in blocking_reasons if str(item) in codes])


def handoff_differing_keys(original: Mapping[str, Any], copy: Mapping[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """The top-level keys in which ``copy`` is not ``original`` as a triage binds it, and the Class record.

    Every key but HANDOFF_COPY_REWRITTEN_KEYS and HANDOFF_CLASS_KEYS["keys"] is compared as canonical JSON;
    blocking_reasons without the Class-derived codes; the readiness keys only where those remaining blocking_reasons
    differ. The record is {excluded_keys, excluded_blocking_reason_codes, readiness_keys, readiness_left_out,
    original, copy (each {class_proposal, class_blocking_reasons, and the readiness keys' values}), differs (whether
    the two class_proposal values differ)}. Pure; the sidecars are verify_handoff_copy's.
    """
    left_out = set(HANDOFF_COPY_REWRITTEN_KEYS) | set(HANDOFF_CLASS_KEYS["keys"])
    readiness = HANDOFF_CLASS_KEYS["readiness_keys"]
    original_rest, original_codes = handoff_class_split(original.get("blocking_reasons"))
    copy_rest, copy_codes = handoff_class_split(copy.get("blocking_reasons"))
    readiness_left_out = canonical_json(original_rest) == canonical_json(copy_rest)
    differing: list[str] = []
    for key in sorted((set(original) | set(copy)) - left_out):
        if key in readiness and readiness_left_out:
            continue
        if key == "blocking_reasons":
            same = readiness_left_out
        else:
            same = canonical_json(original.get(key)) == canonical_json(copy.get(key))
        if not same:
            differing.append(key)

    def side(handoff: Mapping[str, Any], codes: list[str]) -> dict[str, Any]:
        return {"class_proposal": handoff.get("class_proposal"), "class_blocking_reasons": codes,
                **{key: handoff.get(key) for key in readiness}}

    record = {
        "excluded_keys": list(HANDOFF_CLASS_KEYS["keys"]),
        "excluded_blocking_reason_codes": list(HANDOFF_CLASS_KEYS["blocking_reason_codes"]),
        "readiness_keys": list(readiness),
        "readiness_left_out": readiness_left_out,
        "original": side(original, original_codes),
        "copy": side(copy, copy_codes),
        "differs": canonical_json(original.get("class_proposal")) != canonical_json(copy.get("class_proposal")),
    }
    return differing, record


def verify_handoff_copy(original_path: str | Path, copy: Mapping[str, Any]) -> dict[str, Any]:
    """Bind a rewritten handoff copy to the Catalog original it was copied from. Raises CuratedSelectionError.

    The original is read by path and hashed (its sha256 is what a triage names); the copy must hold the same value
    for every key but HANDOFF_COPY_REWRITTEN_KEYS and what HANDOFF_CLASS_KEYS leaves to the Class (compared as
    canonical JSON; handoff_differing_keys): the sample rows, the files with their URLs, sizes and checksums, the
    technical settings, the unit and accession, every non-Class blocking reason. The Class is not the triage's to bind
    (WS0 decision F): it binds through its own ratification, and the copy's Class is the one the run uses, held by the
    copy's own blocking_reasons as any handoff's is. Both sides' Class are returned, not compared. The sidecars the
    rewritten keys name (the external file manifest, sample table and input manifest a large handoff carries its rows
    in) must exist on both sides and hold the same JSON value: a copy pointing at other rows is no copy. Returns
    {sha256, name, compared_keys, sidecars (each side's sha256), class (handoff_differing_keys's record)}.
    """
    location = Path(str(original_path)).expanduser()
    try:
        data = location.read_bytes()
        original = json.loads(data.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise CuratedSelectionError(
            CURATED_SELECTION_HANDOFF_MISMATCH, f"The Catalog's original handoff could not be read: {error}."
        ) from error
    if not isinstance(original, dict):
        raise CuratedSelectionError(CURATED_SELECTION_HANDOFF_MISMATCH, "The original handoff is not one JSON object.")
    keys = sorted((set(original) | set(copy)) - set(HANDOFF_COPY_REWRITTEN_KEYS) - set(HANDOFF_CLASS_KEYS["keys"]))
    differing, class_record = handoff_differing_keys(original, copy)
    if differing:
        raise CuratedSelectionError(
            CURATED_SELECTION_HANDOFF_MISMATCH,
            f"The handoff passed is not a copy of the Catalog's original {location.name}: it differs in "
            f"{', '.join(differing[:20])}. Only {', '.join(HANDOFF_COPY_REWRITTEN_KEYS)} may be rewritten, and only "
            f"the Class may change ({', '.join(HANDOFF_CLASS_KEYS['keys'])}; the blocking_reasons codes "
            f"{', '.join(HANDOFF_CLASS_KEYS['blocking_reason_codes'])}; "
            f"{' and '.join(HANDOFF_CLASS_KEYS['readiness_keys'])} where no other blocking reason changed).",
            differing_keys=differing[:50],
        )

    def sidecar(value: Any) -> tuple[str | None, bytes | None]:
        text = str(value or "").strip()
        path = Path(text).expanduser() if text else None
        if path is None or not path.is_file():
            return None, None
        content = path.read_bytes()
        try:
            return sha256_bytes(content), canonical_json(json.loads(content.decode("utf-8-sig")))
        except (UnicodeDecodeError, ValueError):
            return sha256_bytes(content), content

    sidecars: dict[str, Any] = {}
    for key in HANDOFF_COPY_REWRITTEN_KEYS:
        if not (original.get(key) or copy.get(key)):
            continue
        original_sha, original_value = sidecar(original.get(key))
        copy_sha, copy_value = sidecar(copy.get(key))
        if original_value is None or copy_value is None or original_value != copy_value:
            raise CuratedSelectionError(
                CURATED_SELECTION_HANDOFF_MISMATCH,
                f"The {key} sidecar of the handoff passed is not the original's: "
                + ("one side's file is missing." if original_value is None or copy_value is None else
                   "their contents differ."),
                sidecar=key,
            )
        sidecars[key] = {"original_sha256": original_sha, "copy_sha256": copy_sha}
    return {"sha256": sha256_bytes(data), "name": location.name, "compared_keys": keys, "sidecars": sidecars,
            "class": class_record}


def campaign_selection(authorization_path: str | Path | None, unit_key: str) -> dict[str, Any] | None:
    """What an approved campaign manifest says of this unit's curated selection, or None without an approval.

    The approval is of a manifest digest; CampaignAuthorization.load already refuses a named manifest that no longer
    hashes to it. Returns {approval_id, manifest_digest, manifest_named, entry (the unit's manifest entry, or None)};
    an approval that names no campaign manifest returns manifest_named false, which apply_curated_selection refuses.
    """
    from .campaign_authorization import load_campaign_authorization

    authorization = load_campaign_authorization(authorization_path)
    if authorization is None:
        return None
    manifest_path = str(authorization.record.get("campaign_manifest_path") or "").strip()
    if not manifest_path:
        # An approval that names no manifest approved no selection: apply_curated_selection refuses the triage rather
        # than let it move bytes under a coverage given by unit id alone (review of 0.5.69).
        return {
            "approval_id": authorization.approval_id,
            "manifest_digest": authorization.manifest_digest,
            "manifest_named": False,
            "entry": None,
        }
    manifest = json.loads(Path(manifest_path).expanduser().read_text(encoding="utf-8-sig"))
    units = manifest.get("units") if isinstance(manifest, dict) else None
    entry = next(
        (
            item for item in units or []
            if isinstance(item, dict) and str(item.get("unit_key") or item.get("unit_id") or "") == unit_key
        ),
        None,
    )
    return {
        "approval_id": authorization.approval_id,
        "manifest_digest": authorization.manifest_digest,
        "manifest_named": True,
        "entry": entry,
    }


def _child(triage: Mapping[str, Any], child_key: str) -> dict[str, Any] | None:
    for child in triage.get("children") or []:
        if isinstance(child, dict) and str(child.get("child_key") or "") == child_key:
            return child
    return None


def _kept_rows(triage: Mapping[str, Any], child: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    rows = [row for row in triage.get("rows") or [] if isinstance(row, dict)]
    if child is None:
        return rows
    members = {_member_key(item) for item in child.get("member_paths") or []}
    return [row for row in rows if _member_key(row.get("member_path")) in members]


def _kept_added_members(triage: Mapping[str, Any], child: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The triage's added members, or the child's where a child is named (by its member_paths, as its rows are)."""
    added = [item for item in triage.get("added_members") or [] if isinstance(item, dict)]
    if child is None:
        return added
    members = {_member_key(item) for item in child.get("member_paths") or []}
    return [item for item in added if _member_key(item.get("member_path")) in members]


def polarity_split_of(triage: Mapping[str, Any]) -> dict[str, str] | None:
    """The triage's polarity_split, validated, or None where it has none. Raises CURATED_SELECTION_POLARITY_SPLIT_INVALID.

    The contract (0.5.70): {"ion_mode": "positive" | "negative", "reason": "polarity_switching", "parent_unit_id": the
    Catalog unit id (the triage's unit_id), "sibling_ion_mode": the opposite of ion_mode}, and unit_technical.ion_mode
    equal to ion_mode. Values are compared case-insensitively and returned lower-case.
    """
    if "polarity_split" not in triage or triage.get("polarity_split") is None:
        return None
    value = triage.get("polarity_split")
    unit_id = str(triage.get("unit_id") or "").strip()
    if not isinstance(value, dict):
        raise CuratedSelectionError(
            CURATED_SELECTION_POLARITY_SPLIT_INVALID,
            f"The curated selection of {unit_id} has a polarity_split that is not an object.",
        )
    ion = str(value.get("ion_mode") or "").strip().casefold()
    sibling = str(value.get("sibling_ion_mode") or "").strip().casefold()
    reason = str(value.get("reason") or "").strip().casefold()
    parent = str(value.get("parent_unit_id") or "").strip()
    technical = triage.get("unit_technical") if isinstance(triage.get("unit_technical"), dict) else {}
    unit_ion = str(technical.get("ion_mode") or "").strip().casefold()
    problems = []
    if ion not in _OPPOSITE_POLARITY:
        problems.append(f"ion_mode {value.get('ion_mode')!r} is not positive or negative")
    elif sibling != _OPPOSITE_POLARITY[ion]:
        problems.append(f"sibling_ion_mode {value.get('sibling_ion_mode')!r} is not the opposite of {ion}")
    if reason not in POLARITY_SPLIT_REASONS:
        problems.append(f"reason {value.get('reason')!r} is not one of {list(POLARITY_SPLIT_REASONS)}")
    if not parent or parent != unit_id:
        problems.append(f"parent_unit_id {value.get('parent_unit_id')!r} is not the triage's unit {unit_id!r}")
    if ion in _OPPOSITE_POLARITY and unit_ion != ion:
        problems.append(f"unit_technical.ion_mode {technical.get('ion_mode')!r} is not the split's {ion}")
    if problems:
        raise CuratedSelectionError(
            CURATED_SELECTION_POLARITY_SPLIT_INVALID,
            f"The polarity_split of the curated selection of {unit_id} does not hold: " + "; ".join(problems) + ".",
            problems=problems,
        )
    return {"ion_mode": ion, "sibling_ion_mode": sibling, "reason": reason, "parent_unit_id": parent}


def run_key(unit_id: str, triage: Mapping[str, Any], child_key: str = "") -> str:
    """The campaign unit key of a run held to this triage: <unit_id>-<child_key> for a child, <unit_id>-pos or
    <unit_id>-neg for a polarity_split child (the siblings over one set of files must not share a campaign entry, a
    store claim or a batch row), else the Catalog unit id. Raises on a malformed polarity_split."""
    unit_id = str(unit_id or "").strip()
    child_key = str(child_key or "").strip()
    if child_key:
        return f"{unit_id}-{child_key}"
    split = polarity_split_of(triage)
    return f"{unit_id}-{_POLARITY_SPLIT_SUFFIX[split['ion_mode']]}" if split else unit_id


def unit_key(triage: Mapping[str, Any], child_key: str = "") -> str:
    return run_key(str(triage.get("unit_id") or ""), triage, child_key)


def _admitted_by_member(block: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """{member key: what the selection said of it} for every input it admitted: its kept rows, and the added members
    a selection without kept rows runs on (added_members_admitted, row_index null)."""
    found = {_member_key(row.get("member_path")): row for row in block.get("added_members_admitted") or []
             if isinstance(row, dict)}
    found.update({_member_key(row.get("member_path")): row for row in block.get("rows_kept") or []
                  if isinstance(row, dict)})
    return found


def _added_sample_ids(members: list[str]) -> list[str]:
    """A sample id for each added member: its stem, or its path less the suffix where two members share a stem."""
    stems = [PurePosixPath(member).stem for member in members]
    counts: dict[str, int] = {}
    for stem in stems:
        counts[stem.casefold()] = counts.get(stem.casefold(), 0) + 1
    return [
        stem if counts[stem.casefold()] == 1 else str(PurePosixPath(member).with_suffix(""))
        for stem, member in zip(stems, members)
    ]


def added_class_proposal(
    proposal: Mapping[str, Any] | None, sample_ids: list[str], key: str, triage_sha256: str
) -> dict[str, Any] | None:
    """The run's Class for a selection that keeps no Catalog row and runs on added members: every member takes the
    run's one label, the abstention's (``All``) or the one label the proposal assigns. A proposal of more than one label
    has a contrast none of whose sides keeps a sample here, and is refused (CURATED_SELECTION_CLASS_CONTRAST_LOST)."""
    if not proposal:
        return None
    from copy import deepcopy

    cut = deepcopy(dict(proposal))
    contrast = cut.get("contrast_definition") if isinstance(cut.get("contrast_definition"), dict) else {}
    every = [item for item in cut.get("assignments") or [] if isinstance(item, dict)]
    labels = sorted({str(item.get("class_label") or "").strip() for item in every} - {""})
    if str(contrast.get("kind") or "") == "abstention":
        label = str(contrast.get("class_label") or "").strip() or "All"
    elif len(labels) == 1:
        label = labels[0]
    else:
        raise CuratedSelectionError(
            CURATED_SELECTION_CLASS_CONTRAST_LOST,
            f"The curated selection of {key} keeps no Catalog row and runs on archive members no row names, so the "
            f"approved Class proposal's {len(labels)} levels keep no sample on any side. A Class decided over the "
            "added members is needed.",
            levels_before=labels,
            levels_after=[],
        )
    cut["assignments"] = [{"sample_id": sample_id, "class_label": label} for sample_id in sample_ids]
    cut["curated_from"] = {
        "analysis_unit_key": key,
        "triage_sha256": triage_sha256,
        "assignments_kept": 0,
        "assignments_in_proposal": len(every),
        "added_members_assigned": len(sample_ids),
        "class_label": label,
        "note": "The curated selection keeps no Catalog row; its samples are archive members no row names "
        "(added_members), each in the run's one Class. No sample was regrouped.",
    }
    warnings = list(cut.get("warnings") or [])
    warnings.append(
        f"This run holds none of the {len(every)} samples the proposal assigned: the curated selection of {key} runs "
        f"on {len(sample_ids)} archive member(s) no Catalog row names, each in Class {label}."
    )
    cut["warnings"] = warnings
    return cut


def _contrast_groups(proposal: Mapping[str, Any]) -> list[str]:
    contrast = proposal.get("contrast_definition") if isinstance(proposal.get("contrast_definition"), dict) else {}
    named: list[str] = []
    for key in ("groups", "levels", "class_labels"):
        value = contrast.get(key)
        if isinstance(value, list):
            named.extend(str(item) for item in value if str(item).strip())
    for key in ("case", "control", "numerator", "denominator", "reference"):
        if str(contrast.get(key) or "").strip():
            named.append(str(contrast[key]))
    return list(dict.fromkeys(named))


def _biological(label: str) -> bool:
    from .repository_analysis_rows import _coverage_file_type

    return bool(label) and _coverage_file_type(label) == "Sample"


def cut_class_proposal(
    proposal: Mapping[str, Any] | None, kept_sample_ids: set[str], key: str, triage_sha256: str
) -> dict[str, Any] | None:
    """The approved Class proposal restricted to the kept rows' samples, saying so; refuses a contrast that lost a side.

    A proposal whose contrast_definition is an abstention carries no contrast to lose. Otherwise the contrast's named
    groups (groups, case/control and the like) must each keep a sample, or, where it names none, the biological Class
    labels (not blank or QC by the coverage gate's reading) must still be two or more where they were.
    """
    if not proposal:
        return None
    from copy import deepcopy

    cut = deepcopy(dict(proposal))
    every = [item for item in cut.get("assignments") or [] if isinstance(item, dict)]
    kept = [item for item in every if str(item.get("sample_id") or "").strip() in kept_sample_ids]
    contrast = cut.get("contrast_definition") if isinstance(cut.get("contrast_definition"), dict) else {}
    if every and str(contrast.get("kind") or "") != "abstention":
        before = {str(item.get("class_label") or "") for item in every}
        after = {str(item.get("class_label") or "") for item in kept}
        named = _contrast_groups(cut)
        lost = [group for group in named if group in before and group not in after]
        biological_before = {label for label in before if _biological(label)}
        biological_after = {label for label in after if _biological(label)}
        if lost or (not named and len(biological_before) >= 2 and len(biological_after) < 2):
            raise CuratedSelectionError(
                CURATED_SELECTION_CLASS_CONTRAST_LOST,
                f"The approved Class proposal's contrast loses a side on the triaged rows of {key}: "
                + (f"group(s) {', '.join(sorted(lost))} keep no sample" if lost else
                   f"{len(biological_after)} biological Class level(s) remain of {len(biological_before)}")
                + ". A Class decided over the kept rows is needed (Catalog item: Class over a row subset).",
                lost_groups=sorted(lost),
                levels_before=sorted(before),
                levels_after=sorted(after),
            )
    cut["assignments"] = kept
    cut["curated_from"] = {
        "analysis_unit_key": key,
        "triage_sha256": triage_sha256,
        "assignments_kept": len(kept),
        "assignments_in_proposal": len(every),
        "note": "The accepted proposal, restricted to the samples of the curated selection's kept rows. "
        "No sample was regrouped.",
    }
    if len(kept) != len(every):
        warnings = list(cut.get("warnings") or [])
        warnings.append(
            f"This run holds {len(kept)} of the {len(every)} samples the proposal assigned: the curated selection of "
            f"{key} keeps only its own rows. The rationale describes the whole Catalog unit."
        )
        cut["warnings"] = warnings
    return cut


def apply_curated_selection(
    *,
    handoff: Mapping[str, Any],
    samples: list[dict[str, Any]],
    triage: Mapping[str, Any],
    triage_sha256: str,
    triage_path: str | Path,
    handoff_file_sha256: str = "",
    original_handoff_sha256: str = "",
    original_handoff: Mapping[str, Any] | None = None,
    child_key: str = "",
    validator: Mapping[str, Any] | None = None,
    campaign: Mapping[str, Any] | None = None,
    record: Mapping[str, Any] | None = None,
    listing: Mapping[str, Mapping[str, Any]] | None = None,
    interactive_version: str = "",
) -> dict[str, Any]:
    """Hold a handoff's unit to its triage. Returns what the projection uses; raises CuratedSelectionError.

    The handoff is tied to the one the triage names (``catalog_handoff_sha256``) in one of two ways, never by
    assertion alone: the handoff file passed is it (``handoff_file_sha256`` equals it), or ``original_handoff`` is
    verify_handoff_copy's reading of the triaged original (by original_handoff_path, or the decision record's
    catalog_handoff input) against the copy passed. An ``original_handoff_sha256`` asserted for a copy whose bytes
    differ, with no original to compare it with, is refused (CURATED_SELECTION_HANDOFF_MISMATCH): a handoff whose
    bytes changed is a hold, not a silent pass.

    ``listing`` is the archive listing the decision record binds (read_decision_record), from which
    ``selection_sha256`` takes crc32 and size. Where an approved campaign manifest names a selection_sha256 for the
    unit, it must equal this one (CURATED_SELECTION_SELECTION_MISMATCH).

    Returns {key, samples (the kept rows, in triage order of row_index), analysis_inputs (one per kept row, by member
    path), ion_mode, acquisition_mode, class_proposal (cut), provenance}.
    """
    from . import __version__

    unit_id = str(handoff.get("analysis_unit_id") or "").strip()
    accession = str(handoff.get("accession") or "").strip()
    schema = str(triage.get("schema") or "")
    child_key = str(child_key or "").strip()
    key = f"{unit_id}-{child_key}" if child_key else unit_id
    if schema not in TRIAGE_SCHEMAS:
        raise CuratedSelectionError(
            CURATED_SELECTION_INVALID, f"The curated selection's schema is {schema!r}, not one of {list(TRIAGE_SCHEMAS)}."
        )
    if str(triage.get("unit_id") or "") != unit_id or str(triage.get("accession") or "") != accession:
        raise CuratedSelectionError(
            CURATED_SELECTION_HANDOFF_MISMATCH,
            f"The curated selection is for {triage.get('accession')}/{triage.get('unit_id')}, not the handoff's "
            f"{accession}/{unit_id}.",
        )
    split = polarity_split_of(triage)
    key = run_key(unit_id, triage, child_key)
    status = str(triage.get("status") or "")
    if status != "ready":
        raise CuratedSelectionError(
            CURATED_SELECTION_NOT_READY,
            f"The curated selection of {unit_id} has status {status!r}; only a ready triage goes to download "
            "(needs_person, abstained and out_of_scope wait for a person).",
            status=status,
        )
    declared_handoff = str(triage.get("catalog_handoff_sha256") or "").strip().casefold()
    asserted = str(original_handoff_sha256 or "").strip().casefold()
    verified_original = str((original_handoff or {}).get("sha256") or "").casefold()
    if verified_original and asserted and asserted != verified_original:
        raise CuratedSelectionError(
            CURATED_SELECTION_HANDOFF_MISMATCH,
            f"original_handoff_sha256 {asserted} is not the sha256 of the original handoff read ({verified_original}).",
        )
    copy_sha = str(handoff_file_sha256 or "").strip().casefold()
    if asserted and not verified_original and asserted != copy_sha:
        # WS0 decision 7: the runner passes the triaged handoff's sha256 with its run-time copy, and the copy is
        # verified against the triaged bytes; an assertion that nothing here can check is no tie (review of 0.5.69).
        raise CuratedSelectionError(
            CURATED_SELECTION_HANDOFF_MISMATCH,
            f"The handoff passed ({copy_sha or 'inline, no file'}) is not the triaged handoff {asserted} byte for "
            "byte, and no original was found to compare it with: pass original_handoff_path, or a decision record "
            "whose catalog_handoff input holds the triaged bytes.",
            original_handoff_sha256=asserted,
            handoff_sha256=copy_sha,
        )
    reference = verified_original or asserted or copy_sha
    # How the handoff that drives the plan is tied to the one the triage names (review of 0.5.69): the file passed IS
    # it; or the original was read (by path, or from the decision record's inputs) and the copy holds its every value
    # but the rewritten sidecar paths.
    verification = "original_read_and_copy_compared" if verified_original else "handoff_file"
    if not reference:
        raise CuratedSelectionError(
            CURATED_SELECTION_HANDOFF_MISMATCH,
            "The Catalog handoff the triage was made from cannot be verified: pass the handoff by path, or the sha256 "
            "of the Catalog's original handoff (original_handoff_sha256).",
        )
    if declared_handoff != reference:
        raise CuratedSelectionError(
            CURATED_SELECTION_HANDOFF_MISMATCH,
            f"The triage was made from Catalog handoff {declared_handoff or '(none)'}, not {reference}.",
            triage_catalog_handoff_sha256=declared_handoff,
            handoff_sha256=reference,
        )
    row_count = triage.get("catalog_row_count")
    if not isinstance(row_count, int) or row_count != len(samples):
        raise CuratedSelectionError(
            CURATED_SELECTION_HANDOFF_MISMATCH,
            f"The triage counts {row_count} Catalog rows; the handoff has {len(samples)}.",
        )
    for row in [*(triage.get("rows") or []), *(triage.get("excluded_rows") or [])]:
        index = row.get("row_index") if isinstance(row, dict) else None
        if not isinstance(index, int) or not 0 <= index < len(samples):
            raise CuratedSelectionError(
                CURATED_SELECTION_HANDOFF_MISMATCH, f"The triage names row index {index!r}, outside the handoff's rows."
            )
        named = str(row.get("sample_id") or "").strip()
        if named and named != str(samples[index].get("sample_id") or "").strip():
            raise CuratedSelectionError(
                CURATED_SELECTION_HANDOFF_MISMATCH,
                f"Triage row {index} names sample {named!r}; the handoff's row {index} is "
                f"{samples[index].get('sample_id')!r}.",
            )

    technical = triage.get("unit_technical") if isinstance(triage.get("unit_technical"), dict) else {}
    unit_ion = str(technical.get("ion_mode") or "").strip().casefold()
    children = [item for item in triage.get("children") or [] if isinstance(item, dict)]
    child = None
    if child_key:
        child = _child(triage, child_key)
        if child is None:
            raise CuratedSelectionError(
                CURATED_SELECTION_CHILD_UNKNOWN,
                f"The curated selection of {unit_id} has no child {child_key!r}; its children are "
                f"{[str(item.get('child_key')) for item in children] or 'none'}.",
            )
    ion = str((child or {}).get("ion_mode") or unit_ion).strip().casefold()
    if split is not None and ion != split["ion_mode"]:
        raise CuratedSelectionError(
            CURATED_SELECTION_POLARITY_SPLIT_INVALID,
            f"The curated selection of {key} is the {split['ion_mode']} polarity_split child, and its child "
            f"{child_key!r} states ion mode {ion or 'none'}.",
            problems=[f"child ion_mode {ion!r} is not the split's {split['ion_mode']}"],
        )
    if ion in _MIXED_POLARITY:
        raise CuratedSelectionError(
            CURATED_SELECTION_MIXED_POLARITY,
            f"The curated selection of {unit_id} states ion mode {ion or 'none'}; MS-DIAL runs one polarity per "
            "analysis, so a single-polarity child must be named"
            + (f" (children: {', '.join(str(item.get('child_key')) for item in children)})." if children else
               "; the triage has none."),
            ion_mode=ion,
        )
    kept = _kept_rows(triage, child)
    # ADDED MEMBERS ONLY WHERE NO CATALOG ROW IS KEPT (0.5.70). Beside kept rows they stay recorded and not admitted, as
    # in 0.5.69; a selection whose every Catalog row is excluded (MS1-only samples beside pooled DDA runs that carry the
    # MS/MS) runs on them.
    added = [] if kept else _kept_added_members(triage, child)
    if not kept and not added:
        raise CuratedSelectionError(
            CURATED_SELECTION_EMPTY, f"The curated selection of {key} keeps no Catalog row and no archive member."
        )
    unresolved = [row["row_index"] for row in kept if not str(row.get("member_path") or "").strip()]
    unresolved_added = [index for index, item in enumerate(added) if not str(item.get("member_path") or "").strip()]
    if unresolved or unresolved_added:
        raise CuratedSelectionError(
            CURATED_SELECTION_MEMBER_UNRESOLVED,
            f"{len(unresolved) + len(unresolved_added)} kept item(s) of {key} name no archive member "
            f"({(unresolved or unresolved_added)[:10]}); an input is held to its member by path, never by name.",
            row_indices=unresolved[:50],
            **({"added_member_positions": unresolved_added[:50]} if unresolved_added else {}),
        )
    # A polarity_split child's files hold both polarities, so a row may state switching (or both) as well as its
    # polarity; a row stating the other polarity alone is still refused.
    allowed = {ion} | ({"both", "switching"} if split is not None else set())
    row_polarities = {str(row.get("ion_mode") or "").casefold() for row in [*kept, *added]} - {"unknown", ""}
    if row_polarities - allowed or (split is None and row_polarities & {"both", "switching"}):
        raise CuratedSelectionError(
            CURATED_SELECTION_MIXED_POLARITY,
            f"The kept {'rows' if kept else 'members'} of {key} state polarities {sorted(row_polarities)} in a {ion} "
            "unit.",
            row_polarities=sorted(row_polarities),
        )

    approved = False
    approval: dict[str, Any] = {}
    if campaign is not None:
        if campaign.get("manifest_named") is False:
            raise CuratedSelectionError(
                CURATED_SELECTION_NOT_APPROVED,
                f"The campaign approval {campaign.get('approval_id') or ''} names no campaign manifest, so no curated "
                f"selection of {key} was approved under it; a triage moves bytes under a campaign only where the "
                "approved manifest names its sha256.",
                approval_id=str(campaign.get("approval_id") or ""),
            )
        entry = campaign.get("entry")
        selection = entry.get("curated_selection") if isinstance(entry, dict) else None
        approved_sha = ""
        if isinstance(selection, dict):
            approved_sha = str(selection.get("triage_sha256") or selection.get("sha256") or "").strip().casefold()
        if entry is None or approved_sha != triage_sha256:
            raise CuratedSelectionError(
                CURATED_SELECTION_NOT_APPROVED,
                f"The approved campaign manifest ({campaign.get('manifest_digest')}) "
                + ("does not name unit " + key if entry is None else
                   f"names triage {approved_sha or '(none)'} for {key}, not {triage_sha256}")
                + ".",
                manifest_digest=str(campaign.get("manifest_digest") or ""),
            )
        approved_record = str((selection or {}).get("triage_record_sha256") or "").strip().casefold()
        if approved_record and approved_record != str((record or {}).get("sha256") or ""):
            raise CuratedSelectionError(
                CURATED_SELECTION_RECORD_MISMATCH,
                f"The approved manifest names decision record {approved_record} for {key}; the record beside the "
                f"triage is {(record or {}).get('sha256') or 'absent'}.",
            )
        approved = True
        approval = {
            "approval_id": str(campaign.get("approval_id") or ""),
            "manifest_digest": str(campaign.get("manifest_digest") or ""),
            "manifest_selection_sha256": str((selection or {}).get("selection_sha256") or ""),
        }

    selection_hash = selection_sha256(triage, listing or {}, child)
    planned_selection = str(approval.get("manifest_selection_sha256") or "").strip().casefold()
    if planned_selection and planned_selection != selection_hash:
        raise CuratedSelectionError(
            CURATED_SELECTION_SELECTION_MISMATCH,
            f"The approved manifest names selection_sha256 {planned_selection} for {key}; this triage and the archive "
            f"listing its decision record binds give {selection_hash} ({SELECTION_CANONICAL_FORM}).",
            manifest_selection_sha256=planned_selection,
            selection_sha256=selection_hash,
        )

    acquisition = str((child or {}).get("acquisition_mode") or technical.get("acquisition_mode") or "")
    kept_samples: list[dict[str, Any]] = []
    analysis_inputs: list[dict[str, Any]] = []
    rows_kept: list[dict[str, Any]] = []
    for row in sorted(kept, key=lambda item: int(item["row_index"])):
        index = int(row["row_index"])
        sample = dict(samples[index])
        kept_samples.append(sample)
        member = str(row["member_path"]).replace("\\", "/").strip("/")
        suffix = PurePosixPath(member).suffix.casefold()
        analysis_inputs.append(
            {
                "path": member,
                "kind": "file",
                "sample_id": str(sample.get("sample_id") or ""),
                "archive": str(row.get("archive") or ""),
                "requires_conversion": suffix == ".mzxml",
                "conversion_target": "mzML" if suffix == ".mzxml" else "",
                "selected_by": "curated_selection",
                "triage_row_index": index,
            }
        )
        rows_kept.append(
            {
                "row_index": index,
                "sample_id": str(sample.get("sample_id") or ""),
                "raw_file": str(row.get("raw_file") or ""),
                "member_path": member,
                "archive": str(row.get("archive") or ""),
                "role": str(row.get("role") or ""),
                "ion_mode": str((child or {}).get("ion_mode") or row.get("ion_mode") or ""),
                "acquisition_mode": str((child or {}).get("acquisition_mode") or row.get("acquisition_mode") or ""),
                "chromatography": str(row.get("chromatography") or ""),
                "basis": str(row.get("basis") or ""),
                **({"confidence": row["confidence"]} if row.get("confidence") is not None else {}),
                **({"rationale": row["rationale"]} if row.get("rationale") else {}),
            }
        )
    kept_indices = {row["row_index"] for row in rows_kept}
    rows_dropped: list[dict[str, Any]] = [
        {
            "row_index": int(row["row_index"]),
            "sample_id": str(row.get("sample_id") or ""),
            "reason": str(row.get("reason") or ""),
            "belongs_to_unit": row.get("belongs_to_unit"),
            "detail": str(row.get("detail") or ""),
        }
        for row in triage.get("excluded_rows") or []
        if isinstance(row, dict)
    ]
    if child is not None:
        member_child = {
            _member_key(path): str(item.get("child_key") or "")
            for item in children
            for path in item.get("member_paths") or []
        }
        for row in triage.get("rows") or []:
            if isinstance(row, dict) and int(row["row_index"]) not in kept_indices:
                other = member_child.get(_member_key(row.get("member_path")), "")
                rows_dropped.append(
                    {
                        "row_index": int(row["row_index"]),
                        "sample_id": str(row.get("sample_id") or ""),
                        "reason": "other_child" if other else "not_in_child",
                        "belongs_to_unit": f"{unit_id}-{other}" if other else None,
                        "detail": f"Kept by the unit, in its {other} child." if other else "In no child of the unit.",
                    }
                )
    rows_dropped.sort(key=lambda item: item["row_index"])
    # The added members a selection without kept rows runs on: one input and one sample row each, in member order (the
    # analytical order is then the names' or the headers', as for any row that declares none), typed by role.
    added_admitted: list[dict[str, Any]] = []
    ordered = sorted(added, key=lambda item: _member_key(item.get("member_path")))
    members = [str(item["member_path"]).replace("\\", "/").strip("/") for item in ordered]
    common = {
        name: value
        for name, value in dict(handoff.get("unit_attributes") or {}).items()
        if not any(token in str(name).casefold() for token in ("type", "class", "group", "sample"))
    }
    for item, member, sample_id in zip(ordered, members, _added_sample_ids(members)):
        role = str(item.get("role") or "").strip().casefold()
        file_type = _ROLE_FILE_TYPE.get(role, "")
        kept_samples.append(
            {
                "sample_id": sample_id,
                "raw_file": PurePosixPath(member).name,
                "values": {
                    **common,
                    **({"sample_type": file_type} if file_type else {}),
                    "triage_role": role,
                    "triage_added_member": member,
                },
            }
        )
        suffix = PurePosixPath(member).suffix.casefold()
        analysis_inputs.append(
            {
                "path": member,
                "kind": "file",
                "sample_id": sample_id,
                "archive": str(item.get("archive") or ""),
                "requires_conversion": suffix == ".mzxml",
                "conversion_target": "mzML" if suffix == ".mzxml" else "",
                "selected_by": "curated_selection",
                "triage_row_index": None,
                "triage_added_member": True,
            }
        )
        added_admitted.append(
            {
                "row_index": None,
                "added_member": True,
                "sample_id": sample_id,
                "member_path": member,
                "archive": str(item.get("archive") or ""),
                "role": role,
                "file_type": file_type or None,
                "ion_mode": str((child or {}).get("ion_mode") or item.get("ion_mode") or ""),
                "acquisition_mode": str((child or {}).get("acquisition_mode") or item.get("acquisition_mode") or ""),
                "chromatography": str(item.get("chromatography") or ""),
                "basis": str(item.get("basis") or ""),
                **({"confidence": item["confidence"]} if item.get("confidence") is not None else {}),
                **({"rationale": item["rationale"]} if item.get("rationale") else {}),
            }
        )
    admitted_keys = {_member_key(item["member_path"]) for item in added_admitted}
    kept_ids = {str(sample.get("sample_id") or "").strip() for sample in kept_samples}
    if kept:
        proposal = cut_class_proposal(handoff.get("class_proposal"), kept_ids, key, triage_sha256)
    else:
        proposal = added_class_proposal(
            handoff.get("class_proposal"), [item["sample_id"] for item in added_admitted], key, triage_sha256
        )
    declared_ion = str((handoff.get("technical_settings") or {}).get("ion_mode") or "")
    decided_by = triage.get("decided_by") if isinstance(triage.get("decided_by"), dict) else {}
    provenance: dict[str, Any] = {
        "schema": PROVENANCE_SCHEMA,
        "triage_schema": schema,
        "path": str(triage_path),
        "sha256": triage_sha256,
        "triage_sha256": triage_sha256,
        "triage_record_sha256": str((record or {}).get("sha256") or ""),
        "triage_record": dict(record) if record else None,
        "catalog_handoff_sha256": declared_handoff,
        "catalog_handoff_verification": verification,
        **({"original_handoff": dict(original_handoff)} if original_handoff else {}),
        "original_handoff_sha256": verified_original or asserted,
        "copy_handoff_sha256": str(handoff_file_sha256 or "").casefold(),
        "analysis_unit_key": key,
        "catalog_analysis_unit_id": unit_id,
        "child_key": child_key or None,
        "phase": str(triage.get("phase") or ""),
        "decided_by": {name: str(decided_by.get(name) or "") for name in ("agent", "model", "skill", "skill_version")},
        "decided_at": str(triage.get("decided_at") or ""),
        "purpose": triage.get("purpose"),
        "unit_technical": dict(technical),
        "applied_technical": {"ion_mode": ion, "acquisition_mode": acquisition},
        "validator": dict(validator) if validator else None,
        **approval,
        "selection_sha256": selection_hash,
        "selection_canonical_form": SELECTION_CANONICAL_FORM,
        "selection_listing_entries": len(listing or {}),
        "selection_sha256_matches_manifest": (planned_selection == selection_hash) if planned_selection else None,
        "rows_kept": rows_kept,
        "rows_dropped": rows_dropped,
        "rows_kept_count": len(rows_kept),
        "rows_dropped_count": len(rows_dropped),
        # rows: the inputs are the kept Catalog rows' members; added_members_only: no Catalog row is kept, and the
        # inputs are the triage's added members (0.5.70).
        "admitted_from": "rows" if kept else "added_members_only",
        "added_members_admitted": added_admitted,
        "added_members_admitted_count": len(added_admitted),
        "added_members_not_admitted": [
            {
                "member_path": str(item.get("member_path") or ""),
                "archive": str(item.get("archive") or ""),
                "role": str(item.get("role") or ""),
            }
            for item in triage.get("added_members") or []
            if isinstance(item, dict) and _member_key(item.get("member_path")) not in admitted_keys
        ],
        # A polarity_split child (0.5.70): the run's ion mode is the child's, whatever the Catalog handoff declared
        # (Positive, Negative, Mixed or Both), and both are recorded.
        "polarity_split": (
            {**split, "run_ion_mode": _POLARITY[ion], "handoff_declared_ion_mode": declared_ion}
            if split is not None else None
        ),
        "flags": [dict(item) for item in triage.get("flags") or [] if isinstance(item, dict)],
        **({"rationale": triage["rationale"]} if triage.get("rationale") else {}),
        "standing": "accepted_decision" if approved else "model_inference",
        "applied_by": {"msdial_interactive_version": interactive_version or __version__},
    }
    return {
        "key": key,
        "samples": kept_samples,
        "analysis_inputs": analysis_inputs,
        "ion_mode": _POLARITY[ion],
        "acquisition_mode": _TRIAGE_ACQUISITION.get(acquisition.upper(), ""),
        "class_proposal": proposal,
        "provenance": provenance,
    }


# --------------------------------------------------------------------------------------------------------------------
# Matching inputs on disk to kept rows.


def _relative(path_text: str, data_root: str) -> str:
    path = str(path_text or "").replace("\\", "/")
    root = str(data_root or "").replace("\\", "/").rstrip("/")
    if root and path.casefold().startswith(root.casefold() + "/"):
        path = path[len(root) + 1:]
    return path.strip("/")


def member_for(path_text: str, data_root: str, members: Iterable[str]) -> str:
    """The kept member path an input on disk is: its path under the data root, or that path less leading folders (the
    folder an archive unpacked into), ending on a path boundary with the member. The longest member wins; '' for none.
    """
    relative = _member_key(_relative(path_text, data_root))
    best = ""
    for member in members:
        key = _member_key(member)
        if key and (relative == key or relative.endswith("/" + key)) and len(key) > len(best):
            best = key
    return best


def curated_block(manifest: Mapping[str, Any]) -> dict[str, Any] | None:
    project = manifest.get("project") if isinstance(manifest.get("project"), dict) else {}
    metadata = project.get("repository_metadata") if isinstance(project.get("repository_metadata"), dict) else {}
    block = metadata.get("curated_selection")
    return block if isinstance(block, dict) else None


def tag_curated_lineage(table: dict[str, Any], project: Any, data_root: Path) -> None:
    """Mark each lineage row the curated selection admitted: selected_by, triage_row_index and the member it is."""
    metadata = getattr(project, "repository_metadata", None)
    block = metadata.get("curated_selection") if isinstance(metadata, dict) else None
    if not isinstance(block, dict):
        return
    by_member = _admitted_by_member(block)
    for row in [*(table.get("rows") or []), *(table.get("excluded") or [])]:
        if not isinstance(row, dict):
            continue
        member = member_for(str(row.get("path") or ""), str(data_root), by_member)
        if not member:
            conversion = (row.get("source") or {}).get("conversion") if isinstance(row.get("source"), dict) else None
            source = str((conversion or {}).get("path") or "") if isinstance(conversion, dict) else ""
            member = member_for(source, str(data_root), by_member) if source else ""
        if member:
            kept = by_member[member]
            row["selected_by"] = "curated_selection"
            row["triage_row_index"] = kept.get("row_index")
            row["triage_member_path"] = kept.get("member_path")
            if kept.get("added_member"):
                row["triage_added_member"] = True
        else:
            row["selected_by"] = "curated_selection_unmatched"


# --------------------------------------------------------------------------------------------------------------------
# Header reconciliation.


def _polarity_comparable(value: Any) -> bool:
    return str(value or "").casefold() in _POLARITY


def _acquisition_comparable(value: Any) -> bool:
    return str(value or "").upper() in {"DDA", "DIA", "SWATH", "AIF", "MS1"}


def _triage_value(row_value: Any, unit_value: Any, comparable: Any) -> tuple[str, str]:
    """(the value the triage states for an input, and whether it is the row's or the unit's): the row's where it can
    be compared, else the unit's as applied, else the row's as it is (not compared)."""
    if comparable(row_value):
        return str(row_value), "row"
    if comparable(unit_value):
        return str(unit_value), "unit"
    return str(row_value or ""), "row"


def _raw_directory(manifest: Mapping[str, Any]) -> str:
    """The unit's raw directory: the manifest's raw_directory, else the data root's parent."""
    raw = str(manifest.get("raw_directory") or "").replace("\\", "/").rstrip("/")
    if raw:
        return raw
    data_root = str(manifest.get("input_directory") or "").replace("\\", "/").rstrip("/")
    return data_root.rsplit("/", 1)[0] if "/" in data_root else ""


def _portable_path(path_text: str, data_root: str, raw_directory: str) -> str:
    """A path as it reads on any machine: under the data root, relative to it; else under the unit's raw directory,
    relative to that and prefixed raw:/ (raw:/converted/POS/S1.mzML); else <external>/<name>. Never absolute."""
    path = str(path_text or "").replace("\\", "/")
    root = str(data_root or "").replace("\\", "/").rstrip("/")
    if root and path.casefold().startswith(root.casefold() + "/"):
        return path[len(root) + 1:].strip("/")
    raw = str(raw_directory or "").replace("\\", "/").rstrip("/")
    if raw and path.casefold().startswith(raw.casefold() + "/"):
        return "raw:/" + path[len(raw) + 1:].strip("/")
    if _ABSOLUTE.match(path):
        return "<external>/" + path.rsplit("/", 1)[-1]
    return path.strip("/")


def _conversion_sources(manifest: Mapping[str, Any]) -> dict[str, str]:
    """{converted input path (file key): its source's path}, from the lineage rows and the recorded conversions."""
    found: dict[str, str] = {}
    lineage = manifest.get("input_lineage") if isinstance(manifest.get("input_lineage"), dict) else {}
    for row in [*(lineage.get("rows") or []), *(lineage.get("excluded") or [])]:
        if not isinstance(row, dict):
            continue
        source = row.get("source") if isinstance(row.get("source"), dict) else {}
        conversion = source.get("conversion") if isinstance(source.get("conversion"), dict) else {}
        origin = str(conversion.get("path") or "")
        if origin and str(row.get("path") or "").strip():
            found[_file_key(str(row["path"]))] = origin
    block = manifest.get("input_conversions") if isinstance(manifest.get("input_conversions"), dict) else {}
    for record in block.get("records") or []:
        if not isinstance(record, dict) or record.get("status") != "converted":
            continue
        target = str((record.get("output") or {}).get("path") or "") if isinstance(record.get("output"), dict) else ""
        origin = str((record.get("source") or {}).get("path") or "") if isinstance(record.get("source"), dict) else ""
        if target and origin:
            found.setdefault(_file_key(target), origin)
    return found


def _acquisition_result(triage_value: str, header_value: str) -> str:
    triage = str(triage_value or "").upper()
    header = str(header_value or "")
    if triage not in {"DDA", "DIA", "SWATH", "AIF", "MS1"}:
        return "not_compared"
    if header in {"", "Unknown", "Mixed"}:
        return "unresolved"
    if triage == "MS1":
        return "agree" if header == "FullScan" else "disagree"
    if triage == "DDA":
        return "agree" if header == "DDA" else "disagree"
    return "agree" if header in _DIA_FAMILY else "disagree"


def _polarity_result(triage_value: str, header_value: str, split: bool = False) -> str:
    """agree, disagree, unresolved or not_compared. In a polarity_split child (``split``) a header of both polarities
    agrees (the child reads its own polarity's scans), and one of the other polarity alone is no_child_polarity_scans:
    no disagreement with the triage, a file the disposition refuses for this child (POLARITY_SPLIT_NO_SCANS)."""
    triage = _POLARITY.get(str(triage_value or "").casefold(), "")
    header = str(header_value or "")
    if not triage:
        return "not_compared"
    if header in _HEADER_SWITCHING:
        return "agree" if split else "disagree"
    if split and header in _HEADER_POLARITIES and header != triage:
        return "no_child_polarity_scans"
    if header not in _HEADER_POLARITIES:
        return "unresolved"
    return "agree" if header == triage else "disagree"


def reconcile_curated_selection(manifest: Mapping[str, Any]) -> dict[str, Any] | None:
    """Each inspected input's raw header against what the curated selection said of it, or None without a selection.

    Per input: its member, the triage's ion mode and acquisition against the header's, each agree, disagree,
    unresolved (the header was read and states none) or not_compared (the triage states none). The verdict is
    disagree when any input disagrees, when an input is no kept row's (not_in_selection), or when a header could not
    be read for a row the triage only inferred (basis inferred): the header is what was to confirm the inference.
    A header that states nothing for a declared row is no disagreement; decide_disposition handles it as before.
    """
    block = curated_block(manifest)
    if block is None:
        return None
    preflight = manifest.get("raw_metadata_preflight") if isinstance(manifest.get("raw_metadata_preflight"), dict) else {}
    summary = preflight.get("summary") if isinstance(preflight.get("summary"), dict) else {}
    per_file = [item for item in summary.get("per_file") or [] if isinstance(item, dict) and item.get("file")]
    by_member = _admitted_by_member(block)
    applied = block.get("applied_technical") if isinstance(block.get("applied_technical"), dict) else {}
    split = isinstance(block.get("polarity_split"), dict)
    data_root = str(manifest.get("input_directory") or "")
    raw_directory = _raw_directory(manifest)
    sources = _conversion_sources(manifest)
    files: list[dict[str, Any]] = []
    disagreements: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in per_file:
        path = str(entry["file"])
        member = member_for(path, data_root, by_member)
        if not member and _file_key(path) in sources:
            # An mzXML member converted to mzML under raw/converted: the kept row is its source's (review of 0.5.69).
            member = member_for(sources[_file_key(path)], data_root, by_member)
        relative = _portable_path(path, data_root, raw_directory)
        if not member:
            item = {"file": relative, "member_path": None, "result": "disagree", "reason": "not_in_selection",
                    "reasons": ["not_in_selection"]}
            files.append(item)
            disagreements.append(item)
            continue
        seen.add(member)
        row = by_member[member]
        outcome = str(entry.get("outcome") or "ok")
        read = outcome in _READ_OUTCOMES
        # What the triage said of this input: its row's own value where the row states one that can be compared, else
        # the unit's as applied (the child's, or unit_technical's). A row the triage left unknown is still in a unit of
        # one polarity and one acquisition, and the header is compared with that, never skipped (review of 0.5.69).
        triage_ion, ion_basis = _triage_value(row.get("ion_mode"), applied.get("ion_mode"), _polarity_comparable)
        triage_acquisition, acquisition_basis = _triage_value(
            row.get("acquisition_mode"), applied.get("acquisition_mode"), _acquisition_comparable
        )
        polarity = _polarity_result(triage_ion, entry.get("polarity"), split) if read else "not_read"
        acquisition = _acquisition_result(triage_acquisition, entry.get("acquisition_mode")) if read else "not_read"
        reasons = []
        if polarity == "disagree":
            reasons.append("polarity")
        if acquisition == "disagree":
            reasons.append("acquisition_mode")
        if not read and str(row.get("basis") or "") == "inferred":
            reasons.append("header_unread_for_inferred_row")
        item = {
            "file": relative,
            "member_path": row.get("member_path"),
            "row_index": row.get("row_index"),
            "basis": row.get("basis"),
            "outcome": outcome,
            "ion_mode": {
                "triage": triage_ion,
                "triage_basis": ion_basis,
                "header": entry.get("polarity") or "",
                "result": polarity,
            },
            "acquisition_mode": {
                "triage": triage_acquisition,
                "triage_basis": acquisition_basis,
                "header": entry.get("acquisition_mode") or "",
                "result": acquisition,
            },
            "result": "disagree" if reasons else "agree",
            **({"reasons": reasons} if reasons else {}),
        }
        files.append(item)
        if reasons:
            disagreements.append(item)
    if not per_file:
        verdict = "not_evaluated"
    else:
        verdict = "disagree" if disagreements else "agree"
    return {
        "schema": RECONCILIATION_SCHEMA,
        "triage_sha256": block.get("triage_sha256"),
        "analysis_unit_key": block.get("analysis_unit_key"),
        "verdict": verdict,
        "reason": TRIAGE_HEADER_DISAGREEMENT if verdict == "disagree" else "",
        "inspected": len(per_file),
        "agree": sum(1 for item in files if item["result"] == "agree"),
        "disagree": len(disagreements),
        "kept_rows_not_inspected": sorted(
            str(by_member[key].get("member_path") or "") for key in by_member if key not in seen
        )[:100],
        "files": files[:500],
        "retryable": False,
    }


def apply_reconciliation(manifest: Mapping[str, Any], disposition: dict[str, Any]) -> dict[str, Any]:
    """decide_disposition's record with the curated selection's reconciliation applied. Changes the record in place.

    Without a curated selection nothing changes. With one, the reconciliation is recorded on the disposition. A
    disagreement turns a run, split or skip into a skip that holds (hold true, reason triage_header_disagreement, no
    assignments): the raw data are kept and the unit waits for a person, and a recheck gives the same verdict until a
    revised triage is planned again. An exclusion stays an exclusion, with the reconciliation beside it.
    """
    block = reconcile_curated_selection(manifest)
    if block is None:
        return disposition
    disposition["curated_selection_reconciliation"] = block
    if block["verdict"] != "disagree" or disposition.get("disposition") == "exclude":
        return disposition
    for stale in ("console_acquisition_type", "ion_mode", "aif_run_as_swath", "aif_multi_ce_run"):
        disposition.pop(stale, None)
    disposition["disposition"] = "skip"
    disposition["hold"] = True
    disposition["split_key"] = None
    reasons = list(disposition.get("reasons") or [])
    if TRIAGE_HEADER_DISAGREEMENT not in reasons:
        reasons.append(TRIAGE_HEADER_DISAGREEMENT)
    disposition["reasons"] = reasons
    if isinstance(disposition.get("assignments"), dict):
        disposition["assignments"].clear()
    detail = list(disposition.get("detail") or [])
    shown = ", ".join(
        f"{item.get('member_path') or item.get('file')} ({'/'.join(item.get('reasons') or [item.get('reason') or ''])})"
        for item in block["files"]
        if item["result"] == "disagree"
    )
    detail.append(
        f"The raw headers of {block['disagree']} input(s) disagree with the curated selection "
        f"(triage {block['triage_sha256']}): {shown[:600]}. Neither side wins: the unit is held for a person, and runs "
        "only after a revised triage is planned again."
    )
    disposition["detail"] = detail
    return disposition


def cross_unit_refusal(manifest: Mapping[str, Any]) -> dict[str, Any] | None:
    """The cross-unit check's refusal this unit's manifest records, or None.

    msdial_repository_cross_unit_check writes cross_unit_attribution on each manifest; a refused verdict stands until
    a fresh check passes the unit over a group holding every unit it was refused against (merge_prior_refusals), and a
    new lease of the same workspace carries it (repository_reanalysis.create_download_lease). Nothing else lifts it: a
    recheck of the unit's own headers says nothing about whose files they are.
    """
    block = manifest.get("cross_unit_attribution") if isinstance(manifest.get("cross_unit_attribution"), dict) else None
    if not block or str(block.get("verdict") or "") != "refused":
        return None
    return block


def apply_cross_unit_hold(manifest: Mapping[str, Any], disposition: dict[str, Any]) -> dict[str, Any]:
    """decide_disposition's record with a recorded cross-unit refusal applied. Changes the record in place.

    A unit the cross-unit check refused is held as a header disagreement is: a skip with hold true, the refusal's
    codes among the reasons and no assignments, so a recheck, a resume or a split decides it held again and never
    runs it on another unit's files (review of 0.5.69: the refusal lived only in execution_allowed, which the next
    preflight rewrote). An exclusion stays an exclusion, with the refusal beside it.
    """
    block = cross_unit_refusal(manifest)
    if block is None:
        return disposition
    codes = [str(code) for code in block.get("reasons") or [block.get("reason_code")] if str(code or "").strip()]
    disposition["cross_unit_attribution_hold"] = {
        "rule": block.get("rule"),
        "reason_code": block.get("reason_code"),
        "reasons": codes,
        "retryable": False,
    }
    if disposition.get("disposition") == "exclude":
        return disposition
    for stale in ("console_acquisition_type", "ion_mode", "aif_run_as_swath", "aif_multi_ce_run"):
        disposition.pop(stale, None)
    disposition["disposition"] = "skip"
    disposition["hold"] = True
    disposition["split_key"] = None
    reasons = list(disposition.get("reasons") or [])
    for code in codes:
        if code not in reasons:
            reasons.append(code)
    disposition["reasons"] = reasons
    if isinstance(disposition.get("assignments"), dict):
        disposition["assignments"].clear()
    detail = list(disposition.get("detail") or [])
    detail.append(
        "The cross-unit check refused this unit ("
        + ", ".join(codes)
        + "): its Sample inputs are not its own alone. It is held for a person, and runs only after a fresh "
        "cross-unit check of its group passes it."
    )
    disposition["detail"] = detail
    return disposition


# --------------------------------------------------------------------------------------------------------------------
# Cross-unit attribution.


def _file_key(text: str) -> str:
    return str(text or "").replace("\\", "/").casefold()


def _listing_of(row: Mapping[str, Any]) -> dict[str, Any]:
    basis = row.get("basis") if isinstance(row.get("basis"), dict) else {}
    listing = [item for item in basis.get("listing") or [] if isinstance(item, dict)]
    return listing[0] if listing else {}


def _input_identity(row: Mapping[str, Any], data_root: str) -> tuple[str, str]:
    """(identity, basis) of one lineage row: a declared checksum; the bytes' own sha256 for a file downloaded as
    itself; the store object (its archive's sha256, else its URL) plus the member path; else the path under the data
    root. Never a bare name: two archives of one accession may each hold S1.raw."""
    source = row.get("source") if isinstance(row.get("source"), dict) else {}
    conversion = source.get("conversion") if isinstance(source.get("conversion"), dict) else None
    if conversion and isinstance(conversion.get("source_row"), dict):
        return _input_identity(conversion["source_row"], data_root)
    checksums = row.get("checksums") if isinstance(row.get("checksums"), dict) else {}
    declared = str(checksums.get("declared") or "").strip().casefold()
    if declared:
        return f"declared:{declared}", "declared_checksum"
    if str(row.get("kind") or "") == "file" and str(checksums.get("sha256") or "").strip():
        return f"sha256:{str(checksums['sha256']).casefold()}", "download_sha256"
    listing = _listing_of(row)
    archive = source.get("archive") if isinstance(source.get("archive"), dict) else {}
    archive_id = str(
        listing.get("archive_sha256") or archive.get("sha256") or archive.get("url") or ""
    ).strip().casefold()
    member = str(listing.get("member") or "").strip()
    if archive_id and member:
        return f"object:{archive_id}:{_member_key(member)}", "store_object_member"
    return f"path:{_member_key(_relative(str(row.get('path') or ''), data_root))}", "path_under_data_root"


def _listed_content(row: Mapping[str, Any], cache: dict[str, dict[str, str]]) -> str:
    """crc32:size of an archive member as its archive's member listing records it, or '' where none is recorded.

    The listing is the extract stage's members TSV (archives._TSV_COLUMNS); a member and another of different path
    with the same CRC32 and size are byte copies in all but name (ST002283's Hilic_Neg and Lipid_Neg folders).
    """
    listing = _listing_of(row)
    tsv = str(listing.get("members_tsv") or "")
    member = _member_key(listing.get("member"))
    if not tsv or not member:
        return ""
    if tsv not in cache:
        table: dict[str, str] = {}
        try:
            with open(tsv, encoding="utf-8-sig", newline="") as handle:
                for record in csv.DictReader(handle, delimiter="\t"):
                    crc = str(record.get("crc32") or "").strip().upper()
                    size = str(record.get("size") or "").strip()
                    if not crc or not size.isdigit() or int(size) <= 0:
                        continue
                    for column in ("path", "member_name"):
                        key = _member_key(record.get(column))
                        if key:
                            table.setdefault(key, f"crc32:{crc}:{int(size)}")
        except OSError:
            table = {}
        cache[tsv] = table
    return cache[tsv].get(member, "")


def _polarity_of(manifest: Mapping[str, Any]) -> str:
    disposition = manifest.get("campaign_disposition") if isinstance(manifest.get("campaign_disposition"), dict) else {}
    project = manifest.get("project") if isinstance(manifest.get("project"), dict) else {}
    for value in (disposition.get("ion_mode"), project.get("ion_mode")):
        text = str(value or "").strip().capitalize()
        if text in _HEADER_POLARITIES:
            return text
    return ""


# Names of injections shared on purpose that the coverage gate's reading (repository_analysis_rows._coverage_file_type)
# takes for samples: pooled and long-term references (LTR, SR, PoolQC, QCpool, PBQC, MQC, LQC, batchQC) and solvent
# blanks (Solvent, Water, MeOH). Counting them as Sample inputs refused two units sharing four LTRs at rule S (review
# of 0.5.69); they are left out of the cross-unit comparison, as the blanks and QCs the gate knows are.
_SHARED_REFERENCE_TOKENS = frozenset({
    "ltr", "sr", "pool", "pooled", "blank", "blk", "solvent", "water", "meoh", "methanol", "acn", "wash",
})
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z])(?=[A-Z])")


def shared_reference_name(name: str) -> bool:
    """Whether a file or sample name reads as a QC, reference or blank injection shared across units on purpose.

    By whole tokens (split on anything not a letter, and at a lower-to-upper case change): a token in
    _SHARED_REFERENCE_TOKENS, one holding 'pool', or one that starts or ends with 'qc' (PBQC, MQC, LQC, QCpool,
    batchQC). 'LTR_1', 'SR_1', 'PooledQC_1', 'QCpool1', 'PBQC_01', 'Solvent_01' and 'MeOH_01' do; 'liver_1',
    'seawater_2' and 'S1' do not.
    """
    stem = PurePosixPath(str(name or "").replace("\\", "/")).name
    stem = stem.split(".", 1)[0]
    if "meoh" in stem.casefold():
        return True
    tokens = {
        token.casefold()
        for part in re.split(r"[^A-Za-z]+", stem)
        for token in [part, *_CAMEL_BOUNDARY.split(part)]
        if token
    }
    return any(
        token in _SHARED_REFERENCE_TOKENS or token.startswith("qc") or token.endswith("qc") or "pool" in token
        for token in tokens
    )


def _unit_view(
    manifest: Mapping[str, Any], index: int, label: str, cache: dict[str, dict[str, str]]
) -> dict[str, Any]:
    from .repository_analysis_rows import _coverage_file_type
    from .repository_reanalysis import project_from_dict, unit_chromatography

    project = manifest.get("project") if isinstance(manifest.get("project"), dict) else {}
    block = curated_block(manifest) or {}
    data_root = str(manifest.get("input_directory") or "")
    technical = block.get("unit_technical") if isinstance(block.get("unit_technical"), dict) else {}
    chromatography = str(technical.get("chromatography") or "")
    if chromatography not in {"HILIC", "RP"}:
        try:
            chromatography = unit_chromatography(project_from_dict(dict(project)))
        except (TypeError, ValueError, KeyError):
            chromatography = ""
    kept = _admitted_by_member(block)
    split = block.get("polarity_split")
    split_polarity = _POLARITY.get(str((split or {}).get("ion_mode") or "").casefold(), "") if isinstance(split, dict) else ""
    bases = {str(row.get("basis") or "") for row in kept.values()}
    preflight = manifest.get("raw_metadata_preflight") if isinstance(manifest.get("raw_metadata_preflight"), dict) else {}
    headers = {
        _file_key(str(item.get("file") or "")): str(item.get("polarity") or "")
        for item in ((preflight.get("summary") or {}).get("per_file") or [])
        if isinstance(item, dict)
    }
    lineage = manifest.get("input_lineage") if isinstance(manifest.get("input_lineage"), dict) else {}
    inputs: dict[str, dict[str, Any]] = {}
    for row in lineage.get("rows") or []:
        if not isinstance(row, dict) or not str(row.get("path") or "").strip():
            continue
        path = str(row["path"])
        relative = _relative(path, data_root)
        member = member_for(path, data_root, kept) if kept else ""
        role = str((kept.get(member) or {}).get("role") or "").strip().casefold() if member else ""
        name = PurePosixPath(relative).name
        sample_id = str(row.get("sample_id") or "")
        if role not in _UNKNOWN_ROLES:
            # A role the triage row states (a person's answer reaches the run as the row of a revised triage) decides,
            # whatever the name reads as (WS0 decision D): a biological sample named QC_liver_1 is compared, so two
            # units holding the same such samples are refused at rule D; a row stated qc or blank is not.
            if role != "sample":
                continue
        elif (_coverage_file_type(name) != "Sample" or shared_reference_name(name)
              or (sample_id and (_coverage_file_type(sample_id) != "Sample" or shared_reference_name(sample_id)))):
            # Only a row whose role is unknown (or a lineage row no curated row names) is read by its names.
            continue
        header = headers.get(_file_key(path), "")
        if split_polarity and header in _HEADER_POLARITIES and header != split_polarity:
            # A polarity_split child does not run a file with no scans of its polarity (the disposition refuses it
            # there, POLARITY_SPLIT_NO_SCANS), so the file is not this child's to be compared.
            continue
        identity, basis = _input_identity(row, data_root)
        inputs[identity] = {
            "path": relative,
            "identity_basis": basis,
            "content": _listed_content(row, cache),
            "header_polarity": headers.get(_file_key(path), ""),
        }
    return {
        "index": index,
        "label": label,
        "analysis_unit_id": str(project.get("analysis_unit_id") or ""),
        "accession": str(project.get("accession") or ""),
        "polarity": _polarity_of(manifest),
        "polarity_basis": "inferred" if bases == {"inferred"} else "declared",
        "polarity_split": split if isinstance(split, dict) else None,
        "chromatography": chromatography,
        "acquisition_mode": str(project.get("acquisition_mode") or ""),
        "inputs": inputs,
        "contents": {item["content"]: identity for identity, item in inputs.items() if item["content"]},
    }


def polarity_split_siblings(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Whether two units are the two polarity_split children of one Catalog unit (0.5.70): the same files, read once as
    Positive and once as Negative by the author's decision, so neither holds the other's files."""
    one, other = a.get("polarity_split"), b.get("polarity_split")
    if not (isinstance(one, dict) and isinstance(other, dict)):
        return False
    return (
        str(one.get("parent_unit_id") or "") == str(other.get("parent_unit_id") or "") != ""
        and str(one.get("ion_mode") or "") == str(other.get("sibling_ion_mode") or "")
        and str(other.get("ion_mode") or "") == str(one.get("sibling_ion_mode") or "")
        and str(one.get("ion_mode") or "") != str(other.get("ion_mode") or "")
        and str(a.get("analysis_unit_id") or "") != str(b.get("analysis_unit_id") or "")
    )


def _overlap(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """How much of two units' Sample inputs are one: by identity (the same files), else by listed content (byte copies
    under other names), whichever is the larger; with each side's shared inputs."""
    smaller = min(len(a["inputs"]), len(b["inputs"]))
    by_identity = set(a["inputs"]) & set(b["inputs"])
    by_content = set(a["contents"]) & set(b["contents"])
    if len(by_content) > len(by_identity):
        return {
            "basis": "listed_content_crc32_size",
            "shared": len(by_content),
            "containment": len(by_content) / smaller if smaller else 0.0,
            "a": [a["contents"][key] for key in by_content],
            "b": [b["contents"][key] for key in by_content],
        }
    return {
        "basis": "identity",
        "shared": len(by_identity),
        "containment": len(by_identity) / smaller if smaller else 0.0,
        "a": sorted(by_identity),
        "b": sorted(by_identity),
    }


def cross_unit_attribution(manifests: list[Mapping[str, Any]], labels: list[str] | None = None) -> dict[str, Any]:
    """Whether sibling units' Sample inputs are their own: rules P, C, S and D over their recorded lineage. Pure but for
    reading the archives' member listings the lineage names.

    Only Sample inputs are compared, so blanks and QCs shared on purpose never refuse a unit. A curated row's stated
    role decides (sample is compared, any other role is not, whatever the name reads as); only a row whose role is
    unknown, or an input no curated row names, is read by its names (blanks, QCs and standards by the coverage gate's
    reading or by shared_reference_name). A file is identified by
    _input_identity, never by its bare name; byte copies under other names are found by the CRC32 and size the
    archive's member listing records (_listed_content).

    - P, first: a unit with an input whose header states the other polarity is refused where a sibling of that
      polarity holds the same file, or the unit's polarity is a declaration rather than an inference; an inferred
      polarity with no sibling claiming the file is overridden by the header, on record. Units of different
      polarities sharing at least half of their inputs, none with a header to settle it, are refused both.
    - C: of the units P leaves, a pair of one polarity and different chromatographies sharing at least
      CROSS_UNIT_CONTAINMENT_MINIMUM of the smaller set is refused. Where they share the same files, a member every
      one of whose shared inputs states its own chromatography, and only it, by name keeps them (the other is then
      refused); any other share refuses both. Where they share byte copies under other names, the names prove nothing
      (ST002283's copies were named HILIC and were reversed phase) and both are refused. Above 0 and below the
      minimum is a warning on both.
    - S: units of one polarity whose chromatographies do not tell them apart (the same, or one unknown) and whose
      Sample inputs differ, the smaller set contained at CROSS_UNIT_CONTAINMENT_MINIMUM or more in the larger, are
      refused both (CROSS_UNIT_CONTAINED_INPUTS): one holds files the other claims, and nothing on record says
      which (a liver unit holding the plasma unit's files). Above 0 and below the minimum is a warning on both.
      Applied to every pair whatever P or C decided, so a third unit cannot escape it through a refused sibling.
    - D: of the units left, every unit whose Sample inputs are identical to an earlier unit's, as files or as listed
      content, is refused; the earlier keeps them.

    The two polarity_split children of one Catalog unit (polarity_split_siblings) hold the same files by decision,
    so neither P's shared-file refusal nor D applies between them (0.5.70).

    Every refusal is recorded with retryable false: the same manifests give the same verdict.
    """
    from .repository_reanalysis import name_chromatographies

    labels = list(labels or [])
    cache: dict[str, dict[str, str]] = {}
    units = [
        _unit_view(manifest, index, labels[index] if index < len(labels) else "", cache)
        for index, manifest in enumerate(manifests)
    ]
    verdicts: dict[int, dict[str, Any]] = {
        unit["index"]: {"reasons": [], "warnings": [], "details": [], "against": []} for unit in units
    }
    overlaps: list[dict[str, Any]] = []

    def refuse(unit: dict[str, Any], code: str, detail: str, others: Iterable[dict[str, Any]] = ()) -> None:
        record = verdicts[unit["index"]]
        if code not in record["reasons"]:
            record["reasons"].append(code)
        record["details"].append(detail)
        for other in others:
            # Whom the refusal is against: a later check lifts it only over a group that holds every one of them
            # (msdial_repository_cross_unit_check), so a check that leaves the sibling out cannot pass the unit.
            identity = other["analysis_unit_id"] or name(other)
            if identity not in record["against"]:
                record["against"].append(identity)

    def warn(unit: dict[str, Any], code: str, detail: str) -> None:
        record = verdicts[unit["index"]]
        if code not in record["warnings"]:
            record["warnings"].append(code)
        record["details"].append(detail)

    def name(unit: dict[str, Any]) -> str:
        return unit["label"] or unit["analysis_unit_id"] or f"#{unit['index']}"

    def standing(unit: dict[str, Any]) -> bool:
        return not verdicts[unit["index"]]["reasons"]

    # P: a header polarity that contradicts the unit's.
    for unit in units:
        if not unit["polarity"]:
            continue
        contradicting = {
            identity for identity, item in unit["inputs"].items()
            if item["header_polarity"] in _HEADER_POLARITIES and item["header_polarity"] != unit["polarity"]
        }
        if not contradicting:
            continue
        siblings = [
            other for other in units
            if other is not unit and other["polarity"] and other["polarity"] != unit["polarity"]
            and contradicting & set(other["inputs"])
        ]
        if siblings or unit["polarity_basis"] == "declared":
            refuse(
                unit,
                CROSS_UNIT_POLARITY_CONTRADICTION,
                f"{len(contradicting)} Sample input(s) of {name(unit)} ({unit['polarity']}) have a header of the other "
                "polarity"
                + (f", and {', '.join(name(other) for other in siblings)} hold(s) the same files" if siblings else "")
                + ".",
                siblings,
            )
        else:
            warn(
                unit,
                CROSS_UNIT_HEADER_OVERRIDES_INFERENCE_WARNING,
                f"{len(contradicting)} Sample input(s) of {name(unit)} have a header of the other polarity; its polarity "
                "was inferred and no sibling claims them, so the header stands, on record.",
            )
    for position, a in enumerate(units):
        for b in units[position + 1:]:
            if not (a["polarity"] and b["polarity"] and a["polarity"] != b["polarity"]):
                continue
            if polarity_split_siblings(a, b):
                # One set of polarity-switching files, read once per polarity: the decision of 2026-11-19.
                continue
            found = _overlap(a, b)
            if not found["shared"] or any(
                a["inputs"][identity]["header_polarity"] in _HEADER_POLARITIES for identity in found["a"]
            ):
                continue
            if found["containment"] >= CROSS_UNIT_CONTAINMENT_MINIMUM:
                overlaps.append({"units": [name(a), name(b)], "rule": "P", **_overlap_record(found)})
                for unit, other in ((a, b), (b, a)):
                    refuse(
                        unit,
                        CROSS_UNIT_POLARITY_CONTRADICTION,
                        f"{name(unit)} ({unit['polarity']}) and {name(other)} ({other['polarity']}) share "
                        f"{found['shared']} Sample input(s) ({found['basis']}) whose headers state no polarity; one file "
                        "is not two polarities.",
                        [other],
                    )

    # C: one polarity, different chromatographies. Every pair, whatever P decided: a sibling refused for its polarity
    # still holds the files it holds, and the unit sharing them across a chromatography is no more right for it.
    for position, a in enumerate(units):
        for b in units[position + 1:]:
            if a["polarity"] and b["polarity"] and a["polarity"] != b["polarity"]:
                continue
            if not (a["chromatography"] and b["chromatography"]) or a["chromatography"] == b["chromatography"]:
                continue
            found = _overlap(a, b)
            if not found["shared"]:
                continue
            overlaps.append({"units": [name(a), name(b)], "rule": "C", **_overlap_record(found)})
            ratio = found["containment"]
            if ratio < CROSS_UNIT_CONTAINMENT_MINIMUM:
                for unit, other in ((a, b), (b, a)):
                    warn(
                        unit,
                        CROSS_UNIT_PARTLY_SHARED_WARNING,
                        f"{name(unit)} shares {found['shared']} Sample input(s) with {name(other)} "
                        f"({other['chromatography']}), containment {ratio:.2f} ({found['basis']}).",
                    )
                continue
            for unit, other, own_shared in ((a, b, found["a"]), (b, a, found["b"])):
                own = 0
                if found["basis"] == "identity":
                    # A unit keeps shared files only where EVERY one of them states its own chromatography, and only
                    # its own, by name: then they are its files, and the other unit is the one holding what is not
                    # its own. A shared file that names nothing, or names the other chromatography, is one this unit
                    # cannot claim, so it is refused (review of 0.5.69: ST002283's HILIC-Neg unit held 40 Hilic-named
                    # and 40 Lipid-named files, and half named its own was let through).
                    own = sum(
                        1 for identity in own_shared
                        if name_chromatographies(unit["inputs"][identity]["path"]) == {unit["chromatography"]}
                    )
                    if own_shared and own == len(own_shared):
                        continue
                refuse(
                    unit,
                    CROSS_UNIT_INDISTINGUISHABLE_CHROMATOGRAPHY,
                    f"{name(unit)} ({unit['chromatography']}) and {name(other)} ({other['chromatography']}) share "
                    f"{found['shared']} Sample input(s) ({found['basis']}), containment {ratio:.2f}"
                    + (
                        f", and not every shared file states {unit['chromatography']} alone by its own name "
                        f"({own} of {len(own_shared)} do)."
                        if found["basis"] == "identity"
                        else "; byte copies under other names are never settled by name."
                    ),
                    [other],
                )

    # S: one polarity, chromatographies that do not tell the units apart, different sets, one mostly inside the other.
    for position, a in enumerate(units):
        for b in units[position + 1:]:
            if a["polarity"] and b["polarity"] and a["polarity"] != b["polarity"]:
                continue
            if a["chromatography"] and b["chromatography"] and a["chromatography"] != b["chromatography"]:
                continue
            if not (a["inputs"] and b["inputs"]):
                continue
            if set(a["inputs"]) == set(b["inputs"]) or (
                len(a["contents"]) == len(a["inputs"]) and len(b["contents"]) == len(b["inputs"])
                and set(a["contents"]) == set(b["contents"])
            ):
                continue  # identical: D's
            found = _overlap(a, b)
            if not found["shared"]:
                continue
            overlaps.append({"units": [name(a), name(b)], "rule": "S", **_overlap_record(found)})
            ratio = found["containment"]
            for unit, other in ((a, b), (b, a)):
                if ratio < CROSS_UNIT_CONTAINMENT_MINIMUM:
                    warn(
                        unit,
                        CROSS_UNIT_PARTLY_SHARED_WARNING,
                        f"{name(unit)} shares {found['shared']} Sample input(s) with {name(other)} (one polarity, "
                        f"chromatography {other['chromatography'] or 'unknown'}), containment {ratio:.2f} "
                        f"({found['basis']}).",
                    )
                else:
                    refuse(
                        unit,
                        CROSS_UNIT_CONTAINED_INPUTS,
                        f"{name(unit)} ({len(unit['inputs'])} Sample inputs) and {name(other)} "
                        f"({len(other['inputs'])}) share {found['shared']} ({found['basis']}), containment "
                        f"{ratio:.2f}, at one polarity and a chromatography that does not tell them apart: one holds "
                        "files the other claims, and nothing on record says which.",
                        [other],
                    )

    # D: an identical set.
    for position, b in enumerate(units):
        if not standing(b) or not b["inputs"]:
            continue
        first = None
        basis = ""
        for a in units[:position]:
            # Only a unit still standing keeps a set: one P or C refused keeps nothing for D to protect.
            if not a["inputs"] or not standing(a) or polarity_split_siblings(a, b):
                continue
            if set(a["inputs"]) == set(b["inputs"]):
                first, basis = a, "identity"
                break
            if (
                len(a["contents"]) == len(a["inputs"]) and len(b["contents"]) == len(b["inputs"])
                and set(a["contents"]) == set(b["contents"])
            ):
                first, basis = a, "listed_content_crc32_size"
                break
        if first is not None:
            overlaps.append(
                {"units": [name(first), name(b)], "rule": "D", "basis": basis, "shared": len(b["inputs"]),
                 "containment": 1.0}
            )
            refuse(
                b,
                CROSS_UNIT_DUPLICATE_INPUTS,
                f"{name(b)} has the same {len(b['inputs'])} Sample input(s) as {name(first)} ({basis}), which keeps "
                "them.",
                [first],
            )

    results = []
    for unit in units:
        record = verdicts[unit["index"]]
        results.append(
            {
                "index": unit["index"],
                "label": unit["label"],
                "analysis_unit_id": unit["analysis_unit_id"],
                "accession": unit["accession"],
                "polarity": unit["polarity"],
                "polarity_basis": unit["polarity_basis"],
                "chromatography": unit["chromatography"],
                "sample_inputs": len(unit["inputs"]),
                "identity_bases": sorted({item["identity_basis"] for item in unit["inputs"].values()}),
                "listed_contents": len(unit["contents"]),
                "verdict": "refused" if record["reasons"] else "passed",
                "reason": record["reasons"][0] if record["reasons"] else "",
                "reasons": list(record["reasons"]),
                "refused_against": list(record["against"]),
                "warnings": list(record["warnings"]),
                "details": list(record["details"]),
                "retryable": False,
                "prior_refusal_stands": False,
            }
        )
    return {
        "schema": CROSS_UNIT_SCHEMA,
        "rule": CROSS_UNIT_RULE,
        "containment_minimum": CROSS_UNIT_CONTAINMENT_MINIMUM,
        "verdict": "refused" if any(item["verdict"] == "refused" for item in results) else "passed",
        "units": results,
        "overlaps": overlaps,
    }


def _overlap_record(found: Mapping[str, Any]) -> dict[str, Any]:
    return {"basis": found["basis"], "shared": found["shared"], "containment": round(float(found["containment"]), 4)}


def cross_unit_unit_record(result: Mapping[str, Any], index: int) -> dict[str, Any]:
    """What one unit's manifest records of a cross-unit check: its own verdict, the group, and every overlap."""
    own = dict(result["units"][index])
    return {
        "schema": result["schema"],
        "rule": result["rule"],
        "verdict": own["verdict"],
        "reason_code": own["reason"],
        "reasons": own["reasons"],
        "refused_against": list(own.get("refused_against") or []),
        "warnings": own["warnings"],
        "details": own["details"],
        "retryable": False,
        "group": [
            {key: item[key] for key in ("analysis_unit_id", "polarity", "chromatography", "sample_inputs", "verdict")}
            for item in result["units"]
        ],
        "overlaps": [dict(item) for item in result["overlaps"]],
        "prior_refusal_stands": bool(own.get("prior_refusal_stands")),
        **({"prior_refusal": dict(own["prior_refusal"])} if own.get("prior_refusal") else {}),
    }


def merge_prior_refusals(result: dict[str, Any], manifests: list[Mapping[str, Any]]) -> dict[str, Any]:
    """cross_unit_attribution's result with each unit's recorded refusal kept where this check cannot lift it.

    A unit refused before (cross_unit_refusal) and passed now keeps its refusal unless this group holds every unit the
    refusal was against: its ``refused_against``, else, for a block written before that was recorded, every other
    unit of its recorded group. A check that leaves out the sibling the unit collided with says nothing about the
    files they share (review of 0.5.69). The unit is then refused with the earlier codes, ``prior_refusal_stands``
    true and ``prior_refusal`` {rule, reason_code, reasons, refused_against}; the group's verdict follows. Changes and
    returns ``result``.
    """
    present = {str(item.get("analysis_unit_id") or "") for item in result.get("units") or []}
    for item, manifest in zip(result.get("units") or [], manifests):
        prior = cross_unit_refusal(manifest)
        if prior is None or item.get("verdict") != "passed":
            continue
        own = str(item.get("analysis_unit_id") or "")
        against = [str(value) for value in prior.get("refused_against") or [] if str(value or "")]
        if not against and "refused_against" not in prior:
            against = [
                str(member.get("analysis_unit_id") or "") for member in prior.get("group") or []
                if isinstance(member, dict) and str(member.get("analysis_unit_id") or "") not in {"", own}
            ]
        missing = [value for value in against if value not in present]
        if not missing:
            continue
        codes = [str(code) for code in prior.get("reasons") or [prior.get("reason_code")] if str(code or "").strip()]
        item.update(
            verdict="refused",
            reason=codes[0] if codes else "",
            reasons=codes,
            refused_against=against,
            prior_refusal_stands=True,
            prior_refusal={
                "rule": prior.get("rule"),
                "reason_code": prior.get("reason_code"),
                "reasons": codes,
                "refused_against": against,
            },
        )
        item["details"] = [
            *list(item.get("details") or []),
            f"The earlier refusal of {own} ({', '.join(codes)}) stands: it was against {', '.join(against)}, and this "
            f"check leaves out {', '.join(missing)}.",
        ]
    result["verdict"] = "refused" if any(unit.get("verdict") == "refused" for unit in result.get("units") or []) else "passed"
    return result
