"""A repository unit's analysis CSV, built from its durable manifest: one row per analysis input.

WHAT THIS REPLACES. msdial_prepare_repository_reanalysis took its rows from the download job's
``recognized`` list and matched each to a sample row by file name. The job registry keeps only its hundred
newest jobs, so at campaign scale the list was gone by the time a unit was prepared; the match by name
failed for every folder whose sample row named it with a trailing slash; and the acquisition type came
from the unit's one label, 'DIA' being written SWATH for every file, a Waters MSe or Agilent All Ions file
included. The user decided on 2026-09-30 that a Waters .raw or Agilent/Bruker .d folder is one data file
and that the analysis CSV is generated automatically, so the rows come from the record the lease wrote for
exactly that purpose - input_lineage, one row per input, folder or file - and from nothing held in memory.

EACH ROW:

- file_path: the input, or its Console alias (below);
- file_name: the input's stem, unique within the unit;
- file_type: inferred from the sample row (Sample, Blank, QC, Standard), else from the input's name;
- class_id: the sample's Class as projected from the unit's accepted proposal - "All" for every sample
  where the Catalog abstained - folded to ASCII when it has to be, the grouping unchanged;
- acquisition_type: DDA, SWATH or AIF. Under an applied campaign disposition, the type it decided for the
  input (console_acquisition_type, 0.5.17), which the execution gate holds the input to; an input it read
  and decided none for is refused with acquisition_type_not_decided, never given the unit's declaration.
  Otherwise the file's own console_acquisition_type (raw-metadata preflight); where a preflight recorded
  none, the unit's declared DDA, SWATH or AIF. A bare 'DIA' is never written:
  it is SWATH for windowed MS2 and AIF for all-ion MS2, and which one it is is the header's to say, so
  the unit is refused with acquisition_type_ambiguous. The pinned Console reads an unparsable value as
  DDA without a word, so nothing else may reach the column;
- batch_order, analytical_order: declared in the sample row, else 1 and the order the names embed or
  the listing gives (propose_injection_order, as expand_paths_report numbered them); the raw headers'
  acquisition order is applied over both when the CSV is written (order_rows);
- factor: 1.

WHAT THE CONSOLE CAN READ. Its CSV parser (AnalysisFilesParser.cs) reads ASCII and splits on ',' with no
quoting, so a path or a name holding a comma, a quote or a non-ASCII character does not come back as it
was written. Such an input gets an ASCII-safe alias inside the unit's raw tree, raw\\console-aliases: a
directory junction for a folder, a hard link for a file and the sidecars that travel with it. The Console
reads through it and writes its per-file containers beside it, still under the raw directory. Where
neither can be made the unit fails with a record. The alias is recorded on the input's lineage row
(console_alias), with the file_name and the path the CSV gives it.

AN INPUT THE CAMPAIGN DISPOSITION EXCLUDED IS NO ROW. An applied campaign_disposition (written only by
classify_preflight) may run a unit while excluding some of its inputs - ion mobility, an unreadable header,
MS1-only files beside DIA ones - and leaves them among the input candidates. They get no row, their
declared inputs and samples are not counted missing, and the CSV record names them with their reasons
(analysis_csv.excluded_inputs), so the CSV never names an input the disposition keeps out of the run.
Neither does it name one the lease itself excluded: an mzML whose binary arrays RawDataHandler cannot
decode is no input candidate at all (excluded_input_candidates, reason unsupported_mzml_encoding), and is
matched to its declared input and sample, and named with its reason, in the same way.

A UNIT THAT DISAGREES WITH ITSELF FAILS WITH A RECORD. Rows, input candidates, lineage rows, declared
analysis inputs and sample rows must pair one to one; the failures say where they do not, and the caller
records them in the unit's manifest (analysis_csv) instead of raising, so a campaign goes on to the next unit.

AN INPUT IS A SAMPLE ROW'S, NOT A SAMPLE ID'S. A repository lists each injection as a row of its own, and
the rows of one sample share its id: MetaboLights MTBLS291 names its sample Cel in five rows, one per
replicate mzML, and MetaboBank MTBKS64 names S01 in two, one per .RAW. Keyed by sample id, the second
replicate read as a second input of the first one's row and the unit had no CSV (sample_with_two_inputs, as
the code was then named). An input is the row of its sample whose raw_file names it - by its own name, the
names its lineage row was listed under, the name the Catalog declared it at, or the declared raw file the
lease paired a prefixed archive member with (name_pairing) - and each replicate is a CSV row of its own,
carrying its sample's Class. Nothing is merged, averaged or dropped: technical replicates are inputs. What
stays refused is what is ambiguous: one row two inputs name (sample_row_with_two_inputs), an input two rows
name (input_with_two_sample_rows), and an input of a sample several rows describe that none of them names
(sample_row_not_identified). Each row records the sample row it is (sample_row_index, sample_raw_file).
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .repository_metadata import (
    _write_analysis_csv,
    apply_class_proposal,
    class_token,
    infer_analysis_file_type,
    metadata_integer,
    metadata_match_keys,
    metadata_workspace,
)
from .repository_reanalysis import (
    SCIEX_SUFFIXES,
    _applied_disposition,
    _campaign_excluded_inputs,
    _decided_acquisition_by_file,
    _file_key,
    acquisition_start_order,
    declared_analysis_inputs,
    declared_archive_containers,
    lineage_stands_for,
    match_declared_inputs,
    project_from_dict,
    travels_with_sciex_file,
    update_manifest,
    with_order_source,
)
from .sample_grouping import file_type_for, propose_injection_order
from .workflow import console_safe_text

SCHEMA = "msdial-repository-analysis-csv.v1"
CONSOLE_ACQUISITION_TYPES = ("DDA", "SWATH", "AIF")
# Beside raw\data and raw\downloads, never inside them: the discovery walk and the checksum index read
# raw\data, and both would see an aliased folder twice.
ALIAS_DIRECTORY = "console-aliases"
ORDER_FIELDS = ("analytical order", "injection order", "run order", "acquisition order")
BATCH_FIELDS = ("batch order", "batch number", "batch id")

# Failures a caller may accept with allow_partial_mapping: an input no sample row claims, a row two inputs
# claim, an input two rows claim, or an input of a sample whose rows do not say which is it, keeps the
# default Class, as the name-matching path always allowed. Every other failure - counts that disagree, an
# acquisition type that cannot be written, an alias that cannot be made - is structural.
MAPPING_FAILURES = frozenset(
    {"input_without_sample", "sample_row_with_two_inputs", "input_with_two_sample_rows", "sample_row_not_identified"}
)


def _failure(code: str, message: str, inputs: list[str] | None = None) -> dict[str, Any]:
    return {"code": code, "message": message, "inputs": list(inputs or [])[:20]}


def _unit_workspace(manifest: dict[str, Any]) -> dict[str, Any]:
    """The sample rows with the Class the unit's accepted proposal gives them, as prepare projects them."""
    project = manifest.get("project") or {}
    workspace = metadata_workspace(project)
    proposal = project.get("class_proposal") or {}
    return apply_class_proposal(workspace, proposal) if proposal.get("assignments") else workspace


