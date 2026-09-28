# SPDX-License-Identifier: Apache-2.0
"""``witnesses.json``: the public directory of witnesses, its validator, and
the one way a verifier reads keys and operator names out of it.

A witness is a transparency service that takes a log's checkpoint and returns
a receipt a verifier can check offline. The directory says who runs each one
and which keys its receipts are signed with. No row is privileged: rows are
alphabetical by ``name``, every row (the maintainers' own included) passes the
same checks, and :func:`row_for` / :func:`row_public_keys_pem` treat every row
identically -- ``capsule_emit.witness_bindings.verify_witnesses`` has no other
source of a witness's key and no built-in default for any service.

What the directory does NOT decide is trust. A row says who holds a key.
Whether a verifier accepts that operator, and how many distinct operators it
requires, is the verifier's policy.

**Keys.** ``key_ids`` entries are 64 lowercase hex characters. For an Ed25519
key this is the raw 32-byte public key. A service whose key is not Ed25519
(a Rekor log signs with ECDSA P-256) lists the SHA-256 of the key's DER
SubjectPublicKeyInfo -- the RFC 6962 log ID -- and carries the key itself in
``public_keys`` (base64 DER); every ``public_keys`` entry must hash to one of
the row's ``key_ids``.

**Binding.** ``binding`` is optional: ``cll`` (the default, ``POST
/checkpoints``), ``rekor`` or ``scrapi``. It is the same name
``capsule_emit.witness_bindings`` uses for the URL scheme prefix.

Run ``python -m capsule_emit.witness_directory [witnesses.json]`` to check a
file; it prints each problem and exits non-zero if there is one.
"""
from __future__ import annotations

import base64
import binascii
import datetime
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

DIRECTORY_VERSION = "1"

TOP_LEVEL_FIELDS = ("directory_version", "witnesses")
WITNESS_FIELDS = ("name", "endpoint", "key_ids", "since")
OPTIONAL_WITNESS_FIELDS = ("binding", "public_keys")
BINDINGS = ("cll", "rekor", "scrapi")
DEFAULT_BINDING = "cll"

