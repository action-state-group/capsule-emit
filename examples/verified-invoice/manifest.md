---
wicket_id: "verified-invoice"
title: "Verified Invoice Payment"
autonomy: "execute"
---

## Effect

`pay_invoice` — autonomy `execute`

## Constraints

| constraint id | what it checks | tier | method | blocking |
|---|---|---|---|---|
| `invoice_reconciles` | Line items sum equals declared invoice total (exact arithmetic) | standard | arithmetic_sum | **block** |
| `value_grounded` | Quoted unit-price matches the cited source document | standard | exact_match | **block** |
| `amount_under_policy_cap` | Invoice total is below the $10 000 no-further-approval threshold | policy | threshold | **block** |
| `formal_arithmetic_verified` | Total arithmetic verified by a symbolic prover (recorded result, not computed here) | formal | symbolic_proof | **warn** |
