//! Sealing an Agent Action Capsule (format 4, draft-mih-scitt-agent-action-capsule-05).
//!
//! [`seal`] builds a capsule from a [`CapsuleInput`] and computes its
//! `capsule_id` over the canonical form (§5.1). [`seal_local_record`] seals a
//! record kind that has no served exchange (a citation, an observation of an
//! event) from caller-supplied `compute_attestation` blocks.
//!
//! Anything a deployment adds beyond the core members travels as
//! **`compute_attestation` extension members** ([`CapsuleInput::compute_attestation_extensions`]):
//! this crate commits them into `capsule_id` like every other member and
//! never interprets them. Their order is kept, so a producer that builds the
//! same extensions gets the same bytes.
//!
//! Both seal paths draw nothing the caller cannot pin except
//! [`seal_local_record`]'s store nonce and minute timestamp, which the record
//! kind requires to be fresh.

use crate::jcs::compute_capsule_id;
use serde_json::{json, Map, Value};

pub const SPEC_VERSION: &str = "draft-mih-scitt-agent-action-capsule-05";
pub const FORMAT_VERSION: &str = "4";
pub const CANONICALIZATION_ID: &str = crate::jcs::CANONICALIZATION_JCS;

/// Draws a fresh [`STORE_NONCE_FIELD`] value: 64 lowercase hex from the OS
/// CSPRNG.
pub use evidencebook::padding::fresh_store_nonce;
/// `model_attestation.compute_attestation.store_nonce`: a fresh 256-bit value
/// the store draws from the OS CSPRNG for one sealed record and never reuses
/// (Evidence Layer -00 §12.1: every record carries, inside the bytes its
/// commitment-substrate entry is derived from, at least 128 bits the store
/// generated for it alone). It is committed into `capsule_id`, so the
/// siblings of an inclusion proof cannot be confirmed by guessing a record's
/// content and recomputing its digest.
pub use evidencebook::padding::STORE_NONCE_FIELD;

/// The `compute_attestation` members this crate writes itself. An extension
/// may not use one of these keys.
pub const CORE_COMPUTE_ATTESTATION_MEMBERS: &[&str] = &[
    "agent_input_digest",
    "agent_output_digest",
    "tool_calls_digest",
    "reasoning_digest",
    "host_binding",
    "runtime",
    "attestation_refs",
    STORE_NONCE_FIELD,
];

/// A capsule's `chain` block: the previous capsule in this producer's own
/// stream.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ChainLink {
    pub parent_capsule_id: String,
    pub relation: String,
}

impl ChainLink {
    pub fn to_value(&self) -> Value {
        json!({
            "parent_capsule_id": self.parent_capsule_id,
            "relation": self.relation,
        })
    }
}

/// An OPTIONAL reverse-direction composition binding: the host's own digest
/// for this exchange, under the host's own construction. It is a second,
/// independent claim beside `agent_input_digest`, never a re-derivation of
/// it. The registries of accepted `construction` and `purpose` labels belong
/// to the deployment; this crate carries the three strings as given.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HostBinding {
    pub digest: String,
    pub construction: String,
    pub purpose: String,
}

impl HostBinding {
    pub fn to_value(&self) -> Value {
        json!({
            "digest": self.digest,
            "construction": self.construction,
            "purpose": self.purpose,
        })
    }
}

