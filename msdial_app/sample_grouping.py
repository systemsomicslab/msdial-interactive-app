"""Propose the experimental grouping a set of file names encodes.

Analysts name files after everything about a run at once: the date, the instrument,
the project, the subject, the age, the treatment, the matrix, the replicate, the
polarity. Only some of that is the comparison the experiment is about.

What separates a factor from the rest is how it varies. A token that is the same in
every file describes the whole experiment and groups nothing. A token that differs in
every file identifies the sample and groups nothing either. A factor sits between: a
few values, each shared by several files. That shape is what is looked for here,
rather than a list of words someone thought of in advance -- a matrix name like
"Plasma" is usually constant across a study, and proposing it as the grouping produces
one group containing everything.

The proposal is a starting point for a person to correct, so the alternatives are
returned beside it rather than discarded.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Iterable

BLANK_MARKERS = ("blank",)
QC_MARKERS = ("qc", "quality_control")
# Tokens that describe the run rather than the sample, and are never the comparison.
UNINFORMATIVE_TOKENS = {
    "pos", "neg", "positive", "negative", "p", "n",
    "ms", "msms", "ms2", "dda", "dia", "swath", "aif",
}
SEPARATOR = re.compile(r"[_\-\s]+")


def split_tokens(name: str) -> list[str]:
    return [token for token in SEPARATOR.split(str(name).strip()) if token]


def is_blank(name: str) -> bool:
    lowered = str(name).lower()
    return any(marker in lowered for marker in BLANK_MARKERS)


def is_quality_control(name: str) -> bool:
    lowered = str(name).lower()
    tokens = {token.lower() for token in split_tokens(name)}
    return "qc" in tokens or any(marker in lowered for marker in QC_MARKERS if marker != "qc")


def propose_grouping(names: Iterable[str]) -> dict[str, Any]:
    """Rank the token positions that could be the experimental factor.

    Returns the best candidate, every other candidate considered, and the assignment
    each file would receive. Blanks and quality-control files are set aside: they are
    not part of the comparison and their names rarely carry the factor.
    """
    all_names = [str(name) for name in names]
    subjects = [
        name for name in all_names if not is_blank(name) and not is_quality_control(name)
    ]
    if len(subjects) < 2:
        return {
            "candidates": [],
            "chosen": None,
            "assignments": {name: _fixed_label(name) or "Sample" for name in all_names},
            "reason": "fewer than two comparable files, so nothing varies to group by",
        }

    token_lists = [split_tokens(name) for name in subjects]
    width = min(len(tokens) for tokens in token_lists)
    candidates: list[dict[str, Any]] = []
    for position in range(width):
        values = [tokens[position] for tokens in token_lists]
        counts = Counter(values)
        distinct = len(counts)
        if distinct < 2:
            continue  # constant: describes the study, groups nothing
        if distinct == len(values):
            continue  # unique per file: an identifier, groups nothing
        if all(token.lower() in UNINFORMATIVE_TOKENS for token in counts):
            continue
        smallest_group = min(counts.values())
        if smallest_group < 2:
            # A "group" of one is a sample, not a condition.
            continue
        candidates.append({
            "position": position,
            "values": sorted(counts),
            "group_count": distinct,
            "smallest_group": smallest_group,
            "numeric": all(_looks_numeric(token) for token in counts),
            "score": _score(distinct, smallest_group, counts),
        })

    candidates.sort(key=lambda item: (-item["score"], item["position"]))
    chosen = candidates[0] if candidates else None

    assignments: dict[str, str] = {}
    for name in all_names:
        fixed = _fixed_label(name)
        if fixed:
            assignments[name] = fixed
            continue
        if chosen is None:
            assignments[name] = "Sample"
            continue
        tokens = split_tokens(name)
        assignments[name] = (
            tokens[chosen["position"]] if chosen["position"] < len(tokens) else "Sample"
        )

    return {
        "candidates": candidates,
        "chosen": chosen,
        "assignments": assignments,
        "reason": (
            f"token {chosen['position'] + 1} of the file name takes "
            f"{chosen['group_count']} values ({', '.join(chosen['values'])}), "
            f"each shared by at least {chosen['smallest_group']} files"
            if chosen
            else "no token varies in a way that forms groups"
        ),
    }


def _score(distinct: int, smallest_group: int, counts: Counter) -> float:
    """Prefer few, well-replicated, word-like groups.

    A replicate index and a treatment label can both partition the files; the treatment
    is the one that reads as a word and usually has fewer levels.
    """
    score = 10.0
    score -= abs(distinct - 2) * 1.5      # two groups is the commonest comparison
    score += min(smallest_group, 4) * 0.5  # replication is evidence of a real group
    if all(_looks_numeric(token) for token in counts):
        score -= 3.0                       # numbers are usually indices, not conditions
    if any(len(token) == 1 for token in counts):
        score -= 1.0                       # single characters are rarely a factor name
    return score


def _looks_numeric(token: str) -> bool:
    return bool(re.fullmatch(r"\d+(\.\d+)?", token))


def _fixed_label(name: str) -> str:
    if is_blank(name):
        return "Blank"
    if is_quality_control(name):
        return "QC"
    return ""


def file_type_for(name: str) -> str:
    if is_blank(name):
        return "Blank"
    if is_quality_control(name):
        return "QC"
    return "Sample"
