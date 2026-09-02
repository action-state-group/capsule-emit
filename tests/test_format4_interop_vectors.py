# SPDX-License-Identifier: Apache-2.0
"""Replay the frozen format-4 cross-language vector pack."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

from agent_action_capsule.producer_envelope import verify_producer_envelope

from capsule_emit.verification import verify_capsule

VECTOR_ROOT = Path(__file__).resolve().parents[1] / "test-vectors" / "format4-interop"


def _load_generator():
    path = VECTOR_ROOT / "scripts" / "generate_vectors.py"
    spec = importlib.util.spec_from_file_location("format4_interop_generator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_frozen_checksums() -> None:
    for line in (VECTOR_ROOT / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        expected, relative = line.split("  ", 1)
        actual = hashlib.sha256((VECTOR_ROOT / relative).read_bytes()).hexdigest()
        assert actual == expected, relative


def test_vectors_regenerate_byte_for_byte(tmp_path: Path) -> None:
    generator = _load_generator()
    generated = tmp_path / "format4-interop"
    generator.generate(VECTOR_ROOT / "input.json", generated)

    frozen_paths = ["input.json", "vectors.json", "SHA256SUMS"]
    frozen_paths.extend(
        str(path.relative_to(VECTOR_ROOT)) for path in sorted((VECTOR_ROOT / "valid").glob("*/*"))
    )
    for relative in frozen_paths:
        assert (generated / relative).read_bytes() == (VECTOR_ROOT / relative).read_bytes(), relative


def test_vectors_verify_as_format4_capsules_and_envelopes() -> None:
    manifest = json.loads((VECTOR_ROOT / "vectors.json").read_text(encoding="utf-8"))
    for case in manifest["cases"]:
        case_dir = VECTOR_ROOT / case["path"]
        expected = json.loads((case_dir / "expected.json").read_text(encoding="utf-8"))
        detached = json.loads((case_dir / "capsule.detached.jcs").read_text(encoding="utf-8"))
        stored = json.loads((case_dir / "capsule.stored.json").read_text(encoding="utf-8"))
        envelope = (case_dir / "envelope.cose").read_bytes()

        assert detached["capsule_id"] == expected["capsule_id"]
        assert verify_capsule(detached).ok
        assert verify_capsule(stored).ok
        verified = verify_producer_envelope(expected["capsule_id"], envelope)
        assert verified.ok
        assert verified.public_key.hex() == expected["public_key_hex"]
