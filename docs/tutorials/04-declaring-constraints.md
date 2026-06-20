# Declaring rules (no enforcement yet)

**Goal:** write down the rules an action should follow — in plain English, in a file
next to your code — so a gateway can enforce them later **without you changing any
code**.

## The idea: declare now, enforce later

You probably already know the rules your agent *should* follow ("the PO math has to
add up", "the vendor has to be approved"). Capsules let you **write those down in a
manifest** today. `capsule-emit` just **records** them — it doesn't block anything.
Later, a compatible gateway can read the **same file** and actually **enforce** them,
and your `emit()` calls don't change at all.

Think of it as leaving the rules where the enforcement will plug in.

## Write a manifest

Drop a file at `flows/write-po/manifest.md`:

```markdown
---
wicket_id: write-po
title: Purchase Order — write flow
operator: acme-co
version: 1
status: active
---

# Purchase Order — write flow

An agent extracts a purchase order from a vendor quote and dispatches it.

## Constraints — the rules in force

| id | what it checks (plain English) | method | severity |
|----|--------------------------------|--------|----------|
| `po_arithmetic` | Line items + tax re-add to the stated total. | arithmetic_balance | **block** |
| `vendor_known`  | Vendor is on the approved-supplier list.      | set_membership     | **warn**  |

## Effect

`write_po` — autonomy `narrate` (describe; don't dispatch) until raised deliberately.
```

The columns are for humans first: an `id`, what it checks in plain English, a
`method` name, and a `severity` (`block` stops the action; `warn` just flags it).

## Read it back

```python
from capsule_emit.manifest import load_manifest

m = load_manifest("flows/write-po/manifest.md")
print("flow:      ", m.wicket_id)
print("autonomy:  ", m.autonomy)
print("effect:    ", m.effect_type)
print("rules:     ", m.constraint_names)
```

```console
$ python read_manifest.py
flow:       write-po
autonomy:   narrate
effect:     write_po
rules:      ['po_arithmetic', 'vendor_known']
```

That's the whole interface: the manifest *declares*; `capsule-emit` *reads*. No
enforcement engine, no dependency to install.

## What "enforce later" buys you

Because the rules live in a file and not in your `emit()` calls:

- **today:** the rules are documented, versioned, and travel with your code.
- **later:** point a compatible enforcement gateway at the same `flows/` directory.
  It reads `po_arithmetic` / `vendor_known`, runs them for real, and blocks or defers
  — and **you don't touch your agent code**. The autonomy level (`narrate` →
  act) is a setting in the manifest, not a rewrite.

You get to adopt sealing now and turn on enforcement when you're ready, on the same
declarations.

## You just

Wrote your action's rules in plain English, in a file your code and a future gateway
both read — the "declare now, enforce later" seam.

---

That's the core tour. From here:

- **[anatomy](../anatomy.md)** — exactly what's sealed inside a capsule (two tiers).
- **[adapters](../adapters/)** — let your framework emit capsules for you.
- **[going deeper — and popping out](../going-deeper.md)** — verify it yourself down
  the stack (spec + `scitt-cose`), or pop *up* to a gateway when you want these rules
  **enforced**, not just declared.
- The **[spec](https://datatracker.ietf.org/doc/draft-mih-scitt-agent-action-capsule/)** — the format itself, if you want the depths.
