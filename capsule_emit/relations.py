# SPDX-License-Identifier: Apache-2.0
"""The ``chain.relation`` values capsule-emit writes, and the legacy tokens it
still reads.

Writers emit only the five values registered in the Agent Action Capsule
registry (agent-action-capsule ``spec/REGISTRY.md`` section 6). Records that
earlier releases or examples wrote with an unregistered token are still read:
each legacy token maps to the registered value it meant. Nothing writes a
legacy token any more.
"""
from __future__ import annotations

__all__ = ["REGISTERED_RELATIONS", "LEGACY_RELATION_ALIASES", "registered_meaning"]

#: The registered chain.relation values (REGISTRY.md section 6).
REGISTERED_RELATIONS = frozenset({"follows", "confirms", "supersedes", "epoch_opens", "duplicates"})

#: Deployed legacy aliases: unregistered tokens found in records already
#: written, and the registered value each is read as.
#:
#: - ``sequence``: a bare next-link (the registry's own documented alias of
#:   ``follows``).
#: - ``resolves``: an approval or denial closing a blocked capsule
#:   (:mod:`capsule_emit.approval`), terminal.
#: - ``escalates``: an escalation closing or replacing the parent's open
#:   state, terminal (the registry lists escalation under ``supersedes``).
#: - ``adjudicates``: a twin-comparison verdict over the compared half
#:   (:mod:`capsule_emit.adjudication`); it records an observation of the
#:   half and leaves its open state as it was.
#: - ``assesses``: a judge's verdict citing its subject; an observation of
#:   the subject, like ``adjudicates``.
LEGACY_RELATION_ALIASES = {
    "sequence": "follows",
    "resolves": "supersedes",
    "escalates": "supersedes",
    "adjudicates": "confirms",
    "assesses": "confirms",
}


def registered_meaning(relation: object) -> str | None:
    """The registered value ``relation`` stands for: itself when registered,
    the mapped value for a legacy alias, else ``None``."""
    if not isinstance(relation, str):
        return None
    if relation in REGISTERED_RELATIONS:
        return relation
    return LEGACY_RELATION_ALIASES.get(relation)
