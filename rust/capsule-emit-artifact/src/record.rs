//! The storage-driver-free record model: admission ([`prepare`]),
//! authentication ([`verify`]), the [`storage_checksum`] and the JSONL wire
//! codec, byte-compatible with the Go, TypeScript and Python stores.

use std::collections::{BTreeMap, HashSet};

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use serde::de::{self, DeserializeSeed, MapAccess, SeqAccess, Visitor};
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::error::{ArtifactError, Result};

/// The payload commitment (`agent_input_digest` on the wire).
pub const PAYLOAD_DIGEST: &str = "model_attestation.compute_attestation.agent_input_digest";
/// The agent output commitment.
pub const AGENT_OUTPUT_DIGEST: &str = "model_attestation.compute_attestation.agent_output_digest";
/// The effect request preimage commitment.
pub const EFFECT_REQUEST_DIGEST: &str = "effect.request_digest";
/// The effect response preimage commitment.
pub const EFFECT_RESPONSE_DIGEST: &str = "effect.response_digest";
const DIGEST_FIELDS: [&str; 4] = [
    PAYLOAD_DIGEST,
    AGENT_OUTPUT_DIGEST,
    EFFECT_REQUEST_DIGEST,
    EFFECT_RESPONSE_DIGEST,
];

const MAX_ARTIFACTS: usize = 64;
const MAX_ENVELOPE: usize = 65_535;
const MAX_TOTAL: usize = 8 * 1024 * 1024;

/// Availability, not authenticity.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RetentionState {
    Present,
    Purged,
    NeverRetained,
}

impl RetentionState {
    /// The wire and SQL spelling.
    pub fn as_str(self) -> &'static str {
        match self {
            RetentionState::Present => "present",
            RetentionState::Purged => "purged",
            RetentionState::NeverRetained => "never_retained",
        }
    }

    /// Parses the wire and SQL spelling.
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "present" => Some(RetentionState::Present),
            "purged" => Some(RetentionState::Purged),
            "never_retained" => Some(RetentionState::NeverRetained),
            _ => None,
        }
    }
}

/// An exact original. An empty `binding` means NOT authenticated by the
/// Capsule. `content` is `None` when absent; `Some(vec![])` is present and
/// empty. `content_sha256` protects exact-byte storage integrity, not
/// authorship. Field order is the JSONL wire order.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Artifact {
    pub name: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub binding: String,
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        serialize_with = "ser_opt_b64",
        deserialize_with = "de_opt_b64"
    )]
    pub content: Option<Vec<u8>>,
    pub state: RetentionState,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub content_sha256: String,
}

/// A Capsule with one Producer Envelope and its declared originals.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Record {
    pub capsule_id: String,
    #[serde(serialize_with = "ser_b64", deserialize_with = "de_b64")]
    pub capsule: Vec<u8>,
    #[serde(serialize_with = "ser_b64", deserialize_with = "de_b64")]
    pub producer_envelope: Vec<u8>,
    pub artifacts: Vec<Artifact>,
}

/// Separates a verified preimage from an unbound attachment.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ArtifactVerification {
    pub state: RetentionState,
    pub bound: bool,
    pub verified: bool,
}

fn ser_b64<S: Serializer>(bytes: &[u8], s: S) -> std::result::Result<S::Ok, S::Error> {
    s.serialize_str(&STANDARD.encode(bytes))
}

fn ser_opt_b64<S: Serializer>(
    bytes: &Option<Vec<u8>>,
    s: S,
) -> std::result::Result<S::Ok, S::Error> {
    match bytes {
        Some(b) => ser_b64(b, s),
        None => s.serialize_none(),
    }
}

fn de_b64<'de, D: Deserializer<'de>>(d: D) -> std::result::Result<Vec<u8>, D::Error> {
    let text = String::deserialize(d)?;
    STANDARD.decode(text).map_err(de::Error::custom)
}

