"""What is built to leave this machine carries none of its paths, and no private library.

THE RULE. The contract lets a private library - the laboratory's VS21 MSP pair, an in-house LBM2 - be used
here and forbids it, and where it is kept, from reaching a bundle, a log, a repository or a shared report. A
library is identified by its file name and sha256 instead. Nothing on the way out knew that. The Console writes
``file://<library location>`` into every mzTab-M it exports; the publication report embedded the run's whole
settings, locations included; its table and workbook listed every library path; the workflow bundle zipped the
method file and the annotator settings exactly as the Console reads them. The production library is kept
outside any user profile, so the gate's first check, which matched only a profile path, never saw one.

TWO SCOPES. A repository analysis unit - a run that carries a unit manifest - is built to be redistributed.
Everything it shares is rewritten: the unit's raw directory becomes ``raw/``, its workspace the root of a
workspace-relative path, a library its file name, the Console its file name, and any other absolute path is
withheld. Those artifacts declare :data:`PATH_POLICY`, so the gate refuses whatever path is left in them. A
laboratory run changes only where the private-library rule itself requires it: a private library's location
becomes its file name, and every other path stays as the analyst's local report has always shown it.

WHAT IS PRIVATE. Fail closed, as the gate decides it (SEC-1): a library is public only when a DOI or an https
source is recorded for it and no record gives it a private, institutional, proprietary or in-house licence or a
private distribution. A library the settings load that no record describes is private.

WHAT THIS IS NOT. Pattern redaction is never complete. The writers apply it and then scan what they wrote with
the same patterns the gate uses, and refuse to leave a file that still matches; the gate scans again,
independently. A clean scan says no known pattern matched, never that no private data is present.
"""

from __future__ import annotations

import html
import io
import json
import os
import re
import urllib.parse
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any, Iterable


PATH_POLICY = "msdial-interactive.shared-paths.v1"
# The workflow bundle is written before the run, with no report beside it to declare the policy, so it carries
# its declaration as a member.
SHARED_PATHS_MEMBER = "SHARED-PATHS.json"
WITHHELD = "local path withheld"

# The gate's own patterns (verify-run-invariants.py, SEC-1), so a writer refuses what the gate would.
PROFILE_PATH_PATTERN = re.compile(
    r"[A-Za-z]:[\\/]{1,2}Users[\\/]{1,2}[^\\/\s\"\',;]+"
    r"|file:/{2,3}[A-Za-z]:/+Users/+[^\s\"\',;]+",
    re.IGNORECASE,
)
ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?:(?<![A-Za-z0-9])|(?<=%2F)|(?<=%5C)|(?<=%20))"
    r"[A-Za-z](?::|%3A)(?:\\\\|\\|/|%5C|%2F)(?=[^\s\"'<>|]*[A-Za-z0-9_])[^\s\"'<>|]*",
    re.IGNORECASE,
)
UNC_PATH_PATTERN = re.compile(
    r"(?:^|(?<=[\s\"'<>|;,=]))(?:\\\\\\\\|\\\\|//|%5C%5C)"
    r"[A-Za-z0-9][A-Za-z0-9._$-]{0,62}(?:\\\\|\\|/|%5C)[^\\/\s\"'<>|]+[^\s\"'<>|]*"
    r"|smb://[^\s\"'<>|]+",
    re.IGNORECASE | re.MULTILINE,
)
FILE_URI_PATTERN = re.compile(
    r"file(?::|%3A)(?:/|%2F){2,}(?=[^\s\"'<>|]*[A-Za-z0-9])[^\s\"'<>|]*",
    re.IGNORECASE,
)
_DETECTORS = (
    ("user-profile path", PROFILE_PATH_PATTERN),
    ("file URI", FILE_URI_PATTERN),
    ("UNC path", UNC_PATH_PATTERN),
    ("drive-letter path", ABSOLUTE_PATH_PATTERN),
)
# What the writers withhold: the same anchors, but a match stops at a comma or a semicolon, so a path in a CSV
# row or a list does not take the rest of the row with it. The anchor itself is what the gate looks for, and
# every anchor is replaced.
_WITHHOLD = (
    re.compile(r"file(?::|%3A)(?:/|%2F){2,}[^\s\"'<>|,;]*", re.IGNORECASE),
    re.compile(
        r"(?:^|(?<=[\s\"'<>|;,=]))(?:\\\\\\\\|\\\\|//|%5C%5C)"
        r"[A-Za-z0-9][A-Za-z0-9._$-]{0,62}(?:\\\\|\\|/|%5C)[^\s\"'<>|,;]*"
        r"|smb://[^\s\"'<>|,;]+",
        re.IGNORECASE | re.MULTILINE,
    ),
    re.compile(
        r"(?:(?<![A-Za-z0-9])|(?<=%2F)|(?<=%5C)|(?<=%20))"
        r"[A-Za-z](?::|%3A)(?:\\\\|\\|/|%5C|%2F)[^\s\"'<>|,;]*",
        re.IGNORECASE,
    ),
)

