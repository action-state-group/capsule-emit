# Test vectors: where they come from

Copied verbatim from their source; not edited here. `SHA256SUMS` lists every
file (`shasum -a 256 -c SHA256SUMS` from this directory checks them).

| Directory | Source | Pin | License |
| --- | --- | --- | --- |
| `aac-capsule/` (the whole Class-1 capsule set, with its `README.md` and `SHA256SUMS`) | [agent-action-capsule](https://github.com/action-state-group/agent-action-capsule) `vectors/capsule/` | commit `1e60ec15e019ce4e0ec1251269ac24801e8c73f5` on `main` (agent-action-capsule#147, which adds the nine `neg-field-not-string-*` cases to tag `v0.6.0`'s set); each file matches that commit's `vectors/capsule/SHA256SUMS` | BSD-3-Clause: `aac-capsule/LICENSE`, copied verbatim, travels with the copies |
| `aac-provenance-mode/` (the provenance-mode set, check 9) | [agent-action-capsule](https://github.com/action-state-group/agent-action-capsule) `provenance-mode-vectors/` | tag `v0.6.0` (commit `13a1f4534c31369e927db73af6298e8b9ffdc36a`), unchanged at commit `1e60ec15e019ce4e0ec1251269ac24801e8c73f5`; each file matches that tag's `provenance-mode-vectors/SHA256SUMS` | BSD-3-Clause: `aac-provenance-mode/LICENSE`, the repository's license copied verbatim |

To update: copy the new set unchanged from the source tag or commit, update the pin
here, and regenerate `SHA256SUMS`.
