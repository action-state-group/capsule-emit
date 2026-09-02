# SPDX-License-Identifier: Apache-2.0
"""Module alias -- ``capsule_emit.checkpoint.index`` IS ``cll.checkpoint.index``.
See ``capsule_emit/checkpoint/core.py`` and ``capsule_emit/checkpoint/__init__.py``
for why.
"""
import sys

from cll.checkpoint import index as _index

sys.modules[__name__] = _index
