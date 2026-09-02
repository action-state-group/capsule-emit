# SPDX-License-Identifier: Apache-2.0
"""Module alias -- ``capsule_emit.checkpoint.emit`` IS ``cll.checkpoint.emit``.
See ``capsule_emit/checkpoint/core.py`` and ``capsule_emit/checkpoint/__init__.py``
for why.

**Removal horizon: 0.8.** Slated for removal in capsule-emit 0.8 -- callers
should migrate to ``import cll.checkpoint.emit`` directly. Importing this
module emits a ``DeprecationWarning``.
"""
import sys
import warnings

from cll.checkpoint import emit as _emit

warnings.warn(
    "capsule_emit.checkpoint.emit is a compatibility alias for "
    "cll.checkpoint.emit and will be removed in capsule-emit 0.8 -- "
    "import cll.checkpoint.emit directly instead.",
    DeprecationWarning,
    stacklevel=2,
)

sys.modules[__name__] = _emit