# A library file, or MS-DIAL's serialised copy of the libraries a run loaded, is never a bundle member.
LIBRARY_FILE = re.compile(r"\.(?:msp|msp2|dbs|lbm|lbm2)$|_Loaded\.msp2", re.IGNORECASE)
# Files that carry this machine's paths by design, or MS-DIAL's containers: local, never in a shared bundle.
LOCAL_ONLY_FILE = re.compile(
    r"\.(?:mdproject|mddata|dcl|pai2?|arf2?|aef|rtc|sfs)$|^(?:datamining-handoff|guided-answers)\.json$"
    r"|\.local\.json$",
    re.IGNORECASE,
)
PRIVATE_LICENCE = re.compile(r"private|institutional|proprietary|in-house", re.IGNORECASE)
_SEPARATORS = re.compile(r"[\\/]+")
_JSON_ESCAPE = re.compile(r"\\u([0-9A-Fa-f]{4})")
# A known root is followed by a separator, which is consumed with it, or ends where a path ends; so a root is
# never matched inside a longer directory name.
_ROOT_END = r"(?:(?P<sep>\\\\|\\|/|%5C|%2F)|(?=[\s\"'<>|,;)\]}]|\.(?:\s|$)|$))"
_FILE_NAME = re.compile(r"[^.\\/:]+(?:\.[A-Za-z0-9]{1,8})+")
_ZIP_MAGIC = b"PK\x03\x04"
_SCAN_DEPTH = 2


class SharingError(ValueError):
    """A file built to be shared still carries a path or a private library after redaction.

    A ValueError, so the structured-error paths report it. It means the redaction missed something, which is
    a defect to fix, never a file to share as it is.
    """

    def __init__(self, where: str, findings: list[str]) -> None:
        self.where = where
        self.findings = list(findings)
        super().__init__(
            f"shared_artifact_refused [{where}]: {len(self.findings)} finding(s) no shared artifact may "
            "carry remain after redaction: " + "; ".join(self.findings[:5])
        )


@dataclass(frozen=True)
class Library:
    """One library a run loads: where it is here, and how it is named everywhere else."""

    name: str
    role: str
    locations: tuple[str, ...]
    identity: dict[str, Any] = field(default_factory=dict)
    public_uri: str = ""

    @property
    def private(self) -> bool:
        return bool(self.identity.get("private", True))


def fold(text: str) -> str:
    """Text as SEC-1 compares it with a location: percent-, JSON- and XML-escapes decoded, one case, and
    every run of separators one '/'. The same folding on both sides meets whichever encoding a writer chose.
    """
    if "%" in text:
        text = urllib.parse.unquote(text, errors="replace")
    if "\\u" in text:
        text = _JSON_ESCAPE.sub(lambda match: chr(int(match.group(1), 16)) if int(match.group(1), 16) >= 0x80
                                else match.group(0), text)
    if "&" in text:
        text = html.unescape(text)
    return _SEPARATORS.sub("/", text.casefold())


def _is_absolute(text: str) -> bool:
    value = str(text or "").strip()
    return bool(re.match(r"^(?:[A-Za-z]:[\\/]|\\\\|//|file:)", value, re.IGNORECASE))


def local_path_of(value: str) -> str | None:
    """The local path a file URI or an absolute path names, or None when the value names no local file."""
    text = str(value or "").strip()
    if not text or text.casefold() == "null":
        return None
    if re.match(r"^file(?::|%3A)", text, re.IGNORECASE):
        rest = urllib.parse.unquote(re.sub(r"^file(?::|%3A)", "", text, flags=re.IGNORECASE))
        stripped = rest.lstrip("/\\")
        if re.match(r"^[A-Za-z]:", stripped):
            return stripped
        return "//" + stripped if stripped else None
    if re.match(r"^(?:[A-Za-z]:[\\/]|\\\\|//)", text):
        return text
    return None


