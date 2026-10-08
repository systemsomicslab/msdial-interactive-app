"""Which one of a sample's encodings is analysed: the user's one rule of 2026-10-09.

THE RULE ("A: この一つのルールで統一", 2026-10-09). It supersedes every earlier case-by-case answer about a sample
whose data arrive more than once (the readable twin of an undecodable mzML, the encoding order between archive
members, an admitted encoding beside an unpaired one, copies in several folders). When one sample's data arrive in
several encodings - S1.raw, S1.mzML, S1.mzXML, copies in other folders included:

1. Among the READABLE ones exactly one is used: the highest in the order vendor format (a folder or a container)
   -> mzML -> mzXML. An mzXML is converted to mzML in a campaign; outside a campaign it is no input.
2. Ties (the same rank: one format in two folders, or two vendor formats) go to the first by path name in
   lexicographic order: the path relative to the unit's data root, '/'-separated, compared without case.
3. If the chosen one cannot be read or decoded, or its conversion fails, the next in order is taken.
4. The file used is that sample's own input, paired to its sample row and in its Class, whatever encoding the
   sample row names.
5. Every file not used is recorded with its reason, naming the file that was used.

choose_encoding is the rule, and the one place it is decided: the lease groups the candidates of each sample
(repository_reanalysis._encoding_groups) and asks it once per sample, and every later reader (the lineage, the
analysis-CSV builder, a split part) reads what it decided from the record it returns (EncodingChoice.record).

WHAT "READABLE" IS, AT THE LEASE. The lease knows three facts about a candidate, and the caller's ``readability``
says them: an mzML whose arrays RawDataHandler cannot decode (mzml_encoding) is UNDECODABLE; an mzXML whose
conversion failed, or whose scans contradict the declared polarity, is not readable (CONVERSION_FAILED, or the
convert stage's own reason); an mzXML outside a campaign is no input (REQUIRES_CONVERSION). A vendor file or folder
is taken as readable: whether its header can be read is the raw-header preflight's to say, after the lease.

Readability is asked lazily, in the rule's order, only until the first readable candidate: an mzXML is converted
only where nothing above it can be read, and an mzML is scanned only where no vendor encoding is taken.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from . import encoding_preference

RULE = "one_encoding_per_sample_2026_10_09"

# The rule's order (clause 1). Anything else is no encoding the rule ranks.
VENDOR_RANK = 0
MZML_RANK = 1
MZXML_RANK = 2
_UNRANKED = 9

# Why a candidate is not used (clause 5). A candidate after the one used is lower in the order or tied with it; one
# before it could not be read, for the reason readability gave.
LOWER_IN_ENCODING_ORDER = "lower_in_encoding_order"
TIE_LEXICOGRAPHIC = "tie_lexicographic"
UNDECODABLE = "undecodable"
CONVERSION_FAILED = "conversion_failed"
REQUIRES_CONVERSION = "requires_conversion"


def encoding_rank(path: str) -> int | None:
    """The rule's rank of a path's encoding: 0 vendor (a folder or container), 1 mzML, 2 mzXML; None otherwise."""
    kind = encoding_preference.kind(path)
    if kind == "vendor":
        return VENDOR_RANK
    if kind == "open":
        return MZML_RANK
    if kind == "convertible":
        return MZXML_RANK
    return None


def _slashes(path: str) -> str:
    return str(path or "").replace("\\", "/")


def order_key(relative: str) -> tuple[int, str, str]:
    """Where a candidate stands in the rule's order: by rank (clause 1), then by path without case (clause 2)."""
    text = _slashes(relative)
    rank = encoding_rank(text)
    return (_UNRANKED if rank is None else rank, text.casefold(), text)


@dataclass(frozen=True)
class EncodingChoice:
    """What the rule decided for one sample: the candidate used (None where none could be read), and every other
    candidate with its reason, in the rule's order. Candidates are the caller's own keys; ``paths`` gives each its
    path relative to the data root, as the record names it."""

    used: str | None
    unused: tuple[tuple[str, str], ...]
    paths: Mapping[str, str]

    @property
    def candidates(self) -> list[str]:
        return [*([self.used] if self.used is not None else []), *(item for item, _reason in self.unused)]

    def record(self) -> dict[str, Any]:
        """The choice as the manifest records it: {rule, used, unused: [{path, reason}]}, paths relative to the data
        root ('/'-separated, in their own case); used is None where no candidate could be read."""
        return {
            "rule": RULE,
            "used": _slashes(self.paths[self.used]) if self.used is not None else None,
            "unused": [{"path": _slashes(self.paths[item]), "reason": reason} for item, reason in self.unused],
        }


def choose_encoding(candidates: Mapping[str, str], readability: Callable[[str], str]) -> EncodingChoice:
    """THE RULE for one sample: of ``candidates`` ({candidate: its path relative to the data root}), the one used.

    The candidates are ordered by rank and then by path without case (clauses 1 and 2). ``readability(candidate)``
    returns '' for a readable candidate and otherwise the reason it cannot be used (UNDECODABLE, CONVERSION_FAILED,
    REQUIRES_CONVERSION, or a convert stage's own reason); it is asked in that order, and only until the first
    readable candidate, which is used (clause 3). Each candidate before it is unused for the reason readability
    gave; each after it is unused as LOWER_IN_ENCODING_ORDER, or TIE_LEXICOGRAPHIC where its rank is the used one's
    (clause 5). Where none can be read, none is used, and each is unused for its own reason.
    """
    ordered = sorted(candidates, key=lambda item: order_key(candidates[item]))
    unused: list[tuple[str, str]] = []
    used: str | None = None
    for index, item in enumerate(ordered):
        reason = readability(item)
        if reason:
            unused.append((item, reason))
            continue
        used = item
        rank = order_key(candidates[item])[0]
        for later in ordered[index + 1:]:
            unused.append(
                (later, TIE_LEXICOGRAPHIC if order_key(candidates[later])[0] == rank else LOWER_IN_ENCODING_ORDER)
            )
        break
    return EncodingChoice(used=used, unused=tuple(unused), paths=dict(candidates))


def choices_record(choices: Iterable[EncodingChoice]) -> list[dict[str, Any]]:
    """The unit's record of every sample the rule chose for among more than one candidate, sorted by what was used
    (then by the first candidate), as manifest.encoding_choices lists them."""
    records = [choice.record() for choice in choices if len(choice.candidates) > 1]
    return sorted(
        records,
        key=lambda item: (
            str(item["used"] or (item["unused"][0]["path"] if item["unused"] else "")).casefold(),
            str(item["used"] or ""),
        ),
    )
