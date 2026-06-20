# Security policy

## Reporting a vulnerability

Please report suspected vulnerabilities **privately**:

- **GitHub:** use *Security → Report a vulnerability* on this repository
  (GitHub private vulnerability reporting), or
- **Email:** security@actionstate.ai with `[capsule-emit security]` in the subject.

Please do not open a public issue for a suspected vulnerability. We aim to
acknowledge reports within 72 hours.

## Scope (highest-priority classes)

- **Content leakage on the anchor path.** The anchor client MUST submit a
  **digest only** — never operator/vendor/payload content. Any path that puts raw
  business content on the wire is the **highest-priority** issue.
- **Forgeable evidence.** A capsule that verifies but should not, a
  confirmed-effect binding that can be forged (a *dispatched* attempt presented as
  a *confirmed* effect), or a chain link that can be spoofed.
- **Digest / canonicalization issues.** A collision or canonicalization mismatch
  that lets two different payloads share a `capsule_id`.
- **Parser / resource issues** in the ledger or manifest readers (memory-safety,
  resource exhaustion).

## Out of scope

Verification logic lives in the separate
[`agent-action-capsule`](https://github.com/action-state-group/agent-action-capsule)
package — report verifier bypasses there. Ambiguities or honest-but-misleading
prose about standards status are not security issues — raise those as public
issues or on the SCITT mailing list.

## Supported versions

The latest released version receives fixes.
