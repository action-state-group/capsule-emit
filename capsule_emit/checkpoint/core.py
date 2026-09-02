# SPDX-License-Identifier: Apache-2.0
"""Module alias -- ``capsule_emit.checkpoint.core`` IS ``cll.checkpoint.core``.

See ``capsule_emit/checkpoint/__init__.py`` for why this package graduated
to the ``cll`` library (W3.1 CLL extraction, 2026-09-01) and why this is a
true alias (``sys.modules`` substitution), not a ``from ... import *``
re-export: several tests and internal call sites monkeypatch module-level
attributes (e.g. ``DEFAULT_TS_URL``) on ``capsule_emit.checkpoint.<name>``
and expect the real functions living in ``cll.checkpoint.<name>`` to see
the patched value -- true only if both names resolve to the SAME module
object, not two independently-initialized copies of its names.

**Removal horizon: 0.8.** This compat alias is slated for removal in
capsule-emit 0.8 -- callers should migrate to ``import cll.checkpoint.core``
directly. Importing this module emits a ``DeprecationWarning``.
"""
import sys
import warnings

from cll.checkpoint import core as _core

warnings.warn(
    "capsule_emit.checkpoint.core is a compatibility alias for "
    "cll.checkpoint.core and will be removed in capsule-emit 0.8 -- "
    "import cll.checkpoint.core directly instead.",
    DeprecationWarning,
    stacklevel=2,
)

sys.modules[__name__] = _core
