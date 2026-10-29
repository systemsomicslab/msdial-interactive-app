"""CID/HCD only: what a campaign reads of how an input was fragmented, and the inputs it keeps out for it.

THE RULE (user decision, 2026-10-29; CID_HCD_ONLY_RULE). Every reference spectrum the campaign annotates with is
CID/HCD-derived, so an input fragmented by another method - EAD, OAD, ECD, ETD, EThcD, EID, UVPD, IRMPD or the like
- is excluded, with a recorded reason, where evidence names that method. Absence of evidence means run: nothing
here infers a method, and an input whose method nothing names runs.

WHAT COUNTS AS EVIDENCE OF AN INPUT, here (the campaign plan reads the repository record of the whole unit):

- its file name: a whole token of the base name naming another method (methods_named): MB-POST MPST000010
  names its OAD files "..._OAD_NEG_ddMS_10eV_..." and its EAD files "..._EAD14eV_POS_...". A token is whole when
  no letter or digit stands before it and no letter after it, and a run of eight or more hexadecimal digits (a
  checksum, a GUID segment such as 8ead4b2c) names nothing;
- an mzML's activation cvParams, as the encoding scan reads them over its first spectra (mzml_encoding.
  scan_mzml_encoding, activation): a dissociation-method term of another method (classify_activation_term);
- a converted mzXML's activationMethod attributes, as its conversion record counts them
  (precursors.activation_methods).

WHAT IS NOT READ. No vendor reader here reports the method. RawMetadataExtractor records none for Thermo, Shimadzu,
Waters, Agilent or SCIEX files, and the SCIEX Wiff2Reader in RawDataHandler writes DissociationMethods.CID for
every spectrum whatever the instrument did (a ZenoTOF 7600 can acquire EAD), so a wiff2 "CID" is no evidence of
anything. For those formats the record says the method was not read from the raw header (RAW_HEADER_NOT_READ),
and only the file name and the repository record can speak.

ONLY IN A CAMPAIGN. Outside one, an EAD or OAD analysis is a choice the analyst made, and MS-DIAL has workflows
for it; nothing here is applied.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePath
from typing import Any, Iterable, Mapping

CID_HCD_ONLY_RULE = "cid_hcd_only_2026_10_29"
# The reason an input is kept out (excluded_input_candidates), and a unit nothing of which is left.
NOT_CID_HCD = "fragmentation_not_cid_hcd"
FRAGMENTATION_CHECK_SCHEMA = "msdial-fragmentation-check.v1"
RULE_STATEMENT = (
    "CID/HCD only (user decision, 2026-10-29): every reference spectrum the campaign annotates with is "
    "CID/HCD-derived, so an input whose evidence names another fragmentation method (EAD, OAD, ECD, ETD, EThcD, "
    "EID, UVPD, IRMPD or the like) is excluded; absence of evidence means run."
)
# The evidence an exclusion names (excluded_input_candidates[].evidence).
EVIDENCE_FILE_NAME = "file_name"
EVIDENCE_MZML_ACTIVATION = "mzml_activation"
EVIDENCE_MZXML_ACTIVATION = "mzxml_activation_method"
# The methods a name may give, as they are written, keyed by their casefolded spelling. CID, HCD, CAD and PQD are
# collisional and name nothing against an input.
NON_CID_METHODS = ("EAD", "OAD", "ECD", "ETD", "EThcD", "ETciD", "NETD", "EDD", "EID", "UVPD", "IRMPD")
_CANONICAL = {name.casefold(): name for name in NON_CID_METHODS}
_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])(" + "|".join(sorted(NON_CID_METHODS, key=len, reverse=True)) + r")(?![A-Za-z])",
    re.IGNORECASE,
)
_ALNUM_RUN = re.compile(r"[A-Za-z0-9]+")
_HEX = re.compile(r"[0-9A-Fa-f]{8,}")
# The words that make a dissociation-method term another method's (classify_activation_term), and those that make
# it collisional. A term with neither (sustained off-resonance irradiation) is recorded and decides nothing.
_OTHER_METHOD_WORDS = ("electron", "photo", "infrared", "ultraviolet", "oxygen", "ozone", "radical",
                       "surface-induced", "surface induced", "blackbody")
_COLLISIONAL_WORDS = ("collision", "pulsed q")
COLLISIONAL_ACTIVATION_TEXTS = frozenset({"cid", "hcd", "cad", "pqd", "beam-type cid", "trap-type cid"})
# What the record says of an input whose raw header was not read for its method, by format.
RAW_HEADER_NOT_READ = "not_read_from_raw_header"
RAW_HEADER_NOT_READ_DETAIL = (
    "The dissociation method was not read from the raw header: no reader used here reports it for this format "
    "(RawMetadataExtractor records none, and the SCIEX Wiff2Reader writes CID for every spectrum whatever the "
    "instrument did, so a wiff2 CID is no evidence). Nothing is inferred; only the file name and the repository "
    "record can name another method."
)


def methods_named(text: str) -> list[str]:
    """The non-CID/HCD methods a name names by a whole token, in order and once each; [] for none."""
    found: list[str] = []
    value = str(text or "")
    for match in _TOKEN.finditer(value):
        run = next(
            (item for item in _ALNUM_RUN.finditer(value) if item.start() <= match.start() < item.end()), None
        )
        if run is not None and _HEX.fullmatch(run.group(0)):
            # A checksum or an identifier (8ead4b2c), not a word.
            continue
        name = _CANONICAL[match.group(1).casefold()]
        if name not in found:
            found.append(name)
    return found


def methods_in_file_name(path: str | PurePath) -> list[str]:
    """methods_named over the base name of a path (a vendor folder's own name)."""
    text = str(path).rstrip("\\/")
    return methods_named(re.split(r"[\\/]", text)[-1] if text else "")


def classify_activation_term(name: str) -> str:
    """"other_method", "collisional" or "unclassified", for an mzML dissociation-method term by its name."""
    text = str(name or "").casefold()
    if any(word in text for word in _OTHER_METHOD_WORDS):
        return "other_method"
    if any(word in text for word in _COLLISIONAL_WORDS):
        return "collisional"
    return "unclassified"


def classify_activation_text(text: str) -> str:
    """The same for an mzXML activationMethod value (CID, HCD, ETD, EThcD, ...)."""
    value = str(text or "").strip()
    if methods_named(value):
        return "other_method"
    if value.casefold() in COLLISIONAL_ACTIVATION_TEXTS:
        return "collisional"
    return "unclassified"


def input_format_label(path: str) -> str:
    """The input's format as the record counts it: its suffix, casefolded (".wiff2", ".d", ".mzml")."""
    return Path(str(path).rstrip("\\/")).suffix.casefold() or "(none)"


def exclude_non_cid_inputs(
    inputs: Iterable[str],
    mzml_scans: Mapping[str, Mapping[str, Any]] | None = None,
    mzxml_activation: Mapping[str, Mapping[str, int]] | None = None,
    key: Any = None,
) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    """(the inputs that run, those kept out, the fragmentation_check record) under the rule.

    ``mzml_scans`` maps an input's key (``key(path)``, the path itself by default) to its mzML encoding scan;
    ``mzxml_activation`` maps a converted mzML's key to the activationMethod counts of the mzXML it was written
    from. An input is kept out where its file name, its mzML activation terms or its mzXML activation methods name
    another method; each exclusion records the methods, the evidence and the rule. Never raises.
    """
    key = key or (lambda value: value)
    mzml_scans = mzml_scans or {}
    mzxml_activation = mzxml_activation or {}
    kept: list[str] = []
    excluded: list[dict[str, Any]] = []
    by_evidence: dict[str, int] = {}
    header_read: dict[str, int] = {}
    header_not_read: dict[str, int] = {}
    collisional_terms: dict[str, int] = {}
    unclassified_terms: dict[str, int] = {}
    count = 0
    for path in inputs:
        count += 1
        text = str(path)
        evidence: list[tuple[str, list[str], str]] = []
        named = methods_in_file_name(text)
        if named:
            evidence.append((EVIDENCE_FILE_NAME, named, f"its file name names {', '.join(named)}"))
        label = input_format_label(text)
        scan = mzml_scans.get(key(text))
        activation = mzxml_activation.get(key(text))
        if scan is not None or activation is not None:
            header_read[label] = header_read.get(label, 0) + 1
        else:
            header_not_read[label] = header_not_read.get(label, 0) + 1
        if scan is not None:
            other = []
            for term in scan.get("activation") or []:
                kind = classify_activation_term(str(term.get("name") or ""))
                name = str(term.get("name") or term.get("accession") or "")
                if kind == "other_method":
                    other.append(name)
                elif kind == "collisional":
                    collisional_terms[name] = collisional_terms.get(name, 0) + int(term.get("count") or 0)
                else:
                    unclassified_terms[name] = unclassified_terms.get(name, 0) + int(term.get("count") or 0)
            if other:
                evidence.append((EVIDENCE_MZML_ACTIVATION, other, "its mzML activation names " + ", ".join(other)))
        if activation is not None:
            other = [str(name) for name in activation if classify_activation_text(str(name)) == "other_method"]
            if other:
                evidence.append(
                    (EVIDENCE_MZXML_ACTIVATION, other, "its mzXML's activationMethod gives " + ", ".join(other))
                )
        if not evidence:
            kept.append(text)
            continue
        first = evidence[0][0]
        by_evidence[first] = by_evidence.get(first, 0) + 1
        excluded.append({
            "path": text,
            "reason": NOT_CID_HCD,
            "rule": CID_HCD_ONLY_RULE,
            "evidence": first,
            "methods": list(dict.fromkeys(method for _source, methods, _said in evidence for method in methods)),
            "detail": "; ".join(said for _source, _methods, said in evidence)
            + ". Every reference spectrum is CID/HCD-derived (user decision, 2026-10-29).",
        })
    record = {
        "schema": FRAGMENTATION_CHECK_SCHEMA,
        "rule": CID_HCD_ONLY_RULE,
        "statement": RULE_STATEMENT,
        "inputs_checked": count,
        "excluded": len(excluded),
        "excluded_by_evidence": dict(sorted(by_evidence.items())),
        # Where the method was read from the file itself (an mzML's activation, a converted mzXML's), by format,
        # and where it was not: never inferred.
        "raw_header": {
            "read": dict(sorted(header_read.items())),
            RAW_HEADER_NOT_READ: dict(sorted(header_not_read.items())),
            **({"not_read_detail": RAW_HEADER_NOT_READ_DETAIL} if header_not_read else {}),
            **({"collisional_terms": dict(sorted(collisional_terms.items()))} if collisional_terms else {}),
            **({"unclassified_terms": dict(sorted(unclassified_terms.items()))} if unclassified_terms else {}),
        },
    }
    return kept, excluded, record
