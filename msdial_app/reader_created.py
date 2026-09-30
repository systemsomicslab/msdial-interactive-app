"""Files a raw-data reader writes into an input container, told apart from the files it arrived with.

Bruker's BAF reader needs an analysis.sqlite beside analysis.baf. baf2sql, which RawDataHandler and the
raw-metadata extractor call to open a BAF .d, writes one into the .d when it arrived without one: during the
raw-header preflight, and very likely again during the MS-DIAL run. The .d then holds a file the repository
never published, which a check of the container against its members would read as an unexpected member, and
a comparison with its published checksums as a changed container.

Such a file is reader_created. It is not an analysis input, not a member of its container, and not a checksum
failure: a check of a container's members sets it aside through container_members, and the input lineage and
the unit manifest record it (reader_created_files), with its size and sha256, so what is on disk is still
accounted for.

A file the container arrived with is its own whatever its name: a BAF .d published with its analysis.sqlite
keeps it as a member. So the helpers take ``members``, the relative paths the container arrived with; a
caller that does not know them passes None, and every file a rule names is then reported.

Only the rules below are known. A reader that writes something else into a container is not guessed at: the
file stays unaccounted for, which is what a member check should say about it.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


@dataclass(frozen=True)
class ReaderRule:
    """What one reader writes into a container: its files, by path relative to the container."""

    reader: str
    container_suffix: str
    # A file at the container's top that says the rule applies: the data the reader converts.
    marker: str
    names: tuple[str, ...]


READER_CREATED_RULES = (
    ReaderRule(
        reader="bruker_baf2sql",
        container_suffix=".d",
        marker="analysis.baf",
        # SQLite's rollback journal and write-ahead files are the database's while it is written, and are
        # left behind by a reader that is stopped mid-write.
        names=("analysis.sqlite", "analysis.sqlite-journal", "analysis.sqlite-wal", "analysis.sqlite-shm"),
    ),
)


def _relative_key(relative: str | os.PathLike[str]) -> str:
    return PurePosixPath(str(relative).replace("\\", "/")).as_posix().casefold()


def reader_created_names(container: str | Path) -> dict[str, str]:
    """The paths, relative to the container and casefolded, a reader may write into it, each to its reader.

    Empty when no rule applies: a container of another kind, or one without the rule's marker.
    """
    path = Path(container)
    names: dict[str, str] = {}
    for rule in READER_CREATED_RULES:
        if path.suffix.casefold() == rule.container_suffix and (path / rule.marker).is_file():
            names.update({name.casefold(): rule.reader for name in rule.names})
    return names


def reader_created_reader(
    container: str | Path, relative: str | os.PathLike[str], members: Iterable[str] | None = None
) -> str:
    """The reader that wrote the file at ``relative`` inside ``container``, or "" when it is the container's own."""
    key = _relative_key(relative)
    if members is not None and key in {_relative_key(member) for member in members}:
        return ""
    return reader_created_names(container).get(key, "")


def reader_created_files(container: str | Path, members: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Each file inside ``container`` a reader wrote there: {path, size, sha256, reader}, by relative path.

    ``members`` are the relative paths the container arrived with; one of them is never reported. A file
    that cannot be read is reported with an empty sha256.
    """
    root = Path(container)
    names = reader_created_names(root)
    if not names:
        return []
    arrived = {_relative_key(member) for member in members} if members is not None else set()
    found = []
    for relative in sorted(names):
        path = root.joinpath(*relative.split("/"))
        if relative in arrived or not path.is_file():
            continue
        found.append({"path": path.relative_to(root).as_posix(), **_identity(path), "reader": names[relative]})
    return found


def container_members(
    container: str | Path, relatives: Iterable[str], members: Iterable[str] | None = None
) -> tuple[list[str], list[str]]:
    """Split the relative paths found inside a container into (its own, reader_created).

    For a check of a container against the members it was published with: the first list is compared,
    and the second is recorded (reader_created_files) rather than counted as unexpected.
    """
    names = reader_created_names(container)
    arrived = {_relative_key(member) for member in members} if members is not None else set()
    own: list[str] = []
    created: list[str] = []
    for relative in relatives:
        key = _relative_key(relative)
        (created if key in names and key not in arrived else own).append(relative)
    return own, created


def _identity(path: Path) -> dict[str, Any]:
    try:
        size = path.stat().st_size
    except OSError:
        return {"size": 0, "sha256": ""}
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        # A reader still writing it may hold it open.
        return {"size": size, "sha256": ""}
    return {"size": size, "sha256": digest.hexdigest()}
