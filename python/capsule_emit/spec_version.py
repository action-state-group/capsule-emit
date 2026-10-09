# SPDX-License-Identifier: Apache-2.0
"""The draft revision capsule-emit stamps, and the revisions it accepts.

A producer conforming to draft -05 emits ``spec_version`` -05; a verifier
accepts -04 and -05 (the two revisions that define ``format_version`` "4")
and never rejects a record solely for carrying either. ``spec_version``
selects no digest or verification algorithm (canonicalization follows
``format_version`` + ``canonicalization_id``), and an unrecognized value is
informational, never by itself a rejection -- so no capsule-emit verifier
branches on it. Same rule, same names, as ``agent_action_capsule.emit``
(``DEFAULT_SPEC_VERSION`` / ``ACCEPTED_SPEC_VERSIONS``).

capsule-emit passes :data:`SPEC_VERSION` to ``agent_action_capsule.emit()``
explicitly rather than inheriting that library's default, so what it stamps
does not depend on which ``agent-action-capsule`` release is installed.
"""
from __future__ import annotations

__all__ = ["SPEC_VERSION", "ACCEPTED_SPEC_VERSIONS"]

#: The revision every capsule this library produces carries.
SPEC_VERSION: str = "draft-mih-scitt-agent-action-capsule-05"

#: Revisions a verifier accepts for format 4. Informational: nothing verifies
#: differently, or fails, because of which one (or another) a record carries.
ACCEPTED_SPEC_VERSIONS: tuple[str, ...] = (
    "draft-mih-scitt-agent-action-capsule-04",
    SPEC_VERSION,
)
