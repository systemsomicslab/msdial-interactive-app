"""The MS-DIAL Console's lipid retention-time order check (MsdialWorkbench#832): the setting a run asks for,
the method-file keys that say so, and what the run left to show that it ran.

The check reads every lipid name an alignment's representatives carry, LBM2 and MSP matches alike, and tags
the ones whose retention times follow the reversed-phase rules (more carbons later, more double bonds earlier)
"Confirmed". It is on by default in a Lipidomics project. A campaign writes every one of its seven keys, so a
later change of a Console default cannot change what the campaign ran without the method file saying so (the
author's decision of 2026-10-25, Q5). The Console writes "<alignment stem>_lipidrt_review.tsv" next to the
alignment exports whenever the check ran, also when it listed nothing, so a missing review list means the
check did not run, or failed and withdrew what it had written. An empty list alone does not say the order was
evaluated: where the run-level reversed-phase gate is not passed no lipid is evaluated, and only the Console's
summary line says so (console_summary).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Iterable

LIPID_RT_CHECK_CAPABILITY = "lipid_rt_check"
# The lowercase key the Console's ConfigParser reads (LipidRtCheckLine) and the line its LC-MS process writes
# with the review list (LcmsProcess.WriteLipidRtReview), both in the Console assembly. Every marker is required.
LIPID_RT_CHECK_CONSOLE_MARKERS = ("lipid rt check operating point", "Lipid RT check review list: ")
# Each setting: its answer key, its method-file key, its choices as the Console names them, and the default
# the Console applies when the key is absent (LipidRtCheckParameter; "Auto" runs the check when Target omics
# is Lipidomics, and "True" does not make it run for another target omics).
SETTINGS: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    ("mode", "Lipid RT check", ("Auto", "True", "False"), "Auto"),
    ("operating_point", "Lipid RT check operating point", ("Strict", "HighPrecision", "Mid", "Balanced"),
     "HighPrecision"),
    ("chromatography", "Lipid RT check chromatography", ("Auto", "ReversedPhase", "Other"), "Auto"),
    ("plausibility_profile", "Lipid RT check plausibility profile", ("None", "Mammalian"), "None"),
    ("class_level_matches", "Lipid RT check class-level matches", ("Consistent", "Corroborated", "Never"),
     "Corroborated"),
    ("minor_duplicates", "Lipid RT check minor duplicates", ("Allow", "Block"), "Allow"),
    ("secondary_only_features", "Lipid RT check secondary-only features", ("Evaluate", "Block"), "Evaluate"),
)
DEFAULTS = {answer: default for answer, _, _, default in SETTINGS}
# Lowercase, as console_method_key reads a line, to the title the Console's own method template writes.
METHOD_TITLES = {key.casefold(): key for _, key, _, _ in SETTINGS}
REVIEW_SUFFIX = "_lipidrt_review.tsv"
SUMMARY_PREFIX = "Lipid RT check (alignment): "
REVIEW_LINE_PREFIX = "Lipid RT check review list: "
# "...; rules 1.0, parameter 1a2b3c4d." at the end of the summary line (LipidRtUnitRun.Describe).
_RULES = re.compile(r"\brules ([^\s,;]+), parameter ([0-9A-Za-z]+)")


def read_setting(answer: Any) -> tuple[dict[str, str] | None, list[str]]:
    """The lipid_rt_check answer as the seven settings, each key it leaves out at its default; None for no answer.

    The answer is an object of the keys in SETTINGS, or the mode alone. A value is one of the choices the
    Console names, in any letter case, as the Console reads it; True and False are the mode's. Anything else
    is a problem, never a guess: the Console would refuse the line and run its default while the records said
    otherwise.
    """
    if answer is None:
        return None, []
    if isinstance(answer, (str, bool)):
        answer = {"mode": answer}
    if not isinstance(answer, dict):
        return None, ["lipid_rt_check is an object of " + ", ".join(DEFAULTS) + ", or the mode alone."]
    problems = [
        f"lipid_rt_check.{key} is not a setting of the lipid RT check; it has " + ", ".join(DEFAULTS) + "."
        for key in sorted(set(answer) - set(DEFAULTS))
    ]
    setting: dict[str, str] = {}
    for key, _, choices, default in SETTINGS:
        value = answer.get(key, default)
        text = ("True" if value else "False") if isinstance(value, bool) else str(value or "").strip()
        chosen = next((choice for choice in choices if choice.casefold() == text.casefold()), None)
        if chosen is None:
            problems.append(f"lipid_rt_check.{key} '{value}' is none of {', '.join(choices)}.")
            chosen = default
        setting[key] = chosen
    return (None if problems else setting), problems


def template_values(lines: Iterable[str]) -> dict[str, str]:
    """A parameter template's lipid RT check lines by lowercase key, as the Console reads them.

    The Console's ConfigParser reads a key's last line with a value (ReadLast): a blank value is skipped, so the
    key keeps an earlier line's value or its default. workflow.parse_method keeps the last line whatever its
    value, which would read a blank as a value the Console refuses, or let a later blank line hide a usable one.
    """
    values: dict[str, str] = {}
    for line in lines:
        if line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        if key in METHOD_TITLES and value.strip():
            values[key] = value.strip()
    return values


def template_setting(values: dict[str, str]) -> dict[str, Any]:
    """A parameter template's own lipid RT check lines, read as the answer is, for load_parameter_template.

    ``values`` is the template's lines by lowercase key (template_values). A template the Console wrote (its
    MethodFileTemplate) or a run's own method.txt carries all seven keys; a template with none gives {}. One
    with any is the check's setting, as lipid_rt_check, so it is written and recorded as an answer's is; one the
    Console would refuse is lipid_rt_check_template_problems, which validate_workflow refuses. Either way the
    template's lines are never copied through to a method file (workflow._write_method). A blank value is no
    line at all, as the Console reads it: the key is left at its default.
    """
    given = {
        answer: values[key.casefold()] for answer, key, _, _ in SETTINGS
        if str(values.get(key.casefold()) or "").strip()
    }
    if not given:
        return {}
    setting, _ = read_setting(given)
    if setting is None:
        return {
            "lipid_rt_check_template_problems": [
                f"The parameter template's '{key}' is '{values[key.casefold()]}', none of {', '.join(choices)}."
                for answer, key, choices, _ in SETTINGS
                if answer in given and read_setting({answer: given[answer]})[0] is None
            ]
        }
    return {"lipid_rt_check": setting}


def method_replacements(setting: dict[str, str]) -> dict[str, str]:
    """The seven method-file keys, lowercase as the method writer keys its replacements, with their values."""
    return {key.casefold(): setting[answer] for answer, key, _, _ in SETTINGS}


def method_record(setting: dict[str, str] | None, capabilities: Iterable[str] | None) -> dict[str, Any]:
    """What a prepared run says of the check: the setting asked for, and whether the method file states it.

    The keys are written only for a Console with the check (LIPID_RT_CHECK_CAPABILITY): an older one has no
    check to set and would report every key as unrecognised. A plan that asks for the check on a Console known
    to lack it is refused (workflow.validate_workflow); with no Console to probe, ``capabilities`` is None.
    """
    found = None if capabilities is None else LIPID_RT_CHECK_CAPABILITY in set(capabilities)
    written = bool(setting) and found is True
    if not setting:
        # Nothing is known here of what the Console does by default, nor whether it has the check at all: a
        # Console from before MsdialWorkbench#832 has none. The run's review list shows whether it ran.
        reason = "not set, so no key was written; whether the Console ran the check is what a review list shows"
    elif written:
        reason = "every key written to the method file"
    elif found is None:
        reason = "no Console to probe, so no key was written"
    else:
        reason = "the Console has no lipid RT check, so no key was written"
    return {
        "setting": dict(setting) if setting else None,
        "written_to_method_file": written,
        "method_keys": [METHOD_TITLES[key] for key in method_replacements(setting)] if written else [],
        "console_capability": found,
        "reason": reason,
        "review_file_pattern": "<alignment result>" + REVIEW_SUFFIX,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def review_files(directories: Iterable[str | Path], since: float | None = None) -> list[dict[str, Any]]:
    """Each review list in the directories, by name, sha256 and row count, written at or after ``since``.

    ``since`` is when the run's method file was written: a review list older than it is an earlier run's.
    """
    found: dict[str, dict[str, Any]] = {}
    for directory in directories:
        folder = Path(str(directory or "")).expanduser()
        if not str(directory or "").strip() or not folder.is_dir():
            continue
        for path in sorted(folder.glob("*" + REVIEW_SUFFIX)):
            try:
                if not path.is_file() or (since is not None and path.stat().st_mtime < since):
                    continue
                key = str(path.resolve()).casefold()
                if key in found:
                    continue
                with path.open(encoding="utf-8-sig", errors="replace") as handle:
                    rows = max(sum(1 for line in handle if line.strip()) - 1, 0)
                found[key] = {"name": path.name, "sha256": _sha256(path), "rows": rows}
            except OSError:
                continue
    return list(found.values())


def console_summary(lines: Iterable[str] | None) -> dict[str, str]:
    """The Console's last summary line of the check, and the rule version and parameter hash it states."""
    summary = ""
    for line in lines or []:
        text = str(line or "")
        index = text.find(SUMMARY_PREFIX)
        if index >= 0:
            summary = text[index + len(SUMMARY_PREFIX):].strip()
    match = _RULES.search(summary)
    return {
        "summary": summary,
        "rule_version": match.group(1) if match else "",
        "parameter_hash": match.group(2) if match else "",
    }


