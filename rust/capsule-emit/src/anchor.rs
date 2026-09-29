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

use serde::Deserialize;
use std::time::Duration;

pub const DEFAULT_ANCHOR_BASE: &str = "https://anchor.agentactioncapsule.org";

/// The default *semantic* witness URL a checkpoint's `WitnessRecord.ts_url`
/// records — same value `cll::witness::DEFAULT_TS_URL` uses. Matches
/// `cll.checkpoint.emit._PENDING_CNAME_TARGETS`: `witness.agentactioncapsule.org`
/// has no DNS record of its own yet, so a request to exactly this URL is
/// dispatched to [`DEFAULT_ANCHOR_BASE`] directly (same deployment, already
/// answers `/checkpoints`); any other, explicitly-chosen `ts_url` is never
/// rewritten. Remove this indirection once the alias domain is live.
pub const DEFAULT_WITNESS_URL: &str = "https://witness.agentactioncapsule.org";

/// Where an HTTP request registering `ts_url` should actually be sent —
/// `DEFAULT_ANCHOR_BASE` for the default semantic URL, `ts_url` itself
/// otherwise. See [`DEFAULT_WITNESS_URL`].
pub fn dispatch_base_for(ts_url: &str) -> &str {
    if ts_url == DEFAULT_WITNESS_URL {
        DEFAULT_ANCHOR_BASE
    } else {
        ts_url
    }
}

#[derive(Debug, thiserror::Error)]
pub enum AnchorError {
    #[error("anchor request failed: {0}")]
    Transport(String),
    #[error("anchor service returned HTTP {status}: {body}")]
    Status { status: u16, body: String },
    #[error("anchor response was not valid JSON: {0}")]
    Decode(String),
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
            agent: ureq::AgentBuilder::new()
                .timeout(Duration::from_secs(10))
                .build(),
        }
    }

    /// `POST /v1/digest {"capsule_id": <64-hex>}`. Idempotent on the server
    /// side: resubmitting the same `capsule_id` returns the original
    /// receipt, so callers may retry freely.
    pub fn post_digest(&self, capsule_id: &str) -> Result<AnchorReceipt, AnchorError> {
        let url = format!("{}/v1/digest", self.base_url.trim_end_matches('/'));
        match self
            .agent
            .post(&url)
            .send_json(ureq::json!({ "capsule_id": capsule_id }))
        {
            Ok(resp) => resp
                .into_json()
                .map_err(|e| AnchorError::Decode(e.to_string())),
            Err(ureq::Error::Status(status, resp)) => Err(AnchorError::Status {
                status,
                body: resp.into_string().unwrap_or_default(),
            }),
            Err(e) => Err(AnchorError::Transport(e.to_string())),
        }
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
            Ok(resp) => resp
                .into_json()
                .map(Some)
                .map_err(|e| AnchorError::Decode(e.to_string())),
            Err(ureq::Error::Status(404, _)) => Ok(None),
            Err(ureq::Error::Status(status, resp)) => Err(AnchorError::Status {
                status,
                body: resp.into_string().unwrap_or_default(),
            }),
            Err(e) => Err(AnchorError::Transport(e.to_string())),
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
        match self
            .agent
            .post(&url)
            .set("Content-Type", cll::checkpoint::CLL_CHECKPOINT_CONTENT_TYPE)
            .set("Accept", "application/json")
            .send_bytes(checkpoint_cose)
        {
            Ok(resp) => resp
                .into_json()
                .map_err(|e| AnchorError::Decode(e.to_string())),
            Err(ureq::Error::Status(status, resp)) => Err(AnchorError::Status {
                status,
                body: resp.into_string().unwrap_or_default(),
            }),
            Err(e) => Err(AnchorError::Transport(e.to_string())),
        }
    }

    /// `GET /anchor/authority-pubkey` -- the raw 32-byte Ed25519 authority
    /// public key (hex) + its `key_id`, for out-of-band pinning and for
    /// verifying receipts offline via `scitt_cose.verify_receipt`.
    pub fn authority_pubkey(&self) -> Result<AuthorityPubkey, AnchorError> {
        let url = format!(
            "{}/anchor/authority-pubkey",
            self.base_url.trim_end_matches('/')
        );
        match self.agent.get(&url).call() {
            Ok(resp) => resp
                .into_json()
                .map_err(|e| AnchorError::Decode(e.to_string())),
            Err(ureq::Error::Status(status, resp)) => Err(AnchorError::Status {
                status,
                body: resp.into_string().unwrap_or_default(),
            }),
            Err(e) => Err(AnchorError::Transport(e.to_string())),
        }
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

impl Default for AnchorClient {
    fn default() -> Self {
        Self::new(DEFAULT_ANCHOR_BASE)
    }
}
