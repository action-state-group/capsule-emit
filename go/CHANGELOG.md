# Changelog

All notable changes to the `github.com/action-state-group/capsule-emit/go`
module are documented here. The Python package in this repository keeps its own
changelog at the repository root. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this module uses
[Semantic Versioning](https://semver.org/) once it reaches 1.0. Releases are
`go/vX.Y.Z` tags in this repository (see RELEASING.md).

## 0.3.0 — 2026-10-14

The first release from this repository, and the first under the module path
`github.com/action-state-group/capsule-emit/go`. Versions up to v0.2.0 were
published as `github.com/action-state-group/capsule-emit-go`, which is
archived. Numbering continues from v0.2.0 so one version line covers the code's
history; Go still treats the two paths as different modules.

### Changed — BREAKING: module path

- The module is `github.com/action-state-group/capsule-emit/go`. Update every
  import, including `artifact`, `artifact/jsonl`, `artifact/mysql` and
  `artifact/sqlite`, in one change: types from the old module are not
  assignable to the new one.

### Added

- `Input.Extensions` carries additional top-level Capsule members that a
  companion document defines (for example a settlement record's `settlement`
  member). Each `Extension` value is JSON and participates in `capsule_id`.
  `Build` refuses a base-profile member name, a name outside
  `^[a-z][a-z0-9_]*$`, a repeated name, an empty or `null` value, and any float
  or integer outside the interoperable range.

### Changed

- Requires `github.com/action-state-group/agent-action-capsule/go` v0.7.0 (was
  a pre-release pseudo-version) and tests against its conformance corpus at the
  same commit.
