# SPDX-License-Identifier: Apache-2.0
"""witnesses.json: the committed directory validates, and the validator
refuses each malformed shape it is meant to refuse (every negative case below
is a one-field mutation of a valid document)."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from capsule_emit.witness_directory import (
    COUNTERSIGNER_FIELDS,
    OPTIONAL_TOP_LEVEL_FIELDS,
    OPTIONAL_WITNESS_FIELDS,
    TOP_LEVEL_FIELDS,
    WITNESS_FIELDS,
    main,
    row_for,
    row_public_keys_pem,
    validate_directory,
    validate_file,
)

ROOT = Path(__file__).resolve().parent.parent
DIRECTORY = ROOT / "witnesses.json"
SCHEMA = ROOT / "docs" / "schemas" / "witnesses.schema.json"

KEY_A = "11" * 32
KEY_B = "22" * 32
KEY_C = "33" * 32
KEY_D = "77" * 32
_EC_DER = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
    Encoding.DER, PublicFormat.SubjectPublicKeyInfo
)


def _valid() -> dict:
    return {
        "directory_version": "1",
        "witnesses": [
            {"name": "Alpha Witness", "endpoint": "https://w.alpha.example", "key_ids": [KEY_A], "since": "2026-01-02"},
            {
                "name": "beta log",
                "endpoint": "https://log.beta.example",
                "binding": "rekor",
                "key_ids": [hashlib.sha256(_EC_DER).hexdigest()],
                "public_keys": [base64.b64encode(_EC_DER).decode()],
                "since": "2026-03-04",
            },
            {"name": "Gamma Ltd", "endpoint": "https://ts.gamma.example/v1", "binding": "scrapi", "key_ids": [KEY_B], "since": "2026-05-06"},
        ],
        "countersigners": [
            {
                "name": "beta co",
                "endpoint": "https://cs.beta.example",
                "key_ids": [KEY_C],
                "statement_types_issued": ["countersign/v1"],
                "since": "2026-03-04",
                "independent_of": [],
            },
            {
                "name": "Gamma Ltd",
                "endpoint": "https://cs.gamma.example/v1",
                "key_ids": [KEY_D],
                "statement_types_issued": ["countersign/v1"],
                "since": "2026-05-06",
                "independent_of": ["beta co"],
            },
        ],
    }


def test_committed_directory_validates():
    """Fails while our countersigner row still carries placeholders: the key
    and the start date exist only once the instance is live. That failure is
    the merge gate for the row, on purpose; scripts/fill_countersigner_row.py
    fills both."""
    assert validate_file(DIRECTORY) == []


def test_committed_directory_has_only_the_known_top_level_fields():
    doc = json.loads(DIRECTORY.read_text(encoding="utf-8"))
    assert set(doc) == {"directory_version", "witnesses", "countersigners"}


def test_valid_document_passes():
    assert validate_directory(_valid()) == []


def test_schema_file_matches_validator_fields():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert tuple(schema["required"]) == TOP_LEVEL_FIELDS
    item = schema["properties"]["witnesses"]["items"]
    assert tuple(item["required"]) == WITNESS_FIELDS
    assert set(item["properties"]) == set(WITNESS_FIELDS) | set(OPTIONAL_WITNESS_FIELDS)
    assert set(item["properties"]["binding"]["enum"]) == {"cll", "rekor", "scrapi"}
    assert tuple(schema["properties"]) == TOP_LEVEL_FIELDS + OPTIONAL_TOP_LEVEL_FIELDS
    cs_item = schema["properties"]["countersigners"]["items"]
    assert tuple(cs_item["required"]) == COUNTERSIGNER_FIELDS
    assert set(cs_item["properties"]) == set(COUNTERSIGNER_FIELDS)


def test_countersigner_rows_use_the_field_names_the_go_verifier_reads():
    # capsule-cli's countersignerDirectory decodes {"countersigners": [...]}
    # with these json tags; a renamed field would silently read as empty there.
    assert COUNTERSIGNER_FIELDS == (
        "name", "endpoint", "key_ids", "statement_types_issued", "since", "independent_of",
    )


def test_committed_directory_is_alphabetical():
    doc = json.loads(DIRECTORY.read_text(encoding="utf-8"))
    for array in ("witnesses", "countersigners"):
        names = [row["name"].casefold() for row in doc[array]]
        assert names == sorted(names)


def test_out_of_order_rows_fail():
    doc = _valid()
    doc["witnesses"].reverse()
    assert any("alphabetical" in e for e in validate_directory(doc))


def test_order_is_case_insensitive():
    # "beta log" sorts before "Gamma Ltd" although "G" < "b" in code points.
    assert validate_directory(_valid()) == []


def test_same_name_rows_order_by_endpoint():
    doc = _valid()
    second = copy.deepcopy(doc["witnesses"][0])
    second["endpoint"] = "https://a.alpha.example"
    second["key_ids"] = ["44" * 32]
    doc["witnesses"].insert(1, second)
    assert any("alphabetical" in e for e in validate_directory(doc))
    doc["witnesses"][0], doc["witnesses"][1] = doc["witnesses"][1], doc["witnesses"][0]
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
def test_key_id_must_be_64_lowercase_hex(key):
    doc = _valid()
    doc["witnesses"][0]["key_ids"] = [key]
    assert any("64 lowercase hex" in e for e in validate_directory(doc))


def test_placeholders_are_reported_as_placeholders():
    doc = _valid()
    doc["witnesses"][0]["key_ids"] = ["PLACEHOLDER-key"]
    doc["witnesses"][0]["since"] = "PLACEHOLDER-date"
    errors = validate_directory(doc)
    assert any("key_ids[0]: placeholder" in e for e in errors)
    assert any("since: placeholder" in e for e in errors)


def test_empty_key_ids_fail():
    doc = _valid()
    doc["witnesses"][0]["key_ids"] = []
    assert any("key_ids: must not be empty" in e for e in validate_directory(doc))


def test_a_key_resolves_to_at_most_one_row():
    doc = _valid()
    doc["witnesses"][2]["key_ids"] = [KEY_A]
    assert any("already listed" in e for e in validate_directory(doc))


def test_rotated_keys_are_listed_together_in_one_row():
    doc = _valid()
    doc["witnesses"][0]["key_ids"] = [KEY_A, "44" * 32]
    assert validate_directory(doc) == []


@pytest.mark.parametrize("field", WITNESS_FIELDS)
def test_missing_field_fails(field):
    doc = _valid()
    del doc["witnesses"][0][field]
    assert any("missing field" in e and field in e for e in validate_directory(doc))


@pytest.mark.parametrize("field", ["score", "rating", "tier", "compliant", "statement_types_issued", "independent_of"])
def test_unknown_field_fails(field):
    doc = _valid()
    doc["witnesses"][0][field] = "x"
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
        "http://w.alpha.example",
        "https://user:pw@w.alpha.example",
        "https://w.alpha.example?x=1",
        "https://w.alpha.example#frag",
        "w.alpha.example",
    ],
)
def test_endpoint_must_be_a_plain_https_url(endpoint):
    doc = _valid()
    doc["witnesses"][0]["endpoint"] = endpoint
    assert any(".endpoint:" in e for e in validate_directory(doc))


@pytest.mark.parametrize("since", ["2026-13-01", "2026-9-1", "yesterday", 20260101])
def test_since_must_be_a_calendar_date(since):
    doc = _valid()
    doc["witnesses"][0]["since"] = since
    assert any(".since:" in e for e in validate_directory(doc))


# -- binding and public_keys --------------------------------------------------


def test_binding_is_optional_and_restricted():
    doc = _valid()
    assert "binding" not in doc["witnesses"][0]
    doc["witnesses"][0]["binding"] = "grpc"
    assert any(".binding:" in e for e in validate_directory(doc))


def test_public_key_must_hash_to_a_key_id_of_its_row():
    doc = _valid()
    doc["witnesses"][1]["key_ids"] = ["55" * 32]
    assert any("not one of this row's key_ids" in e for e in validate_directory(doc))


def test_public_key_must_be_der():
    doc = _valid()
    doc["witnesses"][1]["public_keys"] = [base64.b64encode(b"not a key").decode()]
    assert any("base64 DER" in e for e in validate_directory(doc))


def test_a_rekor_row_without_public_keys_is_refused():
    """Any 32 bytes load as an Ed25519 key -- a Rekor log ID included -- so
    decoding cannot catch this; the binding must."""
    doc = _valid()
    del doc["witnesses"][1]["public_keys"]
    assert any("a rekor row's key id is a log ID" in e for e in validate_directory(doc))


def test_every_rekor_key_id_needs_its_public_key():
    doc = _valid()
    doc["witnesses"][1]["key_ids"].append("66" * 32)  # a rotated log ID with no key listed
    assert any("key_ids[1]: a rekor row's key id is a log ID" in e for e in validate_directory(doc))


def test_a_rekor_key_id_is_never_read_as_an_ed25519_key():
    row = copy.deepcopy(_valid()["witnesses"][1])
    del row["public_keys"]
    with pytest.raises(ValueError, match="no public key for log ID"):
        row_public_keys_pem(row)


def test_a_cll_row_may_list_raw_ed25519_keys_without_public_keys():
    doc = _valid()
    assert "public_keys" not in doc["witnesses"][0] and validate_directory(doc) == []


def test_row_keys_read_ed25519_and_other_keys_the_same_way():
    ed_row, ec_row = _valid()["witnesses"][0], _valid()["witnesses"][1]
    assert row_public_keys_pem(ed_row)[0].startswith(b"-----BEGIN PUBLIC KEY-----")
    assert row_public_keys_pem(ec_row)[0].startswith(b"-----BEGIN PUBLIC KEY-----")


def test_row_for_matches_binding_and_endpoint():
    doc = _valid()
    assert row_for(doc, "https://w.alpha.example/")["name"] == "Alpha Witness"
    assert row_for(doc, "rekor+https://log.beta.example")["name"] == "beta log"
    assert row_for(doc, "https://log.beta.example") is None  # listed as rekor, not cll
    assert row_for(doc, "scrapi+https://ts.gamma.example/v1")["name"] == "Gamma Ltd"


def test_cli_exit_codes(tmp_path, capsys):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_valid()), encoding="utf-8")
    assert main([str(good)]) == 0
    bad_doc = _valid()
    bad_doc["witnesses"].reverse()
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(bad_doc), encoding="utf-8")
    assert main([str(bad)]) == 1
    assert "alphabetical" in capsys.readouterr().out


def test_unreadable_file_is_reported(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    assert any("cannot read as JSON" in e for e in validate_file(path))


# -- countersigners ------------------------------------------------------------


def test_countersigners_array_is_optional():
    doc = _valid()
    del doc["countersigners"]
    assert validate_directory(doc) == []


def test_countersigners_must_be_a_list():
    doc = _valid()
    doc["countersigners"] = {}
    assert any("countersigners: must be a list" in e for e in validate_directory(doc))


def test_out_of_order_countersigner_rows_fail():
    doc = _valid()
    doc["countersigners"].reverse()
    assert any("countersigners: rows must be in alphabetical order" in e for e in validate_directory(doc))


def test_countersigner_placeholders_are_reported_as_placeholders():
    doc = _valid()
    doc["countersigners"][0]["key_ids"] = ["PLACEHOLDER-key"]
    doc["countersigners"][0]["since"] = "PLACEHOLDER-date"
    errors = validate_directory(doc)
    assert any("countersigners[0].key_ids[0]: placeholder" in e for e in errors)
    assert any("countersigners[0].since: placeholder" in e for e in errors)


@pytest.mark.parametrize("key", ["11" * 31, ("ab" * 32).upper(), "19a9ab3e02fad55c", "zz" * 32])
def test_countersigner_key_id_must_be_64_lowercase_hex(key):
    doc = _valid()
    doc["countersigners"][0]["key_ids"] = [key]
    assert any("countersigners[0].key_ids[0]: must be 64 lowercase hex" in e for e in validate_directory(doc))


def test_a_countersigner_key_resolves_to_at_most_one_row():
    doc = _valid()
    doc["countersigners"][1]["key_ids"] = [KEY_C]
    assert any("already listed at countersigners[0]" in e for e in validate_directory(doc))


def test_a_witness_key_is_not_reused_as_a_countersigner_key():
    doc = _valid()
    doc["countersigners"][0]["key_ids"] = [KEY_A]
    assert any("already listed at witnesses[0]" in e for e in validate_directory(doc))


@pytest.mark.parametrize("field", COUNTERSIGNER_FIELDS)
def test_countersigner_missing_field_fails(field):
    doc = _valid()
    del doc["countersigners"][0][field]
    assert any("missing field" in e and field in e for e in validate_directory(doc))


@pytest.mark.parametrize("field", ["score", "rating", "tier", "compliant", "binding", "public_keys"])
def test_countersigner_unknown_field_fails(field):
    doc = _valid()
    doc["countersigners"][0][field] = "x"
    assert any("countersigners[0]: unknown field" in e for e in validate_directory(doc))


def test_countersigner_endpoint_and_since_are_checked():
    doc = _valid()
    doc["countersigners"][0]["endpoint"] = "http://cs.beta.example"
    doc["countersigners"][1]["since"] = "2026-13-01"
    errors = validate_directory(doc)
    assert any("countersigners[0].endpoint:" in e for e in errors)
    assert any("countersigners[1].since:" in e for e in errors)


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
