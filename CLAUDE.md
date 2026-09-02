# CLAUDE.md

Guidance for coding agents working in this repository.

## Commands

```bash
go fmt ./...
go mod tidy
go vet ./...
go test ./...
go test -race ./...
scripts/check-coverage.sh 90.0
```

CI runs these checks and the frozen upstream vector suites. New logic needs
same-change tests, including error paths, to preserve the 90-percent coverage
floor.

## Scope

This library builds Agent Action Capsule format 4 records from values observed
by an application at an external-effect boundary. It is a thin deterministic
layer over `github.com/action-state-group/agent-action-capsule/go`; use upstream
canonicalization, Capsule-ID, Class 1, and Producer Envelope verification rather
than reimplementing them.

The library does not execute actions, generate business IDs or timestamps,
retry calls, persist Capsules, authorize signers, or maintain a ledger.

## Architecture

```text
JSON values → DigestJSON → Effect digests → Build ───────────────→ Capsule
foreign bytes ───────────────────────→ Carry / Received ─────────→ Capsule
existing Capsules → Who / Can / Did / Audit → BuildComposition ─→ Capsule

Capsule + identity → Sign → Producer Envelope
Input + identity → Seal (= Build + Sign) → Capsule + Envelope
```

- `digest.go` marshals caller-owned JSON values and derives strict AAC
  JSON-DIGEST values through upstream JCS.
- `build.go` assembles, validates, identifies, and Class-1-verifies Capsules.
- `construction.go` owns typed foreign-artifact and composition bindings.
- `sign.go` owns immutable signing identities, `Sign`, and `Seal`.
- `verify.go` separates Capsule Class 1 from Producer Envelope verification.
- `model.go` and `constants.go` own typed inputs and protocol vocabulary.

## Invariants

- **Format 4 only.** Emit draft-04, format 4, and
  `canonicalization_id: "jcs"`. Do not restore format-2 construction or the
  legacy signed-payload statement API.
- **Signer-independent identity.** Capsule ID excludes top-level `capsule_id`
  and local-only Producer Envelope fields `signature` and `key_id`; `chain`
  remains committed. `VerifyCapsule` does not authenticate those local-only
  fields; consumers separately verify the envelope and bind its public key to
  any outer `key_id`. Signing one or many envelopes never changes Capsule bytes
  or ID.
- **Exact Producer Envelope.** Attached payload is raw 32-byte Capsule ID;
  protected headers are exactly EdDSA `alg`, the AAC Capsule-ID content type,
  and raw 32-byte Ed25519 public-key `kid`; unprotected map is empty. Signer
  authorization remains caller policy.
- **Assurance is derived.** Callers cannot inject effect, attestation, or ledger
  assurance. Preserve the effect-status matrix in `validateEffect`.
- **Foreign bytes stay bytes.** `Carry` and `Received` digest exact transmitted
  bytes and never parse or canonicalize them. `Received` requires a non-empty
  caller-declared type.
- **Composition is typed.** `Who`, `Can`, `Did`, and `Audit` bind existing
  Capsule IDs to closed slot vocabulary. `BuildComposition` canonicalizes slot
  order and rejects empty membership, duplicate slots, duplicate IDs, and
  unverified members. Persistence is external.
- **Determinism.** Do not add wall-clock defaults, random action IDs, implicit
  sorting, or mutable global protocol state.
- **Defensive decoding.** Preserve duplicate-key, trailing-data, invalid UTF-8,
  unsafe-number, negative-zero normalization, and depth checks on their existing
  paths.

## Testing conventions

Use `github.com/stretchr/testify/assert` and `require`. Tests live beside source.
Set `AAC_REPO` to an `agent-action-capsule` checkout to run frozen Capsule and
Producer Envelope corpus tests locally; CI pins and supplies the upstream ref.
