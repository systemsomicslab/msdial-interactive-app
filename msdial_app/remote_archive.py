"""List a remote zip archive's members by reading only its central directory, over HTTP Range requests.

WHY. A public study archive (Metabolomics Workbench's ST002798_rawdata.zip is 8.5 GB) can hold several
analysis units' files side by side. Deciding which members belong to which unit, and spotting byte-identical
members filed under two units, needs the member list before anything is downloaded. A zip keeps that list
at its end, in the central directory: a tail of a few kilobytes says where it starts, and the directory itself
is usually tens of kilobytes. Read with Range requests, the listing costs that much and nothing more.

What it reads, in order: a suffix range of the archive's last bytes (the end of central directory record,
EOCD, and the ZIP64 locator when there is one), more of the tail when an archive comment pushes the EOCD
further back, the ZIP64 end record when the EOCD defers to it, and the central directory. Every request after
the first carries If-Range with the first answer's validator, and every answer must name the same total size,
so a listing is never stitched together from two versions of the archive.

What it refuses, without reading a body it did not ask for: a server that answers a Range request with the
whole file (200), a sign-in or landing page served in its place, any other status, a byte count beyond the
cap, a split (multi-disk) archive, a directory whose entries do not parse to the count the EOCD declares, a
listing that runs past its overall deadline (a watchdog shuts the connection, so a server trickling headers or
body bytes cannot hold it), and any scheme other than http and https, given directly or reached through a
redirect. A redirect's own body is never read: it is closed unread and the hop is recorded. Nothing is written
anywhere; the listing is returned.
"""

from __future__ import annotations

import hashlib
import http.client
import math
import re
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from . import __version__

SCHEMA = "msdial-remote-archive-listing.v1"
USER_AGENT = f"MS-DIAL-Interactive/{__version__} remote-archive-listing"

DEFAULT_MAX_BYTES = 512 * 1024
HARD_MAX_BYTES = 16 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_DEADLINE_SECONDS = 120.0
# The first suffix range. It holds the EOCD of an archive without a comment, the ZIP64 records, and often the
# whole central directory of a small archive.
INITIAL_TAIL_BYTES = 8 * 1024
# The EOCD is 22 bytes and may be followed by a comment of up to 65,535 bytes; the ZIP64 locator (20 bytes)
# sits right before it.
EOCD_SEARCH_BYTES = 22 + 0xFFFF + 20

_EOCD = b"PK\x05\x06"
_ZIP64_LOCATOR = b"PK\x06\x07"
_ZIP64_EOCD = b"PK\x06\x06"
_CENTRAL = b"PK\x01\x02"

COMPRESSION_NAMES = {
    0: "stored",
    1: "shrunk",
    6: "imploded",
    8: "deflate",
    9: "deflate64",
    12: "bzip2",
    14: "lzma",
    93: "zstd",
    95: "xz",
    98: "ppmd",
    99: "aes",
}


