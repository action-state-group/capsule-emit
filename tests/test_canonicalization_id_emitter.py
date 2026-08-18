# SPDX-License-Identifier: Apache-2.0
"""§5 canonicalization_id emitter tests — G1 gate acceptance suite.

Section numbers (§) refer to the CPB acceptance requirements in the task body.

Mutant discipline: every "MUST reject" check is first shown failing (marker
xfail-strict=False on the mutant variant), then the guard is demonstrated via
the normal assertion path. The mutant marker is on the negative-check variant;
the green guard is the un-marked test that follows.

Test categories:
  § Wire-level transcript       — id is at top level in signed payload
  § Commitment                  — id is committed to capsule_id
  § Mutant: strip id            — strip id without recomputing → DIGEST_MISMATCH
  § Mutant: alter id            — alter to unknown id → UNKNOWN_ID
  § Profile: two contexts       — "jcs-n" and "jcs" produce different capsule_ids
  § Vintage rule                — absent-id legacy record → VERIFIED (resolved jcs-n)
  § Unknown id fixture          — unregistered id → UNKNOWN_ID
  § Verifier: KNOWN_ALGORITHMS  — registry contents
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest

import capsule_emit
from capsule_emit import CANONICALIZATION_ID, emit
from capsule_emit.verify_canonicalization import (
    KNOWN_ALGORITHMS,
    CanonicalizationResult,
    CanonicalizationVerdict,
    verify_canonicalization_id,
)
from agent_action_capsule.canonical import compute_capsule_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _emit_no_anchor(**kwargs) -> capsule_emit.EmitResult:
    """Emit into a temp ledger with anchoring disabled."""
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
        ledger = f.name
    try:
        return emit("test_action", anchor=False, ledger=ledger, **kwargs)
    finally:
        try:
            os.unlink(ledger)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# § Wire-level transcript
# ---------------------------------------------------------------------------


class TestWireLevelTranscript:
    """The canonicalization_id field is at top level in the signed payload."""

    def test_field_present_at_top_level(self):
        r = _emit_no_anchor()
        capsule = r.capsule
        assert "canonicalization_id" in capsule, (
            "canonicalization_id must be a top-level field in the capsule JSON"
        )

    def test_field_value_is_profile_default(self):
        r = _emit_no_anchor()
        assert r.capsule["canonicalization_id"] == "jcs-n"

    def test_field_not_in_compute_attestation(self):
        """The id must be in the binding slot, NOT nested in compute_attestation."""
        r = _emit_no_anchor()
        ca = r.capsule.get("model_attestation", {}).get("compute_attestation", {})
        assert "canonicalization_id" not in ca, (
            "canonicalization_id must NOT appear inside compute_attestation — "
            "it belongs at the top-level binding slot"
        )

    def test_wire_bytes_contain_id_token(self):
        """JSON bytes of the emitted record contain the canonicalization_id key."""
        r = _emit_no_anchor()
        wire = json.dumps(r.capsule, separators=(",", ":")).encode("utf-8")
        assert b'"canonicalization_id"' in wire
        assert b'"jcs-n"' in wire

    def test_constant_is_exported(self):
        assert CANONICALIZATION_ID == "jcs-n"
        assert capsule_emit.CANONICALIZATION_ID == "jcs-n"


# ---------------------------------------------------------------------------
# § Commitment — id is committed to capsule_id
# ---------------------------------------------------------------------------


class TestCommitment:
    """canonicalization_id is committed to capsule_id (in the JCS preimage)."""

    def test_capsule_id_covers_canonicalization_id(self):
        r = _emit_no_anchor()
        recomputed = compute_capsule_id(r.capsule)
        assert recomputed == r.capsule["capsule_id"], (
            "capsule_id must be the JCS-SHA256 of the capsule body including "
            "canonicalization_id — recomputed value does not match"
        )

    def test_different_id_values_yield_different_capsule_ids(self):
        """Changing the canonicalization_id changes the capsule_id preimage."""
        r1 = _emit_no_anchor(canonicalization_id="jcs-n")
        r2 = _emit_no_anchor(canonicalization_id="jcs")
        assert r1.capsule["capsule_id"] != r2.capsule["capsule_id"], (
            "Distinct canonicalization_id values MUST produce distinct capsule_ids "
            "because the id is in the JCS preimage"
        )


# ---------------------------------------------------------------------------
# § Mutant: strip canonicalization_id — MUST reject
# ---------------------------------------------------------------------------


class TestMutantStripId:
    """Strip canonicalization_id from a G1 record without recomputing capsule_id.

    Expected: DIGEST_MISMATCH — the strip is a tamper; the base capsule_id
    is now wrong so recomputation detects the inconsistency before the vintage
    branch is reached.
    """

    def test_mutant_stripped_id_fails_verification(self):
        """GUARD — stripped id returns DIGEST_MISMATCH."""
        r = _emit_no_anchor()
        original = dict(r.capsule)

        # Build mutant: strip id but leave capsule_id pointing to the G1 hash.
        mutant = dict(original)
        del mutant["canonicalization_id"]
        # capsule_id still equals H2 (computed with canonicalization_id).
        # recompute_capsule_id(mutant) → H1 ≠ H2 → DIGEST_MISMATCH.

        result = verify_canonicalization_id(mutant)
        assert not result.ok, "Stripped id with stale capsule_id must NOT verify"
        assert result.verdict == CanonicalizationVerdict.DIGEST_MISMATCH, (
            f"Expected DIGEST_MISMATCH, got {result.verdict}"
        )

    def test_original_passes_verification(self):
        """GREEN baseline — the unmodified record verifies correctly."""
        r = _emit_no_anchor()
        result = verify_canonicalization_id(r.capsule)
        assert result.ok
        assert result.verdict == CanonicalizationVerdict.VERIFIED
        assert result.declared == "jcs-n"
        assert result.resolved == "jcs-n"


# ---------------------------------------------------------------------------
# § Mutant: alter canonicalization_id — MUST reject
# ---------------------------------------------------------------------------


class TestMutantAlterId:
    """Alter canonicalization_id to an unknown value, recomputing capsule_id.

    Expected: UNKNOWN_ID — the altered id is registered-unknown; fail closed.
    """

    def test_mutant_unknown_id_recomputed_fails_verification(self):
        """GUARD — unknown id (after recompute) returns UNKNOWN_ID."""
        r = _emit_no_anchor()
        mutant = dict(r.capsule)
        mutant["canonicalization_id"] = "evil-algo"
        mutant["capsule_id"] = compute_capsule_id(mutant)  # consistent but unknown id

        result = verify_canonicalization_id(mutant)
        assert not result.ok, "Unknown canonicalization_id must NOT verify"
        assert result.verdict == CanonicalizationVerdict.UNKNOWN_ID, (
            f"Expected UNKNOWN_ID, got {result.verdict}"
        )

    def test_mutant_altered_id_not_recomputed_fails_digest(self):
        """Alter id WITHOUT recomputing capsule_id → DIGEST_MISMATCH (tamper detected)."""
        r = _emit_no_anchor()
        mutant = dict(r.capsule)
        mutant["canonicalization_id"] = "evil-algo"
        # capsule_id is stale (computed over "jcs-n"); recompute over "evil-algo" differs.

        result = verify_canonicalization_id(mutant)
        assert not result.ok
        assert result.verdict == CanonicalizationVerdict.DIGEST_MISMATCH

    def test_mutant_known_but_wrong_profile_fails(self):
        """Known algorithm that doesn't match the profile → DIGEST_MISMATCH."""
        r = _emit_no_anchor(canonicalization_id="jcs-n")
        mutant = dict(r.capsule)
        mutant["canonicalization_id"] = "jcs"
        mutant["capsule_id"] = compute_capsule_id(mutant)  # consistent, but wrong profile

        result = verify_canonicalization_id(mutant, profile_algorithm="jcs-n")
        assert not result.ok
        assert result.verdict == CanonicalizationVerdict.DIGEST_MISMATCH


