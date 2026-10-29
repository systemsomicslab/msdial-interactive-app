"""CID/HCD only: what a campaign reads of how an input was fragmented, and the inputs it keeps out for it.

THE RULE (user decision, 2026-10-29; CID_HCD_ONLY_RULE). Every reference spectrum the campaign annotates with is
CID/HCD-derived, so an input fragmented by another method - EAD, OAD, ECD, ETD, EThcD, EID, UVPD, IRMPD or the like
- is excluded, with a recorded reason, where evidence names that method. Absence of evidence means run: nothing
here infers a method, and an input whose method nothing names runs.

WHAT COUNTS AS EVIDENCE OF AN INPUT, here (the campaign plan reads the repository record of the whole unit):

- its file name: a whole token of the base name naming another method (methods_named): MB-POST MPST000010
  names its OAD files "..._OAD_NEG_ddMS_10eV_..." and its EAD files "..._EAD14eV_POS_...". A token is whole when
  no letter or digit stands before it and no letter after it, and a run of eight or more hexadecimal digits (a
  checksum, a GUID segment such as 8ead4b2c) names nothing. A token counts only where the unit's instrument could
  fragment so (instrument_can, METHOD_INSTRUMENTS), or where no instrument is recorded; elsewhere it is recorded as
  not credible on that instrument and keeps nothing out (user decision, 2026-10-29: MTBLS2372's "AA_EAD_A_1.raw"
  on a Thermo Exactive is a sample code). The instrument's model name is never evidence of a method itself: an
  "LTQ Orbitrap Velos ETD" says what the instrument can do, not what was done;
- an mzML's activation cvParams, as the encoding scan reads them over its first spectra (mzml_encoding.
  scan_mzml_encoding, activation): a dissociation-method term of another method (classify_activation_term);
- a converted mzXML's activationMethod attributes, as its conversion record counts them
  (precursors.activation_methods);
- a row of the input's sample that states the method in a field that states one (stated_methods: "Fragmentation
  method: OAD"), an explicit statement no instrument is asked to confirm. The campaign plan excludes a unit every
  row of which states it, and plans one only some rows of which do, mapping those rows to their files; the lease
  keeps the same files out (evidence sample_row).

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
    "EID, UVPD, IRMPD or the like) is excluded; absence of evidence means run. A file name counts only where the "
    "unit's instrument could fragment so, or no instrument is recorded; an instrument's model name is no evidence."
)
# The evidence an exclusion names (excluded_input_candidates[].evidence).
EVIDENCE_FILE_NAME = "file_name"
EVIDENCE_MZML_ACTIVATION = "mzml_activation"
EVIDENCE_MZXML_ACTIVATION = "mzxml_activation_method"
# A sample row of the input's sample that states the method ("Fragmentation method: OAD"), where not every row does:
# the campaign plan maps such rows to their files and plans the unit, and the lease keeps those files out.
EVIDENCE_SAMPLE_ROW = "sample_row"
# THE TOKEN RULE, held identical in Interactive (fragmentation.py), the campaign plan and the gate. A token is whole
# when no letter or digit stands before it and no letter after it. It names nothing inside an identifier: a run of
# letters and digits that is all hexadecimal and either eight long or more or holds a digit (8ead4b2c, ead1), or a
# hyphenated GUID (95c44b45-ead1-43e8-afd1-de952d13d4ae). With words, repository text may spell a method out.
NON_CID_METHODS = ("EAD", "OAD", "ECD", "ETD", "EThcD", "ETciD", "NETD", "EDD", "EID", "UVPD", "IRMPD")
GUID_PATTERN = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
METHOD_WORDS_PATTERN = (
    r"electron[- ]activated dissociation|oxygen[- ]attachment dissociation|electron[- ]capture dissociation|"
    r"electron[- ]transfer dissociation|electron[- ]induced dissociation|ultraviolet photodissociation|"
    r"infrared multiphoton dissociation"
)
WORD_METHODS = {"electron activated": "EAD", "oxygen attachment": "OAD", "electron capture": "ECD",
                "electron transfer": "ETD", "electron induced": "EID", "ultraviolet photodissociation": "UVPD",
                "infrared multiphoton": "IRMPD"}
# A sample-row field that states how the ions were fragmented ("analyticalCondition / Fragmentation method"): an
# explicit statement, held to no instrument and never read as the study's text. An instrument field names a model,
# which says what the instrument can do, never what was done, and is no evidence at all.
METHOD_STATEMENT_FIELD_PATTERN = r"fragment|dissociation|activation|collision"
INSTRUMENT_FIELD_PATTERN = r"instrument"
_CANONICAL_METHOD = {name.casefold(): name for name in NON_CID_METHODS}
_METHOD_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])(" + "|".join(sorted(NON_CID_METHODS, key=len, reverse=True)) + r")(?![A-Za-z])", re.IGNORECASE)
_METHOD_WORDS = re.compile(METHOD_WORDS_PATTERN, re.IGNORECASE)
_ALNUM_RUN = re.compile(r"[A-Za-z0-9]+")
_ALL_HEX = re.compile(r"[0-9A-Fa-f]+")
_GUID = re.compile(GUID_PATTERN)
_METHOD_STATEMENT_FIELD = re.compile(METHOD_STATEMENT_FIELD_PATTERN, re.IGNORECASE)
_INSTRUMENT_FIELD = re.compile(INSTRUMENT_FIELD_PATTERN, re.IGNORECASE)
# The words that make a dissociation-method term another method's (classify_activation_term), and those that make
# it collisional. A term with neither (sustained off-resonance irradiation) is recorded and decides nothing.
_OTHER_METHOD_WORDS = ("electron", "photo", "infrared", "ultraviolet", "oxygen", "ozone", "radical",
                       "surface-induced", "surface induced", "blackbody")
_COLLISIONAL_WORDS = ("collision", "pulsed q")
COLLISIONAL_ACTIVATION_TEXTS = frozenset({"cid", "hcd", "cad", "pqd", "beam-type cid", "trap-type cid"})
# WHICH INSTRUMENTS CAN FRAGMENT SO (user decision, 2026-10-29). A file name's token counts only where the unit's
# instrument matches the method's capable families, or names a capable vendor and no model (a model is a word with a
# digit, or one of INSTRUMENT_MODEL_WORDS), or no instrument is recorded (UNRECORDED_INSTRUMENTS). The campaign
# runner's plan and gate hold the same table, and their tests hold it identical to this one.
_TRIBRID = r"fusion|lumos|eclipse|ascend|tribrid"
_FTICR = r"ft\s*-?\s*icr|fticr|solarix|\bapex\b"
_ETD_CAPABLE = _TRIBRID + r"|\betd\b"
METHOD_INSTRUMENTS = {
    "EAD": {"capable": r"zeno\s*-?\s*tof|\b[78]600\b", "vendors": r"sciex|applied biosystems"},
    "OAD": {"capable": r"lcms\s*-?\s*90[35]0|\b90[35]0\b", "vendors": r"shimadzu"},
    "ETD": {"capable": _ETD_CAPABLE, "vendors": r"thermo|bruker|agilent"},
    "EThcD": {"capable": _ETD_CAPABLE, "vendors": r"thermo|bruker|agilent"},
    "ETciD": {"capable": _ETD_CAPABLE, "vendors": r"thermo|bruker|agilent"},
    "NETD": {"capable": _ETD_CAPABLE, "vendors": r"thermo|bruker|agilent"},
    "ECD": {"capable": _FTICR + r"|\bexd\b|\becd\b|6545\s*xt", "vendors": r"bruker|agilent"},
    "EDD": {"capable": _FTICR, "vendors": r"bruker"},
    "EID": {"capable": _FTICR + r"|\bexd\b", "vendors": r"bruker|agilent"},
    "UVPD": {"capable": _TRIBRID + r"|\buvpd\b", "vendors": r"thermo"},
    "IRMPD": {"capable": _FTICR + r"|ion\s*trap|\bltq\b|\blcq\b|amazon|esquire|\bhct\b|\bqtrap\b|\birmpd\b",
              "vendors": r"bruker|thermo"},
}
INSTRUMENT_MODEL_WORDS = (
    r"\d|exactive|orbitrap|tripletof|qtrap|synapt|xevo|vion|impact|maxis|timstof|compact|\bltq\b|\blcq\b|velos|"
    r"elite|fusion|lumos|eclipse|ascend|exploris|astral|zeno|solarix|\bapex\b|q-?tof|\btof\b|ion\s*trap|amazon|"
    r"esquire|\bhct\b|fticr|ft-icr"
)
UNRECORDED_INSTRUMENTS = ("", "unknown", "not available", "n/a", "na", "none", "-", "ms", "lc-ms", "lc/ms", "lcms",
                          "mass spectrometer", "mass spectrometry")


def instrument_can(method: str, instrument: Any) -> bool:
    """Whether a file name's method token is credible on this instrument (METHOD_INSTRUMENTS); True where none is
    recorded, or the method has no row."""
    text = " ".join(str(instrument or "").split())
    if text.casefold().strip(" .") in UNRECORDED_INSTRUMENTS:
        return True
    row = METHOD_INSTRUMENTS.get(method)
    if row is None or re.search(row["capable"], text, re.IGNORECASE):
        return True
    return bool(re.search(row["vendors"], text, re.IGNORECASE)) and not re.search(
        INSTRUMENT_MODEL_WORDS, text, re.IGNORECASE)


# What the record says of an input whose raw header was not read for its method, by format.
RAW_HEADER_NOT_READ = "not_read_from_raw_header"
RAW_HEADER_NOT_READ_DETAIL = (
    "The dissociation method was not read from the raw header: no reader used here reports it for this format "
    "(RawMetadataExtractor records none, and the SCIEX Wiff2Reader writes CID for every spectrum whatever the "
    "instrument did, so a wiff2 CID is no evidence). Nothing is inferred; only the file name and the repository "
    "record can name another method."
)


def methods_named(text: object, words: bool = False) -> list[str]:
    """The non-CID/HCD methods a text names by a whole token (and, with `words`, by a spelled-out name), in order and
    once each; [] for none."""
    value = str(text or "")
    found: list[str] = []
    guids = [(item.start(), item.end()) for item in _GUID.finditer(value)]
    for match in _METHOD_TOKEN.finditer(value):
        if any(start <= match.start() < end for start, end in guids):
            continue
        run = next((item for item in _ALNUM_RUN.finditer(value) if item.start() <= match.start() < item.end()), None)
        if run is not None and _ALL_HEX.fullmatch(run.group(0)) and (
                len(run.group(0)) >= 8 or any(character.isdigit() for character in run.group(0))):
            continue
        name = _CANONICAL_METHOD[match.group(1).casefold()]
        if name not in found:
            found.append(name)
    if words:
        for match in _METHOD_WORDS.finditer(value):
            spelled = re.sub(r"[- ]+", " ", match.group(0).casefold())
            name = next(code for start, code in WORD_METHODS.items() if spelled.startswith(start))
            if name not in found:
                found.append(name)
    return found


def methods_in_file_name(path: object) -> list[str]:
    """methods_named over a path's base name (a vendor folder's own name)."""
    text = str(path or "").rstrip("\\/")
    return methods_named(re.split(r"[\\/]", text)[-1] if text else "")


def stated_methods(values: object) -> list[str]:
    """The non-CID/HCD methods a sample row states in a field that states the method (METHOD_STATEMENT_FIELD_PATTERN),
    an instrument field aside; [] for none."""
    found: list[str] = []
    for field, value in (values.items() if isinstance(values, dict) else []):
        if _INSTRUMENT_FIELD.search(str(field)) or not _METHOD_STATEMENT_FIELD.search(str(field)):
            continue
        for name in methods_named(value, words=True):
            if name not in found:
                found.append(name)
    return found


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
    instrument: str = "",
    stated: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    """(the inputs that run, those kept out, the fragmentation_check record) under the rule.

    ``mzml_scans`` maps an input's key (``key(path)``, the path itself by default) to its mzML encoding scan;
    ``mzxml_activation`` maps a converted mzML's key to the activationMethod counts of the mzXML it was written
    from. An input is kept out where its file name, its mzML activation terms or its mzXML activation methods name
    another method; each exclusion records the methods, the evidence and the rule. A file name's method counts only
    where ``instrument`` (the unit's, as its Catalog handoff records it) could fragment so (instrument_can); one it
    could not is recorded as not credible on that instrument and keeps nothing out. ``stated`` maps an input's key
    to {"sample_id", "methods"} where its sample's row states another method (stated_methods): an explicit
    statement, held to no instrument. Never raises.
    """
    stated = stated or {}
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
    not_credible: list[dict[str, Any]] = []
    count = 0
    for path in inputs:
        count += 1
        text = str(path)
        evidence: list[tuple[str, list[str], str]] = []
        named = methods_in_file_name(text)
        credible = [method for method in named if instrument_can(method, instrument)]
        if credible:
            evidence.append((EVIDENCE_FILE_NAME, credible, f"its file name names {', '.join(credible)}"))
        if len(credible) < len(named):
            not_credible.append({"name": Path(text.rstrip("\\/")).name,
                                 "methods": [method for method in named if method not in credible]})
        row = stated.get(key(text))
        if row and row.get("methods"):
            methods = [str(item) for item in row["methods"]]
            sample = str(row.get("sample_id") or "the input's")
            evidence.append((EVIDENCE_SAMPLE_ROW, methods, f"sample {sample}'s row states {', '.join(methods)}"))
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
        # The instrument a name's method was held to, and the names whose method it could not perform: recorded, and
        # kept out of nothing.
        "instrument": str(instrument or ""),
        **(
            {"names_not_credible_on_instrument": {"count": len(not_credible), "examples": not_credible[:10]}}
            if not_credible
            else {}
        ),
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