_KEY_ID = re.compile(r"^[0-9a-f]{64}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PLACEHOLDER = "PLACEHOLDER"


def _check_name(where: str, value: object, errors: list[str]) -> None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{where}.name: must be a non-empty string")
    elif value != value.strip():
        errors.append(f"{where}.name: no leading or trailing whitespace")


def _check_endpoint(where: str, value: object, errors: list[str]) -> None:
    if not isinstance(value, str):
        errors.append(f"{where}.endpoint: must be a string")
        return
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname:
        errors.append(f"{where}.endpoint: must be an absolute https URL")
    elif parts.username is not None or parts.password is not None:
        errors.append(f"{where}.endpoint: must not embed credentials")
    elif parts.query or parts.fragment:
        errors.append(f"{where}.endpoint: must not carry a query or fragment")


def _check_string_list(
    where: str, field: str, value: object, errors: list[str], *, allow_empty: bool
) -> list[str]:
    if not isinstance(value, list):
        errors.append(f"{where}.{field}: must be a list")
        return []
    if not value and not allow_empty:
        errors.append(f"{where}.{field}: must not be empty")
    strings: list[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{where}.{field}[{i}]: must be a non-empty string")
        else:
            strings.append(item)
    if len(set(strings)) != len(strings):
        errors.append(f"{where}.{field}: duplicate values")
    return strings


def _check_key_ids(where: str, value: object, errors: list[str]) -> list[str]:
    keys = _check_string_list(where, "key_ids", value, errors, allow_empty=False)
    for i, key in enumerate(keys):
        if key.startswith(_PLACEHOLDER):
            errors.append(f"{where}.key_ids[{i}]: placeholder, fill in the real key before merge")
        elif not _KEY_ID.match(key):
            errors.append(f"{where}.key_ids[{i}]: must be 64 lowercase hex characters")
    return keys


def _der(b64: str) -> bytes | None:
    try:
        return base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return None


def _check_public_keys(where: str, value: object, key_ids: list[str], errors: list[str]) -> None:
    from cryptography.hazmat.primitives.serialization import load_der_public_key

    for i, b64 in enumerate(_check_string_list(where, "public_keys", value, errors, allow_empty=False)):
        der = _der(b64)
        try:
            load_der_public_key(der) if der is not None else None
        except ValueError:
            der = None
        if der is None:
            errors.append(f"{where}.public_keys[{i}]: must be a base64 DER SubjectPublicKeyInfo")
        elif hashlib.sha256(der).hexdigest() not in key_ids:
            errors.append(f"{where}.public_keys[{i}]: its SHA-256 is not one of this row's key_ids")


def _check_since(where: str, value: object, errors: list[str]) -> None:
    if isinstance(value, str) and value.startswith(_PLACEHOLDER):
        errors.append(f"{where}.since: placeholder, fill in the real date before merge")
        return
    if not isinstance(value, str) or not _DATE.match(value):
        errors.append(f"{where}.since: must be a YYYY-MM-DD date")
        return
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        errors.append(f"{where}.since: {value!r} is not a calendar date")


def _check_rows(rows: object, errors: list[str]) -> None:
    if not isinstance(rows, list):
        errors.append("witnesses: must be a list")
        return
    key_owner: dict[str, str] = {}
    sort_keys: list[tuple[str, str]] = []
    for i, row in enumerate(rows):
        where = f"witnesses[{i}]"
        if not isinstance(row, dict):
            errors.append(f"{where}: must be an object")
            continue
        missing = [f for f in WITNESS_FIELDS if f not in row]
        unknown = sorted(set(row) - set(WITNESS_FIELDS) - set(OPTIONAL_WITNESS_FIELDS))
        if missing:
            errors.append(f"{where}: missing field(s) {', '.join(missing)}")
        if unknown:
            errors.append(f"{where}: unknown field(s) {', '.join(unknown)}")
        _check_name(where, row.get("name"), errors)
        _check_endpoint(where, row.get("endpoint"), errors)
        key_ids = _check_key_ids(where, row.get("key_ids"), errors)
        for key in key_ids:
            if key in key_owner:
                errors.append(f"{where}.key_ids: {key[:16]}... already listed at {key_owner[key]}")
            else:
                key_owner[key] = where
        _check_since(where, row.get("since"), errors)
        if "binding" in row and row["binding"] not in BINDINGS:
            errors.append(f"{where}.binding: must be one of {', '.join(BINDINGS)}")
        if "public_keys" in row:
            _check_public_keys(where, row["public_keys"], key_ids, errors)
        name, endpoint = row.get("name"), row.get("endpoint")
        if isinstance(name, str) and isinstance(endpoint, str):
            sort_keys.append((name.casefold(), endpoint))
    if len(set(sort_keys)) != len(sort_keys):
        errors.append("witnesses: two rows share the same name and endpoint")
    if sort_keys != sorted(sort_keys):
        errors.append("witnesses: rows must be in alphabetical order by name (case-insensitive), then endpoint")


def validate_directory(doc: object) -> list[str]:
    """Return every problem found in a parsed ``witnesses.json``; empty means valid."""
    if not isinstance(doc, dict):
        return ["directory: must be a JSON object"]
    errors: list[str] = []
    unknown = sorted(set(doc) - set(TOP_LEVEL_FIELDS))
    if unknown:
        errors.append(f"directory: unknown field(s) {', '.join(unknown)}")
    if doc.get("directory_version") != DIRECTORY_VERSION:
        errors.append(f'directory_version: must be "{DIRECTORY_VERSION}"')
    _check_rows(doc.get("witnesses"), errors)
    return errors


def validate_file(path: str | Path) -> list[str]:
    """Parse ``path`` as JSON and validate it."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"{path}: cannot read as JSON ({exc})"]
    return validate_directory(doc)


def load_directory(path: str | Path) -> dict:
    """Read and validate ``path``; raise ``ValueError`` listing every problem."""
    errors = validate_file(path)
    if errors:
        raise ValueError("; ".join(errors))
    return json.loads(Path(path).read_text(encoding="utf-8"))


# -- reading rows: the same code path for every row ---------------------------


def _rows(directory: Any) -> list[dict]:
    if isinstance(directory, dict):
        return list(directory.get("witnesses") or [])
    return list(directory or [])


def row_for(directory: Any, ts_url: str) -> dict | None:
    """The row a receipt's witness URL belongs to: same binding (a row
    without ``binding`` is ``cll``) and the same https endpoint, trailing
    slash ignored. ``None`` when no row matches -- a receipt from an unlisted
    witness is then ``not checked`` unless the verifier pins a key itself."""
    from .witness_bindings import binding_of, endpoint_of

    binding = binding_of(ts_url)
    endpoint = endpoint_of(ts_url)
    for row in _rows(directory):
        if row.get("binding", DEFAULT_BINDING) == binding and row.get("endpoint", "").rstrip("/") == endpoint:
            return row
    return None


def row_public_keys_pem(row: dict) -> list[bytes]:
    """Every public key a row lists, as SubjectPublicKeyInfo PEM, in
    ``key_ids`` order: the ``public_keys`` entry whose SHA-256 is the key id
    when there is one, else the key id read as a raw Ed25519 key."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
        load_der_public_key,
    )

    by_hash = {}
    for b64 in row.get("public_keys", []):
        der = _der(b64)
        if der is not None:
            by_hash[hashlib.sha256(der).hexdigest()] = der
    pems = []
    for key_id in row.get("key_ids", []):
        if key_id in by_hash:
            key = load_der_public_key(by_hash[key_id])
        else:
            key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(key_id))
        pems.append(key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))
    return pems


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    path = args[0] if args else "witnesses.json"
    errors = validate_file(path)
    for error in errors:
        print(error)
    if not errors:
        print(f"{path}: ok")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