def _location_key(text: str) -> str:
    return fold(str(text or "").strip()).rstrip("/")


def _normalized(path: str) -> str:
    """A path with every run of separators one '/', its case kept."""
    return _SEPARATORS.sub("/", str(path or "").strip()).rstrip("/")


def _locations(path: str) -> tuple[str, ...]:
    """A path as the settings name it, and as it resolves here: an 8.3 name or a link reads differently."""
    text = str(path or "").strip()
    found = [text]
    try:
        found.append(str(Path(text).expanduser().resolve()))
    except (OSError, ValueError, RuntimeError):
        pass
    try:
        found.append(os.path.abspath(os.path.expanduser(text)))
    except (OSError, ValueError):
        pass
    return tuple(dict.fromkeys(item for item in found if item))


def _variants(path: str) -> list[str]:
    """Every encoding a writer puts this location in: backslashes, doubled backslashes, forward slashes, a
    file URI (the Console's file://D:/... and the standard file:///D:/...), and percent-encoded forms."""
    text = str(path or "").strip().rstrip("\\/")
    if not text:
        return []
    windows = text.replace("/", "\\")
    forward = windows.replace("\\", "/")
    quoted = urllib.parse.quote(forward, safe="/:")
    found = [windows, forward, windows.replace("\\", "\\\\"), quoted,
             urllib.parse.quote(windows, safe=""), urllib.parse.quote(forward, safe="")]
    if forward.startswith("//"):
        host = forward[2:]
        found += [f"file://{host}", f"file:////{host}", "file://" + urllib.parse.quote(host, safe="/:")]
    else:
        found += [f"file:///{forward}", f"file://{forward}", f"file:///{quoted}", f"file://{quoted}"]
    return list(dict.fromkeys(item for item in found if item))


def _file_name(value: str) -> str:
    """The last component of a path when it reads as a file name, else nothing."""
    local = local_path_of(value) or value
    last = re.split(r"(?:\\|/|%5C|%2F)+", local.rstrip("\\/"), flags=re.IGNORECASE)[-1]
    last = urllib.parse.unquote(last)
    return last if len(last) <= 120 and _FILE_NAME.fullmatch(last) else ""


def withheld(value: str) -> str:
    name = _file_name(value)
    return f"<{WITHHELD}: {name}>" if name else f"<{WITHHELD}>"


def public_uri(entry: dict[str, Any]) -> str:
    """A resolvable public identifier for a library: its DOI as a URL, else its https source."""
    doi = str(entry.get("doi") or "").strip()
    if doi:
        doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)", "", doi, flags=re.IGNORECASE)
        return "https://doi.org/" + doi
    source = str(entry.get("source") or entry.get("record_url") or "").strip()
    return source if source.casefold().startswith("https://") else ""


def _is_public(entry: dict[str, Any] | None) -> bool:
    if not entry:
        return False
    licence = str(entry.get("license") or entry.get("licence") or "")
    if PRIVATE_LICENCE.search(licence) or str(entry.get("distribution") or "").strip().casefold() == "private":
        return False
    return bool(public_uri(entry))


def _role_of(path: str) -> str:
    suffix = Path(str(path)).suffix.casefold()
    return "lbm" if suffix in {".lbm", ".lbm2"} else "msp" if suffix in {".msp", ".msp2"} else "text"


def _used_library_paths(state: dict[str, Any]) -> list[tuple[str, str]]:
    """Every library the settings load, with the role it is loaded in."""
    found: list[tuple[str, str]] = []

    def add(value: Any, role: str) -> None:
        text = str(value or "").strip()
        if text:
            found.append((text, role))

    for row in state.get("msp_annotators") or []:
        if isinstance(row, dict):
            add(row.get("msp_file_path"), "msp")
    add(state.get("msp_path"), "msp")
    for row in state.get("text_annotators") or []:
        if isinstance(row, dict):
            add(row.get("text_db_file_path"), "text")
    add(state.get("text_db_path"), "text")
    add(state.get("lbm_path"), "lbm")
    if isinstance(state.get("lbm_annotator"), dict):
        add(state["lbm_annotator"].get("lbm_file_path"), "lbm")
    for entry in state.get("library_provenance") or []:
        if isinstance(entry, dict):
            path = entry.get("path") or entry.get("local_path")
            add(path, _role_of(str(path or "")))
    return found


