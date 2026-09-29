# Capsule conformance vectors

These are frozen Class-1 vectors for the Agent Action Capsule profile in
`../../spec/`. Every case contains an input and its expected structured result.

Format 4 with `canonicalization_id: "jcs"` is the only supported construction.
Capsule ID removes only the top-level `capsule_id` and applies plain RFC 8785
JCS. Present null, empty-array, empty-object, `chain`, and `references` members
all participate. Deliberate negative cases prove that format versions 1, 2,
and 3 and invalid format-4 canonicalization declarations fail closed.

## Expected-result provenance

Reference-derived cases are frozen outputs from the reference verifier.
Spec-derived cases are hand-derived from the draft and RFC 8785 and MUST NOT be
regenerated from an implementation. Each newly derived SHA-256 value records
the exact canonical string in `canonical_preimages`.

## Expected result

- `ok`: whether Class-1 verification succeeds.
- `findings[].check`: the governing Class-1 check number, with `error`,
  `warning`, or `info` severity.
- `derived`: rederived `effect_mode`, `attestation_mode`, and `ledger_mode`.
- `capsule_id_recomputed`: SHA-256 over the selected canonical preimage, or
  null when format/canonicalization selection fails closed or the input is not
  canonicalizable.
- `code` and `detail`: stable diagnostic labels and explanatory text.

Store-level inputs use `{"ledger": [...]}` and expected results use
`{"results": [...]}`. All other cases contain one Capsule.

## Coverage

The corpus covers verdict classes, the effect-attestation matrix, confirmed
effect binding, chain/store semantics, cross-party assurance, references,
registry handling, format-4 RFC 8785 edge cases, identity tampering, and
structural failures. The only Capsules declaring format versions 1, 2, or 3
are the three deliberate fail-closed cases. The format-4 declaration negatives
cover absent, null, non-string, empty-string, and withdrawn `jcs-n` values.

`neg-unsafe-integer-in-digest-field` enforces the requirement that an integer
outside the IEEE-754 safe range be represented as an exact decimal string.

## Layout

```text
vectors/capsule/
  README.md
  vectors.json
  SHA256SUMS
  <case>/input.json
  <case>/expected.json
```
