# CLAUDE.md

Guidance for coding agents working on this repository. `AGENTS.md` is
different: it tells an agent how to *use* capsule-emit, and is part of the
product documentation. `go/CLAUDE.md` adds the Go module's own rules.

## Commands

```bash
# Python (repository root; Python 3.9 compatible)
pip install -e "./python[dev]"
(cd python && python -m pytest -q)
python -m ruff check --config python/pyproject.toml python .github examples commitment-conformance-vectors test-vectors docs skills rust
python -m pytest -q .github/test_release.py          # release helper
python -m unittest discover -s .github/tests          # consolidation and guard tests
python .github/messaging_guard.py .                   # forbidden repositioning phrases

# TypeScript (ts/). The tests read agent-action-capsule's corpus: check that
# repository out at the commit in ts/test/aac-pin.ts and point AAC_ROOT at it
# (default ../../agent-action-capsule), as ts.yml does.
npm ci && npm run format:check && npm run lint && npm run typecheck && npm test && npm run build

# Go (go/): see go/CLAUDE.md
go vet ./... && go test ./...

# Rust (rust/, a workspace holding the capsule-emit crate)
cargo fmt --all --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked
```

CI also runs hostname, leak and neutrality lints over public material, and
checks pinned vector sets against their `SHA256SUMS`.

## Layout

| Directory | Package |
|---|---|
| `python/` | PyPI `capsule-emit` |
| `ts/` | npm `@action-state-group/capsule-emit` |
| `go/` | `github.com/action-state-group/capsule-emit/go` |
| `rust/` | crate `capsule-emit` |
| `docs/`, `examples/`, `skills/`, `flows/` | guides, runnable examples and the agent skill |
| `test-vectors/`, `commitment-conformance-vectors/` | pinned vector sets the tests read |

The implementations follow the Agent Action Capsule specification in
`action-state-group/agent-action-capsule`; use its canonicalization and
verification rather than reimplementing them.

## Rules

- Commits use conventional commit messages and a DCO sign-off
  (`git commit -s`). Do not add AI attribution trailers.
- New behavior needs tests in the same change, including error paths.
- Public material passes `messaging_guard.py`: describe what the package does,
  not how it repositions other products.

## Releases

Follow [RELEASING.md](RELEASING.md). In short: run **Release · Prepare**, merge
its pull request, and the Release · Tag workflow tags and publishes. Never
create release tags by hand, except `go/v*`, which an admin pushes. Do not
rename `publish-python.yml`, `publish-ts.yml` or `publish-rust.yml`: the
registries' trusted publishers name them, so publishing breaks until the
registry is updated first. `.github/release.py` is an identical copy shared
with the other action-state-group repositories; change it in all of them
together.