/// Everything [`seal`] commits to for one action.
#[derive(Clone, Debug)]
pub struct CapsuleInput {
    pub action_id: String,
    pub action_type: String,
    pub operator: String,
    pub developer: String,
    pub timestamp: String,
    pub domain: Option<String>,
    pub provenance: Option<String>,
    pub model_id: String,
    pub provider: String,
    /// JSON-DIGEST of the input the agent acted on.
    pub agent_input_digest: String,
    /// JSON-DIGEST of the output the agent produced. `None` omits the member
    /// when the producer never saw the output body; never a digest of
    /// something else that would read as one.
    pub agent_output_digest: Option<String>,
    /// OPTIONAL labeled sub-digest over the tool calls the model emitted.
    /// `None` leaves it absent (never a digest over an empty list), so a
    /// reader never misreads the record as asserting "zero tool calls".
    pub tool_calls_digest: Option<String>,
    /// OPTIONAL labeled sub-digest over the model's reasoning output. `None`
    /// leaves it absent.
    pub reasoning_digest: Option<String>,
    /// OPTIONAL host binding; absent (never null) when `None`.
    pub host_binding: Option<HostBinding>,
    /// `compute_attestation.runtime`: `{name, runtime_digest?, ...}`. Never a
    /// fabricated digest for an unmeasured binary.
    pub runtime: Value,
    /// Further `compute_attestation` members, in order, inserted after
    /// `attestation_refs` and before the store nonce. Keys must not collide
    /// with [`CORE_COMPUTE_ATTESTATION_MEMBERS`].
    pub compute_attestation_extensions: Map<String, Value>,
    pub effect_status: String,
    pub effect_type: String,
    /// `None` omits the member. Present only as a 64-hex JSON-DIGEST of the
    /// request body; never a sentinel string.
    pub effect_request_digest: Option<String>,
    /// `None` omits the member. `confirmed` REQUIRES it (a 64-hex digest of
    /// the effect's actual output); `dispatched`/`planned` REQUIRE it absent
    /// (AAC-05 §5.2). [`seal`] refuses any other combination.
    pub effect_response_digest: Option<String>,
    pub effect_attestation: String,
    pub disposition_decision: String,
    pub disposition_approver: String,
    pub disposition_human_disposed: bool,
    pub disposition_verdict_class: String,
    /// `None` for the first capsule in a stream: no `chain` key at all, not a
    /// null value.
    pub chain: Option<ChainLink>,
    /// The record's [`STORE_NONCE_FIELD`]: 64 lowercase hex from
    /// [`fresh_store_nonce`], drawn for this record alone. Supplied by the
    /// caller only so tests can pin it; [`seal`] refuses anything that is not
    /// 64 lowercase hex.
    pub store_nonce: String,
}

fn is_hex64(v: &str) -> bool {
    v.len() == 64
        && v.chars()
            .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
}

