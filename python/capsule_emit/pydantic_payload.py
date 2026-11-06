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


_MRO = type.__dict__["__mro__"]
_CLASS_DICT = type.__dict__["__dict__"]
_FIELD_TYPE = dataclasses.Field.__dict__["_field_type"]


def _class_lookup(t: type, name: str) -> Any:
    """``name`` from the class ``__dict__``s along the MRO of *t*, or ``_MISSING``.
    ``getattr``/``hasattr`` on a type would run a metaclass ``__getattr__`` or
    ``__getattribute__``, which is user code (capsule-emit#435)."""
    for c in _MRO.__get__(t):
        d = _CLASS_DICT.__get__(c)
        if name in d:
            return d[name]
    return _MISSING


_MISSING = object()


def _dataclass_fields(t: type) -> dict[str, Any] | None:
    """The ``__dataclass_fields__`` mapping of *t*, or None when *t* is not a
    dataclass. A dataclass copies its bases' fields into its own mapping, so the
    nearest hit is complete."""
    found = _class_lookup(t, "__dataclass_fields__")
    return found if type(found) is dict else None


def _is_data_descriptor(attr: Any) -> bool:
    """True when the type of *attr* defines ``__set__`` or ``__delete__``, so the
    attribute takes priority over the instance ``__dict__`` and reading it would run
    its ``__get__``. Looked up in the type's ``__dict__``s, not with ``hasattr``."""
    for c in _MRO.__get__(type(attr)):
        d = _CLASS_DICT.__get__(c)
        if "__set__" in d or "__delete__" in d:
            return True
    return False


def _field_value(value: Any, t: type, name: str) -> Any:
    """The stored value of dataclass field *name* on *value*, or ``_MISSING`` when it
    cannot be read without running user code (capsule-emit#435).

    Reads the instance ``__dict__`` through the interpreter's own ``__dict__`` slot
    a ``__slots__`` member through its member descriptor, and a plain class attribute
    (an ``init=False`` default) as is. A field shadowed by a
    data descriptor (a property, or a descriptor default) is not read: reading it
    would run its ``__get__``. A known limit, like the other unwalked objects."""
    attr = _class_lookup(t, name)
    if type(attr) is types.MemberDescriptorType:
        return attr.__get__(value)
    if attr is not _MISSING and _is_data_descriptor(attr):
        return _MISSING
    slot = _class_lookup(t, "__dict__")
    if type(slot) is types.GetSetDescriptorType:
        d = slot.__get__(value)
        if type(d) is dict and name in d:
            return d[name]
    # Not stored on the instance (``field(default=..., init=False)`` lives on the
    # class): a plain class attribute runs no code; a non-data descriptor's
    # ``__get__`` would, so that one is not read.
    if attr is _MISSING or any("__get__" in _CLASS_DICT.__get__(c) for c in _MRO.__get__(type(attr))):
        return _MISSING
    return attr


def _label_path(nodes: list[tuple[int, str, Any]], idx: int) -> str:
    """Render a node's path from its parent links. Called only once a float is
    found. A key is formatted only if its type is exactly ``str`` or ``int``
    (never a subclass, whose ``__str__`` is user code); anything else is ``<key>``."""
    parts: list[str] = []
    while idx >= 0:
        parent, kind, payload = nodes[idx]
        if kind == "i":
            parts.append(f"[{payload}]")
        else:
            t = type(payload)
            parts.append("." + (str(payload) if t is int else payload if t is str else "<key>"))
        idx = parent
    out = "".join(reversed(parts))
    return out[1:] if out.startswith(".") else out or "<payload>"


