# SPDX-License-Identifier: Apache-2.0
"""Consumer for agentgateway's ExtMcp ``metadata_context`` audit metadata.

This is the reading half of agentgateway issue #3042 ("Expose ID-JAG audit
metadata to native access logging").  The gateway side of that issue is not
implemented yet; this module is written against the *carrier* that already
exists, so it starts working the moment the fields land.

The carrier
-----------
``McpRequest.metadata_context`` / ``McpResponse.metadata_context`` are
``google.protobuf.Struct``, documented upstream as "CEL-evaluated context from
gateway config, one field per config key"
(``crates/protos/proto/ext_mcp.proto``).  They are filled by ``build_metadata``
(``crates/agentgateway/src/mcp/guardrails/client.rs``) from the processor's
``metadata`` config — ``HashMap<String, Arc<cel::Expression>>``, "CEL
expressions evaluated per request and sent to the processor as metadata"
(``crates/agentgateway/src/mcp/guardrails/mod.rs``).

Four properties of that carrier drive every design choice here:

1. **Keys are literal map keys, not paths.**  Upstream's own test uses the key
   ``"tenant.io"`` (``crates/agentgateway/src/mcp/mcp_tests.rs``).  A literal
   key therefore always wins over a dotted-path walk.
2. **Values may be nested.**  The same test maps a key to ``{"path":
   request.path}``, so one config key can carry a whole object.  Both the flat
   and the nested wiring are accepted.
3. **A failing CEL expression is dropped silently** — ``build_metadata`` logs at
   debug and omits the key.  A missing field is therefore indistinguishable on
   the wire from "not configured" and from "expression errored".  Absence is
   recorded as absence; it is never treated as a pass.
4. **Numbers arrive as protobuf doubles.**  ``Struct`` has no integer type, so
   an integral value round-trips through a ``float``.

Ordering and multiplexing
-------------------------
On #3042, @howardjohn noted that for MCP "the guardrail happens before IDJAG",
and that multiplexing can run two ID-JAG flows behind one MCP guardrail call.
Both are handled without needing the gateway to change its ordering:

* **Ordering** — the gateway evaluates the processor's ``metadata`` CEL twice,
  once in ``check_request`` and again in ``check_response`` (client.rs).  This
  consumer reads *both* and keeps the later value when the two differ, and it
  records which hook each field came from in ``phase``.  If #3042 populates the
  CEL context at backend-auth time, the response-phase evaluation is the one
  that will carry it.  This module does not assume that it does — it reports
  what arrived.
* **Multiplexing** — a slot whose value is a list is kept as a list, and
  ``grants`` is built by zipping the ID-JAG ``jti`` and ``audience`` slots, so
  N grants behind one call produce N grant entries rather than one lossy join.

Never token material
--------------------
#3042 says the metadata "must never expose raw tokens".  That is the gateway's
constraint, but this consumer is what writes a *durable, signed* record, so it
enforces the same rule on its own side: any value shaped like token material is
dropped before sealing and the slot is named in ``redacted``.  The adapter also
never reads ``McpRequest.headers`` — with an empty ``request_headers.allowed``
filter the gateway forwards every header, ``authorization`` included.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from capsule_emit.numbers import canonicalize_for_digest

_log = logging.getLogger(__name__)

#: Version tag on the emitted block.  Bump when the shape changes.
SCHEMA = "capsule-emit/agentgateway-audit/1"

#: The upstream issue this contract is written against.
ISSUE = "agentgateway/agentgateway#3042"

#: Capsule slots, in the order #3042 names them.
AUDIT_SLOTS = ("subject", "idjag_jti", "idjag_aud", "resource_token")

#: Slot → candidate ``metadata_context`` keys, most-specific first.
#:
#: The defaults follow the shape @christian-posta proposed on #3042 — "expose a
#: ``backendAuth.*`` object ... containing the ID-JAG jti/audience, the subject,
#: final resource token" — in the lowerCamelCase agentgateway already uses for
#: this feature (``crossAppAccess``, ``requestedTokenType``, the ``IdJag``
#: token-type variant).  Nothing here is load-bearing: whatever names the
#: implementation lands on, remap with ``CAPSULE_AG_AUDIT_KEYS`` or the
#: ``key_map=`` argument and this consumer follows.
DEFAULT_KEY_MAP: dict[str, tuple[str, ...]] = {
    "subject": ("backendAuth.subject", "backendAuth.sub", "subject"),
    "idjag_jti": ("backendAuth.idJag.jti", "idjag_jti"),
    "idjag_aud": ("backendAuth.idJag.audience", "backendAuth.idJag.aud", "idjag_aud"),
    "resource_token": ("backendAuth.resourceToken.jti", "resource_token"),
}

#: Env var carrying a JSON object of ``{slot: key}`` or ``{slot: [key, ...]}``.
KEY_MAP_ENV = "CAPSULE_AG_AUDIT_KEYS"

# A JWT/ID-JAG in compact serialization: three base64url segments.
_JWS_COMPACT = re.compile(r"\A[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\Z")
_AUTH_SCHEME = re.compile(r"\A(bearer|basic|dpop)\s", re.IGNORECASE)
_HEX = re.compile(r"\A[0-9a-fA-F]+\Z")

# A non-hex string this long is not an identifier; treat it as opaque token
# material.  Well above a sha-256 hex digest (64) and a long audience URL.
_OPAQUE_LEN = 256

# Hard cap on any single sealed value, so a misconfigured CEL expression cannot
# push an entire request body into the capsule.
_MAX_LEN = 512

_MISSING = object()


class _Absent:
    """Sentinel distinguishing 'no metadata_context on the wire' from 'empty'."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "<absent>"


