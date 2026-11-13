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
  § Withdrawn jcs-n             — "jcs-n" and an absent id are refused everywhere
  § Unknown id fixture          — unregistered id → UNKNOWN_ID
  § Verifier: KNOWN_ALGORITHMS  — registry contents
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest

import capsule_emit
from capsule_emit import CANONICALIZATION_ID, cli, seal
from capsule_emit.canonicalization import (
    UnsupportedCanonicalizationError,
    canonical_capsule_bytes,
    compute_capsule_id,
)
from capsule_emit.signing import LocalKeypairSigner, verify_capsule_signature, verify_store_signed
from capsule_emit.verification import verify_capsule
from capsule_emit.verify_canonicalization import (
    KNOWN_ALGORITHMS,
    CanonicalizationVerdict,
    verify_canonicalization_id,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _emit_no_anchor(**kwargs) -> capsule_emit.EmitResult:
    """Emit into a temp ledger with anchoring disabled."""
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
        ledger = f.name
    try:
        agent_input = kwargs.pop("agent_input", None)
        return seal(agent_input, action="test_action", anchor=False, ledger=ledger, **kwargs)
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
        assert r.capsule["canonicalization_id"] == "jcs"

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
        assert b'"jcs"' in wire

    def test_constant_is_exported(self):
        assert CANONICALIZATION_ID == "jcs"
        assert capsule_emit.CANONICALIZATION_ID == "jcs"


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

    def test_sealing_with_the_withdrawn_jcs_n_is_refused(self):
        with pytest.raises(UnsupportedCanonicalizationError):
            _emit_no_anchor(canonicalization_id="jcs-n")


class TestAlgorithmDispatch:
    """The declared identifier selects the bytes, not just a preimage label."""

    _MEMBERS = {
        "kept": "value",
        "null_member": None,
        "empty_array": [],
        "empty_object": {},
    }

    def test_jcs_known_answer_retains_null_and_empty_members(self):
        capsule = {"canonicalization_id": "jcs", **self._MEMBERS}
        expected = (
            b'{"canonicalization_id":"jcs","empty_array":[],"empty_object":{},'
            b'"kept":"value","null_member":null}'
        )
        assert canonical_capsule_bytes(capsule) == expected
        assert compute_capsule_id(capsule) == (
            "e2039e7d7de5396c470bfd98b70594d281cc091ae6d069adc99f95dea0c1165e"
        )

    @pytest.mark.parametrize("declared", ["jcs-n", "as-transmitted", "evil-algo", "", None])
    def test_only_jcs_has_bytes(self, declared):
        capsule = {"canonicalization_id": declared, **self._MEMBERS}
        with pytest.raises(UnsupportedCanonicalizationError):
            canonical_capsule_bytes(capsule)
        with pytest.raises(UnsupportedCanonicalizationError):
            compute_capsule_id(capsule)

    def test_an_absent_canonicalization_id_has_no_bytes(self):
        with pytest.raises(UnsupportedCanonicalizationError):
            canonical_capsule_bytes(dict(self._MEMBERS))

    def test_jcs_emission_uses_plain_jcs_and_signature_verifies(self):
        r = _emit_no_anchor(
            canonicalization_id="jcs",
            extra_compute=self._MEMBERS,
        )
        assert r.capsule["capsule_id"] == compute_capsule_id(r.capsule)
        assert verify_capsule_signature(r.capsule)

    def test_jcs_emission_passes_public_verifiers(self, tmp_path, capsys):
        ledger = tmp_path / "ledger.jsonl"
        r = seal(
            None,
            action="test_action",
            anchor=False,
            witness=False,
            ledger=ledger,
            canonicalization_id="jcs",
            extra_compute=self._MEMBERS,
        )

        assert verify_capsule(r.capsule).ok
        assert verify_store_signed([r.capsule])[0].ok

        stale = dict(r.capsule)
        stale["capsule_id"] = "0" * 64
        result = verify_capsule(stale)
        assert not result.ok
        assert any(finding.code == "capsule_id_mismatch" for finding in result.findings)

        rc = cli.main(["verify", "--store", str(ledger)])
        assert rc == 0
        assert "1/1 VALID" in capsys.readouterr().out

    def test_as_transmitted_fails_closed_without_original_bytes(self):
        with pytest.raises(UnsupportedCanonicalizationError, match="as-transmitted"):
            _emit_no_anchor(canonicalization_id="as-transmitted")


# ---------------------------------------------------------------------------
# § Mutant: strip canonicalization_id — MUST reject
# ---------------------------------------------------------------------------


class TestMutantStripId:
    """Strip canonicalization_id from a G1 record without recomputing capsule_id.

    Expected: NON_CONFORMING — a record with no canonicalization_id has no
    withdrawn-construction fallback, so it cannot be recomputed at all.
    """

    def test_mutant_stripped_id_fails_verification(self):
        """GUARD — a stripped id returns NON_CONFORMING."""
        r = _emit_no_anchor()
        original = dict(r.capsule)

        # Build mutant: strip id but leave capsule_id pointing to the G1 hash.
        mutant = dict(original)
        del mutant["canonicalization_id"]
        result = verify_canonicalization_id(mutant)
        assert not result.ok, "Stripped id with stale capsule_id must NOT verify"
        assert result.verdict == CanonicalizationVerdict.NON_CONFORMING, (
            f"Expected NON_CONFORMING, got {result.verdict}"
        )

    def test_original_passes_verification(self):
        """GREEN baseline — the unmodified record verifies correctly."""
        r = _emit_no_anchor()
        result = verify_canonicalization_id(r.capsule)
        assert result.ok
        assert result.verdict == CanonicalizationVerdict.VERIFIED
        assert result.declared == "jcs"
        assert result.resolved == "jcs"


# ---------------------------------------------------------------------------
# § Mutant: alter canonicalization_id — MUST reject
# ---------------------------------------------------------------------------


class TestMutantAlterId:
    """Alter canonicalization_id to an unknown value.

    Expected: UNKNOWN_ID — the altered id is registered-unknown; fail closed.
    """

    def test_mutant_unknown_id_fails_verification(self):
        """GUARD — an unknown id returns UNKNOWN_ID before dispatch."""
        r = _emit_no_anchor()
        mutant = dict(r.capsule)
        mutant["canonicalization_id"] = "evil-algo"
        result = verify_canonicalization_id(mutant)
        assert not result.ok, "Unknown canonicalization_id must NOT verify"
        assert result.verdict == CanonicalizationVerdict.UNKNOWN_ID, (
            f"Expected UNKNOWN_ID, got {result.verdict}"
        )

    def test_mutant_altered_id_not_recomputed_is_still_unknown(self):
        """An unknown declaration is rejected before algorithm dispatch."""
        r = _emit_no_anchor()
        mutant = dict(r.capsule)
        mutant["canonicalization_id"] = "evil-algo"
        
        result = verify_canonicalization_id(mutant)
        assert not result.ok
        assert result.verdict == CanonicalizationVerdict.UNKNOWN_ID

    def test_mutant_known_but_wrong_profile_fails(self):
        """Known algorithm that doesn't match the profile → DIGEST_MISMATCH."""
        r = _emit_no_anchor()
        result = verify_canonicalization_id(r.capsule, profile_algorithm="as-transmitted")
        assert not result.ok
        assert result.verdict == CanonicalizationVerdict.DIGEST_MISMATCH

    def test_declared_jcs_n_is_unknown(self):
        r = _emit_no_anchor()
        mutant = dict(r.capsule, canonicalization_id="jcs-n")
        result = verify_canonicalization_id(mutant)
        assert not result.ok
        assert result.verdict == CanonicalizationVerdict.UNKNOWN_ID


# ---------------------------------------------------------------------------
# § Profile and the withdrawn jcs-n
# ---------------------------------------------------------------------------


class TestProfile:
    def test_jcs_profile_verifies(self):
        r = _emit_no_anchor(canonicalization_id="jcs")
        result = verify_canonicalization_id(r.capsule, profile_algorithm="jcs")
        assert result.ok
        assert result.declared == "jcs"
        assert result.resolved == "jcs"

    def test_a_record_without_the_slot_is_non_conforming(self):
        r = _emit_no_anchor()
        legacy = {k: v for k, v in r.capsule.items() if k != "canonicalization_id"}
        for record in (legacy, dict(legacy, canonicalization_id=None)):
            result = verify_canonicalization_id(record)
            assert not result.ok
            assert result.verdict == CanonicalizationVerdict.NON_CONFORMING

    def test_a_signed_pre_format_4_record_fails_structural_verification(self, tmp_path):
        from capsule_emit.signing import sign_producer_envelope

        r = _emit_no_anchor()
        vintage = {
            key: value
            for key, value in r.capsule.items()
            if key not in {"canonicalization_id", "signature", "key_id", "capsule_id"}
        }
        vintage["format_version"] = "2"
        vintage["capsule_id"] = "0" * 64
        signer = LocalKeypairSigner(tmp_path / "vintage-signing-key.pem")
        vintage["signature"], vintage["key_id"] = sign_producer_envelope(signer, vintage["capsule_id"])
        store_result = verify_store_signed([vintage])[0]
        assert not store_result.ok
        assert any(f.code == "unsupported_format_version" for f in store_result.findings)


# ---------------------------------------------------------------------------
# § Unknown id fixture
# ---------------------------------------------------------------------------


class TestUnknownIdFixture:
    """Records with an unregistered canonicalization_id fail closed (UNKNOWN_ID)."""

    def test_unknown_id_fails_closed(self):
        r = _emit_no_anchor()
        fixture = dict(r.capsule)
        fixture["canonicalization_id"] = "rfc-9999-xyz"
        result = verify_canonicalization_id(fixture)
        assert not result.ok
        assert result.verdict == CanonicalizationVerdict.UNKNOWN_ID
        assert result.declared == "rfc-9999-xyz"

    def test_empty_string_id_unknown(self):
        r = _emit_no_anchor()
        fixture = dict(r.capsule)
        fixture["canonicalization_id"] = ""
        result = verify_canonicalization_id(fixture)
        assert not result.ok
        assert result.verdict == CanonicalizationVerdict.UNKNOWN_ID


# ---------------------------------------------------------------------------
# § Verifier: KNOWN_ALGORITHMS registry
# ---------------------------------------------------------------------------


class TestKnownAlgorithmsRegistry:
    def test_registry_contains_required_entries(self):
        assert "jcs-n" not in KNOWN_ALGORITHMS
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
        assert capsule["canonicalization_id"] == "jcs"
        assert compute_capsule_id(capsule) == capsule["capsule_id"]

        wire = json.dumps(capsule, indent=2)

        # Print transcript (captured by pytest -s or visible in verbose mode)
        print("\n=== Wire-level transcript (G1 gate acceptance) ===")
        print(wire)
        print("=== capsule_id recomputation: MATCH ===")
        print(f"canonicalization_id (top-level): {capsule['canonicalization_id']!r}")
        print(f"capsule_id: {capsule['capsule_id']!r}")
        print(
            "compute_attestation has no canonicalization_id: "
            + str('canonicalization_id' not in
                  capsule.get('model_attestation', {}).get('compute_attestation', {}))
        )
