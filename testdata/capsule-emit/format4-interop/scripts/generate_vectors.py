# SPDX-License-Identifier: Apache-2.0
"""Generate the implementation-neutral format-4 interoperability vectors."""

from __future__ import annotations

import hashlib
import importlib
import json
import shutil
import tempfile
import uuid
from importlib.metadata import version
from pathlib import Path
from unittest import mock

from agent_action_capsule.canonical import jcs
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from capsule_emit import __version__ as capsule_emit_version
from capsule_emit import audit, can, did, received, seal, who
from capsule_emit.canonicalization import _LOCAL_ONLY_FIELDS
from capsule_emit.signing import LocalKeypairSigner

VECTOR_ROOT = Path(__file__).resolve().parents[1]
SLOT_WRAPPERS = {"who": who, "can": can, "did": did, "audit": audit}


def _signer(path: Path, seed_hex: str) -> LocalKeypairSigner:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed_hex))
    path.write_bytes(private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    return LocalKeypairSigner(path)


def _common_kwargs(spec: dict, record: dict, ledger: Path, signer: LocalKeypairSigner) -> dict:
    disposition = spec["disposition"]
    return {
        "operator": spec["operator"],
        "developer": spec["developer"],
        "action_type": record["action_type"],
        "verdict": record["verdict"],
        "decision": disposition["decision"],
        "approver": disposition["approver"],
        "human_disposed": disposition["human_disposed"],
        "anchor": False,
        "witness": False,
        "ledger": ledger,
        "signer": signer,
    }


def _emit_records(spec: dict, ledger: Path, signer: LocalKeypairSigner) -> dict:
    emitted = {}
    uuids = iter(uuid.UUID(record["uuid"]) for record in spec["records"])
    base_emit = importlib.import_module("agent_action_capsule.emit")
    with (
        mock.patch.object(base_emit.uuid, "uuid4", side_effect=lambda: next(uuids)),
        mock.patch.object(base_emit, "_utc_now", return_value=spec["timestamp"]),
    ):
        for record in spec["records"]:
            kwargs = _common_kwargs(spec, record, ledger, signer)
            operation = record["operation"]
            if operation == "seal":
                result = seal(
                    record["payload"],
                    action=record["action"],
                    agent_output=record.get("agent_output"),
                    model=record.get("model"),
                    runtime=record.get("runtime"),
                    **kwargs,
                )
            elif operation == "received":
                result = received(
                    record["artifact_utf8"],
                    type=record["artifact_type"],
                    action=record["action"],
                    **kwargs,
                )
            elif operation == "composition":
                members = [
                    SLOT_WRAPPERS[member["slot"]](emitted[member["record"]])
                    for member in record["members"]
                ]
                result = seal(*members, **kwargs)
            else:
                raise ValueError(f"unsupported vector operation: {operation}")
            if result.capsule["action_id"] != record["action_id"]:
                raise AssertionError(
                    f"{record['name']} action_id: {result.capsule['action_id']} != {record['action_id']}"
                )
            emitted[record["name"]] = result
    return emitted


def _write_case(output_root: Path, name: str, result) -> dict:
    case_dir = output_root / "valid" / name
    case_dir.mkdir(parents=True, exist_ok=True)
    stored = result.capsule
    detached = {key: value for key, value in stored.items() if key not in _LOCAL_ONLY_FIELDS}
    detached_bytes = jcs(detached)
    stored_bytes = json.dumps(stored, separators=(",", ":")).encode("utf-8") + b"\n"
    envelope = bytes.fromhex(result.signature)

    (case_dir / "capsule.detached.jcs").write_bytes(detached_bytes)
    (case_dir / "capsule.stored.json").write_bytes(stored_bytes)
    (case_dir / "envelope.cose").write_bytes(envelope)

    expected = {
        "capsule_id": result.capsule_id,
        "public_key_hex": result.key_id,
    }
    (case_dir / "expected.json").write_text(
        json.dumps(expected, indent=2) + "\n", encoding="utf-8"
    )
    return {"name": name, "path": f"valid/{name}"}


def _write_checksums(output_root: Path) -> None:
    paths = [output_root / "input.json", output_root / "vectors.json"]
    paths.extend(sorted((output_root / "valid").glob("*/*")))
    lines = []
    for path in paths:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.relative_to(output_root)}")
    (output_root / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def generate(input_path: Path = VECTOR_ROOT / "input.json", output_root: Path = VECTOR_ROOT) -> None:
    spec = json.loads(input_path.read_text(encoding="utf-8"))
    output_root.mkdir(parents=True, exist_ok=True)
    if input_path.resolve() != (output_root / "input.json").resolve():
        (output_root / "input.json").write_bytes(input_path.read_bytes())

    valid_root = output_root / "valid"
    if valid_root.exists():
        shutil.rmtree(valid_root)

    with tempfile.TemporaryDirectory() as temporary:
        temp_root = Path(temporary)
        ledger = temp_root / "ledger.jsonl"
        signer = _signer(temp_root / "signing-key.pem", spec["seed_hex"])
        emitted = _emit_records(spec, ledger, signer)

    cases = [_write_case(output_root, name, result) for name, result in emitted.items()]
    manifest = {
        "format_version": "1",
        "profile": "draft-mih-scitt-agent-action-capsule-04#format4-interop",
        "generator": "capsule_emit.surface seal/received/slot composition",
        "generator_versions": {
            "capsule-emit": capsule_emit_version,
            "agent-action-capsule": version("agent-action-capsule"),
        },
        "cases": cases,
    }
    (output_root / "vectors.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    _write_checksums(output_root)


if __name__ == "__main__":
    generate()
