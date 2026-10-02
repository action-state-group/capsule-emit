//! Settlement records: the payer and the payee each record the same payment
//! ("Two-Party Settlement Records for Agent Payments",
//! draft-mih-agent-settlement-records-00,
//! <https://datatracker.ietf.org/doc/draft-mih-agent-settlement-records/>).
//!
//! A leg record is an ordinary local record with a top-level `settlement`
//! member. Each party seals only what its own system observed:
//!
//! - `terms`: what the payment is for and how much (sealed by the party that
//!   set the terms);
//! - `payer_observed`: `amount` sent toward the payee and `routing_fee` paid
//!   on top, as the payer's wallet reported them;
//! - `payee_observed`: `received` (credited) and `receive_fee` (deducted), as
//!   the payee's wallet reported them;
//! - `delivered`: `delivery.direction` (`sent` by the payee, `received` by the
//!   payer) and the `content_digest` of the delivered octets.
//!
//! Every leg but the terms names the terms leg's `capsule_id` in `terms_ref`.
//! This module builds and validates legs ([`build_leg`],
//! [`structure_failures`]) and seals them ([`seal_leg`]). Deriving the
//! settlement states from both parties' legs is the verifier's job (the
//! Python `capsule_emit.settlement.verify_settlements`).

use serde_json::{json, Map, Value};

use crate::capsule::{seal_local_record_with_members, ChainLink, LocalRecordHeader, SealError};

/// The top-level member a leg record carries.
pub const SETTLEMENT_MEMBER: &str = "settlement";
/// The settlement member's `version` for draft-mih-agent-settlement-records-00.
pub const SETTLEMENT_VERSION: &str = "0";

pub const LEGS: &[&str] = &["terms", "payer_observed", "payee_observed", "delivered"];
pub const ROLES: &[&str] = &["payer", "payee"];
pub const STATUSES: &[&str] = &["pending", "settled", "failed", "reversed"];

/// `references[].type` for a citation of another leg, per the draft.
pub const REFERENCE_TYPE_AGENT_ACTION_CAPSULE: &str = "agent-action-capsule";
/// `references[].citation_purpose` for the other party's leg.
pub const CITATION_PURPOSE_COUNTERPARTY_HALF: &str = "counterparty_half";

/// The initial payment reference types and their qualifier members.
pub const PAYMENT_REF_TYPES: &[(&str, &[&str])] = &[
    ("x402.transaction", &["network"]),
    ("ln.payment_hash", &[]),
    ("bolt12.invoice_payment_hash", &[]),
    ("ap2.transaction_id", &[]),
    ("ap2.payment_id", &[]),
    ("ap2.network_confirmation_id", &[]),
    ("acp.order_id", &[]),
    ("ucp.order_id", &[]),
    ("mpp.reference", &["method"]),
    ("iso20022.uetr", &[]),
    ("iso20022.end_to_end_id", &["debtor_agent"]),
    ("open_payments.incoming_payment", &[]),
];

/// The initial wrapped object types.
pub const WRAPPED_TYPES: &[&str] = &[
    "x402.offer",
    "x402.receipt",
    "x402.payment-payload",
    "x402.settle-response",
    "ap2.checkout-mandate",
    "ap2.payment-mandate",
    "ap2.checkout-receipt",
    "ap2.payment-receipt",
    "bolt12.invoice",
    "bolt12.payer-proof",
    "mpp.payment-receipt",
    "iso20022.message",
    "delivery.proof",
];

/// Required and optional `settlement` members per leg.
pub fn members(leg: &str) -> Option<(&'static [&'static str], &'static [&'static str])> {
    match leg {
        "terms" => Some((
            &["version", "leg", "sealer_role", "amount"],
            &["payment_ref", "deliverable", "valid_until", "wrapped"],
        )),
        "payer_observed" => Some((
            &[
                "version",
                "leg",
                "sealer_role",
                "terms_ref",
                "amount",
                "payment_ref",
                "status",
                "observed_at",
            ],
            &["routing_fee", "wrapped"],
        )),
        "payee_observed" => Some((
            &[
                "version",
                "leg",
                "sealer_role",
                "terms_ref",
                "received",
                "payment_ref",
                "status",
                "observed_at",
            ],
            &["receive_fee", "wrapped"],
        )),
        "delivered" => Some((
            &[
                "version",
                "leg",
                "sealer_role",
                "terms_ref",
                "observed_at",
                "delivery",
            ],
            &["wrapped"],
        )),
        _ => None,
    }
}