def _per_file_verdicts(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Each inspected file's raw-header record by _file_key: the unit's own preflight, then its parent's."""
    verdicts: dict[str, dict[str, Any]] = {}
    summary = (manifest.get("raw_metadata_preflight") or {}).get("summary") or {}
    for item in [*(summary.get("per_file") or []), *(manifest.get("header_verdicts_from_parent") or [])]:
        if isinstance(item, dict) and str(item.get("file") or "").strip():
            verdicts.setdefault(_file_key(str(item["file"])), item)
    return verdicts


def _inspected_files(manifest: dict[str, Any]) -> set[str]:
    """The _file_key of every input the unit's own raw-metadata preflight recorded, as a disposition read them."""
    summary = (manifest.get("raw_metadata_preflight") or {}).get("summary") or {}
    return {
        _file_key(str(item["file"]))
        for item in summary.get("per_file") or []
        if isinstance(item, dict) and str(item.get("file") or "").strip()
    }


def _acquisition_type(
    verdict: dict[str, Any] | None, declared: str, decided: str | None = None
) -> tuple[str, str, str]:
    """(acquisition_type, source, failure code) for one input; the type is '' when a code is given.

    ``decided`` is the type an applied campaign disposition decided for the input, '' where it decided none
    for an input the preflight read, and None where no disposition was applied or no preflight read the
    input. A decided type is the one the execution gate holds the input to (_decided_acquisition_by_file);
    an input the disposition read and decided none for does not run under it, and is not given the unit's
    declaration instead.
    """
    if decided is not None:
        value = decided.strip().upper()
        if value in CONSOLE_ACQUISITION_TYPES:
            return value, "campaign_disposition", ""
        return "", "", "acquisition_type_not_decided"
    value = str((verdict or {}).get("console_acquisition_type") or "").strip().upper()
    if value in CONSOLE_ACQUISITION_TYPES:
        return value, "raw_header", ""
    unit = str(declared or "").strip().upper()
    if unit in CONSOLE_ACQUISITION_TYPES:
        return unit, "unit_declaration", ""
    if unit == "DIA":
        return "", "", "acquisition_type_ambiguous"
    return "", "", "acquisition_type_unknown"


def ascii_name(value: str, fallback: str = "input") -> str:
    """An ASCII file-name stem for value: accents folded, anything else unsafe replaced by '_'."""
    folded = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    folded = re.sub(r"[^A-Za-z0-9._-]+", "_", folded).strip("._-")
    return (folded or fallback)[:60]


def _digest(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).casefold().encode("utf-8")).hexdigest()[:8]


