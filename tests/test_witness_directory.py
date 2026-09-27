# SPDX-License-Identifier: Apache-2.0
"""``witnesses/witnesses.json`` -- the witness directory's shape and order.

The directory is alphabetical by operator and by nothing else; every row
names a binding this library speaks and only the two receipt grades."""
from __future__ import annotations

import json
from pathlib import Path

from capsule_emit import witness_bindings as wb

DIRECTORY = Path(__file__).resolve().parent.parent / "witnesses" / "witnesses.json"
REQUIRED = {
    "operator",
    "endpoint",
    "binding",
    "statement_types",
    "wire_forms",
    "grades_issued",
    "key_id",
    "public_key_pem",
    "since",
    "independent_of",
}


def _rows():
    return json.loads(DIRECTORY.read_text())["witnesses"]


def test_every_row_has_the_schema_fields():
    for row in _rows():
        assert REQUIRED <= set(row), REQUIRED - set(row)
        assert row["binding"] in wb.BINDINGS
        assert row["grades_issued"] and set(row["grades_issued"]) <= {"countersigned-observed", "mmr-verified"}
        assert row["endpoint"].startswith("https://")
        assert isinstance(row["independent_of"], list)


def test_rows_are_alphabetical_by_operator_and_unique():
    ops = [r["operator"] for r in _rows()]
    assert ops == sorted(ops, key=str.casefold)
    assert len(set(e["endpoint"] for e in _rows())) == len(ops)


def test_a_rekor_row_never_claims_mmr_verified():
    for row in _rows():
        if row["binding"] == wb.BINDING_REKOR:
            assert row["grades_issued"] == ["countersigned-observed"]


def test_listed_keys_match_the_keys_this_library_pins():
    """The default witness's row must carry the same key the library pins
    for it, so the directory and the verifier cannot drift apart."""
    from capsule_emit.checkpoint import DEFAULT_TS_PUBLIC_KEY_PEM, DEFAULT_TS_URL

    for row in _rows():
        if row["endpoint"] == DEFAULT_TS_URL:
            assert row["public_key_pem"].encode() == DEFAULT_TS_PUBLIC_KEY_PEM
