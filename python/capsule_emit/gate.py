# SPDX-License-Identifier: Apache-2.0
"""capsule-emit gate — stateless check-gate-seal interface (the "wicket").

A Constraint is a deterministic predicate over action inputs and outputs.
Constraints MUST NOT call a model or perform I/O — they are pure functions.

Usage::

    from capsule_emit.gate import Constraint, GateResult, gate_and_emit, run_gate
    from capsule_emit.constraints.apache import AmountUnderCap, VendorKnown

    constraints = [AmountUnderCap(5000), VendorKnown({"Acme", "Globex"})]
    result = gate_and_emit(
        action="write_po",
        constraints=constraints,
        inputs={"vendor": "Acme", "amount": 1200},
        output={"po_id": "PO-001"},
        emitter=emitter,
        on_block=None,  # None -> raise GateBlockedError on failure
    )
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

__all__ = [
    "Constraint",
    "CheckResult",
    "GateResult",
    "GateBlockedError",
    "EscalationCallback",
    "run_gate",
    "gate_and_emit",
]


@runtime_checkable
class Constraint(Protocol):
    """Protocol for a deterministic predicate over action inputs/outputs.

    Implementations MUST be deterministic and MUST NOT call a model,
    perform I/O, or have side effects.  A constraint is a pure function
    of its arguments.

    Attributes:
        name: A stable, human-readable identifier for this constraint.

    Methods:
        check: Return ``(passed: bool, reason: str | None)``.  When
            ``passed`` is ``True``, ``reason`` SHOULD be ``None``.
            When ``passed`` is ``False``, ``reason`` SHOULD explain why.
    """

    name: str

    def check(self, inputs: dict, output: Any) -> tuple[bool, str | None]:
        """Evaluate this constraint.

        Args:
            inputs: The action's named input arguments.
            output: The action's return value (may be ``None``).

        Returns:
            ``(True, None)`` when the constraint passes.
            ``(False, reason_str)`` when it fails.
        """
        ...  # pragma: no cover


@dataclass
class CheckResult:
    """The outcome of running a single :class:`Constraint`.

    Attributes:
        name: The constraint's stable name.
        passed: Whether the constraint passed.
        reason: Failure explanation when ``passed`` is ``False``;
            ``None`` when the constraint passed.
    """

    name: str
    passed: bool
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict suitable for ``extra_compute``."""
        d: dict[str, Any] = {"name": self.name, "passed": self.passed}
        if self.reason is not None:
            d["reason"] = self.reason
        return d


@dataclass
class GateResult:
    """The aggregate outcome of running a set of :class:`Constraint` objects.

    Attributes:
        results: One :class:`CheckResult` per constraint, in order.
    """

    results: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """``True`` iff every constraint passed."""
        return all(r.passed for r in self.results)

    def to_gate_checks(self) -> list[dict[str, Any]]:
        """Return a list of dicts for embedding in ``extra_compute``."""
        return [r.to_dict() for r in self.results]


#: Callback type for blocked-gate escalation.
#: Receives ``(action_name, gate_result)`` and MUST NOT raise.
EscalationCallback = Callable[[str, GateResult], None]


class GateBlockedError(Exception):
    """Raised by :func:`gate_and_emit` when a gate fails and no callback is set.

    Attributes:
        action: The action name that was blocked.
        gate_result: The :class:`GateResult` that triggered the block.
    """

    def __init__(self, action: str, gate_result: GateResult) -> None:
        self.action = action
        self.gate_result = gate_result
        failures = [r for r in gate_result.results if not r.passed]
        reasons = "; ".join(
            f"{r.name}: {r.reason}" if r.reason else r.name for r in failures
        )
        super().__init__(f"Gate blocked '{action}': {reasons}")


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------


def run_gate(
    constraints: list[Constraint],
    inputs: dict,
    output: Any,
) -> GateResult:
    """Run every constraint and return the aggregate :class:`GateResult`.

    All constraints are always evaluated (no short-circuit) so the full
    set of failures is visible to callers.

    Args:
        constraints: Sequence of :class:`Constraint` objects to run.
        inputs: The action's named input arguments.
        output: The action's return value.

    Returns:
        :class:`GateResult` with one :class:`CheckResult` per constraint.
    """
    results: list[CheckResult] = []
    for c in constraints:
        try:
            passed, reason = c.check(inputs, output)
        except Exception as exc:  # noqa: BLE001
            passed = False
            reason = f"constraint raised: {exc}"
        results.append(CheckResult(name=c.name, passed=passed, reason=reason))
    return GateResult(results=results)


def gate_and_emit(
    action: str,
    constraints: list[Constraint],
    inputs: dict,
    output: Any,
    emitter: Any,
    *,
    on_block: EscalationCallback | None = None,
) -> Any:
    """Run constraints then seal a capsule reflecting the gate outcome.

    The capsule's ``compute_attestation`` block includes ``gate_checks`` —
    a list of dicts (name, passed, reason) — so an auditor can verify not
    just *what ran* but *which constraints were checked and whether they
    passed*.

    **Pass path** (all constraints pass):
        Calls ``emitter.emit_capsule(action, ..., verdict="executed",
        extra_compute={"gate_checks": [...]})`` and returns *output*
        unchanged.

    **Blocked path, with callback**:
        Calls ``on_block(action, gate_result)``, then seals a capsule with
        ``verdict="blocked"`` and ``effect.status="planned"``, and returns
        *output* unchanged.

    **Blocked path, no callback**:
        Raises :class:`GateBlockedError`.

    Args:
        action: Stable action name (e.g. ``"write_po"``).
        constraints: Constraints to evaluate.  Empty list always passes.
        inputs: Named inputs to the action (passed to each constraint's
            ``check()``).
        output: The action's output value (pass-through returned).
        emitter: A :class:`~capsule_emit.adapters._base.CapsuleEmitterBase`
            (or any object with ``emit_capsule(action, ...)``).
        on_block: Optional escalation callback.  Called when the gate fails
            *before* sealing the blocked capsule.  Signature:
            ``(action: str, gate_result: GateResult) -> None``.

    Returns:
        *output* (the action output, unmodified).

    Raises:
        :class:`GateBlockedError`: When the gate fails and no ``on_block``
            callback is provided.
    """
    gate_result = run_gate(constraints, inputs, output)
    gate_checks = gate_result.to_gate_checks()

    if gate_result.passed:
        emitter.emit_capsule(
            action,
            tool_input=inputs,
            tool_output=output,
            verdict="executed",
            effect={"type": action, "status": "dispatched"},
            extra_compute={"gate_checks": gate_checks},
        )
        return output

    # Gate failed.
    if on_block is not None:
        on_block(action, gate_result)
        emitter.emit_capsule(
            action,
            tool_input=inputs,
            tool_output=output,
            verdict="blocked",
            effect={"type": action, "status": "planned"},
            extra_compute={"gate_checks": gate_checks},
        )
        return output

    raise GateBlockedError(action, gate_result)