fn de_opt_b64<'de, D: Deserializer<'de>>(d: D) -> std::result::Result<Option<Vec<u8>>, D::Error> {
    de_b64(d).map(Some)
}

/// Reports whether `value` is a valid namespace or artifact name.
pub fn valid_name(value: &str) -> bool {
    let bytes = value.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= 64
        && bytes[0].is_ascii_alphanumeric()
        && bytes[1..]
            .iter()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'))
}

/// Reports whether `value` is 64 lowercase hex characters.
pub fn valid_id(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

pub(crate) fn sha256_hex(data: &[u8]) -> String {
    hex::encode(Sha256::digest(data))
}

/// Rejects duplicate object keys at any depth; serde_json and the AAC decoder
/// both keep the last duplicate, while the Go and TypeScript decoders reject.
struct NoDuplicates;

impl<'de> DeserializeSeed<'de> for NoDuplicates {
    type Value = ();
    fn deserialize<D: Deserializer<'de>>(self, d: D) -> std::result::Result<(), D::Error> {
        d.deserialize_any(self)
    }
}

impl<'de> Visitor<'de> for NoDuplicates {
    type Value = ();
    fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        f.write_str("JSON")
    }
    fn visit_bool<E>(self, _: bool) -> std::result::Result<(), E> {
        Ok(())
    }
    fn visit_i64<E>(self, _: i64) -> std::result::Result<(), E> {
        Ok(())
    }
    fn visit_u64<E>(self, _: u64) -> std::result::Result<(), E> {
        Ok(())
    }
    fn visit_f64<E>(self, _: f64) -> std::result::Result<(), E> {
        Ok(())
    }
    fn visit_str<E>(self, _: &str) -> std::result::Result<(), E> {
        Ok(())
    }
    fn visit_unit<E>(self) -> std::result::Result<(), E> {
        Ok(())
    }
    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> std::result::Result<(), A::Error> {
        while seq.next_element_seed(NoDuplicates)?.is_some() {}
        Ok(())
    }
    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> std::result::Result<(), A::Error> {
        let mut seen = HashSet::new();
        while let Some(key) = map.next_key::<String>()? {
            if !seen.insert(key) {
                return Err(de::Error::custom("duplicate object key"));
            }
            map.next_value_seed(NoDuplicates)?;
        }
        Ok(())
    }
}

/// Decodes strict JSON: UTF-8, no duplicate keys, no trailing data, as the Go
/// and TypeScript decoders require. Numbers keep their source text.
pub(crate) fn decode_strict_json(data: &[u8]) -> std::result::Result<Value, String> {
    let mut deserializer = serde_json::Deserializer::from_slice(data);
    NoDuplicates
        .deserialize(&mut deserializer)
        .map_err(|e| e.to_string())?;
    agent_action_capsule::verify::decode_capsule_json(data).map_err(|e| e.to_string())
}

fn digest_at(capsule: &Value, binding: &str) -> Result<String> {
    if !DIGEST_FIELDS.contains(&binding) {
        return Err(ArtifactError::Invalid(format!(
            "unsupported digest field {binding}"
        )));
    }
    let mut value = capsule;
    for key in binding.split('.') {
        value = value
            .as_object()
            .and_then(|o| o.get(key))
            .ok_or_else(|| ArtifactError::Invalid(format!("missing {binding}")))?;
    }
    match value.as_str() {
        Some(digest) if valid_id(digest) => Ok(digest.to_string()),
        _ => Err(ArtifactError::Invalid(format!("malformed {binding}"))),
    }
}