/// Why [`seal`] or [`seal_local_record`] refused to build a capsule.
#[derive(Debug, thiserror::Error)]
pub enum SealError {
    #[error(transparent)]
    Jcs(#[from] crate::jcs::JcsError),
    /// The effect's status and digests break AAC-05 §5.2's status/digest
    /// table, e.g. `confirmed` with no digest of the actual output.
    #[error("effect record violates AAC-05 §5.2: {0}")]
    EffectInvariant(&'static str),
    /// The store nonce is missing or malformed. A record without one is
    /// guessable from its content (Evidence Layer -00 §12.1).
    #[error("store_nonce must be 64 lowercase hex (256 bits from the store's CSPRNG)")]
    StoreNonce,
    /// An extension member would overwrite a member this crate writes.
    #[error("compute_attestation extension {0:?} collides with a core member")]
    ReservedExtensionKey(String),
    /// A profile's top-level member would replace a member this crate writes.
    #[error("top-level member {0:?} would replace a capsule member")]
    ReservedMember(String),
}

/// AAC-05 §5.2's confirmed-effect invariant and status/digest table.
fn check_effect(input: &CapsuleInput) -> Result<(), SealError> {
    let req = input.effect_request_digest.as_deref();
    let resp = input.effect_response_digest.as_deref();
    if req.is_some_and(|d| !is_hex64(d)) || resp.is_some_and(|d| !is_hex64(d)) {
        return Err(SealError::EffectInvariant(
            "request_digest/response_digest must be a 64-hex JSON-DIGEST when present",
        ));
    }
    match input.effect_status.as_str() {
        "confirmed" if resp.is_none() => Err(SealError::EffectInvariant(
            "confirmed requires a response_digest over the observed output",
        )),
        "planned" if req.is_some() || resp.is_some() => Err(SealError::EffectInvariant(
            "planned requires request_digest and response_digest absent",
        )),
        "dispatched" if resp.is_some() => Err(SealError::EffectInvariant(
            "dispatched requires response_digest absent",
        )),
        _ => Ok(()),
    }
}

/// The reference implementation's `derive_effect_mode` for the statuses this
/// crate emits.
fn derive_effect_mode(status: &str, response_digest: Option<&str>) -> &'static str {
    let is_hex64 = response_digest.is_some_and(is_hex64);
    match status {
        "planned" => "not_applicable",
        "confirmed" if is_hex64 => "confirmed",
        _ => "dispatched_unconfirmed",
    }
}

fn check_extensions(extensions: &Map<String, Value>) -> Result<(), SealError> {
    match extensions
        .keys()
        .find(|k| CORE_COMPUTE_ATTESTATION_MEMBERS.contains(&k.as_str()))
    {
        Some(key) => Err(SealError::ReservedExtensionKey(key.clone())),
        None => Ok(()),
    }
}

/// Build and seal a capsule: returns the capsule object with `capsule_id`
/// computed over its canonical form (§5.1).
pub fn seal(input: &CapsuleInput) -> Result<Value, SealError> {
    finish_seal(seal_body(input)?)
}

/// Everything [`seal`] commits to, before `capsule_id` is computed. Public so
/// a record kind that adds top-level blocks to an exchange record commits
/// them under the same id computation (add them, then call [`finish_seal`]),
/// never by editing a sealed capsule.
pub fn seal_body(input: &CapsuleInput) -> Result<Map<String, Value>, SealError> {
    check_effect(input)?;
    if !is_hex64(&input.store_nonce) {
        return Err(SealError::StoreNonce);
    }
    check_extensions(&input.compute_attestation_extensions)?;

    let mut body = Map::new();
    body.insert("spec_version".into(), json!(SPEC_VERSION));
    body.insert("format_version".into(), json!(FORMAT_VERSION));
    body.insert("canonicalization_id".into(), json!(CANONICALIZATION_ID));
    body.insert("action_id".into(), json!(input.action_id));
    body.insert("action_type".into(), json!(input.action_type));
    body.insert("operator".into(), json!(input.operator));
    body.insert("developer".into(), json!(input.developer));
    body.insert("timestamp".into(), json!(input.timestamp));
    if let Some(d) = &input.domain {
        body.insert("domain".into(), json!(d));
    }
    if let Some(p) = &input.provenance {
        body.insert("provenance".into(), json!(p));
    }

    // The mandatory input/output digests, then the OPTIONAL labeled
    // sub-digests, each inserted only when present (absent, never a
    // fabricated digest). Each is committed under its own label, so a holder
    // can later disclose one of them without the others.
    let mut compute_attestation = Map::new();
    compute_attestation.insert("agent_input_digest".into(), json!(input.agent_input_digest));
    if let Some(output) = &input.agent_output_digest {
        compute_attestation.insert("agent_output_digest".into(), json!(output));
    }
    if let Some(tcd) = &input.tool_calls_digest {
        compute_attestation.insert("tool_calls_digest".into(), json!(tcd));
    }
    if let Some(rd) = &input.reasoning_digest {
        compute_attestation.insert("reasoning_digest".into(), json!(rd));
    }
    if let Some(hb) = &input.host_binding {
        compute_attestation.insert("host_binding".into(), hb.to_value());
    }
    compute_attestation.insert("runtime".into(), input.runtime.clone());
    compute_attestation.insert("attestation_refs".into(), json!([]));
    for (key, value) in &input.compute_attestation_extensions {
        compute_attestation.insert(key.clone(), value.clone());
    }
    compute_attestation.insert(STORE_NONCE_FIELD.into(), json!(input.store_nonce));
    body.insert(
        "model_attestation".into(),
        json!({
            "model_id": input.model_id,
            "provider": input.provider,
            "compute_attestation": Value::Object(compute_attestation),
        }),
    );

    let mut effect = json!({
        "status": input.effect_status,
        "type": input.effect_type,
        "effect_attestation": input.effect_attestation,
    });
    for (key, digest) in [
        ("request_digest", &input.effect_request_digest),
        ("response_digest", &input.effect_response_digest),
    ] {
        if let Some(digest) = digest {
            effect[key] = json!(digest);
        }
    }
    body.insert("effect".into(), effect);
    let effect_mode = derive_effect_mode(
        &input.effect_status,
        input.effect_response_digest.as_deref(),
    );
    // `ledger_mode` is "chained" iff a chain block is present -- not a
    // statement about whether the producer also writes a local ledger.
    let ledger_mode = if input.chain.is_some() {
        "chained"
    } else {
        "standalone"
    };
    body.insert(
        "assurance".into(),
        json!({
            "attestation_mode": "self_attested",
            "effect_mode": effect_mode,
            "ledger_mode": ledger_mode,
        }),
    );
    body.insert(
        "disposition".into(),
        json!({
            "decision": input.disposition_decision,
            "approver": input.disposition_approver,
            "human_disposed": input.disposition_human_disposed,
            "verdict_class": input.disposition_verdict_class,
        }),
    );
    if let Some(chain) = &input.chain {
        body.insert("chain".into(), chain.to_value());
    }
    Ok(body)
}

/// Compute `capsule_id` over a finished body and lay the sealed capsule out
/// as [`seal`] does: version fields, then the id, then the body.
pub fn finish_seal(body: Map<String, Value>) -> Result<Value, SealError> {
    let capsule_id = compute_capsule_id(&Value::Object(body.clone()))?;
    let mut sealed = Map::new();
    sealed.insert("spec_version".into(), json!(SPEC_VERSION));
    sealed.insert("format_version".into(), json!(FORMAT_VERSION));
    sealed.insert("capsule_id".into(), json!(capsule_id));
    for (k, v) in body {
        sealed.entry(k).or_insert(v);
    }
    Ok(Value::Object(sealed))
}

/// Why [`attach_producer_envelope`] refused.
#[derive(Debug, thiserror::Error)]
pub enum EnvelopeError {
    #[error("capsule must be a JSON object")]
    NotAnObject,
    #[error("capsule must carry a string capsule_id (seal it first)")]
    MissingCapsuleId,
    #[error("capsule_id must be lowercase hex")]
    CapsuleIdNotHex,
}

/// Attach the **inline producer-signature envelope** to a sealed capsule, so
/// it verifies in isolation, the way the reference implementation's sealed
/// capsules do:
///   - `capsule["signature"]`: the hex COSE_Sign1 producer envelope over the
///     raw 32-byte `capsule_id` digest ([`crate::cose::sign_producer_envelope`]);
///   - `capsule["key_id"]`: the raw 32-byte Ed25519 public key, hex.
///
/// Both are added after `capsule_id` is computed and are excluded from every
/// `capsule_id` preimage, so attaching the envelope never changes the id.
pub fn attach_producer_envelope(
    capsule: &mut Value,
    signing_key: &ed25519_dalek::SigningKey,
) -> Result<(), EnvelopeError> {
    let obj = capsule.as_object_mut().ok_or(EnvelopeError::NotAnObject)?;
    let capsule_id = obj
        .get("capsule_id")
        .and_then(Value::as_str)
        .ok_or(EnvelopeError::MissingCapsuleId)?
        .to_string();
    let digest = hex::decode(&capsule_id).map_err(|_| EnvelopeError::CapsuleIdNotHex)?;
    let envelope = crate::cose::sign_producer_envelope(&digest, signing_key);
    let key_id = hex::encode(signing_key.verifying_key().to_bytes());
    obj.insert("signature".into(), json!(hex::encode(&envelope)));
    obj.insert("key_id".into(), json!(key_id));
    Ok(())
}

/// The bytes a detached COSE_Sign1 statement signs and carries as its
/// payload: the sealed capsule as compact JSON. Verifiers re-parse the
/// payload and recompute `capsule_id` from the parsed object, so this is a
/// transport encoding, not a canonical form.
pub fn payload_bytes(capsule: &Value) -> Vec<u8> {
    serde_json::to_vec(capsule).expect("capsule must be JSON-serializable")
}

// ---------------------------------------------------------------------------
// References between records.
// ---------------------------------------------------------------------------

/// `references[].type` for "another Agent Action Capsule".
pub const REFERENCE_TYPE_CAPSULE: &str = "capsule";
/// The digest algorithm a `references[]` entry pins. A capsule's
/// `capsule_id` is its SHA-256 JSON-DIGEST, so citing `digest = <capsule_id>`
/// under SHA-256 is a self-consistent typed digest reference.
pub const REFERENCE_DIGEST_ALG: &str = "SHA-256";
/// `chain.relation` for the next record in a producer's own stream. A
/// citation of another producer's record is a `references[]` entry, never a
/// `chain.relation` value (AAC-05).
pub const CHAIN_RELATION_FOLLOWS: &str = "follows";
/// `references[].citation_purpose` for a record citing the other party's
/// record of the same exchange (AAC-05 citation-purpose registry).
pub const CITATION_PURPOSE_COUNTERPARTY_HALF: &str = "counterparty_half";
/// `references[].citation_purpose` for a record citing the other party's
/// inclusion proof and covering checkpoint (AAC-05 citation-purpose registry).
pub const CITATION_PURPOSE_COUNTERPARTY_INCLUSION: &str = "counterparty_inclusion";

/// A typed digest reference to another capsule.
pub fn capsule_reference(capsule_id: &str, citation_purpose: &str) -> Value {
    json!({
        "type": REFERENCE_TYPE_CAPSULE,
        "digest_alg": REFERENCE_DIGEST_ALG,
        "digest": capsule_id,
        "citation_purpose": citation_purpose,
    })
}

// ---------------------------------------------------------------------------
// Local records: a record kind with no served exchange.
// ---------------------------------------------------------------------------

/// The header fields of a [`seal_local_record`] record. They are the
/// producer's own identity and the model the record is about.
#[derive(Clone, Debug)]
pub struct LocalRecordHeader<'a> {
    pub operator: &'a str,
    pub developer: &'a str,
    pub provider: &'a str,
    pub model_id: &'a str,
}

