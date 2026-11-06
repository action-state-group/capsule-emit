# SPDX-License-Identifier: Apache-2.0
"""Payload helpers for the digest layer: Pydantic v2 models reduced to their
JSON dump (capsule-emit#426), and the float walk that guards the legacy
``default=str`` fallback (capsule-emit#431).

Shared by the core digest, ``verify_input_digest`` and the adapter base so a
model means the same thing everywhere. Pydantic is never imported here.
"""
from __future__ import annotations

import array
import collections.abc
import enum
import functools
import numbers
import sys
import types
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
    """Path of the first float anywhere in ``root``, or None (capsule-emit#431).

    The legacy ``default=str`` digest fallback seals any non-JSON value through
    ``str()``/``repr()``, which shows a float held inside it. This therefore
    walks everything such a value can hold: mappings (keys and values), sized
    collections, ``array`` and ``complex``, Pydantic models (their dump and
    their own attributes, since ``repr`` shows fields a serializer hides) and
    plain objects (``__dict__`` and ``__slots__``). Classes, modules,
    functions and enums are leaves: their ``str()`` does not show a float.
    Iterative, so a deep payload cannot raise ``RecursionError``; every visited
    object is kept alive so a recycled ``id()`` cannot hide a later one.
    """
    base = _model_base()
    leaf = (str, bytes, bytearray, int, bool, range, type, types.ModuleType,
            types.FunctionType, types.BuiltinFunctionType, types.MethodType, enum.Enum)
    seen: set[int] = set()
    keep: list[Any] = []
    stack: list[tuple[Any, str]] = [(root, "")]
    while stack:
        value, path = stack.pop()
        if isinstance(value, (float, complex)) or (
            isinstance(value, numbers.Real) and not isinstance(value, numbers.Rational)
        ):
            return path or "<payload>"
        if value is None or isinstance(value, leaf) or id(value) in seen:
            continue
        seen.add(id(value))
        keep.append(value)
        try:
            if isinstance(value, array.array):
                if value.typecode in "fd":
                    return path or "<payload>"
            elif isinstance(value, collections.abc.Mapping):
                for k, v in list(value.items()):
                    sub = f"{path}.{k}" if path else str(k)
                    stack.append((k, sub))
                    stack.append((v, sub))
            elif isinstance(value, collections.abc.Collection):
                for i, v in enumerate(list(value)):
                    stack.append((v, f"{path}[{i}]"))
        except Exception:  # a collection whose iteration fails: str() never iterates it
            pass
        if isinstance(value, slice):
            stack.extend([(value.start, path), (value.stop, path), (value.step, path)])
        elif isinstance(value, BaseException):
            stack.append((value.args, f"{path}.args"))
        elif isinstance(value, functools.partial):
            stack.append((value.args, f"{path}.args"))
            stack.append((value.keywords, f"{path}.keywords"))
        if base is not None and isinstance(value, base):
            try:
                stack.append((value.model_dump(mode="json"), path))
            except Exception:
                pass
        attrs = dict(getattr(value, "__dict__", None) or {})
        for name in _slot_names(value):
            if hasattr(value, name):
                attrs.setdefault(name, getattr(value, name))
        for k, v in attrs.items():
            stack.append((v, f"{path}.{k}" if path else k))
    return None
