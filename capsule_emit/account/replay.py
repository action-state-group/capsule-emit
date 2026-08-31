# SPDX-License-Identifier: Apache-2.0
"""Replay / verify — the two derivation-class paths, and idempotent-on-replay.

  * ``deterministic``  : recompute+match. The caller supplies a ``recompute``
    callable that re-derives the result from the account's selection; verify
    passes iff the recomputed result equals the asserted result. The core does
    not know how to fetch a consumer's records — it knows the SHAPE of the
    contract (selection in, result out) and enforces the match.

  * ``model_assisted`` : provenance-verify. There is nothing to recompute (a
    model judgment is not a pure function of the inputs), so verify instead
    confirms the provenance is present, well-formed, and marked re-adjudicable —
    the account carries enough to RE-ADJUDICATE (re-run the model against the
    same prompt), not to recompute byte-for-byte.

Idempotent-on-replay: ``verify_account`` is a pure predicate over the account
(and, for deterministic, the supplied recompute) — running it twice yields the
same ``VerificationResult``; it mutates nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .account import Account, Selection
from .definition import (
    DERIVATION_DETERMINISTIC,
    DERIVATION_MODEL_ASSISTED,
    AccountDefinition,
)
from .errors import (
    DEFINITION_DIGEST_MISMATCH,
    DERIVATION_CLASS_MISMATCH,
    NOT_RE_ADJUDICABLE,
    RECOMPUTE_REQUIRED_DETERMINISTIC,
    RESULT_MISMATCH,
)

# A recompute callable re-derives the asserted result from the selection alone.
RecomputeFn = Callable[[Selection], Any]


@dataclass(frozen=True)
class VerificationResult:
    """The outcome of a replay/verify. ``ok`` is the verdict; ``reason`` is a
    named code when it failed (``None`` on success); ``method`` records which
    path ran so a reader knows a model_assisted pass was provenance-verified,
    not recomputed."""

    ok: bool
    method: str  # "recompute" | "provenance"
    reason: str | None = None
    detail: str | None = None


def _verify_deterministic(account: Account, recompute: RecomputeFn | None) -> VerificationResult:
    if recompute is None:
        return VerificationResult(
            ok=False,
            method="recompute",
            reason=RECOMPUTE_REQUIRED_DETERMINISTIC,
            detail="a deterministic account is verified by recompute+match; supply a recompute callable",
        )
    recomputed = recompute(account.selection)
    if recomputed == account.asserted_result:
        return VerificationResult(ok=True, method="recompute")
    return VerificationResult(
        ok=False,
        method="recompute",
        reason=RESULT_MISMATCH,
        detail=f"recomputed result {recomputed!r} != asserted {account.asserted_result!r}",
    )


def _verify_model_assisted(account: Account) -> VerificationResult:
    prov = account.provenance
    if prov is None:  # pragma: no cover - build_account refuses this at construction
        return VerificationResult(
            ok=False, method="provenance", reason="model_assisted_account_missing_provenance",
            detail="no provenance to verify",
        )
    if not prov.re_adjudicable:
        return VerificationResult(
            ok=False,
            method="provenance",
            reason=NOT_RE_ADJUDICABLE,
            detail="provenance marks the judgment as not re-adjudicable; it cannot be provenance-verified",
        )
    # Provenance-verify: the binding is present and re-adjudicable. We confirm
    # the fields that make a re-adjudication possible are all there — model id,
    # prompt digest, and an entropy binding (seed or entropy).
    if not prov.model_id or not prov.prompt_digest:
        return VerificationResult(
            ok=False, method="provenance", reason="model_assisted_account_missing_provenance",
            detail="provenance is missing model_id or prompt_digest",
        )
    if prov.seed is None and prov.entropy is None:
        return VerificationResult(
            ok=False, method="provenance", reason="model_assisted_account_missing_provenance",
            detail="provenance carries no seed/entropy binding",
        )
    return VerificationResult(ok=True, method="provenance")


def verify_account(
    account: Account,
    *,
    definition: AccountDefinition | None = None,
    recompute: RecomputeFn | None = None,
) -> VerificationResult:
    """Verify an account by the path its derivation class dictates.

    If ``definition`` is supplied, it is cross-checked against the account:
      * the account's ``derivation_class`` must equal the definition's;
      * when the account cites a ``definition_digest``, it must equal the
        definition's recomputed ``definition_digest`` (the definition-as-data
        binding — a swapped definition is caught here).

    Then:
      * deterministic  -> ``recompute`` is required; result must match.
      * model_assisted -> provenance is verified (present, well-formed,
        re-adjudicable); ``recompute`` is ignored.

    Pure and idempotent: no mutation; identical inputs give identical results.
    """
    cls = account.derivation.derivation_class

    if definition is not None:
        if definition.derivation_class != cls:
            return VerificationResult(
                ok=False,
                method="recompute" if cls == DERIVATION_DETERMINISTIC else "provenance",
                reason=DERIVATION_CLASS_MISMATCH,
                detail=f"account class {cls!r} != definition class {definition.derivation_class!r}",
            )
        cited = account.derivation.definition_digest
        if cited is not None and cited != definition.definition_digest():
            return VerificationResult(
                ok=False,
                method="recompute" if cls == DERIVATION_DETERMINISTIC else "provenance",
                reason=DEFINITION_DIGEST_MISMATCH,
                detail="account cites a definition_digest that does not match the supplied definition",
            )

    if cls == DERIVATION_MODEL_ASSISTED:
        return _verify_model_assisted(account)
    return _verify_deterministic(account, recompute)
