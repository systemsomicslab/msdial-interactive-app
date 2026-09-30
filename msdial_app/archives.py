"""Archive formats, the 7-Zip adapter, and extraction that refuses before it writes.

Repository raw data arrive packed. In the declared campaign pool 4,672 distinct URLs are zip, 33 are
7z, 19 are rar and 9 are tar.gz, and the acquisition-unknown pool adds 159 7z and 161 rar objects and
a bare .gz; MTBLS688 publishes its LC-MS files only as legacy LZMA-alone .lzma streams, which have no
magic bytes. The lease code recognised only .zip, .tar, .tar.gz and .tgz: a .7z or .rar landed as an
opaque file that input discovery ignored, and a bare .gz was routed as an archive and never opened.
This module is the one place that decides what an archive is and how it is opened.

Three findings shape it.

7-Zip rewrites unsafe member names silently and exits 0. A probe extracted '../evil.txt' as
'evil.txt' and 'C:/abs.txt' as 'C_/abs.txt', and with -y it also overwrites. So the listing is
validated here, for every format and every reader, before anything is written, and one unsafe
name refuses the whole archive: traversal, drive and UNC paths, ':' (an alternate data stream on
NTFS), Windows device names, trailing dots and spaces, names that collide up to case, links and
reparse points, encrypted members, names that are not valid Unicode, and paths the .NET Framework
Console could not open (LongPathsEnabled is 0, so 259 characters for a file and 247 for a folder).
What an operating system adds when it packs a folder (__MACOSX/, .DS_Store, '._' AppleDouble
files, Thumbs.db) is validated like any member and then dropped, with a record of what went.

An encrypted archive must fail, not wait. 7-Zip always runs with stdin closed and a sentinel
password, so a header-encrypted archive fails at once instead of prompting, and encrypted members
are refused from the listing.

Nothing is extracted in place. An archive expands into '<destination>.partial'; the tree is
compared with the listing (names, sizes, no links) and only then renamed to the destination. An
interrupted extraction leaves only the .partial directory, which the next attempt removes. Nested
archives expand inside that staging tree, each checked the same way, under one budget of members,
bytes and depth.

The record extract_archive returns is the provenance of what came out of an archive: the reader
and its version (7-Zip's executable and library hashes, or the Python version for the standard
library), the command, the counts, the destination rule, the nested records, and the member
listing written as a TSV whose sha256 is recorded, so the listing outlives the raw data.
"""

from __future__ import annotations

import bz2
import gzip
import hashlib
import lzma
import math
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
import zlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


EXTRACTION_SCHEMA = "msdial-archive-extraction.v1"
GB = 1000 ** 3

# Name-based kinds, longest suffix first so '.tar.gz' wins over '.gz'.
_SUFFIX_KINDS = tuple(
    sorted(
        {
            ".tar.gz": "tar.gz", ".tgz": "tar.gz",
            ".tar.bz2": "tar.bz2", ".tbz2": "tar.bz2", ".tbz": "tar.bz2",
            ".tar.xz": "tar.xz", ".txz": "tar.xz",
            ".zip": "zip", ".tar": "tar", ".7z": "7z", ".rar": "rar",
            ".gz": "gz", ".bz2": "bz2", ".xz": "xz", ".lzma": "lzma",
        }.items(),
        key=lambda item: -len(item[0]),
    )
)
ARCHIVE_SUFFIXES = frozenset(suffix for suffix, _ in _SUFFIX_KINDS)
TAR_KINDS = frozenset({"tar", "tar.gz", "tar.bz2", "tar.xz"})
# 'lzma' is the legacy LZMA-alone stream of LZMA Utils, which xz replaced. MTBLS688 publishes its
# two LC-MS units only as x.mzXML.lzma. It has no magic bytes and no checksum (see
# _looks_like_lzma_alone); a tar inside one is expanded as a nested archive, not read as tar.lzma.
STREAM_KINDS = frozenset({"gz", "bz2", "xz", "lzma"})

# What an archived vendor container unpacks to. X.raw.zip is the Waters folder X.raw (or a Thermo
# file of that name), X.d.zip the Agilent or Bruker folder X.d. Only the folder kinds get a directory
# of their own when their members are stored without it; the file kinds already carry their name.
# .mzxml is not an MS-DIAL input, but it is converted to one (mzxml_conversion), so x.mzXML.lzma
# stands for x.mzXML as x.mzML.gz stands for x.mzML.
CONTAINER_SUFFIXES = (
    ".raw", ".d", ".wiff", ".wiff2", ".mzml", ".mzxml", ".lcd", ".cdf", ".qgd", ".abf",
)
FOLDER_CONTAINER_SUFFIXES = (".raw", ".d")

SEVENZIP_ENVIRONMENT_VARIABLE = "MSDIAL_SEVENZIP"
# 25.00 carries the ZIP symlink-traversal fixes; the probe host has 25.01.
MINIMUM_SEVENZIP_VERSION = (25, 0)
# Never a real password. It stops 7-Zip asking for one, so an encrypted archive fails at once.
SENTINEL_PASSWORD = "msdial-interactive-no-password"
_SEVENZIP_TYPE_SWITCH = {"7z": "7z", "rar4": "rar", "rar5": "rar5", "zip": "zip"}
_SEVENZIP_FORMAT_NAME = {"7z": "7z", "rar4": "Rar", "rar5": "Rar5", "zip": "zip"}

_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$"}
    | {f"{device}{digit}" for device in ("com", "lpt") for digit in "0123456789\u00b9\u00b2\u00b3"}
)
_INVALID_CHARACTERS = frozenset('<>"|?*')
_REPARSE_POINT = stat.FILE_ATTRIBUTE_REPARSE_POINT
_CHUNK = 1024 * 1024
# How often a compressed stream, which declares no size, re-checks the ratio and the disk reserve.
_STREAM_CHECK_BYTES = 64 * _CHUNK
_TSV_COLUMNS = (
    "path", "type", "size", "crc32", "modified", "depth", "archive", "member_name", "disposition",
)


class ArchiveError(Exception):
    """An archive that cannot be listed, is refused, or failed to extract.

    reason is a stable code for manifests and failure records; rejected_members lists each refused
    member with its own reason, so a record can say which names were unsafe and why.
    """

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        rejected: list[dict[str, str]] | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.rejected = list(rejected or [])
        self.detail = dict(detail or {})

    def record(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "message": self.message,
            "rejected_members": self.rejected,
            "detail": self.detail,
        }


class ArchiveToolError(ArchiveError):
    """7-Zip is needed and was not found, is too old, or does not run."""


@dataclass(frozen=True)
class ExtractionLimits:
    """The guards every extraction runs under. Nested archives share one budget of all of them."""

    reserve_bytes: int = 20 * GB
    max_ratio: float = 100.0
    # A ratio above max_ratio is refused only when the expansion is also larger than this.
    ratio_floor_bytes: int = 10 * GB
    max_members: int = 1_000_000
    max_depth: int = 3
    max_path_length: int = 259
    # CreateDirectoryW without long-path support: MAX_PATH less room for an 8.3 file name.
    max_directory_length: int = 247
    watchdog_interval_seconds: float = 5.0
    timeout_base_seconds: float = 600.0
    timeout_seconds_per_gb: float = 60.0
    listing_timeout_seconds: float = 600.0

    def record(self) -> dict[str, Any]:
        return asdict(self)


# --- Format detection ------------------------------------------------------------------------

def archive_kind_from_name(name: str | os.PathLike[str]) -> str:
    """The archive kind a file name claims ('' when the name is not an archive's)."""
    lower = Path(str(name)).name.casefold()
    for suffix, kind in _SUFFIX_KINDS:
        if lower.endswith(suffix) and len(lower) > len(suffix):
            return kind
    return ""


def is_archive_name(name: str | os.PathLike[str]) -> bool:
    return bool(archive_kind_from_name(name))


def archive_stem(name: str | os.PathLike[str]) -> str:
    """The name without its archive suffix: 'X.raw.zip' -> 'X.raw', 'a.tar.gz' -> 'a'."""
    text = Path(str(name)).name
    lower = text.casefold()
    for suffix, _kind in _SUFFIX_KINDS:
        if lower.endswith(suffix) and len(lower) > len(suffix):
            return text[: -len(suffix)]
    return text


def container_suffix(name: str) -> str:
    """The vendor-container suffix a name ends in ('' for none)."""
    lower = str(name).casefold()
    for suffix in sorted(CONTAINER_SUFFIXES, key=len, reverse=True):
        if lower.endswith(suffix) and len(lower) > len(suffix):
            return suffix
    return ""


def container_alias(name: str | os.PathLike[str]) -> str:
    """The container an archived container stands for: 'X.raw.zip' -> 'X.raw', else ''.

    This is the single alias rule. Sample attribution, the Catalog and the gate should agree with
    it rather than each strip suffixes their own way.
    """
    if not is_archive_name(name):
        return ""
    stem = archive_stem(name)
    return stem if container_suffix(stem) else ""


def read_signature(path: str | os.PathLike[str]) -> str:
    """The format the leading bytes identify: zip, 7z, rar4, rar5, gzip, bzip2, xz, tar or ''."""
    with open(path, "rb") as handle:
        head = handle.read(1024)
    return _signature_of(head)


def _signature_of(head: bytes) -> str:
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    if head.startswith(b"Rar!\x1a\x07\x01\x00"):
        return "rar5"
    if head.startswith(b"Rar!\x1a\x07\x00"):
        return "rar4"
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return "zip"
    if head.startswith(b"\x1f\x8b"):
        return "gzip"
    if head.startswith(b"BZh"):
        return "bzip2"
    if head.startswith(b"\xfd7zXZ\x00"):
        return "xz"
    if _looks_like_tar(head[:512]):
        return "tar"
    return ""


