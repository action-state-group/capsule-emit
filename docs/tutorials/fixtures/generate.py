# SPDX-License-Identifier: Apache-2.0
"""Generate the two sample ledgers used by tutorial A1 (see-a-ledger).

These ledgers are produced by **real** ``seal()`` calls — real Ed25519
signatures, real SHA-256 input/output digests, real content-addressed
``capsule_id``s, real chaining. What is *synthetic* is the run itself: no
model is ever called and no real-world effect happens. The tool "results"
below are fixed strings written by hand so the tutorial can show a complete,
self-contained story.

Determinism. So the committed ``ledger.jsonl`` files are byte-stable and this
script is safe to re-run in CI, three ambient inputs are pinned:

  1. the signing key — a fixed Ed25519 key derived from a constant 32-byte
     seed (never a real production key; it exists only to make the signatures
     reproducible for a docs fixture);
  2. ``action_id`` — the spec layer draws a UUID4 per action; here it is
     replaced with a seeded counter so the ids are stable;
  3. ``timestamp`` — pinned to a fixed UTC instant per capsule.

Everything else is the genuine library code path (CPB bind + sign + ledger
append). Witnessing is turned OFF (``CAPSULE_WITNESS=off``) so nothing leaves
the process and no checkpoint stamp is written — the fixtures are pure capsule
streams.

Run it:

    python docs/tutorials/fixtures/generate.py

It (re)writes, under this directory:

    refund-support-agent/ledger.jsonl        use case 1 — write actions + a refusal
    refund-support-agent/held-run.md         the verbose run the ledger records
    incident-investigation-agent/ledger.jsonl  use case 2 — read-only, tool-heavy
    incident-investigation-agent/held-run.md   the verbose run the ledger records
"""
from __future__ import annotations

import itertools
import os
import pathlib

# Witnessing off: no network, no checkpoint stamp entries — a pure capsule
# stream. Set before importing capsule_emit so the first-run notice never
# arms a network path.
os.environ["CAPSULE_WITNESS"] = "off"

HERE = pathlib.Path(__file__).resolve().parent

# A fixed Ed25519 seed. NOT a production key — its only job is to make the
# fixture signatures byte-reproducible. key_id is derived from it.
_SIGNING_SEED = bytes.fromhex("00" * 31 + "2a")
_KEY_PATH = HERE / ".fixture-signing-key.pem"

# A fixed instant per run so timestamps don't drift between regenerations.
_BASE_TS = "2026-08-25T15:04:05Z"


def _write_fixed_key() -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.from_private_bytes(_SIGNING_SEED)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    _KEY_PATH.write_bytes(pem)


def _pin_determinism() -> None:
    """Seed action_id and timestamp so the spec layer's per-action UUID4 and
    wall-clock reads become reproducible. The seal path is otherwise real."""
    import importlib

    # Import the submodule for its side effect (registering it) and bind the
    # module object directly — importlib.import_module keeps this lint-clean
    # (no "imported but unused") while still patching the real code path.
    _emit_mod = importlib.import_module("agent_action_capsule.emit")

    counter = itertools.count(1)

    class _SeededUUID:
        def __init__(self, n: int) -> None:
            # A UUID4-shaped, deterministic value: a fixed prefix + counter.
            self._hex = f"a1c0de00{n:024x}"

        def __str__(self) -> str:
            return (
                f"{self._hex[0:8]}-{self._hex[8:12]}-{self._hex[12:16]}-"
                f"{self._hex[16:20]}-{self._hex[20:32]}"
            )

    def _seeded_uuid4():
        return _SeededUUID(next(counter))

    _emit_mod.uuid.uuid4 = _seeded_uuid4  # type: ignore[assignment]
    _emit_mod._utc_now = lambda: _BASE_TS  # type: ignore[assignment]


