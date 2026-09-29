//! The request digest (§4.2 item 1): SHA-256 over the request bytes **as
//! received**, never over a re-serialized form. A requester that records the
//! bytes it sent (deterministically encoded, §3.8) can then match a refusal
//! to its own record of asking.

use sha2::{Digest, Sha256};

/// SHA-256 of `request_bytes`, as 64 lowercase hex characters (A2).
pub fn request_digest(request_bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(request_bytes))
}

/// Whether a refusal's `request_digest` identifies the request whose bytes
/// were `sent_bytes`. Only the digest of the exact bytes does.
pub fn identifies_request(refusal_request_digest: &str, sent_bytes: &[u8]) -> bool {
    refusal_request_digest == request_digest(sent_bytes)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn digest_is_over_the_exact_bytes() {
        let compact = br#"{"a":1}"#;
        let spaced = br#"{ "a": 1 }"#;
        assert_eq!(request_digest(compact).len(), 64);
        assert_ne!(request_digest(compact), request_digest(spaced));
        assert!(identifies_request(&request_digest(spaced), spaced));
        assert!(!identifies_request(&request_digest(compact), spaced));
    }
}
