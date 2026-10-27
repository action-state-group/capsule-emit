# SPDX-License-Identifier: Apache-2.0
"""The artifact store's optional index extension.

A namespace declares JSON paths into its Capsules; the store extracts their
values when it admits a record, and :meth:`IndexStore.find` returns matching
Capsule IDs without reading record bodies (``docs/artifact-store.md``,
"Optional index extension"). Detect it with ``isinstance(store, IndexStore)``;
every built-in backend except the legacy ledger implements it.

This module also holds what the backends share, as the Go module's
``artifact/internal/index`` package does: path validation, value extraction
from Capsule bytes, timestamp normalization, query checks, cursors, and the
in-memory index that the jsonl and memory backends keep.
"""

from __future__ import annotations

import bisect
import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from . import Store
from .record import ConflictError, CorruptError, InvalidError, decode_strict_json

__all__ = [
    "DEFAULT_FIND_LIMIT",
    "MAX_FIND_LIMIT",
    "MAX_INDEX_PATHS",
    "IndexStore",
    "Page",
]

#: The most paths one namespace may declare.
MAX_INDEX_PATHS = 32
#: The page size :meth:`IndexStore.find` uses when ``limit`` is not given.
DEFAULT_FIND_LIMIT = 100
#: The largest page :meth:`IndexStore.find` returns.
MAX_FIND_LIMIT = 1000

_MAX_PATH_BYTES = 255
_MAX_SEGMENTS = 8
# [0-9], not \d: Python's \d also matches non-ASCII digits.
_SEGMENT = re.compile(r"[A-Za-z0-9_-]{1,64}")
_TIMESTAMP = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(\.[0-9]{1,9})?(Z|[+-][0-9]{2}:[0-9]{2})"
)
_CURSOR = re.compile(r"[1-9][0-9]*")
_MAX_CURSOR = (1 << 63) - 1


@dataclass
class Page:
    """One page of :meth:`IndexStore.find` results.

    ``ids`` are the matching Capsule IDs in the store's admission order, which
    is not the order of any CLL log. ``next`` is the cursor for the following
    page, empty when there is none."""

    ids: list = field(default_factory=list)
    next: str = ""


@runtime_checkable
class IndexStore(Store, Protocol):
    """The optional index extension. Detect it with
    ``isinstance(store, IndexStore)``."""

    def init_index(self, paths: Iterable[str]) -> None:
        """Provisions storage as ``init`` does, then declares ``paths`` and
        indexes the records already stored. Declaring the same set again does
        nothing; a different set raises :class:`ConflictError`."""

    def reindex(self, paths: Iterable[str]) -> None:
        """Replaces the declared paths and rebuilds the index."""

    def find(
        self,
        where: Mapping[str, str] | None = None,
        since: str | None = None,
        after: str | None = None,
        limit: int = DEFAULT_FIND_LIMIT,
    ) -> Page:
        """Returns one page of matching Capsule IDs. ``where`` maps declared
        paths to the value each must hold (a string, or an array containing
        it); ``since`` is an RFC 3339 date-time; ``after`` is the ``next`` of a
        previous page; ``limit`` is 1 to :data:`MAX_FIND_LIMIT`."""


# ---- Paths -----------------------------------------------------------------


def _valid_path(path: Any) -> bool:
    if not isinstance(path, str) or len(path.encode("utf-8", "surrogatepass")) > _MAX_PATH_BYTES:
        return False
    segments = path.split(".")
    return len(segments) <= _MAX_SEGMENTS and all(_SEGMENT.fullmatch(s) for s in segments)


def normalize_paths(paths: Iterable[str]) -> list:
    """Validates a declared path list and returns it sorted. The list is a
    set: order does not matter and a repeated path is invalid."""
    if isinstance(paths, (str, bytes)) or paths is None:
        raise InvalidError("index paths must be a list of strings")
    paths = list(paths)
    if len(paths) > MAX_INDEX_PATHS:
        raise InvalidError(f"more than {MAX_INDEX_PATHS} index paths")
    for path in paths:
        if not _valid_path(path):
            raise InvalidError(f"index path {path!r}")
    normalized = sorted(paths)
    for previous, path in zip(normalized, normalized[1:]):
        if previous == path:
            raise InvalidError(f"repeated index path {path!r}")
    return normalized