class RemoteArchiveError(RuntimeError):
    """A listing that was refused or failed, with a reason a caller can branch on and what was fetched so far."""

    def __init__(self, reason: str, detail: str, *, provenance: dict[str, Any] | None = None) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.provenance = dict(provenance or {})

    def as_result(self) -> dict[str, Any]:
        return {
            "ok": False,
            "schema": SCHEMA,
            "reason": self.reason,
            "detail": self.detail,
            "provenance": self.provenance,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dos_datetime(date: int, time_value: int) -> str:
    """The member's DOS timestamp as written (local time of the writer, no zone), or "" when it is not a date."""
    try:
        moment = datetime(
            1980 + (date >> 9),
            (date >> 5) & 0x0F,
            date & 0x1F,
            time_value >> 11,
            (time_value >> 5) & 0x3F,
            min((time_value & 0x1F) * 2, 59),
        )
    except ValueError:
        return ""
    return moment.strftime("%Y-%m-%d %H:%M:%S")


_CONTENT_RANGE = re.compile(r"^\s*bytes\s+(\d+)-(\d+)/(\d+|\*)\s*$", re.IGNORECASE)
# Bodies are read in pieces of this size, and the deadline is checked after each.
READ_CHUNK_BYTES = 64 * 1024
# A Content-Type naming a page or a document, not an archive: what a sign-in, landing or error page is served as.
_PAGE_CONTENT_TYPE = re.compile(r"^\s*(text/|application/(xhtml\+xml|json|xml|problem\+json)\b)", re.IGNORECASE)


class _Watchdog:
    """Shuts the sockets of the request in flight when the overall deadline passes.

    urlopen's timeout bounds each socket operation, not the request: a server that sends a header line or a few
    body bytes inside every timeout would hold a listing for as long as it liked. Shutting the socket ends the
    blocked read at once (the read sees end of stream), and the caller then refuses with reason timeout.
    """

    def __init__(self, seconds: float) -> None:
        self.fired = False
        self._sockets: list[Any] = []
        self._lock = threading.Lock()
        self._timer = threading.Timer(max(0.0, seconds), self._fire)
        self._timer.daemon = True

    def watch(self, sock: Any) -> None:
        if sock is None:
            return
        with self._lock:
            self._sockets.append(sock)
            fired = self.fired
        if fired:
            self._shut(sock)

    @staticmethod
    def _shut(sock: Any) -> None:
        try:
            # The plain socket's shutdown, also for a TLS socket: the TLS layer then reads end of stream.
            socket.socket.shutdown(sock, socket.SHUT_RDWR)
        except (OSError, ValueError, TypeError):
            pass

    def _fire(self) -> None:
        with self._lock:
            self.fired = True
            sockets = list(self._sockets)
        for sock in sockets:
            self._shut(sock)

    def __enter__(self) -> "_Watchdog":
        self._timer.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._timer.cancel()


def _watched_connection(connection_class: Any, reader: "_RangeReader") -> Any:
    """A connection factory for urllib's do_open that hands each connected socket to the reader's watchdog."""

    def factory(host: str, **kwargs: Any) -> Any:
        connection = connection_class(host, **kwargs)
        connect = connection.connect

        def connect_and_watch() -> None:
            connect()
            reader._watch(connection.sock)

        connection.connect = connect_and_watch
        return connection

    return factory


_SCHEMES = ("http", "https")


class _RedirectRefused(Exception):
    """A redirect to a scheme other than http or https, raised out of the opener so no handler follows it."""

    def __init__(self, code: int, target: str) -> None:
        super().__init__(target)
        self.code = code
        self.target = target


class _RedirectHandler(urllib.request.HTTPRedirectHandler):
    """urllib's redirect handling, except that a redirect's body is closed unread and only http(s) is followed.

    urllib's own handler reads the whole body of a 3xx answer before it follows Location, and those bytes would
    pass outside the cap and the provenance. Here the body is closed before urllib reaches it (a closed response
    reads as empty), and each hop is recorded with its status, target and declared Content-Length.
    """

    def __init__(self, reader: "_RangeReader") -> None:
        super().__init__()
        self._reader = reader

    def http_error_302(self, req, fp, code, msg, headers):  # urllib's signature
        location = headers.get("location") or headers.get("uri") or ""
        target = urllib.parse.urljoin(req.full_url, location.strip()) if location else ""
        self._reader._record_redirect(code, target, headers.get("Content-Length", "") or "")
        fp.close()
        if target and urllib.parse.urlsplit(target).scheme.lower() not in _SCHEMES:
            raise _RedirectRefused(code, target)
        return super().http_error_302(req, fp, code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def _opener(reader: "_RangeReader") -> urllib.request.OpenerDirector:
    """An opener for http and https only. There is no ftp, file or data handler, so nothing else is opened, even
    through a redirect, and every socket it makes is one the watchdog sees."""

    class _HTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):  # urllib's signature
            return self.do_open(_watched_connection(http.client.HTTPConnection, reader), req)

    handlers: list[Any] = [_HTTPHandler()]
    if hasattr(http.client, "HTTPSConnection"):

        class _HTTPSHandler(urllib.request.HTTPSHandler):
            def https_open(self, req):  # urllib's signature
                options: dict[str, Any] = {"context": self._context}
                if hasattr(self, "_check_hostname"):  # Python 3.10 and 3.11
                    options["check_hostname"] = self._check_hostname
                return self.do_open(_watched_connection(http.client.HTTPSConnection, reader), req, **options)

        handlers.append(_HTTPSHandler())
    opener = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.ProxyHandler(),
        urllib.request.UnknownHandler(),
        *handlers,
        urllib.request.HTTPDefaultErrorHandler(),
        _RedirectHandler(reader),
        urllib.request.HTTPErrorProcessor(),
    ):
        opener.add_handler(handler)
    return opener


