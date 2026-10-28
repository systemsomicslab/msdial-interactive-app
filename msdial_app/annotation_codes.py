"""MS-DIAL's annotation codes: what each states of a feature's annotation, and what a report may count by them.

MS-DIAL writes one code per annotated feature: the "Annotation tag (VS1.0)" column of its exports and the SML
reliability column of its mzTab-M ("compound identification confidence code in MS-DIAL", MS:1003032). The table
below is MsdialCore's DataAccess.GetAnnotationCode, and 450 the code the MS-DIAL author gave an accepted match
against an in-silico (predicted) reference spectrum on 2026-10-28 (440 is the EI-MS match code). A Console from
before that decision gives such a match 430, the code of a match against an acquired reference spectrum, so the
code alone cannot tell the two apart there: what a match rests on is read from its evidence source ("Evidence
source" in the exports, opt_global_evidence_source in mzTab-M; InSilico for a predicted library), and nothing here
counts an identification by its code (workflow.parse_mdpeak counts them by evidence source).

Which Console gives which code is not told by any capability marker yet: the companion MS-DIAL change is not
merged, so a run's own output is what is read (in_silico_codes).
"""

from __future__ import annotations

from typing import Any, Iterable

IN_SILICO_ANNOTATION_CODE = 450
EI_SPECTRUM_ANNOTATION_CODE = 440
ACQUIRED_SPECTRUM_ANNOTATION_CODE = 430
UNKNOWN_ANNOTATION_CODE = 999
IN_SILICO_EVIDENCE_SOURCE = "InSilico"
IN_SILICO_CODE_DESCRIPTION = "MS/MS matched to an in-silico (predicted) reference spectrum"

ANNOTATION_CODES: dict[int, str] = {
    100: "RT, CCS and MS/MS matched; lipid acyl positions resolved",
    110: "RT, CCS and MS/MS matched; lipid acyl chains resolved",
    120: "RT, CCS and MS/MS matched; lipid class resolved",
    130: "RT, CCS, m/z and MS/MS matched",
    200: "CCS and MS/MS matched; lipid acyl positions resolved",
    210: "CCS and MS/MS matched; lipid acyl chains resolved",
    220: "CCS and MS/MS matched; lipid class resolved",
    230: "CCS, m/z and MS/MS matched",
    300: "RT and MS/MS matched; lipid acyl positions resolved",
    310: "RT and MS/MS matched; lipid acyl chains resolved",
    320: "RT and MS/MS matched; lipid class resolved",
    330: "RT, m/z and MS/MS matched",
    340: "RT or RI and EI-MS matched",
    400: "MS/MS matched; lipid acyl positions resolved",
    410: "MS/MS matched; lipid acyl chains resolved",
    420: "MS/MS matched; lipid class resolved",
    430: "m/z and MS/MS matched",
    440: "EI-MS matched",
    450: IN_SILICO_CODE_DESCRIPTION,
    500: "RT, CCS and m/z matched",
    510: "CCS and m/z matched",
    520: "RT and m/z matched",
    530: "m/z matched",
    999: "unknown",
}


def parse_annotation_code(value: Any) -> int | None:
    """A code as an export or mzTab-M writes it ("430", "430.0"); None for a blank, "null" or anything else."""
    text = str(value if value is not None else "").strip()
    if not text or text.casefold() == "null":
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if not number.is_integer():
        return None
    return int(number)


def describe_annotation_code(code: Any) -> str:
    """What MS-DIAL states by a code; "" for one this table does not know."""
    parsed = parse_annotation_code(code)
    return ANNOTATION_CODES.get(parsed, "") if parsed is not None else ""


def is_in_silico_match(code: Any, evidence_source: Any = None) -> bool:
    """Whether a feature's annotation rests on an in-silico spectrum: its evidence source InSilico, or code 450.

    The evidence source decides where it is given, so a Console that gives an in-silico match 430 still has it
    read as one; the code alone decides only where no evidence source is recorded.
    """
    source = str(evidence_source or "").strip()
    if source and source.casefold() != "null":
        return source == IN_SILICO_EVIDENCE_SOURCE
    return parse_annotation_code(code) == IN_SILICO_ANNOTATION_CODE


def in_silico_codes(rows: Iterable[tuple[Any, Any]]) -> dict[str, Any]:
    """What a run's (code, evidence source) pairs say of its in-silico matches' codes.

    codes: how many in-silico matches (is_in_silico_match) carry each code; with_code_450_not_in_silico: rows
    coded 450 whose evidence source is another; unknown_codes: codes this table does not know, and how often.
    """
    codes: dict[str, int] = {}
    contradicting = 0
    unknown: dict[str, int] = {}
    for code, source in rows:
        parsed = parse_annotation_code(code)
        if parsed is not None and parsed not in ANNOTATION_CODES:
            unknown[str(parsed)] = unknown.get(str(parsed), 0) + 1
        text = str(source or "").strip()
        if is_in_silico_match(parsed, text):
            key = str(parsed) if parsed is not None else "null"
            codes[key] = codes.get(key, 0) + 1
        elif parsed == IN_SILICO_ANNOTATION_CODE and text and text.casefold() != "null":
            contradicting += 1
    return {
        "codes": dict(sorted(codes.items())),
        "with_code_450_not_in_silico": contradicting,
        "unknown_codes": dict(sorted(unknown.items())),
    }


def in_silico_code_notes(summary: dict[str, Any]) -> list[str]:
    """Sentences on what in_silico_codes found, for a validation record or a report."""
    notes: list[str] = []
    codes = summary.get("codes") or {}
    other = {code: count for code, count in codes.items() if code != str(IN_SILICO_ANNOTATION_CODE)}
    if other:
        listed = ", ".join(f"{code} ({count})" for code, count in other.items())
        notes.append(
            f"{sum(other.values())} in-silico match(es) (evidence source InSilico) carry the code {listed}, not "
            f"{IN_SILICO_ANNOTATION_CODE} ({IN_SILICO_CODE_DESCRIPTION}): the MS-DIAL Console predates the in-silico "
            f"code, and {ACQUIRED_SPECTRUM_ANNOTATION_CODE} is the code it gives a match against an acquired reference "
            "spectrum. They are in-silico predictions, not identifications; read the evidence source, not the code."
        )
    if summary.get("with_code_450_not_in_silico"):
        notes.append(
            f"{summary['with_code_450_not_in_silico']} row(s) carry the code {IN_SILICO_ANNOTATION_CODE} "
            f"({IN_SILICO_CODE_DESCRIPTION}) but another evidence source."
        )
    if summary.get("unknown_codes"):
        listed = ", ".join(f"{code} ({count})" for code, count in summary["unknown_codes"].items())
        notes.append(f"Annotation code(s) this version does not know: {listed}.")
    return notes
