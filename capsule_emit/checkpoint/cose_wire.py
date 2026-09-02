# SPDX-License-Identifier: Apache-2.0
"""Module alias -- ``capsule_emit.checkpoint.cose_wire`` IS
``cll.checkpoint.cose_wire``. See ``capsule_emit/checkpoint/core.py`` and
``capsule_emit/checkpoint/__init__.py`` for why.
"""
import sys

from cll.checkpoint import cose_wire as _cose_wire

sys.modules[__name__] = _cose_wire