class _RangeReader:
    """Range requests against one URL, holding the bytes [start, total) of the archive read so far."""

    def __init__(
        self,
        url: str,
        *,
        max_bytes: int,
        timeout: float,
        deadline_seconds: float,
        user_agent: str,
    ) -> None:
        self.url = url
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.deadline = time.monotonic() + deadline_seconds
        self.user_agent = user_agent
        self.total: int | None = None
        self.start: int | None = None
        self.buffer = b""
        self.bytes_fetched = 0
        self.requests: list[dict[str, Any]] = []
        self.etag = ""
        self.last_modified = ""
        self.final_url = url
        self.accept_ranges = ""
        self._watchdog: _Watchdog | None = None
        self._record: dict[str, Any] | None = None
        self._open = _opener(self).open

    def provenance(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "final_url": self.final_url,
            "bytes_fetched": self.bytes_fetched,
            "max_bytes": self.max_bytes,
            "requests": list(self.requests),
            "etag": self.etag,
            "last_modified": self.last_modified,
            "accept_ranges": self.accept_ranges,
            "size_bytes": self.total,
            "user_agent": self.user_agent,
            "tool": {"name": "msdial_app.remote_archive", "version": __version__},
        }

    def _refuse(self, reason: str, detail: str) -> RemoteArchiveError:
        return RemoteArchiveError(reason, detail, provenance=self.provenance())

    def _record_redirect(self, status: int, target: str, content_length: str) -> None:
        # The redirect's body is closed unread, so it adds nothing to the bytes fetched; its declared length is kept.
        if self._record is not None:
            self._record.setdefault("redirects", []).append(
                {"status": status, "location": target, "content_length": content_length, "bytes": 0}
            )

    def _watch(self, sock: Any) -> None:
        if self._watchdog is not None:
            self._watchdog.watch(sock)

    def _past_deadline(self, range_value: str, watchdog: _Watchdog) -> RemoteArchiveError | None:
        if watchdog.fired or time.monotonic() >= self.deadline:
            return self._refuse(
                "timeout",
                f"The listing ran past its overall deadline while {range_value} was being answered; the "
                "connection was shut and nothing more was read.",
            )
        return None

    def _validator(self) -> str:
        # If-Range takes a strong entity tag or an HTTP date; a weak tag cannot be used for it.
        if self.etag and not self.etag.startswith("W/"):
            return self.etag
        return self.last_modified

    def fetch(self, range_value: str, expected: int | None) -> tuple[int, int, bytes]:
        """One Range request. Returns (first, last, body) of a 206 answer and refuses anything else unread."""
        if self.bytes_fetched + (expected or 0) > self.max_bytes:
            raise self._refuse(
                "byte_cap_exceeded",
                f"Reading {range_value} would bring the bytes fetched to {self.bytes_fetched + (expected or 0):,}, "
                f"past the cap of {self.max_bytes:,}. Nothing more was requested; raise max_bytes (at most "
                f"{HARD_MAX_BYTES:,}) to list this archive.",
            )
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise self._refuse("timeout", "The listing ran past its overall deadline.")
        headers = {"User-Agent": self.user_agent, "Range": range_value, "Accept-Encoding": "identity"}
        validator = self._validator()
        if self.requests and validator:
            headers["If-Range"] = validator
        request = urllib.request.Request(self.url, headers=headers, method="GET")
        record: dict[str, Any] = {"range": range_value, "status": None, "bytes": 0}
        self.requests.append(record)
        with _Watchdog(remaining) as watchdog:
            self._watchdog, self._record = watchdog, record
            try:
                return self._exchange(request, headers, range_value, expected, record, watchdog)
            finally:
                self._watchdog = self._record = None

    def _exchange(
        self,
        request: urllib.request.Request,
        headers: dict[str, str],
        range_value: str,
        expected: int | None,
        record: dict[str, Any],
        watchdog: _Watchdog,
    ) -> tuple[int, int, bytes]:
        try:
            response = self._open(request, timeout=min(self.timeout, max(0.001, self.deadline - time.monotonic())))
        except _RedirectRefused as refused:
            raise self._refuse(
                "unsupported_redirect",
                f"The server answered {range_value} with {refused.code}, a redirect to {refused.target!r}. Only http "
                "and https are followed; nothing was requested from it and the redirect's body was not read.",
            )
        except urllib.error.HTTPError as error:
            record["status"] = error.code
            error.close()
            if error.code == 416:
                raise self._refuse("range_not_satisfiable", f"The server refused the range {range_value} (416).")
            raise self._refuse("http_error", f"The server answered {error.code} {error.reason} for {range_value}.")
        except (socket.timeout, TimeoutError) as error:
            raise self._past_deadline(range_value, watchdog) or self._refuse(
                "timeout", f"No answer within {self.timeout:g} s for {range_value}: {error}."
            )
        except urllib.error.URLError as error:
            late = self._past_deadline(range_value, watchdog)
            if late is not None:
                raise late
            if isinstance(error.reason, (socket.timeout, TimeoutError)):
                raise self._refuse("timeout", f"No answer within {self.timeout:g} s for {range_value}.")
            raise self._refuse("connection_error", f"Could not reach the server: {error.reason}.")
        except (http.client.HTTPException, OSError, ValueError) as error:
            # Raised while the status line and headers were read (urlopen wraps only the sending in URLError).
            raise self._past_deadline(range_value, watchdog) or self._refuse(
                "connection_error", f"The answer to {range_value} could not be read: {type(error).__name__}: {error}."
            )
        with response:
            status = response.status
            record["status"] = status
            self.final_url = response.geturl() or self.final_url
            content_type = response.headers.get("Content-Type", "") or ""
            if status != 206:
                # Whatever came instead of a range, close it without reading a byte of the body.
                record["content_type"] = content_type
                redirected = self.final_url != self.url
                if _PAGE_CONTENT_TYPE.match(content_type):
                    raise self._refuse(
                        "not_an_archive_response",
                        f"The server answered {status} with {content_type.strip()!r} instead of the archive"
                        + (f", after a redirect to {self.final_url}" if redirected else "")
                        + ": a sign-in, landing or error page, not a zip. Access to the archive is the problem, "
                        "not range support. The body was not read.",
                    )
                if status == 200 and "If-Range" in headers:
                    raise self._refuse(
                        "changed_during_listing",
                        "The server answered a conditional Range request with the whole file, so the archive "
                        "changed after the first request (If-Range did not match). Nothing more was read.",
                    )
                raise self._refuse(
                    "range_not_supported",
                    f"The server answered {status} instead of 206 Partial Content to {range_value}"
                    + (f" (after a redirect to {self.final_url})" if redirected else "")
                    + ": it does not serve byte ranges for this URL, so the central directory cannot be read "
                    "without downloading the archive. The body was not read.",
                )
            match = _CONTENT_RANGE.match(response.headers.get("Content-Range", ""))
            if not match or match.group(3) == "*":
                raise self._refuse(
                    "range_not_supported",
                    "A 206 answer without a usable Content-Range (bytes first-last/total) cannot be placed in the "
                    "archive. The body was not read.",
                )
            first, last, total = int(match.group(1)), int(match.group(2)), int(match.group(3))
            etag = response.headers.get("ETag", "") or ""
            last_modified = response.headers.get("Last-Modified", "") or ""
            if self.total is None:
                self.total = total
                self.etag = etag
                self.last_modified = last_modified
                self.accept_ranges = response.headers.get("Accept-Ranges", "") or ""
            elif (
                total != self.total
                or (self.etag and etag and etag != self.etag)
                # Without an ETag, Last-Modified is the only validator: a server that ignores If-Range could
                # otherwise swap in a same-size archive unseen.
                or (not self.etag and self.last_modified and last_modified and last_modified != self.last_modified)
            ):
                raise self._refuse(
                    "changed_during_listing",
                    f"The archive changed between requests (size {self.total:,} then {total:,}; ETag {self.etag!r} "
                    f"then {etag!r}; Last-Modified {self.last_modified!r} then {last_modified!r}). Nothing was "
                    "listed.",
                )
            length = last - first + 1
            if length <= 0 or (expected is not None and length > expected):
                raise self._refuse(
                    "unexpected_range",
                    f"Asked for {range_value} ({expected} bytes) and was offered bytes {first}-{last}. The body was "
                    "not read.",
                )
            if self.bytes_fetched + length > self.max_bytes:
                raise self._refuse(
                    "byte_cap_exceeded",
                    f"The server offered {length:,} bytes, which would pass the cap of {self.max_bytes:,}. The body "
                    "was not read.",
                )
            pieces: list[bytes] = []
            received = 0
            # read1 returns what one socket read brings, so a trickled body is counted as it arrives and the
            # deadline is checked between pieces. One byte past the promised length notices a server sending more.
            read_piece = getattr(response, "read1", None) or response.read
            while received < length + 1:
                try:
                    piece = read_piece(min(READ_CHUNK_BYTES, length + 1 - received))
                except (socket.timeout, TimeoutError):
                    raise self._past_deadline(range_value, watchdog) or self._refuse(
                        "timeout", f"The body of {range_value} stalled for more than {self.timeout:g} s."
                    )
                except (http.client.HTTPException, OSError, ValueError) as error:
                    raise self._past_deadline(range_value, watchdog) or self._refuse(
                        "connection_error",
                        f"The body of {range_value} broke off after {received:,} bytes: {type(error).__name__}.",
                    )
                if piece:
                    pieces.append(piece)
                    received += len(piece)
                    record["bytes"] += len(piece)
                    self.bytes_fetched += len(piece)
                late = self._past_deadline(range_value, watchdog)
                if late is not None:
                    raise late
                if not piece:
                    break
            body = b"".join(pieces)
        if len(body) != length:
            raise self._refuse(
                "unexpected_range",
                f"Content-Range promised {length:,} bytes and {len(body):,} arrived.",
            )
        return first, last, body

    def read_tail(self, size: int) -> None:
        first, last, body = self.fetch(f"bytes=-{size}", size)
        assert self.total is not None
        if last != self.total - 1:
            raise self._refuse("unexpected_range", f"A suffix range ended at {last}, not at the archive's end.")
        self.start, self.buffer = first, body

    def ensure(self, offset: int) -> None:
        """Extend the held tail backwards so it starts at or before ``offset``."""
        assert self.start is not None and self.total is not None
        offset = max(0, offset)
        if offset >= self.start:
            return
        wanted = self.start - offset
        first, last, body = self.fetch(f"bytes={offset}-{self.start - 1}", wanted)
        if first != offset or last != self.start - 1:
            raise self._refuse("unexpected_range", f"Asked for bytes {offset}-{self.start - 1}, got {first}-{last}.")
        self.buffer = body + self.buffer
        self.start = offset

    def bytes_at(self, offset: int, length: int) -> bytes:
        assert self.start is not None
        if offset < self.start:
            self.ensure(offset)
        relative = offset - self.start
        piece = self.buffer[relative : relative + length]
        if len(piece) != length:
            raise self._refuse("malformed_archive", f"Bytes {offset}-{offset + length - 1} lie outside the archive.")
        return piece


