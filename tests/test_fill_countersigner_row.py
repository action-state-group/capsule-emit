# SPDX-License-Identifier: Apache-2.0
"""scripts/fill_countersigner_row.py: fills only the countersigner row's two
placeholders, from a fixture authority-pubkey response (no network), refuses
a second fill and a bad key, and leaves a directory that validates."""
from __future__ import annotations

import datetime
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from capsule_emit.witness_directory import validate_file

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "fill_countersigner_row.py"
COMMITTED = ROOT / "witnesses.json"
ENDPOINT = "https://countersign.actionstate.ai"

_spec = importlib.util.spec_from_file_location("fill_countersigner_row", SCRIPT)
fill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fill)

# A fixed fixture key: the Ed25519 key for the all-0x07 seed.
PUBKEY_HEX = (
    Ed25519PrivateKey.from_private_bytes(b"\x07" * 32)
    .public_key()
    .public_bytes(Encoding.Raw, PublicFormat.Raw)
    .hex()
)
SHORT_KEY_ID = hashlib.sha256(bytes.fromhex(PUBKEY_HEX)).hexdigest()[:16]
SINCE = "2026-09-30"

# The same layout as the committed file while its row is unfilled.
FIXTURE = {
    "directory_version": "1",
    "witnesses": [
        {
            "name": "witness.example",
            "endpoint": "https://witness.example",
            "binding": "cll",
            "key_ids": ["39bb654c9dc0afe1c0edef0deffaa69099b8518836c9ba26e0491535840f96b5"],
            "since": "2026-06-28",
        }
    ],
    "countersigners": [
        {
            "name": "Action State Group",
            "endpoint": ENDPOINT,
            "key_ids": ["PLACEHOLDER-countersign-instance-key-not-yet-generated"],
            "statement_types_issued": ["countersign/v1"],
            "since": "PLACEHOLDER-date-of-first-live-countersignature",
            "independent_of": [],
        }
    ],
}


def _response(pubkey_hex: str = PUBKEY_HEX, key_id: str | None = SHORT_KEY_ID) -> str:
    # The exact body capsule-anchor's GET /anchor/authority-pubkey returns.
    body = {"pubkey_hex": pubkey_hex}
    if key_id is not None:
        body["key_id"] = key_id
    return json.dumps(body)


@pytest.fixture()
def directory(tmp_path) -> Path:
    path = tmp_path / "witnesses.json"
    path.write_text(json.dumps(FIXTURE, indent=2) + "\n", encoding="utf-8")
    return path


@pytest.fixture()
def pubkey_file(tmp_path) -> Path:
    path = tmp_path / "authority-pubkey.json"
    path.write_text(_response(), encoding="utf-8")
    return path


def _run(directory: Path, pubkey_file: Path, *extra: str) -> int:
    return fill.main(["--since", SINCE, "--pubkey-file", str(pubkey_file), "--directory", str(directory), *extra])