def encode_paths(paths: list) -> str:
    """The stored form of a normalized path list: a compact JSON array. Path
    characters need no escaping, so every language writes the same text."""
    return "[" + ",".join('"' + p + '"' for p in paths) + "]"


def decode_paths(stored: Any) -> list:
    """Reads :func:`encode_paths` output back. Anything else is corrupt."""
    try:
        if isinstance(stored, (bytes, bytearray, memoryview)):
            stored = bytes(stored).decode("utf-8")
        paths = decode_strict_json(stored.encode("utf-8"))
        if not isinstance(paths, list):
            raise ValueError("not an array")
        return normalize_paths(paths)
    except (AttributeError, ValueError, InvalidError) as exc:
        raise CorruptError(f"stored index paths: {exc}") from None


# ---- Values ------------------------------------------------------------------


@dataclass
class Extracted:
    """What a record contributes to the index: ``(path, value)`` entries and
    its top-level timestamp in UTC microseconds, or None."""

    entries: list = field(default_factory=list)
    ts: int | None = None


def extract(capsule: bytes, paths: list) -> Extracted:
    """Reads the declared paths and the top-level timestamp from Capsule bytes.
    A string value is one entry and each distinct string in an array is one
    entry; any other value, or a missing member, gives none. The bytes were
    verified at admission, so a decode failure raises :class:`CorruptError`."""
    try:
        root = decode_strict_json(bytes(capsule))
    except ValueError as exc:
        raise CorruptError(f"capsule is not JSON: {exc}") from None
    out = Extracted()
    if not isinstance(root, dict):
        return out
    timestamp = root.get("timestamp")
    if isinstance(timestamp, str):
        out.ts = parse_timestamp(timestamp)
    for path in paths:
        value: Any = root
        for segment in path.split("."):
            value = value.get(segment) if isinstance(value, dict) else None
        if isinstance(value, str):
            out.entries.append((path, value))
        elif isinstance(value, list):
            seen = set()
            for element in value:
                if isinstance(element, str) and element not in seen:
                    seen.add(element)
                    out.entries.append((path, element))
    return out


def _leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _days_in(year: int, month: int) -> int:
    if month == 2:
        return 29 if _leap(year) else 28
    return 30 if month in (4, 6, 9, 11) else 31


def _days_from_civil(year: int, month: int, day: int) -> int:
    """Days since 1970-01-01 in the proleptic Gregorian calendar; unlike
    ``datetime``, it covers year 0."""
    year -= month <= 2
    era = year // 400
    yoe = year - era * 400
    doy = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def parse_timestamp(value: str) -> int | None:
    """Reads an RFC 3339 date-time with an upper-case T and Z (or a numeric
    offset) and at most nine fraction digits, and returns it in UTC
    microseconds since the Unix epoch, truncating finer digits; None when it
    is not one."""
    m = _TIMESTAMP.fullmatch(value)
    if m is None:
        return None
    year, month, day, hour, minute, second = (int(m.group(i)) for i in range(1, 7))
    if not (1 <= month <= 12 and 1 <= day <= _days_in(year, month) and hour <= 23 and minute <= 59 and second <= 59):
        return None
    micros = int((m.group(7)[1:] + "00000")[:6]) if m.group(7) else 0
    zone = m.group(8)
    offset = 0
    if zone != "Z":
        oh, om = int(zone[1:3]), int(zone[4:6])
        if oh > 23 or om > 59:
            return None
        offset = oh * 60 + om
        if zone[0] == "-":
            offset = -offset
    seconds = _days_from_civil(year, month, day) * 86400 + hour * 3600 + minute * 60 + second
    return seconds * 1_000_000 + micros - offset * 60_000_000


def value_key(value: str) -> str:
    """The key SQL backends store for a value: SHA-256 of its UTF-8 bytes in
    lowercase hex, so values of any length fit an index key."""
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


# ---- Queries -----------------------------------------------------------------


@dataclass
class Checked:
    """A validated query; ``where`` holds ``(path, value)`` sorted by path."""

    where: list
    since: int | None
    after: int
    limit: int