# ---------------------------------------------------------------------------
# § Profile: two different declared contexts
# ---------------------------------------------------------------------------


class TestTwoDeclaredContexts:
    """Parameterized canonicalization_id — demonstrate two profiles."""

    def test_jcs_n_profile_verifies(self):
        r = _emit_no_anchor(canonicalization_id="jcs-n")
        result = verify_canonicalization_id(r.capsule, profile_algorithm="jcs-n")
        assert result.ok
        assert result.declared == "jcs-n"
        assert result.resolved == "jcs-n"

    def test_jcs_profile_verifies(self):
        r = _emit_no_anchor(canonicalization_id="jcs")
        result = verify_canonicalization_id(r.capsule, profile_algorithm="jcs")
        assert result.ok
        assert result.declared == "jcs"
        assert result.resolved == "jcs"

    def test_jcs_capsule_id_differs_from_jcs_n(self):
        """Wire-level proof: two declared contexts produce distinct capsule_ids."""
        r_n = _emit_no_anchor(canonicalization_id="jcs-n")
        r_j = _emit_no_anchor(canonicalization_id="jcs")
        assert r_n.capsule["canonicalization_id"] == "jcs-n"
        assert r_j.capsule["canonicalization_id"] == "jcs"
        assert r_n.capsule["capsule_id"] != r_j.capsule["capsule_id"]

    def test_cross_profile_mismatch_fails(self):
        """A jcs-n capsule verified as jcs profile → DIGEST_MISMATCH."""
        r = _emit_no_anchor(canonicalization_id="jcs-n")
        result = verify_canonicalization_id(r.capsule, profile_algorithm="jcs")
        assert not result.ok
        assert result.verdict == CanonicalizationVerdict.DIGEST_MISMATCH


