# SPDX-License-Identifier: Apache-2.0
"""``status`` — ladder position, checkpoint/stamp lag, and (unless
``--offline``) a read-only witness re-check.

O16 audit item 17 ("status's fetch-fold", frozen-surface §7): there was no
``status`` verb and no separate ``fetch`` verb to fold into it -- this module
is the net-new implementation of both in one. ``status`` answers, from a
ledger alone: how many capsules are sealed, how many checkpoints exist and
what ladder rung each is on (``self-attested``/``witnessed`` -- see
``capsule_emit.checkpoint.Grade``), and the two honest lag numbers the
frozen surface names: records awaiting the next checkpoint, and checkpoints
still awaiting a witness stamp.

**Reads never write** (the read-verb family's standing rule). Unless
``offline=True``, the only network call this makes is a GET of a
Transparency Service's public key (``capsule_emit.checkpoint
.verify_receipt_offline``) to independently re-confirm a witness receipt
the ledger *already* holds. It never registers a *new* stamp for a
self-attested checkpoint -- that is a write (it creates a TS log entry),
and ``push`` -- not ``status`` -- is the verb that writes checkpoints.

**O16-03: the witness kill switch also gates this fetch.** ``--offline`` is
one way to skip the re-check; ``witness=False`` / ``CAPSULE_WITNESS=off`` is
the other, and it applies here even without ``--offline`` -- the kill switch
is meant to be a single, absolute "zero network egress" guarantee (frozen
surface §1a.3, "local-only"), and a `status` call that quietly re-opened a
network path around it would violate that. See ``docs/checkpoint.md``'s
"Kill switch scope" section.
"""
from __future__ import annotations

from typing import Any


def compute_status(path: str, *, offline: bool = False) -> dict:
    """Read ``path`` (a JSONL ledger) and report its ladder position.

    Returns a plain, JSON-serializable dict -- see the module docstring for
    what each field means. ``offline=True`` skips the network re-check of
    the latest checkpoint's witness receipt(s); ``offline=False`` (default)
    performs it.
    """
    from . import witness
    from .checkpoint import CheckpointRecord, Grade, core
    from .ledger import CHECKPOINT_STAMP_KIND, read_ledger_entries

    entries = read_ledger_entries(path)
    capsules = [e for e in entries if e.get("kind") != CHECKPOINT_STAMP_KIND]
    checkpoints = [
        CheckpointRecord.from_dict(e["checkpoint"])
        for e in entries
        if e.get("kind") == CHECKPOINT_STAMP_KIND
    ]

    last_cp = checkpoints[-1] if checkpoints else None
    covered_leaves = core.leaf_count(last_cp.mmr_size) if last_cp is not None else 0

    if last_cp is None:
        records_awaiting_checkpoint = len(capsules)
    else:
        # Everything past the leaf count the latest checkpoint actually
        # covers -- INCLUDING that checkpoint's own stamp entry (appended
        # after its mmr_size was fixed, so it is genuinely uncovered until
        # the *next* checkpoint folds it in, per item 16). Only capsules
        # count as "records" here; a checkpoint's own stamp backlog is
        # reported separately below.
        records_awaiting_checkpoint = sum(
            1 for e in entries[covered_leaves:] if e.get("kind") != CHECKPOINT_STAMP_KIND
        )

    checkpoints_awaiting_stamp = sum(1 for cp in checkpoints if cp.grade() == Grade.SELF_ATTESTED)
    witnessing_enabled_now = witness.witness_enabled(None)

    result: dict[str, Any] = {
        "path": str(path),
        "offline": offline,
        "capsule_count": len(capsules),
        "checkpoint_count": len(checkpoints),
        "records_awaiting_checkpoint": records_awaiting_checkpoint,
        "checkpoints_awaiting_stamp": checkpoints_awaiting_stamp,
        "witnessing_enabled_now": witnessing_enabled_now,
        "witnessing_mode_now": witness.witness_mode(None),
        "latest_checkpoint": None,
    }

    if last_cp is not None:
        witnesses_info = []
        # O16-03: the kill switch (CAPSULE_WITNESS=off) skips this network
        # re-check even when --offline was NOT passed -- it is the one
        # switch that zeroes all egress, not just an alias for --offline.
        skip_network_recheck = offline or not witnessing_enabled_now
        for w in last_cp.witnesses:
            info: dict[str, Any] = {"ts_url": w.ts_url, "is_stub": w.is_stub, "confirmed": None}
            if w.is_stub:
                # Never re-checked over the network -- a stub stamp was never
                # registered with anything, so there is nothing to confirm.
                info["confirmed"] = False
            elif not skip_network_recheck:
                from .checkpoint import verify_receipt_offline

                ok, errors = verify_receipt_offline(w, ts_base_url=w.ts_url)
                info["confirmed"] = ok
                if not ok:
                    info["errors"] = errors
            witnesses_info.append(info)
        # Stub scream (frozen surface §1a.4): the latest checkpoint's grade
        # already stays self-attested when its witnesses are stub-only (see
        # CheckpointRecord.grade()) -- this flag is what render_status uses
        # to make that loud instead of silently correct.
        stub_witness_only = bool(last_cp.witnesses) and all(w.is_stub for w in last_cp.witnesses)
        result["latest_checkpoint"] = {
            "mmr_size": last_cp.mmr_size,
            "leaf_count": covered_leaves,
            "grade": last_cp.grade().value,
            "stub_witness": stub_witness_only,
            "timestamp": last_cp.timestamp,
            "key_id": last_cp.key_id,
            "witnesses": witnesses_info,
        }

    return result


