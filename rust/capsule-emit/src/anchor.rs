//! Optional anchor client for a SCITT transparency service.
//!
//! - [`AnchorClient::post_digest`]: POST a `capsule_id` digest
//!   (`/v1/digest`) and check its durable status
//!   (`/v1/inclusion/{capsule_id}`), with the same request and response
//!   shapes as the Python reference's registration client.
//! - [`AnchorClient::post_checkpoint_cose`]: register a checkpoint
//!   (`POST /checkpoints`, COSE only, `Content-Type:
//!   application/cll-checkpoint+cbor`, response `{entry_hash, receipt_b64,
//!   leaf_index, tree_size}`), the checkpointed-local-log reference's wire
//!   contract. [`crate::checkpoint`] uses this route for witness
//!   registration.
//!
//! Deliberately **not** wired into `capsule::seal` or `ledger::append`: the
//! caller decides when and whether to anchor, and a failed anchor call never
//! invalidates an already-sealed, already-ledgered capsule (fail-open, like
//! the Python reference's witness client).
//!
//! There is no default service: every client is built with the URL its caller
//! chose (`AnchorClient::new`), and registration reaches each configured
//! witness at exactly its configured URL. One public witness is, for example,
//! `https://witness.agentactioncapsule.org`; it is named here as an example
//! only, and nothing in this crate sends to it unless a caller configures it.

use serde::Deserialize;
use std::time::Duration;

#[derive(Debug, thiserror::Error)]
pub enum AnchorError {
    #[error("anchor request failed: {0}")]
    Transport(String),
    #[error("anchor service returned HTTP {status}: {body}")]
    Status { status: u16, body: String },
    #[error("anchor response was not valid JSON: {0}")]
    Decode(String),
    #[error("anchor response is larger than {0} bytes")]
    TooLarge(u64),
}

/// The largest response body a client reads. A receipt, an inclusion proof
/// or an authority key is a few kilobytes; anything past this is refused.
pub const MAX_RESPONSE_BYTES: u64 = 1 << 20;

/// How much of an error response's body an [`AnchorError::Status`] keeps.
const ERROR_BODY_CHARS: usize = 4096;

/// A response body, read up to [`MAX_RESPONSE_BYTES`].
fn read_capped(resp: ureq::Response) -> Result<Vec<u8>, AnchorError> {
    use std::io::Read;
    let mut body = Vec::new();
    resp.into_reader()
        .take(MAX_RESPONSE_BYTES + 1)
        .read_to_end(&mut body)
        .map_err(|e| AnchorError::Transport(e.to_string()))?;
    if body.len() as u64 > MAX_RESPONSE_BYTES {
        return Err(AnchorError::TooLarge(MAX_RESPONSE_BYTES));
    }
    Ok(body)
}

fn status_error(status: u16, resp: ureq::Response) -> AnchorError {
    let body = match read_capped(resp) {
        Ok(bytes) => String::from_utf8_lossy(&bytes)
            .chars()
            .take(ERROR_BODY_CHARS)
            .collect(),
        Err(_) => String::new(),
    };
    AnchorError::Status { status, body }
}

/// The JSON a request returned. Redirects are not followed, so a 3xx is an
/// error like any other non-2xx status; every body is read up to
/// [`MAX_RESPONSE_BYTES`].
fn json_response<T: serde::de::DeserializeOwned>(
    result: Result<ureq::Response, ureq::Error>,
) -> Result<T, AnchorError> {
    match result {
        Ok(resp) if (200..300).contains(&resp.status()) => {
            let body = read_capped(resp)?;
            serde_json::from_slice(&body).map_err(|e| AnchorError::Decode(e.to_string()))
        }
        Ok(resp) => Err(status_error(resp.status(), resp)),
        Err(ureq::Error::Status(status, resp)) => Err(status_error(status, resp)),
        Err(e) => Err(AnchorError::Transport(e.to_string())),
    }
}

/// `RegisterStatementResponse` shape from `capsule-anchor`'s
/// `POST /v1/digest` and `POST /transparency/register-statement`.
#[derive(Debug, Clone, Deserialize)]
pub struct AnchorReceipt {
    pub receipt_b64: String,
    pub entry_hash: String,
    #[serde(default)]
    pub entry_hash_scheme: Option<String>,
    pub leaf_index: u64,
    pub tree_size: u64,
    #[serde(default)]
    pub checkpoint_witness: Option<serde_json::Value>,
}

