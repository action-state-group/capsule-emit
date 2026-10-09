# SPDX-License-Identifier: Apache-2.0
"""Permalink fragments use the evidence-bundle/v2 §9 codec, and an oversize
permalink falls back to the pointer form instead of an unopenable URL.

``test-vectors/permalink-bundle/vectors.json`` pins each case's expected
Bundle, its §9 fragment, its bundle digest and its pointer fragment. The
fragments and digests were produced by agent-action-capsule's Go
``bundle.EncodeFragment`` / ``canonical.JCS``, not by capsule-emit.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from agent_action_capsule.bundle import bundle_digest, decode_fragment, verify_bundle

from capsule_emit import seal
from capsule_emit.cli import main as cli_main
from capsule_emit.permalink import (
    MAX_INLINE_URL_BYTES,
    PermalinkError,
    build_bundle,
    build_url,
    pointer_fragment,
    resolve_pointer,
)

#: The verify surface these tests name (there is no default).
DEFAULT_BASE_URL = "https://verify.example"

_DOC = json.loads(
    (Path(__file__).resolve().parents[2] / "test-vectors" / "permalink-bundle" / "vectors.json").read_text(
        encoding="utf-8"
    )
)
CASES = pytest.mark.parametrize("case", _DOC["cases"], ids=[c["id"] for c in _DOC["cases"]])


def _fragment(url: str) -> str:
    return url.split("#", 1)[1]


# ---------------------------------------------------------------------------
# §9 codec — cross-implementation vectors
# ---------------------------------------------------------------------------


@CASES
def test_bundle_shape_matches_the_vector(case):
    assert build_bundle(case["capsules"], bundle=case["bundle"], disclosures=case["disclosures"]) == case[
        "expected_bundle"
    ]


@CASES
def test_fragment_matches_the_go_reference_encoder(case):
    url = build_url(case["capsules"], bundle=case["bundle"], disclosures=case["disclosures"], base_url=DEFAULT_BASE_URL)
    assert url == f"{DEFAULT_BASE_URL}/bundle#{case['fragment']}"


@CASES
def test_bundle_digest_matches_the_go_reference(case):
    assert bundle_digest(case["expected_bundle"]) == case["bundle_digest"]


@CASES
def test_pointer_fragment_matches_the_go_reference_encoder(case):
    url = build_url(
        case["capsules"],
        bundle=case["bundle"],
        disclosures=case["disclosures"],
        bundle_locations=_DOC["locations"],
        max_url_bytes=1024,  # every inline vector URL is longer, every pointer URL shorter
        base_url=DEFAULT_BASE_URL,
    )
    assert url == f"{DEFAULT_BASE_URL}/bundle#{case['pointer_fragment']}"
    assert decode_fragment(case["pointer_fragment"]) == case["pointer"]


@CASES
def test_fragment_is_unpadded_base64url(case):
    frag = _fragment(build_url(case["capsules"], bundle=case["bundle"], disclosures=case["disclosures"], base_url=DEFAULT_BASE_URL))
    assert "=" not in frag and "+" not in frag and "/" not in frag


@CASES
def test_reference_verifier_accepts_the_decoded_bundle(case):
    decoded = decode_fragment(case["fragment"])
    result = verify_bundle(decoded)
    expected_closure = "withheld" if decoded["completeness"]["records_mode"] == "declared_incomplete" else "pass"
    assert result.graph_closure.status == expected_closure
    assert all(r.ok for r in result.capsule_results.values())
    assert all(d.status in ("disclosure_match", "withheld") for d in result.disclosures)


# ---------------------------------------------------------------------------
# Oversize → pointer fallback
# ---------------------------------------------------------------------------


@pytest.fixture
def oversize_capsule():
    """A capsule whose disclosed input alone puts the inline URL past 2 MiB."""
    payload = {"blob": "x" * (MAX_INLINE_URL_BYTES * 3 // 4 + 1024)}
    cap = seal(payload, action="write_order", operator="example-org", developer="agent@v1", verdict="executed", anchor=False)
    return cap.capsule, {"agent_input": payload}


def test_oversize_permalink_is_refused_without_a_location(oversize_capsule):
    capsule, disclosures = oversize_capsule
    with pytest.raises(PermalinkError, match="--bundle-location"):
        build_url([capsule], bundle=False, disclosures=disclosures, base_url=DEFAULT_BASE_URL)


def test_oversize_permalink_falls_back_to_a_pointer(oversize_capsule):
    capsule, disclosures = oversize_capsule
    url = build_url(
        [capsule], bundle=False, disclosures=disclosures, bundle_locations=["https://bundles.example.org/1.json"], base_url=DEFAULT_BASE_URL
    )
    assert len(url) < 1024
    pointer = decode_fragment(_fragment(url))
    full = build_bundle([capsule], bundle=False, disclosures=disclosures)
    assert pointer == {
        "bundle_ref": {
            "digest": bundle_digest(full),
            "root": capsule["capsule_id"],
            "locations": ["https://bundles.example.org/1.json"],
        }
    }
    assert resolve_pointer(pointer, full) is full


def test_inline_permalink_under_the_limit_ignores_locations():
    cap = seal({"a": 1}, action="a", operator="example-org", developer="agent@v1", verdict="executed", anchor=False)
    url = build_url([cap.capsule], bundle=False, bundle_locations=["https://bundles.example.org/1.json"], base_url=DEFAULT_BASE_URL)
    assert decode_fragment(_fragment(url))["bundle_kind"] == "evidence-bundle/v2"


def test_resolve_pointer_rejects_a_substituted_bundle():
    a = seal({"a": 1}, action="a", operator="example-org", developer="agent@v1", verdict="executed", anchor=False)
    b = seal({"b": 2}, action="b", operator="example-org", developer="agent@v1", verdict="executed", anchor=False)
    genuine = build_bundle([a.capsule], bundle=False)
    pointer = pointer_fragment(genuine, ["https://bundles.example.org/1.json"])
    with pytest.raises(PermalinkError, match="digest"):
        resolve_pointer(pointer, build_bundle([b.capsule], bundle=False))
    tampered = dict(genuine, completeness={"closure_depth": 0, "records_mode": "complete"})
    with pytest.raises(PermalinkError, match="digest"):
        resolve_pointer(pointer, tampered)


def test_resolve_pointer_rejects_a_root_mismatch():
    a = seal({"a": 1}, action="a", operator="example-org", developer="agent@v1", verdict="executed", anchor=False)
    genuine = build_bundle([a.capsule], bundle=False)
    pointer = pointer_fragment(genuine, ["https://bundles.example.org/1.json"])
    pointer["bundle_ref"]["root"] = "0" * 64
    with pytest.raises(PermalinkError, match="root"):
        resolve_pointer(pointer, genuine)


def test_pointer_needs_a_location():
    a = seal({"a": 1}, action="a", operator="example-org", developer="agent@v1", verdict="executed", anchor=False)
    with pytest.raises(PermalinkError, match="location"):
        pointer_fragment(build_bundle([a.capsule], bundle=False), [])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _oversize_ledger(tmp_path):
    ledger = tmp_path / "big.jsonl"
    payload = {"blob": "x" * (MAX_INLINE_URL_BYTES * 3 // 4 + 1024)}
    seal(payload, action="write_order", operator="example-org", developer="agent@v1", verdict="executed",
         anchor=False, ledger=ledger)
    reveal = tmp_path / "input.json"
    reveal.write_text(json.dumps(payload))
    return ledger, reveal


def test_cli_oversize_without_location_exits_1_and_prints_no_url(tmp_path, capsys):
    ledger, reveal = _oversize_ledger(tmp_path)
    assert cli_main(["permalink", "--base-url", DEFAULT_BASE_URL, "--ledger", str(ledger), "--reveal", f"agent_input={reveal}"]) == 1
    captured = capsys.readouterr()
    assert "--bundle-location" in captured.err
    assert "http" not in captured.out


def test_cli_oversize_with_location_prints_a_pointer_matching_the_written_bundle(tmp_path, capsys):
    ledger, reveal = _oversize_ledger(tmp_path)
    out_path = tmp_path / "bundle.json"
    location = "https://bundles.example.org/big.json"
    code = cli_main(
        [
            "permalink", "--base-url", DEFAULT_BASE_URL, "--ledger", str(ledger), "--reveal", f"agent_input={reveal}",
            "--bundle-out", str(out_path), "--bundle-location", location,
        ]
    )
    assert code == 0
    url = [line for line in capsys.readouterr().out.splitlines() if line.startswith("http")][0]
    pointer = decode_fragment(_fragment(url))
    hosted = json.loads(out_path.read_bytes())
    assert pointer["bundle_ref"]["locations"] == [location]
    assert resolve_pointer(pointer, hosted) == hosted
    assert verify_bundle(hosted).disclosures[0].status == "disclosure_match"
