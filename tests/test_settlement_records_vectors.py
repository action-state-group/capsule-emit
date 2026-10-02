# SPDX-License-Identifier: Apache-2.0
"""The settlement-records conformance vectors, run through capsule_emit's verifier.

The vectors are the draft's own (vendored from agent-action-capsule, see
test-vectors/settlement-records/README.md). Each case gives the leg records,
the key policy, the octets of the wrapped objects, and the states, failures
and findings a verifier must derive. This is the counterparty's check: our
verifier must agree with it case by case.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from capsule_emit.settlement import (
    DELIVERY_DIRECTIONS,
    LEGS,
    MEMBERS,
    PAYMENT_REF_TYPES,
    ROLES,
    SETTLEMENT_VERSION,
    STATUSES,
    WRAPPED_TYPES,
    verify_settlements,
)

DIR = Path(__file__).resolve().parents[1] / "test-vectors" / "settlement-records"
CASES = json.loads((DIR / "cases.json").read_text())
REGISTRY = json.loads((DIR / "registry.json").read_text())


def _b64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def test_vendored_files_match_their_checksums():
    for line in (DIR / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split()
        assert hashlib.sha256((DIR / name).read_bytes()).hexdigest() == digest, name


def test_our_tables_match_the_draft_registry():
    assert SETTLEMENT_VERSION == REGISTRY["settlement_version"]
    assert list(LEGS) == REGISTRY["legs"]
    assert list(ROLES) == REGISTRY["sealer_roles"]
    assert list(STATUSES) == REGISTRY["statuses"]
    assert DELIVERY_DIRECTIONS == REGISTRY["delivery_directions"]
    assert WRAPPED_TYPES == REGISTRY["wrapped_types"]
    assert {t: (list(v["qualifiers"]), v["receive_fee"]) for t, v in PAYMENT_REF_TYPES.items()} == {
        t: (v["qualifiers"], v["receive_fee"]) for t, v in REGISTRY["payment_ref_types"].items()}
    assert {leg: {k: list(v) for k, v in m.items()} for leg, m in MEMBERS.items()} == REGISTRY["settlement_members"]


def _run(case: dict) -> dict:
    capsules, labels = [], {}
    for r in case["records"]:
        capsule = dict(r["capsule"])
        if r.get("envelope_hex"):
            capsule["signature"] = r["envelope_hex"]
            capsule["key_id"] = r["envelope_kid"]
        capsules.append(capsule)
        labels[r["capsule_id"]] = r["label"]
    objects = {w["digest"]: _b64u(w["octets_b64u"]) for w in case["wrapped_objects"]}
    report = verify_settlements(capsules, key_policy=case["key_policy"], wrapped_objects=objects)

    def relabel(entries):
        return [{"records": [labels.get(c, c) for c in e["records"]], "code": e["code"]} for e in entries]

    settlements = []
    for s in report.settlements:
        s = {k: v for k, v in s.items() if k != "iso20022"}  # ours only; the vectors do not carry it
        s["terms"] = labels[s["terms"]]
        settlements.append(s)
    return {"conforming": report.conforming, "failures": relabel(report.failures),
            "findings": relabel(report.findings), "settlements": settlements}


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda c: c["id"])
def test_derivation_matches_the_vector(case):
    assert _run(case) == case["expect"]


def test_every_case_ran():
    assert len(CASES["cases"]) == CASES["count"] == 19
