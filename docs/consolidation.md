# Producer language-root consolidation

The execution scope is [#286](https://github.com/action-state-group/capsule-emit/issues/286).
Code moving to AAC, EvidenceBook or capsule-viewer is tracked separately in
[#290](https://github.com/action-state-group/capsule-emit/issues/290).

## Import inputs

| Source | Frozen commit | Tracked files | Destination |
|---|---|---|---|
| capsule-emit | `6c18d2608e383b64d631f5fffae1f640e42d1415` | Existing Python/Rust tree | Python project moves to `python/`; Rust workspace stays |
| capsule-emit-go | `2d2ec479397badb03a58f986cc6c0b9cc53802dc` | 117 | `go/` |
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

The configured incoming-tree preflight passed before import. Both import commits
retain the original source commit as a merge parent; no source history or tags
were rewritten. Python's 86 module files remain byte-identical to the frozen
emit base. Project metadata, dependencies, extras and console entrypoints are
unchanged. External source, editable, wheel and rebuilt-sdist consumers verify
sealing, Capsule verification, protobuf imports and CLI availability.

Local Go checks include exact pinned AAC corpus, real MySQL 8.4, race tests and
90.8% coverage against the existing 90% floor. TypeScript has 240 passing tests,
including real MySQL 8.4, and a packed consumer proving all six exports plus
core operation without optional storage drivers. The producer is Node-only;
its `node:crypto` import is not a browser compatibility claim.

DCO remains a merge gate. Five imported historical TS commits lack sign-off;
this branch preserves those original commits and does not weaken the DCO job
or add sign-offs on behalf of their authors. Their disposition requires a
maintainer decision before merge. New consolidation commits are signed off.