/// Seal a record that observes or cites something rather than serving an
/// exchange: `action_type` `fyi`, `effect_mode` `not_applicable`, the
/// caller's `compute_attestation` blocks (in order) plus a fresh store nonce,
/// the minute timestamp, optional `references[]` and `chain`. The inline
/// producer envelope is attached with `signing_key`; the caller writes the
/// detached statement.
pub fn seal_local_record(
    header: &LocalRecordHeader<'_>,
    action_id: String,
    blocks: Map<String, Value>,
    references: Option<Value>,
    chain: Option<ChainLink>,
    signing_key: &ed25519_dalek::SigningKey,
) -> Result<Value, SealError> {
    seal_local_record_with_members(
        header,
        action_id,
        blocks,
        Map::new(),
        references,
        chain,
        signing_key,
    )
}

/// [`seal_local_record`], plus top-level members a profile defines (for
/// example a settlement record's `settlement`). They are added before
/// `capsule_id` is computed, so they are committed like every other member.
/// A name that would replace a member this function writes is refused.
pub fn seal_local_record_with_members(
    header: &LocalRecordHeader<'_>,
    action_id: String,
    blocks: Map<String, Value>,
    members: Map<String, Value>,
    references: Option<Value>,
    chain: Option<ChainLink>,
    signing_key: &ed25519_dalek::SigningKey,
) -> Result<Value, SealError> {
    if blocks.contains_key(STORE_NONCE_FIELD) {
        return Err(SealError::ReservedExtensionKey(
            STORE_NONCE_FIELD.to_string(),
        ));
    }
    let mut body = Map::new();
    body.insert("spec_version".into(), json!(SPEC_VERSION));
    body.insert("format_version".into(), json!(FORMAT_VERSION));
    body.insert("canonicalization_id".into(), json!(CANONICALIZATION_ID));
    body.insert("action_id".into(), json!(action_id));
    body.insert("action_type".into(), json!("fyi"));
    body.insert("operator".into(), json!(header.operator));
    body.insert("developer".into(), json!(header.developer));
    body.insert(
        "timestamp".into(),
        json!(crate::timestamp::utc_now_minute()),
    );
    body.insert("domain".into(), json!("action"));
    body.insert("provenance".into(), json!("collector"));

    let mut compute_attestation = blocks;
    compute_attestation.insert(STORE_NONCE_FIELD.into(), json!(fresh_store_nonce()));
    body.insert(
        "model_attestation".into(),
        json!({
            "model_id": header.model_id,
            "provider": header.provider,
            "compute_attestation": Value::Object(compute_attestation),
        }),
    );
    body.insert(
        "assurance".into(),
        json!({
            "attestation_mode": "self_attested",
            "effect_mode": "not_applicable",
            "ledger_mode": if chain.is_some() { "chained" } else { "standalone" },
        }),
    );
    body.insert(
        "disposition".into(),
        json!({
            "decision": "accept",
            "approver": "policy",
            "human_disposed": false,
            "verdict_class": "executed",
        }),
    );
    if let Some(chain) = &chain {
        body.insert("chain".into(), chain.to_value());
    }
    if let Some(references) = references {
        body.insert("references".into(), references);
    }
    for (name, value) in members {
        if body.contains_key(&name)
            || matches!(name.as_str(), "capsule_id" | "signature" | "key_id")
        {
            return Err(SealError::ReservedMember(name));
        }
        body.insert(name, value);
    }

    let capsule_id = compute_capsule_id(&Value::Object(body.clone()))?;
    let mut sealed = Map::new();
    sealed.insert("capsule_id".into(), json!(capsule_id));
    for (k, v) in body {
        sealed.entry(k).or_insert(v);
    }
    let mut capsule = Value::Object(sealed);
    attach_producer_envelope(&mut capsule, signing_key)
        .expect("a sealed record always carries a hex capsule_id");
    Ok(capsule)
}