def _find_eocd(buffer: bytes) -> int:
    """The position of the last EOCD whose comment length reaches exactly the end of the buffer, or -1."""
    position = buffer.rfind(_EOCD)
    while position >= 0:
        if position + 22 <= len(buffer):
            comment_length = struct.unpack_from("<H", buffer, position + 20)[0]
            if position + 22 + comment_length == len(buffer):
                return position
        position = buffer.rfind(_EOCD, 0, position)
    return -1


def _parse_extra(extra: bytes) -> dict[int, bytes]:
    fields: dict[int, bytes] = {}
    index = 0
    while index + 4 <= len(extra):
        header_id, size = struct.unpack_from("<HH", extra, index)
        fields.setdefault(header_id, extra[index + 4 : index + 4 + size])
        index += 4 + size
    return fields


def _parse_central_directory(directory: bytes, entries: int, refuse) -> list[dict[str, Any]]:
    members: list[dict[str, Any]] = []
    index = 0
    while index < len(directory):
        if directory[index : index + 4] != _CENTRAL or index + 46 > len(directory):
            raise refuse(
                "malformed_central_directory",
                f"Entry {len(members) + 1} at directory byte {index} is not a central directory header.",
            )
        (
            _version_made,
            _version_needed,
            flags,
            method,
            mod_time,
            mod_date,
            crc,
            compressed,
            uncompressed,
            name_length,
            extra_length,
            comment_length,
            disk_start,
            _internal,
            external,
            header_offset,
        ) = struct.unpack_from("<HHHHHHIIIHHHHHII", directory, index + 4)
        end = index + 46 + name_length + extra_length + comment_length
        if end > len(directory):
            raise refuse("malformed_central_directory", f"Entry {len(members) + 1} runs past the directory's end.")
        raw_name = directory[index + 46 : index + 46 + name_length]
        extra = _parse_extra(directory[index + 46 + name_length : index + 46 + name_length + extra_length])
        utf8 = bool(flags & 0x0800)
        name = raw_name.decode("utf-8" if utf8 else "cp437", errors="replace")
        name_source = "utf8_flag" if utf8 else "cp437"
        unicode_path = extra.get(0x7075)
        if not utf8 and unicode_path and len(unicode_path) >= 5 and unicode_path[0] == 1:
            # Info-ZIP Unicode Path: valid only while its CRC32 still matches the header name.
            if struct.unpack_from("<I", unicode_path, 1)[0] == (_crc32(raw_name)):
                try:
                    name = unicode_path[5:].decode("utf-8")
                    name_source = "unicode_path_extra"
                except UnicodeDecodeError:
                    pass
        zip64 = extra.get(0x0001)
        zip64_used = False
        if zip64 is not None or 0xFFFFFFFF in (compressed, uncompressed, header_offset) or disk_start == 0xFFFF:
            values = list(struct.unpack_from(f"<{len(zip64 or b'') // 8}Q", zip64 or b""))
            try:
                if uncompressed == 0xFFFFFFFF:
                    uncompressed = values.pop(0)
                    zip64_used = True
                if compressed == 0xFFFFFFFF:
                    compressed = values.pop(0)
                    zip64_used = True
                if header_offset == 0xFFFFFFFF:
                    header_offset = values.pop(0)
                    zip64_used = True
            except IndexError:
                raise refuse(
                    "malformed_central_directory",
                    f"{name}: a size or offset defers to a ZIP64 extra field that is missing or too short.",
                )
        is_directory = name.endswith("/") or name.endswith("\\")
        members.append(
            {
                "path": name,
                "is_dir": is_directory,
                "size": uncompressed,
                "compressed_size": compressed,
                "crc32": f"{crc:08X}",
                "compression": COMPRESSION_NAMES.get(method, f"method_{method}"),
                "method": method,
                "encrypted": bool(flags & 0x0001),
                "modified": _dos_datetime(mod_date, mod_time),
                "local_header_offset": header_offset,
                "zip64": zip64_used,
                "name_encoding": name_source,
                "external_attributes": external,
            }
        )
        index = end
    if len(members) != entries:
        raise refuse(
            "malformed_central_directory",
            f"The end record declares {entries:,} entries and the directory holds {len(members):,}.",
        )
    return members


