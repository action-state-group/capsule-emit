//! Padding records (Evidence Layer -00 §12.1, "What a Checkpoint Reveals").
//!
//! A checkpoint states the size of this store's whole committed history, so
//! two checkpoints handed to one party (a pushed bundle's covering
//! checkpoint, a witness registration) tell it how many records this node
//! committed between them -- across every counterparty, not only that party.
//! Before a checkpoint is cut, the store appends padding records until its
//! leaf count (`mmr_leaf_count`, never `mmr_size`) falls on a bucket
//! boundary, so two checkpoints reveal the count between them only to within
//! the bucket.
//!
//! This module owns the record shape, its builder and its checks; the caller
//! decides when to pad (see `checkpoint::PaddingSink` and
//! `ledger::Ledger::pad_to_bucket`). The Evidence Layer draft names the nonce
//! member `store_nonce`, and the agent-action-capsule padding vectors pin the
//! shape.
//!
//! **Where padding lives: in the store -- not in CLL.**
//! Padding is a record, not a log mechanism: the log folds a padding record's
//! id as an ordinary leaf, and nothing in `checkpointed-local-log` changes.
//! The Evidence Layer -00 draft (AAC #130) reserves `padding` in its own text:
//! no CLL change and no new registry.
//!
//! **Shape.** One ledger line:
//! `{"capsule_id", "record_type": "padding", "epistemic_type":
//! "producer_claim", "store_nonce"}` plus the local-only producer envelope
//! (`signature`/`key_id`, outside the id preimage). Its only committed content
//! is the fresh store nonce. It carries no payload commitments, no `chain`,
//! no `references`, no subject or principal -- and it is never the target of a
//! link: the ledger does NOT advance its chain head over a padding record, so
//! the next real record still chains to the previous real record.
//!
//! **Why `store_nonce` is top-level here but nested in a capsule.** §12.1 only
//! requires the random value to sit inside the bytes the record's commitment
//! entry is derived from; it does not name a location. A capsule has a fixed
//! AAC envelope whose one free-form extension point is
//! `model_attestation.compute_attestation`, so the nonce rides there. A
//! padding record is not a capsule and has no such envelope: its whole body
//! is the committed content, so the nonce sits at the top level. Both are
//! inside the `capsule_id` preimage, which is what the requirement is about.
//!
//! **Readers.** A verifier treats a padding record as an ordinary leaf for
//! inclusion, consistency and checkpoints. Everything that counts, lists or
//! summarises records skips it ([`is_padding`]), and it is never returned as
//! responsive to a record, correlation or exchange query.
//!
//! PROVISIONAL: `record_type: "padding"` is reserved by the Evidence Layer -00
//! draft text (AAC #130); it is a provisional constant pending the
//! evidence-layer privacy considerations text.

use crate::jcs::{compute_capsule_id, JcsError};
use serde_json::{json, Map, Value};

/// The reserved `record_type` of a padding record.
pub const RECORD_TYPE_PADDING: &str = "padding";

/// The content type of a padding record's signed statement.
pub const PADDING_CONTENT_TYPE: &str = "application/json";

/// A padding record's `epistemic_type`.
pub const PADDING_EPISTEMIC_TYPE: &str = "producer_claim";

/// The member holding a record's store nonce: on a padding record, at the
/// top level; on a capsule, in `model_attestation.compute_attestation`.
pub const STORE_NONCE_FIELD: &str = "store_nonce";

/// The members a padding record may carry: its id, its type pair, the
/// nonce, and the local-only producer envelope.
const PADDING_MEMBERS: &[&str] = &[
    "capsule_id",
    "record_type",
    "epistemic_type",
    STORE_NONCE_FIELD,
    "signature",
    "key_id",
];

/// A fresh store nonce: 32 bytes from the OS CSPRNG, as 64 lowercase hex,
/// drawn for one record and never reused.
pub fn fresh_store_nonce() -> String {
    use rand_core::RngCore;
    let mut bytes = [0u8; 32];
    rand_core::OsRng.fill_bytes(&mut bytes);
    hex::encode(bytes)
}

/// Whether `record` is a padding record (its `record_type` is `padding`).
pub fn is_padding(record: &Value) -> bool {
    record.get("record_type").and_then(Value::as_str) == Some(RECORD_TYPE_PADDING)
}