#[cfg(test)]
mod tests {
    use super::*;
    use ed25519_dalek::SigningKey;

    pub(crate) fn input() -> CapsuleInput {
        CapsuleInput {
            action_id: "example/action/1".into(),
            action_type: "decide".into(),
            operator: "op".into(),
            developer: "dev".into(),
            timestamp: "2026-09-28T00:00:00Z".into(),
            domain: Some("action".into()),
            provenance: Some("collector".into()),
            model_id: "model".into(),
            provider: "provider".into(),
            agent_input_digest: "a".repeat(64),
            agent_output_digest: Some("b".repeat(64)),
            tool_calls_digest: None,
            reasoning_digest: None,
            host_binding: None,
            runtime: json!({"name": "runtime"}),
            compute_attestation_extensions: Map::new(),
            effect_status: "confirmed".into(),
            effect_type: "inference_completion".into(),
            effect_request_digest: Some("c".repeat(64)),
            effect_response_digest: Some("d".repeat(64)),
            effect_attestation: "gate_executed".into(),
            disposition_decision: "accept".into(),
            disposition_approver: "policy".into(),
            disposition_human_disposed: false,
            disposition_verdict_class: "executed".into(),
            chain: None,
            store_nonce: "e".repeat(64),
        }
    }

    #[test]
    fn capsule_id_recomputes_from_the_sealed_body() {
        let capsule = seal(&input()).unwrap();
        let id = capsule["capsule_id"].as_str().unwrap();
        assert_eq!(id.len(), 64);
        assert_eq!(compute_capsule_id(&capsule).unwrap(), id);
    }

