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
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from capsule_emit.witness_directory import (
    OPTIONAL_WITNESS_FIELDS,
    TOP_LEVEL_FIELDS,
    WITNESS_FIELDS,
    main,
    row_for,
    row_public_keys_pem,
    validate_directory,
    validate_file,
)

ROOT = Path(__file__).resolve().parents[2]
DIRECTORY = ROOT / "witnesses.json"
SCHEMA = ROOT / "docs" / "schemas" / "witnesses.schema.json"

KEY_A = "11" * 32
KEY_B = "22" * 32
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
    }


def test_committed_directory_validates():
    assert validate_file(DIRECTORY) == []


def test_committed_directory_has_only_the_two_top_level_fields():
    doc = json.loads(DIRECTORY.read_text(encoding="utf-8"))
    assert set(doc) == {"directory_version", "witnesses"}


def test_valid_document_passes():
    assert validate_directory(_valid()) == []


def test_schema_file_matches_validator_fields():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert tuple(schema["required"]) == TOP_LEVEL_FIELDS
    item = schema["properties"]["witnesses"]["items"]
    assert tuple(item["required"]) == WITNESS_FIELDS
    assert set(item["properties"]) == set(WITNESS_FIELDS) | set(OPTIONAL_WITNESS_FIELDS)
    assert set(item["properties"]["binding"]["enum"]) == {"cll", "rekor", "scrapi"}


def test_committed_directory_is_alphabetical():
    doc = json.loads(DIRECTORY.read_text(encoding="utf-8"))
    names = [row["name"].casefold() for row in doc["witnesses"]]
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
    assert any("matches none of this row's key_ids" in e for e in validate_directory(doc))


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


def _ed25519_key() -> tuple[str, str, bytes]:
    """A fresh Ed25519 key: its raw hex (a key id), its base64 DER, its PEM."""
    public = ed25519.Ed25519PrivateKey.generate().public_key()
    raw = public.public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    der = public.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    return raw, base64.b64encode(der).decode(), public.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)


def test_an_ed25519_row_may_publish_its_key_in_public_keys():
    raw, b64, pem = _ed25519_key()
    doc = _valid()
    doc["witnesses"][0]["key_ids"] = [raw]
    doc["witnesses"][0]["public_keys"] = [b64]
    assert validate_directory(doc) == []
    assert row_public_keys_pem(doc["witnesses"][0]) == [pem]


def test_an_ed25519_public_key_must_be_the_key_its_key_id_names():
    raw, _, _ = _ed25519_key()
    _, other, _ = _ed25519_key()
    doc = _valid()
    doc["witnesses"][0]["key_ids"] = [raw]
    doc["witnesses"][0]["public_keys"] = [other]
    assert any("witnesses[0].public_keys[0]: matches none of this row's key_ids" in e for e in validate_directory(doc))


def test_an_ed25519_public_key_does_not_cover_a_rekor_log_id():
    """A rekor key id is a log ID: only a key that hashes to it covers it."""
    raw, b64, _ = _ed25519_key()
    doc = _valid()
    doc["witnesses"][1]["key_ids"] = [raw]
    doc["witnesses"][1]["public_keys"] = [b64]
    assert any("a rekor row's key id is a log ID" in e for e in validate_directory(doc))


def test_committed_ed25519_public_keys_are_the_keys_their_key_ids_name():
    """Reading a row with or without its public_keys gives the same keys."""
    rows = json.loads(DIRECTORY.read_text(encoding="utf-8"))["witnesses"]
    published = [r for r in rows if r.get("binding", "cll") != "rekor" and r.get("public_keys")]
    assert published, "the directory lists at least one Ed25519 public key"
    for row in published:
        bare = {k: v for k, v in row.items() if k != "public_keys"}
        assert row_public_keys_pem(row) == row_public_keys_pem(bare)


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
