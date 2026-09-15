#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Inspect sealed-log demo — end to end.

Seals a small, offline Inspect ``.eval`` log with
``capsule_emit.adapters.inspect_ai``, registers ONE checkpoint with the live
public witness (``witness.agentactioncapsule.org``), and builds two bundles:

  1. ``bundle_disclosed.json``  — every sample's payloads revealed
  2. ``bundle_withheld.json``   — the SAME records, one sample's payloads
                                  withheld (digest present, payload absent)

Both are verified offline (``capsule_emit.disclose.verify_disclosure`` — no
network, no reader beyond this library) and a tamper check is run against
the disclosed bundle: flip one byte in a revealed payload, re-verify,
confirm it fails and names the record.

Everything this script produces is written to ``--out-dir`` (default:
``./out``) for a person to load into the public verifier
(``https://verify.agentactioncapsule.org``) — see the example README for the
click-by-click steps. This script performs no browser step itself.

Usage:
    pip install inspect_ai "capsule-emit[dev]"
    python run_demo.py [--out-dir DIR] [--no-witness]
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from generate_eval_log import mock_generate, tiny_arithmetic  # noqa: E402

from capsule_emit import witness  # noqa: E402
from capsule_emit.adapters.inspect_ai import InspectAIListenerCore, seal_eval_log  # noqa: E402
from capsule_emit.disclose import Disclosure, disclose, verify_disclosure  # noqa: E402
from capsule_emit.ledger import read_ledger  # noqa: E402
from capsule_emit.permalink import DEFAULT_BASE_URL, build_url  # noqa: E402

WITHHELD_SAMPLE_ID = "add-9-9"  # the sample whose payloads bundle_withheld.json omits


class _CapturingCore(InspectAIListenerCore):
    """Demo-only: remembers the exact payload each capsule_id committed to,
    so this script can later hand it back to ``disclose(reveal=...)``.
    capsule-emit itself never stores payloads (digest-only by design) --
    this side channel exists purely so the demo can disclose its own data
    without re-deriving it, and is not part of the adapter."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.payloads: dict[str, dict] = {}
        self.sample_of: dict[str, str] = {}

    def seal_model_call(self, *, sample_id, **kw):  # noqa: D102
        result = super().seal_model_call(sample_id=sample_id, **kw)
        self.sample_of[result.capsule_id] = str(sample_id)
        return result

    def seal_sample_terminal(self, *, sample_id, **kw):  # noqa: D102
        result = super().seal_sample_terminal(sample_id=sample_id, **kw)
        self.sample_of[result.capsule_id] = str(sample_id)
        return result

    def emit_capsule(self, action, tool_input=None, tool_output=None, **kw):  # noqa: D102
        result = super().emit_capsule(action, tool_input=tool_input, tool_output=tool_output, **kw)
        self.payloads[result.capsule_id] = {"agent_input": tool_input, "agent_output": tool_output}
        return result


def _section(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, 70 - len(title)))


def run_eval(out_dir: Path) -> Path:
    _section("Step 1 -- run the Inspect eval (mockllm/model, offline, deterministic)")
    from inspect_ai import eval as inspect_eval

    log_dir = out_dir / "eval_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logs = inspect_eval(
        tiny_arithmetic(),
        model="mockllm/model",
        model_args={"custom_outputs": mock_generate},
        log_dir=str(log_dir),
        log_format="eval",
        display="plain",
    )
    log = logs[0]
    print(f"  status: {log.status}")
    print(f"  log: {log.location}")
    assert log.status == "success"
    return Path(log.location.replace("file://", "")) if str(log.location).startswith("file://") else Path(log.location)


def seal_log(eval_log_path: Path, ledger_path: Path) -> _CapturingCore:
    _section("Step 2 -- seal the .eval log: one record per model call, one per sample result")
    core = _CapturingCore(
        operator="inspect-sealed-log-demo",
        developer="capsule-emit-inspect-adapter@0.1",
        ledger=str(ledger_path),
        anchor=False,
    )
    per_sample = seal_eval_log(
        str(eval_log_path),
        operator="inspect-sealed-log-demo",
        developer="capsule-emit-inspect-adapter@0.1",
        ledger=str(ledger_path),
        core=core,
    )
    for key, results in per_sample.items():
        print(f"  {key}: {len(results)} record(s) -> {[r.capsule_id[:8] for r in results]}")
    total = sum(len(v) for v in per_sample.values())
    print(f"  total sealed: {total}")
    return core


def register_checkpoint(ledger_path: Path, out_dir: Path, use_witness: bool) -> dict | None:
    _section("Step 3 -- register one checkpoint with the public witness")
    if not use_witness:
        print("  --no-witness: skipping (local-only run)")
        return None
    cp = witness.push(str(ledger_path))
    if cp is None:
        print("  nothing to checkpoint (unexpected -- ledger should be non-empty)")
        return None
    receipt = cp.to_dict()
    (out_dir / "checkpoint_receipt.json").write_text(json.dumps(receipt, indent=2))
    if cp.witnesses:
        w = cp.witnesses[0]
        print(f"  witness: {w.ts_url}")
        print(f"  leaf_index={w.leaf_index}  tree_size={w.tree_size}  is_stub={w.is_stub}")
    else:
        print("  WARNING: no witness confirmed this checkpoint (stayed self-attested)")
    print(f"  saved receipt -> {out_dir / 'checkpoint_receipt.json'}")
    return receipt


def build_bundles(ledger_path: Path, core: _CapturingCore, out_dir: Path) -> tuple[Disclosure, Disclosure]:
    _section("Step 4 -- build two bundles: all disclosed, then one sample withheld")
    records = read_ledger(ledger_path)
    first_id, last_id = records[0]["capsule_id"], records[-1]["capsule_id"]
    selector = f"{first_id}..{last_id}"

    withheld_ids = {cid for cid, sid in core.sample_of.items() if sid == WITHHELD_SAMPLE_ID}
    assert withheld_ids, f"no records found for sample {WITHHELD_SAMPLE_ID!r}"

    def reveal_for(cids: set[str]) -> dict[str, dict]:
        reveal = {}
        for cid in cids:
            fields = core.payloads[cid]
            entry = {k: v for k, v in fields.items() if v is not None}
            if entry:
                reveal[cid] = entry
        return reveal

    all_ids = {r["capsule_id"] for r in records}
    disclosed = disclose(
        ledger_path,
        selector,
        audience="evaluator-demo-full-disclosure",
        payloads="all",
        reveal=reveal_for(all_ids),
    )
    print(f"  bundle_disclosed: {len(disclosed.record_ids)} record(s), audience={disclosed.audience!r}")
    print(f"    completeness: {disclosed.completeness['records_note']}")
    print(f"    payloads: {disclosed.completeness['payloads_note']}")

    withheld = disclose(
        ledger_path,
        selector,
        audience="evaluator-demo-one-sample-withheld",
        payloads="selected",
        reveal=reveal_for(all_ids - withheld_ids),
    )
    print(f"  bundle_withheld:  {len(withheld.record_ids)} record(s), audience={withheld.audience!r}")
    print(f"    completeness: {withheld.completeness['records_note']}")
    print(f"    payloads: {withheld.completeness['payloads_note']}")
    print(f"    withheld sample {WITHHELD_SAMPLE_ID!r}: {len(withheld_ids)} record(s), digests present, no payload")

    (out_dir / "bundle_disclosed.json").write_text(json.dumps(disclosed.to_dict(), indent=2, default=str))
    (out_dir / "bundle_withheld.json").write_text(json.dumps(withheld.to_dict(), indent=2, default=str))
    return disclosed, withheld


def verify_bundles(disclosed: Disclosure, withheld: Disclosure, out_dir: Path) -> None:
    _section("Step 5 -- verify both bundles offline (no network, no reader)")
    lines = []
    for label, d in (("bundle_disclosed", disclosed), ("bundle_withheld", withheld)):
        ok, errors = verify_disclosure(d)
        line = f"{label}: ok={ok}" + (f"  notices={errors}" if errors else "")
        print(f"  {line}")
        lines.append(line)
        assert ok, f"{label} did not verify: {errors}"
    (out_dir / "verify_output.txt").write_text("\n".join(lines) + "\n")


def tamper_demo(disclosed: Disclosure, out_dir: Path) -> None:
    _section("Step 6 -- tamper demo: flip one byte in a disclosed payload, re-verify")
    target_cid = next(
        cid for cid in disclosed.record_ids
        if disclosed.envelopes[cid]["disclosures"].get("agent_output")
    )
    tampered_dict = copy.deepcopy(disclosed.to_dict())
    payload = tampered_dict["envelopes"][target_cid]["disclosures"]["agent_output"]
    original_text = json.dumps(payload, sort_keys=True)
    # Flip one digit in the payload content -- a one-byte tamper.
    if "content" in payload and isinstance(payload["content"], str) and payload["content"]:
        chars = list(payload["content"])
        for i, c in enumerate(chars):
            if c.isdigit():
                chars[i] = str((int(c) + 1) % 10)
                break
        payload["content"] = "".join(chars)
    else:
        payload["__tampered__"] = True

    tampered = Disclosure.from_dict(tampered_dict)
    ok, errors = verify_disclosure(tampered)
    print(f"  before: {original_text[:80]}...")
    print(f"  after:  {json.dumps(payload, sort_keys=True)[:80]}...")
    print(f"  verify_disclosure(tampered).ok = {ok}")
    for e in errors:
        print(f"    - {e}")
    assert not ok, "tamper demo should have failed verification"
    assert any(target_cid[:12] in e for e in errors), "the failing record should be named"

    report = {
        "target_capsule_id": target_cid,
        "field": "agent_output",
        "before": original_text,
        "after": json.dumps(payload, sort_keys=True),
        "verify_disclosure_ok": ok,
        "errors": errors,
    }
    (out_dir / "tamper_demo.json").write_text(json.dumps(report, indent=2))
    print(f"  saved -> {out_dir / 'tamper_demo.json'}")


def build_permalinks(disclosed: Disclosure, withheld: Disclosure, core: _CapturingCore, out_dir: Path) -> None:
    _section("Step 7 -- build browser-verifier permalinks (for the person-verify pass)")
    disclosed_capsules = [disclosed.envelopes[cid]["capsule"] for cid in disclosed.record_ids]
    disclosed_disclosures = {
        cid: disclosed.envelopes[cid]["disclosures"]
        for cid in disclosed.record_ids
        if disclosed.envelopes[cid]["disclosures"]
    }
    url_disclosed = build_url(disclosed_capsules, base_url=DEFAULT_BASE_URL, bundle=True, disclosures=disclosed_disclosures)

    withheld_capsules = [withheld.envelopes[cid]["capsule"] for cid in withheld.record_ids]
    withheld_disclosures = {
        cid: withheld.envelopes[cid]["disclosures"]
        for cid in withheld.record_ids
        if withheld.envelopes[cid]["disclosures"]
    }
    url_withheld = build_url(withheld_capsules, base_url=DEFAULT_BASE_URL, bundle=True, disclosures=withheld_disclosures)

    (out_dir / "permalinks.json").write_text(
        json.dumps({"bundle_disclosed_url": url_disclosed, "bundle_withheld_url": url_withheld}, indent=2)
    )
    print(f"  bundle_disclosed permalink length: {len(url_disclosed)} chars -- saved to permalinks.json")
    print(f"  bundle_withheld permalink length:  {len(url_withheld)} chars -- saved to permalinks.json")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out-dir", default="out", help="directory for all produced artifacts")
    parser.add_argument("--no-witness", action="store_true", help="skip the live public-witness registration")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = out_dir / "ledger.jsonl"

    print("=== Inspect sealed-log demo ===")

    eval_log_path = run_eval(out_dir)
    core = seal_log(eval_log_path, ledger_path)
    register_checkpoint(ledger_path, out_dir, use_witness=not args.no_witness)
    disclosed, withheld = build_bundles(ledger_path, core, out_dir)
    verify_bundles(disclosed, withheld, out_dir)
    tamper_demo(disclosed, out_dir)
    build_permalinks(disclosed, withheld, core, out_dir)

    _section("Done")
    print(f"  artifacts written to {out_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