    #[test]
    fn optional_members_are_absent_not_null() {
        let mut i = input();
        i.agent_output_digest = None;
        i.effect_status = "dispatched".into();
        i.effect_response_digest = None;
        let capsule = seal(&i).unwrap();
        let ca = &capsule["model_attestation"]["compute_attestation"];
        for key in [
            "agent_output_digest",
            "tool_calls_digest",
            "reasoning_digest",
            "host_binding",
        ] {
            assert!(ca.get(key).is_none(), "{key} must be absent");
        }
        assert!(capsule.get("chain").is_none());
        assert!(capsule["effect"].get("response_digest").is_none());
        assert_eq!(capsule["assurance"]["ledger_mode"], json!("standalone"));
        assert_eq!(
            capsule["assurance"]["effect_mode"],
            json!("dispatched_unconfirmed")
        );
    }

    #[test]
    fn extensions_are_committed_in_order_before_the_store_nonce() {
        let mut i = input();
        i.compute_attestation_extensions
            .insert("x-first".into(), json!({"k": 1}));
        i.compute_attestation_extensions
            .insert("x-second".into(), json!("v"));
        let capsule = seal(&i).unwrap();
        let keys: Vec<&str> = capsule["model_attestation"]["compute_attestation"]
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect();
        assert_eq!(
            keys,
            [
                "agent_input_digest",
                "agent_output_digest",
                "runtime",
                "attestation_refs",
                "x-first",
                "x-second",
                STORE_NONCE_FIELD
            ]
        );
        // And they are committed: changing one changes the id.
        let before = capsule["capsule_id"].clone();
        i.compute_attestation_extensions
            .insert("x-second".into(), json!("w"));
        assert_ne!(seal(&i).unwrap()["capsule_id"], before);
    }