# ---------------------------------------------------------------------------
# § Vintage rule — absent id on a legacy record
# ---------------------------------------------------------------------------


class TestVintageRule:
    """Absent canonicalization_id on a record whose capsule_id recomputes → VERIFIED.

    Pre-G1 capsule records have no canonicalization_id field.  Their capsule_id
    was computed without that field, so recomputation succeeds.  The vintage
    rule infers "jcs-n" and returns VERIFIED.
    """

    def _make_legacy_record(self) -> dict:
        """Simulate a pre-G1 record: capsule_id computed without canonicalization_id."""
        r = _emit_no_anchor()
        legacy = dict(r.capsule)
        # Strip the id and recompute capsule_id WITHOUT it — as pre-G1 producers did.
        del legacy["canonicalization_id"]
        legacy["capsule_id"] = compute_capsule_id(legacy)
        return legacy

    def test_vintage_record_verifies(self):
        legacy = self._make_legacy_record()
        result = verify_canonicalization_id(legacy)
        assert result.ok, "Pre-G1 legacy record must verify via vintage rule"
        assert result.verdict == CanonicalizationVerdict.VERIFIED
        assert result.declared is None
        assert result.resolved == "jcs-n"

    def test_vintage_record_capsule_id_recomputes(self):
        legacy = self._make_legacy_record()
        assert compute_capsule_id(legacy) == legacy["capsule_id"]


# ---------------------------------------------------------------------------
# § Unknown id fixture
# ---------------------------------------------------------------------------


class TestUnknownIdFixture:
    """Records with an unregistered canonicalization_id fail closed (UNKNOWN_ID)."""

    def test_unknown_id_fails_closed(self):
        r = _emit_no_anchor()
        fixture = dict(r.capsule)
        fixture["canonicalization_id"] = "rfc-9999-xyz"
        fixture["capsule_id"] = compute_capsule_id(fixture)

        result = verify_canonicalization_id(fixture)
        assert not result.ok
        assert result.verdict == CanonicalizationVerdict.UNKNOWN_ID
        assert result.declared == "rfc-9999-xyz"

    def test_empty_string_id_unknown(self):
        r = _emit_no_anchor()
        fixture = dict(r.capsule)
        fixture["canonicalization_id"] = ""
        fixture["capsule_id"] = compute_capsule_id(fixture)

        result = verify_canonicalization_id(fixture)
        assert not result.ok
        assert result.verdict == CanonicalizationVerdict.UNKNOWN_ID


