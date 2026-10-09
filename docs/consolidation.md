# Producer language-root consolidation

The execution scope is [#286](https://github.com/action-state-group/capsule-emit/issues/286).
Code moving to AAC, EvidenceBook or capsule-viewer is tracked separately in
[#290](https://github.com/action-state-group/capsule-emit/issues/290).

## Import inputs

| Source | Frozen commit | Tracked files | Destination |
|---|---|---|---|
| capsule-emit | `6c18d2608e383b64d631f5fffae1f640e42d1415` | Existing Python/Rust tree | Python project moves to `python/`; Rust workspace stays |
| capsule-emit-go | `415c38f21449941c43c4729f23da8a7ebbae04ac` | 119 | `go/`; initial history import plus #11 port |
| capsule-emit-ts | `e73735ba3ec1029c5fba0544c2f6a421f7176621` | 41 | `ts/` |

Import preflight checks incoming tracked trees with this repository's hostname,
positioning and neutrality policies. The incoming code is read as inert text;
its scripts and dependencies are never executed in the secret-bearing job.
Missing neutrality configuration fails the check. TypeScript source and each
language root's pitch documents are included in scanning.

Preserve licenses, executable modes, generated modules, package data and frozen
vectors. Go and TS history must remain traceable to the source commits. Keep
Go's current module identity until a separately accepted consumer cutover;
source staging does not redirect existing Go consumers to this repository.
Preserve TS's npm identity and six exports, runtime AAC 0.1.0 and source-corpus
pin `36d6770cf1856ed9043d98782275a14ce221fdde`. Preserve Go's separate AAC
corpus pin `df3af95221da3cd77792a7fd1c24c1db9ce88376` and Python's declared
CLL `>=0.5,<0.6` bound.

## Python release isolation

Future Python releases use `python/v<project.version>` tags. Only published
GitHub releases in that namespace enter the Python publisher; Go `go/v*`,
TS `ts/v*` and Rust `crates/*` release events cannot upload Python artifacts.
The checked-out tag must match package metadata and be an ancestor of main.
The filename `.github/workflows/release.yml` and `pypi` environment remain the
existing PyPI Trusted Publisher identity. No publisher configuration changes
or registry uploads are part of source consolidation. Historical tags and
published versions remain unchanged.

## Acceptance still required

Complete language tests and external source/editable/wheel/sdist Python
installation checks follow the layout move. Go requires full race/storage
checks, frozen AAC corpus and live producer interoperability. TS requires
packed consumers, explicit real-MySQL coverage and relevant browser coverage;
its existing MySQL suite is opt-in and skipped tests do not establish parity.

Rust flattening waits for accepted Evidence Request relocation. Registry
publication, new publisher identities, consumer module migration, compatibility
removal and old-repository retirement are separate gates.

## Source-stage validation

The configured incoming-tree preflight passed before import. TS retains its exact source ancestry;
Go retains ancestry through the author-approved rewritten commits below. With author approval, five historical Go commits
received DCO sign-offs; their descendants were rewritten in the import branch
without changing file trees, authors or timestamps. The original source repos
and tags remain unchanged. Rewritten Go commits cannot retain their original
cryptographic signatures; the source repository remains the signed record. Python's 86 module files remain byte-identical
to the frozen emit base. Project metadata, dependencies, extras and console entrypoints are
unchanged. External source, editable, wheel and rebuilt-sdist consumers verify
sealing, Capsule verification, protobuf imports and CLI availability.

Local Go checks include exact pinned AAC corpus, real MySQL 8.4, race tests and
90.8% coverage against the existing 90% floor. TypeScript has 240 passing tests,
including real MySQL 8.4, and a packed consumer proving all six exports plus
core operation without optional storage drivers. The producer is Node-only;
its `node:crypto` import is not a browser compatibility claim.

DCO remains an unchanged merge gate. All imported and new commits carry
sign-offs. Nested workflows under `go/.github/` and `ts/.github/` are retained
only as imported history; active workflows live in the repository root.
The incoming-tree preflight is now manual because its pre-import check is
complete; regular root policy workflows scan subsequent changes.

## Go import provenance after DCO repair

The frozen Go source remains `2d2ec479397badb03a58f986cc6c0b9cc53802dc`
in the [original repository](https://github.com/action-state-group/capsule-emit-go/commit/2d2ec479397badb03a58f986cc6c0b9cc53802dc).
Its corresponding import parent is `554188c4b4cd682c3e9b7d88d53f165c5426c63d`.
These parents have identical file trees. The five sign-off additions below
change their descendant hashes while preserving all original author/committer
metadata and parent relationships through the rewrite.

| Original Go commit | Commit with author-approved sign-off |
|---|---|
| `c022f71e699bbb9fb00d7f13e182219fb0a94039` | `6384ab6dbd16066cd25f10c53dee8ad787d7e2eb` |
| `7f78287490384571e949cf963bf23911ab8e040f` | `a54dee29e6d1d4d62a05cba3ba17e7e1a5be3cdf` |
| `cd566cbbff5160dcf0d91d549fee4f8cfeff0013` | `e388ecae069ac22b617ea05fccfc012d0dd8885a` |
| `3210173c86c8b646421be2c8a9e67f539f6ebd5a` | `0b031ce9b0ec8a5c7c7550ea5f7f1ac3b9875135` |
| `5185fed4e5c47b6f545e936927641e3b36e68ba4` | `6c49fb628ac07e247a3eec3433ddebe4bb4bc8f6` |

## Go source refresh

The initial import above used `2d2ec479`; the source advanced to
[`415c38f21449941c43c4729f23da8a7ebbae04ac`](https://github.com/action-state-group/capsule-emit-go/commit/415c38f21449941c43c4729f23da8a7ebbae04ac)
through [Go #11](https://github.com/action-state-group/capsule-emit-go/pull/11).
That commit is ported under `go/`, including extension-member construction,
validation, tests and documentation, preserving original authorship and sign-off.
Go source files now match that revision; destination-specific README/script
adaptations remain. The earlier DCO provenance mapping still describes the
initial history import, not a new source freeze at its older tip.
