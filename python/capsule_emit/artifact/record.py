# SPDX-License-Identifier: Apache-2.0
"""The storage-driver-free artifact record model.

A byte-compatible Python port of the Go module's ``artifact`` package and the
TypeScript ``./artifact`` subpath, following the shared contract in
``docs/artifact-store.md``. It verifies with the ``agent_action_capsule``
reference verifier rather than reimplementing Capsule or Producer Envelope
checks.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

PAYLOAD_DIGEST = "model_attestation.compute_attestation.agent_input_digest"
AGENT_OUTPUT_DIGEST = "model_attestation.compute_attestation.agent_output_digest"
EFFECT_REQUEST_DIGEST = "effect.request_digest"
EFFECT_RESPONSE_DIGEST = "effect.response_digest"
DIGEST_FIELDS = frozenset({PAYLOAD_DIGEST, AGENT_OUTPUT_DIGEST, EFFECT_REQUEST_DIGEST, EFFECT_RESPONSE_DIGEST})

PRESENT = "present"
PURGED = "purged"
NEVER_RETAINED = "never_retained"

MAX_ARTIFACTS = 64
MAX_ENVELOPE = 65_535
MAX_TOTAL = 8 * 1024 * 1024
_MAX_JSON_DEPTH = 1000

NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
ID_PATTERN = re.compile(r"[0-9a-f]{64}")


def valid_name(value: object) -> bool:
    """Reports whether ``value`` is a valid namespace or artifact name."""
    return isinstance(value, str) and NAME_PATTERN.fullmatch(value) is not None


def valid_id(value: object) -> bool:
    """Reports whether ``value`` is 64 lowercase hex characters."""
    return isinstance(value, str) and ID_PATTERN.fullmatch(value) is not None


class ArtifactError(Exception):
    """A contract failure. ``code`` is one of the contract's error codes."""

    code = ""

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.code)


class InvalidError(ArtifactError):
    code = "invalid"


class ConflictError(ArtifactError):
    code = "conflict"


class NotFoundError(ArtifactError):
    code = "not_found"


class PurgedError(ArtifactError):
    code = "purged"


class DigestMismatchError(ArtifactError):
    code = "digest_mismatch"


class UntrustedSignerError(ArtifactError):
    code = "untrusted_signer"


class CorruptError(ArtifactError):
    code = "corrupt"


class UnsupportedError(ArtifactError):
    """The store does not offer this operation: a declared capability limit
    (the legacy ledger backend refuses originals and purge)."""

    code = "unsupported"


@dataclass
class Artifact:
    """An exact original. An empty ``binding`` means NOT authenticated by the
    Capsule. ``content`` is None when absent; ``b""`` is present and empty.
    ``content_sha256`` protects exact-byte storage integrity, not authorship."""

    name: str
    state: str
    binding: str = ""
    content: bytes | None = None
    content_sha256: str = ""


@dataclass
class Record:
    """A Capsule with one Producer Envelope and its declared originals."""

    capsule_id: str
    capsule: bytes
    producer_envelope: bytes
    artifacts: list = field(default_factory=list)


@dataclass(frozen=True)
class ArtifactVerification:
    """Separates a verified preimage from an unbound attachment."""

    state: str
    bound: bool
    verified: bool