def test_fills_exactly_the_two_fields_and_validates(directory, pubkey_file):
    before = directory.read_text(encoding="utf-8")
    assert validate_file(directory) != []  # the placeholders are the only problems
    assert _run(directory, pubkey_file) == 0
    after = directory.read_text(encoding="utf-8")
    assert validate_file(directory) == []
    doc = json.loads(after)
    row = doc["countersigners"][0]
    assert row["key_ids"] == [PUBKEY_HEX]
    assert row["since"] == SINCE
    # Every other byte is unchanged: only the two placeholder lines differ.
    changed = [(a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
    assert len(before.splitlines()) == len(after.splitlines())
    assert [b.strip() for _, b in changed] == [f'"{PUBKEY_HEX}"', f'"since": "{SINCE}",']


def test_the_directory_lists_the_full_key_not_the_short_key_id(directory, pubkey_file):
    assert _run(directory, pubkey_file) == 0
    assert SHORT_KEY_ID not in json.loads(directory.read_text())["countersigners"][0]["key_ids"]


def test_refuses_a_second_fill(directory, pubkey_file, capsys):
    assert _run(directory, pubkey_file) == 0
    filled = directory.read_text(encoding="utf-8")
    assert _run(directory, pubkey_file) == 1
    assert "already filled" in capsys.readouterr().err
    assert directory.read_text(encoding="utf-8") == filled


def test_refuses_a_half_filled_row(directory, pubkey_file, capsys):
    doc = json.loads(directory.read_text())
    doc["countersigners"][0]["since"] = "2026-09-01"
    directory.write_text(json.dumps(doc, indent=2) + "\n")
    assert _run(directory, pubkey_file) == 1
    assert "half filled" in capsys.readouterr().err


def test_dry_run_writes_nothing(directory, pubkey_file, capsys):
    before = directory.read_text(encoding="utf-8")
    assert _run(directory, pubkey_file, "--dry-run") == 0
    assert directory.read_text(encoding="utf-8") == before
    out = capsys.readouterr().out
    assert f'+        "{PUBKEY_HEX}"' in out and "nothing written" in out


@pytest.mark.parametrize(
    "body",
    [
        _response(PUBKEY_HEX[:-2], key_id=None),  # 31 bytes
        _response(PUBKEY_HEX.upper(), key_id=None),  # not lowercase
        _response("zz" * 32, key_id=None),  # not hex
        _response(SHORT_KEY_ID, key_id=None),  # the short id, not the key
        _response(PUBKEY_HEX, key_id="0" * 16),  # key_id does not match the key
        _response(PUBKEY_HEX, key_id=SHORT_KEY_ID.upper()),
        json.dumps({"key_id": SHORT_KEY_ID}),  # no pubkey_hex
        "{not json",
    ],
)
def test_rejects_a_bad_key(directory, tmp_path, body, capsys):
    before = directory.read_text(encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text(body, encoding="utf-8")
    assert _run(directory, bad) == 1
    assert "ERROR" in capsys.readouterr().err
    assert directory.read_text(encoding="utf-8") == before


def test_accepts_a_bare_hex_key(directory, tmp_path):
    bare = tmp_path / "key.txt"
    bare.write_text(PUBKEY_HEX + "\n", encoding="utf-8")
    assert _run(directory, bare) == 0
    assert validate_file(directory) == []


def test_refuses_a_key_already_listed_elsewhere(directory, tmp_path, capsys):
    witness_key = FIXTURE["witnesses"][0]["key_ids"][0]
    reused = tmp_path / "reused.json"
    reused.write_text(_response(witness_key, key_id=None), encoding="utf-8")
    before = directory.read_text(encoding="utf-8")
    assert _run(directory, reused) == 1
    assert "already listed at witnesses[0]" in capsys.readouterr().err
    assert directory.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("since", ["2026-13-01", "2026-9-1", "today"])
def test_rejects_a_bad_since(directory, pubkey_file, since):
    assert fill.main(["--since", since, "--pubkey-file", str(pubkey_file), "--directory", str(directory)]) == 1


def test_rejects_a_future_since():
    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    with pytest.raises(fill.FillError, match="in the future"):
        fill.check_since(tomorrow.isoformat())


def test_unknown_endpoint_is_refused(directory, pubkey_file, capsys):
    assert _run(directory, pubkey_file, "--endpoint", "https://other.example") == 1
    assert "found 0" in capsys.readouterr().err


def test_mark_ready_is_printed_not_run_by_default(directory, pubkey_file, capsys, monkeypatch):
    calls = []
    monkeypatch.setattr(fill.subprocess, "run", lambda *a, **k: calls.append(a))
    assert _run(directory, pubkey_file) == 0
    assert calls == []
    assert "gh pr ready 226 --repo action-state-group/capsule-emit" in capsys.readouterr().out


def test_fills_the_committed_directory_while_its_placeholders_remain(tmp_path, pubkey_file):
    """Run against a copy of the real witnesses.json, so the go-sheet command
    is known to work on the file it will touch. Skips once the row is filled."""
    text = COMMITTED.read_text(encoding="utf-8")
    if "PLACEHOLDER" not in text:
        pytest.skip("the committed countersigner row is already filled")
    copy = tmp_path / "witnesses.json"
    copy.write_text(text, encoding="utf-8")
    assert _run(copy, pubkey_file) == 0
    assert validate_file(copy) == []
    after = copy.read_text(encoding="utf-8")
    assert after == text.replace(
        '"PLACEHOLDER-countersign-instance-key-not-yet-generated"', f'"{PUBKEY_HEX}"'
    ).replace('"PLACEHOLDER-date-of-first-live-countersignature"', f'"{SINCE}"')
