"""Writes a historical ledger.jsonl with origin/main's writers and prints its
MMR root as origin/main's checkpoint index computes it.

usage: make_fixture.py OUT_DIR   (run with origin/main's python on sys.path)
"""
import hashlib
import json
import os
import sys
from pathlib import Path

os.environ["CAPSULE_WITNESS"] = "off"

from cll.checkpoint.index import MmrLedger  # noqa: E402

from capsule_emit import ledger, seal, witness  # noqa: E402
from capsule_emit.canonicalization import compute_capsule_id  # noqa: E402
from capsule_emit.signing import LocalKeypairSigner, sign_producer_envelope  # noqa: E402
from capsule_emit.surface import log  # noqa: E402

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
path = out / "ledger.jsonl"
key_path = out / "producer-key.pem"
signer = LocalKeypairSigner(key_path)

first = seal({"n": 1}, action="fixture-a", operator="acme", anchor=False, witness=False, ledger=path, signer=signer)
seal({"n": 2}, action="fixture-b", operator="acme", anchor=False, witness=False, ledger=path, signer=signer,
     confirms=first.capsule_id)
log(b"raw log bytes", action="fixture-log", operator="acme", ledger=path, witness=False)


def vintage(action, declared):
    capsule = {
        "format_version": "2",
        "action_id": action,
        "action_type": "fyi",
        "operator": "acme",
        "timestamp": "2025-01-02T03:04:05.678901+00:00",
        "chain": {"parent_capsule_id": first.capsule_id, "relation": "follows"},
        "empty_list": [],
        "null_member": None,
    }
    if declared:
        capsule["canonicalization_id"] = "jcs-n"
    capsule["capsule_id"] = compute_capsule_id(capsule)
    capsule["signature"], capsule["key_id"] = sign_producer_envelope(signer, capsule["capsule_id"])
    ledger.append_to_ledger(capsule, path)


vintage("fixture-jcs-n", True)
vintage("fixture-absent", False)
# Old writers never deduplicated: a repeated capsule line, written as raw bytes
# because append_to_ledger now writes each line once.
with open(path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(first.capsule, separators=(",", ":")) + "\n")


def digest(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


ledger.append_to_ledger({"kind": ledger.CHECKPOINT_STAMP_KIND, "v": 1, "capsule_id": digest({"stamp": 1}),
                         "checkpoint": {"v": 1, "kind": "checkpoint", "log_id": "f" * 64, "mmr_size": 3,
                                        "root": "a" * 64, "timestamp": "2026-10-15T00:00:01Z"}}, path)
ledger.append_to_ledger({"kind": ledger.DISCLOSURE_RECORD_KIND, "v": 1, "audience": "auditor",
                         "disclosed_capsule_ids": [first.capsule_id], "timestamp": "2026-10-15T00:00:02.5Z",
                         "capsule_id": digest({"disclosure": 1})}, path)
ledger.append_to_ledger({"kind": ledger.WITNESS_BACKFILL_KIND, "v": 1, "capsule_id": digest({"backfill": 1}),
                         "checkpoint_entry_digest": digest({"stamp": 1}), "witness": {"ts_url": "https://ts.example"}},
                        path)
ledger.append_to_ledger({"capsule_id": digest({"padding": 1}), "record_type": "padding"}, path)
seal({"n": 3}, action="fixture-c", operator="acme", anchor=False, witness=False, ledger=path, signer=signer)

mmr = MmrLedger(witness._JsonlLogSource(str(path)))
mmr.sync()
print(json.dumps({"leaves": mmr.leaf_count(), "size": mmr.size(), "root": mmr.root().hex()}))
