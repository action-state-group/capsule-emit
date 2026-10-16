# SPDX-License-Identifier: Apache-2.0
"""Tests for §5.3(bis) provenance_mode — a MODE on the ordinary Capsule, not a
distinct record type (draft -05, "Provenance mode and backfilled records").

Acceptance criteria:
- Round-trip: emit with provenance_mode=ProvenanceMode(mode="backfilled", ...)
  -> verify -> ok, block present in sealed dict.
- Absent provenance_mode still verifies ok (implies "contemporaneous").
- mode="backfilled" REQUIRES source_ref/source_asserted_at/import_batch/
  imported_at; missing any is a structural failure (check 9), both at
  ProvenanceMode construction (InvariantError) and at verify() over raw bytes.
- provenance_mode fields other than mode are meaningful only when
  mode="backfilled" (InvariantError at construction; verify() finding over
  raw bytes).
- time_rung="witnessed" REQUIRES a references[] entry citing
  citation_purpose="corroborates_source_time"; unsupported claim is an
  error-severity overclaim (check 9), unlike the informational treatment
  check 7 gives attestation_mode/ledger_mode/cross_party_rung.
- imported_at == source_asserted_at on a backfilled record is the laundering
  shape and is an error-severity finding (check 9), regardless of time_rung.
- derived.provenance_mode / derived.provenance_time_rung are always reported
  when the block is present, independent of the producer's own claims.
- chain.relation="duplicates" (store-level): duplicate_collapsed info finding;
  duplicate_parent_not_contemporaneous info finding when the parent is itself
  backfilled.
- capsule_id commits provenance_mode (tamper = mismatch).
"""
import pytest
from agent_action_capsule import verify, verify_store
from agent_action_capsule.contracts import (
    PROVENANCE_MODES,
    TIME_RUNG_RANK,
    TIME_RUNGS,
    InvariantError,
    ProvenanceMode,
    ReferenceEntry,
)
from agent_action_capsule.parse import parse_capsule

from capsule_emit.builder import build_capsule as emit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE = dict(
    action_id="act-provenance-mode-01",
    action_type="fyi",
    operator="test-org",
    developer="test-agent@1.0",
)

_SOURCE_REF = ReferenceEntry(type="x-external-ledger-entry", digest_alg="SHA-256", digest="5" * 64)


def _backfilled(**overrides) -> ProvenanceMode:
    kwargs = dict(
        mode="backfilled",
        source_ref=_SOURCE_REF,
        source_asserted_at="2019-01-01T00:00:00Z",
        import_batch="batch-1",
        imported_at="2026-09-22T00:00:00Z",
    )
    kwargs.update(overrides)
    return ProvenanceMode(**kwargs)


# ---------------------------------------------------------------------------
# Contract constants
# ---------------------------------------------------------------------------


def test_provenance_modes_frozen():
    assert PROVENANCE_MODES == frozenset({"contemporaneous", "backfilled"})


def test_time_rungs_frozen():
    assert TIME_RUNGS == frozenset({"self_attested", "witnessed"})


def test_time_rung_rank_order():
    assert TIME_RUNG_RANK["witnessed"] > TIME_RUNG_RANK["self_attested"]


# ---------------------------------------------------------------------------
# emit() round-trips
# ---------------------------------------------------------------------------


def test_emit_backfilled():
    c = emit(**_BASE, provenance_mode=_backfilled())
    assert c["provenance_mode"]["mode"] == "backfilled"
    assert c["provenance_mode"]["source_ref"]["digest"] == "5" * 64
    r = verify(c)
    assert r.ok
    assert r.assurance["provenance_mode"] == "backfilled"
    assert r.assurance["provenance_time_rung"] == "self_attested"


def test_emit_without_provenance_mode_implies_contemporaneous():
    c = emit(**_BASE)
    assert "provenance_mode" not in c
    r = verify(c)
    assert r.ok
    assert "provenance_mode" not in r.assurance


def test_emit_explicit_contemporaneous():
    c = emit(**_BASE, provenance_mode=ProvenanceMode(mode="contemporaneous"))
    assert c["provenance_mode"] == {"mode": "contemporaneous"}
    r = verify(c)
    assert r.ok
    assert r.assurance["provenance_mode"] == "contemporaneous"
    assert "provenance_time_rung" not in r.assurance


# ---------------------------------------------------------------------------
# capsule_id tamper-evidence
# ---------------------------------------------------------------------------