fn is_nonce(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

/// Check a padding record's shape: an object with only the members above,
/// the reserved type pair, and a 64-lowercase-hex `store_nonce`. Its
/// `capsule_id` is checked where every line's is, not here.
pub fn check_padding_shape(record: &Value) -> Result<(), String> {
    let obj = record
        .as_object()
        .ok_or_else(|| "padding record is not a JSON object".to_string())?;
    if let Some(extra) = obj.keys().find(|k| !PADDING_MEMBERS.contains(&k.as_str())) {
        return Err(format!(
            "padding record carries {extra:?}; its only content is the store nonce"
        ));
    }
    if obj.get("record_type").and_then(Value::as_str) != Some(RECORD_TYPE_PADDING) {
        return Err("padding record's record_type is not \"padding\"".to_string());
    }
    if obj.get("epistemic_type").and_then(Value::as_str) != Some(PADDING_EPISTEMIC_TYPE) {
        return Err(format!(
            "padding record's epistemic_type is not {PADDING_EPISTEMIC_TYPE:?}"
        ));
    }
    if !obj
        .get(STORE_NONCE_FIELD)
        .and_then(Value::as_str)
        .is_some_and(is_nonce)
    {
        return Err("padding record's store_nonce is not 64 lowercase hex".to_string());
    }
    Ok(())
}

/// A new padding record with a fresh store nonce, its `capsule_id` set. The
/// producer envelope is the caller's to attach.
pub fn build_padding_record() -> Result<Value, JcsError> {
    padding_record_with_nonce(&fresh_store_nonce())
}

/// The padding record for `store_nonce`, its `capsule_id` set. For checking
/// against conformance vectors and pinned output: a store pads with
/// [`build_padding_record`], since a nonce must never be reused.
pub fn padding_record_with_nonce(store_nonce: &str) -> Result<Value, JcsError> {
    let mut body = Map::new();
    body.insert("record_type".into(), json!(RECORD_TYPE_PADDING));
    body.insert("epistemic_type".into(), json!(PADDING_EPISTEMIC_TYPE));
    body.insert(STORE_NONCE_FIELD.into(), json!(store_nonce));
    let capsule_id = compute_capsule_id(&Value::Object(body.clone()))?;
    let mut record = Map::new();
    record.insert("capsule_id".into(), json!(capsule_id));
    record.extend(body);
    Ok(Value::Object(record))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn padding_record_is_well_formed_and_unique() {
        let a = build_padding_record().unwrap();
        let b = build_padding_record().unwrap();
        assert!(is_padding(&a));
        check_padding_shape(&a).unwrap();
        assert_ne!(a["capsule_id"], b["capsule_id"], "fresh nonce per record");
        assert_eq!(
            compute_capsule_id(&a).unwrap(),
            a["capsule_id"].as_str().unwrap()
        );
        let mut members: Vec<&str> = a.as_object().unwrap().keys().map(String::as_str).collect();
        members.sort_unstable();
        assert_eq!(
            members,
            [
                "capsule_id",
                "epistemic_type",
                "record_type",
                STORE_NONCE_FIELD
            ]
        );
    }

    #[test]
    fn the_id_is_the_digest_of_the_three_content_members() {
        let record = padding_record_with_nonce(&"ab".repeat(32)).unwrap();
        let body = json!({
            "record_type": "padding",
            "epistemic_type": "producer_claim",
            "store_nonce": "ab".repeat(32),
        });
        assert_eq!(
            record["capsule_id"].as_str().unwrap(),
            crate::jcs::json_digest(&body).unwrap()
        );
    }

    #[test]
    fn the_shape_check_refuses_anything_but_the_nonce() {
        let base = padding_record_with_nonce(&"0".repeat(64)).unwrap();
        check_padding_shape(&base).unwrap();
        let mut with_envelope = base.clone();
        with_envelope["signature"] = json!("s");
        with_envelope["key_id"] = json!("k");
        check_padding_shape(&with_envelope).unwrap();

        let refused = |edit: &dyn Fn(&mut Value)| {
            let mut rec = base.clone();
            edit(&mut rec);
            assert!(check_padding_shape(&rec).is_err(), "{rec}");
        };
        refused(&|r| {
            r["chain"] = json!({"parent_capsule_id": "a".repeat(64), "relation": "follows"})
        });
        refused(&|r| r["payload_commitments"] = json!([]));
        refused(&|r| r["subject_ref"] = json!("x"));
        refused(&|r| r[STORE_NONCE_FIELD] = json!("short"));
        refused(&|r| r[STORE_NONCE_FIELD] = json!("AB".repeat(32)));
        refused(&|r| {
            r.as_object_mut().unwrap().remove(STORE_NONCE_FIELD);
        });
        refused(&|r| r["epistemic_type"] = json!("observation"));
        refused(&|r| r["record_type"] = json!("close"));
        assert!(check_padding_shape(&json!([1])).is_err());
    }

    #[test]
    fn ordinary_records_are_not_padding() {
        assert!(!is_padding(&json!({"capsule_id": "a".repeat(64)})));
        assert!(!is_padding(&json!({"record_type": "close"})));
    }

    #[test]
    fn store_nonces_are_64_lowercase_hex_and_fresh() {
        let a = fresh_store_nonce();
        assert!(is_nonce(&a));
        assert_ne!(a, fresh_store_nonce());
    }
}
