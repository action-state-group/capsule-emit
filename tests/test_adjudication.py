# SPDX-License-Identifier: Apache-2.0
"""Tests for the capsule-emit adjudication module.

Covers:
- seal_adjudication produces correct chain (parent_capsule_id + relation="adjudicates")
- disposition.verdict_class == "assessed" (detection, never enforcement)
- compute_attestation.adjudication carries source/capture_method/verdict/margin/margin_tau
- corroborated / inconclusive / contradicted:<owner_id> verdict shapes
- invalid verdict strings are rejected
- contradicted() rejects an empty owner_id
- sealed capsule passes agent_action_capsule.verify()
- Zero engine imports (no closed-source engine references in adjudication.py)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from capsule_emit.adjudication import (
    RELATION_ADJUDICATES,
    VERDICT_CORROBORATED,
    VERDICT_INCONCLUSIVE,
    contradicted,
    seal_adjudication,
)
from capsule_emit.core import _emit_capsule
from capsule_emit.verification import verify_capsule as verify

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _half(tmp_path: Path, action: str = "serve_exchange") -> dict:
    """Emit a stand-in 'twin half' capsule and return the capsule dict."""
    result = _emit_capsule(
        action=action,
        operator="test-org",
        developer="agent@v1",
        anchor=False,
        ledger=tmp_path / "ledger.jsonl",
    )
    return result.capsule


def _adjudication_result(tmp_path: Path, half_a_id: str, half_b_id: str, verdict: str, **kwargs):
    return seal_adjudication(
        half_a_capsule_id=half_a_id,
        half_b_capsule_id=half_b_id,
        verdict=verdict,
        margin=kwargs.pop("margin", 1.0),
        margin_tau=kwargs.pop("margin_tau", 1.0),
        ledger=tmp_path / "ledger.jsonl",
        anchor=False,
        operator="test-org",
        developer="referee@v1",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Chain shape
# ---------------------------------------------------------------------------


def test_seal_adjudication_chains_to_half_a(tmp_path):
    half_a = _half(tmp_path, "serve_a")
    half_b = _half(tmp_path, "serve_b")

    result = _adjudication_result(tmp_path, half_a["capsule_id"], half_b["capsule_id"], VERDICT_CORROBORATED)
    chain = result.capsule.get("chain") or {}

    assert chain.get("parent_capsule_id") == half_a["capsule_id"]
    assert chain.get("relation") == RELATION_ADJUDICATES == "adjudicates"


def test_seal_adjudication_verdict_class_is_assessed(tmp_path):
    half_a = _half(tmp_path, "serve_a")
    half_b = _half(tmp_path, "serve_b")

    result = _adjudication_result(tmp_path, half_a["capsule_id"], half_b["capsule_id"], VERDICT_INCONCLUSIVE)

    assert result.capsule["disposition"]["verdict_class"] == "assessed"
    assert result.capsule["action_type"] == "decide"


def test_seal_adjudication_capsule_verifies(tmp_path):
    half_a = _half(tmp_path, "serve_a")
    half_b = _half(tmp_path, "serve_b")

    result = _adjudication_result(tmp_path, half_a["capsule_id"], half_b["capsule_id"], VERDICT_CORROBORATED)
    v = verify(result.capsule)
    assert v.ok, f"Adjudication capsule failed verify: {v}"


# ---------------------------------------------------------------------------
# compute_attestation.adjudication payload
# ---------------------------------------------------------------------------


def test_seal_adjudication_carries_honesty_labels(tmp_path):
    half_a = _half(tmp_path, "serve_a")
    half_b = _half(tmp_path, "serve_b")

    result = _adjudication_result(
        tmp_path,
        half_a["capsule_id"],
        half_b["capsule_id"],
        VERDICT_CORROBORATED,
        margin=1.0,
        margin_tau=1.0,
        divergence_index=None,
        twin_owner_distinct=True,
        weights_digest="sha256:stub",
    )
    ca = result.capsule["model_attestation"]["compute_attestation"]
    adj = ca["adjudication"]

    assert adj["source"] == "twin_comparison"
    assert adj["capture_method"] == "deterministic_replay"
    assert adj["verdict"] == "corroborated"
    # Exact decimal strings (RFC 8785 §3.2.2.3) -- a raw JSON float in a
    # digest-bearing field raises FloatInDigestError (§5.1).
    assert adj["margin"] == "1"
    assert adj["margin_tau"] == "1"
    assert isinstance(adj["margin"], str)
    assert adj["divergence_index"] is None
    assert adj["twin_owner_distinct"] is True
    assert adj["weights_digest"] == "sha256:stub"
    assert adj["half_a_capsule_id"] == half_a["capsule_id"]
    assert adj["half_b_capsule_id"] == half_b["capsule_id"]


# ---------------------------------------------------------------------------
# Verdict shapes
# ---------------------------------------------------------------------------


def test_verdict_inconclusive_is_first_class(tmp_path):
    """inconclusive is a normal, sealable verdict -- not an error path."""
    half_a = _half(tmp_path, "serve_a")
    half_b = _half(tmp_path, "serve_b")

    result = _adjudication_result(
        tmp_path, half_a["capsule_id"], half_b["capsule_id"], VERDICT_INCONCLUSIVE, margin=0.2, margin_tau=1.0,
    )
    assert result.capsule["model_attestation"]["compute_attestation"]["adjudication"]["verdict"] == "inconclusive"


def test_verdict_contradicted_names_the_owner(tmp_path):
    half_a = _half(tmp_path, "serve_a")
    half_b = _half(tmp_path, "serve_b")

    verdict = contradicted("owner-b")
    assert verdict == "contradicted:owner-b"

    result = _adjudication_result(tmp_path, half_a["capsule_id"], half_b["capsule_id"], verdict)
    assert result.capsule["model_attestation"]["compute_attestation"]["adjudication"]["verdict"] == "contradicted:owner-b"


def test_contradicted_rejects_empty_owner():
    with pytest.raises(ValueError, match="non-empty owner_id"):
        contradicted("")


def test_seal_adjudication_rejects_invalid_verdict(tmp_path):
    half_a = _half(tmp_path, "serve_a")
    half_b = _half(tmp_path, "serve_b")

    with pytest.raises(ValueError, match="verdict="):
        _adjudication_result(tmp_path, half_a["capsule_id"], half_b["capsule_id"], "yes")


def test_seal_adjudication_rejects_bare_contradicted_prefix(tmp_path):
    """'contradicted:' with no owner_id suffix is not a valid verdict shape."""
    half_a = _half(tmp_path, "serve_a")
    half_b = _half(tmp_path, "serve_b")

    with pytest.raises(ValueError, match="verdict="):
        _adjudication_result(tmp_path, half_a["capsule_id"], half_b["capsule_id"], "contradicted:")


# ---------------------------------------------------------------------------
# Zero engine imports -- adjudication.py must only import public capsule packages
# ---------------------------------------------------------------------------


def test_no_engine_imports():
    """adjudication.py must not import from closed-source engine packages."""
    import ast
    import pathlib

    src = pathlib.Path(__file__).parent.parent / "capsule_emit" / "adjudication.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))

    _PUBLIC = frozenset({"capsule_emit", "agent_action_capsule", "scitt_cose"})
    _STDLIB = frozenset({
        "__future__", "abc", "collections", "contextlib", "copy", "dataclasses",
        "datetime", "enum", "functools", "hashlib", "itertools", "json", "math",
        "os", "pathlib", "re", "sys", "typing", "uuid",
    })

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue  # relative imports are always internal
            if not node.module:
                continue
            root = node.module.split(".")[0]
            assert root in _PUBLIC or root in _STDLIB, (
                f"adjudication.py must not import from {root!r} — only public capsule packages are allowed"
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root in _PUBLIC or root in _STDLIB, (
                    f"adjudication.py must not import {alias.name!r} — only public capsule packages are allowed"
                )
