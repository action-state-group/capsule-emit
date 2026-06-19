---
wicket_id: write-po
title: Purchase Order — write flow
operator: acme-co
version: 1
status: active
---

# Purchase Order — write flow

An agent extracts a purchase order from a vendor quote and dispatches it.
Every consequential action is sealed as an Agent Action Capsule.

## Constraints — the rules in force

| id | what it checks (plain English) | method | severity |
|----|--------------------------------|--------|----------|
| `po_arithmetic` | Line items + tax re-add to the stated total. | arithmetic_balance | **block** |
| `vendor_known` | Vendor is on the approved-supplier list. | set_membership | **warn** |

## Effect

`write_po` — autonomy `narrate`, reversibility `two_way`.
Default: narrate (describe; do not dispatch) until autonomy is deliberately raised.

## Human-in-the-loop

A `block` failure or total over the operator's standing authority defers
to operator approval. Approval is sealed as a disposition capsule.