ABSENT = _Absent()


# ---------------------------------------------------------------------------
# Struct → plain Python
# ---------------------------------------------------------------------------


def _value_to_py(v: Any) -> Any:
    kind = v.WhichOneof("kind")
    if kind == "string_value":
        return v.string_value
    if kind == "bool_value":
        return v.bool_value
    if kind == "number_value":
        n = v.number_value
        # Struct has no integer type; recover the integer when it is exact and
        # inside the range a capsule digest accepts as an integer token.
        if n.is_integer() and abs(n) <= 2**53 - 1:
            return int(n)
        return n
    if kind == "struct_value":
        return {k: _value_to_py(x) for k, x in v.struct_value.fields.items()}
    if kind == "list_value":
        return [_value_to_py(x) for x in v.list_value.values]
    # null_value, or a kind this protobuf runtime does not know.
    return None


def struct_to_dict(struct: Any) -> dict[str, Any]:
    """Convert a ``google.protobuf.Struct`` (or a plain dict) to plain Python.

    Accepts ``None`` and returns ``{}``, so callers can pass an unset field
    straight through.  Duck-typed on ``.fields`` rather than importing
    ``struct_pb2``, which keeps this module importable without protobuf.
    """
    if struct is None:
        return {}
    if isinstance(struct, dict):
        return struct
    fields = getattr(struct, "fields", None)
    if fields is None:
        return {}
    return {k: _value_to_py(v) for k, v in fields.items()}


def metadata_from_message(msg: Any) -> dict[str, Any] | _Absent:
    """Read ``metadata_context`` off an ``McpRequest``/``McpResponse``.

    Returns :data:`ABSENT` when the gateway did not set the field at all —
    which means the processor has no ``metadata`` configured, a different
    condition from "configured, but every expression failed" (which arrives as
    a present-but-empty Struct).  Collapsing the two would make a broken CEL
    expression look like an operator who never asked for the fields.
    """
    if msg is None:
        return ABSENT
    try:
        if not msg.HasField("metadata_context"):
            return ABSENT
    except (AttributeError, ValueError):
        # Not a proto message, or a runtime without presence on this field.
        return struct_to_dict(getattr(msg, "metadata_context", None))
    return struct_to_dict(msg.metadata_context)


# ---------------------------------------------------------------------------
# Key map
# ---------------------------------------------------------------------------


def normalize_key_map(raw: dict[str, Any] | None) -> dict[str, tuple[str, ...]]:
    """Coerce a ``{slot: key | [key, ...]}`` mapping to the internal form.

    Unknown slots are dropped with a warning rather than raising: a remap is
    something an operator does in a hurry against a moving upstream, and a typo
    should cost that one field, not the whole processor.
    """
    if not raw:
        return dict(DEFAULT_KEY_MAP)
    out = dict(DEFAULT_KEY_MAP)
    for slot, keys in raw.items():
        if slot not in AUDIT_SLOTS:
            _log.warning("agentgateway audit: ignoring unknown slot %r in key map", slot)
            continue
        if isinstance(keys, str):
            out[slot] = (keys,)
        elif isinstance(keys, (list, tuple)) and all(isinstance(k, str) for k in keys):
            out[slot] = tuple(keys)
        else:
            _log.warning("agentgateway audit: ignoring non-string key(s) for slot %r", slot)
    return out


def key_map_from_env(env: dict[str, str] | None = None) -> dict[str, tuple[str, ...]]:
    """Build the key map from ``CAPSULE_AG_AUDIT_KEYS`` (JSON), else defaults."""
    src = os.environ if env is None else env
    raw = src.get(KEY_MAP_ENV)
    if not raw:
        return dict(DEFAULT_KEY_MAP)
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        _log.warning("agentgateway audit: %s is not valid JSON (%s); using defaults", KEY_MAP_ENV, exc)
        return dict(DEFAULT_KEY_MAP)
    if not isinstance(parsed, dict):
        _log.warning("agentgateway audit: %s must be a JSON object; using defaults", KEY_MAP_ENV)
        return dict(DEFAULT_KEY_MAP)
    return normalize_key_map(parsed)


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def _lookup(md: dict[str, Any], key: str) -> Any:
    """Resolve *key* against *md*: literal key first, then dotted-path walk.

    Literal wins because upstream keys are free-form strings that may contain
    dots (``"tenant.io"`` in agentgateway's own guardrails test).
    """
    if key in md:
        return md[key]
    if "." not in key:
        return _MISSING
    cur: Any = md
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def token_shaped(value: Any) -> str | None:
    """Return a reason string when *value* looks like token material, else None.

    Shape-based, deliberately: the point is to hold even when an operator wires
    a CEL expression that resolves to a credential.  It fails toward redaction.
    """
    if not isinstance(value, str):
        return None
    if _AUTH_SCHEME.match(value):
        return "authorization-scheme"
    if _JWS_COMPACT.match(value):
        return "jws-compact"
    if len(value) >= _OPAQUE_LEN and not _HEX.match(value):
        return "opaque-long"
    return None


