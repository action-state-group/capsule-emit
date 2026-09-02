# SPDX-License-Identifier: Apache-2.0
"""Module alias -- ``capsule_emit.checkpoint.emit`` IS ``cll.checkpoint.emit``.
See ``capsule_emit/checkpoint/core.py`` and ``capsule_emit/checkpoint/__init__.py``
for why.
"""
import sys

from cll.checkpoint import emit as _emit

sys.modules[__name__] = _emit
