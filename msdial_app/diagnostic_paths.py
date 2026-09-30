"""Where a peak-count diagnostic writes, and how to keep its output out of a production result.

A diagnostic runs MS-DIAL Console on a single representative file to count peaks at a candidate
threshold. It is a measurement about the data, not a result of the study, and it shares every
preparation step with a production run: the same preparer writes an analysis CSV, a method file and a
run manifest into whatever directory it is given.

It used to be given the production output directory. That reduced a reviewed thirty-row
analysis_files.csv to the one representative, and rewrote method.txt and run-manifest.json with the
diagnostic's parameters, while the reviewed metadata, the Class proposal and every downstream report
still described thirty samples in ten classes. The next planning call then read the mutilated CSV and
produced a fully self-consistent single-file plan with no warning of any kind.

Two separations are applied here, and the second exists because the first cannot cover both layouts.

Structural. A repository analysis unit owns a workspace, so its diagnostics go in a sibling of the
production output directory and are never reachable from a scan rooted at that output. A local
analysis has no workspace, only the output directory the user chose, and quietly creating a sibling
next to it would write somewhere the user did not point at; so a local diagnostic goes in a dotted
subdirectory of the output instead.

By path. The local layout is therefore inside the tree production artifacts are discovered from, and
several of those discoveries are recursive and pick the newest file they find. Every one of them
filters through :func:`is_diagnostic_artifact`, so a diagnostic mzTab-M can never become the run's
primary mzTab-M, and a diagnostic file can never be retained, archived, published or handed off as a
result of the study.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

# A repository unit's diagnostics live beside its output, inside the workspace the manifest owns.
REPOSITORY_DIAGNOSTIC_DIRECTORY = "diagnostics"

# A local analysis has no workspace to place a sibling in, so its diagnostics live inside the output
# directory under a dotted name that nothing else writes.
LOCAL_DIAGNOSTIC_DIRECTORY = ".msdial-diagnostics"

_DIAGNOSTIC_DIRECTORY_NAMES = frozenset(
    {REPOSITORY_DIAGNOSTIC_DIRECTORY, LOCAL_DIAGNOSTIC_DIRECTORY}
)


# Where a bundle's reproduction scripts write, inside the run directory. A run's own results
# are never under it, so a scan of the run for its results skips it.
REPRODUCTION_DIRECTORY_NAME = "reproduced-results"

# Where a campaign unit's MS-DIAL containers go when they leave the raw tree after the run:
# <output>/msdial-intermediates/<path under the raw directory>. The job that moved each one is in
# the record, not in the path, which is already longer than the one the Console wrote. They are
# retained, never results: a container named after a sample whose name holds "mztab" is not the
# run's mzTab-M.
INTERMEDIATES_DIRECTORY = "msdial-intermediates"

# LongPathsEnabled is 0 on the campaign host. Without it Windows refuses a file path of MAX_PATH
# (260) characters or more, and creates no directory of 248 or more, unless the call is given the
# \\?\ form. A container the Console could write beside its input is 24 characters longer below
# output\msdial-intermediates\ than below raw\, so its move, and every later look at it, takes
# that form once a path reaches this length.
_EXTENDED_FROM = 248
_EXTENDED = "\\\\?\\"
_EXTENDED_UNC = "\\\\?\\UNC\\"


def extended_path(path: str | Path, *, always: bool = False) -> str:
    """The path as the Windows file functions take one of any length; elsewhere, as it is.

    ``always`` prefixes a short path too: a walk from it reaches the long paths below it only then.
    """
    text = str(path)
    if os.name != "nt" or text.startswith(_EXTENDED) or (len(text) < _EXTENDED_FROM and not always):
        return text
    absolute = os.path.abspath(text)
    if absolute.startswith("\\\\"):
        return _EXTENDED_UNC + absolute[2:]
    return _EXTENDED + absolute


def plain_path(text: str) -> str:
    """A path without the extended-length prefix, as a record names it."""
    if text.startswith(_EXTENDED_UNC):
        return "\\\\" + text[len(_EXTENDED_UNC):]
    if text.startswith(_EXTENDED):
        return text[len(_EXTENDED):]
    return text


def path_is_file(path: str | Path) -> bool:
    """Path.is_file(), for a path of any length."""
    return os.path.isfile(extended_path(path))


def intermediate_files(output: str | Path) -> list[Path]:
    """Every file below <output>/msdial-intermediates, one whose path is longer than MAX_PATH included.

    Path.rglob does not see a file whose path is too long - it is silently left out - so a moved
    container would be neither retained nor inventoried.
    """
    top = extended_path(Path(output) / INTERMEDIATES_DIRECTORY, always=True)
    found = [
        Path(plain_path(os.path.join(directory, name)))
        for directory, _directories, names in os.walk(top)
        for name in names
    ]
    return sorted(found)


def is_intermediate_artifact(path: str | Path, root: str | Path) -> bool:
    """True when a path lies under a relocated-containers directory below the scan root."""
    try:
        parts = Path(path).relative_to(Path(root)).parts
    except (TypeError, ValueError):
        return False
    return INTERMEDIATES_DIRECTORY in parts[:-1]


def is_reproduction_artifact(path: str | Path, root: str | Path) -> bool:
    """True when a path lies under the reproduction directory directly below the scan root.

    Judged relative to the root, not by any path part, so a run that itself sits inside a
    folder of that name still finds its own results.
    """
    try:
        parts = Path(path).relative_to(Path(root)).parts
    except (TypeError, ValueError):
        return False
    return bool(parts) and parts[0] == REPRODUCTION_DIRECTORY_NAME


def is_diagnostic_artifact(path: str | Path) -> bool:
    """True when a path lies under a peak-count diagnostic directory.

    Both directory names are recognised. The repository layout does not strictly need it, because it
    sits outside the production output, but a filter that depends on where a scan happens to be rooted
    is one refactor away from being wrong.
    """
    try:
        parts = Path(path).parts
    except (TypeError, ValueError):
        return False
    return any(part in _DIAGNOSTIC_DIRECTORY_NAMES for part in parts)


def exclude_diagnostic_artifacts(paths: Iterable[Path]) -> list[Path]:
    """Drop everything a peak-count diagnostic produced."""
    return [path for path in paths if not is_diagnostic_artifact(path)]


def diagnostic_run_directory(
    output_root: str | Path,
    diagnostic_job_id: str,
    workspace: str | Path | None = None,
) -> Path:
    """The directory one diagnostic run owns, unique to that run.

    ``workspace`` is the repository analysis unit's workspace when there is one. It must be the parent
    of ``output_root``; a workspace that does not own the output directory is not this unit's, and
    following it would write a diagnostic outside the unit that asked for it.
    """
    output = Path(output_root).expanduser().resolve()
    job = str(diagnostic_job_id).strip()
    if not job:
        raise ValueError("A diagnostic run directory needs a diagnostic job id.")
    if workspace:
        root = Path(workspace).expanduser().resolve()
        if output.parent != root:
            raise ValueError(
                f"The repository workspace {root} does not own the output directory {output}; "
                "refusing to place a diagnostic outside the unit that requested it."
            )
        return root / REPOSITORY_DIAGNOSTIC_DIRECTORY / job
    return output / LOCAL_DIAGNOSTIC_DIRECTORY / job
