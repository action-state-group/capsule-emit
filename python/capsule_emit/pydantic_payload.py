# SPDX-License-Identifier: Apache-2.0
"""Payload helpers for the digest layer: Pydantic v2 models reduced to their
JSON dump (capsule-emit#426), and the float walk that guards the legacy
``default=str`` fallback (capsule-emit#431).

Shared by the core digest, ``verify_input_digest`` and the adapter base so a
model means the same thing everywhere. Pydantic is never imported here.
"""
from __future__ import annotations

import dataclasses
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


def _slot_names(value: Any) -> list[str]:
    names: list[str] = []
    for cls in type(value).__mro__:
        slots = cls.__dict__.get("__slots__", ())
        for name in (slots,) if isinstance(slots, str) else slots:
            if name.startswith("__") and not name.endswith("__"):
                name = f"_{cls.__name__.lstrip('_')}{name}"  # name mangling
            names.append(name)
    return names


def deep_float_path(root: Any) -> str | None:
    """Path of the first Python float in the standard containers of ``root``, or
    None (capsule-emit#431).

    The legacy ``default=str`` digest fallback would seal such a float through
    ``repr()``. This walks only types the payload contract already knows: dict
    (keys and values), list, tuple, set, frozenset, dataclass instances and
    Pydantic v2 models (their dump and their own fields, since ``repr`` shows
    fields a serializer hides). Any other object is never touched, so it keeps
    exactly its legacy behaviour; a float held only inside such an object, such
    as a numpy array, is a known limit. Iterative, so a deep payload cannot
    raise ``RecursionError``; visited objects are kept alive so a recycled
    ``id()`` cannot hide a later one.
    """
    base = _model_base()
    seen: set[int] = set()
    keep: list[Any] = []
    stack: list[tuple[Any, str]] = [(root, "")]
    while stack:
        value, path = stack.pop()
        if isinstance(value, float):
            return path or "<payload>"
        is_dc = dataclasses.is_dataclass(value) and not isinstance(value, type)
        if value is None or id(value) in seen:
            continue
        if isinstance(value, (str, bytes, int)) and not is_dc:
            continue
        seen.add(id(value))
        keep.append(value)
        if isinstance(value, dict):
            for k, v in value.items():
                # never call a key's own __str__: it may raise on a float-free payload
                sub = f"{path}.{k}" if isinstance(k, (str, int)) else f"{path}.<key>"
                stack.append((k, sub))
                stack.append((v, sub))
        elif isinstance(value, (list, tuple, set, frozenset)):
            for i, v in enumerate(value):
                stack.append((v, f"{path}[{i}]"))
        if base is not None and isinstance(value, base):
            try:
                stack.append((value.model_dump(mode="json"), path))
            except Exception:
                pass
            stack.append((dict(value.__dict__), path))
        if is_dc:
            for f in dataclasses.fields(value):
                sub = f"{path}.{f.name}" if path else f.name
                stack.append((getattr(value, f.name, None), sub))
    return None