/// Authenticates the Capsule, authorizes its signer against the caller's
/// trusted keys and verifies every available bound original. It does not
/// verify CLL inclusion, external references or business correctness.
pub fn verify(
    record: &Record,
    trusted: &[[u8; 32]],
) -> Result<BTreeMap<String, ArtifactVerification>> {
    if !valid_id(&record.capsule_id) {
        return Err(ArtifactError::Invalid("capsule id".into()));
    }
    let capsule = decode_strict_json(&record.capsule)
        .map_err(|e| ArtifactError::Invalid(format!("capsule verification: {e}")))?;
    let profile_ok = capsule.get("format_version").and_then(Value::as_str) == Some("4")
        && capsule.get("canonicalization_id").and_then(Value::as_str) == Some("jcs");
    if !profile_ok {
        return Err(ArtifactError::Invalid(
            "capsule verification: only AAC format 4 with canonicalization_id \"jcs\"".into(),
        ));
    }
    let class1 = agent_action_capsule::verify::verify(&capsule, None);
    if !class1.ok || class1.capsule_id.as_deref() != Some(record.capsule_id.as_str()) {
        let codes: Vec<&str> = class1
            .findings
            .iter()
            .filter(|f| f.severity == "error")
            .map(|f| f.code.as_str())
            .collect();
        return Err(ArtifactError::Invalid(format!(
            "capsule verification: {}",
            codes.join(",")
        )));
    }
    let envelope =
        agent_action_capsule::envelope::verify(&record.capsule_id, &record.producer_envelope);
    if !envelope.ok {
        let codes: Vec<&str> = envelope.findings.iter().map(|f| f.code.as_str()).collect();
        return Err(ArtifactError::Invalid(format!(
            "envelope: {}",
            codes.join(",")
        )));
    }
    if !trusted.iter().any(|k| k[..] == envelope.public_key[..]) {
        return Err(ArtifactError::UntrustedSigner);
    }
    let mut results = BTreeMap::new();
    for a in &record.artifacts {
        if !valid_name(&a.name) {
            return Err(ArtifactError::Invalid("artifact name".into()));
        }
        if results.contains_key(&a.name) {
            return Err(ArtifactError::Invalid(format!(
                "duplicate artifact {}",
                a.name
            )));
        }
        let committed = if a.binding.is_empty() {
            String::new()
        } else {
            digest_at(&capsule, &a.binding)?
        };
        let mut verified = false;
        match a.state {
            RetentionState::Present => {
                let content = a.content.as_ref().ok_or_else(|| {
                    ArtifactError::Invalid(format!("missing content for {}", a.name))
                })?;
                if !a.content_sha256.is_empty() && a.content_sha256 != sha256_hex(content) {
                    return Err(ArtifactError::Corrupt(a.name.clone()));
                }
                if !a.binding.is_empty() {
                    let digest = decode_strict_json(content)
                        .ok()
                        .and_then(|v| agent_action_capsule::canonical::json_digest(&v).ok());
                    if digest.as_deref() != Some(committed.as_str()) {
                        return Err(ArtifactError::DigestMismatch(format!(
                            "{} ({})",
                            a.name, a.binding
                        )));
                    }
                    verified = true;
                }
            }
            RetentionState::Purged => {
                if a.content.is_some() || !valid_id(&a.content_sha256) {
                    return Err(ArtifactError::Invalid("invalid purge tombstone".into()));
                }
            }
            RetentionState::NeverRetained => {
                if a.content.is_some() || !a.content_sha256.is_empty() {
                    return Err(ArtifactError::Invalid("never-retained content".into()));
                }
            }
        }
        results.insert(
            a.name.clone(),
            ArtifactVerification {
                state: a.state,
                bound: !a.binding.is_empty(),
                verified,
            },
        );
    }
    Ok(results)
}

#[derive(Serialize)]
struct ChecksumArtifact<'a> {
    name: &'a str,
    #[serde(skip_serializing_if = "str::is_empty")]
    binding: &'a str,
    state: RetentionState,
    #[serde(skip_serializing_if = "str::is_empty")]
    content_sha256: &'a str,
}

#[derive(Serialize)]
struct ChecksumRecord<'a> {
    capsule_id: &'a str,
    #[serde(serialize_with = "ser_b64")]
    capsule: &'a [u8],
    #[serde(serialize_with = "ser_b64")]
    producer_envelope: &'a [u8],
    artifacts: Vec<ChecksumArtifact<'a>>,
}