def _unique_by_name(entries: Iterable[dict[str, Any]], name_of: Any) -> dict[str, dict[str, Any]]:
    """Entries keyed by file name, leaving out a name two entries share: a name is evidence only when unique."""
    by_name: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        name = str(name_of(entry) or "").strip().casefold()
        if name:
            by_name.setdefault(name, []).append(entry)
    return {name: items[0] for name, items in by_name.items() if len(items) == 1}


def library_records(state: dict[str, Any], recorded: Iterable[dict[str, Any]] | None = None) -> list[Library]:
    """Each library the run loads, with the identity every shared artifact names it by.

    ``recorded`` are the run manifest's library entries, written when the run was prepared: the checksum of
    the file the run actually loaded. The file is read again only when no recorded checksum exists, so a
    report made after the library moved still names what was used.
    """
    from .workflow import file_identity

    provenance = [entry for entry in state.get("library_provenance") or [] if isinstance(entry, dict)]
    provenance_by_key = {
        _location_key(entry.get("path") or entry.get("local_path")): entry
        for entry in provenance if str(entry.get("path") or entry.get("local_path") or "").strip()
    }
    provenance_by_name = _unique_by_name(
        provenance, lambda entry: entry.get("filename") or PureWindowsPath(
            str(entry.get("path") or entry.get("local_path") or "")).name)
    recorded = [entry for entry in recorded or [] if isinstance(entry, dict)]
    recorded_by_key = {
        _location_key(entry.get("path")): entry for entry in recorded if str(entry.get("path") or "").strip()
    }
    recorded_by_name = _unique_by_name(
        recorded, lambda entry: entry.get("name") or entry.get("filename") or PureWindowsPath(
            str(entry.get("path") or "")).name)

    libraries: list[Library] = []
    seen: dict[str, int] = {}
    for path, role in _used_library_paths(state):
        locations = _locations(path)
        keys = {_location_key(item) for item in locations}
        if keys & set(seen):
            continue
        name = PureWindowsPath(path).name or Path(path).name
        entry = next((provenance_by_key[key] for key in keys if key in provenance_by_key), None)
        if entry is None:
            entry = provenance_by_name.get(name.casefold())
        record = next((recorded_by_key[key] for key in keys if key in recorded_by_key), None)
        if record is None:
            record = recorded_by_name.get(name.casefold())
        digest = str((record or {}).get("sha256") or "").strip().casefold()
        size = (record or {}).get("bytes", (record or {}).get("size_bytes", (record or {}).get("size")))
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            measured = file_identity(path)
            digest = str(measured.get("sha256") or "")
            size = measured.get("size") if digest else size
        public = _is_public(entry) and not (
            (record or {}).get("private") is True
            or str((record or {}).get("distribution") or "").casefold() == "private"
        )
        identity: dict[str, Any] = {
            "name": name,
            # The gate reads a library entry by "filename" and its size by "size_bytes"; "name" and "bytes"
            # are what every other record calls them.
            "filename": name,
            "role": role,
            "sha256": digest,
            "bytes": size if isinstance(size, int) and not isinstance(size, bool) else None,
            "size_bytes": size if isinstance(size, int) and not isinstance(size, bool) else None,
            "private": not public,
            "distribution": "public" if public else "private",
        }
        for key in ("version", "doi", "source", "license"):
            value = str((entry or {}).get(key) or (entry or {}).get("record_url" if key == "source" else key) or "")
            if value.strip() and (public or key in {"version", "license"}):
                identity[key] = value.strip()
        for key in keys:
            seen[key] = len(libraries)
        libraries.append(Library(name=name, role=role, locations=locations, identity=identity,
                                 public_uri=public_uri(entry or {}) if public else ""))
    return libraries