def summary_outcome(summary: str) -> str:
    """What the Console's summary line (LipidRtUnitRun.Describe) says the check did, or "" without one.

    "failed": it stopped with an exception and withdrew its verdicts and tags, and wrote no review list.
    "not_run": it did not run. "gate_not_passed": it ran, but its run-level reversed-phase gate was not passed,
    so no lipid was evaluated or tagged Confirmed; its review list is still written. "evaluated": it ran and
    evaluated the eligible lipids.
    """
    text = str(summary or "").strip()
    if not text:
        return ""
    if text.startswith("failed"):
        return "failed"
    if text.startswith("not run"):
        return "not_run"
    if "Reversed-phase gate not passed" in text:
        return "gate_not_passed"
    return "evaluated"


def run_outcome(
    directories: Iterable[str | Path], since: float | None = None, console_lines: Iterable[str] | None = None
) -> dict[str, Any]:
    """What a finished run left of the check: its review lists, and what the Console said of it.

    ran is true only where a review list exists: the Console writes one, also empty, whenever the check ran.
    Whether it evaluated anything is the summary line's to say (summary_outcome).
    """
    files = review_files(directories, since)
    return {
        "ran": bool(files),
        "review_files": files,
        **console_summary(console_lines),
        "note": (
            "A review list is written whenever the check ran, also when it evaluated nothing; the Console's "
            "summary line says whether its reversed-phase gate was passed."
            if files else "No review list was written: the check did not run, or failed and withdrew what it had "
            "written."
        ),
    }
