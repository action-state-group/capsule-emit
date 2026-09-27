# SPDX-License-Identifier: Apache-2.0
"""Validator for ``witnesses.json``, the public directory of witnesses and
countersigners.

The directory lists who runs a witness (a Transparency Service that receipts
checkpoints) and who issues countersignatures (a signature by a party other
than the producer over an Evidence Bundle digest, with a statement of what
that party recomputed). A verifier resolves a countersignature by its
``signer.key_id`` against ``countersigners[].key_ids``; a row is how a stamp
gets a name. No row is privileged: both arrays are alphabetical by ``name``,
and every row, including the maintainers' own, passes the same checks.

What the directory does NOT decide:

* **Independence of a given countersignature.** That is computed per entry,
  by the verifier: a countersignature whose signer key equals the producer's
  key is a self-countersignature, well-formed, and rendered as not
  independent. ``independent_of`` is the operator's own published
  declaration of the parties it is under no common control with; it can be
  contradicted, and a verifier never derives independence from it.
* **Trust.** A row says who holds a key. Whether a relying party accepts that
  operator is the relying party's call.

Run ``python -m capsule_emit.witness_directory [witnesses.json]`` to check a
file; it prints each problem and exits non-zero if there is one.
"""
from __future__ import annotations

import datetime
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

DIRECTORY_VERSION = "1"

TOP_LEVEL_FIELDS = ("directory_version", "witnesses", "countersigners")
WITNESS_FIELDS = ("name", "endpoint", "key_ids", "since")
COUNTERSIGNER_FIELDS = (
    "name",
    "endpoint",
    "key_ids",
    "statement_types_issued",
    "since",
    "independent_of",
)

#: A key id is the full 32-byte Ed25519 public key, lowercase hex -- the same
#: form ``countersignatures[].signer.key_id`` carries, so a row matches an
#: entry byte for byte and a verifier holding only the entry can check it.
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
            errors.append(
                f"{where}.key_ids[{i}]: must be a 32-byte Ed25519 public key as 64 lowercase hex characters"
            )
    return keys


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


def _check_rows(
    array: str, rows: object, fields: tuple[str, ...], errors: list[str], key_owner: dict[str, str]
) -> None:
    if not isinstance(rows, list):
        errors.append(f"{array}: must be a list")
        return
    sort_keys: list[tuple[str, str]] = []
    for i, row in enumerate(rows):
        where = f"{array}[{i}]"
        if not isinstance(row, dict):
            errors.append(f"{where}: must be an object")
            continue
        missing = [f for f in fields if f not in row]
        unknown = sorted(set(row) - set(fields))
        if missing:
            errors.append(f"{where}: missing field(s) {', '.join(missing)}")
        if unknown:
            errors.append(f"{where}: unknown field(s) {', '.join(unknown)}")
        _check_name(where, row.get("name"), errors)
        _check_endpoint(where, row.get("endpoint"), errors)
        for key in _check_key_ids(where, row.get("key_ids"), errors):
            if key in key_owner:
                errors.append(f"{where}.key_ids: {key[:16]}... already listed at {key_owner[key]}")
            else:
                key_owner[key] = where
        _check_since(where, row.get("since"), errors)
        if "statement_types_issued" in fields:
            _check_string_list(
                where, "statement_types_issued", row.get("statement_types_issued"), errors, allow_empty=False
            )
        if "independent_of" in fields:
            declared = _check_string_list(
                where, "independent_of", row.get("independent_of"), errors, allow_empty=True
            )
            if isinstance(row.get("name"), str) and row["name"] in declared:
                errors.append(f"{where}.independent_of: a row cannot declare independence of itself")
        name, endpoint = row.get("name"), row.get("endpoint")
        if isinstance(name, str) and isinstance(endpoint, str):
            sort_keys.append((name.casefold(), endpoint))
    if len(set(sort_keys)) != len(sort_keys):
        errors.append(f"{array}: two rows share the same name and endpoint")
    if sort_keys != sorted(sort_keys):
        errors.append(f"{array}: rows must be in alphabetical order by name (case-insensitive), then endpoint")


def validate_directory(doc: object) -> list[str]:
    """Return every problem found in a parsed ``witnesses.json``; empty means valid."""
    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["directory: must be a JSON object"]
    unknown = sorted(set(doc) - set(TOP_LEVEL_FIELDS))
    if unknown:
        errors.append(f"directory: unknown field(s) {', '.join(unknown)}")
    if doc.get("directory_version") != DIRECTORY_VERSION:
        errors.append(f'directory_version: must be "{DIRECTORY_VERSION}"')
    key_owner: dict[str, str] = {}
    _check_rows("witnesses", doc.get("witnesses"), WITNESS_FIELDS, errors, key_owner)
    _check_rows("countersigners", doc.get("countersigners"), COUNTERSIGNER_FIELDS, errors, key_owner)
    return errors


def validate_file(path: str | Path) -> list[str]:
    """Parse ``path`` as JSON and validate it."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"{path}: cannot read as JSON ({exc})"]
    return validate_directory(doc)


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
