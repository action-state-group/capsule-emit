# SPDX-License-Identifier: Apache-2.0
"""Reduce Pydantic v2 models in a payload to their JSON dump (capsule-emit#426).

Shared by the core digest, ``verify_input_digest`` and the adapter base so a
model means the same thing everywhere. Pydantic is never imported here.
"""
from __future__ import annotations

import sys
from typing import Any

__all__ = ["PydanticPayloadError", "plain_json"]


class PydanticPayloadError(ValueError):
    """A Pydantic model passed to ``seal()`` has a field that is not JSON.

    Raised instead of guessing a value for it (no ``str()`` fallback); the
    message names the model and carries Pydantic's own reason.
    """


def _model_base() -> type | None:
    """Pydantic v2's ``BaseModel`` if pydantic is already imported, else None.

    A model instance can only exist if its module imported pydantic, so this
    never imports it (it stays an optional dependency). v1 models have no
    ``model_dump`` and are not handled.
    """
    # pydantic.main, not pydantic: a bare ``import pydantic`` loads submodules
    # lazily, and reading ``pydantic.BaseModel`` would trigger that import.
    # Any v2 model instance implies pydantic.main is already loaded.
    base = getattr(sys.modules.get("pydantic.main"), "BaseModel", None)
    return base if base is not None and hasattr(base, "model_dump") else None


def plain_json(value: Any) -> Any:
    """Replace Pydantic v2 models (top level, or inside dict/list/tuple) with
    ``model_dump(mode="json")``; leave every other value exactly as it was.

    Floats in the dump are not converted: the digest layer still rejects them
    (§5.1), so a model digests the same as its ``model_dump(mode="json")``.
    Whatever ``model_dump(mode="json")`` does applies as-is: field and model
    serializers run, and keys are field names (aliases only if a serializer
    says so).
    """
    base = _model_base()
    if base is None:
        return value
    if isinstance(value, base):
        try:
            return value.model_dump(mode="json")
        except Exception as exc:  # pydantic raises PydanticSerializationError
            raise PydanticPayloadError(
                f"{type(value).__name__} cannot be sealed: a field is not JSON "
                f"serializable ({exc}). Convert it to a JSON value yourself "
                "(e.g. a field_serializer) — seal() does not guess one."
            ) from exc
    # A container with no model inside is returned as the same object, so the
    # caller can tell "a model was converted" from "nothing changed".
    if isinstance(value, dict):
        out = {k: plain_json(v) for k, v in value.items()}
        return value if all(out[k] is v for k, v in value.items()) else out
    if isinstance(value, (list, tuple)):
        out_l = [plain_json(v) for v in value]
        if all(a is b for a, b in zip(out_l, value)):
            return value
        # A tuple holding a model becomes a list: a tuple would reach the
        # digest layer's legacy str()/repr() fallback and seal a float
        # silently instead of refusing it (§5.1).
        return out_l
    return value


def float_path(value: Any, path: str = "") -> str | None:
    """Path of the first float in an already-dumped payload (``order.score``,
    ``[0].x``), or None; used only to make the float error actionable."""
    if isinstance(value, float):
        return path or "<payload>"
    if isinstance(value, dict):
        for k, v in value.items():
            found = float_path(v, f"{path}.{k}" if path else str(k))
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            found = float_path(v, f"{path}[{i}]")
            if found:
                return found
    return None


def deep_float_path(value: Any, path: str = "", _seen: set[int] | None = None) -> str | None:
    """Path of the first float anywhere in ``value``, or None (capsule-emit#431).

    Unlike :func:`float_path` this also walks sets, dataclasses, Pydantic models
    and plain objects (their ``__dict__`` / ``__slots__``), because the legacy
    ``default=str`` digest fallback would otherwise seal a float through
    ``repr()`` when a non-JSON value sits beside it. Cycles are cut by id.
    """
    if isinstance(value, float):
        return path or "<payload>"
    if value is None or isinstance(value, (str, bytes, bytearray, int, bool)):
        return None
    seen = _seen if _seen is not None else set()
    if id(value) in seen:
        return None
    seen.add(id(value))
    children: list[tuple[str, Any]]
    if isinstance(value, dict):
        children = []
        for k, v in value.items():
            children.append((f"{path}.{k}" if path else str(k), k))
            children.append((f"{path}.{k}" if path else str(k), v))
    elif isinstance(value, (list, tuple)):
        children = [(f"{path}[{i}]", v) for i, v in enumerate(value)]
    elif isinstance(value, (set, frozenset)):
        children = [(f"{path}{{}}", v) for v in value]
    else:
        base = _model_base()
        dumped = None
        if base is not None and isinstance(value, base):
            try:
                dumped = value.model_dump(mode="json")
            except Exception:
                dumped = None
        if dumped is not None:
            return deep_float_path(dumped, path, seen)
        attrs: dict[str, Any] = dict(getattr(value, "__dict__", None) or {})
        for cls in type(value).__mro__:
            slots = cls.__dict__.get("__slots__", ())
            for name in (slots,) if isinstance(slots, str) else slots:
                if hasattr(value, name):
                    attrs.setdefault(name, getattr(value, name))
        children = [(f"{path}.{k}" if path else k, v) for k, v in attrs.items()]
    for p, child in children:
        found = deep_float_path(child, p, seen)
        if found:
            return found
    return None