def _private_directories(libraries: list[Library], keep: Iterable[str]) -> list[tuple[str, str]]:
    """Each private library's directory, as a root to withhold, unless it is a drive root or holds, or lies
    inside, a location in ``keep`` - the directory rule SEC-1 applies, which would otherwise refuse every
    path to the workspace or to a public library kept beside the private one."""
    kept = [_location_key(item) for item in keep if str(item or "").strip()]
    roots: list[tuple[str, str]] = []
    for library in libraries:
        if not library.private:
            continue
        for location in library.locations:
            parent = PureWindowsPath(location).parent
            directory = _location_key(str(parent))
            if len(parent.parts) < 2 or any(
                item == directory or item.startswith(directory + "/") or directory.startswith(item + "/")
                for item in kept
            ):
                continue
            roots.append((str(parent), f"<{WITHHELD}>"))
    return roots


def unit_layout(manifest_path: str | Path, output_root: str | Path | None = None) -> dict[str, str]:
    """A repository unit's workspace, raw directory and output directory, from its own manifest.

    A split part reads its parent's raw tree, which lies in another workspace; the manifest's raw_directory
    is that tree either way. A manifest that cannot be read falls back to the layout Interactive creates.
    """
    from .repository_reanalysis import read_manifest

    path = Path(str(manifest_path)).expanduser()
    try:
        manifest = read_manifest(path)
    except (OSError, ValueError):
        manifest = {}
    output = str(manifest.get("output_directory") or output_root or "").strip()
    workspace = str(manifest.get("workspace") or "").strip()
    if not workspace:
        if path.parent.name.casefold() == "provenance":
            workspace = str(path.parent.parent)
        elif output:
            workspace = str(Path(output).expanduser().resolve().parent)
    raw = str(manifest.get("raw_directory") or (str(Path(workspace) / "raw") if workspace else "")).strip()
    return {"workspace": workspace, "raw_directory": raw, "output_directory": output}


