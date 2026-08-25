# SPDX-License-Identifier: Apache-2.0
# Copyright 2024 Action State Group
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""W8 interop tests: capsule-emit JSONL ledger ↔ capsule verify tooling.

**draft-04 reversal ([capsule-cose-sign1], 2026-08-24):** ``capsule_id`` is
signer-independent again — the producer's COSE_Sign1 envelope + ``key_id``
are capsule-emit-local bookkeeping, not neutral Capsule members (see
``capsule_emit.canonicalization``), so the raw, generic
``agent-action-capsule verify --store`` (which knows nothing about that
local envelope) no longer round-trips a capsule-emit ledger line as-is —
same reason it was never spec'd to understand ``kind``-tagged bookkeeping
entries either. capsule-emit ships its own ``capsule-emit verify --store``
CLI (``capsule_emit.cli``, backed by ``capsule_emit.signing
.verify_store_signed``) as the tool that IS aware of both: this file
exercises that one instead.
"""
from __future__ import annotations

import io
import json
import subprocess

import pytest
from agent_action_capsule.cli import _load_store

from capsule_emit import ledger_view, read_ledger, seal
from capsule_emit.signing import verify_store_signed as verify_store
from capsule_emit.verification import verify_capsule as verify

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_ledger(tmp_path):
    return tmp_path / "ledger.jsonl"


def _emit_n(n, tmp_ledger):
    """Emit n minimal capsules into tmp_ledger; return list of EmitResult."""
    return [
        seal(
            None,
            action="test_action",
            operator="test-org",
            developer="test-agent@v1",
            verdict="executed",
            anchor=False,
            ledger=tmp_ledger,
        )
        for _ in range(n)
    ]


# ---------------------------------------------------------------------------
# Group: _load_store reads JSONL
# ---------------------------------------------------------------------------

def test_load_store_single_capsule_jsonl(tmp_ledger):
    _emit_n(1, tmp_ledger)
    caps = _load_store(str(tmp_ledger))
    assert len(caps) == 1
    assert "capsule_id" in caps[0]


def test_load_store_two_capsule_jsonl(tmp_ledger):
    _emit_n(2, tmp_ledger)
    caps = _load_store(str(tmp_ledger))
    assert len(caps) == 2
    assert all("capsule_id" in c for c in caps)


def test_load_store_five_capsule_jsonl(tmp_ledger):
    _emit_n(5, tmp_ledger)
    caps = _load_store(str(tmp_ledger))
    assert len(caps) == 5


def test_load_store_jsonl_each_verifies(tmp_ledger):
    _emit_n(3, tmp_ledger)
    caps = _load_store(str(tmp_ledger))
    for cap in caps:
        result = verify(cap)
        assert result.ok, [f.detail for f in result.findings if f.severity == "error"]


def test_load_store_json_array_still_works(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    results = _emit_n(2, ledger)
    array_file = tmp_path / "store.json"
    array_file.write_text(json.dumps([r.capsule for r in results]), encoding="utf-8")
    caps = _load_store(str(array_file))
    assert len(caps) == 2
    assert all("capsule_id" in c for c in caps)


def test_load_store_blank_lines_ignored(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    _emit_n(2, ledger)
    # Rewrite with a blank line in the middle.
    lines = ledger.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    ledger.write_text(lines[0] + "\n\n" + lines[1] + "\n", encoding="utf-8")
    caps = _load_store(str(ledger))
    assert len(caps) == 2


# ---------------------------------------------------------------------------
# Group: round-trip JSONL ↔ array
# ---------------------------------------------------------------------------

def test_round_trip_jsonl_to_array(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    _emit_n(3, ledger)
    from_jsonl = _load_store(str(ledger))
    array_file = tmp_path / "array.json"
    array_file.write_text(json.dumps(from_jsonl), encoding="utf-8")
    from_array = _load_store(str(array_file))
    assert [c["capsule_id"] for c in from_jsonl] == [c["capsule_id"] for c in from_array]


def test_read_ledger_matches_load_store(tmp_ledger):
    _emit_n(4, tmp_ledger)
    via_read = read_ledger(tmp_ledger)
    via_load = _load_store(str(tmp_ledger))
    assert [c["capsule_id"] for c in via_read] == [c["capsule_id"] for c in via_load]


@pytest.mark.parametrize("n", [1, 3, 10])
def test_jsonl_line_count_matches_capsule_count(n, tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    _emit_n(n, ledger)
    non_blank = [ln for ln in ledger.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(non_blank) == n


# ---------------------------------------------------------------------------
# Group: ledger view vs verify agree
# ---------------------------------------------------------------------------

def test_ledger_view_and_verify_same_capsule_count(tmp_ledger):
    _emit_n(3, tmp_ledger)
    buf = io.StringIO()
    ledger_view(tmp_ledger, out=buf)
    assert "3 record(s)" in buf.getvalue()
    results = verify_store(read_ledger(tmp_ledger))
    assert len(results) == 3


def test_verify_store_all_ok_matches_ledger_view(tmp_ledger):
    _emit_n(2, tmp_ledger)
    results = verify_store(read_ledger(tmp_ledger))
    assert all(r.ok for r in results)
    buf = io.StringIO()
    ledger_view(tmp_ledger, out=buf)
    output = buf.getvalue().lower()
    assert "error" not in output
    assert "invalid" not in output


def test_tampered_capsule_in_ledger_fails_verify(tmp_ledger):
    _emit_n(2, tmp_ledger)
    # Append a tampered capsule: change operator but keep original capsule_id.
    tampered = {
        "spec_version": "0.4",
        "format_version": "1",
        "capsule_id": "a" * 64,
        "action_id": "tampered-action/00000000-0000-0000-0000-000000000000",
        "action_type": "decide",
        "operator": "evil-corp",
        "developer": "bad-agent@v0",
        "timestamp": "2024-01-01T00:00:00Z",
        "disposition": {
            "decision": "accept",
            "approver": "policy",
            "human_disposed": False,
            "verdict_class": "executed",
        },
    }
    with open(tmp_ledger, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(tampered) + "\n")
    results = verify_store(read_ledger(tmp_ledger))
    assert any(not r.ok for r in results)


# ---------------------------------------------------------------------------
# Group: CLI interop (subprocess)
# ---------------------------------------------------------------------------

def test_cli_verify_store_jsonl_exits_0(tmp_ledger):
    _emit_n(2, tmp_ledger)
    proc = subprocess.run(
        ["capsule-emit", "verify", "--store", str(tmp_ledger)],
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr.decode()


def test_cli_verify_store_single_capsule_exits_0(tmp_ledger):
    _emit_n(1, tmp_ledger)
    proc = subprocess.run(
        ["capsule-emit", "verify", "--store", str(tmp_ledger)],
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr.decode()


def test_cli_verify_store_tampered_exits_nonzero(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    results = _emit_n(1, ledger)
    # Rewrite the ledger with a modified operator field (capsule_id unchanged → mismatch).
    cap = dict(results[0].capsule)
    cap["operator"] = "evil-tamper"
    ledger.write_text(json.dumps(cap) + "\n", encoding="utf-8")
    proc = subprocess.run(
        ["capsule-emit", "verify", "--store", str(ledger)],
        capture_output=True,
    )
    assert proc.returncode != 0


def test_cli_verify_store_nonexistent_file_exits_nonzero(tmp_path):
    missing = tmp_path / "no_such_file.jsonl"
    proc = subprocess.run(
        ["capsule-emit", "verify", "--store", str(missing)],
        capture_output=True,
    )
    assert proc.returncode != 0


def test_cli_verify_store_empty_file_exits_nonzero_or_zero(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    proc = subprocess.run(
        ["capsule-emit", "verify", "--store", str(empty)],
        capture_output=True,
    )
    assert proc.returncode in {0, 1, 2}


# ---------------------------------------------------------------------------
# Group: chain-aware store verification
# ---------------------------------------------------------------------------

def test_verify_store_chain_parent_exists(tmp_ledger):
    cap_a = seal(
        None,
        action="action_a",
        operator="test-org",
        developer="agent@v1",
        verdict="executed",
        anchor=False,
        ledger=tmp_ledger,
    )
    seal(
        None,
        action="action_b",
        operator="test-org",
        developer="agent@v1",
        confirms=cap_a.capsule_id,
        verdict="confirmed",
        anchor=False,
        ledger=tmp_ledger,
    )
    capsules = read_ledger(tmp_ledger)
    results = verify_store(capsules)
    assert all(r.ok for r in results), [
        f.detail for r in results for f in r.findings if f.severity == "error"
    ]


def test_verify_store_chain_parent_missing(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    fake_parent_id = "b" * 64
    seal(
        None,
        action="action_b",
        operator="test-org",
        developer="agent@v1",
        confirms=fake_parent_id,
        verdict="confirmed",
        anchor=False,
        ledger=ledger,
    )
    capsules = read_ledger(ledger)
    results = verify_store(capsules)
    # The confirming capsule must not be ok: parent not in store.
    error_codes = [f.code for r in results for f in r.findings if f.severity == "error"]
    assert "chain_parent_missing" in error_codes


def test_ledger_view_shows_chain(tmp_ledger):
    cap_a = seal(
        None,
        action="action_a",
        operator="test-org",
        developer="agent@v1",
        verdict="executed",
        anchor=False,
        ledger=tmp_ledger,
    )
    seal(
        None,
        action="action_b",
        operator="test-org",
        developer="agent@v1",
        confirms=cap_a.capsule_id,
        verdict="confirmed",
        anchor=False,
        ledger=tmp_ledger,
    )
    buf = io.StringIO()
    ledger_view(tmp_ledger, out=buf)
    assert "confirms" in buf.getvalue()


# ---------------------------------------------------------------------------
# Group: neutral capsule_id recomputes cleanly WITHOUT the local envelope
# ---------------------------------------------------------------------------


def test_neutral_capsule_id_recomputes_after_stripping_local_fields(tmp_ledger):
    """The signer-independent capsule_id boundary, made explicit: strip
    capsule-emit's own local bookkeeping (``signature``/``key_id`` — the
    COSE_Sign1 producer envelope and its key, never neutral Capsule members)
    and the NEUTRAL ``agent_action_capsule.canonical.compute_capsule_id``
    recomputes the exact same id capsule-emit stored — proving capsule_id
    itself is genuinely signer-independent, even though the raw neutral
    tooling can't recompute it from a capsule-emit ledger LINE as-is (that
    line also carries the local envelope; see this module's docstring)."""
    from agent_action_capsule.canonical import compute_capsule_id as neutral_compute_capsule_id

    cap = _emit_n(1, tmp_ledger)[0].capsule
    neutral_capsule = {k: v for k, v in cap.items() if k not in ("signature", "key_id")}
    assert neutral_compute_capsule_id(neutral_capsule) == cap["capsule_id"]
