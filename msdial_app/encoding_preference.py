"""Which encoding of a sample MS-DIAL analyses: the Catalog's rule, for files only an archive showed.

ONE RULE, TWO PLACES. A repository may publish one injection more than once - a vendor .raw beside an
.mzXML of it (MetaboBank MTBKS157), an mzML archive beside an mzXML archive of the same samples
(Metabolomics Workbench ST003038) - and exactly one may be analysed, or the sample is measured twice and
aligned against itself. The Catalog decides which wherever a repository lists its files
(msdial_repository_catalog.class_proposal._prefer_one_container_per_sample). It cannot see inside a study
archive, so for those files the lease decides after extraction, and it must decide as the Catalog would.
The two are held together by one set of test vectors, tests/vectors/encoding_preference.v1.json, copied
from the Catalog's tests/vectors and decided here case by case.

THE RULE, as the vectors state it:

- files name the same sample when their basenames match with the container suffix removed; a per-sample
  archive (x.d.zip, x.mzML.gz, x.mzXML.lzma) is read as the file it unpacks to, and a vendor folder
  listed through its members as the folder;
- a container listed both unpacked and as its own archive is analysed unpacked;
- a vendor container is analysed and every other encoding of its sample is the alternate;
- with none, an mzML (or imzML) is analysed and a format MS-DIAL cannot read is the alternate;
- with neither, an mzXML, which is converted to mzML, is analysed, and a format nothing converts (mzData,
  mgf, ibd, dat, scan) is the alternate;
- two containers of equal preference are both left: no preference was stated between them;
- a .wiff2 always wins over the .wiff of the same sample;
- what travels with a SCIEX acquisition (.wiff.scan, .wiff2.scan, .timeseries.data) never competes, and
  files inside a vendor folder are its members.

Every format MS-DIAL cannot read requires conversion; only mzXML is converted, to mzML.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import PurePosixPath
from typing import Iterable

from . import archives

# What MS-DIAL opens, from SupportMsRawDataExtension in MsdialCore/Enum/SupportFormat.cs, split as the
# Catalog splits it: what the instrument wrote, and an open re-encoding of it.
VENDOR_SUFFIXES = (".raw", ".d", ".wiff", ".wiff2", ".lcd", ".qgd", ".abf", ".ibf", ".cdf", ".lrp")
OPEN_SUFFIXES = (".mzml", ".imzml")
# What MS-DIAL has no reader for. Only mzXML is converted (msdial_app.mzxml_conversion).
UNREADABLE_SUFFIXES = (".mzxml", ".mzdata", ".mgf", ".ibd", ".dat", ".scan")
CONVERTIBLE_SUFFIXES = (".mzxml",)
FOLDER_SUFFIXES = (".raw", ".d")
SIDECAR_SUFFIXES = (".wiff2.scan", ".wiff.scan")
AUXILIARY_SUFFIXES = (".timeseries.data",)

RAW = "raw"
ALTERNATE = "raw_alternate"
MEMBER = "vendor_folder_member"
SIDECAR = "sidecar"
AUXILIARY = "auxiliary"

_EXTENSIONS = {suffix[1:]: suffix for suffix in VENDOR_SUFFIXES + OPEN_SUFFIXES + UNREADABLE_SUFFIXES}


def _slashes(value: object) -> str:
    return str(value or "").replace("\\", "/").strip().strip("/")


def container_suffix(path: str) -> str:
    """The container suffix a name ends in, read from its last extension ('' for none)."""
    _, dot, extension = _slashes(path).casefold().rpartition(".")
    return _EXTENSIONS.get(extension, "") if dot else ""


def unpacked(path: str) -> str:
    """The file a packed name stands for: 'x.d.zip' -> 'x.d', 'x.wiff.scan.zip' -> 'x.wiff.scan'; else ''."""
    value = _slashes(path)
    name = PurePosixPath(value).name
    if not archives.is_archive_name(name):
        return ""
    inner = value[: len(value) - len(name)] + archives.archive_stem(name)
    name = PurePosixPath(inner).name.casefold()
    known = next((suffix for suffix in SIDECAR_SUFFIXES + AUXILIARY_SUFFIXES if name.endswith(suffix)), "")
    known = known or container_suffix(name)
    return inner if known and len(name) > len(known) else ""


def folder_of(path: str) -> str:
    """The vendor folder a path lies inside (its outermost .raw or .d segment but the last), or ''."""
    segments = _slashes(path).split("/")
    for index, segment in enumerate(segments[:-1]):
        lower = segment.casefold()
        if lower.endswith(FOLDER_SUFFIXES) and lower not in FOLDER_SUFFIXES:
            return "/".join(segments[: index + 1])
    return ""


def kind(path: str) -> str:
    """vendor, open, convertible or unreadable for a container, packed or not; '' for anything else."""
    value = (unpacked(path) or _slashes(path)).casefold()
    if value.endswith(CONVERTIBLE_SUFFIXES):
        return "convertible"
    if value.endswith(UNREADABLE_SUFFIXES):
        return "unreadable"
    if value.endswith(OPEN_SUFFIXES):
        return "open"
    if value.endswith(VENDOR_SUFFIXES):
        return "vendor"
    return ""


def stem(path: str) -> str:
    """The sample a container belongs to: its basename, unpacked, less the container suffix."""
    name = PurePosixPath(unpacked(path) or _slashes(path)).name.casefold()
    suffix = container_suffix(name)
    return name[: -len(suffix)] if suffix and len(name) > len(suffix) else name


def requires_conversion(path: str) -> bool:
    """Whether MS-DIAL has no reader for the file (packed or not); members and SCIEX companions never do."""
    if folder_of(path) or travels_with_an_acquisition(path):
        return False
    return kind(path) in {"convertible", "unreadable"}


def is_convertible(path: str) -> bool:
    """Whether a file, packed or not, is an mzXML, which is converted to mzML."""
    return not folder_of(path) and kind(path) == "convertible"


def travels_with_an_acquisition(path: str) -> str:
    """sidecar or auxiliary for what travels with a SCIEX file, packed or not; '' for anything else."""
    value = (unpacked(path) or _slashes(path)).casefold()
    if value.endswith(AUXILIARY_SUFFIXES):
        return AUXILIARY
    if value.endswith(SIDECAR_SUFFIXES):
        return SIDECAR
    return ""


def prefer_encodings(paths: Iterable[str]) -> dict[str, str]:
    """The role each of one unit's paths ends with: raw, raw_alternate, vendor_folder_member, sidecar or auxiliary.

    ``paths`` are files, as a listing names them, or files and vendor folders as they lie on disk; a file
    inside a vendor folder is its member and the folder competes for it. A path whose name is no container
    and travels with none (a README) keeps raw: the rule says nothing about it.
    """
    listed = [str(path) for path in paths]
    roles: dict[str, str] = {}
    competitors: list[str] = []
    folders: dict[str, str] = {}
    for path in listed:
        folder = folder_of(path)
        if folder:
            roles[path] = MEMBER
            folders.setdefault(folder.casefold(), folder)
            continue
        travelling = travels_with_an_acquisition(path)
        if travelling:
            roles[path] = travelling
            continue
        roles[path] = RAW
        if kind(path):
            competitors.append(path)
    known = {(unpacked(path) or _slashes(path)).casefold() for path in competitors}
    known.update(folders)
    # A .wiff2 wins over the .wiff of the same sample, whatever else competes.
    for path in competitors:
        value = (unpacked(path) or _slashes(path)).casefold()
        if value.endswith(".wiff") and value[: -len(".wiff")] + ".wiff2" in known:
            roles[path] = ALTERNATE
    groups: dict[str, list[str]] = defaultdict(list)
    for folder in folders.values():
        groups[stem(folder)].append(folder)
    for path in competitors:
        if roles[path] == RAW:
            groups[stem(path)].append(path)
    for group in groups.values():
        unpacked_paths = {_slashes(path).casefold() for path in group if not unpacked(path)}
        for path in group:
            inner = unpacked(path)
            if inner and _slashes(inner).casefold() in unpacked_paths:
                roles[path] = ALTERNATE
        competing = [path for path in group if roles.get(path, RAW) == RAW]
        kinds = {path: ("vendor" if path in folders.values() else kind(path)) for path in competing}
        vendor = [path for path in competing if kinds[path] == "vendor"]
        readable = [path for path in competing if kinds[path] in {"vendor", "open"}]
        convertible = [path for path in competing if kinds[path] == "convertible"]
        preferred = vendor or readable or convertible
        if preferred and len(preferred) < len(competing):
            for path in competing:
                if path not in preferred:
                    roles[path] = ALTERNATE
    return {path: roles[path] for path in listed}
