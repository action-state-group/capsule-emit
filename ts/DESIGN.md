# capsule-emit-ts design

Status: initial TypeScript implementation contract.

## Scope

This ESM package builds, signs, and verifies AAC format-4 records. The root
entry point does not persist, retry, witness, execute actions, or invent IDs and
timestamps. Optional persistence lives behind the `./artifact` subpath, which
imports no storage driver into the root; see [Artifact storage](#artifact-storage).

## Source baseline

| Repository             | Revision                                   | Authority                                         |
| ---------------------- | ------------------------------------------ | ------------------------------------------------- |
| `agent-action-capsule` | `7e112c8b877ad79d4d2a53be7b522a63470a2b1d` | Pinned draft-04 implementation and frozen vectors |
| `agent-action-capsule` | `439dc02c05d1177ea7c786bf759994d71b85ccc3` | Draft-05 wire, registries and vector layout       |
| `capsule-emit-go`      | `280596e03070d6c3333224313fd6aa20b0cb992a` | Public producer API and behavior                  |
| `capsule-emit`         | `40b592192e19622ff7a8c82674eb7caddb52e8db` | Released 0.7.0 Python byte-exact fixtures         |

These revisions record the historical implementation baseline. Runtime record
verification uses exact npm `@action-state-group/agent-action-capsule@0.1.0`,
whose reviewed artifact comes from `36d6770cf1856ed9043d98782275a14ce221fdde`
(PR #184). `AAC_COMMIT` in `test/aac-pin.ts` pins the same-commit authoritative
corpus in tests, CI and publication checks. Other peer interoperability inputs
continue to use their repositories' `main`.

AAC stays external in the emit build and retains its BSD-3-Clause LICENSE in
the installed dependency. Emit remains Apache-2.0; no AAC implementation is
bundled into its package.

At `439dc02` AAC moved its corpora to `vectors/capsule/` and
`vectors/producer-envelope/`, folded the cross-record reference vectors into
`vectors/capsule/reference-*`, dropped format 2 from the Class 1 verifier, and
defined draft -05. This package stamps `spec_version`
`draft-mih-scitt-agent-action-capsule-05` (`SPEC_VERSION`); its verifiers
accept -04 and -05 (`ACCEPTED_SPEC_VERSIONS`) and never branch on the value:
it selects no algorithm, and an unrecognized value is not a rejection.

## Parity matrix

| Go                            | TypeScript                                       | Wire behavior and coverage                                                                 |
| ----------------------------- | ------------------------------------------------ | ------------------------------------------------------------------------------------------ |
| `DigestJSON`                  | `digestJSON`                                     | SHA-256 over RFC 8785 JCS; rejects floats and unsafe integers; normalizes negative zero    |
| `DecodePayload`               | `decodePayload`                                  | Strict UTF-8 object decode; rejects duplicates and trailing data; preserves number lexemes |
| `Build`                       | `build`                                          | Draft-05, format 4, `jcs`, UTC microseconds, derived assurance, signer-independent ID      |
| `Carry`                       | `carry`                                          | Exact opaque bytes using artifact type `foreign-artifact`                                  |
| `Received`                    | `received`                                       | Exact opaque bytes with a caller-declared non-empty type                                   |
| `Who`/`Can`/`Did`/`Audit`     | `who`/`can`/`did`/`audit`                        | Verified Capsule slot wrappers                                                             |
| `BuildComposition`            | `buildComposition`                               | One member per slot, unique IDs, order `who,can,did,audit`                                 |
| `Sign`                        | `sign`                                           | Tagged COSE_Sign1 with the raw Capsule-ID bytes attached as its payload                    |
| `Seal`                        | `seal`                                           | Delegates to digest, build or composition, and sign                                        |
| `VerifyCapsule`               | `verifyCapsule`                                  | Format-4 gate and complete Class 1 result on failure                                       |
| `VerifyEnvelope`              | `verifyEnvelope`                                 | Exact protected map, empty unprotected map, payload binding, Ed25519                       |
| `IsV4IrreversibilityClass`    | `isV4IrreversibilityClass`                       | Tests the pinned draft-04 seed set                                                         |
| signing identity constructors | `createEd25519Identity`, `createSigningIdentity` | Immutable signer and raw public-key pair                                                   |

Tests replay the complete upstream Class 1 corpus, including all canonical,
positive, negative, store, honesty, and JCS cases, every Producer Envelope
case, plus the five Go/Python authored, received, WHO, DID, and composition
fixtures byte for byte.

## Types and timestamps

Public wire types are explicit and readonly. Binary APIs use `Uint8Array` and
return copies. `Buffer` works through `Uint8Array` inheritance.

Timestamps accept `Date` or RFC 3339 strings. Strings retain sub-millisecond
input before truncation to microseconds. Output is UTC: whole seconds omit the
fraction, while non-zero fractions contain exactly six digits.

## JSON, JCS, and identity

The strict decoder rejects malformed UTF-8, duplicate keys, trailing data,
invalid surrogates, and nesting beyond the Go limit of 1,000. It preserves
number lexemes so Class 1 rejects every lexical float, including `2.0` and
`1e2`, and reports floats and unsafe integers at exact paths. It normalizes the
decoded integer lexeme `-0` to `0` before identity computation.
The JCS layer rejects those values when canonical bytes are requested.

JavaScript input rejects unsupported values, sparse arrays, cycles, non-plain
objects, non-finite or non-integer numbers, and integers outside the safe range.
Object names sort by UTF-16 code units. Negative zero serializes as zero.

JavaScript number inputs are checked by value, while decoded JSON numbers use
the lexical rule above. Format-4 Capsule identity removes only top-level `capsule_id`, `signature`, and
`key_id`. It retains `canonicalization_id` and `chain`.

## Low-level AAC compatibility subpath

`@action-state-group/capsule-emit/aac` re-exports canonicalization, identity,
Class-1 and store verification from the authoritative AAC `/core` entry.
It supports format 4 and preserves AAC's async behavior. It does not retain a
private verifier, registry mirror, or vintage format-2 algorithm.

Digest-dependent producer APIs and all verification APIs are awaited before
acceptance or signing. Decoding, JCS encoding, opaque slot wrappers, signing
identity constructors and raw Capsule-ID signing remain synchronous.
Unknown registry values remain informational. `verifyCapsule` throws
`CapsuleVerificationError` with the complete AAC Class-1 result on failure.

Artifact backends await authentication and bound-original verification before
writes or successful reads. SQLite `putTx`/`getTx` now return promises: callers
must use explicit BEGIN/await/COMMIT and roll back on rejection, never an async
callback passed to better-sqlite3's synchronous `transaction` helper. The
caller-owned connection must have only one in-flight transaction. MySQL uses
its existing async transaction lifecycle; JSONL awaits verification before
its synchronous file mutation. No additional queue or recovery mechanism is
introduced.

## Producer Envelope

AAC's `producer-envelope-wire.ts` owns the exact COSE encoding, and its
Producer Envelope modules own Ed25519 signing and verification. The protected
map retains the frozen byte order: content type label 3, raw 32-byte public-key
`kid` label 4, then EdDSA label 1. Tagged COSE_Sign1 carries the raw Capsule ID
in its payload slot; the signature covers
`["Signature1", protected, empty-bstr, raw-id]`.
Emit's `envelope.ts` re-exports AAC identity/raw-ID signing and envelope
verification, with an awaited complete Capsule verification before its public
`sign` wrapper delegates to AAC.

Verification requires tag 18, four array items, empty unprotected map, exactly
three protected headers, a 32-byte payload, a 64-byte signature, and no more
than 4,096 total envelope bytes.

The raw `signCapsuleId` primitive replays the upstream synthetic envelope
whose ID has no matching Capsule. Public `sign` first verifies that its built
Capsule matches the ID, then delegates to the same primitive.

## Toolchain

Node.js 24 LTS, npm, TypeScript 7 strict mode, Vitest 4, and tsup are used.
AAC owns envelope encoding, protected-header decoding and cryptographic
verification; emit retains the awaited verify-before-sign producer wrapper. GitHub Actions use
read-only permissions and immutable action revisions.

## Artifact storage

The optional `./artifact` subpath persists exact sealed Capsules, Producer
Envelopes, and business originals. It neither seals nor appends to a CLL and is
distinct from ledger maintenance, checkpointing, and witnessing. It is the
byte-compatible TypeScript port of `capsule-emit-go`'s `artifact` package.

`./artifact` exports the storage-driver-free core: the `Record`/`Artifact`
model, retention states (`present`, `purged`, `never_retained`), digest bindings
to the four supported format-4 locations, `prepare`, `verify` (which reuses
`verifyCapsule`, `verifyEnvelope`, `decodePayload`, and `digestJSON`), and
`storageChecksum`. Three backends build on it: `./artifact/sqlite` on
`better-sqlite3` and `./artifact/mysql` on `mysql2` (declared as optional peer
dependencies so importing the root pulls no database driver), and
`./artifact/jsonl` on a single flat file using Node's built-in `fs` (no peer
dependency). All share one `Store` contract with immutable records,
byte-identical idempotent retry, `conflict` on divergence, fail-closed reads,
purge tombstones, namespace scoping, and the v1 limits (64 artifacts,
65,535-byte envelope, 8 MiB total); the SQL backends additionally support
caller-transaction joins, which the JSONL backend does not.

`storageChecksum` is byte-identical to the Go `StorageChecksum` — `sha256` over
the normalized record marshaled with Go `encoding/json/v2`, reproduced here with
`JSON.stringify` over a field-ordered projection, standard-base64 byte fields,
and `omitempty` semantics — so a Go-written row and a TypeScript reader
interoperate over a shared database. json/v2 encodes an empty inventory as `[]`
(v1 used `null`), which `JSON.stringify([])` matches. Frozen Go golden vectors,
including an empty-inventory case, pin this. MySQL integration tests are opt-in
behind `ARTIFACT_MYSQL_TEST=1` and use testcontainers.

## Exclusions

- format-2 or format-3 construction and top-level emitter verification;
- Python persistence, pass-through, checkpoint, and witness conveniences;
- implicit IDs or time, action execution, retries, root-level storage,
  authorization, anchors, policy evaluation, or application request/response
  projections. Verified artifact persistence is available only through the
  opt-in `./artifact` subpath above.