def copy_record(record: Record) -> Record:
    """Returns a deep copy that shares no mutable state with ``record``."""
    return Record(
        capsule_id=record.capsule_id,
        capsule=bytes(record.capsule),
        producer_envelope=bytes(record.producer_envelope),
        artifacts=[dataclasses.replace(a) for a in record.artifacts],
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _StrictJSONError(ValueError):
    pass


def _reject_duplicates(pairs: list) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise _StrictJSONError(f"duplicate object key {key!r}")
        result[key] = value
    return result


def _reject_constant(token: str) -> Any:
    raise _StrictJSONError(f"non-JSON constant {token}")


def _depth(value: Any, level: int = 1) -> int:
    stack = [(value, level)]
    deepest = level
    while stack:
        item, depth = stack.pop()
        deepest = max(deepest, depth)
        if deepest > _MAX_JSON_DEPTH:
            break
        if isinstance(item, dict):
            stack.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            stack.extend((v, depth + 1) for v in item)
    return deepest


def decode_strict_json(data: bytes) -> Any:
    """Decodes UTF-8 JSON, rejecting duplicate keys, NaN/Infinity, trailing
    data and nesting beyond 1000 levels, like the Go and TypeScript decoders."""
    try:
        text = bytes(data).decode("utf-8")
        value = json.loads(text, object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise _StrictJSONError(str(exc)) from exc
    if _depth(value) > _MAX_JSON_DEPTH:
        raise _StrictJSONError("JSON exceeds maximum depth")
    return value


def _decode_capsule(data: bytes) -> dict:
    capsule = decode_strict_json(data)
    if not isinstance(capsule, dict):
        raise _StrictJSONError("capsule payload is not an object")
    if capsule.get("format_version") != "4" or capsule.get("canonicalization_id") != "jcs":
        raise _StrictJSONError('only AAC format 4 with canonicalization_id "jcs" is supported')
    return capsule


def _digest_at(capsule: Mapping[str, Any], binding: str) -> str:
    if binding not in DIGEST_FIELDS:
        raise InvalidError(f"unsupported digest field {binding}")
    value: Any = capsule
    for key in binding.split("."):
        if not isinstance(value, dict) or key not in value:
            raise InvalidError(f"missing {binding}")
        value = value[key]
    if not valid_id(value):
        raise InvalidError(f"malformed {binding}")
    return value


def _json_digest(content: bytes) -> str:
    from agent_action_capsule.canonical import json_digest

    return json_digest(decode_strict_json(content))


def verify(record: Record, trusted: Sequence[bytes]) -> dict:
    """Authenticates the Capsule, authorizes its signer against caller-owned
    trusted keys, and verifies every available bound original. Returns
    ``{name: ArtifactVerification}``. It does not verify CLL inclusion,
    external references or business correctness."""
    from agent_action_capsule.producer_envelope import verify_producer_envelope
    from agent_action_capsule.verify import verify as verify_class1

    if not valid_id(record.capsule_id):
        raise InvalidError("capsule id")
    try:
        capsule = _decode_capsule(record.capsule)
    except _StrictJSONError as exc:
        raise InvalidError(f"capsule verification: {exc}") from exc
    result = verify_class1(capsule)
    if not result.ok or result.capsule_id != record.capsule_id:
        codes = ",".join(f.code for f in result.errors)
        raise InvalidError(f"capsule verification: {codes or 'capsule id mismatch'}")
    envelope = verify_producer_envelope(record.capsule_id, bytes(record.producer_envelope))
    if not envelope.ok or envelope.public_key is None:
        raise InvalidError("envelope: " + ",".join(f.code for f in envelope.findings))
    if not any(len(key) == 32 and bytes(key) == envelope.public_key for key in trusted):
        raise UntrustedSignerError("producer signer is not trusted")

    results: dict = {}
    for a in record.artifacts:
        if not valid_name(a.name):
            raise InvalidError("artifact name")
        if a.name in results:
            raise InvalidError(f"duplicate artifact {a.name}")
        committed = _digest_at(capsule, a.binding) if a.binding else ""
        verified = False
        if a.state == PRESENT:
            if a.content is None:
                raise InvalidError(f"missing content for {a.name}")
            if a.content_sha256 and a.content_sha256 != _sha256(a.content):
                raise CorruptError(a.name)
            if a.binding:
                try:
                    digest = _json_digest(a.content)
                except (ValueError, TypeError) as exc:
                    raise DigestMismatchError(f"{a.name} ({a.binding})") from exc
                if digest != committed:
                    raise DigestMismatchError(f"{a.name} ({a.binding})")
                verified = True
        elif a.state == PURGED:
            if a.content is not None or not valid_id(a.content_sha256):
                raise InvalidError("invalid purge tombstone")
        elif a.state == NEVER_RETAINED:
            if a.content is not None or a.content_sha256 != "":
                raise InvalidError("never-retained content")
        else:
            raise InvalidError("retention state")
        results[a.name] = ArtifactVerification(state=a.state, bound=bool(a.binding), verified=verified)
    return results


def _b64(data: bytes) -> str:
    return base64.b64encode(bytes(data)).decode("ascii")


def _dumps(value: Any) -> str:
    # No whitespace and no ASCII escaping, matching Go encoding/json/v2 and
    # JavaScript JSON.stringify for this store's constrained values.
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def storage_checksum(record: Record) -> str:
    """The contract's storage checksum: commits the exact Capsule, envelope and
    complete artifact inventory with each original's SHA-256, unchanged by
    purge. Byte-identical to Go ``StorageChecksum`` and TS ``storageChecksum``.
    It is NOT a producer authentication claim."""
    artifacts = []
    for a in sorted(record.artifacts, key=lambda a: a.name.encode("utf-8")):
        item: dict = {"name": a.name}
        if a.binding:
            item["binding"] = a.binding
        item["state"] = PRESENT if a.state == PURGED else a.state
        if a.content_sha256:
            item["content_sha256"] = a.content_sha256
        artifacts.append(item)
    encoded = {
        "capsule_id": record.capsule_id,
        "capsule": _b64(record.capsule),
        "producer_envelope": _b64(record.producer_envelope),
        "artifacts": artifacts,
    }
    return _sha256(_dumps(encoded).encode("utf-8"))


def prepare(record: Record) -> Record:
    """Copies the caller's buffers, fills exact-byte checksums and enforces the
    v1 admission limits. It does not authenticate a producer; stores also call
    :func:`verify` with their trusted keys before persisting."""
    prepared = copy_record(record)
    size = len(prepared.capsule) + len(prepared.producer_envelope)
    if len(prepared.artifacts) > MAX_ARTIFACTS or len(prepared.producer_envelope) > MAX_ENVELOPE:
        raise InvalidError("record limits")
    for a in prepared.artifacts:
        if a.state == PURGED:
            raise PurgedError("originals were purged")
        if a.content is not None:
            a.content = bytes(a.content)
            size += len(a.content)
        if a.state == PRESENT:
            digest = _sha256(a.content or b"")
            if a.content_sha256 and a.content_sha256 != digest:
                raise CorruptError("content checksum")
            a.content_sha256 = digest
    if size > MAX_TOTAL:
        raise InvalidError("record exceeds 8 MiB")
    return prepared


def purged_copy(record: Record) -> Record:
    """Returns ``record`` with every present original replaced by a tombstone
    that keeps its SHA-256."""
    result = copy_record(record)
    for a in result.artifacts:
        if a.state == PRESENT:
            a.content = None
            a.state = PURGED
    return result


def has_purged(record: Record) -> bool:
    return any(a.state == PURGED for a in record.artifacts)


def trusted_keys(keys: Iterable[bytes]) -> list:
    """Validates and copies a store's trusted Ed25519 public keys."""
    copied = [bytes(k) for k in keys]
    if not copied:
        raise InvalidError("store configuration")
    if any(len(k) != 32 for k in copied):
        raise InvalidError("public key size")
    return copied


def check_store_config(namespace: str, trusted: Iterable[bytes]) -> list:
    """Validates a constructor's namespace and returns the copied keys."""
    if not valid_name(namespace):
        raise InvalidError("store configuration")
    return trusted_keys(trusted)


# ---- JSONL wire codec ------------------------------------------------------


def encode_record(record: Record) -> str:
    """The record's one-line JSON wire form (no trailing newline), byte-identical
    to the Go and TypeScript JSONL stores: keys in declaration order, standard
    base64, ``binding``/``content_sha256`` omitted when empty, ``content``
    omitted only when absent."""
    artifacts = []
    for a in record.artifacts:
        item: dict = {"name": a.name}
        if a.binding:
            item["binding"] = a.binding
        if a.content is not None:
            item["content"] = _b64(a.content)
        item["state"] = a.state
        if a.content_sha256:
            item["content_sha256"] = a.content_sha256
        artifacts.append(item)
    return _dumps(
        {
            "capsule_id": record.capsule_id,
            "capsule": _b64(record.capsule),
            "producer_envelope": _b64(record.producer_envelope),
            "artifacts": artifacts,
        }
    )


def _unb64(value: Any) -> bytes:
    if not isinstance(value, str):
        raise CorruptError("malformed record in JSONL store")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise CorruptError("invalid base64 in JSONL store") from exc


def decode_record(line: str) -> Record:
    """Parses a wire line back to a Record; raises :class:`CorruptError`."""
    try:
        stored = json.loads(line)
    except ValueError as exc:
        raise CorruptError("invalid JSON in JSONL store") from exc
    if not isinstance(stored, dict) or not isinstance(stored.get("capsule_id"), str):
        raise CorruptError("malformed record in JSONL store")
    raw_artifacts = stored.get("artifacts")
    if not isinstance(raw_artifacts, list):
        raise CorruptError("malformed record in JSONL store")
    artifacts = []
    for raw in raw_artifacts:
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("name"), str)
            or not isinstance(raw.get("state"), str)
            or not isinstance(raw.get("binding", ""), str)
            or not isinstance(raw.get("content_sha256", ""), str)
        ):
            raise CorruptError("malformed artifact in JSONL store")
        artifacts.append(
            Artifact(
                name=raw["name"],
                state=raw["state"],
                binding=raw.get("binding", ""),
                content=_unb64(raw["content"]) if "content" in raw else None,
                content_sha256=raw.get("content_sha256", ""),
            )
        )
    return Record(
        capsule_id=stored["capsule_id"],
        capsule=_unb64(stored.get("capsule")),
        producer_envelope=_unb64(stored.get("producer_envelope")),
        artifacts=artifacts,
    )