/// `GET /v1/inclusion/{capsule_id}` response shape -- proves the digest was
/// actually logged, not just accepted by `/v1/digest` (registration is fire-
/// and-forget from the caller's view; inclusion is the durable-status check).
#[derive(Debug, Clone, Deserialize)]
pub struct InclusionProof {
    pub capsule_id: String,
    pub entry_hash: String,
    pub leaf_index: u64,
    pub tree_size: u64,
    pub leaf_hash: String,
    pub audit_path: Vec<String>,
    pub root_hash: String,
    pub receipt_b64: String,
}

pub struct AnchorClient {
    base_url: String,
    agent: ureq::Agent,
}

impl AnchorClient {
    pub fn new(base_url: impl Into<String>) -> Self {
        Self {
            base_url: base_url.into(),
            // A redirect is never followed: a request goes to the URL the
            // caller chose and nowhere else.
            agent: ureq::AgentBuilder::new()
                .timeout(Duration::from_secs(10))
                .redirects(0)
                .build(),
        }
    }

    /// The URL this client sends to, exactly as it was given.
    pub fn base_url(&self) -> &str {
        &self.base_url
    }

    /// `POST /v1/digest {"capsule_id": <64-hex>}`. Idempotent on the server
    /// side: resubmitting the same `capsule_id` returns the original
    /// receipt, so callers may retry freely.
    pub fn post_digest(&self, capsule_id: &str) -> Result<AnchorReceipt, AnchorError> {
        let url = format!("{}/v1/digest", self.base_url.trim_end_matches('/'));
        json_response(
            self.agent
                .post(&url)
                .send_json(ureq::json!({ "capsule_id": capsule_id })),
        )
    }

    /// `GET /v1/inclusion/{capsule_id}` -- `Ok(None)` on a 404 (not yet
    /// durable, or never submitted); never registers as a side effect,
    /// unlike `post_digest`.
    pub fn check_inclusion(&self, capsule_id: &str) -> Result<Option<InclusionProof>, AnchorError> {
        let url = format!(
            "{}/v1/inclusion/{}",
            self.base_url.trim_end_matches('/'),
            capsule_id
        );
        match self.agent.get(&url).call() {
            Err(ureq::Error::Status(404, _)) => Ok(None),
            result => json_response(result).map(Some),
        }
    }

    /// `POST /checkpoints {COSE_Sign1 bytes}` -- registers a checkpoint's
    /// COSE-wire statement (`cll::checkpoint::checkpoint_to_cose`'s output)
    /// with the Transparency Service. COSE-only: never a plain JSON
    /// `CheckpointRecord` body (a transparency service registers the signed
    /// statement, never an unsigned body) — never the
    /// `/v1/digest` route `post_digest` uses for a single capsule digest.
    pub fn post_checkpoint_cose(
        &self,
        checkpoint_cose: &[u8],
    ) -> Result<CheckpointWitnessResponse, AnchorError> {
        let url = format!("{}/checkpoints", self.base_url.trim_end_matches('/'));
        json_response(
            self.agent
                .post(&url)
                .set("Content-Type", cll::checkpoint::CLL_CHECKPOINT_CONTENT_TYPE)
                .set("Accept", "application/json")
                .send_bytes(checkpoint_cose),
        )
    }

