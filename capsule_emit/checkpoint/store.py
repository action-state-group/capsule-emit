# SPDX-License-Identifier: Apache-2.0
"""Module alias -- ``capsule_emit.checkpoint.store`` IS ``cll.checkpoint.store``.
See ``capsule_emit/checkpoint/core.py`` and ``capsule_emit/checkpoint/__init__.py``
for why.
"""
import sys

from cll.checkpoint import store as _store

sys.modules[__name__] = _store
