# SPDX-License-Identifier: Apache-2.0
"""Module alias -- ``capsule_emit.checkpoint.cose_wire`` IS
``cll.checkpoint.cose_wire``. See ``capsule_emit/checkpoint/core.py`` and
``capsule_emit/checkpoint/__init__.py`` for why.

**Removal horizon: 0.8.** Slated for removal in capsule-emit 0.8 -- callers
should migrate to ``import cll.checkpoint.cose_wire`` directly. Importing
this module emits a ``DeprecationWarning``.
"""
import sys
import warnings

from cll.checkpoint import cose_wire as _cose_wire

warnings.warn(
    "capsule_emit.checkpoint.cose_wire is a compatibility alias for "
    "cll.checkpoint.cose_wire and will be removed in capsule-emit 0.8 -- "
    "import cll.checkpoint.cose_wire directly instead.",
    DeprecationWarning,
    stacklevel=2,
)

sys.modules[__name__] = _cose_wire