def test_provenance_mode_committed_to_capsule_id():
    c = emit(**_BASE, provenance_mode=_backfilled())
    tampered = dict(c)
    tampered["provenance_mode"] = dict(tampered["provenance_mode"])
    tampered["provenance_mode"]["import_batch"] = "tampered-batch"
    r = verify(tampered)
    assert not r.ok
    assert any(f.code == "capsule_id_mismatch" for f in r.findings)


# ---------------------------------------------------------------------------
# ProvenanceMode dataclass invariants (producer/typed path)
# ---------------------------------------------------------------------------


def test_bad_mode_rejected():
    with pytest.raises(InvariantError, match="provenance_mode.mode"):
        ProvenanceMode(mode="retroactive")


def test_backfilled_missing_source_ref_rejected():
    with pytest.raises(InvariantError, match="REQUIRES"):
        ProvenanceMode(
            mode="backfilled", source_asserted_at="2019-01-01T00:00:00Z",
            import_batch="b1", imported_at="2026-09-22T00:00:00Z",
        )


def test_backfilled_missing_all_fields_rejected():
    with pytest.raises(InvariantError, match="REQUIRES"):
        ProvenanceMode(mode="backfilled")


def test_contemporaneous_with_orphan_field_rejected():
    with pytest.raises(InvariantError, match="meaningful only when"):
        ProvenanceMode(mode="contemporaneous", import_batch="orphan")


def test_contemporaneous_with_orphan_time_rung_rejected():
    with pytest.raises(InvariantError, match="meaningful only when"):
        ProvenanceMode(mode="contemporaneous", time_rung="witnessed")


def test_bad_time_rung_rejected():
    with pytest.raises(InvariantError, match="time_rung"):
        _backfilled(time_rung="verified")


def test_well_formed_backfilled_with_time_rung_witnessed_constructs():
    pm = _backfilled(time_rung="witnessed")
    assert pm.time_rung == "witnessed"


# ---------------------------------------------------------------------------
# verify() over raw bytes — required-field / structural findings (check 9)
# ---------------------------------------------------------------------------


def _sealed_with_pm(pm_dict):
    c = emit(**_BASE)
    c = dict(c)
    c["provenance_mode"] = pm_dict
    # verify() recomputes capsule_id independently of what's carried, so a
    # hand-crafted provenance_mode dict that never went through the typed
    # constructor is exactly the "arbitrary bytes" case check 9 must handle.
    return c


def test_verify_backfilled_missing_required_fields():
    c = _sealed_with_pm({"mode": "backfilled"})
    r = verify(c)
    codes = [f.code for f in r.findings if f.check == 9]
    assert codes.count("provenance_mode_missing_required_field") == 4
    assert all(f.severity == "error" for f in r.findings if f.code == "provenance_mode_missing_required_field")


def test_verify_backfilled_malformed_source_ref():
    c = _sealed_with_pm({
        "mode": "backfilled", "source_ref": {"type": "x"},  # missing digest_alg/digest
        "source_asserted_at": "2019-01-01T00:00:00Z", "import_batch": "b1",
        "imported_at": "2026-09-22T00:00:00Z",
    })
    r = verify(c)
    assert not r.ok
    assert any(f.code == "provenance_mode_source_ref_malformed" for f in r.findings)


def test_verify_unknown_mode_is_structural_error():
    c = _sealed_with_pm({"mode": "retroactive"})
    r = verify(c)
    assert not r.ok
    assert any(f.code == "provenance_mode_invalid" and f.severity == "error" for f in r.findings)
    assert "provenance_mode" not in r.assurance


def test_verify_contemporaneous_orphan_field_is_error():
    c = _sealed_with_pm({"mode": "contemporaneous", "import_batch": "orphan"})
    r = verify(c)
    assert not r.ok
    assert any(f.code == "provenance_mode_invalid" for f in r.findings)


# ---------------------------------------------------------------------------
# verify() — laundering shape and time_rung overclaim (check 9, gating)
# ---------------------------------------------------------------------------


def test_laundering_shape_is_gating_error():
    pm = _backfilled(source_asserted_at="2026-09-22T00:00:00Z", imported_at="2026-09-22T00:00:00Z")
    c = emit(**_BASE, provenance_mode=pm)
    r = verify(c)
    assert not r.ok
    f = next(f for f in r.findings if f.code == "provenance_time_laundering_shape")
    assert f.severity == "error"
    assert f.check == 9