def check_query(where: Any, since: Any, after: Any, limit: Any, declared: list) -> Checked:
    """Validates a :meth:`IndexStore.find` query against the declared paths.
    An empty ``since`` or ``after`` is the same as None."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_FIND_LIMIT:
        raise InvalidError(f"limit {limit!r}")
    if where is None:
        where = {}
    if not isinstance(where, Mapping):
        raise InvalidError("where must map paths to strings")
    pairs = []
    for path, value in where.items():
        if not isinstance(path, str) or path not in declared:
            raise InvalidError(f"{path!r} is not a declared index path")
        if not isinstance(value, str):
            raise InvalidError(f"where value for {path!r} is not a string")
        pairs.append((path, value))
    pairs.sort()
    since_us = None
    if since is not None and since != "":
        since_us = parse_timestamp(since) if isinstance(since, str) else None
        if since_us is None:
            raise InvalidError(f"since {since!r} is not an RFC 3339 date-time")
    position = 0
    if after is not None and after != "":
        if not isinstance(after, str) or not _CURSOR.fullmatch(after) or int(after) > _MAX_CURSOR:
            raise InvalidError("cursor")
        position = int(after)
    return Checked(where=pairs, since=since_us, after=position, limit=limit)


def cursor(position: int) -> str:
    """Formats an admission position as the opaque cursor ``find`` returns."""
    return str(position)


# ---- In-memory index -----------------------------------------------------------


class MemoryIndex:
    """The in-memory index of the jsonl and memory backends. Positions start at
    1 and follow admission order. Not thread-safe; the backend holds its own
    lock."""

    def __init__(self) -> None:
        self._paths: list | None = None
        self._ids: list = []  # by position - 1
        self._ts: list = []  # by position - 1
        self._values: dict = {}  # path -> value -> ascending positions
        self._position: dict = {}  # capsule ID -> position

    @property
    def declared(self) -> list | None:
        """The declared paths, or None before a declaration."""
        return self._paths

    def declare(self, paths: Iterable[str]) -> bool:
        """Records paths for a fresh index, or checks them against the
        existing declaration: the same set is a no-op, another is a conflict.
        Returns whether the caller must now add the existing records."""
        normalized = normalize_paths(paths)
        if self._paths is not None:
            if self._paths != normalized:
                raise ConflictError(f"namespace already indexes {encode_paths(self._paths)}")
            return False
        self.reset(normalized)
        return True

    def reset(self, normalized: list) -> None:
        """Replaces the declaration with normalized paths and empties the index."""
        self._paths = normalized
        self._ids, self._ts, self._values, self._position = [], [], {}, {}

    def add(self, capsule_id: str, capsule: bytes) -> None:
        """Indexes one record at the next position. A record already indexed is
        left where it is; nothing happens before a declaration."""
        if self._paths is None or capsule_id in self._position:
            return
        extracted = extract(capsule, self._paths)
        self._ids.append(capsule_id)
        position = len(self._ids)
        self._position[capsule_id] = position
        self._ts.append(extracted.ts)
        for path, value in extracted.entries:
            self._values.setdefault(path, {}).setdefault(value, []).append(position)

    def find(self, where: Any, since: Any, after: Any, limit: Any) -> Page:
        if self._paths is None:
            raise InvalidError("no index paths declared")
        c = check_query(where, since, after, limit, self._paths)
        if c.where:
            path, value = c.where[0]
            positions = self._values.get(path, {}).get(value, [])
            candidates: Iterable[int] = positions[bisect.bisect_left(positions, c.after + 1) :]
        else:
            candidates = range(c.after + 1, len(self._ids) + 1)
        page = Page()
        last = 0
        for p in candidates:
            if not self._matches(p, c):
                continue
            if len(page.ids) == c.limit:
                page.next = cursor(last)
                break
            page.ids.append(self._ids[p - 1])
            last = p
        return page

    def _matches(self, p: int, c: Checked) -> bool:
        if c.since is not None:
            ts = self._ts[p - 1]
            if ts is None or ts < c.since:
                return False
        for path, value in c.where[1:]:
            positions = self._values.get(path, {}).get(value, [])
            i = bisect.bisect_left(positions, p)
            if i == len(positions) or positions[i] != p:
                return False
        return True