def _safe_name(value: str) -> bool:
    return bool(value) and value == value.strip(" .") and console_safe_text(value) and not any(
        character in value for character in '<>:/\\|?*'
    )


def _class_aliases(labels: list[str]) -> dict[str, str]:
    """An ASCII token for each Class label the Console could not read back, keeping the grouping.

    A label is folded to ASCII; one that folds to nothing, or to another label's token, is numbered
    instead (Class1, Class2, ... in label order). Labels that are already safe map to themselves.
    """
    distinct = sorted(set(labels))
    aliases: dict[str, str] = {}
    taken = {label.casefold() for label in distinct if console_safe_text(label)}
    number = 0
    for label in distinct:
        if console_safe_text(label):
            aliases[label] = label
            continue
        folded = class_token(
            unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode("ascii")
        )
        if not folded or folded.casefold() in taken:
            number += 1
            folded = f"Class{number}"
            while folded.casefold() in taken:
                number += 1
                folded = f"Class{number}"
        taken.add(folded.casefold())
        aliases[label] = folded
    return aliases


def build_repository_analysis_rows(
    manifest: dict[str, Any], workspace: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The analysis CSV rows for one repository unit, from its manifest. Writes nothing.

    ``workspace`` is the unit's sample rows with Class projected (apply_class_proposal, or the default
    hierarchy); without it the manifest's own accepted proposal is applied. Returns the rows in the order
    expand_paths_report lists the same inputs, the failures, the counts they were checked against, and the
    inputs the lease or an applied campaign disposition excluded. A row whose input needs a Console alias
    carries it in ``console_alias`` (path, kind, target); create_console_aliases makes it.
    """
    project = manifest.get("project") or {}
    workspace = workspace if workspace is not None else _unit_workspace(manifest)
    # In the order expand_paths_report listed them, so a unit's rows keep the order they always had.
    candidates = sorted(
        {str(item) for item in manifest.get("input_candidates") or [] if str(item).strip()}, key=str.lower
    )
    lineage = manifest.get("input_lineage") if isinstance(manifest.get("input_lineage"), dict) else {}
    lineage_rows = [row for row in lineage.get("rows") or [] if isinstance(row, dict)]
    lineage_by_key = {_file_key(str(row.get("path") or "")): row for row in lineage_rows}
    # What the lease kept out of the input candidates (_exclude_undecodable_inputs) has no lineage row, only
    # one among the lineage's excluded rows, and is still the declared input and the sample it was fetched for.
    lease_excluded = {
        _file_key(str(item["path"])): (str(item["path"]), str(item.get("reason") or ""))
        for item in manifest.get("excluded_input_candidates") or []
        if isinstance(item, dict) and str(item.get("path") or "").strip()
    }
    excluded_lineage = {
        _file_key(str(row.get("path") or "")): row
        for row in lineage.get("excluded") or []
        if isinstance(row, dict) and str(row.get("path") or "").strip()
    }
    data_root = Path(str(manifest.get("input_directory") or ""))
    raw_directory = Path(str(manifest.get("raw_directory") or data_root.parent))
    typed = project_from_dict(project)
    declared = declared_analysis_inputs(typed)
    verdicts = _per_file_verdicts(manifest)
    unit_mode = str(project.get("acquisition_mode") or "")
    # The execution gate's own readings of an applied campaign disposition: the inputs it excluded, and the
    # type it decided for each input that runs. A disposition only decided (outside a campaign) is neither.
    # The lease's exclusions are the lease's whatever the disposition says.
    excluded = {
        **_campaign_excluded_inputs(manifest),
        **{key: reason for key, (_path, reason) in lease_excluded.items()},
    }
    decided_types = _decided_acquisition_by_file(manifest)
    decided_over = _inspected_files(manifest) if _applied_disposition(manifest) else set()
    # Every input the lease found, whether or not it runs, in the order expand_paths_report would list it.
    considered = sorted({*candidates, *(path for path, _reason in lease_excluded.values())}, key=str.lower)
    # Which declared input each candidate is, answered as the lease's allow-list answered it: by path, at
    # the place its archive put an archived container, else through the sample its lineage row names. An
    # mzML the lease converted from a declared mzXML is matched through that mzXML (lineage_stands_for).
    matched = (
        match_declared_inputs(
            considered,
            data_root,
            declared,
            containers=declared_archive_containers(typed, declared, manifest.get("archive_extractions")),
            samples={
                key: str(row.get("sample_id") or "") for key, row in {**excluded_lineage, **lineage_by_key}.items()
            },
            stands_for=lineage_stands_for(manifest),
        )
        if declared
        else {}
    )
    declared_of = {candidate: form for form, found in matched.items() for candidate in found}
    stands_for = lineage_stands_for(manifest)

    # The sample rows by position (sample_row_index), by id, and by the names they give: their raw file's
    # and their id's, which the name-matching path has always read, and their raw file's alone, which tells
    # the rows of one sample apart.
    samples = [row for row in workspace.get("rows") or [] if isinstance(row, dict)]
    rows_of_id: dict[str, list[int]] = {}
    by_key: dict[str, list[int]] = {}
    file_keys: list[set[str]] = []
    for index, row in enumerate(samples):
        rows_of_id.setdefault(str(row.get("sample_id") or ""), []).append(index)
        for key in metadata_match_keys(row.get("raw_file", ""), row.get("sample_id", "")):
            by_key.setdefault(key, []).append(index)
        file_keys.append(metadata_match_keys(row.get("raw_file", "")))

    def input_names(candidate: str, entry: dict[str, Any] | None, lineage_row: dict[str, Any] | None) -> set[str]:
        """Every name a sample row may give an input by: its own; the declared input's path and archive; the
        names its lineage row was listed under; the declared raw file the lease paired it with by a prefixed
        member name (name_pairing); and the mzXML it stands for."""
        values: list[Any] = [candidate]
        if entry:
            values.extend((entry.get("path"), entry.get("archive")))
        if lineage_row:
            values.extend(lineage_row.get("declared_names") or [])
            values.append((lineage_row.get("name_pairing") or {}).get("declared_raw_file"))
        values.append(stands_for.get(_file_key(candidate)))
        return metadata_match_keys(*(value for value in values if value))

    def sample_row(
        candidate: str, entry: dict[str, Any] | None, lineage_row: dict[str, Any] | None
    ) -> tuple[int | None, str]:
        """(the position of the sample row the input is, or None; the failure code where it is None).

        The sample: the one the Catalog attributed the input to, else the lineage's, else the one sample row
        whose file name is this input's. Stripped, as the sample rows' own ids are (scalar_text). Where
        several rows carry the sample's id, the input is the one of them whose raw file names it.
        """
        sample_id = (
            str((entry or {}).get("sample_id") or "").strip()
            or str((lineage_row or {}).get("sample_id") or "").strip()
        )
        indexes = rows_of_id.get(sample_id, []) if sample_id else []
        if len(indexes) == 1:
            return indexes[0], ""
        names = input_names(candidate, entry, lineage_row)
        if indexes:
            named = [index for index in indexes if file_keys[index] & names]
            if len(named) == 1:
                return named[0], ""
            return None, "input_with_two_sample_rows" if named else "sample_row_not_identified"
        if declared:
            return None, "input_without_sample"
        found = sorted({index for key in names for index in by_key.get(key, [])})
        if len(found) == 1:
            return found[0], ""
        return None, "input_with_two_sample_rows" if found else "input_without_sample"

    def described(index: int) -> str:
        """A sample row as a failure names it: its id, and its raw file where other rows share the id."""
        row = samples[index]
        sample_id = str(row.get("sample_id") or "")
        if len(rows_of_id.get(sample_id, [])) > 1:
            return f"{sample_id} ({row.get('raw_file') or 'row ' + str(index + 1)})"
        return sample_id

    failures: list[dict[str, Any]] = []
    missing_lineage: list[str] = []
    undeclared: list[str] = []
    unresolved: dict[str, list[str]] = {}
    doubled: list[str] = []
    acquisition_failures: dict[str, list[str]] = {}
    used: dict[int, str] = {}
    declared_order_files: list[str] = []
    rows: list[dict[str, Any]] = []
    excluded_inputs: list[dict[str, Any]] = []
    excluded_forms: set[str] = set()
    excluded_rows: set[int] = set()
    excluded_samples: set[str] = set()
    kept: list[str] = []
    for candidate in considered:
        key = _file_key(candidate)
        if key not in excluded:
            kept.append(candidate)
            continue
        form = declared_of.get(candidate, "")
        lineage_row = lineage_by_key.get(key) or excluded_lineage.get(key)
        sample_id = str(declared[form].get("sample_id") or "").strip() if form else ""
        sample_id = sample_id or str((lineage_row or {}).get("sample_id") or "").strip()
        index, _code = sample_row(candidate, declared.get(form) if form else None, lineage_row)
        record = {"path": candidate, "reason": excluded[key], "sample_id": sample_id}
        if index is not None:
            record["sample_raw_file"] = str(samples[index].get("raw_file") or "")
            excluded_rows.add(index)
        elif sample_id:
            # The row is not known, so every row of the sample is spared, as every one always was.
            excluded_samples.add(sample_id)
        excluded_inputs.append(record)
        if form:
            excluded_forms.add(form)
    listing = propose_injection_order([Path(item).stem for item in kept])

    for position, candidate in enumerate(kept, start=1):
        path = Path(candidate)
        key = _file_key(candidate)
        lineage_row = lineage_by_key.get(key)
        if lineage_row is None:
            missing_lineage.append(path.name)
        entry: dict[str, Any] | None = None
        if declared:
            entry = declared.get(declared_of.get(candidate, ""))
            if entry is None:
                undeclared.append(path.name)

        # The sample row: sample_row says how it is found. A row two inputs name is the first one's.
        index, code = sample_row(candidate, entry, lineage_row)
        sample = samples[index] if index is not None else None
        if index is None:
            unresolved.setdefault(code, []).append(path.name)
        else:
            if index in used:
                doubled.append(f"{path.name} and {Path(used[index]).name} ({described(index)})")
            used.setdefault(index, candidate)

        acquisition, source, code = _acquisition_type(
            verdicts.get(key), unit_mode, decided_types.get(key, "" if key in decided_over else None)
        )
        if code:
            acquisition_failures.setdefault(code, []).append(path.name)
        # As the Console names its outputs, and as expand_paths_report always named them: the name less its
        # last suffix, so a folder x.raw is x and a file x.mzML is x.
        stem, suffix = path.stem, path.suffix
        batch = metadata_integer(sample, BATCH_FIELDS) if sample else None
        order = metadata_integer(sample, ORDER_FIELDS) if sample else None
        if order is not None:
            declared_order_files.append(candidate)
        listed = listing["orders"].get(stem, position)
        inferred = infer_analysis_file_type(sample) if sample else ""
        row: dict[str, Any] = {
            "file_path": candidate,
            "file_name": stem,
            "file_type": inferred if inferred and inferred != "Sample" else file_type_for(stem),
            "class_id": str((sample or {}).get("class_id") or "") or "Sample",
            "acquisition_type": acquisition,
            "batch_order": batch if batch is not None else 1,
            "analytical_order": order if order is not None else listed,
            "factor": 1,
            "sample_id": str((sample or {}).get("sample_id") or ""),
            # Which of the sample's rows this input is, by its position in the unit's sample_metadata and its
            # raw file: replicate rows share the id.
            "sample_row_index": index,
            "sample_raw_file": str((sample or {}).get("raw_file") or ""),
            "input_path": candidate,
            "listing_order": listed,
            "acquisition_type_source": source,
            "console_alias": None,
        }
        reasons = [
            reason
            for reason, safe in (("path_not_console_safe", console_safe_text(candidate)), ("name_not_console_safe", _safe_name(stem)))
            if not safe
        ]
        if reasons:
            # The vendor reader is chosen by the suffix, so the alias keeps it (.raw, .d, .mzML, .wiff).
            alias_stem = f"{ascii_name(stem)}-{_digest(path)}"
            alias_suffix = "." + ascii_name(suffix[1:], "") if suffix[1:] else ""
            row["file_name"] = alias_stem
            row["file_path"] = str(raw_directory / ALIAS_DIRECTORY / f"{alias_stem}{alias_suffix}")
            row["console_alias"] = {
                "path": row["file_path"],
                "kind": "junction" if path.is_dir() else "hardlink",
                "target": candidate,
                "reasons": reasons,
            }
        rows.append(row)

    # A file_name the Console writes twice (its .mdpeak and containers are named by it) is made unique by
    # the input's digest; the lineage records which name each input was given.
    names: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        names.setdefault(row["file_name"].casefold(), []).append(row)
    for group in names.values():
        if len(group) > 1:
            for row in group:
                if row["console_alias"] is None:
                    row["file_name"] = f"{row['file_name']}-{_digest(Path(row['input_path']))}"
                    row["file_name_reason"] = "file_name_not_unique"

    labels = _class_aliases([row["class_id"] for row in rows])
    class_aliases = {label: alias for label, alias in labels.items() if label != alias}
    for row in rows:
        row["class_id"] = labels.get(row["class_id"], row["class_id"])

    candidate_keys = {_file_key(item) for item in candidates}
    extra_lineage = [
        Path(str(row.get("path") or "")).name for key, row in lineage_by_key.items() if key not in candidate_keys
    ]
    if missing_lineage:
        failures.append(_failure(
            "input_without_lineage",
            f"{len(missing_lineage)} input candidate(s) have no input_lineage row, so what they are is not recorded.",
            missing_lineage,
        ))
    if extra_lineage:
        failures.append(_failure(
            "lineage_without_input",
            f"{len(extra_lineage)} input_lineage row(s) name no input candidate of this unit.",
            extra_lineage,
        ))
    if undeclared:
        failures.append(_failure(
            "input_not_declared",
            f"{len(undeclared)} input candidate(s) are none of the analysis inputs the Catalog declared.",
            undeclared,
        ))
    if declared:
        found_kept = {
            form: [item for item in found if _file_key(item) not in excluded] for form, found in matched.items()
        }
        absent = [
            str(declared[form].get("path") or form)
            for form, found in found_kept.items()
            if not found and form not in excluded_forms
        ]
        twice = [str(declared[form].get("path") or form) for form, found in found_kept.items() if len(found) > 1]
        if absent:
            failures.append(_failure(
                "analysis_input_not_found",
                f"{len(absent)} of the {len(declared)} declared analysis inputs have no input candidate.",
                absent,
            ))
        if twice:
            failures.append(_failure(
                "analysis_input_found_twice",
                f"{len(twice)} declared analysis input(s) match more than one input candidate.",
                twice,
            ))
    # A sample row no input is, and whose input no exclusion accounts for: by the row the excluded input was
    # found to be, else, where that row is not known, by the sample's id.
    rows_without_input = [
        index
        for index, row in enumerate(samples)
        if index not in used
        and index not in excluded_rows
        and str(row.get("sample_id") or "") not in excluded_samples
    ]
    if declared:
        without_input = sorted(described(index) for index in rows_without_input)
        if without_input:
            failures.append(_failure(
                "sample_without_input",
                f"{len(without_input)} sample row(s) of the unit have no analysis input.",
                without_input,
            ))
    for code, message in (
        ("input_without_sample", "{} input(s) are named by no sample row of the unit."),
        (
            "input_with_two_sample_rows",
            "{} input(s) are named by more than one sample row of the unit, so which row each is is not stated.",
        ),
        (
            "sample_row_not_identified",
            "{} input(s) belong to a sample that several rows of the unit describe, and none of those rows "
            "names the input, so which injection it is is not stated.",
        ),
    ):
        if unresolved.get(code):
            failures.append(_failure(code, message.format(len(unresolved[code])), unresolved[code]))
    if doubled:
        failures.append(_failure(
            "sample_row_with_two_inputs",
            f"{len(doubled)} sample row(s) are named by two inputs.",
            doubled,
        ))
    for code, inputs in sorted(acquisition_failures.items()):
        if code == "acquisition_type_ambiguous":
            message = (
                f"{len(inputs)} input(s) have no acquisition type MS-DIAL can be given: their raw-header "
                "record gives no console_acquisition_type of DDA, SWATH or AIF, and the unit is declared "
                "'DIA', which is SWATH for windowed MS2 and AIF for all-ion MS2. The header has to say which."
            )
        elif code == "acquisition_type_not_decided":
            message = (
                f"{len(inputs)} input(s) have no acquisition type: the unit's applied campaign disposition "
                "read them and decided none of DDA, SWATH or AIF for them, which it does for an input that "
                "does not run, and for every input of a unit it skips or excludes."
            )
        else:
            message = (
                f"{len(inputs)} input(s) have no acquisition type: their raw-header record gives no "
                f"console_acquisition_type of DDA, SWATH or AIF, and the unit's declared mode "
                f"{unit_mode or 'Unknown'!r} is none of them."
            )
        failures.append(_failure(code, message, inputs))

    alias_root = raw_directory / ALIAS_DIRECTORY
    if any(row["console_alias"] for row in rows) and not console_safe_text(str(alias_root)):
        failures.append(_failure(
            "console_alias_unsafe_location",
            "Inputs need an ASCII-safe alias, and the unit's raw directory itself is not one the Console "
            "can read, so no alias inside it can be.",
            [Path(row["input_path"]).name for row in rows if row["console_alias"]],
        ))
    if not rows:
        failures.append(_failure(
            "no_analysis_input",
            "No input of the unit is left to analyse"
            + (f": all {len(excluded_inputs)} were excluded (excluded_inputs)." if excluded_inputs else "."),
            [Path(item["path"]).name for item in excluded_inputs],
        ))
    unused_rows = [] if declared else rows_without_input
    unused_samples = sorted(str(samples[index].get("sample_id") or "") for index in unused_rows)
    return {
        "schema": SCHEMA,
        "built_from": "input_lineage",
        "rows": rows,
        "failures": failures,
        "counts": {
            "input_candidates": len(candidates),
            "input_lineage_rows": len(lineage_rows),
            "declared_analysis_inputs": len(declared),
            "sample_rows": len(samples),
            "rows": len(rows),
        },
        "declared_order_files": declared_order_files,
        "class_id_aliases": class_aliases,
        # Where the Catalog declared no inputs (an archive unit), a sample row the download did not deliver
        # is said here rather than failed: its inputs are found after the download, by name.
        "samples_without_input": unused_samples,
        # The same rows, each by its position and raw file, since replicate rows share a sample id.
        "sample_rows_without_input": [
            {
                "sample_row_index": index,
                "sample_id": str(samples[index].get("sample_id") or ""),
                "raw_file": str(samples[index].get("raw_file") or ""),
            }
            for index in unused_rows
        ],
        # The inputs the lease or the applied campaign disposition excluded, with the reasons and their samples.
        "excluded_inputs": excluded_inputs,
        "aliases": [row["console_alias"] for row in rows if row["console_alias"]],
        "acquisition_types": sorted({row["acquisition_type"] for row in rows if row["acquisition_type"]}),
    }


def order_rows(manifest: dict[str, Any], built: dict[str, Any]) -> dict[str, Any]:
    """Decide the rows' analytical order, apply it to them, and return the record of how it was decided.

    As for the CSV matched by name: the acquisition start times the raw headers record outrank the order
    a sample row declares and the one the names embed (acquisition_start_order, with_order_source). Both
    are read against each input's own path. The record then names each file as the CSV does, by its
    file_name, because that is what recorded_order_match and the gate compare it with: an input read
    through an alias, or renamed to be unique, is not named in the CSV by its own stem.
    """
    rows = built["rows"]
    inputs = [{"file_path": row["input_path"], "analytical_order": row["analytical_order"]} for row in rows]
    record = with_order_source(
        acquisition_start_order(
            manifest, [row["input_path"] for row in rows], [row["class_id"] for row in rows]
        ),
        inputs,
        built.get("declared_order_files") or [],
        [{"file_path": row["input_path"], "analytical_order": row["listing_order"]} for row in rows],
    )
    orders = record.get("orders") or {}
    for row in rows:
        rank = orders.get(_file_key(row["input_path"]))
        if rank is not None:
            row["analytical_order"] = rank

    def csv_name(row: dict[str, Any]) -> str:
        return f"{row['file_name']}{Path(row['file_path']).suffix}"

    files = [dict(item) for item in record.get("files") or [] if isinstance(item, dict)]
    if orders:
        by_rank = {orders.get(_file_key(row["input_path"])): row for row in rows}
        for item in files:
            row = by_rank.get(item.get("analytical_order"))
            if row is not None:
                item["file"] = csv_name(row)
    else:
        for item, row in zip(files, rows):
            item["file"] = csv_name(row)
            item["analytical_order"] = row["analytical_order"]
    if files:
        record["files"] = files
    return record


def write_analysis_csv(built: dict[str, Any], path: str | Path) -> Path:
    """Write the rows as the analysis CSV (repository_metadata's columns and encoding). Returns its path."""
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_analysis_csv(target, built["rows"])
    return target


def blocking_failures(built: dict[str, Any], allow_partial_mapping: bool = False) -> list[dict[str, Any]]:
    """The failures that stop the CSV; with allow_partial_mapping, the sample-mapping ones do not."""
    return [
        item
        for item in built.get("failures") or []
        if not (allow_partial_mapping and item["code"] in MAPPING_FAILURES)
    ]


def _create_junction(target: Path, link: Path) -> None:
    if os.name != "nt":
        os.symlink(target, link, target_is_directory=True)
        return
    try:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
        return
    except (ImportError, AttributeError):
        pass
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise OSError((completed.stderr or completed.stdout or "mklink /J failed").strip())


def _travelling_files(target: Path) -> list[tuple[Path, str]]:
    """(sidecar, the suffix it carries past the stem) for the files that travel with a SCIEX file.

    Which files those are is travels_with_sciex_file's to say (x.wiff2's x.wiff.scan and x.timeseries.data,
    any x.wiff.<n>.scan), the rule the store lease's prune keeps them by. The suffix is what follows the
    input's stem, so the alias's sidecar is the alias's stem plus it: x.wiff2's x.wiff.scan goes with
    alias-1234.wiff2 as alias-1234.wiff.scan, where the reader looks. A file that is no SCIEX file has none.
    """
    found: list[tuple[Path, str]] = []
    if target.suffix.casefold() not in SCIEX_SUFFIXES:
        return found
    try:
        entries = list(os.scandir(target.parent))
    except OSError:
        return found
    for entry in entries:
        if entry.is_file() and travels_with_sciex_file(entry.name, target.name):
            found.append((Path(entry.path), entry.name[len(target.stem):]))
    return sorted(found, key=lambda item: item[0].name.casefold())


def _same_file(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def create_console_aliases(built: dict[str, Any]) -> list[dict[str, Any]]:
    """Make every alias the rows name. Returns the failures; an alias already in place is reused.

    A folder gets a directory junction, a file a hard link, and a file's sidecars (x.wiff.scan) hard links
    of their own beside it, so the vendor reader finds them. An alias path that holds something else is
    never replaced.
    """
    failures: list[dict[str, Any]] = []
    for row in built.get("rows") or []:
        alias = row.get("console_alias")
        if not alias:
            continue
        link, target = Path(alias["path"]), Path(alias["target"])
        pairs = [(target, link)]
        if alias["kind"] == "hardlink":
            pairs.extend(
                (sidecar, link.with_name(link.stem + rest))
                for sidecar, rest in _travelling_files(target)
            )
        made: list[str] = []
        try:
            link.parent.mkdir(parents=True, exist_ok=True)
            for source, destination in pairs:
                if os.path.lexists(destination):
                    if not _same_file(destination, source):
                        raise FileExistsError(f"{destination.name} already holds something other than {source.name}")
                    continue
                if alias["kind"] == "junction":
                    _create_junction(source, destination)
                else:
                    os.link(source, destination)
                made.append(destination.name)
        except OSError as error:
            failures.append(_failure(
                "console_alias_failed",
                f"No ASCII-safe alias could be made for {target.name}: {error}",
                [target.name],
            ))
            continue
        alias["created_at"] = datetime.now(timezone.utc).isoformat()
        alias["sidecars"] = [destination.name for _source, destination in pairs[1:]]
        if not made:
            alias["reused"] = True
    return failures


def record_analysis_csv(
    manifest_path: str | Path, built: dict[str, Any], csv_path: str | Path
) -> dict[str, Any]:
    """Record the CSV and every input's name in it on the unit's lineage rows (file_name, console_path)."""
    by_key = {_file_key(row["input_path"]): row for row in built["rows"]}
    summary = {
        "schema": SCHEMA,
        "status": "written",
        "built_from": built["built_from"],
        "path": str(csv_path),
        "rows": len(built["rows"]),
        "counts": dict(built["counts"]),
        "acquisition_types": {
            value: sum(1 for row in built["rows"] if row["acquisition_type"] == value)
            for value in built["acquisition_types"]
        },
        "acquisition_type_sources": sorted({row["acquisition_type_source"] for row in built["rows"]}),
        "aliases": len(built["aliases"]),
        "class_id_aliases": dict(built["class_id_aliases"]),
        "samples_without_input": list(built["samples_without_input"]),
        "sample_rows_without_input": [dict(item) for item in built.get("sample_rows_without_input") or []],
        "excluded_inputs": [dict(item) for item in built.get("excluded_inputs") or []],
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }

    def change(manifest: dict[str, Any]) -> None:
        lineage = manifest.get("input_lineage")
        for item in (lineage or {}).get("rows") or []:
            row = by_key.get(_file_key(str(item.get("path") or "")))
            if row is None:
                # No row in this CSV (the disposition excluded it): nothing an earlier CSV said of it stands.
                item["file_name"] = ""
                for stale in ("console_path", "console_alias", "acquisition_type", "file_name_reason", "sample_row"):
                    item.pop(stale, None)
                continue
            item["file_name"] = row["file_name"]
            item["console_path"] = row["file_path"]
            # The sample row the CSV row was written from. sample_id stays the lease's attribution; this is the
            # CSV writer's, and says which of a sample's rows the input is where replicate rows share the id.
            if row.get("sample_row_index") is not None:
                item["sample_row"] = {
                    "index": row["sample_row_index"],
                    "sample_id": row["sample_id"],
                    "raw_file": row["sample_raw_file"],
                }
            else:
                item.pop("sample_row", None)
            # What the execution gate holds the workflow to: the type written from this input's own header.
            item["acquisition_type"] = row["acquisition_type"]
            if row.get("file_name_reason"):
                item["file_name_reason"] = row["file_name_reason"]
            else:
                item.pop("file_name_reason", None)
            if row["console_alias"]:
                item["console_alias"] = dict(row["console_alias"])
            else:
                item.pop("console_alias", None)
        manifest["analysis_csv"] = summary

    return update_manifest(Path(manifest_path), change)


def record_analysis_csv_failure(
    manifest_path: str | Path, built: dict[str, Any], failures: list[dict[str, Any]]
) -> dict[str, Any]:
    """Record why a unit's analysis CSV was not written. Never raises: the caller is reporting a failure."""
    record = {
        "schema": SCHEMA,
        "status": "failed",
        "built_from": built.get("built_from", "input_lineage"),
        "failures": [dict(item) for item in failures],
        "counts": dict(built.get("counts") or {}),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }

    def change(manifest: dict[str, Any]) -> None:
        manifest["analysis_csv"] = record
        manifest["analysis_csv_failures"] = [*(manifest.get("analysis_csv_failures") or []), record]

    try:
        update_manifest(Path(manifest_path), change)
    except (OSError, ValueError) as error:
        return {**record, "manifest_error": str(error)}
    return record