/// The contract's storage checksum: commits the exact Capsule, envelope and
/// complete artifact inventory with each original's SHA-256, unchanged by
/// purge. It is NOT a producer authentication claim.
pub fn storage_checksum(record: &Record) -> String {
    let mut artifacts: Vec<ChecksumArtifact> = record
        .artifacts
        .iter()
        .map(|a| ChecksumArtifact {
            name: &a.name,
            binding: &a.binding,
            state: match a.state {
                RetentionState::Purged => RetentionState::Present,
                other => other,
            },
            content_sha256: &a.content_sha256,
        })
        .collect();
    artifacts.sort_by(|x, y| x.name.as_bytes().cmp(y.name.as_bytes()));
    let encoded = serde_json::to_vec(&ChecksumRecord {
        capsule_id: &record.capsule_id,
        capsule: &record.capsule,
        producer_envelope: &record.producer_envelope,
        artifacts,
    })
    .expect("checksum preimage serializes");
    sha256_hex(&encoded)
}

/// Copies the caller's record, fills exact-byte checksums and enforces the v1
/// admission limits. It does not authenticate a producer; stores also call
/// [`verify`] with their trusted keys before persisting.
pub fn prepare(record: &Record) -> Result<Record> {
    let mut prepared = record.clone();
    if prepared.artifacts.len() > MAX_ARTIFACTS || prepared.producer_envelope.len() > MAX_ENVELOPE {
        return Err(ArtifactError::Invalid("record limits".into()));
    }
    let mut size = prepared.capsule.len() + prepared.producer_envelope.len();
    for a in &mut prepared.artifacts {
        if a.state == RetentionState::Purged {
            return Err(ArtifactError::Purged);
        }
        size += a.content.as_ref().map_or(0, Vec::len);
        if a.state == RetentionState::Present {
            let digest = sha256_hex(a.content.as_deref().unwrap_or_default());
            if !a.content_sha256.is_empty() && a.content_sha256 != digest {
                return Err(ArtifactError::Corrupt("content checksum".into()));
            }
            a.content_sha256 = digest;
        }
    }
    if size > MAX_TOTAL {
        return Err(ArtifactError::Invalid("record exceeds 8 MiB".into()));
    }
    Ok(prepared)
}

/// `record` with every present original replaced by a tombstone that keeps
/// its SHA-256.
pub fn purged(record: &Record) -> Record {
    let mut result = record.clone();
    for a in &mut result.artifacts {
        if a.state == RetentionState::Present {
            a.content = None;
            a.state = RetentionState::Purged;
        }
    }
    result
}

pub(crate) fn has_purged(record: &Record) -> bool {
    record
        .artifacts
        .iter()
        .any(|a| a.state == RetentionState::Purged)
}

/// Validates a constructor's namespace and trusted keys.
pub(crate) fn check_config<K: AsRef<[u8]>>(
    namespace: &str,
    trusted: impl IntoIterator<Item = K>,
) -> Result<Vec<[u8; 32]>> {
    if !valid_name(namespace) {
        return Err(ArtifactError::Invalid("store configuration".into()));
    }
    let keys = trusted
        .into_iter()
        .map(|k| {
            <[u8; 32]>::try_from(k.as_ref())
                .map_err(|_| ArtifactError::Invalid("public key size".into()))
        })
        .collect::<Result<Vec<_>>>()?;
    if keys.is_empty() {
        return Err(ArtifactError::Invalid("store configuration".into()));
    }
    Ok(keys)
}

/// The record's one-line JSONL wire form (no trailing newline), byte-identical
/// to the Go, TypeScript and Python stores.
pub fn encode_record(record: &Record) -> String {
    serde_json::to_string(record).expect("record serializes")
}

/// Parses a JSONL wire line; any failure is [`ArtifactError::Corrupt`].
pub fn decode_record(line: &[u8]) -> Result<Record> {
    serde_json::from_slice(line).map_err(|e| ArtifactError::Corrupt(format!("parse failed: {e}")))
}
