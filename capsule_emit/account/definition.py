# SPDX-License-Identifier: Apache-2.0
"""Fold-definition-as-DATA and the ``definition_digest``.

An account definition is a canonical *document*: a name, a selection-kind, the
field set it reads, a bounded predicate spec, and a ``derivation_class``. The
``definition_digest`` is SHA-256 over the JCS bytes of that document — never
over code. This is the load-bearing property of the whole core: two
implementations (this repo, and any consumer that re-imports this module) that
share the same definition document share the same digest, and altering an
implementation's internals MUST NOT change the digest.

JCS canonicalization reuses ``agent_action_capsule.canonical.json_digest`` — the
same canonicalization the capsule format itself uses — rather than
reimplementing it, so there is exactly one JCS/digest implementation across the
neutral stack.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_action_capsule.canonical import FloatInDigestError, UnsafeIntegerError, json_digest

from .errors import (
    DUPLICATE_READ_PATH,
    EMPTY_READS,
    FLOAT_IN_DEFINITION,
    MALFORMED_DEFINITION,
    UNBOUNDED_PREDICATE_OP,
    UNDECLARED_PREDICATE_FIELD,
    UNKNOWN_DERIVATION_CLASS,
    UNKNOWN_SELECTION_KIND,
    UNSAFE_INTEGER_IN_DEFINITION,
    AccountDefinitionError,
)

# ---------------------------------------------------------------------------
# derivation_class — the split the whole core turns on.
#
#   deterministic  -- the asserted result is a pure function of the selected
#                     inputs; verify by recompute+match.
#   model_assisted -- the asserted result came from a model judgment; verify by
#                     provenance (model id, prompt digest, seed/entropy binding)
#                     and re-adjudicability, NOT by recompute. A model_assisted
#                     account with no provenance is refused at construction
#                     (see ``account.build_account``).
# ---------------------------------------------------------------------------
DERIVATION_DETERMINISTIC = "deterministic"
DERIVATION_MODEL_ASSISTED = "model_assisted"
DERIVATION_CLASSES = frozenset({DERIVATION_DETERMINISTIC, DERIVATION_MODEL_ASSISTED})

# ---------------------------------------------------------------------------
# selection_kind — how the account names the inputs it accounts over.
#
#   range         -- a contiguous coverage: input identity is (coverage_root,
#                    range), NEVER the per-member digests. A range account cites
#                    WHAT it covered (the root the range lives under and the
#                    [lo, hi] span), not each member.
#   explicit_set  -- an enumerated set: cites its members via ``references[]``.
#                    (That cross-reference path is a NOTED dependency of the
#                    core, not built here — see ``account.Coverage``.)
# ---------------------------------------------------------------------------
SELECTION_RANGE = "range"
SELECTION_EXPLICIT_SET = "explicit_set"
SELECTION_KINDS = frozenset({SELECTION_RANGE, SELECTION_EXPLICIT_SET})

# Bounded predicate operations only — equality, ranges, set membership, prefix.
# No regex, no user-supplied code (mirrors the ledger fold filter grammar).
BOUNDED_PREDICATE_OPS = frozenset({"eq", "ne", "in", "not_in", "prefix", "gt", "gte", "lt", "lte"})


@dataclass(frozen=True)
class Predicate:
    """One bounded predicate clause over a declared read field (data, not code)."""

    field: str
    op: str
    value: Any


@dataclass(frozen=True)
class AccountDefinition:
    """The canonical definition DOCUMENT.

    ``definition_digest`` is computed over ``canonical_document()`` — the fields
    below and nothing else. Implementation internals (how a consumer stores or
    evaluates this) are deliberately NOT part of the document, so changing them
    cannot move the digest.
    """

    name: str
    selection_kind: str
    reads: tuple[str, ...]
    derivation_class: str
    predicate: tuple[Predicate, ...] = ()

    def read_fields(self) -> frozenset[str]:
        return frozenset(self.reads)

    def canonical_document(self) -> dict:
        """The JCS-canonicalizable definition document — drives ``definition_digest``.

        This is the ONLY thing the digest sees. Keys are the definition's own
        semantic fields; there is no implementation detail here by design.
        """
        out: dict[str, Any] = {
            "name": self.name,
            "selection_kind": self.selection_kind,
            "reads": list(self.reads),
            "derivation_class": self.derivation_class,
        }
        if self.predicate:
            out["predicate"] = [{"field": p.field, "op": p.op, "value": p.value} for p in self.predicate]
        return out

    def definition_digest(self) -> str:
        """SHA-256 over the JCS bytes of ``canonical_document()``.

        Reuses ``agent_action_capsule.canonical.json_digest`` (the one JCS
        implementation) so a consumer that re-imports this module produces a
        byte-identical digest over the same document.
        """
        try:
            return json_digest(self.canonical_document())
        except FloatInDigestError as exc:
            raise AccountDefinitionError(FLOAT_IN_DEFINITION, str(exc)) from exc
        except UnsafeIntegerError as exc:
            raise AccountDefinitionError(UNSAFE_INTEGER_IN_DEFINITION, str(exc)) from exc


def _reject_float(value: Any, context: str) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, float):
        raise AccountDefinitionError(
            FLOAT_IN_DEFINITION,
            f"{context} is a float ({value!r}); floats are forbidden in a digest-bearing "
            "definition (JSON float serialization is not cross-implementation deterministic)",
        )
    if isinstance(value, (list, tuple)):
        for item in value:
            _reject_float(item, context)


def parse_definition(data: Any) -> AccountDefinition:
    """Validate a plain dict (a definition document) into an ``AccountDefinition``.

    Rejections carry a named ``reason`` so a conformance vector can pin it.
    """
    if not isinstance(data, dict):
        raise AccountDefinitionError(MALFORMED_DEFINITION, "definition must be a mapping")

    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise AccountDefinitionError(MALFORMED_DEFINITION, "name is required and must be a non-empty string")

    selection_kind = data.get("selection_kind")
    if selection_kind not in SELECTION_KINDS:
        raise AccountDefinitionError(
            UNKNOWN_SELECTION_KIND,
            f"selection_kind {selection_kind!r} must be one of {sorted(SELECTION_KINDS)}",
        )

    derivation_class = data.get("derivation_class")
    if derivation_class not in DERIVATION_CLASSES:
        raise AccountDefinitionError(
            UNKNOWN_DERIVATION_CLASS,
            f"derivation_class {derivation_class!r} must be one of {sorted(DERIVATION_CLASSES)}",
        )

    raw_reads = data.get("reads")
    if not isinstance(raw_reads, list) or not raw_reads:
        raise AccountDefinitionError(EMPTY_READS, "reads must be a non-empty list of field paths")
    reads: list[str] = []
    seen: set[str] = set()
    for path in raw_reads:
        if not isinstance(path, str) or not path:
            raise AccountDefinitionError(MALFORMED_DEFINITION, f"each reads entry must be a non-empty string: {path!r}")
        if path in seen:
            raise AccountDefinitionError(DUPLICATE_READ_PATH, f"read path {path!r} declared more than once")
        seen.add(path)
        reads.append(path)
    declared = set(reads)

    raw_predicate = data.get("predicate") or []
    if not isinstance(raw_predicate, list):
        raise AccountDefinitionError(MALFORMED_DEFINITION, "predicate must be a list")
    predicate: list[Predicate] = []
    for clause in raw_predicate:
        if not isinstance(clause, dict):
            raise AccountDefinitionError(MALFORMED_DEFINITION, f"predicate clause must be a mapping: {clause!r}")
        field, op, value = clause.get("field"), clause.get("op"), clause.get("value")
        if op not in BOUNDED_PREDICATE_OPS:
            raise AccountDefinitionError(
                UNBOUNDED_PREDICATE_OP,
                f"predicate op {op!r} is not bounded; allowed: {sorted(BOUNDED_PREDICATE_OPS)} "
                "(no regex, no user-supplied code)",
            )
        if field not in declared:
            raise AccountDefinitionError(UNDECLARED_PREDICATE_FIELD, f"predicate references undeclared field {field!r}")
        if op in ("in", "not_in") and not isinstance(value, list):
            raise AccountDefinitionError(UNBOUNDED_PREDICATE_OP, f"predicate op {op!r} requires a list value")
        _reject_float(value, f"predicate[{field!r}].value")
        predicate.append(Predicate(field=field, op=op, value=value))

    return AccountDefinition(
        name=name,
        selection_kind=selection_kind,
        reads=tuple(reads),
        derivation_class=derivation_class,
        predicate=tuple(predicate),
    )
