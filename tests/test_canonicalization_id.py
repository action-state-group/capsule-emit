# SPDX-License-Identifier: Apache-2.0
"""Tests for canonicalization_id presence in emitted capsules.

Every emitted capsule MUST carry ``compute_attestation.canonicalization_id``
naming the digest algorithm used to compute ``capsule_id``.  This is required
by mesh-llm #1332 and is a BREAKING change relative to format_version ≤ 2
records, which recorded no identifier.

Migration note (documented-not-recorded inference):
  Records with format_version ≤ '2' that carry no canonicalization_id are
  inferred to use the legacy repr-era convention (Python repr() /
  f"{x:.3f}" — neither of which is the jcs-n rule).  This mapping is an
  *inference*, not a recorded fact; old digests do not verify under any named
  algorithm, and existing ledgers are demo-grade.  Nothing of evidentiary weight
  depends on their digests.
"""
from __future__ import annotations

import os
import tempfile

from capsule_emit import CANONICALIZATION_ID, emit
from capsule_emit.gate import gate_and_emit
from capsule_emit.adapters.mcp import MCPCapsuleEmitter


# ---------------------------------------------------------------------------
# Presence in core emit()
# ---------------------------------------------------------------------------


def _tmp_ledger() -> str:
    return os.path.join(tempfile.mkdtemp(), "ledger.jsonl")


def test_canonicalization_id_present_with_agent_input() -> None:
    r = emit(
        "write_po",
        operator="acme",
        developer="agent@v1",
        agent_input={"vendor": "X", "total": "100.00"},
        anchor=False,
        ledger=_tmp_ledger(),
    )
    ca = r.capsule["model_attestation"]["compute_attestation"]
    assert "canonicalization_id" in ca, "compute_attestation must carry canonicalization_id"
    assert ca["canonicalization_id"] == CANONICALIZATION_ID


def test_canonicalization_id_present_without_agent_input() -> None:
    r = emit(
        "ping",
        operator="acme",
        developer="agent@v1",
        anchor=False,
        ledger=_tmp_ledger(),
    )
    ca = r.capsule["model_attestation"]["compute_attestation"]
    assert "canonicalization_id" in ca
    assert ca["canonicalization_id"] == CANONICALIZATION_ID


def test_canonicalization_id_present_with_model() -> None:
    r = emit(
        "write_po",
        operator="acme",
        developer="agent@v1",
        agent_input={"vendor": "X", "total": "100.00"},
        model={"provider": "anthropic", "model_id": "claude-sonnet-4-6"},
        anchor=False,
        ledger=_tmp_ledger(),
    )
    ca = r.capsule["model_attestation"]["compute_attestation"]
    assert "canonicalization_id" in ca
    assert ca["canonicalization_id"] == CANONICALIZATION_ID


def test_canonicalization_id_committed_to_capsule_id() -> None:
    """canonicalization_id is sealed into capsule_id — tampering must change the id."""
    r = emit(
        "action",
        operator="op",
        developer="dev",
        anchor=False,
        ledger=_tmp_ledger(),
    )
    capsule = r.capsule
    original_id = capsule["capsule_id"]

    # Tamper with canonicalization_id
    import copy
    tampered = copy.deepcopy(capsule)
    tampered["model_attestation"]["compute_attestation"]["canonicalization_id"] = "wrong-alg"

    # A tampered capsule must not verify with the original capsule_id.
    from agent_action_capsule import verify
    result = verify(tampered)
    assert not result.ok, (
        "Tampered canonicalization_id should fail verification; "
        "the field must be committed to capsule_id."
    )


# ---------------------------------------------------------------------------
# Presence via gate_and_emit path
# ---------------------------------------------------------------------------


def test_canonicalization_id_present_via_gate_and_emit() -> None:
    emitter = MCPCapsuleEmitter(
        operator="acme",
        developer="agent@v1",
        ledger=_tmp_ledger(),
        anchor=False,
    )
    gate_and_emit(
        action="write_po",
        constraints=[],
        inputs={"vendor": "X", "total": "100.00"},
        output={"status": "ok"},
        emitter=emitter,
    )
    assert emitter.last is not None
    ca = emitter.last.capsule["model_attestation"]["compute_attestation"]
    assert "canonicalization_id" in ca
    assert ca["canonicalization_id"] == CANONICALIZATION_ID


# ---------------------------------------------------------------------------
# Naming distinction from forwarded_copy.transforms
# ---------------------------------------------------------------------------


def test_canonicalization_id_name_distinct_from_transforms() -> None:
    """Field name must not be 'transforms' or anything in the forwarded_copy namespace."""
    r = emit("action", operator="op", developer="dev", anchor=False, ledger=_tmp_ledger())
    ca = r.capsule["model_attestation"]["compute_attestation"]
    assert "canonicalization_id" in ca
    # The content-transform list lives at forwarded_copy.transforms (mesh-sidecar).
    # Ensure the new field uses a distinct name and does not accidentally shadow it.
    assert "transforms" not in ca
    assert "forwarded_copy" not in ca
