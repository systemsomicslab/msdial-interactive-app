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