def _looks_like_tar(block: bytes) -> bool:
    if len(block) < 512:
        return False
    if block[257:262] == b"ustar":
        return True
    # A pre-POSIX tar has no magic; its header checksum is the only evidence.
    field_text = block[148:156].replace(b"\x00", b" ").strip()
    if not field_text or not all(48 <= byte <= 55 for byte in field_text):
        return False
    computed = sum(block[:148]) + 8 * 32 + sum(block[156:512])
    return computed == int(field_text, 8)


def _looks_like_lzma_alone(path: Path) -> bool:
    """Whether the bytes are an LZMA-alone ('.lzma') stream, which has no magic bytes to read.

    Its 13-byte header is decoded instead and held to what LZMA Utils and xz write, the same rules
    liblzma applies when it has to tell such a stream from noise: a properties byte below 225 whose
    lc + lp is at most 4; a dictionary size of 2^n or 2^n + 2^(n-1), or 0xFFFFFFFF; an uncompressed
    size that is unknown (all ones) or below 256 GiB. Then the range coder's first byte, which an
    encoder always writes as zero, and then the first block must decode. An HTML page named .lzma
    fails the first test.
    """
    with open(path, "rb") as handle:
        head = handle.read(65536)
    if len(head) < 14:
        return False
    properties = head[0]
    if properties >= 225:
        return False
    literal_context, rest = properties % 9, properties // 9
    if literal_context + rest % 5 > 4:
        return False
    dictionary = int.from_bytes(head[1:5], "little")
    if dictionary != 0xFFFFFFFF:
        lowest = dictionary & -dictionary
        if not dictionary or dictionary // lowest not in (1, 3):
            return False
    uncompressed = int.from_bytes(head[5:13], "little")
    if uncompressed != (1 << 64) - 1 and uncompressed >= 1 << 38:
        return False
    if head[13] != 0:
        return False
    try:
        lzma.LZMADecompressor(format=lzma.FORMAT_ALONE).decompress(head, max_length=65536)
    except (lzma.LZMAError, EOFError):
        return False
    return True


def _describe_bytes(head: bytes) -> str:
    if not head:
        return "an empty file"
    text = head.lstrip()[:64].lower()
    if text.startswith((b"<!doctype html", b"<html", b"<?xml", b"<head", b"<body")):
        return "an HTML or XML page, probably a server error page saved under the archive's name"
    if text.startswith((b"{", b"[")):
        return "a JSON document, probably a server error response"
    return "no recognised archive signature"


@dataclass(frozen=True)
class ArchiveDetection:
    kind: str
    name_kind: str
    signature: str

    @property
    def name_mismatch(self) -> bool:
        return self.kind != self.name_kind

    def record(self) -> dict[str, Any]:
        return {
            "format": self.kind,
            "name_format": self.name_kind,
            "signature": self.signature,
            "name_mismatch": self.name_mismatch,
        }


def detect_archive(
    path: str | os.PathLike[str], name: str | None = None
) -> ArchiveDetection | None:
    """The kind of the archive at path, by its name and confirmed by its bytes.

    None when the name is not an archive's. When the name claims an archive and the bytes carry no
    archive signature (an HTML error page saved as .zip is the usual case) this raises
    ArchiveError('not_an_archive'), so the caller can record a failed download instead of finding
    no inputs. When the bytes identify a different archive format than the name, the bytes win and
    the record says so. A compressed stream holding a tar is a tar: a bare '.gz' can be 'tar.gz'.
    """
    path = Path(path)
    name_kind = archive_kind_from_name(name or path.name)
    if not name_kind:
        return None
    with open(path, "rb") as handle:
        head = handle.read(1024)
    signature = _signature_of(head)
    kind = ""
    if signature in ("7z", "zip", "tar"):
        kind = signature
    elif signature in ("rar4", "rar5"):
        kind = "rar"
    elif signature in ("gzip", "bzip2", "xz"):
        stream = {"gzip": "gz", "bzip2": "bz2", "xz": "xz"}[signature]
        kind = f"tar.{stream}" if _stream_holds_tar(path, stream) else stream
    elif not signature and name_kind == "lzma" and _looks_like_lzma_alone(path):
        # No magic to read, so the name is what claims it and the decoded header what confirms it.
        kind, signature = "lzma", "lzma_alone"
    if not kind:
        raise ArchiveError(
            "not_an_archive",
            f"{name or path.name} is named as a {name_kind} archive, but its bytes are "
            f"{_describe_bytes(head)}.",
            detail={"name_format": name_kind, "leading_bytes_hex": head[:16].hex()},
        )
    return ArchiveDetection(kind=kind, name_kind=name_kind, signature=signature)


def archive_kind(path: str | os.PathLike[str], name: str | None = None) -> str:
    """The confirmed archive kind, or '' when the name is not an archive's. See detect_archive."""
    detection = detect_archive(path, name)
    return detection.kind if detection else ""


def _open_stream(path: Path, stream: str):
    if stream == "lzma":
        return lzma.open(path, "rb", format=lzma.FORMAT_ALONE)
    return {"gz": gzip.open, "bz2": bz2.open, "xz": lzma.open}[stream](path, "rb")


def _stream_holds_tar(path: Path, stream: str) -> bool:
    try:
        with _open_stream(path, stream) as handle:
            block = handle.read(512)
    except (OSError, EOFError, lzma.LZMAError, zlib.error):
        return False
    return _looks_like_tar(block)


# --- 7-Zip discovery -------------------------------------------------------------------------

@dataclass(frozen=True)
class SevenZipTool:
    executable: str
    version: str
    source: str
    executable_sha256: str
    library: str = ""
    library_sha256: str = ""
    formats: tuple[str, ...] = ()

    def supports(self, signature: str) -> bool:
        needed = _SEVENZIP_FORMAT_NAME.get(signature)
        # An unparsed format table is not evidence of absence; 7-Zip then decides for itself.
        return not self.formats or needed is None or needed in self.formats

    def record(self) -> dict[str, Any]:
        return {
            "name": "7-Zip",
            "version": self.version,
            "executable": self.executable,
            "executable_sha256": self.executable_sha256,
            "library": self.library,
            "library_sha256": self.library_sha256,
            "source": self.source,
        }


def _sevenzip_executable_name() -> str:
    return "7z.exe" if os.name == "nt" else "7z"


def _registry_sevenzip_directories() -> list[str]:
    """7-Zip's install directories from the registry: HKLM then HKCU, Path64 then Path."""
    try:
        import winreg
    except ImportError:
        return []
    directories = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            key = winreg.OpenKey(
                hive, r"SOFTWARE\7-Zip", 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY
            )
        except OSError:
            continue
        with key:
            for value_name in ("Path64", "Path"):
                try:
                    value, _kind = winreg.QueryValueEx(key, value_name)
                except OSError:
                    continue
                if str(value or "").strip():
                    directories.append(str(value).strip())
    return directories