    #[test]
    fn an_extension_may_not_overwrite_a_core_member() {
        for key in CORE_COMPUTE_ATTESTATION_MEMBERS {
            let mut i = input();
            i.compute_attestation_extensions
                .insert((*key).into(), json!("forged"));
            assert!(
                matches!(seal(&i), Err(SealError::ReservedExtensionKey(k)) if k == *key),
                "{key} must be refused"
            );
        }
    }

    #[test]
    fn effect_table_is_enforced() {
        let mut confirmed_without_output = input();
        confirmed_without_output.effect_response_digest = None;
        assert!(matches!(
            seal(&confirmed_without_output),
            Err(SealError::EffectInvariant(_))
        ));

        let mut planned_with_digest = input();
        planned_with_digest.effect_status = "planned".into();
        planned_with_digest.effect_response_digest = None;
        assert!(matches!(
            seal(&planned_with_digest),
            Err(SealError::EffectInvariant(_))
        ));

        let mut dispatched_with_output = input();
        dispatched_with_output.effect_status = "dispatched".into();
        assert!(matches!(
            seal(&dispatched_with_output),
            Err(SealError::EffectInvariant(_))
        ));

        let mut sentinel = input();
        sentinel.effect_request_digest = Some("n/a".into());
        assert!(matches!(
            seal(&sentinel),
            Err(SealError::EffectInvariant(_))
        ));
    }

    #[test]
    fn store_nonce_must_be_64_lowercase_hex() {
        for bad in [
            "",
            "E".repeat(64).as_str(),
            "e".repeat(63).as_str(),
            "g".repeat(64).as_str(),
        ] {
            let mut i = input();
            i.store_nonce = bad.to_string();
            assert!(matches!(seal(&i), Err(SealError::StoreNonce)), "{bad:?}");
        }
        assert_eq!(fresh_store_nonce().len(), 64);
        assert_ne!(fresh_store_nonce(), fresh_store_nonce());
    }

