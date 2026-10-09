# SPDX-License-Identifier: Apache-2.0
"""Module alias -- ``capsule_emit.checkpoint.index`` IS ``cll.checkpoint.index``.
See ``capsule_emit/checkpoint/core.py`` and ``capsule_emit/checkpoint/__init__.py``
for why.

**Removal horizon: 0.8.** Slated for removal in capsule-emit 0.8 -- callers
should migrate to ``import cll.checkpoint.index`` directly. Importing this
module emits a ``DeprecationWarning``.
"""
import sys
import warnings

from cll.checkpoint import index as _index

warnings.warn(
    "capsule_emit.checkpoint.index is a compatibility alias for "
    "cll.checkpoint.index and will be removed in capsule-emit 0.8 -- "
    "import cll.checkpoint.index directly instead.",
    DeprecationWarning,
    stacklevel=2,
)

sys.modules[__name__] = _index
