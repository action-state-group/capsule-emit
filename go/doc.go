// Package emit builds format-4 Agent Action Capsules and independent
// Producer Envelopes from values observed at an external-effect boundary.
// [DigestJSON] produces the request and response digests stored in [Effect].
// [CanonicalizeForDigest] first converts decoded JSON numbers that DigestJSON
// rejects (floats) into their RFC 8785 decimal strings.
//
// The package does not execute actions, generate business operation IDs,
// retry calls, persist Capsules, authorize signers, or maintain a ledger.
package emit
