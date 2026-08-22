# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
go fmt ./...            # CI fails if `gofmt -l .` is non-empty
go vet ./...
go test ./...
go test -race ./...     # CI runs this separately
scripts/check-coverage.sh 90.0   # enforces a 90% statement-coverage floor
go test ./evidence/ -run TestName   # run a single test by name (-run takes a regexp)
```

CI (`.github/workflows/ci.yml`) runs all of the above on every push and PR. The coverage floor is real: new code paths need same-change tests or the build fails.

## What this library is

It builds and signs [Agent Action Capsules](https://github.com/action-state-group/agent-action-capsule) (AAC) from facts an application observed at an external-effect boundary (an HTTP call, SQL write, message send). It is a thin, deterministic layer over the upstream `github.com/action-state-group/agent-action-capsule/go` module, which owns all canonicalization, digesting, capsule-ID computation, and Class 1 verification. Do not reimplement those; call `canonical.*` and `verify.*`.

It deliberately does **not** execute actions, generate action/business IDs, retry, persist statements, or maintain a ledger. When tempted to add any of those, it belongs in the caller, not here (see `doc.go` and README "Non-goals").

## Architecture

Two packages, one pipeline:

```
adapter captures facts  →  evidence.DigestExchanges  →  producer.Build / Create  →  signed COSE_Sign1
(evidence/httpfact)         request_digest +             AAC payload + capsule_id     statement
                            response_digest
```

- **`evidence/`** — turns observed request/response facts into ordered, minimized JSON and digests them. `RequestFact` / `ResponseFact` are the extension points; `httpfact` is the one built-in transport. Other transports implement the same interfaces without dragging HTTP concepts into the core.
- **root `producer` package** — `build.go` (payload assembly + validation), `sign.go` (`Create`, COSE signing), `verify.go` (`VerifyStatement`), `model.go` (`Input`/`Effect`/`Disposition`/`Chain`), `constants.go` (typed protocol vocabularies).

`Create` = `Build` + COSE_Sign1 wrap. `Build` validates, derives assurance, computes `capsule_id`, and **round-trips through the upstream Class 1 verifier before returning**. A produced payload that would fail verification is an error, not output.

## Invariants that span files (read before changing behavior)

- **Assurance is derived, never supplied.** `assuranceMap` in `build.go` derives `effect_mode` from `Effect.Status` and `ledger_mode` from whether a `Chain` is present. `attestation_mode` is not derived from either input; it is always `self_attested`, because this library only self-attests and never anchors to a transparency log. `Input` has no fields for any of the three, and callers must not be able to inject them. Keep this mapping and the README "Typed protocol values" table in sync.

- **Effect status gates digests and attestation.** `validateEffect` enforces a matrix: `planned` must carry no digests and no attestation; `dispatched` must not carry a response digest; `confirmed` must carry a response digest; every non-`planned` status requires an attestation. A request digest is permitted but never *required* by any status. Changing effect handling means updating this matrix and its tests together.

- **Response-observed vs response-evidence are distinct.** `DigestExchanges` only folds a response into `response_digest` when `ResponseObserved()` is true; if any exchange is unobserved, `ResponseKnown` is false and `ResponseDigest` is empty. `NoResponse` returns diagnostic evidence but reports `ResponseObserved() == false` so a failed call never masquerades as a confirmed one.

- **Closed enums vs registry-backed values.** `ActionType`, `Approver`, `EffectStatus` are closed and validated strictly. Registry-backed values (`Decision`, `VerdictClass`, `IrreversibilityClass`, `EffectAttestation`, `ChainRelation`) seed the draft-02 constants in `constants.go` but permit registered extensions; `knownRegistries()` in `verify.go` lists the seeds. `ChainConfirms` is a library convenience extension, **not** draft-02 registered, so verification reports it as an informational finding rather than rejecting it.

- **Determinism.** Payloads are RFC 8785 JCS via the upstream `canonical.JCS`. `capsule_id` is computed excluding top-level `capsule_id` and `chain`, so adding a chain to an already-chained payload does not change the ID, but adding the *first* chain flips `ledger_mode` (which is inside the ID). Do not introduce non-deterministic ordering or wall-clock defaults; `Input.Timestamp` and `ActionID` are always caller-supplied.

- **Defensive decoding — two distinct validators.** `DecodePayload` (build.go) parses raw JSON *bytes* on the verify path and rejects duplicate object keys, trailing data, non-UTF-8, and nesting past `maxJSONDepth` (1000). The evidence validators (evidence.go) run over an already-materialized `map[string]any`, where duplicate keys and trailing bytes cannot exist; they instead enforce the value contract — only `bool`, `string`, `json.Number` (canonical integers), and non-empty `[]any` / `map[string]any` are allowed. Everything else is rejected with the offending path in the message: disallowed types (Go ints, floats, typed slices), non-UTF-8, empty or nil containers, and nesting past `maxEvidenceDepth` (1000). Preserve both when touching decode/validation paths.

## Testing conventions

Use `github.com/stretchr/testify` (`assert`/`require`), not raw `if` + `t.Errorf`. Tests live beside their source (`build_test.go`, `evidence/evidence_test.go`, etc.); `e2e_test.go` exercises the full capture→sign→verify path.
