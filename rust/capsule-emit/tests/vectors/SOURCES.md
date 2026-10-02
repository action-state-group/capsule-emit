# Test vectors: where they come from

Copied verbatim from their source; not edited here. `SHA256SUMS` lists every
file (`shasum -a 256 -c SHA256SUMS` from this directory checks them).

| Directory | Source | Pin | License |
| --- | --- | --- | --- |
| `aac-capsule/` (the whole Class-1 capsule set, with its `README.md` and `SHA256SUMS`) | [agent-action-capsule](https://github.com/action-state-group/agent-action-capsule) `vectors/capsule/` | commit `6470239bfd9681ee22e0a3dc9b28fc5240434349` on `main` (agent-action-capsule#150; with #147, #148 and #149 it adds the twelve `neg-field-not-string-*` and six `neg-retention-*` cases to tag `v0.6.0`'s set); each file matches that commit's `vectors/capsule/SHA256SUMS` | BSD-3-Clause: `aac-capsule/LICENSE`, copied verbatim, travels with the copies |
| `aac-provenance-mode/` (the provenance-mode set, check 9) | [agent-action-capsule](https://github.com/action-state-group/agent-action-capsule) `provenance-mode-vectors/` | commit `3ce5d54d3f98f848757adf4be30fc118d8f6cdd7` on `main` (agent-action-capsule#148, which adds the three `neg-field-not-string-provenance-*` cases to tag `v0.6.0`'s set), unchanged at commit `6470239bfd9681ee22e0a3dc9b28fc5240434349`; each file matches that commit's `provenance-mode-vectors/SHA256SUMS` | BSD-3-Clause: `aac-provenance-mode/LICENSE`, the repository's license copied verbatim |
| `cll-checkpoint/` (`vectors.json`, `cose-vectors.json`) | [checkpointed-local-log](https://github.com/action-state-group/checkpointed-local-log) `checkpoint-conformance-vectors/` | commit `04339d9ae622014b5c1651961a795d9977e4bba1` on `main` (checkpointed-local-log#34, the 0.2.1 deterministic-claims fix) | BSD-3-Clause: `cll-checkpoint/LICENSE`, the repository's license copied verbatim |

To update: copy the new set unchanged from the source tag or commit, update the pin
here, and regenerate `SHA256SUMS`.