def _sanitize(value: Any) -> tuple[Any, str | None]:
    """Return ``(safe_value, redaction_reason)`` for one resolved slot value."""
    if isinstance(value, list):
        out, reasons = [], []
        for item in value:
            safe, reason = _sanitize(item)
            if reason:
                reasons.append(reason)
                continue
            out.append(safe)
        if reasons and not out:
            return None, reasons[0]
        return out, (reasons[0] if reasons else None)
    reason = token_shaped(value)
    if reason:
        return None, reason
    if isinstance(value, str) and len(value) > _MAX_LEN:
        return value[:_MAX_LEN], None
    if isinstance(value, dict):
        # A nested object in a slot is not an identifier; keep it out of the
        # signed record rather than guessing which member was meant.
        return None, "not-an-identifier"
    return value, None


# ---------------------------------------------------------------------------
# The block
# ---------------------------------------------------------------------------


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else [value]


def build_authority_block(
    request_md: dict[str, Any] | _Absent,
    response_md: dict[str, Any] | _Absent,
    *,
    key_map: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, Any]:
    """Build the authority-chain block sealed into ``compute_attestation``.

    Args:
        request_md: ``metadata_context`` from ``CheckRequest``, or :data:`ABSENT`.
        response_md: ``metadata_context`` from ``CheckResponse``, or :data:`ABSENT`.
        key_map: Slot → candidate keys.  Defaults to :data:`DEFAULT_KEY_MAP`.

    Returns:
        A JSON-safe dict.  ``fields`` carries only what actually arrived, each
        with the ``key`` it came from and the ``phase`` that supplied it;
        ``absent`` names every slot that did not, so a reader can never mistake
        a missing authority reference for a checked one.
    """
    kmap = key_map or dict(DEFAULT_KEY_MAP)
    req = {} if isinstance(request_md, _Absent) else dict(request_md)
    resp = {} if isinstance(response_md, _Absent) else dict(response_md)

    fields: dict[str, Any] = {}
    absent: list[str] = []
    redacted: list[str] = []

    for slot in AUDIT_SLOTS:
        hit = _MISSING
        hit_key = hit_phase = None
        # Response phase last so it wins: on #3042 the guardrail request hook
        # runs before backend auth, so the later evaluation is the one that can
        # carry a grant the request hook could not have seen.
        for phase, md in (("request", req), ("response", resp)):
            for key in kmap.get(slot, ()):
                found = _lookup(md, key)
                if found is not _MISSING and found is not None:
                    hit, hit_key, hit_phase = found, key, phase
                    break
        if hit is _MISSING:
            absent.append(slot)
            continue
        safe, reason = _sanitize(hit)
        if reason is not None and safe is None:
            redacted.append(slot)
            absent.append(slot)
            _log.warning(
                "agentgateway audit: slot %r from key %r dropped (%s) — never sealing token material",
                slot, hit_key, reason,
            )
            continue
        if reason is not None:
            redacted.append(slot)
        fields[slot] = {"value": safe, "key": hit_key, "phase": hit_phase}

    grants = _build_grants(fields)

    block: dict[str, Any] = {
        "schema": SCHEMA,
        "issue": ISSUE,
        "fields": fields,
        "grants": grants,
        "absent": sorted(absent),
        "redacted": sorted(redacted),
        "metadata_keys": {
            "request": None if isinstance(request_md, _Absent) else sorted(req),
            "response": None if isinstance(response_md, _Absent) else sorted(resp),
        },
    }
    # Floats are a §5.1 error in digest-bearing fields, and a metadata_context
    # number is always a protobuf double — canonicalize at the one shared exit.
    return canonicalize_for_digest(block, field="extra_compute")


def _build_grants(fields: dict[str, Any]) -> list[dict[str, Any]]:
    """Zip the ID-JAG jti/audience slots into one entry per grant.

    Multiplexing (@howardjohn, #3042) can put N grants behind one guardrail
    call, so both slots are treated as lists and joined positionally.  Uneven
    lengths keep the longer side and leave the short side's member out of that
    entry rather than repeating a value across grants it does not belong to.
    """
    jti = _as_list(fields["idjag_jti"]["value"]) if "idjag_jti" in fields else []
    aud = _as_list(fields["idjag_aud"]["value"]) if "idjag_aud" in fields else []
    grants = []
    for i in range(max(len(jti), len(aud))):
        entry: dict[str, Any] = {}
        if i < len(jti):
            entry["jti"] = jti[i]
        if i < len(aud):
            entry["aud"] = aud[i]
        if entry:
            grants.append(entry)
    return grants