def test_close_but_unequal_timestamps_are_not_laundering():
    pm = _backfilled(source_asserted_at="2026-09-22T00:00:00Z", imported_at="2026-09-22T00:00:01Z")
    c = emit(**_BASE, provenance_mode=pm)
    r = verify(c)
    assert r.ok
    assert not any(f.code == "provenance_time_laundering_shape" for f in r.findings)


def test_time_rung_witnessed_without_reference_is_overclaim():
    pm = _backfilled(time_rung="witnessed")
    c = emit(**_BASE, provenance_mode=pm)
    r = verify(c)
    assert not r.ok
    f = next(f for f in r.findings if f.code == "provenance_time_rung_overclaim")
    assert f.severity == "error"
    assert f.check == 9
    # the rederived cap, never the claim
    assert r.assurance["provenance_time_rung"] == "self_attested"


def test_time_rung_witnessed_with_well_formed_reference_verifies_clean():
    pm = _backfilled(time_rung="witnessed")
    witness_ref = ReferenceEntry(
        type="x-witnessed-timestamp", digest_alg="SHA-256", digest="7" * 64,
        citation_purpose="corroborates_source_time",
    )
    c = emit(**_BASE, provenance_mode=pm, references=(witness_ref,))
    r = verify(c)
    assert r.ok
    assert not any(f.code == "provenance_time_rung_overclaim" for f in r.findings)
    assert r.assurance["provenance_time_rung"] == "witnessed"


def test_time_rung_witnessed_with_wrong_purpose_reference_still_overclaims():
    pm = _backfilled(time_rung="witnessed")
    wrong_purpose_ref = ReferenceEntry(
        type="x-witnessed-timestamp", digest_alg="SHA-256", digest="7" * 64,
        citation_purpose="acted_on",
    )
    c = emit(**_BASE, provenance_mode=pm, references=(wrong_purpose_ref,))
    r = verify(c)
    assert not r.ok
    assert any(f.code == "provenance_time_rung_overclaim" for f in r.findings)


# ---------------------------------------------------------------------------
# store-level: chain.relation="duplicates" (check 9)
# ---------------------------------------------------------------------------


def test_duplicates_collapsed_once():
    contemporaneous = emit(action_id="act-parent", operator="test-org", developer="test-agent@1.0")
    backfilled = emit(
        action_id="act-duplicate", operator="test-org", developer="test-agent@1.0",
        provenance_mode=_backfilled(), prior_capsule_id=contemporaneous["capsule_id"],
        chain_relation="duplicates",
    )
    results = verify_store([contemporaneous, backfilled])
    assert all(r.ok for r in results)
    dup_finding = next(f for f in results[1].findings if f.code == "duplicate_collapsed")
    assert dup_finding.severity == "info"
    assert dup_finding.check == 9


def test_duplicate_parent_itself_backfilled_flagged():
    parent = emit(
        action_id="act-parent-2", operator="test-org", developer="test-agent@1.0",
        provenance_mode=_backfilled(import_batch="parent-batch"),
    )
    child = emit(
        action_id="act-duplicate-2", operator="test-org", developer="test-agent@1.0",
        provenance_mode=_backfilled(import_batch="child-batch"),
        prior_capsule_id=parent["capsule_id"], chain_relation="duplicates",
    )
    results = verify_store([parent, child])
    assert any(f.code == "duplicate_parent_not_contemporaneous" for f in results[1].findings)


# ---------------------------------------------------------------------------
# parse_capsule round-trip
# ---------------------------------------------------------------------------


def test_parse_capsule_provenance_mode_backfilled():
    c = emit(**_BASE, provenance_mode=_backfilled())
    parsed = parse_capsule(c)
    assert parsed.provenance_mode is not None
    assert parsed.provenance_mode.mode == "backfilled"
    assert parsed.provenance_mode.source_ref.digest == "5" * 64
    assert parsed.provenance_mode.import_batch == "batch-1"


def test_parse_capsule_none_when_absent():
    c = emit(**_BASE)
    parsed = parse_capsule(c)
    assert parsed.provenance_mode is None


def test_parse_capsule_rejects_backfilled_missing_fields():
    c = _sealed_with_pm({"mode": "backfilled"})
    with pytest.raises(InvariantError):
        parse_capsule(c)