def _fresh_ledger(path: pathlib.Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    return str(path)


def build_refund_support_ledger() -> pathlib.Path:
    """Use case 1 — a customer-refund support agent.

    A WRITE run with a refusal. The agent handles two refund requests in one
    session. It looks up each order and checks policy; one refund is inside
    policy and is issued (a real-world effect: a refund dispatched); the other
    fails the policy check and is refused (a ``blocked`` capsule — the gate
    firing is itself auditor-grade evidence). We seal one capsule per
    consequential action and chain the issued refund back to its policy check.
    """
    from capsule_emit import seal

    out_dir = HERE / "refund-support-agent"
    ledger = _fresh_ledger(out_dir / "ledger.jsonl")

    common = dict(
        operator="northwind-retail",
        developer="refund-agent@v3",
        ledger=ledger,
        signing_key_path=str(_KEY_PATH),
        model={"provider": "anthropic", "model_id": "claude-sonnet-4-6"},
    )

    # --- request A: order ORD-90287, $42.00, in policy -> refund issued ---

    look_up_a = seal(
        {"tool": "look_up_order", "order_id": "ORD-90287"},
        action="look_up_order",
        agent_output={
            "order_id": "ORD-90287",
            "status": "delivered",
            "total": "42.00",
            "delivered_at": "2026-08-19T00:00:00Z",
            "days_since_delivery": 6,
        },
        verdict="executed",
        effect={"type": "look_up_order", "status": "dispatched"},
        **common,
    )

    policy_a = seal(
        {
            "tool": "check_refund_policy",
            "order_id": "ORD-90287",
            "days_since_delivery": 6,
            "reason": "item_not_as_described",
        },
        action="check_refund_policy",
        agent_output={"eligible": True, "rule": "returns-window-30d", "within_days": 6},
        verdict="executed",
        effect={"type": "check_refund_policy", "status": "dispatched"},
        **common,
    )

    # The consequential write. Chained to the policy check that authorized it.
    seal(
        {
            "tool": "issue_refund",
            "order_id": "ORD-90287",
            "amount": "42.00",
            "currency": "USD",
        },
        action="issue_refund",
        agent_output={"refund_id": "RF-55021", "amount": "42.00", "state": "dispatched"},
        verdict="executed",
        effect={"type": "issue_refund", "status": "dispatched"},
        confirms=policy_a.capsule_id,
        **common,
    )

    # --- request B: order ORD-90291, $980.00, out of policy -> refused ---

    look_up_b = seal(
        {"tool": "look_up_order", "order_id": "ORD-90291"},
        action="look_up_order",
        agent_output={
            "order_id": "ORD-90291",
            "status": "delivered",
            "total": "980.00",
            "delivered_at": "2026-06-01T00:00:00Z",
            "days_since_delivery": 85,
        },
        verdict="executed",
        effect={"type": "look_up_order", "status": "dispatched"},
        **common,
    )

    policy_b = seal(
        {
            "tool": "check_refund_policy",
            "order_id": "ORD-90291",
            "days_since_delivery": 85,
            "reason": "changed_mind",
        },
        action="check_refund_policy",
        agent_output={
            "eligible": False,
            "rule": "returns-window-30d",
            "within_days": 85,
            "limit_days": 30,
        },
        verdict="executed",
        effect={"type": "check_refund_policy", "status": "dispatched"},
        **common,
    )

    # The refusal. A `blocked` verdict, chained to the policy check that denied
    # it (relation=None: it does not "confirm" that check, it records the refusal
    # that check drove). No effect record is passed — a `blocked` verdict never
    # dispatches, so its effect_mode must derive to `not_applicable` (spec §5.4.2);
    # omitting `effect=` is exactly how you seal "the gate fired, nothing ran".
    seal(
        {
            "tool": "issue_refund",
            "order_id": "ORD-90291",
            "amount": "980.00",
            "decision": "refuse",
        },
        action="issue_refund",
        agent_output={
            "refund_id": None,
            "state": "refused",
            "reason": "outside 30-day returns window (85 days since delivery)",
        },
        verdict="blocked",
        confirms=policy_b.capsule_id,
        relation=None,
        **common,
    )

    # touch to keep the linter from complaining about intentionally-unused ids
    _ = (look_up_a, look_up_b)
    return pathlib.Path(ledger)


def build_incident_investigation_ledger() -> pathlib.Path:
    """Use case 2 — an on-call incident-investigation agent.

    A READ-ONLY, tool-heavy run. The agent investigates why a deploy failed.
    It fetches the repo, searches logs, and reads the diff, then returns an
    advisory answer with proof citations — no write, no real-world effect.
    Every capsule is an ``fyi`` (a passive read/observation), a deliberately
    different shape from use case 1: no ``confirms`` chain, no effect record,
    no ``blocked``. The value is a complete, verifiable record of what the
    agent *looked at* before it advised.
    """
    from capsule_emit import seal

    out_dir = HERE / "incident-investigation-agent"
    ledger = _fresh_ledger(out_dir / "ledger.jsonl")

    common = dict(
        operator="northwind-retail",
        developer="oncall-investigator@v2",
        ledger=ledger,
        signing_key_path=str(_KEY_PATH),
        model={"provider": "anthropic", "model_id": "claude-opus-4-6"},
        # Reads, not decisions. action_type "fyi" is the spec's passive-
        # observation class (§5.1: action_type MUST be 'fyi' or 'decide') — the
        # capsule records what was looked at without gating or deciding. No
        # `effect=` is passed on any of these: a read dispatches nothing, so its
        # effect_mode must derive to `not_applicable`. That is the whole point of
        # this second shape — a verifiable record of what the agent *observed*,
        # with no real-world effect claimed anywhere.
        action_type="fyi",
    )

    seal(
        {"tool": "fetch_repo", "repo": "northwind/checkout-svc", "ref": "main"},
        action="fetch_repo",
        agent_output={
            "repo": "northwind/checkout-svc",
            "head_sha": "d4e9f10",
            "last_deploy": "deploy-4471",
            "deploy_status": "failed",
        },
        **common,
    )

    seal(
        {
            "tool": "search_logs",
            "service": "checkout-svc",
            "deploy": "deploy-4471",
            "query": "level=error",
            "window": "15m",
        },
        action="search_logs",
        agent_output={
            "matches": 3,
            "first_error": "startup probe failed: connect ECONNREFUSED redis:6379",
            "log_ref": "logs/deploy-4471#L2210",
        },
        **common,
    )

    seal(
        {
            "tool": "read_diff",
            "repo": "northwind/checkout-svc",
            "from": "deploy-4470",
            "to": "deploy-4471",
        },
        action="read_diff",
        agent_output={
            "files_changed": 2,
            "notable": "config/redis.yaml: host changed cache-redis -> redis",
            "diff_ref": "compare/deploy-4470...deploy-4471",
        },
        **common,
    )

    # The final advisory answer, sealed as its own read-class capsule. It
    # carries the observation and its proof citations; it dispatches nothing.
    seal(
        {
            "tool": "summarize_findings",
            "incident": "deploy-4471-failed",
            "prompt": "root cause + evidence",
        },
        action="summarize_findings",
        agent_output={
            "root_cause": (
                "deploy-4471 changed the Redis host in config/redis.yaml from "
                "'cache-redis' to 'redis', which does not resolve in this "
                "cluster; the startup probe then fails on ECONNREFUSED and the "
                "deploy is marked failed."
            ),
            "evidence": [
                "logs/deploy-4471#L2210",
                "compare/deploy-4470...deploy-4471",
            ],
            "recommendation": "revert the config/redis.yaml host change",
            "write_performed": False,
        },
        **common,
    )

    return pathlib.Path(ledger)


# --------------------------------------------------------------------------
# The "held run" companion fixtures.
#
# A capsule commits DIGESTS of the input/output — the raw prompt, reasoning,
# tool arguments, and tool results are NOT in the ledger; you (the operator)
# hold them. These two files ARE that held material for each use case, written
# out verbatim so the tutorial can show, side by side, "the run you hold" and
# "the ledger that records it" — and so a reader can re-hash a raw value and
# check it against the committed digest. They are static fixtures (the runs are
# synthetic — no model was ever called), regenerated here only so the whole
# fixture set lives behind one script.
# --------------------------------------------------------------------------

_REFUND_HELD_RUN = """\
# Held run — customer-refund support agent (synthetic, deterministic)

> **This is the raw material the operator holds.** None of the text below is in
> the ledger — the capsules commit only SHA-256 *digests* of the per-action
> input and output. This file is what those digests are digests *of*. The run
> is synthetic: no model was called and no refund was really dispatched.

- **Operator:** `northwind-retail`
- **Agent (developer):** `refund-agent@v3`
- **Model:** `anthropic / claude-sonnet-4-6`
- **Ledger it produced:** `refund-support-agent/ledger.jsonl` (6 capsules)

## System prompt

> You are Northwind Retail's refund support agent. For each customer request:
> look up the order, check it against the 30-day returns policy, and issue the
> refund only if the order is eligible. If it is outside policy, refuse and
> explain why. Never issue a refund that fails the policy check.

## Request A — in policy, refund issued

**User:** "Order ORD-90287 arrived damaged. I'd like a refund."

**Agent reasoning (held, not sealed):** The order was delivered 6 days ago and
the reason is item-not-as-described. That is inside the 30-day returns window,
so the refund is eligible. Look it up, confirm policy, then issue.

| # | Tool call (args) | Result | Capsule |
|---|---|---|---|
| 1 | `look_up_order(order_id="ORD-90287")` | delivered, $42.00, 6 days ago | `205b7676…` (executed) |
| 2 | `check_refund_policy(order_id="ORD-90287", days_since_delivery=6, reason="item_not_as_described")` | eligible=True, rule returns-window-30d | `5d0309e2…` (executed) |
| 3 | `issue_refund(order_id="ORD-90287", amount="42.00", currency="USD")` | refund RF-55021 dispatched | `d63f86d9…` (executed, `confirms→5d0309e2`) |

**Outcome:** refund **dispatched** ($42.00). The issue-refund capsule chains
back to the policy check that authorized it.

## Request B — out of policy, refused

**User:** "I changed my mind about order ORD-90291. Refund me."

**Agent reasoning (held, not sealed):** The order was delivered 85 days ago and
the reason is changed-mind. The returns window is 30 days, so this is outside
policy. The policy check returns ineligible; the refund must be refused.

| # | Tool call (args) | Result | Capsule |
|---|---|---|---|
| 4 | `look_up_order(order_id="ORD-90291")` | delivered, $980.00, 85 days ago | `4b928b13…` (executed) |
| 5 | `check_refund_policy(order_id="ORD-90291", days_since_delivery=85, reason="changed_mind")` | eligible=False, limit 30 days | `a81ba6e1…` (executed) |
| 6 | `issue_refund(order_id="ORD-90291", decision="refuse")` | refused: outside 30-day window | `3a027af5…` (**blocked**, no effect, `sequence→a81ba6e1`) |

**Outcome:** refund **refused**. The refusal is sealed as a `blocked` capsule
with **no effect record** — the gate firing is itself auditor-grade evidence
that a refund was *not* dispatched.
"""

_INCIDENT_HELD_RUN = """\
# Held run — on-call incident-investigation agent (synthetic, deterministic)

> **This is the raw material the operator holds.** None of the text below is in
> the ledger — the capsules commit only SHA-256 *digests* of the per-action
> input and output. This file is what those digests are digests *of*. The run
> is synthetic: no model was called and no repository was really read.

- **Operator:** `northwind-retail`
- **Agent (developer):** `oncall-investigator@v2`
- **Model:** `anthropic / claude-opus-4-6`
- **Ledger it produced:** `incident-investigation-agent/ledger.jsonl` (4 capsules)

## System prompt

> You are an on-call incident-investigation agent. Given a failed deploy,
> gather read-only evidence — the repo state, the error logs, the diff — and
> report the most likely root cause with citations. You have no write access;
> you investigate and advise, you do not change anything.

## User prompt

> "deploy-4471 for checkout-svc failed. What happened?"

## Agent reasoning (held, not sealed)

Start from the repo to confirm the failed deploy, then pull the error logs for
that deploy to find the first failure, then read the diff between the last good
deploy and this one to explain it. Every step is a read — nothing is changed.

| # | Tool call (args) | Result | Capsule |
|---|---|---|---|
| 1 | `fetch_repo(repo="northwind/checkout-svc", ref="main")` | head d4e9f10, last deploy deploy-4471 = failed | `2e3d7361…` (fyi) |
| 2 | `search_logs(service="checkout-svc", deploy="deploy-4471", query="level=error", window="15m")` | 3 matches; first error `ECONNREFUSED redis:6379`; `logs/deploy-4471#L2210` | `1ca2cd78…` (fyi) |
| 3 | `read_diff(repo="northwind/checkout-svc", from="deploy-4470", to="deploy-4471")` | 2 files changed; `config/redis.yaml` host `cache-redis` → `redis`; `compare/deploy-4470...deploy-4471` | `b20d4e4e…` (fyi) |
| 4 | `summarize_findings(incident="deploy-4471-failed")` | root cause + evidence citations; write_performed=False | `be6e389c…` (fyi) |

## Outcome (the advisory answer, held)

> **Root cause:** deploy-4471 changed the Redis host in `config/redis.yaml` from
> `cache-redis` to `redis`, which does not resolve in this cluster; the startup
> probe then fails on `ECONNREFUSED` and the deploy is marked failed.
>
> **Evidence:** `logs/deploy-4471#L2210`, `compare/deploy-4470...deploy-4471`
>
> **Recommendation:** revert the `config/redis.yaml` host change.

No write was performed. Every capsule is an `fyi` observation with **no effect
record** — the value is a verifiable record of exactly what the agent *looked
at* before it advised.
"""


def _write_held_runs() -> None:
    (HERE / "refund-support-agent" / "held-run.md").write_text(_REFUND_HELD_RUN)
    (HERE / "incident-investigation-agent" / "held-run.md").write_text(_INCIDENT_HELD_RUN)


def _cleanup_lock_files() -> None:
    """Ledger locks carry a PID/hostname and are not deliverables — never
    commit them. Remove any left after generation."""
    for lock in HERE.glob("*/ledger.jsonl.lock"):
        lock.unlink()


def main() -> None:
    _write_fixed_key()
    _pin_determinism()
    a = build_refund_support_ledger()
    b = build_incident_investigation_ledger()
    _write_held_runs()
    # Clean up the fixture key — it is not a deliverable, and regeneration
    # re-derives it from the fixed seed.
    if _KEY_PATH.exists():
        _KEY_PATH.unlink()
    _cleanup_lock_files()
    print(f"wrote {a.relative_to(HERE.parent.parent.parent)}")
    print(f"wrote {b.relative_to(HERE.parent.parent.parent)}")
    print(f"wrote {(HERE / 'refund-support-agent' / 'held-run.md').relative_to(HERE.parent.parent.parent)}")
    print(f"wrote {(HERE / 'incident-investigation-agent' / 'held-run.md').relative_to(HERE.parent.parent.parent)}")


if __name__ == "__main__":
    main()