def render_status(status: dict, *, out: Any = None) -> None:
    """Human-readable rendering of :func:`compute_status`'s result."""
    import sys

    if out is None:
        out = sys.stdout

    print(f"\ncapsule-emit status: {status['path']}\n", file=out)
    print(f"  {'capsules sealed':<30}{status['capsule_count']}", file=out)
    print(f"  {'checkpoints':<30}{status['checkpoint_count']}", file=out)

    cp = status["latest_checkpoint"]
    if cp is None:
        print(f"  {'latest checkpoint':<30}none yet", file=out)
    else:
        print(f"  {'latest checkpoint grade':<30}{cp['grade']}", file=out)
        if cp.get("stub_witness"):
            print(
                f"  {'':<30}⚠ STUB WITNESS — proves nothing beyond self-attested",
                file=out,
            )
        print(
            f"  {'latest checkpoint covers':<30}{cp['leaf_count']} leaf(ves) "
            f"(mmr_size={cp['mmr_size']}, at {cp['timestamp']})",
            file=out,
        )

    print(f"  {'records awaiting checkpoint':<30}{status['records_awaiting_checkpoint']}", file=out)
    print(f"  {'checkpoints awaiting stamp':<30}{status['checkpoints_awaiting_stamp']}", file=out)
    mode = status.get("witnessing_mode_now", "on" if status["witnessing_enabled_now"] else "off")
    mode_display = "STUB (proves nothing beyond self-attested)" if mode == "stub" else mode
    print(f"  {'witnessing (this process)':<30}{mode_display}", file=out)

    if cp is not None and cp["witnesses"]:
        print("\n  witnesses (latest checkpoint):", file=out)
        for w in cp["witnesses"]:
            if w.get("is_stub"):
                state = "STUB — not a real Transparency Service"
            elif not status["witnessing_enabled_now"]:
                state = "unconfirmed (witness disabled)"
            elif status["offline"]:
                state = "unconfirmed (--offline)"
            elif w["confirmed"]:
                state = "confirmed"
            else:
                detail = f" ({w['errors'][0]})" if w.get("errors") else ""
                state = f"NOT confirmed{detail}"
            print(f"    {w['ts_url']:<40}{state}", file=out)

    print(file=out)
