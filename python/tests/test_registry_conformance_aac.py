# SPDX-License-Identifier: Apache-2.0
"""agent-action-capsule's registry-conformance check over every record from
``_library_emissions``: each registry-governed value capsule-emit writes
itself must be registered on the axis it is carried on.

``effect.type`` is reported, never failed: helpers write an action name there
(the caller's, or a fixed one such as ``seal_approval``'s ``review_action``),
and the registry's namespacing rule for it is not written yet. The
``effect.type`` findings go to stdout and, in CI, to the job summary; any
other non-conformant value fails the test.

``agent_action_capsule.registry_conformance`` is not in a released
agent-action-capsule yet, so this module skips in the main ``python`` job.
The ``registry-conformance`` job in ``.github/workflows/python.yml`` installs
the agent-action-capsule commit that has it and checks the import first, so
it cannot skip there.
"""
from __future__ import annotations

import os

import pytest
from _library_emissions import library_emissions

rc = pytest.importorskip(
    "agent_action_capsule.registry_conformance",
    reason="needs the agent-action-capsule commit the registry-conformance CI job installs",
)

REPORT_ONLY_PATHS = ("effect.type",)

# (producer, field path, value) still written while a ruling or a follow-up
# is open. The test fails when one of these becomes conformant, so it is
# removed then rather than left behind.
KNOWN_NONCONFORMANT = {
    # Registered through draft-mih-agent-bilateral-attestation-03 (em-spec).
    ("bilateral.seal_ghost", "disposition.verdict_class", "countersign_refused"),
    # seal_approval writes its decision argument as-is; the registered values
    # are accept/reject. Not part of the verdict_class change.
    ("seal_approval (approve)", "disposition.decision", "approve"),
    ("seal_approval (deny)", "disposition.decision", "deny"),
}


@pytest.fixture(scope="module")
def findings(tmp_path_factory):
    emissions = library_emissions(tmp_path_factory.mktemp("emissions"))
    axes, aliases = rc.load_axes(), rc.legacy_aliases()
    return {
        producer: rc.check_capsule(capsule, axes=axes, aliases=aliases)
        for producer, capsule in emissions.items()
    }


def _report(lines: list[str]) -> None:
    text = "\n".join(lines) + "\n"
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text)


def test_registry_governed_values_conform(findings):
    # every record carries at least disposition.decision, so an empty list
    # means the check ran over nothing
    assert all(findings.values()), [p for p, fs in findings.items() if not fs]
    assert any(f.path == "disposition.verdict_class" for f in findings["bilateral.seal_bilateral"])
    failing = {
        (producer, f.path, f.value): f.detail
        for producer, fs in findings.items()
        for f in fs
        if not f.conformant and f.path not in REPORT_ONLY_PATHS
    }
    unexpected = {k: v for k, v in failing.items() if k not in KNOWN_NONCONFORMANT}
    assert not unexpected, f"non-conformant registry values: {unexpected}"
    resolved = KNOWN_NONCONFORMANT - failing.keys()
    assert not resolved, f"now conformant, drop from KNOWN_NONCONFORMANT: {sorted(resolved)}"


def test_report_effect_type(findings):
    lines = ["### effect.type (report-only)", "", "| producer | value | status |", "|---|---|---|"]
    for producer, fs in findings.items():
        for f in fs:
            if f.path in REPORT_ONLY_PATHS:
                lines.append(f"| {producer} | `{f.value}` | {f.status} |")
    assert len(lines) > 4, "no record carried effect.type, so nothing was reported"
    _report(lines)
