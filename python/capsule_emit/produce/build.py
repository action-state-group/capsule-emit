# SPDX-License-Identifier: Apache-2.0
"""Capsule assembly and validation, mirroring Go ``build.go``.

``build`` validates an :class:`~.model.Input`, derives assurance, computes
``capsule_id`` with agent-action-capsule, runs Class 1 and returns the exact
JCS bytes. The carried-artifact and composition bindings that ``received``
and ``build_composition`` add go through the same function, so the
validation boundary is shared.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from agent_action_capsule.canonical import compute_capsule_id, jcs

from .constants import (
    CANONICALIZATION_ID,
    FORMAT_VERSION,
    SPEC_VERSION,
    ActionType,
    Approver,
    AttestationMode,
    ChainRelation,
    EffectMode,
    EffectStatus,
    LedgerMode,
    ProvenanceModeValue,
    TimeRung,
)
from .digest import canonical, check_json_value, decode_strict_json, integral_floats
from .model import (
    BuiltPayload,
    Chain,
    ComputeAttestation,
    Disposition,
    Effect,
    Extension,
    Input,
    Model,
    ProvenanceMode,
)
from .verify import Class1Error, class1

__all__ = ["build", "format_timestamp"]

_HEX64 = re.compile(r"[0-9a-f]{64}")
_EXTENSION_NAME = re.compile(r"[a-z][a-z0-9_]*")
_TIMESTAMP = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)", re.ASCII)
_MAX_SAFE_INTEGER = 2**53 - 1

# The top-level member names the base profile defines, including the
# local-only envelope fields capsule_id derivation strips. Extensions may not
# use them. Mirrors Go extension.go baseMembers.
_BASE_MEMBERS = frozenset(
    {
        "spec_version", "format_version", "canonicalization_id", "capsule_id",
        "action_id", "action_type", "operator", "developer", "timestamp",
        "epoch_id", "domain", "provenance", "provenance_mode", "disposition",
        "effect", "chain", "assurance", "cross_party", "model_attestation",
        "references", "constraints", "compliance", "signature", "key_id",
    }
)

_EFFECT_STATUSES = (
    EffectStatus.PLANNED,
    EffectStatus.DISPATCHED,
    EffectStatus.CONFIRMED,
    EffectStatus.FAILED,
    EffectStatus.REVERTED,
)
_APPROVERS = (Approver.HUMAN, Approver.POLICY, Approver.COUNTERPARTY)


def build(input: Input) -> BuiltPayload:
    """Validate ``input`` and return a signature-free format-4 Capsule."""
    return build_with_binding(input, None)


def build_with_binding(input: Input, binding: dict[str, Any] | None) -> BuiltPayload:
    """Build with an internal compute-attestation binding (carried artifact or
    composed members) that callers cannot set directly."""
    if not isinstance(input, Input):
        raise TypeError("input must be a capsule_emit.produce.Input")
    _validate_input(input, binding)

    payload: dict[str, Any] = {
        "spec_version": SPEC_VERSION,
        "format_version": FORMAT_VERSION,
        "canonicalization_id": CANONICALIZATION_ID,
        "action_id": input.action_id,
        "action_type": input.action_type,
        "operator": input.operator,
        "developer": input.developer,
        "timestamp": format_timestamp(input.timestamp),
        "assurance": _assurance(input.effect, input.chain),
    }
    if input.epoch_id:
        payload["epoch_id"] = input.epoch_id
    if input.domain:
        payload["domain"] = input.domain
    if input.provenance:
        payload["provenance"] = input.provenance
    if input.provenance_mode is not None:
        payload["provenance_mode"] = _provenance_mode_map(input.provenance_mode)
    if input.disposition is not None:
        payload["disposition"] = _disposition_map(input.disposition)
    if input.effect is not None:
        payload["effect"] = _effect_map(input.effect)
    if input.chain is not None:
        payload["chain"] = {
            "parent_capsule_id": input.chain.parent_capsule_id,
            "relation": input.chain.relation,
        }
    attestation = _model_attestation(input.model, input.compute, binding)
    if attestation is not None:
        payload["model_attestation"] = attestation
    if input.references is not None:
        references: list[Any] = []
        for reference in input.references:
            value = _digest_reference(reference.type, reference.digest_alg, reference.digest)
            if reference.citation_purpose:
                value["citation_purpose"] = reference.citation_purpose
            if reference.log_coordinates is not None:
                value["log_coordinates"] = _copy_json(reference.log_coordinates, "reference log coordinates")
            references.append(value)
        payload["references"] = references
    for extension in input.extensions:
        payload[extension.name] = _extension_value(extension, payload)
    check_json_value(payload, "$")

    capsule_id = canonical(compute_capsule_id, payload)
    payload["capsule_id"] = capsule_id
    result = canonical(class1, payload)
    if not result.ok:
        raise Class1Error(result)
    encoded = canonical(jcs, payload)
    return BuiltPayload(capsule_id=capsule_id, value=canonical(json.loads, encoded), json=encoded)


_GO_ZERO_TIME = datetime(1, 1, 1, tzinfo=timezone.utc)


def format_timestamp(timestamp: Any) -> str:
    """Render a timestamp in the UTC wire form Go and TypeScript share: whole
    seconds omit the fraction, otherwise six fractional digits. Sub-microsecond
    digits are truncated."""
    fraction = ""
    if isinstance(timestamp, datetime):
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        try:
            utc = timestamp.astimezone(timezone.utc)
        except OverflowError:
            raise ValueError("timestamp is invalid") from None
    elif isinstance(timestamp, str):
        match = _TIMESTAMP.fullmatch(timestamp)
        if match is None:
            raise ValueError("timestamp must be RFC 3339 with timezone")
        base, fraction, offset = match.group(1), match.group(2) or "", match.group(3)
        try:
            parsed = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            raise ValueError("timestamp is invalid") from None
        if offset != "Z":
            sign = 1 if offset[0] == "+" else -1
            hours, minutes = int(offset[1:3]), int(offset[4:6])
            if hours > 23 or minutes > 59:
                raise ValueError("timestamp is invalid")
            try:
                parsed -= sign * timedelta(hours=hours, minutes=minutes)
            except OverflowError:
                raise ValueError("timestamp is invalid") from None
        utc = parsed.replace(microsecond=int(fraction.ljust(9, "0")[:6]), tzinfo=timezone.utc)
    else:
        raise ValueError("timestamp is required")
    # Go's zero time.Time is its "no timestamp"; the same instant is refused
    # here so both build the same set of Capsules. Go checks before it
    # truncates to microseconds, so a nonzero sub-microsecond fraction is not
    # the zero time.
    if utc == _GO_ZERO_TIME and not fraction.strip("0"):
        raise ValueError("timestamp is required")
    text = (
        f"{utc.year:04d}-{utc.month:02d}-{utc.day:02d}T"
        f"{utc.hour:02d}:{utc.minute:02d}:{utc.second:02d}"
    )
    if utc.microsecond:
        text += f".{utc.microsecond:06d}"
    return text + "Z"


def _validate_input(input: Input, binding: dict[str, Any] | None) -> None:
    for name, value in (("action id", input.action_id), ("operator", input.operator), ("developer", input.developer)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} is required")
    if input.timestamp is None or input.timestamp == "":
        raise ValueError("timestamp is required")
    if input.action_type not in (ActionType.FYI, ActionType.DECIDE):
        raise ValueError(f'action type must be "{ActionType.FYI}" or "{ActionType.DECIDE}"')
    if input.action_type == ActionType.DECIDE and input.disposition is None:
        raise ValueError("decide action requires a disposition")
    if input.disposition is not None:
        _validate_disposition(input.disposition)
    if input.effect is not None:
        _validate_effect(input.effect)
    _validate_attestation(input.model, input.compute)
    if binding is not None and input.compute is not None and (
        input.compute.agent_input_digest or input.compute.agent_output_digest
    ):
        raise ValueError("carried or composed binding must not include agent input or output digest")
    if input.chain is not None:
        _validate_chain(input.chain, input.epoch_id)
    if input.provenance_mode is not None:
        _validate_provenance_mode(input.provenance_mode)


def _validate_chain(chain: Chain, epoch_id: str) -> None:
    if not _is_hex64(chain.parent_capsule_id):
        raise ValueError("chain parent capsule id must be 64 lowercase hex characters")
    if not chain.relation:
        raise ValueError("chain relation is required")
    if chain.relation == ChainRelation.EPOCH_OPENS and not epoch_id:
        raise ValueError("epoch_opens chain requires epoch id")


def _validate_disposition(disposition: Disposition) -> None:
    if not disposition.decision:
        raise ValueError("disposition decision is required")
    if disposition.approver not in _APPROVERS:
        raise ValueError('disposition approver must be "human", "policy", or "counterparty"')
    if not isinstance(disposition.human_disposed, bool):
        raise ValueError("disposition human_disposed must be a bool")
    if disposition.human_disposed and disposition.approver != Approver.HUMAN:
        raise ValueError("human-disposed decision requires human approver")
    if disposition.reason_digest and not _is_hex64(disposition.reason_digest):
        raise ValueError("disposition reason digest must be 64 lowercase hex characters")


def _validate_effect(effect: Effect) -> None:
    if not effect.type or not effect.type.strip():
        raise ValueError("effect type is required")
    if effect.status not in _EFFECT_STATUSES:
        raise ValueError(f"unsupported effect status {json.dumps(effect.status)}")
    if not effect.irreversibility_class:
        raise ValueError("effect irreversibility class is required")
    for name, digest in (("request", effect.request_digest), ("response", effect.response_digest)):
        if digest and not _is_hex64(digest):
            raise ValueError(f"effect {name} digest must be 64 lowercase hex characters")
    status = effect.status
    if status == EffectStatus.PLANNED:
        if effect.request_digest or effect.response_digest:
            raise ValueError("planned effect must not carry request or response digests")
        if effect.effect_attestation:
            raise ValueError("planned effect must not carry effect attestation")
    elif status == EffectStatus.DISPATCHED:
        if effect.response_digest:
            raise ValueError("dispatched effect must not carry a response digest")
        if not effect.effect_attestation:
            raise ValueError("dispatched effect requires effect attestation")
    elif status == EffectStatus.CONFIRMED:
        if not effect.response_digest:
            raise ValueError("confirmed effect requires a response digest")
        if not effect.effect_attestation:
            raise ValueError("confirmed effect requires effect attestation")
    elif not effect.effect_attestation:
        raise ValueError(f"{status} effect requires effect attestation")


def _validate_attestation(model: Model | None, compute: ComputeAttestation | None) -> None:
    if model is not None:
        if not model.provider.strip() and not model.model_id.strip():
            raise ValueError("model must include provider or model id")
        if model.provider and not model.provider.strip():
            raise ValueError("model provider must be non-empty when present")
        if model.model_id and not model.model_id.strip():
            raise ValueError("model id must be non-empty when present")
    if compute is None:
        return
    if compute.runtime and not compute.runtime.strip():
        raise ValueError("compute attestation runtime must be non-empty when present")
    if not (compute.agent_input_digest or compute.agent_output_digest or compute.runtime):
        raise ValueError("compute attestation must include a digest or runtime")
    for name, digest in (("agent input", compute.agent_input_digest), ("agent output", compute.agent_output_digest)):
        if digest and not _is_hex64(digest):
            raise ValueError(f"compute attestation {name} digest must be 64 lowercase hex characters")


def _validate_provenance_mode(mode: ProvenanceMode) -> None:
    # draft -05 §5.3(bis): "backfilled" requires the four companions and
    # permits time_rung; "contemporaneous" permits none of them.
    if mode.mode not in (ProvenanceModeValue.CONTEMPORANEOUS, ProvenanceModeValue.BACKFILLED):
        raise ValueError('provenance mode must be "contemporaneous" or "backfilled"')
    source = mode.source_ref
    if source is not None and (source.citation_purpose or source.log_coordinates is not None):
        raise ValueError("provenance mode source ref must not carry citation purpose or log coordinates")
    companions = source is not None or mode.source_asserted_at or mode.import_batch or mode.imported_at
    if mode.mode == ProvenanceModeValue.BACKFILLED:
        missing = []
        if source is None:
            missing.append("source ref")
        elif not source.type.strip() or not source.digest_alg.strip() or not source.digest.strip():
            raise ValueError("provenance mode source ref requires type, digest alg, and digest")
        if not mode.source_asserted_at.strip():
            missing.append("source asserted at")
        if not mode.import_batch.strip():
            missing.append("import batch")
        if not mode.imported_at.strip():
            missing.append("imported at")
        if missing:
            raise ValueError("backfilled provenance mode requires " + ", ".join(missing))
    elif companions or mode.time_rung:
        raise ValueError(
            "provenance mode source ref, source asserted at, import batch, imported at, and time rung "
            'are meaningful only when mode is "backfilled"'
        )
    if mode.time_rung and mode.time_rung not in (TimeRung.SELF_ATTESTED, TimeRung.WITNESSED):
        raise ValueError('provenance mode time rung must be "self_attested" or "witnessed"')


def _is_hex64(value: Any) -> bool:
    return isinstance(value, str) and _HEX64.fullmatch(value) is not None


def _digest_reference(type_: str, digest_alg: str, digest: str, slot: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {"type": type_, "digest_alg": digest_alg, "digest": digest}
    if slot:
        result["slot"] = slot
    return result


def _provenance_mode_map(mode: ProvenanceMode) -> dict[str, Any]:
    result: dict[str, Any] = {"mode": mode.mode}
    if mode.source_ref is not None:
        result["source_ref"] = _digest_reference(mode.source_ref.type, mode.source_ref.digest_alg, mode.source_ref.digest)
    if mode.source_asserted_at:
        result["source_asserted_at"] = mode.source_asserted_at
    if mode.import_batch:
        result["import_batch"] = mode.import_batch
    if mode.imported_at:
        result["imported_at"] = mode.imported_at
    if mode.time_rung:
        result["time_rung"] = mode.time_rung
    return result


def _disposition_map(disposition: Disposition) -> dict[str, Any]:
    result: dict[str, Any] = {
        "decision": disposition.decision,
        "approver": disposition.approver,
        "human_disposed": disposition.human_disposed,
    }
    if disposition.verdict_class:
        result["verdict_class"] = disposition.verdict_class
    if disposition.reason_digest:
        result["reason_digest"] = disposition.reason_digest
    return result


def _effect_map(effect: Effect) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": effect.type,
        "status": effect.status,
        "irreversibility_class": effect.irreversibility_class,
    }
    if effect.effect_attestation:
        result["effect_attestation"] = effect.effect_attestation
    if effect.request_digest:
        result["request_digest"] = effect.request_digest
    if effect.response_digest:
        result["response_digest"] = effect.response_digest
    if effect.external_ref:
        result["external_ref"] = effect.external_ref
    return result


def _assurance(effect: Effect | None, chain: Chain | None) -> dict[str, Any]:
    effect_mode = EffectMode.NOT_APPLICABLE
    if effect is not None and effect.status != EffectStatus.PLANNED:
        effect_mode = EffectMode.CONFIRMED if effect.status == EffectStatus.CONFIRMED else EffectMode.DISPATCHED_UNCONFIRMED
    return {
        "effect_mode": effect_mode,
        "attestation_mode": AttestationMode.SELF_ATTESTED,
        "ledger_mode": LedgerMode.STANDALONE if chain is None else LedgerMode.CHAINED,
    }


def _model_attestation(
    model: Model | None, compute: ComputeAttestation | None, binding: dict[str, Any] | None
) -> dict[str, Any] | None:
    result: dict[str, Any] = {}
    if model is not None:
        if model.model_id:
            result["model_id"] = model.model_id
        if model.provider:
            result["provider"] = model.provider
    attestation: dict[str, Any] = {}
    if compute is not None:
        if compute.agent_input_digest:
            attestation["agent_input_digest"] = compute.agent_input_digest
        if compute.agent_output_digest:
            attestation["agent_output_digest"] = compute.agent_output_digest
        if compute.runtime:
            attestation["runtime"] = compute.runtime
    if binding:
        attestation.update(binding)
    if attestation:
        result["compute_attestation"] = attestation
    return result or None


def _copy_json(value: Mapping[str, Any], what: str) -> Any:
    """Detach and validate a caller-owned JSON object through canonical JSON."""
    try:
        if not isinstance(value, dict):
            value = dict(value)
        check_json_value(value, "$")
        return canonical(json.loads, canonical(jcs, integral_floats(value)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{what}: {exc}") from None


def _extension_value(extension: Extension, payload: Mapping[str, Any]) -> Any:
    name = extension.name
    if not isinstance(name, str) or not _EXTENSION_NAME.fullmatch(name):
        raise ValueError(
            f"extension name {json.dumps(name)} must be lowercase letters, digits and underscores, starting with a letter"
        )
    if name in _BASE_MEMBERS:
        raise ValueError(f"extension {json.dumps(name)} is a base-profile member")
    if name in payload:
        raise ValueError(f"extension {json.dumps(name)} is given more than once")
    if not extension.value:
        raise ValueError(f"extension {json.dumps(name)} has no value")
    try:
        value = decode_strict_json(extension.value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"extension {json.dumps(name)}: {exc}") from None
    if value is None:
        raise ValueError(f"extension {json.dumps(name)} must not be null")
    _check_extension_numbers(value, "$." + name)
    return value


def _check_extension_numbers(value: Any, path: str) -> None:
    # Iterative, like check_json_value, so nesting up to MAX_JSON_DEPTH cannot
    # exhaust Python's recursion limit here.
    stack: list = [(value, path)]
    while stack:
        item, where = stack.pop()
        if isinstance(item, bool):
            continue
        if isinstance(item, float):
            raise ValueError(f"float at {where}: floating-point values are forbidden in a Capsule")
        if isinstance(item, int) and not -_MAX_SAFE_INTEGER <= item <= _MAX_SAFE_INTEGER:
            raise ValueError(f"integer at {where} is outside the interoperable range")
        if isinstance(item, dict):
            stack.extend((member, f"{where}.{key}") for key, member in reversed(list(item.items())))
        elif isinstance(item, list):
            stack.extend((member, f"{where}[{index}]") for index, member in reversed(list(enumerate(item))))