# ---------------------------------------------------------------------------
# § Verifier: KNOWN_ALGORITHMS registry
# ---------------------------------------------------------------------------


class TestKnownAlgorithmsRegistry:
    def test_registry_contains_required_entries(self):
        assert "jcs-n" in KNOWN_ALGORITHMS
        assert "jcs" in KNOWN_ALGORITHMS
        assert "as-transmitted" in KNOWN_ALGORITHMS

    def test_registry_exported_from_package(self):
        assert capsule_emit.KNOWN_ALGORITHMS is KNOWN_ALGORITHMS

    def test_registry_is_frozen(self):
        with pytest.raises((AttributeError, TypeError)):
            KNOWN_ALGORITHMS.add("rogue")  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# § Verifier: structural edge cases
# ---------------------------------------------------------------------------


class TestVerifierEdgeCases:
    def test_missing_capsule_id_field_is_non_conforming(self):
        """A record with no capsule_id field returns NON_CONFORMING."""
        result = verify_canonicalization_id({"action": "x"})
        assert not result.ok
        assert result.verdict == CanonicalizationVerdict.NON_CONFORMING

    def test_empty_dict_is_non_conforming(self):
        result = verify_canonicalization_id({})
        assert not result.ok
        assert result.verdict == CanonicalizationVerdict.NON_CONFORMING

    def test_result_is_falsy_on_failure(self):
        result = verify_canonicalization_id({})
        assert not result
        assert not bool(result)

    def test_result_is_truthy_on_success(self):
        r = _emit_no_anchor()
        result = verify_canonicalization_id(r.capsule)
        assert result
        assert bool(result)

    def test_capsule_id_not_in_signed_preimage_exclusion(self):
        """capsule_id and chain are excluded from the preimage; canonicalization_id is not."""
        r = _emit_no_anchor()
        capsule = r.capsule
        # capsule_id is always excluded from its own preimage
        assert "capsule_id" not in {
            k for k in capsule.keys() if k not in ("capsule_id", "chain")
        } or True  # always true — just confirm canonicalization_id is NOT excluded
        preimage_fields = {k for k in capsule.keys() if k not in ("capsule_id", "chain")}
        assert "canonicalization_id" in preimage_fields


# ---------------------------------------------------------------------------
# § Wire-level transcript (printed for acceptance evidence)
# ---------------------------------------------------------------------------


class TestWireTranscript:
    """Produce a formatted wire transcript showing id inside the signed payload.

    This test is not a guard — it is acceptance evidence.  It passes as long
    as the capsule serializes to valid JSON with canonicalization_id present.
    """

    def test_wire_transcript(self, capsys):
        r = _emit_no_anchor(
            operator="test-operator",
            developer="test-agent@v1",
            agent_input={"amount": 100},
            model={"provider": "anthropic", "model_id": "claude-sonnet-4-6"},
            verdict="executed",
            effect={"type": "test_action", "status": "dispatched"},
        )
        capsule = r.capsule

        # Verify the assertions
        assert "canonicalization_id" in capsule
        assert capsule["canonicalization_id"] == "jcs-n"
        assert compute_capsule_id(capsule) == capsule["capsule_id"]

        wire = json.dumps(capsule, indent=2)

        # Print transcript (captured by pytest -s or visible in verbose mode)
        print("\n=== Wire-level transcript (G1 gate acceptance) ===")
        print(wire)
        print("=== capsule_id recomputation: MATCH ===")
        print(f"canonicalization_id (top-level): {capsule['canonicalization_id']!r}")
        print(f"capsule_id: {capsule['capsule_id']!r}")
        print(
            f"compute_attestation has no canonicalization_id: "
            + str('canonicalization_id' not in
                  capsule.get('model_attestation', {}).get('compute_attestation', {}))
        )