def identical_content_groups(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Files whose CRC32 and size coincide, largest groups first: the same bytes filed under two or more paths.

    Two files under different unit folders with one CRC32 and size are a pair to put to a person, not to be
    settled by their folder names. An empty file is never grouped.
    """
    groups: dict[tuple[str, int], list[str]] = {}
    for member in members:
        if member.get("is_dir") or not member.get("size"):
            continue
        groups.setdefault((str(member["crc32"]), int(member["size"])), []).append(str(member["path"]))
    found = [
        {"crc32": crc, "size": size, "paths": sorted(paths)}
        for (crc, size), paths in groups.items()
        if len(paths) > 1
    ]
    return sorted(found, key=lambda group: (-len(group["paths"]), group["paths"][0]))


def _crc32(data: bytes) -> int:
    import zlib

    return zlib.crc32(data) & 0xFFFFFFFF


def list_remote_zip(
    url: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    expected_size_bytes: int | None = None,
    user_agent: str = USER_AGENT,
) -> dict[str, Any]:
    """List a remote zip's members from its central directory alone. Raises RemoteArchiveError when refused.

    The result carries every member (path, size, compressed size, CRC32, compression, timestamp) and the
    provenance of the listing: the URL and the URL answered after redirects, each request with its range,
    status and byte count, the total fetched, the archive's size, ETag and Last-Modified, and the SHA-256 of
    the central directory bytes as read. ``expected_size_bytes`` (a size a repository or the Catalog declared)
    adds a warning when the served archive differs from it; it never stops the listing.
    """
    text = str(url or "").strip()
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise RemoteArchiveError("unsupported_url", f"Give an http or https URL of a zip archive, not {text!r}.")
    try:
        cap = int(max_bytes)
    except (TypeError, ValueError):
        raise RemoteArchiveError("invalid_argument", f"max_bytes must be a whole number, not {max_bytes!r}.")
    if cap < 1024 or cap > HARD_MAX_BYTES:
        raise RemoteArchiveError(
            "invalid_argument", f"max_bytes must lie between 1,024 and {HARD_MAX_BYTES:,}; {cap:,} was given."
        )
    try:
        timeout_value, deadline_value = float(timeout_seconds), float(deadline_seconds)
    except (TypeError, ValueError):
        timeout_value = deadline_value = float("nan")
    if not (0 < timeout_value <= 300) or not (0 < deadline_value <= 900):
        raise RemoteArchiveError(
            "invalid_argument", "timeout_seconds must lie in (0, 300] and deadline_seconds in (0, 900]."
        )
    # A declared size is checked here, before any request, so a bad one is refused rather than raised late.
    declared_size: int | None = None
    if expected_size_bytes not in (None, ""):
        if isinstance(expected_size_bytes, bool):
            declared_size = -1
        elif isinstance(expected_size_bytes, float):
            # A size read from a table or JSON often arrives as a float; a finite whole number is a byte count.
            finite_whole = math.isfinite(expected_size_bytes) and expected_size_bytes.is_integer()
            declared_size = int(expected_size_bytes) if finite_whole else -1
        else:
            try:
                declared_size = int(str(expected_size_bytes).strip())
            except (TypeError, ValueError):
                declared_size = -1
        if declared_size < 0:
            raise RemoteArchiveError(
                "invalid_argument",
                f"expected_size_bytes must be a whole number of bytes or empty, not {expected_size_bytes!r}.",
            )
    reader = _RangeReader(
        text,
        max_bytes=cap,
        timeout=timeout_value,
        deadline_seconds=deadline_value,
        user_agent=user_agent,
    )
    started_at = _utc_now()
    reader.read_tail(min(INITIAL_TAIL_BYTES, cap))
    assert reader.total is not None and reader.start is not None
    total = reader.total
    if total < 22:
        raise reader._refuse("not_a_zip", f"The file is {total} bytes long, shorter than a zip end record.")

    eocd_relative = _find_eocd(reader.buffer)
    if eocd_relative < 0 and reader.start > 0:
        reader.ensure(total - min(total, EOCD_SEARCH_BYTES))
        eocd_relative = _find_eocd(reader.buffer)
    if eocd_relative < 0:
        raise reader._refuse(
            "not_a_zip",
            "No zip end of central directory record was found in the archive's last "
            f"{len(reader.buffer):,} bytes, so this is not a zip file (or it is truncated).",
        )
    eocd_offset = reader.start + eocd_relative
    (
        disk_number,
        directory_disk,
        disk_entries,
        entries,
        directory_size,
        directory_offset,
        comment_length,
    ) = struct.unpack_from("<HHHHIIH", reader.buffer, eocd_relative + 4)
    comment = reader.buffer[eocd_relative + 22 : eocd_relative + 22 + comment_length]

    zip64 = False
    directory_end = eocd_offset
    locator_offset = eocd_offset - 20
    if locator_offset >= 0 and reader.bytes_at(locator_offset, 4) == _ZIP64_LOCATOR:
        zip64 = True
        locator_disk, zip64_eocd_recorded, total_disks = struct.unpack(
            "<IQI", reader.bytes_at(locator_offset + 4, 16)
        )
        if locator_disk != 0 or total_disks > 1:
            raise reader._refuse("multi_disk", "The archive is split across disks (a multi-part zip).")
        # The record normally sits right before the locator (no extensible data); the offset the locator records
        # is tried second, since bytes before the archive proper shift it.
        candidates = []
        if locator_offset - 56 >= 0:
            candidates.append(locator_offset - 56)
        if 0 <= zip64_eocd_recorded < locator_offset - 56:
            candidates.append(zip64_eocd_recorded)
        zip64_offset = -1
        for candidate in candidates:
            if reader.bytes_at(candidate, 4) == _ZIP64_EOCD:
                zip64_offset = candidate
                break
        if zip64_offset < 0:
            raise reader._refuse(
                "malformed_archive", "The ZIP64 locator points at no ZIP64 end of central directory record."
            )
        record = reader.bytes_at(zip64_offset, 56)
        (
            _record_size,
            _made,
            _needed,
            disk_number,
            directory_disk,
            disk_entries,
            entries,
            directory_size,
            directory_offset,
        ) = struct.unpack_from("<QHHIIQQQQ", record, 4)
        directory_end = zip64_offset
    # Without a ZIP64 locator, 0xFFFF and 0xFFFFFFFF in the end record are the values themselves: CPython's
    # zipfile writes a plain end record for exactly 65,535 entries. The directory's fit and its parsed entry
    # count below decide whether they are.
    if disk_number != 0 or directory_disk != 0 or disk_entries != entries:
        raise reader._refuse("multi_disk", "The archive is split across disks (a multi-part zip).")

    # Bytes before the archive proper (a self-extractor stub, or a zip appended to another file) shift every
    # offset; the directory still ends where the end records begin.
    prefix = directory_end - directory_size - directory_offset
    if prefix < 0 or directory_size > directory_end:
        raise reader._refuse(
            "malformed_archive",
            f"The central directory ({directory_size:,} bytes at offset {directory_offset:,}) does not fit "
            f"before the end record at {directory_end:,}."
            + (
                " The end record holds 0xFFFFFFFF, the ZIP64 placeholder, but no ZIP64 locator precedes it."
                if not zip64 and 0xFFFFFFFF in (directory_size, directory_offset)
                else ""
            ),
        )
    directory_start = directory_offset + prefix
    if reader.start is not None and directory_start < reader.start:
        needed = reader.start - directory_start
        if reader.bytes_fetched + needed > cap:
            raise reader._refuse(
                "byte_cap_exceeded",
                f"The central directory is {directory_size:,} bytes ({entries:,} entries); reading the "
                f"{needed:,} bytes not yet held would pass the cap of {cap:,} after {reader.bytes_fetched:,} "
                f"already fetched. Nothing more was requested; raise max_bytes (at most {HARD_MAX_BYTES:,}).",
            )
    directory = reader.bytes_at(directory_start, directory_size)
    members = _parse_central_directory(directory, entries, reader._refuse)

    files = [member for member in members if not member["is_dir"]]
    warnings: list[str] = []
    if declared_size is not None and declared_size != total:
        warnings.append(
            f"The server's archive is {total:,} bytes and {declared_size:,} were declared for it; the "
            "listing describes the archive served now."
        )
    if prefix:
        warnings.append(f"{prefix:,} bytes precede the archive proper; local header offsets are as recorded.")
    encrypted = sum(1 for member in members if member["encrypted"])
    if encrypted:
        warnings.append(f"{encrypted} member(s) are encrypted.")
    if not reader._validator():
        warnings.append(
            "The server sent neither a strong ETag nor Last-Modified, so the requests could be checked against "
            "each other only by the archive's total size."
        )
    seen: dict[str, int] = {}
    for member in members:
        seen[member["path"]] = seen.get(member["path"], 0) + 1
    duplicates = sorted(path for path, count in seen.items() if count > 1)
    if duplicates:
        warnings.append(f"{len(duplicates)} path(s) occur more than once in the directory: {duplicates[:5]}.")

    provenance = reader.provenance()
    provenance.update(
        {
            "started_at": started_at,
            "finished_at": _utc_now(),
            "central_directory_offset": directory_start,
            "central_directory_size": directory_size,
            "central_directory_sha256": hashlib.sha256(directory).hexdigest(),
            "prefix_bytes": prefix,
        }
    )
    return {
        "ok": True,
        "schema": SCHEMA,
        "url": text,
        "size_bytes": total,
        "zip64": zip64,
        "entry_count": entries,
        "file_count": len(files),
        "directory_count": len(members) - len(files),
        "uncompressed_bytes": sum(member["size"] for member in files),
        "compressed_bytes": sum(member["compressed_size"] for member in files),
        "comment": comment.decode("utf-8", errors="replace"),
        "members": members,
        "warnings": warnings,
        "provenance": provenance,
    }