def _numpy_float(value: Any, t: type) -> bool:
    """True when *value* is a numpy array or scalar whose dtype holds floats
    (capsule-emit#433).

    Runs only if the caller already imported numpy (a numpy object cannot exist
    otherwise; this never imports it). Like the rest of the walk it runs no user
    code: the type is decided with ``issubclass(t, ...)``, and ``dtype`` and
    ``size`` are read through numpy's own class descriptors, so a subclass's
    overrides, ``__getattr__``, ``__array__`` and ``__class__`` never run. It
    reads no element, so a huge array costs the same as a small one. An array
    with no element (``size == 0``) holds no float. Object arrays are not
    walked (an object dtype, or an object field of a structured dtype): their
    elements are arbitrary objects, a documented limit.
    """
    np = sys.modules.get("numpy")
    if np is None:
        return False
    try:
        # Read the classes from the module dict: attribute access on a lazily
        # loaded numpy module would import it during a seal.
        ns = object.__getattribute__(np, "__dict__")
        ndarray, generic = ns.get("ndarray"), ns.get("generic")
        if ndarray is None or generic is None:
            return False
        if issubclass(t, ndarray):
            cls = ndarray
            if not cls.size.__get__(value):
                return False
        elif issubclass(t, generic):
            cls = generic
        else:
            return False
        todo = [cls.dtype.__get__(value)]
        while todo:
            dt = todo.pop()
            if dt.kind in "fc":
                return True
            if dt.subdtype is not None and all(dt.subdtype[1]):
                # A zero-length subarray (shape with a 0) holds no element.
                todo.append(dt.subdtype[0])
            if dt.fields:
                todo.extend(field[0] for field in dt.fields.values())
    except Exception:
        return False
    return False


def deep_float_path(root: Any) -> str | None:
    """Path of the first Python float in the standard containers of ``root``, or
    None (capsule-emit#431).

    The legacy ``default=str`` digest fallback would seal such a float through
    ``repr()``. This walks only types the payload contract already knows: dict
    (keys and values), list, tuple, set, frozenset, dataclass instances and
    Pydantic v2 models (their own fields), plus numpy arrays and scalars by dtype
    alone (:func:`_numpy_float`, capsule-emit#433). Any other object is never
    touched, so it keeps exactly its legacy behaviour; a float held only inside such
    an object, or in an object-dtype numpy array or field, is a known limit.

    Rule: **the walk runs no user code.** It decides every type with
    ``issubclass(type(x), ...)`` (never ``isinstance`` on the object, which can
    consult a user ``__class__``), reads containers through the builtin
    ``dict``/``list``/``tuple``/``set``/``frozenset`` methods (a subclass's
    overrides are bypassed), reads Pydantic fields with ``object.__getattribute__`` and dataclass fields with
    :func:`_field_value`, and
    builds the path only after a float is found. Dataclass-ness is read from the class
    ``__dict__``s along the MRO, and a dataclass field backed by a descriptor
    (a property, or a class attribute whose type defines ``__set__``) is skipped, a
    known limit. It is iterative, so a deep
    payload cannot raise ``RecursionError``, and keeps visited objects alive so a
    recycled ``id()`` cannot hide a later one.
    """
    base = _model_base()
    seen: set[int] = set()
    keep: list[Any] = []
    nodes: list[tuple[int, str, Any]] = []
    stack: list[tuple[Any, int]] = [(root, -1)]

    def child(value: Any, parent: int, kind: str, payload: Any) -> None:
        nodes.append((parent, kind, payload))
        stack.append((value, len(nodes) - 1))

    while stack:
        value, idx = stack.pop()
        t = type(value)
        if issubclass(t, float) or _numpy_float(value, t):
            return _label_path(nodes, idx)
        dc_fields = _dataclass_fields(t)
        if value is None or id(value) in seen:
            continue
        if issubclass(t, (str, bytes, int)) and dc_fields is None:
            continue
        seen.add(id(value))
        keep.append(value)
        if issubclass(t, dict):
            for k, v in dict.items(value):
                nodes.append((idx, "k", k))
                stack.append((k, len(nodes) - 1))
                stack.append((v, len(nodes) - 1))
        elif issubclass(t, (list, tuple, set, frozenset)):
            it = (
                list.__iter__ if issubclass(t, list)
                else tuple.__iter__ if issubclass(t, tuple)
                else set.__iter__ if issubclass(t, set)
                else frozenset.__iter__
            )
            for i, v in enumerate(it(value)):
                child(v, idx, "i", i)
        if base is not None and issubclass(t, base):
            try:
                for k, v in dict(object.__getattribute__(value, "__dict__")).items():
                    if type(k) is str:
                        child(v, idx, "k", k)
            except Exception:
                pass
        if dc_fields is not None:
            for name, f in dict.items(dc_fields):
                try:
                    # ClassVar and InitVar pseudo-fields are not instance fields.
                    if not issubclass(type(f), dataclasses.Field) or _FIELD_TYPE.__get__(f) is not dataclasses._FIELD:
                        continue
                    found = _field_value(value, t, name) if type(name) is str else _MISSING
                    if found is not _MISSING:
                        child(found, idx, "k", name)
                except Exception:
                    pass
    return None
