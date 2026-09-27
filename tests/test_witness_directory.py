# SPDX-License-Identifier: Apache-2.0
"""witnesses.json: the committed directory validates, and the validator
refuses each malformed shape it is meant to refuse (every negative case below
is a one-field mutation of a valid document)."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from capsule_emit.witness_directory import (
    COUNTERSIGNER_FIELDS,
    TOP_LEVEL_FIELDS,
    WITNESS_FIELDS,
    main,
    validate_directory,
    validate_file,
)

ROOT = Path(__file__).resolve().parent.parent
DIRECTORY = ROOT / "witnesses.json"
SCHEMA = ROOT / "docs" / "schemas" / "witnesses.schema.json"

KEY_A = "11" * 32
KEY_B = "22" * 32
KEY_C = "33" * 32


def _valid() -> dict:
    return {
        "directory_version": "1",
        "witnesses": [
            {"name": "Alpha Witness", "endpoint": "https://w.alpha.example", "key_ids": [KEY_A], "since": "2026-01-02"},
        ],
        "countersigners": [
            {
                "name": "beta co",
                "endpoint": "https://cs.beta.example",
                "key_ids": [KEY_B],
                "statement_types_issued": ["countersign/v1"],
                "since": "2026-03-04",
                "independent_of": [],
            },
            {
                "name": "Gamma Ltd",
                "endpoint": "https://cs.gamma.example/v1",
                "key_ids": [KEY_C],
                "statement_types_issued": ["countersign/v1"],
                "since": "2026-05-06",
                "independent_of": ["beta co"],
            },
        ],
    }


def test_committed_directory_validates():
    """Fails while our countersigner row still carries placeholders: the key
    and the start date exist only once the instance is live. That failure is
    the merge gate for the row, on purpose."""
    assert validate_file(DIRECTORY) == []


def test_valid_document_passes():
    assert validate_directory(_valid()) == []


def test_rows_use_the_field_names_the_go_verifier_reads():
    # capsule-cli's countersignerDirectory decodes {"countersigners": [...]}
    # with these json tags; a renamed field would silently read as empty there.
    assert COUNTERSIGNER_FIELDS == (
        "name", "endpoint", "key_ids", "statement_types_issued", "since", "independent_of",
    )


def test_schema_file_matches_validator_fields():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert tuple(schema["required"]) == TOP_LEVEL_FIELDS
    props = schema["properties"]
    assert tuple(props["witnesses"]["items"]["required"]) == WITNESS_FIELDS
    assert tuple(props["countersigners"]["items"]["required"]) == COUNTERSIGNER_FIELDS
    assert set(props["witnesses"]["items"]["properties"]) == set(WITNESS_FIELDS)
    assert set(props["countersigners"]["items"]["properties"]) == set(COUNTERSIGNER_FIELDS)


def test_committed_directory_is_alphabetical():
    doc = json.loads(DIRECTORY.read_text(encoding="utf-8"))
    for array in ("witnesses", "countersigners"):
        names = [row["name"].casefold() for row in doc[array]]
        assert names == sorted(names), array


def test_out_of_order_rows_fail():
    doc = _valid()
    doc["countersigners"].reverse()
    assert any("alphabetical" in e for e in validate_directory(doc))


def test_order_is_case_insensitive():
    # "beta co" sorts before "Gamma Ltd" although "G" < "b" in code points.
    doc = _valid()
    assert validate_directory(doc) == []


def test_same_name_rows_order_by_endpoint():
    doc = _valid()
    second = copy.deepcopy(doc["countersigners"][0])
    second["endpoint"] = "https://a.beta.example"
    second["key_ids"] = ["44" * 32]
    doc["countersigners"].insert(1, second)
    assert any("alphabetical" in e for e in validate_directory(doc))
    doc["countersigners"][0], doc["countersigners"][1] = doc["countersigners"][1], doc["countersigners"][0]
    assert validate_directory(doc) == []


@pytest.mark.parametrize(
    "key",
    [
        "11" * 31,  # 31 bytes
        ("ab" * 32).upper(),
        "19a9ab3e02fad55c",  # a truncated key identifier, not the key
        "zz" * 32,
    ],
)
def test_key_id_must_be_full_lowercase_ed25519_hex(key):
    doc = _valid()
    doc["countersigners"][0]["key_ids"] = [key]
    assert any("64 lowercase hex" in e for e in validate_directory(doc))


def test_placeholders_are_reported_as_placeholders():
    doc = _valid()
    doc["countersigners"][0]["key_ids"] = ["PLACEHOLDER-key"]
    doc["countersigners"][0]["since"] = "PLACEHOLDER-date"
    errors = validate_directory(doc)
    assert any("key_ids[0]: placeholder" in e for e in errors)
    assert any("since: placeholder" in e for e in errors)


def test_empty_key_ids_fail():
    doc = _valid()
    doc["witnesses"][0]["key_ids"] = []
    assert any("key_ids: must not be empty" in e for e in validate_directory(doc))


def test_a_key_resolves_to_at_most_one_row():
    doc = _valid()
    doc["countersigners"][1]["key_ids"] = [KEY_B]
    assert any("already listed" in e for e in validate_directory(doc))


def test_a_witness_key_is_not_reused_as_a_countersigner_key():
    doc = _valid()
    doc["countersigners"][0]["key_ids"] = [KEY_A]
    assert any("already listed at witnesses[0]" in e for e in validate_directory(doc))


def test_rotated_keys_are_listed_together_in_one_row():
    doc = _valid()
    doc["countersigners"][0]["key_ids"] = [KEY_B, "44" * 32]
    assert validate_directory(doc) == []


@pytest.mark.parametrize("array,field", [("witnesses", f) for f in WITNESS_FIELDS]
                         + [("countersigners", f) for f in COUNTERSIGNER_FIELDS])
def test_missing_field_fails(array, field):
    doc = _valid()
    del doc[array][0][field]
    assert any("missing field" in e and field in e for e in validate_directory(doc))


@pytest.mark.parametrize("field", ["score", "rating", "tier", "compliant"])
def test_unknown_field_fails(field):
    doc = _valid()
    doc["countersigners"][0][field] = "x"
    assert any("unknown field" in e for e in validate_directory(doc))


def test_witness_rows_do_not_carry_countersigner_fields():
    doc = _valid()
    doc["witnesses"][0]["statement_types_issued"] = ["countersign/v1"]
    assert any("unknown field" in e for e in validate_directory(doc))


def test_unknown_top_level_field_fails():
    doc = _valid()
    doc["featured"] = "Alpha Witness"
    assert any("directory: unknown field" in e for e in validate_directory(doc))


def test_wrong_directory_version_fails():
    doc = _valid()
    doc["directory_version"] = "2"
    assert any("directory_version" in e for e in validate_directory(doc))


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://cs.beta.example",
        "https://user:pw@cs.beta.example",
        "https://cs.beta.example?x=1",
        "https://cs.beta.example#frag",
        "cs.beta.example",
    ],
)
def test_endpoint_must_be_a_plain_https_url(endpoint):
    doc = _valid()
    doc["countersigners"][0]["endpoint"] = endpoint
    assert any(".endpoint:" in e for e in validate_directory(doc))


@pytest.mark.parametrize("since", ["2026-13-01", "2026-9-1", "yesterday", 20260101])
def test_since_must_be_a_calendar_date(since):
    doc = _valid()
    doc["countersigners"][0]["since"] = since
    assert any(".since:" in e for e in validate_directory(doc))


def test_statement_types_must_not_be_empty():
    doc = _valid()
    doc["countersigners"][0]["statement_types_issued"] = []
    assert any("statement_types_issued: must not be empty" in e for e in validate_directory(doc))


def test_a_row_cannot_declare_independence_of_itself():
    doc = _valid()
    doc["countersigners"][0]["independent_of"] = ["beta co"]
    assert any("independence of itself" in e for e in validate_directory(doc))


def test_independent_of_has_no_duplicates():
    doc = _valid()
    doc["countersigners"][1]["independent_of"] = ["beta co", "beta co"]
    assert any("independent_of: duplicate" in e for e in validate_directory(doc))


def test_empty_independent_of_is_valid():
    # Declaring nothing is allowed. Independence of a given countersignature
    # is computed per entry by the verifier (signer key vs producer key), so a
    # row's declaration never makes a self-countersignature independent.
    doc = _valid()
    doc["countersigners"][1]["independent_of"] = []
    assert validate_directory(doc) == []


def test_cli_exit_codes(tmp_path, capsys):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_valid()), encoding="utf-8")
    assert main([str(good)]) == 0
    bad_doc = _valid()
    bad_doc["countersigners"].reverse()
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(bad_doc), encoding="utf-8")
    assert main([str(bad)]) == 1
    assert "alphabetical" in capsys.readouterr().out


def test_unreadable_file_is_reported(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    assert any("cannot read as JSON" in e for e in validate_file(path))