    /// `GET /anchor/authority-pubkey` -- the raw 32-byte Ed25519 authority
    /// public key (hex) + its `key_id`, for out-of-band pinning and for
    /// verifying receipts offline via `scitt_cose.verify_receipt`.
    pub fn authority_pubkey(&self) -> Result<AuthorityPubkey, AnchorError> {
        let url = format!(
            "{}/anchor/authority-pubkey",
            self.base_url.trim_end_matches('/')
        );
        json_response(self.agent.get(&url).call())
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct AuthorityPubkey {
    pub pubkey_hex: String,
    pub key_id: String,
}

/// Response shape from `POST /checkpoints` -- matches
/// `cll.checkpoint.emit.register_checkpoint`'s parse of the same route
/// field-for-field (`ts_url` is never part of the response; the caller
/// already knows which URL it dispatched to).
#[derive(Debug, Clone, Deserialize)]
pub struct CheckpointWitnessResponse {
    pub entry_hash: String,
    pub receipt_b64: String,
    pub leaf_index: i64,
    pub tree_size: i64,
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader, Write};
    use std::net::TcpListener;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;

    /// A server that answers every request with `response` (raw HTTP) and
    /// counts the requests it got.
    fn serve(response: Vec<u8>) -> (String, Arc<AtomicUsize>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let hits = Arc::new(AtomicUsize::new(0));
        let counted = hits.clone();
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut stream) = stream else { continue };
                counted.fetch_add(1, Ordering::SeqCst);
                let mut reader = BufReader::new(stream.try_clone().unwrap());
                let mut length = 0usize;
                loop {
                    let mut line = String::new();
                    if reader.read_line(&mut line).unwrap_or(0) == 0 || line == "\r\n" {
                        break;
                    }
                    if let Some(v) = line.to_ascii_lowercase().strip_prefix("content-length:") {
                        length = v.trim().parse().unwrap_or(0);
                    }
                }
                let mut body = vec![0; length];
                let _ = std::io::Read::read_exact(&mut reader, &mut body);
                let _ = stream.write_all(&response);
            }
        });
        (url, hits)
    }

    fn json_ok(body: &[u8]) -> Vec<u8> {
        let mut r = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
            body.len()
        )
        .into_bytes();
        r.extend_from_slice(body);
        r
    }

    #[test]
    fn a_redirect_is_not_followed() {
        let (elsewhere, elsewhere_hits) = serve(json_ok(br#"{"pubkey_hex":"00","key_id":"k"}"#));
        let (url, hits) = serve(
            format!(
                "HTTP/1.1 302 Found\r\nLocation: {elsewhere}/anchor/authority-pubkey\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            .into_bytes(),
        );
        let client = AnchorClient::new(&url);
        match client.authority_pubkey() {
            Err(AnchorError::Status { status: 302, .. }) => {}
            other => panic!("expected the 302 as an error, got {other:?}"),
        }
        match client.post_checkpoint_cose(b"cose") {
            Err(AnchorError::Status { status: 302, .. }) => {}
            other => panic!("expected the 302 as an error, got {other:?}"),
        }
        assert_eq!(hits.load(Ordering::SeqCst), 2);
        assert_eq!(
            elsewhere_hits.load(Ordering::SeqCst),
            0,
            "the redirect target was contacted"
        );
    }

    #[test]
    fn a_response_body_is_capped() {
        // A JSON string padded past the cap: valid JSON, refused for its size.
        let mut big = br#"{"pubkey_hex":""#.to_vec();
        big.extend(std::iter::repeat_n(b'0', MAX_RESPONSE_BYTES as usize));
        big.extend_from_slice(br#"","key_id":"k"}"#);
        let (url, _) = serve(json_ok(&big));
        match AnchorClient::new(&url).authority_pubkey() {
            Err(AnchorError::TooLarge(cap)) => assert_eq!(cap, MAX_RESPONSE_BYTES),
            other => panic!("expected TooLarge, got {other:?}"),
        }
        // Just under the cap is read.
        let mut fits = br#"{"pubkey_hex":""#.to_vec();
        fits.extend(std::iter::repeat_n(b'0', 1000));
        fits.extend_from_slice(br#"","key_id":"k"}"#);
        let (url, _) = serve(json_ok(&fits));
        let key = AnchorClient::new(&url).authority_pubkey().unwrap();
        assert_eq!(key.pubkey_hex.len(), 1000);
        // An error body is capped too, and truncated in the error.
        let mut err = format!(
            "HTTP/1.1 500 Internal Server Error\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
            10_000
        )
        .into_bytes();
        err.extend(std::iter::repeat_n(b'x', 10_000));
        let (url, _) = serve(err);
        match AnchorClient::new(&url).authority_pubkey() {
            Err(AnchorError::Status { status: 500, body }) => {
                assert_eq!(body.len(), ERROR_BODY_CHARS)
            }
            other => panic!("expected a 500, got {other:?}"),
        }
    }
}
