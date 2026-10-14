# SPDX-License-Identifier: Apache-2.0
"""Every ``disposition.verdict_class`` capsule-emit writes itself is
registered (agent-action-capsule ``spec/REGISTRY.md``, read through the
installed package's ``load_registries``), or is absent.

The one exception is ``countersign_refused`` from ``seal_ghost``: it stays
until draft-mih-agent-bilateral-attestation-03 registers it. The test fails
when that value becomes registered, so the exception is removed then rather
than left behind.
"""
from __future__ import annotations

import pytest
from _library_emissions import library_emissions
from agent_action_capsule.registries import load_registries

# producer -> the unregistered verdict_class it is allowed to write for now.
PENDING_REGISTRATION = {"bilateral.seal_ghost": "countersign_refused"}


@pytest.fixture(scope="module")
def emissions(tmp_path_factory):
    return library_emissions(tmp_path_factory.mktemp("emissions"))


@pytest.fixture(scope="module")
def registered_verdict_classes():
    return load_registries()["verdict_class"]


def test_core_helper_verdict_class_is_registered_or_absent(emissions, registered_verdict_classes):
    bad = {}
    for producer, capsule in emissions.items():
        value = capsule["disposition"].get("verdict_class")
        if value is None or value in registered_verdict_classes:
            continue
        if PENDING_REGISTRATION.get(producer) == value:
            continue
        bad[producer] = value
    assert not bad, f"unregistered verdict_class written by capsule-emit: {bad}"


def test_pending_registrations_are_still_unregistered(emissions, registered_verdict_classes):
    for producer, value in PENDING_REGISTRATION.items():
        assert emissions[producer]["disposition"]["verdict_class"] == value
        assert value not in registered_verdict_classes, (
            f"{value!r} is now registered: drop it from PENDING_REGISTRATION"
        )


def test_log_entry_has_no_verdict_class(emissions):
    assert "verdict_class" not in emissions["log"]["disposition"]
    assert emissions["log"]["action_type"] == "fyi"


def test_adjudication_has_no_verdict_class(emissions):
    assert "verdict_class" not in emissions["seal_adjudication"]["disposition"]
    assert emissions["seal_adjudication"]["action_type"] == "decide"


def test_bilateral_completion_is_executed_with_a_confirmed_effect(emissions):
    capsule = emissions["bilateral.seal_bilateral"]
    assert capsule["disposition"]["verdict_class"] == "executed"
    assert capsule["effect"]["status"] == "confirmed"
    assert capsule["action_type"] == "decide"