    #[test]
    fn chain_block_sets_the_ledger_mode() {
        let mut i = input();
        i.chain = Some(ChainLink {
            parent_capsule_id: "f".repeat(64),
            relation: CHAIN_RELATION_FOLLOWS.into(),
        });
        let capsule = seal(&i).unwrap();
        assert_eq!(capsule["chain"]["parent_capsule_id"], json!("f".repeat(64)));
        assert_eq!(capsule["assurance"]["ledger_mode"], json!("chained"));
    }

    #[test]
    fn host_binding_is_carried_as_given() {
        let mut i = input();
        i.host_binding = Some(HostBinding {
            digest: "1".repeat(64),
            construction: "example/construction/v1".into(),
            purpose: "example-purpose".into(),
        });
        let capsule = seal(&i).unwrap();
        assert_eq!(
            capsule["model_attestation"]["compute_attestation"]["host_binding"],
            json!({"digest": "1".repeat(64), "construction": "example/construction/v1", "purpose": "example-purpose"})
        );
    }

    #[test]
    fn producer_envelope_never_changes_the_capsule_id() {
        let key = SigningKey::from_bytes(&[7u8; 32]);
        let mut capsule = seal(&input()).unwrap();
        let id = capsule["capsule_id"].clone();
        attach_producer_envelope(&mut capsule, &key).unwrap();
        assert_eq!(capsule["capsule_id"], id);
        assert_eq!(compute_capsule_id(&capsule).unwrap(), id.as_str().unwrap());
        assert_eq!(
            capsule["key_id"],
            json!(hex::encode(key.verifying_key().to_bytes()))
        );
        assert!(capsule["signature"].as_str().is_some_and(|s| !s.is_empty()));

        let mut not_sealed = json!({"capsule_id": "not-hex"});
        assert!(matches!(
            attach_producer_envelope(&mut not_sealed, &key),
            Err(EnvelopeError::CapsuleIdNotHex)
        ));
    }

    #[test]
    fn local_record_carries_its_blocks_references_and_a_fresh_nonce() {
        let key = SigningKey::from_bytes(&[9u8; 32]);
        let header = LocalRecordHeader {
            operator: "op",
            developer: "dev",
            provider: "provider",
            model_id: "model",
        };
        let mut blocks = Map::new();
        blocks.insert("observed".into(), json!({"event": "e1"}));
        let references = json!([capsule_reference(
            &"2".repeat(64),
            CITATION_PURPOSE_COUNTERPARTY_HALF
        )]);
        let a = seal_local_record(
            &header,
            "example/local/1".into(),
            blocks.clone(),
            Some(references.clone()),
            None,
            &key,
        )
        .unwrap();
        let b = seal_local_record(
            &header,
            "example/local/1".into(),
            blocks.clone(),
            Some(references),
            None,
            &key,
        )
        .unwrap();
        assert_eq!(a["action_type"], json!("fyi"));
        assert_eq!(a["assurance"]["effect_mode"], json!("not_applicable"));
        assert_eq!(
            a["model_attestation"]["compute_attestation"]["observed"],
            json!({"event": "e1"})
        );
        assert_eq!(
            a["references"][0]["citation_purpose"],
            json!("counterparty_half")
        );
        // Fresh store nonce per record: the same content gives different ids.
        assert_ne!(a["capsule_id"], b["capsule_id"]);
        assert_eq!(
            compute_capsule_id(&a).unwrap(),
            a["capsule_id"].as_str().unwrap()
        );

        blocks.insert(STORE_NONCE_FIELD.into(), json!("0".repeat(64)));
        assert!(matches!(
            seal_local_record(&header, "x".into(), blocks, None, None, &key),
            Err(SealError::ReservedExtensionKey(_))
        ));
    }
}
