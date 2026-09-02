# SPDX-License-Identifier: Apache-2.0
"""``bundle()`` — the hand-to-anyone artifact (O16 audit item 14, frozen
surface §2.5).

**Thin wrapper over ``cll.checkpoint.bundle`` (2026-09-01, W3.1 CLL
extraction).** The generic record/range-level disclosure-bundle mechanism
(MMR inclusion, checkpoint signature, consistency, witness stamps, COSE
wire — everything the LOG proves) now lives in ``cll.checkpoint.bundle``,
genericized (parameterized leaf-id/kind fields, takes pre-read entries
rather than reading a log file itself) so it carries no capsule vocabulary.
This module supplies the two things that ARE capsule-specific: reading
``capsule_emit.ledger``'s JSONL file format, and the receipt's own
content-authenticity check (a capsule's self-attested producer signature)
— composed on top of ``cll``'s generic
:func:`~cll.checkpoint.bundle.verify_bundle_log_integrity` rather than
duplicating it. See that function's docstring for why content-authenticity
is deliberately split out of the generic log-integrity check.

The verification chain documented in ``capsule_emit.checkpoint.emit`` is
four separate, caller-composed primitives: MMR inclusion, checkpoint
signature, TS receipt, and rollback/consistency. This module is what
assembles them into ONE standalone object for one record, per §2.5:

    {receipt, inclusion proof, covering checkpoint (+ its witness stamp),
     prior checkpoint, consistency proof between the two}

Once built, a ``Bundle`` is offline-verifiable by a stranger — no account,
no further help from the producer, no network (see :func:`verify_bundle`;
witness-stamp re-confirmation is a separate, explicitly optional step since
it may need a network fetch of the Transparency Service's public key). It
gives the two-sided append bracket the frozen surface names (§2.4): the
record provably entered the log no later than the covering checkpoint's
stamp and no earlier than the prior checkpoint (it wasn't in that one yet)
— except for a record covered by the very first checkpoint a log ever had,
where there is no prior checkpoint to bound the lower side (``prior_checkpoint``
and ``consistency_proof`` are both ``None``; ``checkpoint.prev_size == 0``
says so honestly rather than gap-filling one).

A bundle is buildable at any later time for any record the log still
retains — this module never caches or persists one; every call re-reads the
ledger and re-derives the MMR fresh from it, exactly the way
``capsule_emit.witness`` built it at production time (each raw ledger line —
capsule or checkpoint-stamp alike — is one leaf, in append order; see
``capsule_emit.ledger``'s module docstring).

Deliberately NOT imported from ``capsule_emit/__init__.py`` — like
``capsule_emit.checkpoint`` and ``capsule_emit.status``, this stays
structurally opt-in (``from capsule_emit.bundle import bundle``) so a bare
``import capsule_emit`` never pays for the MMR/checkpoint subpackage (see
``tests/test_checkpoint_layer0_cost.py``).
"""
from __future__ import annotations

from cll.checkpoint.bundle import Bundle, BundleError

__all__ = ["Bundle", "BundleError", "bundle", "verify_bundle"]


def bundle(path, capsule_id: str) -> Bundle:
    """Build a standalone-verifiable :class:`Bundle` for one record in the
    JSONL ledger at ``path``.

    Re-reads the whole ledger and re-derives the MMR fresh each call — this
    never assumes an in-process ``MmrLedger`` is warm (bundle can be built by
    a completely different process than the one that sealed the record).

    Raises :class:`BundleError` if ``capsule_id`` doesn't resolve to exactly
    one record, or if that record is not yet covered by any checkpoint (a
    record only becomes bundle-able once a checkpoint's ``mmr_size`` reaches
    it — see ``capsule_emit.status`` for a read-only way to check that lag
    before calling this).
    """
    from cll.checkpoint.bundle import bundle as _cll_bundle

    from .ledger import NON_CAPSULE_KINDS, read_ledger_entries

    entries = read_ledger_entries(path)
    if not entries:
        raise BundleError(f"{path}: empty or not found")
    return _cll_bundle(entries, capsule_id, non_leaf_kinds=frozenset(NON_CAPSULE_KINDS))


def verify_bundle(
    b: Bundle, *, trust_anchor: dict[str, bytes | str] | None = None
) -> tuple[bool, list[str]]:
    """Pure, offline, total verification of a standalone :class:`Bundle` —
    no reader, no network, never raises. ``trust_anchor``
    [verify-threestate-trustanchor] is an optional caller-supplied mapping
    of ``ts_url -> pubkey_pem`` — one or several pins for Transparency
    Services the caller trusts beyond the built-in pinned default witness
    (``capsule_emit.checkpoint.DEFAULT_TS_URL`` /
    ``DEFAULT_TS_PUBLIC_KEY_PEM``, always consulted regardless of
    ``trust_anchor``). Confirms every link the two-sided append bracket
    depends on:

      1. the receipt's own ``capsule_id`` matches the leaf the inclusion
         proof was built for, AND is recomputed from the receipt's own
         content (not just compared as an opaque label) — a bundle whose
         receipt body was tampered but whose ``capsule_id`` was left alone
         is caught here; AND its self-attested producer signature verifies
         (``capsule_emit.signing.verify_capsule_signature``) — a receipt
         body rewritten with a matching, recomputed ``capsule_id`` (the
         [verify-checks-producer-signature] forgery, replayed against a
         bundle) is caught here instead. This step is capsule-specific and
         lives in THIS module (see :func:`cll.checkpoint.bundle.
         verify_bundle_log_integrity`'s docstring for why);
      2–6. everything the LOG proves — inclusion, checkpoint signature,
         consistency (labeled honestly: anti-REWRITE, never "no fork"),
         witness stamp tri-state, COSE wire cross-check — delegated to
         :func:`cll.checkpoint.bundle.verify_bundle_log_integrity`
         unchanged; see that function's docstring for the full description
         of each check.

    Returns ``(ok, errors)`` — ``ok`` is false iff a FATAL problem was
    found; ``errors`` also carries non-fatal notices, in the order: this
    module's step-1 findings, then ``cll``'s step 2–6 findings.
    """
    from cll.checkpoint.bundle import verify_bundle_log_integrity

    from .canonicalization import compute_capsule_id
    from .signing import verify_capsule_signature

    step1_errors: list[str] = []
    try:
        if b.receipt.get("capsule_id") != b.capsule_id:
            step1_errors.append("receipt.capsule_id does not match bundle.capsule_id")

        try:
            recomputed_capsule_id = compute_capsule_id(b.receipt)
        except Exception as exc:
            step1_errors.append(f"receipt {b.capsule_id} content could not be hashed: {exc}")
        else:
            if recomputed_capsule_id != b.receipt.get("capsule_id"):
                step1_errors.append(
                    f"receipt {b.capsule_id} does not hash to its own capsule_id -- "
                    "receipt body was tampered"
                )
        if not verify_capsule_signature(b.receipt):
            step1_errors.append(
                f"receipt {b.capsule_id} signature does not verify -- receipt content, signature, "
                "or key_id was tampered, forged, or unsigned"
            )
    except Exception as exc:  # noqa: BLE001 — pure verifier, never raises
        return False, step1_errors + [f"unexpected error: {exc}"]

    log_ok, log_messages = verify_bundle_log_integrity(b, trust_anchor=trust_anchor)
    return (not step1_errors) and log_ok, step1_errors + log_messages