class SharingContext:
    """How one run's shared artifacts are written: which locations become what, and what is withheld."""

    def __init__(
        self,
        libraries: list[Library],
        roots: list[tuple[str, str]] | None = None,
        *,
        full: bool,
        layout: dict[str, str] | None = None,
        bundle: bool = False,
    ) -> None:
        self.libraries = list(libraries)
        self.full = bool(full)
        self.bundle = bool(bundle)
        self.layout = dict(layout or {})
        # Directory roots: a location under one becomes the replacement plus the rest of the path.
        self._roots: list[tuple[str, str]] = []
        for path, replacement in roots or []:
            for location in _locations(path):
                self._roots.append((location, replacement))
        self._replacements: dict[str, tuple[str, bool]] = {}
        variants: list[str] = []
        for library in self.libraries:
            if not (self.full or library.private):
                continue
            for location in library.locations:
                for variant in _variants(location):
                    self._replacements.setdefault(variant.casefold(), (library.name, False))
                    variants.append(variant)
        for location, replacement in self._roots:
            for variant in _variants(location):
                self._replacements.setdefault(variant.casefold(), (replacement, True))
                variants.append(variant)
        ordered = sorted(dict.fromkeys(variants), key=len, reverse=True)
        self._pattern = (
            re.compile(
                "(?P<root>" + "|".join(re.escape(item) for item in ordered) + ")" + _ROOT_END,
                re.IGNORECASE,
            )
            if ordered else None
        )
        private_names = sorted({library.name for library in self.libraries if library.private and library.name},
                               key=len, reverse=True)
        # A private library's name after a separator is a path to it, relative or not.
        self._named_in_path = (
            re.compile(
                r"(?:[^\s\"'<>|,;=\\/]*(?:\\\\|\\|/|%5C|%2F))+(?=(?:"
                + "|".join(re.escape(name) for name in private_names) + r")(?![A-Za-z0-9_.\-]))",
                re.IGNORECASE,
            )
            if private_names else None
        )
        self._needles: list[tuple[str, str]] = []
        self._fragments: list[tuple[str, str]] = []
        self._directories = [
            re.compile(re.escape(_location_key(path)) + r"(?=/|[\s\"'<>|,;)\]]|\.(?:\s|$)|$)", re.MULTILINE)
            for path, replacement in self._roots if replacement == f"<{WITHHELD}>"
        ]
        for library in self.libraries:
            if not library.private:
                continue
            self._fragments.append(("/" + fold(library.name), library.name))
            for location in library.locations:
                if PureWindowsPath(location).anchor:
                    self._needles.append((_location_key(location), library.name))

    @classmethod
    def for_state(
        cls,
        state: dict[str, Any],
        *,
        run_directory: str | Path | None = None,
        recorded: Iterable[dict[str, Any]] | None = None,
        bundle: bool = False,
    ) -> "SharingContext":
        """The context for a run's settings: a repository unit's when they name its manifest, else a
        laboratory run's. ``bundle`` writes paths relative to the bundle, whose root is the run directory."""
        libraries = library_records(state, recorded)
        manifest = str(state.get("repository_run_manifest") or "").strip()
        run = str(run_directory or state.get("output_root") or "").strip()
        if not manifest:
            # Where a private library is kept is withheld too, but only a directory that holds nothing else
            # the report names: not the output, an input or a public library, whose paths stay as they were.
            keep = [run, *(str((item or {}).get("file_path") or "") for item in state.get("files") or []),
                    *(location for library in libraries if not library.private for location in library.locations)]
            return cls(libraries, _private_directories(libraries, keep), full=False)
        layout = unit_layout(manifest, run)
        roots: list[tuple[str, str]] = []
        if layout["raw_directory"]:
            roots.append((layout["raw_directory"], "raw"))
        if bundle and run:
            roots.append((run, ""))
            if layout["workspace"]:
                try:
                    relative = os.path.relpath(layout["workspace"], run).replace("\\", "/")
                except ValueError:  # another drive
                    relative = ""
                if relative and not relative.startswith(("/", "\\")) and ":" not in relative:
                    roots.append((layout["workspace"], relative))
        elif layout["workspace"]:
            roots.append((layout["workspace"], ""))
        for key in ("console_path", "template_path"):
            value = str(state.get(key) or "").strip()
            if value and PureWindowsPath(value).name:
                roots.append((value, PureWindowsPath(value).name))
        # Matched as a whole, so a directory name with a space in it is withheld whole, not up to the space.
        keep = [layout["workspace"], layout["raw_directory"], run,
                *(location for library in libraries if not library.private for location in library.locations)]
        roots.extend(_private_directories(libraries, keep))
        return cls(libraries, roots, full=True, layout=layout, bundle=bundle)

    # ---- redaction ----------------------------------------------------------------------------------

    def _replace_root(self, match: "re.Match") -> str:
        replacement, is_directory = self._replacements.get(match.group("root").casefold(), (None, False))
        separator = match.group("sep") or ""
        if replacement is None:  # pragma: no cover - every alternative is a key
            return match.group(0)
        if not is_directory:
            return replacement + separator
        if replacement == "":
            return "" if separator else "."
        return replacement + separator

    def text(self, value: str) -> str:
        """One text with every known location replaced and, for a repository unit, every other path withheld."""
        if not isinstance(value, str) or not value:
            return value
        result = value
        if self._pattern is not None and self._pattern.search(result):
            result = self._pattern.sub(self._replace_root, result)
        if self._named_in_path is not None:
            result = self._named_in_path.sub("", result)
        if self.full:
            for pattern in _WITHHOLD:
                result = pattern.sub(lambda match: withheld(match.group(0)), result)
        return result

    def view(self, value: Any) -> Any:
        """A copy of a JSON-like value with every string redacted as :meth:`text` redacts it."""
        if isinstance(value, str):
            result = self.text(value)
            if result != value and _is_absolute(value) and not result.startswith("<"):
                result = result.replace("\\", "/")
            return result
        if isinstance(value, dict):
            return {(self.text(key) if isinstance(key, str) else key): self.view(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.view(item) for item in value]
        return value

    def relative(self, local: str) -> str | None:
        """A local path as a path relative to the root that holds it, or None when no known root does."""
        path = _normalized(local)
        for root, replacement in sorted(self._roots, key=lambda item: len(item[0]), reverse=True):
            if replacement.startswith("<"):
                continue  # a withheld directory is nothing to be relative to
            base = _normalized(root)
            if path.casefold() == base.casefold():
                return replacement or "."
            if path.casefold().startswith(base.casefold() + "/"):
                rest = path[len(base) + 1:]
                return f"{replacement}/{rest}" if replacement else rest
        return None

    def library_for(self, local: str, version: str = "") -> Library | None:
        """The library a location or a -version file name names, when exactly one does."""
        key = _location_key(local)
        for library in self.libraries:
            if any(_location_key(location) == key for location in library.locations):
                return library
        for name in (PureWindowsPath(local.replace("/", "\\")).name, str(version or "").strip()):
            same = [library for library in self.libraries if name and library.name.casefold() == name.casefold()]
            if len(same) == 1:
                return same[0]
        return None

    def identities(self) -> list[dict[str, Any]]:
        return [dict(library.identity) for library in self.libraries]

    def describe(self) -> dict[str, Any]:
        """What a reader of a shared artifact needs to read its paths: the policy and the placeholders."""
        paths = (
            {"raw/": "the analysis unit's raw data directory, as the repository files were placed there",
             "": "a path with neither prefix is relative to the bundle, the run's output directory"}
            if self.bundle else
            {"raw/": "the analysis unit's raw data directory, as the repository files were placed there",
             "": "a path with no prefix is relative to the analysis unit's workspace (output/, provenance/)"}
        )
        return {
            "policy": PATH_POLICY,
            "scope": "repository_unit",
            "paths": paths,
            "withheld": f"<{WITHHELD}: NAME> stands for a path outside the unit's workspace; NAME is its file "
                        "name when it ends in one",
            "library_naming": "each library is named by its file name; its sha256, size and distribution are "
                              "listed under libraries, and a private library is not distributed",
        }

    # ---- detection ----------------------------------------------------------------------------------

    def residual(self, text: str) -> list[str]:
        """What in a text no shared artifact of this run may carry."""
        found: list[str] = []
        if self.full:
            for kind, pattern in _DETECTORS:
                match = pattern.search(text)
                if match:
                    found.append(kind + (f" ({withheld(match.group(0))})" if kind != "user-profile path" else ""))
        if self._needles or self._fragments or self._directories:
            folded = fold(text)
            for location, name in self._needles:
                if location in folded:
                    found.append(f"the location of private library {name}")
            for fragment, name in self._fragments:
                if fragment in folded:
                    found.append(f"private library {name} named inside a path")
            if any(pattern.search(folded) for pattern in self._directories):
                found.append("the directory of a private library")
        return list(dict.fromkeys(found))

    def scan(self, where: str, data: bytes, depth: int = 1, *, shared_container: bool = True) -> list[str]:
        """Findings in one file's bytes, reading a zip (a bundle, a workbook) by its members."""
        if data[:4] == _ZIP_MAGIC and depth <= _SCAN_DEPTH:
            findings: list[str] = []
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    for info in archive.infolist():
                        if info.is_dir():
                            continue
                        member = f"{where}:{info.filename}"
                        base = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
                        if LIBRARY_FILE.search(base):
                            findings.append(f"{member}: a library file, or MS-DIAL's copy of one, as a member")
                            continue
                        if shared_container and LOCAL_ONLY_FILE.search(base):
                            findings.append(f"{member}: a local-only file as a member")
                            continue
                        findings.extend(self.scan(member, archive.read(info), depth + 1))
            except (zipfile.BadZipFile, OSError, RuntimeError) as error:
                findings.append(f"{where}: unreadable as a zip ({type(error).__name__})")
            return findings
        text = data.decode("utf-8-sig", errors="replace")
        return [f"{where}: {kind}" for kind in self.residual(text)]

    def assert_shareable(self, path: str | Path) -> None:
        """Refuse a written file that still carries what it may not. Raises SharingError."""
        target = Path(path)
        findings = self.scan(target.name, target.read_bytes())
        if findings:
            raise SharingError(target.name, findings)


def bundle_member_allowed(name: str, *, shared: bool) -> bool:
    """Whether a file may go into a bundle: never a library or its copy, and into a shared one never a
    local-only file."""
    base = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    if LIBRARY_FILE.search(base):
        return False
    return not (shared and LOCAL_ONLY_FILE.search(base))


def render_member(path: Path, context: SharingContext) -> bytes:
    """A file's bytes as a shared bundle carries them: JSON through :meth:`SharingContext.view`, any other
    text through :meth:`SharingContext.text`, the encoding and a byte-order mark kept. The file on disk is
    left as it is; the Console and the gate read it."""
    data = path.read_bytes()
    bom = data.startswith(b"\xef\xbb\xbf")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data
    if path.suffix.casefold() == ".json":
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if parsed is not None:
            rendered = json.dumps(context.view(parsed), ensure_ascii=False, indent=2)
            if text.endswith("\n"):
                rendered += "\n"
            return (b"\xef\xbb\xbf" if bom else b"") + rendered.encode("utf-8")
    return (b"\xef\xbb\xbf" if bom else b"") + context.text(text).encode("utf-8")