const AMOUNT_MEMBERS: &[&str] = &["amount", "routing_fee", "received", "receive_fee"];
const DELIVERY_MEMBERS: &[&str] = &[
    "direction",
    "content_digest",
    "carrier",
    "tracking_digest",
    "status",
    "shipped_at",
    "delivered_at",
    "address_digest",
];

/// Why a leg was refused: the draft's failure codes.
#[derive(Debug, thiserror::Error)]
#[error("settlement leg refused: {codes:?}")]
pub struct SettlementError {
    pub codes: Vec<&'static str>,
}

/// Why sealing a leg failed.
#[derive(Debug, thiserror::Error)]
pub enum SealLegError {
    #[error(transparent)]
    Leg(#[from] SettlementError),
    #[error(transparent)]
    Seal(#[from] SealError),
}

fn is_hex64(v: &str) -> bool {
    v.len() == 64 && v.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

fn is_digits(v: &str) -> bool {
    !v.is_empty() && v.bytes().all(|b| b.is_ascii_digit()) && (v == "0" || !v.starts_with('0'))
}

fn is_rfc3339_utc(v: &str) -> bool {
    // YYYY-MM-DDTHH:MM:SS[.fraction]Z
    let b = v.as_bytes();
    if b.len() < 20 || !v.ends_with('Z') {
        return false;
    }
    let digits = |r: std::ops::Range<usize>| b[r].iter().all(u8::is_ascii_digit);
    let base = digits(0..4)
        && b[4] == b'-'
        && digits(5..7)
        && b[7] == b'-'
        && digits(8..10)
        && b[10] == b'T'
        && digits(11..13)
        && b[13] == b':'
        && digits(14..16)
        && b[16] == b':'
        && digits(17..19);
    let rest = &b[19..b.len() - 1];
    base && (rest.is_empty()
        || (rest[0] == b'.' && rest.len() > 1 && rest[1..].iter().all(u8::is_ascii_digit)))
}

/// The exact amount triple `{value, assetCode, assetScale}`. `value` is an
/// integer count of the smallest unit: 1,000 msat is `"1000"` at scale 11,
/// with `assetCode` `BTC` (Lightning; on-chain bitcoin is its CAIP-19 type,
/// a different asset).
pub fn amount(value: u128, asset_code: &str, asset_scale: u8) -> Value {
    json!({"value": value.to_string(), "assetCode": asset_code, "assetScale": asset_scale})
}

fn amount_ok(a: &Value) -> bool {
    let Some(o) = a.as_object() else { return false };
    o.len() == 3
        && o.get("value")
            .and_then(Value::as_str)
            .is_some_and(is_digits)
        && o.get("assetCode")
            .and_then(Value::as_str)
            .is_some_and(|c| !c.is_empty())
        && o.get("assetScale")
            .and_then(Value::as_u64)
            .is_some_and(|s| s <= 255)
        && o.get("assetScale").is_some_and(|s| s.is_u64())
}

/// The draft's structural failure codes for one `settlement` member (empty if
/// none). Mirrors the Python `capsule_emit.settlement.structure_failures`.
pub fn structure_failures(member: &Value) -> Vec<&'static str> {
    let Some(s) = member.as_object() else {
        return vec!["settlement_malformed"];
    };
    let leg = s.get("leg").and_then(Value::as_str).unwrap_or("");
    let role = s.get("sealer_role").and_then(Value::as_str).unwrap_or("");
    let Some((required, optional)) = members(leg) else {
        return vec!["settlement_malformed"];
    };
    if s.get("version").and_then(Value::as_str) != Some(SETTLEMENT_VERSION)
        || !ROLES.contains(&role)
    {
        return vec!["settlement_malformed"];
    }
    let mut out: Vec<&'static str> = Vec::new();
    let mut push = |code: &'static str| {
        if !out.contains(&code) {
            out.push(code);
        }
    };
    if !required.iter().all(|m| s.contains_key(*m))
        || !s
            .keys()
            .all(|k| required.contains(&k.as_str()) || optional.contains(&k.as_str()))
    {
        push("settlement_malformed");
    }
    if (leg == "payer_observed" && role != "payer") || (leg == "payee_observed" && role != "payee")
    {
        push("leg_role_mismatch");
    }
    if AMOUNT_MEMBERS
        .iter()
        .any(|m| s.get(*m).is_some_and(|a| !amount_ok(a)))
    {
        push("amount_not_exact");
    }
    if leg.ends_with("_observed")
        && !s
            .get("status")
            .and_then(Value::as_str)
            .is_some_and(|v| STATUSES.contains(&v))
    {
        push("settlement_malformed");
    }
    if s.get("terms_ref")
        .is_some_and(|v| !v.as_str().is_some_and(is_hex64))
    {
        push("settlement_malformed");
    }
    if s.get("observed_at")
        .is_some_and(|v| !v.as_str().is_some_and(is_rfc3339_utc))
    {
        push("settlement_malformed");
    }
    if leg == "delivered" {
        let ok = s
            .get("delivery")
            .and_then(Value::as_object)
            .is_some_and(|d| {
                let direction_ok = match d.get("direction").and_then(Value::as_str) {
                    Some("sent") => role == "payee",
                    Some("received") => role == "payer",
                    _ => false,
                };
                direction_ok
                    && d.keys().all(|k| DELIVERY_MEMBERS.contains(&k.as_str()))
                    && (d.contains_key("content_digest") || d.contains_key("carrier"))
                    && d.get("content_digest")
                        .is_none_or(|c| c.as_str().is_some_and(is_hex64))
            });
        if !ok {
            push("settlement_malformed");
        }
    }
    if leg == "terms" {
        if let Some(dv) = s.get("deliverable") {
            let ok = dv.as_object().is_some_and(|d| {
                !d.is_empty()
                    && d.iter().all(|(k, v)| {
                        matches!(k.as_str(), "content_digest" | "description_digest")
                            && v.as_str().is_some_and(is_hex64)
                    })
            });
            if !ok {
                push("settlement_malformed");
            }
        }
    }
    if let Some(r) = s.get("payment_ref") {
        match r.as_object() {
            Some(o)
                if o.get("type").is_some_and(Value::is_string)
                    && o.get("value").is_some_and(Value::is_string) =>
            {
                let ty = o["type"].as_str().unwrap_or("");
                if let Some((_, quals)) = PAYMENT_REF_TYPES.iter().find(|(t, _)| *t == ty) {
                    let want = 2 + quals.len();
                    if o.len() != want || !quals.iter().all(|q| o.contains_key(*q)) {
                        push("settlement_malformed");
                    }
                }
            }
            _ => push("settlement_malformed"),
        }
    }
    if let Some(w) = s.get("wrapped") {
        let ok = w.as_array().is_some_and(|items| {
            items.iter().all(|e| {
                e.as_object().is_some_and(|o| {
                    o.get("digest_alg").and_then(Value::as_str) == Some("SHA-256")
                        && o.get("type")
                            .and_then(Value::as_str)
                            .is_some_and(|t| WRAPPED_TYPES.contains(&t))
                        && o.get("digest")
                            .and_then(Value::as_str)
                            .is_some_and(is_hex64)
                        && o.keys().all(|k| {
                            matches!(k.as_str(), "type" | "digest_alg" | "digest" | "content")
                        })
                })
            })
        });
        if !ok {
            push("settlement_malformed");
        }
    }
    out
}

/// Build and validate one leg's `settlement` member from the draft's members
/// (`terms_ref`, `amount`, `routing_fee`, `received`, `receive_fee`,
/// `payment_ref`, `status`, `observed_at`, `deliverable`, `valid_until`,
/// `delivery`, `wrapped`). An EVM x402 transaction hash is lowercased (its
/// normal form). Record a fee the wallet reported as zero as a zero amount:
/// an absent fee states nothing.
pub fn build_leg(
    leg: &str,
    sealer_role: &str,
    members: Map<String, Value>,
) -> Result<Value, SettlementError> {
    let mut s = Map::new();
    s.insert("version".into(), json!(SETTLEMENT_VERSION));
    s.insert("leg".into(), json!(leg));
    s.insert("sealer_role".into(), json!(sealer_role));
    for (name, mut value) in members {
        if value.is_null() {
            continue;
        }
        if name == "payment_ref" {
            normalize_ref(&mut value);
        }
        s.insert(name, value);
    }
    let member = Value::Object(s);
    let codes = structure_failures(&member);
    if codes.is_empty() {
        Ok(member)
    } else {
        Err(SettlementError { codes })
    }
}

fn normalize_ref(r: &mut Value) {
    let Some(o) = r.as_object_mut() else { return };
    let evm = o.get("type").and_then(Value::as_str) == Some("x402.transaction")
        && o.get("network")
            .and_then(Value::as_str)
            .is_some_and(|n| n.starts_with("eip155:"));
    if evm {
        if let Some(v) = o.get("value").and_then(Value::as_str) {
            let lower = v.to_ascii_lowercase();
            o.insert("value".into(), json!(lower));
        }
    }
}

/// Cite the other party's leg: custody of it, not an observation of it.
pub fn counterparty_reference(capsule_id: &str) -> Value {
    json!({
        "type": REFERENCE_TYPE_AGENT_ACTION_CAPSULE,
        "digest_alg": "SHA-256",
        "digest": capsule_id,
        "citation_purpose": CITATION_PURPOSE_COUNTERPARTY_HALF,
    })
}

/// Seal one leg as a local record (`action_type` `fyi`, `effect_mode`
/// `not_applicable`) with the leg as its top-level `settlement` member.
/// `chain` links the sealer's own legs (relation `follows`, or `supersedes`
/// for a later observation of the same payment); `references` carries
/// [`counterparty_reference`] citations.
pub fn seal_leg(
    header: &LocalRecordHeader<'_>,
    action_id: String,
    member: Value,
    references: Option<Value>,
    chain: Option<ChainLink>,
    signing_key: &ed25519_dalek::SigningKey,
) -> Result<Value, SealLegError> {
    let codes = structure_failures(&member);
    if !codes.is_empty() {
        return Err(SettlementError { codes }.into());
    }
    let mut top = Map::new();
    top.insert(SETTLEMENT_MEMBER.into(), member);
    Ok(seal_local_record_with_members(
        header,
        action_id,
        Map::new(),
        top,
        references,
        chain,
        signing_key,
    )?)
}

#[cfg(test)]
mod tests {
    use super::*;
    use ed25519_dalek::SigningKey;

