# SPDX-License-Identifier: Apache-2.0
"""Integer minor units only -- no floats anywhere in reserve/release/expire/
reconcile amounts. Direct builder-level MUST-FAIL tests."""
from __future__ import annotations

import pytest

from capsule_emit.holds import Action
from capsule_emit.holds.capsules import (
    build_hold_expire_capsule,
    build_hold_reconcile_capsule,
    build_hold_release_capsule,
    build_hold_reserve_capsule,
)
from capsule_emit.holds.errors import FLOAT_IN_HOLD_AMOUNT, NON_INTEGER_HOLD_AMOUNT, HoldError

ACTION = Action(verb="transfer_funds", operator="op", developer="dev1", action_class="money.transfer", amount_minor=100)


def test_reserve_rejects_float_amount():
    with pytest.raises(HoldError) as exc:
        build_hold_reserve_capsule(action=ACTION, reserved_amount_minor=100.0, aggregate_before_minor=0)
    assert exc.value.reason == FLOAT_IN_HOLD_AMOUNT


def test_release_rejects_float_amount():
    with pytest.raises(HoldError) as exc:
        build_hold_release_capsule(action=ACTION, reserve_capsule_id="a" * 64, reserved_amount_minor=100.5)
    assert exc.value.reason == FLOAT_IN_HOLD_AMOUNT


def test_expire_rejects_float_amount():
    with pytest.raises(HoldError) as exc:
        build_hold_expire_capsule(action=ACTION, reserve_capsule_id="a" * 64, reserved_amount_minor=100.5)
    assert exc.value.reason == FLOAT_IN_HOLD_AMOUNT


@pytest.mark.parametrize(
    "kwargs",
    [
        {"reserved_amount_minor": 100.0, "executed_amount_minor": 100, "tolerance_minor": 0},
        {"reserved_amount_minor": 100, "executed_amount_minor": 100.0, "tolerance_minor": 0},
        {"reserved_amount_minor": 100, "executed_amount_minor": 100, "tolerance_minor": 0.5},
    ],
)
def test_reconcile_rejects_float_anywhere(kwargs):
    with pytest.raises(HoldError) as exc:
        build_hold_reconcile_capsule(
            action=ACTION, reserve_capsule_id="a" * 64, execution_capsule_id=None, **kwargs
        )
    assert exc.value.reason == FLOAT_IN_HOLD_AMOUNT


def test_bool_amount_is_not_treated_as_integer():
    """bool is an int subclass in Python -- must not silently pass as a
    legitimate amount."""
    with pytest.raises(HoldError) as exc:
        build_hold_release_capsule(action=ACTION, reserve_capsule_id="a" * 64, reserved_amount_minor=True)
    assert exc.value.reason == NON_INTEGER_HOLD_AMOUNT
