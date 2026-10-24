# SPDX-License-Identifier: Apache-2.0
"""Capsule verification, under the names this package has always used.

``verify_capsule`` and ``verify_store`` are ``agent_action_capsule.verify``
and ``agent_action_capsule.verify_store`` themselves, so a capsule gets the
same verdict whichever package a caller imports. This package produces
records; agent-action-capsule verifies them.
"""

from __future__ import annotations

from agent_action_capsule import verify as verify_capsule
from agent_action_capsule import verify_store

__all__ = ["verify_capsule", "verify_store"]
