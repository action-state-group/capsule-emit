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