def sevenzip_candidates(
    setting: str | None = None,
    *,
    environ: dict[str, str] | None = None,
    registry: Callable[[], list[str]] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> list[tuple[str, str]]:
    """(source, path) pairs in the order 7-Zip is looked for.

    The injected setting, then MSDIAL_SEVENZIP, then the registry, then the Program Files
    locations, then PATH. 7-Zip's installer does not put it on PATH, so PATH comes last.
    """
    environ = os.environ if environ is None else environ
    registry = _registry_sevenzip_directories if registry is None else registry
    candidates: list[tuple[str, str]] = []
    if str(setting or "").strip():
        candidates.append(("setting", str(setting).strip()))
    if str(environ.get(SEVENZIP_ENVIRONMENT_VARIABLE, "")).strip():
        candidates.append(("environment", str(environ[SEVENZIP_ENVIRONMENT_VARIABLE]).strip()))
    for directory in registry():
        candidates.append(("registry", str(Path(directory) / _sevenzip_executable_name())))
    for variable in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        root = str(environ.get(variable, "")).strip()
        if root:
            candidates.append(
                ("program_files", str(Path(root) / "7-Zip" / _sevenzip_executable_name()))
            )
    for command in ("7z", "7zz"):
        found = which(command)
        if found:
            candidates.append(("path", found))
    unique: list[tuple[str, str]] = []
    seen: set[str] = set()
    for source, candidate in candidates:
        key = os.path.normcase(os.path.abspath(candidate))
        if key not in seen:
            seen.add(key)
            unique.append((source, candidate))
    return unique


def parse_sevenzip_info(text: str) -> dict[str, Any]:
    """Version, library path and format names from the output of `7z i`."""
    lines = text.replace("\r\n", "\n").split("\n")
    version = ""
    for line in lines:
        if line.strip():
            if line.strip().startswith("7-Zip"):
                match = re.search(r"(?<![\d.])(\d{1,3})\.(\d{2})(?![\d.])", line)
                if match:
                    version = f"{int(match.group(1))}.{match.group(2)}"
            break
    library = ""
    formats: list[str] = []
    section = ""
    for line in lines:
        stripped = line.strip()
        if stripped.endswith(":") and " " not in stripped:
            section = stripped[:-1]
            continue
        if not stripped:
            continue
        if section == "Libs" and not library:
            match = re.match(r"^\d+\s*:\s*[\d.]+\s*:\s*(.+?)\s*$", stripped)
            if match:
                library = match.group(1)
        elif section == "Formats":
            tokens = stripped.split()
            # index, flags, [a second flag column for some formats], name, extensions...
            for token in tokens[2:]:
                if "." not in token:
                    formats.append(token)
                    break
    return {"version": version, "library": library, "formats": tuple(formats)}


def _version_tuple(version: str) -> tuple[int, int]:
    major, _, minor = version.partition(".")
    return int(major), int(minor or 0)


def inspect_sevenzip(
    path: str | os.PathLike[str],
    source: str = "setting",
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> SevenZipTool:
    """Run `7z i` on a candidate and describe it, or raise ArchiveToolError."""
    executable = Path(path)
    if executable.is_dir():
        executable = executable / _sevenzip_executable_name()
    if not executable.is_file():
        raise ArchiveToolError(
            "sevenzip_not_found", f"7-Zip was not found at {executable} ({source}).",
            detail={"source": source},
        )
    try:
        completed = run(
            [str(executable), "i", "-sccUTF-8"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ArchiveToolError(
            "sevenzip_unusable", f"7-Zip at {executable} did not run: {error}",
            detail={"source": source},
        ) from error
    output = completed.stdout
    text = output.decode("utf-8", "replace") if isinstance(output, bytes) else str(output or "")
    info = parse_sevenzip_info(text)
    if completed.returncode != 0 or not info["version"]:
        raise ArchiveToolError(
            "sevenzip_version_unreadable",
            f"`7z i` at {executable} exited {completed.returncode} without a readable version.",
            detail={"source": source},
        )
    if _version_tuple(info["version"]) < MINIMUM_SEVENZIP_VERSION:
        minimum = "%d.%02d" % MINIMUM_SEVENZIP_VERSION
        raise ArchiveToolError(
            "sevenzip_version_too_old",
            f"7-Zip {info['version']} at {executable} is older than the required {minimum}.",
            detail={"source": source, "version": info["version"]},
        )
    library = Path(info["library"]) if info["library"] else executable.parent / "7z.dll"
    return SevenZipTool(
        executable=str(executable),
        version=info["version"],
        source=source,
        executable_sha256=file_sha256(executable),
        library=str(library) if library.is_file() else "",
        library_sha256=file_sha256(library) if library.is_file() else "",
        formats=info["formats"],
    )


def find_sevenzip(
    setting: str | None = None,
    *,
    environ: dict[str, str] | None = None,
    registry: Callable[[], list[str]] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> SevenZipTool:
    """The 7-Zip to use, or ArchiveToolError.

    A configured path (the setting or MSDIAL_SEVENZIP) is authoritative: when it is unusable the
    search stops there rather than quietly running some other 7-Zip. Discovered candidates are
    tried in order, and the first that runs and is new enough is used.
    """
    rejected: list[ArchiveToolError] = []
    for source, candidate in sevenzip_candidates(
        setting, environ=environ, registry=registry, which=which
    ):
        explicit = source in ("setting", "environment")
        if not explicit and not Path(candidate).is_file():
            continue
        try:
            return inspect_sevenzip(candidate, source, run=run)
        except ArchiveToolError as error:
            if explicit:
                raise
            rejected.append(error)
    if rejected:
        first = rejected[0]
        raise ArchiveToolError(
            first.reason,
            "No usable 7-Zip was found: " + "; ".join(error.message for error in rejected),
            detail={"rejected": [error.record() for error in rejected]},
        )
    raise ArchiveToolError(
        "sevenzip_not_found",
        "7-Zip was not found in the setting, MSDIAL_SEVENZIP, the registry, Program Files or "
        "PATH. It is needed for 7z and rar archives and for zip methods Python cannot read.",
    )


# --- Listings and their validation ----------------------------------------------------------

@dataclass
class ArchiveMember:
    name: str
    is_dir: bool = False
    size: int = 0
    crc32: str = ""
    modified: str = ""
    encrypted: bool = False
    # Why the entry is not a plain file or directory: symbolic_link, hard_link, copy_link,
    # reparse_point, special_file or alternate_stream. Any value refuses the archive.
    unsafe_type: str = ""
    method: str = ""
    checksum: str = ""
    path: str = ""


@dataclass
class ArchiveListing:
    detection: ArchiveDetection
    reader: str
    members: list[ArchiveMember]
    tool: dict[str, Any]
    argv: list[str] = field(default_factory=list)
    fallback_reason: str = ""
    reported_type: str = ""
    sevenzip: SevenZipTool | None = None


def normalise_member_name(name: str) -> tuple[str, str]:
    """(relative path with '/' separators, '') or ('', reason the name is refused).

    Both separators count, since Windows treats both as one. '.' and empty components are dropped;
    everything else that 7-Zip or Windows would rewrite, reinterpret or refuse is refused here.
    """
    text = str(name).replace("\\", "/")
    if any("\ud800" <= character <= "\udfff" for character in text):
        # tarfile decodes a name that is not UTF-8 (cp932 from a Japanese instrument PC, say) with
        # surrogateescape. Written out, it is a file no sample name can match and the listing TSV
        # cannot encode; guessing its code page would be an inference, so it is refused.
        return "", "undecodable_name"
    if not text.strip("/"):
        return "", "empty_name"
    if text.startswith("//"):
        return "", "unc_path"
    if text.startswith("/"):
        return "", "absolute_path"
    if len(text) >= 2 and text[1] == ":" and text[0].isascii() and text[0].isalpha():
        return "", "drive_path"
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts:
        return "", "empty_name"
    if ".." in parts:
        return "", "parent_traversal"
    for part in parts:
        if ":" in part:
            return "", "colon_in_name"
        if any(ord(character) < 32 or character in _INVALID_CHARACTERS for character in part):
            return "", "invalid_character"
        if part[-1] in ". ":
            return "", "trailing_dot_or_space"
        if part.split(".", 1)[0].rstrip(" ").casefold() in _RESERVED_NAMES:
            return "", "reserved_name"
    return "/".join(parts), ""


def _encodable(name: str) -> str:
    """name, with any lone surrogate spelled as an escape so a UTF-8 record can hold it."""
    return name.encode("utf-8", "backslashreplace").decode("utf-8")


def validate_listing(
    members: list[ArchiveMember],
    bases: Iterable[str | os.PathLike[str]] = (),
    *,
    max_path_length: int = 259,
    max_directory_length: int = 247,
    sizes_known: bool = True,
) -> list[dict[str, str]]:
    """Refusals for a listing, one per refused member; also fills each member's path.

    bases are the directories the members will be written under. The longest decides the length
    check, so the staging directory, which is longer than the destination, is the one that counts.
    A file's path is held to max_path_length and the folder it needs (itself, for a directory)
    to max_directory_length: without long-path support Windows creates no longer folder, and
    zipfile would fail mid-write where 7-Zip, which writes \\\\?\\ paths, would succeed.
    """
    base_length = max((len(str(base)) for base in bases), default=0)
    rejected: list[dict[str, str]] = []
    spelling: dict[str, str] = {}
    kinds: dict[str, str] = {}

    def register(path: str, kind: str) -> str:
        key = path.casefold()
        if key not in spelling:
            spelling[key] = path
            kinds[key] = kind
            return ""
        if spelling[key] != path:
            return "case_duplicate"
        if kinds[key] != kind:
            return "path_conflict"
        return "duplicate_name" if kind == "file" else ""

    for member in members:
        path, reason = normalise_member_name(member.name)
        member.path = path
        if reason == "empty_name" and member.is_dir and not str(member.name).strip("/\\."):
            # './' in a tar made with `tar cf x.tar .` is the extraction root itself.
            continue
        if not reason and member.encrypted:
            reason = "encrypted"
        if not reason and member.unsafe_type:
            reason = member.unsafe_type
        if not reason and sizes_known and not member.is_dir and member.size < 0:
            reason = "unknown_size"
        if not reason and base_length:
            folder = path if member.is_dir else path.rpartition("/")[0]
            folder_length = base_length + 1 + len(folder) if folder else base_length
            if base_length + 1 + len(path) > max_path_length or folder_length > max_directory_length:
                reason = "path_too_long"
        if not reason:
            parts = path.split("/")
            for index in range(1, len(parts)):
                reason = register("/".join(parts[:index]), "dir")
                if reason:
                    break
            if not reason:
                reason = register(path, "dir" if member.is_dir else "file")
        if reason:
            rejected.append({"name": _encodable(member.name), "reason": reason})
    return rejected


def _refusal(rejected: list[dict[str, str]], archive_name: str) -> ArchiveError:
    reasons = sorted({item["reason"] for item in rejected})
    code = "encrypted_archive" if "encrypted" in reasons else "unsafe_listing"
    sample = ", ".join(f"{item['name']!r} ({item['reason']})" for item in rejected[:5])
    return ArchiveError(
        code,
        f"{archive_name} was refused before extraction: {len(rejected)} member(s) "
        f"[{', '.join(reasons)}], for example {sample}.",
        rejected=rejected,
    )


def _python_tool(module: str) -> dict[str, Any]:
    return {
        "name": f"python-{module}",
        "version": platform.python_version(),
        "implementation": platform.python_implementation(),
    }


def _stdlib_zip_methods() -> set[int]:
    methods = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA}
    zstandard = getattr(zipfile, "ZIP_ZSTANDARD", None)
    if zstandard is not None:
        try:
            import compression.zstd  # noqa: F401 - availability probe
        except ImportError:
            pass
        else:
            methods.add(zstandard)
    return methods


_ZIP_METHOD_NAMES = {
    0: "Store", 6: "Implode", 8: "Deflate", 9: "Deflate64", 12: "BZip2", 14: "LZMA",
    93: "Zstandard", 95: "XZ", 98: "PPMd", 99: "AES",
}


def _zip_unsafe_type(info: zipfile.ZipInfo) -> str:
    mode = info.external_attr >> 16
    kind = stat.S_IFMT(mode)
    if kind == stat.S_IFLNK:
        return "symbolic_link"
    if kind and kind not in (stat.S_IFREG, stat.S_IFDIR):
        return "special_file"
    if info.external_attr & _REPARSE_POINT:
        return "reparse_point"
    return ""


def _list_zip(path: Path) -> tuple[list[ArchiveMember], set[int]]:
    with zipfile.ZipFile(path) as handle:
        infos = handle.infolist()
    supported = _stdlib_zip_methods()
    members, unsupported = [], set()
    for info in infos:
        encrypted = bool(info.flag_bits & 0x1) or info.compress_type == 99
        if not encrypted and not info.is_dir() and info.compress_type not in supported:
            unsupported.add(info.compress_type)
        members.append(
            ArchiveMember(
                name=info.filename,
                is_dir=info.is_dir(),
                size=info.file_size,
                crc32=f"{info.CRC:08X}",
                modified="%04d-%02d-%02d %02d:%02d:%02d" % info.date_time,
                encrypted=encrypted,
                unsafe_type=_zip_unsafe_type(info),
                method=_ZIP_METHOD_NAMES.get(info.compress_type, str(info.compress_type)),
            )
        )
    return members, unsupported


def _tar_mode(kind: str) -> str:
    return {"tar": "r:", "tar.gz": "r:gz", "tar.bz2": "r:bz2", "tar.xz": "r:xz"}[kind]


def _tar_unsafe_type(info: tarfile.TarInfo) -> str:
    if info.issym():
        return "symbolic_link"
    if info.islnk():
        return "hard_link"
    if not (info.isreg() or info.isdir()):
        return "special_file"
    return ""


def _list_tar(path: Path, kind: str, max_members: int) -> list[ArchiveMember]:
    members = []
    with tarfile.open(path, _tar_mode(kind)) as handle:
        for info in handle:
            members.append(
                ArchiveMember(
                    name=info.name,
                    is_dir=info.isdir(),
                    size=0 if info.isdir() else int(info.size),
                    modified=datetime.fromtimestamp(info.mtime, timezone.utc).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    unsafe_type=_tar_unsafe_type(info),
                )
            )
            if len(members) > max_members:
                raise ArchiveError(
                    "member_count_exceeded",
                    f"{path.name} holds more than {max_members} members.",
                )
    return members


# 7-Zip's `l -slt` output: a preamble, '--', the archive's own properties, '----------', then one
# block of 'Key = Value' lines per member, blocks separated by blank lines. A value with line
# breaks (an archive comment) is printed as 'Key = ', then '{', its lines, and '}'. Warnings and
# errors arrive as lines that are not properties, in the archive block or after the last member,
# or as the ERROR and WARNING properties.
def _split_property(line: str) -> tuple[str | None, str]:
    key, separator, value = line.partition(" = ")
    if separator:
        return key, value
    if line.endswith(" ="):
        return line[:-2], ""
    return None, ""


def _unix_mode(text: str) -> int | None:
    text = text.strip()
    if re.fullmatch(r"[0-9A-Fa-f]{8}", text):
        return int(text, 16) >> 16
    if len(text) == 10 and text[0] in "-dlcbps":
        return {
            "-": stat.S_IFREG, "d": stat.S_IFDIR, "l": stat.S_IFLNK, "c": stat.S_IFCHR,
            "b": stat.S_IFBLK, "p": stat.S_IFIFO, "s": stat.S_IFSOCK,
        }[text[0]]
    return None


def _member_from_properties(properties: dict[str, str]) -> ArchiveMember:
    attributes = properties.get("Attributes", "")
    windows, _, extended = attributes.partition(" ")
    is_dir = properties.get("Folder") == "+" or "D" in windows
    unsafe = ""
    for key in ("Symbolic Link", "Hard Link", "Copy Link", "Link"):
        if properties.get(key, "").strip():
            unsafe = key.casefold().replace(" ", "_")
            break
    if not unsafe and "L" in windows:
        unsafe = "reparse_point"
    if not unsafe and extended:
        mode = _unix_mode(extended)
        kind = stat.S_IFMT(mode) if mode is not None else 0
        if kind == stat.S_IFLNK:
            unsafe = "symbolic_link"
        elif kind and kind not in (stat.S_IFREG, stat.S_IFDIR):
            unsafe = "special_file"
    if not unsafe and properties.get("Alternate Stream") == "+":
        unsafe = "alternate_stream"
    size_text = properties.get("Size", "").strip()
    return ArchiveMember(
        name=properties["Path"],
        is_dir=is_dir,
        size=int(size_text) if size_text.isdigit() else (0 if is_dir else -1),
        crc32=properties.get("CRC", "").strip(),
        checksum=properties.get("Checksum", "").strip(),
        modified=properties.get("Modified", "").strip(),
        encrypted=properties.get("Encrypted") == "+",
        unsafe_type=unsafe,
        method=properties.get("Method", "").strip(),
    )


class _SevenZipListing:
    def __init__(self, max_members: int) -> None:
        self.max_members = max_members
        self.state = "preamble"
        self.archive: dict[str, str] = {}
        self.messages: list[str] = []
        self.members: list[ArchiveMember] = []
        self.block: dict[str, str] = {}
        # The last property printed with an empty value, whose lines may follow between braces,
        # and those lines while they are being read.
        self._open: tuple[dict[str, str], str] | None = None
        self._value: list[str] | None = None

    def feed(self, line: str) -> None:
        if self.state == "preamble":
            if line == "--":
                self.state = "archive"
            return
        if self._value is not None:
            # Inside a braced value every line is text, blank lines and '----------' included.
            if line == "}":
                target, key = self._open
                target[key] = "\n".join(self._value)
                self._open = self._value = None
            else:
                self._value.append(line)
            return
        if self._open is not None:
            if line == "{":
                self._value = []
                return
            self._open = None
        if self.state == "archive":
            if line == "----------":
                self.state = "members"
                return
            if not line:
                return
            key, value = _split_property(line)
            if key is None:
                self.messages.append(line)
            else:
                self.archive[key] = value
                if not value:
                    self._open = (self.archive, key)
            return
        if not line:
            self._close()
            return
        key, value = _split_property(line)
        if key is None:
            if self.block:
                raise ArchiveError(
                    "unparsable_listing", f"7-Zip listing line not understood: {line!r}"
                )
            self.messages.append(line)
            return
        if key in self.block:
            raise ArchiveError(
                "unparsable_listing", f"7-Zip listing repeats {key!r} within one member."
            )
        self.block[key] = value
        if not value:
            self._open = (self.block, key)

    def diagnostics(self) -> list[str]:
        """What 7-Zip said about the archive: its non-property lines and ERROR/WARNING values."""
        return self.messages + [
            f"{key}: {self.archive[key]}"
            for key in ("ERROR", "WARNING", "Errors", "Warnings")
            if key in self.archive
        ]

    def _close(self) -> None:
        if not self.block:
            return
        if "Path" not in self.block:
            raise ArchiveError("unparsable_listing", "A 7-Zip listing block has no Path.")
        self.members.append(_member_from_properties(self.block))
        self.block = {}
        if len(self.members) > self.max_members:
            raise ArchiveError(
                "member_count_exceeded", f"The archive holds more than {self.max_members} members."
            )

    def finish(self) -> None:
        if self._value is not None:
            raise ArchiveError(
                "unparsable_listing", "The 7-Zip listing ends inside a multi-line value."
            )
        self._close()


@dataclass
class _ToolRun:
    exit_code: int
    stdout_tail: str
    stderr_tail: str
    killed_reason: str
    elapsed_seconds: float
    # What a watch that raised said, when that is why the tool was killed.
    killed_detail: str = ""


def _tail(chunks: list[bytes], limit: int = 65536) -> str:
    return b"".join(chunks)[-limit:].decode("utf-8", "replace")


def _run_tool(
    argv: list[str],
    *,
    timeout: float,
    on_line: Callable[[str], None] | None = None,
    watch: Callable[[], str] | None = None,
    interval: float = 5.0,
) -> _ToolRun:
    """Run a tool with stdin closed, a timeout and a watchdog; kill it on any breach.

    The watch runs once before the first wait, so a breach that exists already stops the tool
    before it writes, and then every interval. A watch that raises is a breach too
    (watchdog_failed): a free-space probe that cannot answer has not said there is room. on_line
    sees stdout line by line; an ArchiveError it raises (a listing over the member limit) kills
    the tool too. Whatever leaves the loop, the tool is dead before this returns or raises, so a
    caller's cleanup never races it.
    """
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creationflags,
    )
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    failures: list[ArchiveError] = []

    def read_stdout() -> None:
        assert process.stdout is not None
        for raw in process.stdout:
            stdout_chunks.append(raw)
            if len(stdout_chunks) > 4096:
                del stdout_chunks[:2048]
            if on_line is None or failures:
                continue
            try:
                on_line(raw.decode("utf-8", "replace").rstrip("\r\n"))
            except ArchiveError as error:
                failures.append(error)

    def read_stderr() -> None:
        assert process.stderr is not None
        for raw in process.stderr:
            stderr_chunks.append(raw)
            if len(stderr_chunks) > 4096:
                del stderr_chunks[:2048]

    readers = [threading.Thread(target=read_stdout, daemon=True),
               threading.Thread(target=read_stderr, daemon=True)]
    for reader in readers:
        reader.start()
    started = time.monotonic()
    killed = ""
    killed_detail = ""
    try:
        while True:
            if failures:
                killed = failures[0].reason
            elif watch is not None:
                try:
                    killed = watch() or ""
                except Exception as error:  # noqa: BLE001 - any failure of the probe is a breach
                    killed = "watchdog_failed"
                    killed_detail = f"{type(error).__name__}: {error}"
            if not killed and time.monotonic() - started > timeout:
                killed = "tool_timeout"
            if killed:
                break
            try:
                process.wait(timeout=interval)
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        for reader in readers:
            reader.join(timeout=30)
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
    if failures:
        raise failures[0]
    return _ToolRun(
        exit_code=process.returncode,
        stdout_tail=_tail(stdout_chunks),
        stderr_tail=_tail(stderr_chunks),
        killed_reason=killed,
        elapsed_seconds=round(time.monotonic() - started, 3),
        killed_detail=killed_detail,
    )


# 7-Zip's own diagnoses, tried in this order. Damage comes first: a truncated download is the
# common failure and a retry can mend it, while encrypted_archive is permanent. The damage
# patterns exclude 7-Zip's wrong-password variants ('CRC Failed in encrypted file. Wrong
# password?', 'Data Error in encrypted file. ...'), so those read as encryption. 'Headers Error'
# follows encryption because a header-encrypted 7z reports it beside 'Cannot open encrypted
# archive'.
_SEVENZIP_DIAGNOSES = (
    ("corrupt_archive", re.compile(
        r"crc failed(?! in encrypted)|data error(?! in encrypted)|unexpected end|unavailable data"
    )),
    ("encrypted_archive", re.compile(
        r"wrong password|cannot open encrypted archive|in encrypted file"
    )),
    ("corrupt_archive", re.compile(r"headers error")),
    ("not_an_archive", re.compile(r"can ?not open (?:the )?file as|is not archive")),
)


def _classify_sevenzip_failure(exit_code: int, lines: list[str]) -> tuple[str, list[str]]:
    """(reason, the lines that decided it) for a failed 7-Zip run.

    lines are 7-Zip's messages: stderr, and the stdout lines that are neither a property nor a
    property's value. Properties are never read. Every zip and rar block carries 'Encrypted = -',
    and searching the whole output for 'encrypted' named every truncated download encrypted.
    """
    for reason, pattern in _SEVENZIP_DIAGNOSES:
        evidence = [line for line in lines if pattern.search(line.casefold())]
        if evidence:
            return reason, evidence
    return ("sevenzip_warning" if exit_code == 1 else "sevenzip_failed"), []


def _stdout_messages(text: str) -> list[str]:
    """The diagnostic lines of 7-Zip's stdout: after the '--' marker, never properties or values."""
    parser = _SevenZipListing(sys.maxsize)
    try:
        for line in text.replace("\r\n", "\n").split("\n"):
            parser.feed(line)
    except ArchiveError:
        pass  # a tail that does not parse still yields the messages read before it
    return parser.diagnostics()


def _sevenzip_error(
    run: _ToolRun, archive_name: str, action: str, messages: list[str] | None = None
) -> ArchiveError:
    """The ArchiveError for a 7-Zip run that exited non-zero.

    messages are stdout's diagnostic lines when the caller parsed the whole stream (a listing's
    archive block can be far behind the tail); otherwise they are read from the stdout tail.
    """
    stdout_messages = _stdout_messages(run.stdout_tail) if messages is None else messages
    lines = [line.strip() for line in run.stderr_tail.splitlines() if line.strip()]
    lines += [line.strip() for line in stdout_messages if line.strip()]
    reason, evidence = _classify_sevenzip_failure(run.exit_code, lines)
    shown = evidence[:3] or lines[-3:] or [
        line.strip() for line in run.stdout_tail.splitlines() if line.strip()
    ][-3:]
    text = (run.stderr_tail + "\n" + run.stdout_tail).strip()
    return ArchiveError(
        reason,
        f"7-Zip {action} of {archive_name} exited {run.exit_code}: {' | '.join(shown)}",
        detail={"exit_code": run.exit_code, "messages": lines[-40:], "output_tail": text[-4000:]},
    )


def _list_sevenzip(
    tool: SevenZipTool, archive: Path, signature: str, limits: ExtractionLimits
) -> tuple[list[ArchiveMember], dict[str, str], list[str]]:
    argv = [
        tool.executable, "l", "-slt", "-sccUTF-8", f"-p{SENTINEL_PASSWORD}",
        f"-t{_SEVENZIP_TYPE_SWITCH[signature]}", "--", str(archive),
    ]
    parser = _SevenZipListing(limits.max_members)
    run = _run_tool(
        argv,
        timeout=limits.listing_timeout_seconds,
        on_line=parser.feed,
        interval=min(limits.watchdog_interval_seconds, 1.0),
    )
    if run.killed_reason:
        raise ArchiveError(
            run.killed_reason, f"7-Zip listing of {archive.name} was stopped: {run.killed_reason}."
        )
    if run.exit_code != 0:
        raise _sevenzip_error(run, archive.name, "listing", parser.diagnostics())
    parser.finish()
    if parser.state != "members":
        raise ArchiveError(
            "unparsable_listing", f"7-Zip listed {archive.name} without a member section."
        )
    warnings = parser.diagnostics()
    if warnings:
        # Exit 0 with warnings (data after the end of the archive, for one) is still a warning,
        # and a warning is a failure here.
        raise ArchiveError(
            "sevenzip_warning",
            f"7-Zip reported warnings while listing {archive.name}: {' | '.join(warnings)}",
            detail={"warnings": warnings},
        )
    return parser.members, parser.archive, argv


# 7-Zip failures that, after zipfile has refused a zip, say only that the zip is damaged.
_SEVENZIP_READ_FAILURES = frozenset(
    {"corrupt_archive", "not_an_archive", "sevenzip_failed", "sevenzip_warning"}
)


def list_archive(
    path: str | os.PathLike[str],
    *,
    detection: ArchiveDetection | None = None,
    sevenzip: SevenZipTool | None = None,
    sevenzip_setting: str | None = None,
    reader: str = "auto",
    limits: ExtractionLimits | None = None,
) -> ArchiveListing:
    """List an archive's members with the reader that will extract it. Writes nothing.

    zip, tar and compressed streams are read with the standard library. A zip whose methods it
    cannot read (Deflate64 from Windows Explorer, PPMd, and so on) or that it refuses to open falls
    back to 7-Zip, as do 7z and rar always. reader='sevenzip' sends a zip to 7-Zip directly. A zip
    that zipfile refused and 7-Zip cannot read, or that finds no 7-Zip, is corrupt_archive.
    """
    path = Path(path)
    limits = limits or ExtractionLimits()
    detection = detection or detect_archive(path)
    if detection is None:
        raise ArchiveError("not_an_archive_name", f"{path.name} does not have an archive suffix.")
    kind = detection.kind
    fallback = ""
    stdlib_error = ""
    if kind == "zip":
        if reader == "sevenzip":
            fallback = "reader_requested"
        else:
            try:
                members, unsupported = _list_zip(path)
            except (zipfile.BadZipFile, NotImplementedError, ValueError) as error:
                fallback = f"stdlib_refused:{type(error).__name__}"
                stdlib_error = f"{type(error).__name__}: {error}"
            else:
                # Encrypted members are refused whatever the method, so they need no 7-Zip.
                if not unsupported or any(member.encrypted for member in members):
                    return ArchiveListing(detection, "zipfile", members, _python_tool("zipfile"))
                fallback = "zip_method_unsupported_by_stdlib:" + ",".join(
                    _ZIP_METHOD_NAMES.get(method, str(method)) for method in sorted(unsupported)
                )
    elif kind in TAR_KINDS:
        try:
            members = _list_tar(path, kind, limits.max_members)
        except (tarfile.TarError, EOFError, lzma.LZMAError, zlib.error, OSError) as error:
            raise ArchiveError(
                "corrupt_archive", f"{path.name} could not be listed: {error}"
            ) from error
        return ArchiveListing(detection, "tarfile", members, _python_tool("tarfile"))
    elif kind in STREAM_KINDS:
        module = {"gz": "gzip", "bz2": "bz2", "xz": "lzma", "lzma": "lzma"}[kind]
        member = ArchiveMember(name=archive_stem(path.name), size=-1)
        return ArchiveListing(detection, module, [member], _python_tool(module))
    try:
        tool = sevenzip or find_sevenzip(sevenzip_setting)
        # Only zip, 7z, rar4 and rar5 reach here, and each names its own -t switch.
        signature = detection.signature
        if not tool.supports(signature):
            raise ArchiveToolError(
                "format_unsupported_by_sevenzip",
                f"7-Zip {tool.version} at {tool.executable} lists no "
                f"{_SEVENZIP_FORMAT_NAME[signature]} handler, so {path.name} cannot be read.",
            )
        members, properties, argv = _list_sevenzip(tool, path, signature, limits)
    except ArchiveError as error:
        if stdlib_error and (
            isinstance(error, ArchiveToolError) or error.reason in _SEVENZIP_READ_FAILURES
        ):
            # zipfile lists any well-formed single-volume zip, whatever its methods; refusing it
            # means the structure is damaged (a truncated download, as a rule; the Catalog has no
            # split volumes). 7-Zip was a second opinion, and neither its absence nor its failure
            # is the unit's reason.
            unavailable = isinstance(error, ArchiveToolError)
            raise ArchiveError(
                "corrupt_archive",
                f"{path.name} is a damaged zip: Python's zipfile refused it ({stdlib_error}), and "
                + ("7-Zip, which might have read it, is not available: " if unavailable
                   else "7-Zip could not read it either: ")
                + error.message,
                detail={"stdlib_error": stdlib_error, "sevenzip_error": error.record()},
            ) from error
        raise
    return ArchiveListing(
        detection, "7-Zip", members, tool.record(), argv=argv, fallback_reason=fallback,
        reported_type=properties.get("Type", ""), sevenzip=tool,
    )


# What an operating system adds when it packs a folder, and never data. Finder writes an
# AppleDouble copy of every file under __MACOSX/ and a .DS_Store into folders, tar on macOS
# writes the AppleDouble data as '._<name>' beside each file, and Explorer leaves Thumbs.db.
# Kept, __MACOSX/S1.d/ looks like a second Bruker folder, and any of them is a second top-level
# entry that sends S1.d.zip into S1.d/S1.d/.
_METADATA_DIRECTORY = "__macosx"
_METADATA_FILES = frozenset({".ds_store", "thumbs.db"})


def _metadata_entry(path: str, is_dir: bool = False) -> str:
    """The entry to drop (a member path, or '__MACOSX') when path is such metadata, else ''."""
    if not path:
        return ""
    first = path.split("/", 1)[0]
    if first.casefold() == _METADATA_DIRECTORY:
        return first
    name = path.rsplit("/", 1)[-1]
    if not is_dir and (name.casefold() in _METADATA_FILES or name.startswith("._")):
        return path
    return ""


def _kept_paths(members: list[ArchiveMember], kind: str) -> list[tuple[ArchiveMember, str]]:
    """(member, normalised path) for the members that are data, before validation fills paths."""
    kept = []
    for member in members:
        path = normalise_member_name(member.name)[0]
        if path and (kind in STREAM_KINDS or not _metadata_entry(path, member.is_dir)):
            kept.append((member, path))
    return kept


def destination_rule(
    archive_name: str, members: list[ArchiveMember], *, kind: str = "", nested: bool = False
) -> tuple[str, str, str]:
    """(rule, directory, container) for where an archive's members go, relative to where it expands.

    A compressed stream is one file beside where the archive was. An archived vendor container
    (X.raw.zip, X.d.zip) keeps a top-level folder of its own name when it has one, and otherwise
    gets one, which is what stops two root-less per-sample zips from writing _FUNC001.DAT over each
    other. A container that is a single file (a Thermo X.raw zipped alone, X.mzML.gz) lands as that
    file. Any other nested archive expands into a directory named after it, unless it already
    holds exactly that directory; a top-level bundle keeps its own relative paths. Operating-system
    metadata is not an entry here, since it is dropped.

    container is the vendor container the archive produces, relative to where it expands ('' when
    the archive is not an archived container, or no member can be said to be it). A container
    packed under another sample's name (A.raw.zip holding B.raw) is extracted as it was packed, and
    container names B.raw: renaming it would decide which sample it is.
    """
    stem = archive_stem(archive_name)
    container = container_suffix(stem)
    if kind in STREAM_KINDS:
        return "stream_file", "", stem if container else ""
    kept = _kept_paths(members, kind)
    files = [path for member, path in kept if not member.is_dir]
    tops = {path.split("/", 1)[0] for _member, path in kept}
    top = next(iter(tops)) if len(tops) == 1 else ""
    top_is_dir = bool(top) and any(
        path.startswith(top + "/") or (member.is_dir and path == top) for member, path in kept
    )
    if container:
        if top_is_dir and top.casefold() == stem.casefold():
            return "container_rooted", "", top
        if top_is_dir and top.casefold().endswith(container):
            return "container_rooted_other_name", "", top
        if len(files) == 1 and "/" not in files[0] and files[0].casefold().endswith(container):
            return "container_single_file", "", files[0]
        if container in FOLDER_CONTAINER_SUFFIXES:
            return "container_stem", stem, stem
        at_root = [path for path in files if "/" not in path and path.casefold().endswith(container)]
        named = [path for path in at_root if path.casefold() == stem.casefold()]
        produced = named[0] if named else (at_root[0] if len(at_root) == 1 else "")
        return "container_files", "", produced
    if nested:
        if top_is_dir and top.casefold() == stem.casefold():
            return "nested_rooted", "", ""
        return "nested_directory", stem, ""
    return "archive_root", "", ""


def _container_record(archive_name: str, container: str, relative: str) -> dict[str, Any]:
    """Where the container an archived container stands for is, and whether it has that name."""
    alias = container_alias(archive_name)
    produced = container.rsplit("/", 1)[-1]
    return {
        # Relative to the outermost destination, so attribution uses what exists on disk.
        "container_root": "/".join(part for part in (relative, container) if part) if container else "",
        "container_name_mismatch": bool(alias) and produced.casefold() != alias.casefold(),
    }


# --- Extraction ------------------------------------------------------------------------------

def file_sha256(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(8 * _CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_digests(path: str | os.PathLike[str]) -> dict[str, str]:
    """sha256, md5 and sha1 of a file, in one read."""
    digests = {name: hashlib.new(name) for name in ("sha256", "md5", "sha1")}
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(8 * _CHUNK), b""):
            for digest in digests.values():
                digest.update(chunk)
    return {name: digest.hexdigest() for name, digest in digests.items()}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def _existing_ancestor(path: Path) -> Path:
    current = path
    while not current.exists() and current.parent != current:
        current = current.parent
    return current


def _remove_tree(path: Path) -> None:
    """Remove a tree this module created, clearing read-only attributes 7-Zip may have restored."""
    if not os.path.lexists(path):
        return

    def retry(function: Callable[[str], Any], target: str, _error: Any) -> None:
        try:
            os.chmod(target, stat.S_IWRITE)
            function(target)
        except OSError:
            pass

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


def _tree_bytes(root: Path) -> int:
    total = 0
    stack = [str(root)]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _copy_capped(
    source: Any, target: Path, expected: int, on_chunk: Callable[[int], None] | None = None
) -> int:
    written = 0
    with open(target, "xb") as output:
        while True:
            chunk = source.read(_CHUNK)
            if not chunk:
                break
            written += len(chunk)
            if expected >= 0 and written > expected:
                raise ArchiveError(
                    "member_larger_than_listed",
                    f"{target.name} expanded past the {expected} bytes its listing declared.",
                )
            output.write(chunk)
            if on_chunk is not None:
                on_chunk(len(chunk))
    if expected >= 0 and written != expected:
        raise ArchiveError(
            "member_size_mismatch",
            f"{target.name} expanded to {written} bytes; its listing declared {expected}.",
        )
    return written


def _drain(stream: Any) -> None:
    # Reading a compressed stream to its end is what makes gzip, bz2 and xz check their CRC.
    while stream.read(8 * _CHUNK):
        pass


def _xz_has_check(path: Path) -> bool:
    decompressor = lzma.LZMADecompressor(format=lzma.FORMAT_XZ)
    with open(path, "rb") as handle:
        decompressor.decompress(handle.read(65536), max_length=1)
    return decompressor.check not in (lzma.CHECK_NONE, lzma.CHECK_UNKNOWN)


def _member_target(root: Path, path: str) -> Path:
    return root.joinpath(*path.split("/"))


def _verify_tree(
    root: Path, files: dict[str, int], directories: Iterable[str], label: str
) -> dict[str, int]:
    """Compare the tree under root with the expected files (casefolded path -> size)."""
    found_files: dict[str, int] = {}
    found_directories: set[str] = set()
    for current, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            full = os.path.join(current, name)
            status = os.lstat(full)
            relative = os.path.relpath(full, root).replace(os.sep, "/").casefold()
            if stat.S_ISLNK(status.st_mode) or getattr(status, "st_file_attributes", 0) & _REPARSE_POINT:
                raise ArchiveError(
                    "link_on_disk", f"{label}: {relative} was extracted as a link or reparse point."
                )
            if stat.S_ISDIR(status.st_mode):
                found_directories.add(relative)
            elif stat.S_ISREG(status.st_mode):
                found_files[relative] = status.st_size
            else:
                raise ArchiveError(
                    "special_file_on_disk", f"{label}: {relative} is not a regular file."
                )
    implied = {
        "/".join(path.split("/")[:index])
        for path in list(files) + [directory.casefold() for directory in directories]
        for index in range(1, path.count("/") + 1)
    }
    expected_directories = {directory.casefold() for directory in directories} | implied
    missing = sorted(set(files) - set(found_files))
    unexpected = sorted(set(found_files) - set(files))
    mismatched = sorted(path for path in files if path in found_files and found_files[path] != files[path])
    missing_directories = sorted(expected_directories - found_directories)
    unexpected_directories = sorted(found_directories - expected_directories)
    if missing or unexpected or mismatched or missing_directories or unexpected_directories:
        raise ArchiveError(
            "extraction_mismatch",
            f"{label}: the extracted tree does not match the listing "
            f"({len(missing)} missing, {len(unexpected)} unexpected, {len(mismatched)} of the wrong "
            f"size, {len(missing_directories) + len(unexpected_directories)} directory differences).",
            detail={
                "missing": missing[:20],
                "unexpected": unexpected[:20],
                "size_mismatch": mismatched[:20],
                "missing_directories": missing_directories[:20],
                "unexpected_directories": unexpected_directories[:20],
            },
        )
    return {
        "files": len(found_files),
        "directories": len(found_directories),
        "bytes": sum(found_files.values()),
    }


class _Extraction:
    """One extract_archive call: the shared budget, the tool, and the rows of the listing TSV."""

    def __init__(
        self,
        limits: ExtractionLimits,
        free_bytes: Callable[[Path], int],
        sevenzip: SevenZipTool | None,
        sevenzip_setting: str | None,
        reader: str,
        outer_bytes: int,
    ) -> None:
        self.limits = limits
        self.free_bytes = free_bytes
        self.sevenzip = sevenzip
        self.sevenzip_setting = sevenzip_setting
        self.reader = reader
        self.outer_bytes = max(1, outer_bytes)
        self.members = 0
        self.expanded = 0
        self.rows: list[dict[str, Any]] = []
        # Directories under the staging root that exist although no kept row implies them: the
        # folder a nested archive expanded into, the folder that held it, the folder that held
        # only metadata that was dropped.
        self.directories: list[str] = []
        self.counter = 0

    # The guards. A compressed stream declares no size, so it is checked as it expands.
    def admit(self, count: int, declared: int, depth: int, where: Path, label: str) -> None:
        limits = self.limits
        if depth > limits.max_depth:
            raise ArchiveError(
                "nesting_depth_exceeded",
                f"{label} is nested {depth} deep; at most {limits.max_depth} levels are expanded.",
            )
        if self.members + count > limits.max_members:
            raise ArchiveError(
                "member_count_exceeded",
                f"{label} would bring the extraction to {self.members + count} members; the limit "
                f"is {limits.max_members}.",
            )
        self._check_ratio(self.expanded + declared, label)
        free = self.free_bytes(_existing_ancestor(where))
        if declared > free - limits.reserve_bytes:
            raise ArchiveError(
                "insufficient_disk_space",
                f"{label} expands to {declared} bytes; {free} are free and {limits.reserve_bytes} "
                "are held in reserve.",
                detail={"declared_bytes": declared, "free_bytes": free},
            )
        self.members += count
        self.expanded += declared

    def _check_ratio(self, total: int, label: str) -> None:
        limits = self.limits
        ratio = total / self.outer_bytes
        if total > limits.ratio_floor_bytes and ratio > limits.max_ratio:
            raise ArchiveError(
                "expansion_ratio_exceeded",
                f"{label} would expand {self.outer_bytes} archived bytes to {total} "
                f"(ratio {ratio:.0f}, limit {limits.max_ratio:g} above {limits.ratio_floor_bytes} "
                "bytes).",
            )

    def stream_guard(self, where: Path, label: str) -> Callable[[int], None]:
        state = {"since_check": 0}

        def on_chunk(size: int) -> None:
            self.expanded += size
            state["since_check"] += size
            if state["since_check"] < _STREAM_CHECK_BYTES:
                return
            state["since_check"] = 0
            self._check_ratio(self.expanded, label)
            if self.free_bytes(where) < self.limits.reserve_bytes:
                raise ArchiveError(
                    "insufficient_disk_space",
                    f"Free space fell below the {self.limits.reserve_bytes}-byte reserve while "
                    f"{label} expanded.",
                )

        return on_chunk

    def tool(self) -> SevenZipTool:
        if self.sevenzip is None:
            self.sevenzip = find_sevenzip(self.sevenzip_setting)
        return self.sevenzip

    def list(self, archive: Path, detection: ArchiveDetection) -> ArchiveListing:
        listing = list_archive(
            archive,
            detection=detection,
            sevenzip=self.sevenzip,
            sevenzip_setting=self.sevenzip_setting,
            reader=self.reader,
            limits=self.limits,
        )
        # A zip fallback finds 7-Zip inside list_archive; keep it for the rest of the lineage.
        if listing.sevenzip is not None:
            self.sevenzip = listing.sevenzip
        return listing

    def expand(
        self,
        archive: Path,
        listing: ArchiveListing,
        work: Path,
        *,
        depth: int,
        label: str,
        bases: list[Path],
    ) -> dict[str, Any]:
        """Validate, guard, extract into work (which must not exist) and verify one archive."""
        kind = listing.detection.kind
        members = listing.members
        rejected = validate_listing(
            members, bases, max_path_length=self.limits.max_path_length,
            max_directory_length=self.limits.max_directory_length,
            sizes_known=kind not in STREAM_KINDS,
        )
        if rejected:
            raise _refusal(rejected, label)
        declared = sum(max(0, member.size) for member in members if not member.is_dir)
        self.admit(len(members), declared, depth, work, label)
        work.mkdir(parents=True)
        started = _now()
        exit_code = None
        argv: list[str] = []
        timeout = None
        if listing.reader == "7-Zip":
            argv, exit_code, timeout = self._extract_sevenzip(listing, archive, work, declared, label)
            integrity = "member_crc32"
            crc_verified = all(
                member.is_dir or member.size == 0 or bool(member.crc32 or member.checksum)
                for member in members
            )
        elif kind == "zip":
            with zipfile.ZipFile(archive) as handle:
                infos = handle.infolist()
                if [info.filename for info in infos] != [member.name for member in members]:
                    raise ArchiveError("listing_changed", f"{label} changed after it was listed.")
                for info, member in zip(infos, members):
                    target = _member_target(work, member.path)
                    if member.is_dir:
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with handle.open(info) as source:
                        _copy_capped(source, target, member.size)
            integrity, crc_verified = "member_crc32", True
        elif kind in TAR_KINDS:
            with tarfile.open(archive, _tar_mode(kind)) as handle:
                index = 0
                for info in handle:
                    if index >= len(members) or info.name != members[index].name:
                        raise ArchiveError("listing_changed", f"{label} changed after it was listed.")
                    member = members[index]
                    index += 1
                    target = _member_target(work, member.path)
                    if member.is_dir:
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = handle.extractfile(info)
                    if source is None:
                        raise ArchiveError("listing_changed", f"{label}: {member.name} has no data.")
                    with source:
                        _copy_capped(source, target, member.size)
                if index != len(members):
                    raise ArchiveError("listing_changed", f"{label} changed after it was listed.")
                if kind != "tar":
                    _drain(handle.fileobj)
            if kind == "tar":
                integrity, crc_verified = "none", False
            elif kind == "tar.xz":
                crc_verified = _xz_has_check(archive)
                integrity = "stream_check" if crc_verified else "none"
            else:
                integrity, crc_verified = "stream_crc32", True
        else:
            member = members[0]
            target = _member_target(work, member.path)
            with _open_stream(archive, kind) as source:
                member.size = _copy_capped(source, target, -1, self.stream_guard(work, label))
            if kind == "xz":
                crc_verified = _xz_has_check(archive)
                integrity = "stream_check" if crc_verified else "none"
            elif kind == "lzma":
                # LZMA-alone carries no checksum at all; only the archive's own hash vouches for it.
                integrity, crc_verified = "none", False
            else:
                integrity, crc_verified = "stream_crc32", True
        # Metadata was validated and written like any member; it goes before the tree is compared,
        # so the comparison also shows that it went.
        dropped = [] if kind in STREAM_KINDS else [
            member for member in members if _metadata_entry(member.path, member.is_dir)
        ]
        dropped_entries = sorted({_metadata_entry(member.path, member.is_dir) for member in dropped})
        for entry in dropped_entries:
            target = _member_target(work, entry)
            if os.path.isdir(target) and not os.path.islink(target):
                _remove_tree(target)
            elif os.path.lexists(target):
                os.chmod(target, stat.S_IWRITE)
                target.unlink()
        dropped_ids = {id(member) for member in dropped}
        listed = [member for member in members if member.path and id(member) not in dropped_ids]
        files = [member for member in listed if not member.is_dir]
        tree = _verify_tree(
            work,
            {member.path.casefold(): member.size for member in files},
            [member.path for member in listed if member.is_dir]
            + [entry.rsplit("/", 1)[0] for entry in dropped_entries if "/" in entry],
            label,
        )
        return {
            **listing.detection.record(),
            "reader": listing.reader,
            "tool": listing.tool,
            "fallback_reason": listing.fallback_reason,
            "reported_type": listing.reported_type,
            "listing_argv": listing.argv,
            "argv": argv,
            "timeout_seconds": timeout,
            "exit_code": exit_code,
            "started_at": started,
            "finished_at": _now(),
            "member_count": len(listed),
            "file_count": len(files),
            "directory_count": len(listed) - len(files),
            "expanded_bytes": sum(member.size for member in files),
            "tree": tree,
            "rejected_members": [],
            # Counted apart from the members above; the listing TSV has a row for each of them.
            "dropped_metadata": {
                "members": len(dropped),
                "bytes": sum(max(0, member.size) for member in dropped if not member.is_dir),
                "entries": dropped_entries[:50],
            },
            "integrity": integrity,
            "crc_verified": crc_verified,
            "depth": depth,
        }

    def _extract_sevenzip(
        self, listing: ArchiveListing, archive: Path, work: Path, declared: int, label: str
    ) -> tuple[list[str], int, float]:
        tool = listing.sevenzip or self.tool()
        signature = listing.detection.signature
        argv = [
            tool.executable, "x", "-y", "-bd", "-bb0", "-sccUTF-8", f"-p{SENTINEL_PASSWORD}",
            f"-t{_SEVENZIP_TYPE_SWITCH[signature]}", f"-o{work}", "--", str(archive),
        ]
        limits = self.limits
        cap = int(declared * 1.01) + _CHUNK
        timeout = limits.timeout_base_seconds + limits.timeout_seconds_per_gb * math.ceil(declared / GB)

        def watch() -> str:
            if self.free_bytes(work) < limits.reserve_bytes:
                return "insufficient_disk_space"
            if _tree_bytes(work) > cap:
                return "expansion_exceeds_listing"
            return ""

        run = _run_tool(argv, timeout=timeout, watch=watch, interval=limits.watchdog_interval_seconds)
        if run.killed_reason:
            because = f" ({run.killed_detail})" if run.killed_detail else ""
            raise ArchiveError(
                run.killed_reason,
                f"7-Zip was stopped while extracting {label}: {run.killed_reason}{because}.",
                detail={"elapsed_seconds": run.elapsed_seconds, "watchdog_error": run.killed_detail},
            )
        if run.exit_code != 0:
            raise _sevenzip_error(run, label, "extraction")
        return argv, run.exit_code, timeout

    def add_rows(
        self,
        members: list[ArchiveMember],
        relative: str,
        depth: int,
        archive_label: str,
        record_key: str,
        kind: str,
    ) -> None:
        for member in members:
            if not member.path:
                continue  # the root entry of a tar made from '.'
            path = f"{relative}/{member.path}" if relative else member.path
            entry = "" if kind in STREAM_KINDS else _metadata_entry(member.path, member.is_dir)
            if entry and entry == member.path and "/" in path:
                # The folder that held a dropped .DS_Store was a folder in the archive; it stays.
                self.directories.append(path.rsplit("/", 1)[0])
            self.rows.append(
                {
                    "path": path,
                    "type": "dir" if member.is_dir else "file",
                    "size": 0 if member.is_dir else member.size,
                    "crc32": member.crc32,
                    "modified": member.modified,
                    "depth": depth,
                    "archive": archive_label,
                    "member_name": _encodable(member.name),
                    "disposition": "dropped_metadata" if entry else "extracted",
                    # Which record a nested archive among these members is filed under. Not
                    # written to the TSV; the outermost archive's key cannot equal a member path.
                    "record_key": record_key,
                }
            )


_OUTERMOST = "\0outermost"


def _entry_exists(directory: Path, name: str) -> bool:
    key = name.casefold()
    try:
        return any(entry.casefold() == key for entry in os.listdir(directory))
    except OSError:
        return False


def _inside_vendor_container(path: str) -> bool:
    return any(part.casefold().endswith(FOLDER_CONTAINER_SUFFIXES) for part in path.split("/")[:-1])


def _expand_nested(context: _Extraction, staging: Path, records: dict[str, dict[str, Any]]) -> None:
    """Expand archives found among the extracted files, breadth first, inside the staging tree."""
    index = 0
    while index < len(context.rows):
        row = context.rows[index]
        index += 1
        if row["type"] != "file" or row["disposition"] != "extracted":
            continue
        if not is_archive_name(row["path"]):
            continue
        parent_record = records[row["record_key"]]
        if _inside_vendor_container(row["path"]):
            # A file inside a .d or .raw folder belongs to the vendor's layout; unpacking it would
            # change the container the reader expects.
            parent_record["nested_skipped"].append(
                {"path": row["path"], "reason": "inside_vendor_container"}
            )
            continue
        archive = _member_target(staging, row["path"])
        try:
            detection = detect_archive(archive)
        except ArchiveError as error:
            parent_record["nested_skipped"].append({"path": row["path"], "reason": error.reason})
            continue
        depth = int(row["depth"]) + 1
        label = row["path"]
        listing = context.list(archive, detection)
        rule, prefix, container = destination_rule(
            archive.name, listing.members, kind=detection.kind, nested=True
        )
        parent = archive.parent
        # What it would add beside itself. A study that ships run.mzML and run.mzML.gz, or S1/ and
        # S1.zip, keeps the packed copy as it came rather than losing the whole lineage.
        entries = [prefix] if prefix else sorted(
            {path.split("/", 1)[0] for _member, path in _kept_paths(listing.members, detection.kind)}
        )
        existing = next((entry for entry in entries if _entry_exists(parent, entry)), "")
        if existing:
            parent_record["nested_skipped"].append(
                {"path": row["path"], "reason": "destination_exists", "existing": existing}
            )
            continue
        context.counter += 1
        work = parent / f"~{context.counter}.partial"
        final = parent / prefix if prefix else parent
        # All three, because the archive is gone once it has expanded, and a repository may have
        # published any of them for it: MB-POST lists an MD5 for each per-sample zip in its tar.
        digests = file_digests(archive)
        record = context.expand(archive, listing, work, depth=depth, label=label, bases=[work, final])
        entries = [prefix] if prefix else os.listdir(work)
        for entry in entries:
            # The check above saw the same names; the tree was just verified against them.
            if _entry_exists(parent, entry):
                raise ArchiveError(
                    "nested_destination_exists",
                    f"{label} would expand into {entry}, which already exists beside it.",
                )
        if prefix:
            os.replace(work, final)
        else:
            for entry in entries:
                os.replace(work / entry, parent / entry)
            work.rmdir()
        os.chmod(archive, stat.S_IWRITE)
        archive.unlink()
        row["disposition"] = "expanded_archive"
        parent_relative = row["path"].rsplit("/", 1)[0] if "/" in row["path"] else ""
        relative = "/".join(part for part in (parent_relative, prefix) if part)
        # Neither folder need be implied by a member: an empty archive, or one alone in its folder.
        context.directories.extend(part for part in (parent_relative, relative) if part)
        context.add_rows(listing.members, relative, depth, label, label, detection.kind)
        record.update(
            {
                "archive_name": archive.name,
                # Both relative to the outermost destination.
                "archive_path": label,
                "destination_relative": relative,
                "archive_bytes": row["size"],
                "archive_sha256": digests["sha256"],
                "archive_md5": digests["md5"],
                "archive_sha1": digests["sha1"],
                "destination_rule": rule,
                "container_stem": prefix if rule == "container_stem" else "",
                **_container_record(archive.name, container, parent_relative),
                "nested": [],
                "nested_skipped": [],
            }
        )
        parent_record["nested"].append(record)
        records[label] = record


def _write_members_tsv(rows: list[dict[str, Any]], path: Path) -> str:
    lines = ["\t".join(_TSV_COLUMNS)]
    for row in rows:
        lines.append("\t".join(str(row[column]) for column in _TSV_COLUMNS))
    data = ("\n".join(lines) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)
    return hashlib.sha256(data).hexdigest()


def extract_archive(
    archive: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    archive_sha256: str = "",
    listing_directory: str | os.PathLike[str] | None = None,
    limits: ExtractionLimits | None = None,
    free_bytes: Callable[[Path], int] | None = None,
    sevenzip: SevenZipTool | None = None,
    sevenzip_setting: str | None = None,
    reader: str = "auto",
    nested: bool = True,
) -> dict[str, Any]:
    """Extract an archive into destination, which must not exist, and return its provenance record.

    The tree is built in '<destination>.partial' and renamed only after it matches the listing.
    archive_sha256 is the hash the download already computed; without it the archive is hashed
    here. With listing_directory, the member listing of the whole lineage is written there as
    archive-members-<sha12>.tsv and its sha256 recorded. Raises ArchiveError, leaving neither the
    destination nor the staging directory behind, whatever went wrong: an unexpected exception is
    wrapped as extraction_failed, and one that is not an Exception (an interrupt) still clears the
    staging directory on its way out.
    """
    archive = Path(archive).absolute()
    destination = Path(destination).absolute()
    limits = limits or ExtractionLimits()
    if not archive.is_file():
        raise ArchiveError("archive_missing", f"The archive {archive} does not exist.")
    if os.path.lexists(destination):
        raise ArchiveError(
            "destination_exists", f"The extraction destination {destination} already exists."
        )
    staging = destination.with_name(destination.name + ".partial")
    started = _now()
    finished = False
    try:
        removed_stale_staging = os.path.lexists(staging)
        if removed_stale_staging:
            _remove_tree(staging)
        destination.parent.mkdir(parents=True, exist_ok=True)
        context = _Extraction(
            limits, free_bytes or _free_bytes, sevenzip, sevenzip_setting, reader,
            archive.stat().st_size,
        )
        detection = detect_archive(archive)
        if detection is None:
            raise ArchiveError(
                "not_an_archive_name", f"{archive.name} does not have an archive suffix."
            )
        sha256 = archive_sha256 or file_sha256(archive)
        listing = context.list(archive, detection)
        rule, prefix, container = destination_rule(
            archive.name, listing.members, kind=detection.kind
        )
        work = staging / prefix if prefix else staging
        record = context.expand(archive, listing, work, depth=1, label=archive.name, bases=[work])
        context.add_rows(listing.members, prefix, 1, archive.name, _OUTERMOST, detection.kind)
        record.update(
            {
                "schema": EXTRACTION_SCHEMA,
                "archive_name": archive.name,
                "archive_path": str(archive),
                "archive_bytes": context.outer_bytes,
                "archive_sha256": sha256,
                "archive_sha256_source": "caller" if archive_sha256 else "computed",
                "compressed_bytes": context.outer_bytes,
                "destination": str(destination),
                "destination_rule": rule,
                "container_stem": prefix,
                **_container_record(archive.name, container, ""),
                "nested": [],
                "nested_skipped": [],
                "limits": limits.record(),
                "removed_stale_staging": removed_stale_staging,
                "started_at": started,
            }
        )
        if nested:
            _expand_nested(context, staging, {_OUTERMOST: record})
        extracted = [row for row in context.rows if row["disposition"] == "extracted"]
        expected = {row["path"].casefold(): row["size"] for row in extracted if row["type"] == "file"}
        directories = [row["path"] for row in extracted if row["type"] == "dir"]
        directories += context.directories + ([prefix] if prefix else [])
        record["tree"] = _verify_tree(staging, expected, directories, archive.name)
        record["lineage"] = {
            "archives": 1 + sum(1 for row in context.rows if row["disposition"] == "expanded_archive"),
            "members": context.members,
            "expanded_bytes": context.expanded,
        }
        record["members_tsv"] = None
        if listing_directory is not None:
            tsv = Path(listing_directory) / f"archive-members-{sha256[:12]}.tsv"
            record["members_tsv"] = {
                "path": str(tsv.absolute()),
                "sha256": _write_members_tsv(context.rows, tsv),
                "rows": len(context.rows),
            }
        os.replace(staging, destination)
        finished = True
    except ArchiveError:
        raise
    except (zipfile.BadZipFile, EOFError, lzma.LZMAError, zlib.error, tarfile.TarError) as error:
        raise ArchiveError(
            "corrupt_archive", f"{archive.name} could not be read: {error}"
        ) from error
    except OSError as error:
        reason = "corrupt_archive" if isinstance(error, gzip.BadGzipFile) else "extraction_failed"
        raise ArchiveError(reason, f"{archive.name} could not be extracted: {error}") from error
    except Exception as error:  # noqa: BLE001 - a defect here must not leave a tree to pay for
        raise ArchiveError(
            "extraction_failed",
            f"{archive.name} could not be extracted: {type(error).__name__}: {error}",
            detail={"exception": type(error).__name__},
        ) from error
    finally:
        if not finished:
            _remove_tree(staging)
    record["finished_at"] = _now()
    return record
