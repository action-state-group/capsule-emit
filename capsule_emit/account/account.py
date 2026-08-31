# SPDX-License-Identifier: Apache-2.0
"""The account object: what was selected, how it was derived, the asserted
result, and (for model-assisted derivations) the provenance that makes the
result re-adjudicable.

    Account = {
        selection(kind, coverage),
        derivation(definition_digest | registry_token, class),
        asserted_result,
        provenance,          # required iff derivation_class == model_assisted
    }

Construction is fail-closed on the two things the spec makes non-negotiable:

  * a ``model_assisted`` account with no provenance is REFUSED at construction
    (never minted as a provenance-free model claim);
  * a ``range``-kind selection's input identity is (coverage_root, range) and
    NEVER the per-member digests — the coverage object refuses per-member
    digests for a range.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .definition import (
    DERIVATION_DETERMINISTIC,
    DERIVATION_MODEL_ASSISTED,
    SELECTION_EXPLICIT_SET,
    SELECTION_RANGE,
    AccountDefinition,
)
from .errors import (
    AMBIGUOUS_DERIVATION_REFERENCE,
    MALFORMED_COVERAGE,
    MISSING_DERIVATION_REFERENCE,
    MISSING_PROVENANCE,
    PER_MEMBER_DIGEST_ON_RANGE,
    PROVENANCE_ON_DETERMINISTIC,
    AccountConstructionError,
)


@dataclass(frozen=True)
class Coverage:
    """What a selection covers.

    For a ``range`` selection the coverage is a ``coverage_root`` plus a
    ``[lo, hi]`` range — the input identity. It deliberately carries NO
    per-member digests: a range account cites the root and the span, not each
    member. For an ``explicit_set`` selection the coverage carries
    ``references`` (member citations); building that cross-reference path is a
    NOTED dependency, not implemented here.
    """

    coverage_root: str | None = None
    range: tuple[int, int] | None = None
    references: tuple[str, ...] = ()

    def input_identity(self, selection_kind: str) -> dict:
        """The identity of the inputs this coverage names — the thing a replay
        must reproduce to be comparing the same inputs.

        range        -> {"coverage_root", "range"}   (NEVER per-member digests)
        explicit_set -> {"references"}
        """
        if selection_kind == SELECTION_RANGE:
            return {"coverage_root": self.coverage_root, "range": list(self.range)}  # type: ignore[arg-type]
        return {"references": list(self.references)}


@dataclass(frozen=True)
class Derivation:
    """How the asserted result was derived.

    Cites the definition either by its ``definition_digest`` (definition-as-data,
    the default) OR by a ``registry_token`` (a later fold-name registry entry) —
    exactly one. ``derivation_class`` echoes the definition's class so a reader
    of just the account object knows which verification path applies.
    """

    derivation_class: str
    definition_digest: str | None = None
    registry_token: str | None = None


@dataclass(frozen=True)
class Provenance:
    """Model-assisted provenance — the fields that make a model judgment
    re-adjudicable. Required iff the account is ``model_assisted``.

    ``seed``/``entropy`` are the entropy binding: at least one MUST be present
    so a re-adjudication can reproduce (or explicitly note the non-determinism
    of) the sampling. ``re_adjudicable`` is the producer's honest marker that
    the judgment can be re-run against the same prompt/model.
    """

    model_id: str
    prompt_digest: str
    seed: Any = None
    entropy: str | None = None
    re_adjudicable: bool = True

    def to_document(self) -> dict:
        out: dict[str, Any] = {
            "model_id": self.model_id,
            "prompt_digest": self.prompt_digest,
            "re_adjudicable": self.re_adjudicable,
        }
        if self.seed is not None:
            out["seed"] = self.seed
        if self.entropy is not None:
            out["entropy"] = self.entropy
        return out


@dataclass(frozen=True)
class Selection:
    kind: str
    coverage: Coverage

    def input_identity(self) -> dict:
        return self.coverage.input_identity(self.kind)


@dataclass(frozen=True)
class Account:
    selection: Selection
    derivation: Derivation
    asserted_result: Any
    provenance: Provenance | None = None

    def to_document(self) -> dict:
        """The canonical account document (JCS-ready) — selection identity,
        derivation reference + class, the asserted result, and provenance when
        present. Stable enough to digest or seal onto a capsule."""
        derivation: dict[str, Any] = {"derivation_class": self.derivation.derivation_class}
        if self.derivation.definition_digest is not None:
            derivation["definition_digest"] = self.derivation.definition_digest
        if self.derivation.registry_token is not None:
            derivation["registry_token"] = self.derivation.registry_token
        doc: dict[str, Any] = {
            "selection": {
                "kind": self.selection.kind,
                "input_identity": self.selection.input_identity(),
            },
            "derivation": derivation,
            "asserted_result": self.asserted_result,
        }
        if self.provenance is not None:
            doc["provenance"] = self.provenance.to_document()
        return doc


def _validate_coverage(kind: str, coverage: Coverage) -> None:
    if kind == SELECTION_RANGE:
        if coverage.references:
            # The spec's headline mistake to avoid: a range account must not
            # cite members. Per-member references on a range selection are a
            # category error — the range's identity is root + span.
            raise AccountConstructionError(
                PER_MEMBER_DIGEST_ON_RANGE,
                "a range selection's input identity is (coverage_root, range); it must NOT "
                "carry per-member references/digests — that is only the explicit_set path",
            )
        if not isinstance(coverage.coverage_root, str) or not coverage.coverage_root:
            raise AccountConstructionError(MALFORMED_COVERAGE, "range selection requires a coverage_root")
        rng = coverage.range
        if (
            not isinstance(rng, tuple)
            or len(rng) != 2
            or not all(isinstance(x, int) and not isinstance(x, bool) for x in rng)
            or rng[0] > rng[1]
        ):
            raise AccountConstructionError(
                MALFORMED_COVERAGE, "range selection requires range=(lo, hi) with integer lo <= hi"
            )
    elif kind == SELECTION_EXPLICIT_SET:
        if not coverage.references:
            raise AccountConstructionError(
                MALFORMED_COVERAGE, "explicit_set selection requires a non-empty references[] (its member citations)"
            )
    else:  # defensive: Selection.kind should come from a validated definition
        raise AccountConstructionError(MALFORMED_COVERAGE, f"unknown selection kind {kind!r}")


def build_account(
    *,
    definition: AccountDefinition,
    selection: Selection,
    asserted_result: Any,
    provenance: Provenance | None = None,
    registry_token: str | None = None,
) -> Account:
    """Construct an ``Account`` for a definition, fail-closed.

    Refusals (each with a named ``reason``):
      * ``model_assisted`` definition + no provenance -> refused
        (``MISSING_PROVENANCE``);
      * ``deterministic`` definition + provenance supplied -> refused
        (``PROVENANCE_ON_DETERMINISTIC``) — provenance is meaningless there and
        its presence would misrepresent the derivation;
      * a ``range`` selection carrying per-member references -> refused
        (``PER_MEMBER_DIGEST_ON_RANGE``).

    The selection kind must match the definition's declared ``selection_kind``.
    The derivation cites the definition by its ``definition_digest`` (default),
    or by ``registry_token`` when one is supplied — never both, never neither.
    """
    if selection.kind != definition.selection_kind:
        raise AccountConstructionError(
            MALFORMED_COVERAGE,
            f"selection.kind {selection.kind!r} does not match definition.selection_kind "
            f"{definition.selection_kind!r}",
        )
    _validate_coverage(selection.kind, selection.coverage)

    cls = definition.derivation_class
    if cls == DERIVATION_MODEL_ASSISTED and provenance is None:
        raise AccountConstructionError(
            MISSING_PROVENANCE,
            "a model_assisted account requires provenance (model_id, prompt_digest, "
            "seed/entropy binding); a provenance-free model claim is refused at construction",
        )
    if cls == DERIVATION_DETERMINISTIC and provenance is not None:
        raise AccountConstructionError(
            PROVENANCE_ON_DETERMINISTIC,
            "a deterministic account carries no model provenance; supplying provenance would "
            "misrepresent the derivation class",
        )

    # Derivation reference: exactly one of definition_digest / registry_token.
    if registry_token is not None:
        definition_digest = None
    else:
        definition_digest = definition.definition_digest()
    if definition_digest is None and registry_token is None:
        raise AccountConstructionError(
            MISSING_DERIVATION_REFERENCE, "an account must cite its definition by digest or registry_token"
        )
    if definition_digest is not None and registry_token is not None:  # pragma: no cover - guarded above
        raise AccountConstructionError(
            AMBIGUOUS_DERIVATION_REFERENCE, "cite the definition by digest OR registry_token, not both"
        )

    return Account(
        selection=selection,
        derivation=Derivation(
            derivation_class=cls,
            definition_digest=definition_digest,
            registry_token=registry_token,
        ),
        asserted_result=asserted_result,
        provenance=provenance,
    )
