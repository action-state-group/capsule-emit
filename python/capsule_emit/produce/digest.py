# SPDX-License-Identifier: Apache-2.0
"""JSON-DIGEST and strict JSON decoding, mirroring Go ``digest.go`` and the
decoder in ``build.go``. Canonicalization is agent-action-capsule's
``json_digest``/``jcs``; this module only checks values on the way in."""
from __future__ import annotations

import json
from typing import Any, Callable, TypeVar

from agent_action_capsule.canonical import json_digest

__all__ = ["digest_json", "MAX_JSON_DEPTH"]

_T = TypeVar("_T")

#: The deepest JSON nesting accepted, matching Go's decoder.
MAX_JSON_DEPTH = 1000


def digest_json(value: Any) -> str:
    """Return the AAC JSON-DIGEST of ``value``: lowercase-hex SHA-256 over
    RFC 8785 JCS. ``value`` is a JSON value built from ``dict`` (string
    keys), ``list``, ``str``, ``int``, ``bool`` and ``None``. Floats, integers
    outside the interoperable range, lone surrogates and excessive depth are
    rejected with ``ValueError``; other types with ``TypeError``."""
    check_json_value(value, "$")
    return canonical(json_digest, integral_floats(value))


def integral_floats(value: Any) -> Any:
    """Copy ``value`` with each finite whole-number ``float`` as an ``int``
    (``-0.0`` as ``0``), as Go's digest does by marshaling the value before
    canonicalizing: ``1.0`` digests as ``1``. Other floats are kept, so the
    canonicalizer still rejects them. Iterative, so depth costs no recursion."""

    def convert(item: Any) -> Any:
        if isinstance(item, float) and item == item and item not in (float("inf"), float("-inf")) and item.is_integer():
            return int(item)
        if isinstance(item, dict):
            return {}
        if isinstance(item, list):
            return [None] * len(item)
        return item

    root = convert(value)
    stack: list = [(value, root)]
    while stack:
        source, target = stack.pop()
        if isinstance(source, dict):
            members = source.items()
        elif isinstance(source, list):
            members = enumerate(source)
        else:
            continue
        for key, member in members:
            copy = convert(member)
            target[key] = copy
            stack.append((member, copy))
    return root


def canonical(call: Callable[..., _T], *args: Any) -> _T:
    """Run an agent-action-capsule canonicalization call. Its serializer
    recurses once per nesting level, so JSON within :data:`MAX_JSON_DEPTH` can
    still exhaust Python's recursion limit (Go accepts it); report that as the
    same ``ValueError`` as excessive depth rather than a ``RecursionError``."""
    try:
        return call(*args)
    except RecursionError:
        raise ValueError("JSON nesting is too deep for this canonicalizer") from None


def check_json_value(value: Any, path: str) -> None:
    """Reject what a JSON value may not carry into a digest: non-string keys,
    unpaired surrogates and nesting deeper than :data:`MAX_JSON_DEPTH`.
    Numbers are left to agent-action-capsule's canonicalizer, which rejects
    floats and unsafe integers."""
    stack: list[tuple[Any, str, int]] = [(value, path, 1)]
    while stack:
        item, where, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            raise ValueError(f"JSON exceeds maximum depth of {MAX_JSON_DEPTH}")
        if isinstance(item, str):
            _check_text(item, where)
        elif isinstance(item, dict):
            for key, member in item.items():
                if not isinstance(key, str):
                    raise TypeError(f"JSON object key at {where} must be a string")
                _check_text(key, where)
                stack.append((member, f"{where}.{key}", depth + 1))
        elif isinstance(item, list):
            for index, member in enumerate(item):
                stack.append((member, f"{where}[{index}]", depth + 1))


def _check_text(text: str, where: str) -> None:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"text at {where} must be valid Unicode (unpaired surrogate)") from None


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate object key {json.dumps(key)}")
        result[key] = value
    return result


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-standard JSON constant {name}")


def decode_strict_json(data: Any) -> Any:
    """Decode UTF-8 JSON text, rejecting invalid UTF-8, duplicate object
    names, NaN/Infinity, trailing data and excessive depth."""
    if isinstance(data, (bytes, bytearray, memoryview)):
        try:
            text = bytes(data).decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("JSON must be UTF-8") from None
    elif isinstance(data, str):
        text = data
    else:
        raise TypeError("JSON input must be bytes or str")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except RecursionError:
        raise ValueError(f"JSON exceeds maximum depth of {MAX_JSON_DEPTH}") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from None
    check_json_value(value, "$")
    return value