    const HASH: &str = "59c0c4d2859c994004ad65ce13831b9cdaf688828d6c9f08cdb06f1bda9f15dd";
    const AT: &str = "2026-10-02T02:41:16Z";

    fn msat(v: u128) -> Value {
        amount(v, "BTC", 11)
    }

    fn ln() -> Value {
        json!({"type": "ln.payment_hash", "value": HASH})
    }

    fn m(pairs: &[(&str, Value)]) -> Map<String, Value> {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.clone()))
            .collect()
    }

    fn payee_leg() -> Value {
        build_leg(
            "payee_observed",
            "payee",
            m(&[
                ("terms_ref", json!("a".repeat(64))),
                ("received", msat(995)),
                ("receive_fee", msat(5)),
                ("payment_ref", ln()),
                ("status", json!("settled")),
                ("observed_at", json!(AT)),
            ]),
        )
        .unwrap()
    }

    #[test]
    fn a_valid_leg_has_no_failures() {
        assert!(structure_failures(&payee_leg()).is_empty());
    }

    #[test]
    fn a_leg_carries_only_its_own_members() {
        let err = build_leg(
            "payee_observed",
            "payee",
            m(&[
                ("terms_ref", json!("a".repeat(64))),
                ("amount", msat(1000)),
                ("received", msat(995)),
                ("payment_ref", ln()),
                ("status", json!("settled")),
                ("observed_at", json!(AT)),
            ]),
        )
        .unwrap_err();
        assert_eq!(err.codes, vec!["settlement_malformed"]);
        let err = build_leg(
            "payer_observed",
            "payee",
            m(&[
                ("terms_ref", json!("a".repeat(64))),
                ("amount", msat(1000)),
                ("payment_ref", ln()),
                ("status", json!("settled")),
                ("observed_at", json!(AT)),
            ]),
        )
        .unwrap_err();
        assert_eq!(err.codes, vec!["leg_role_mismatch"]);
    }

    #[test]
    fn amounts_must_be_exact() {
        let mut leg = payee_leg();
        leg["received"]["value"] = json!("995.0");
        assert_eq!(structure_failures(&leg), vec!["amount_not_exact"]);
        leg["received"] = json!({"value": "995", "assetCode": "BTC", "assetScale": 11.0});
        assert_eq!(structure_failures(&leg), vec!["amount_not_exact"]);
        leg["received"] = json!({"value": "0995", "assetCode": "BTC", "assetScale": 11});
        assert_eq!(structure_failures(&leg), vec!["amount_not_exact"]);
    }

    #[test]
    fn status_and_time_are_closed() {
        let mut leg = payee_leg();
        leg["status"] = json!("received");
        assert_eq!(structure_failures(&leg), vec!["settlement_malformed"]);
        let mut leg = payee_leg();
        leg["observed_at"] = json!("2026-10-02 02:41:16");
        assert_eq!(structure_failures(&leg), vec!["settlement_malformed"]);
    }

    #[test]
    fn delivered_needs_a_content_digest_unless_a_carrier_is_present() {
        let base = m(&[
            ("terms_ref", json!("a".repeat(64))),
            ("observed_at", json!(AT)),
        ]);
        let with = |d: Value| {
            let mut x = base.clone();
            x.insert("delivery".into(), d);
            x
        };
        assert!(build_leg("delivered", "payee", with(json!({"direction": "sent"}))).is_err());
        assert!(build_leg(
            "delivered",
            "payee",
            with(json!({"direction": "received", "content_digest": "d".repeat(64)}))
        )
        .is_err());
        assert!(build_leg(
            "delivered",
            "payee",
            with(json!({"direction": "sent", "carrier": "example-carrier"}))
        )
        .is_ok());
        assert!(build_leg(
            "delivered",
            "payer",
            with(json!({"direction": "received", "content_digest": "d".repeat(64)}))
        )
        .is_ok());
    }

    #[test]
    fn known_reference_types_need_exactly_their_qualifiers() {
        let mut leg = payee_leg();
        leg["payment_ref"] = json!({"type": "x402.transaction", "value": "0xab"});
        assert_eq!(structure_failures(&leg), vec!["settlement_malformed"]);
        leg["payment_ref"] = json!({"type": "example.rail_ref", "value": "r-1"});
        assert!(
            structure_failures(&leg).is_empty(),
            "an unknown type is a verifier finding, not malformed"
        );
    }

    #[test]
    fn an_evm_transaction_hash_takes_its_normal_form() {
        let leg = build_leg(
            "terms",
            "payee",
            m(&[
                ("amount", amount(10000, "USD", 6)),
                (
                    "payment_ref",
                    json!({"type": "x402.transaction", "value": "0xAB", "network": "eip155:84532"}),
                ),
            ]),
        )
        .unwrap();
        assert_eq!(leg["payment_ref"]["value"], json!("0xab"));
    }

    #[test]
    fn a_sealed_leg_carries_the_member_at_the_top_level_and_verifies() {
        let key = SigningKey::from_bytes(&[7u8; 32]);
        let header = LocalRecordHeader {
            operator: "op",
            developer: "dev",
            provider: "mesh-llm",
            model_id: "model",
        };
        let capsule = seal_leg(
            &header,
            "settlement/payee_observed".into(),
            payee_leg(),
            Some(json!([counterparty_reference(&"b".repeat(64))])),
            None,
            &key,
        )
        .unwrap();
        assert_eq!(
            capsule[SETTLEMENT_MEMBER]["received"]["value"],
            json!("995")
        );
        assert_eq!(capsule["action_type"], json!("fyi"));
        assert_eq!(capsule["assurance"]["effect_mode"], json!("not_applicable"));
        assert_eq!(
            capsule["references"][0]["type"],
            json!("agent-action-capsule")
        );
        let id = crate::jcs::compute_capsule_id(&{
            let mut c = capsule.clone();
            let o = c.as_object_mut().unwrap();
            o.remove("capsule_id");
            o.remove("signature");
            o.remove("key_id");
            c
        })
        .unwrap();
        assert_eq!(capsule["capsule_id"], json!(id));
    }

    #[test]
    fn a_malformed_leg_is_never_sealed_and_members_cannot_replace_core_ones() {
        let key = SigningKey::from_bytes(&[7u8; 32]);
        let header = LocalRecordHeader {
            operator: "op",
            developer: "dev",
            provider: "p",
            model_id: "m",
        };
        let mut leg = payee_leg();
        leg["status"] = json!("received");
        assert!(matches!(
            seal_leg(&header, "x".into(), leg, None, None, &key),
            Err(SealLegError::Leg(_))
        ));
        let mut top = Map::new();
        top.insert("operator".into(), json!("x"));
        assert!(matches!(
            seal_local_record_with_members(&header, "x".into(), Map::new(), top, None, None, &key),
            Err(SealError::ReservedMember(_))
        ));
    }
}
