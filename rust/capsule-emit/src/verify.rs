//! Offline verification: the Class 1 checks on the capsule's own bytes
//! (`agent_action_capsule::verify::verify`, which recomputes `capsule_id` and,
//! given a store, checks chain-parent membership), plus the COSE_Sign1
//! signature, the COSE payload matching the supplied capsule bytes, and the
//! CWT subject naming that capsule -- entirely local, no network calls. The
//! Class 1 verdict is the reference verifier's own; this module adds only the
//! signed-statement checks, which that verifier leaves to the SCITT/COSE
//! substrate.
//!
//! Chain-parent membership follows the reference verifier's Check 6: with no
//! store supplied, an unresolvable parent is an `info`-level finding, not a
//! failure (the capsule may simply be verified in isolation, without its
//! ledger); with a store, a missing parent gates the verdict.

use crate::cose::verify_signed_statement;
use agent_action_capsule::verify::{verify, Finding};
use ed25519_dalek::VerifyingKey;
use serde_json::Value;
use std::collections::HashSet;

#[derive(Debug)]
pub struct VerifyReport {
    /// The capsule passes the reference verifier's Class 1 checks
    /// (`agent_action_capsule::verify::verify`); each failing one is also in
    /// `findings`.
    pub structure_ok: bool,
    /// The carried `capsule_id` recomputes (Class 1 check 2).
    pub capsule_id_ok: bool,
    pub cose_ok: bool,
    pub payload_matches_capsule: bool,
    /// The COSE protected header's CWT `sub` claim equals the recomputed
    /// `capsule_id`. GATED by [`ok`](Self::ok): a receipt whose subject was
    /// spliced from another capsule must not verify green -- previously this
    /// mismatch only landed in `findings` and `ok()` still passed.
    pub subject_matches_capsule_id: bool,
    /// The chain block passes Class 1 check 6: well formed, and with a store,
    /// its parent is in it.
    pub chain_ok: bool,
    pub capsule_id: Option<String>,
    pub findings: Vec<String>,
}

impl VerifyReport {
    pub fn ok(&self) -> bool {
        self.structure_ok
            && self.capsule_id_ok
            && self.cose_ok
            && self.payload_matches_capsule
            && self.subject_matches_capsule_id
            && self.chain_ok
    }
}

fn is_error(finding: &Finding) -> bool {
    finding.severity == "error"
}

pub fn verify_offline(
    capsule: &Value,
    signed_statement: &[u8],
    verifying_key: &VerifyingKey,
    known_capsule_ids: Option<&HashSet<String>>,
) -> VerifyReport {
    let mut findings = Vec::new();

    // The reference verifier reads a store of bare `capsule_id` strings.
    let store: Option<Vec<Value>> =
        known_capsule_ids.map(|ids| ids.iter().map(|id| Value::String(id.clone())).collect());
    let class1 = verify(capsule, store.as_deref());
    // Every error, and check 6's note that a parent was not looked up.
    for finding in &class1.findings {
        if is_error(finding) || finding.check == Some(6) {
            findings.push(format!(
                "check {}: {}: {}",
                finding.check.unwrap_or(0),
                finding.code,
                finding.detail
            ));
        }
    }
    let failed = |check: i32| {
        class1
            .findings
            .iter()
            .any(|f| is_error(f) && f.check == Some(check))
    };
    let stored = capsule.get("capsule_id").and_then(Value::as_str);
    let capsule_id = class1
        .capsule_id
        .clone()
        .filter(|recomputed| stored == Some(recomputed.as_str()));
    let capsule_id_ok = capsule_id.is_some() && !failed(2);
    let chain_ok = !failed(6);

    let mut cose_ok = false;
    let mut payload_matches_capsule = false;
    let mut subject_matches_capsule_id = false;
    match verify_signed_statement(signed_statement, verifying_key) {
        Ok(verified) => {
            cose_ok = true;
            match crate::jcs::decode_record(&verified.payload) {
                Ok(payload_json) if payload_json == *capsule => payload_matches_capsule = true,
                Ok(_) => findings
                    .push("COSE payload does not match the supplied capsule JSON".to_string()),
                Err(e) => findings.push(format!("COSE payload is not valid JSON: {e}")),
            }
            // Gated (not merely a finding): a spliced CWT subject must fail
            // the overall verdict, matching what the subject claim is FOR --
            // binding the receipt to exactly this capsule_id.
            if verified.subject.is_some() && verified.subject.as_deref() == capsule_id.as_deref() {
                subject_matches_capsule_id = true;
            } else {
                findings.push(format!(
                    "COSE subject {:?} does not match recomputed capsule_id {:?}",
                    verified.subject, capsule_id
                ));
            }
        }
        Err(e) => findings.push(format!("COSE verification failed: {e}")),
    }

    VerifyReport {
        structure_ok: class1.ok,
        capsule_id_ok,
        cose_ok,
        payload_matches_capsule,
        subject_matches_capsule_id,
        chain_ok,
        capsule_id,
        findings,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cose::{build_signed_statement, SignedStatementInput};
    use crate::jcs::compute_capsule_id;
    use serde_json::{json, Map};

    fn sample_capsule(seed: &str) -> Value {
        let mut body = Map::new();
        body.insert(
            "spec_version".into(),
            json!("draft-mih-scitt-agent-action-capsule-02"),
        );
        body.insert("format_version".into(), json!("4"));
        body.insert("canonicalization_id".into(), json!("jcs"));
        body.insert("action_id".into(), json!(format!("verify-test/{seed}")));
        body.insert("action_type".into(), json!("fyi"));
        body.insert("operator".into(), json!("verify-test"));
        body.insert("developer".into(), json!("verify-test@v1"));
        body.insert("timestamp".into(), json!("2026-09-29T00:00:00Z"));
        body.insert("seed".into(), json!(seed));
        let capsule_id = compute_capsule_id(&Value::Object(body.clone())).unwrap();
        body.insert("capsule_id".into(), json!(capsule_id));
        Value::Object(body)
    }

    fn statement_with_subject(
        capsule: &Value,
        subject: &str,
        key: &ed25519_dalek::SigningKey,
    ) -> Vec<u8> {
        build_signed_statement(
            &SignedStatementInput {
                payload: &serde_json::to_vec(capsule).unwrap(),
                issuer: "verify-test",
                subject,
                content_type: "application/vnd.agent-action-capsule+json",
            },
            key,
        )
    }

    #[test]
    fn genuine_statement_verifies_green_with_subject_gated() {
        let key = ed25519_dalek::SigningKey::from_bytes(&[5u8; 32]);
        let capsule = sample_capsule("one");
        let capsule_id = capsule["capsule_id"].as_str().unwrap();
        let statement = statement_with_subject(&capsule, capsule_id, &key);

        let report = verify_offline(&capsule, &statement, &key.verifying_key(), None);
        assert!(report.subject_matches_capsule_id, "{:?}", report.findings);
        assert!(report.ok(), "{:?}", report.findings);
    }

    /// The spliced-subject receipt: signature and payload are genuine, but
    /// the CWT `sub` claim names ANOTHER capsule -- `ok()` must gate on it,
    /// not just note it in findings.
    #[test]
    fn spliced_cwt_subject_fails_the_overall_verdict() {
        let key = ed25519_dalek::SigningKey::from_bytes(&[5u8; 32]);
        let capsule = sample_capsule("one");
        let other_id = "f".repeat(64);
        let statement = statement_with_subject(&capsule, &other_id, &key);

        let report = verify_offline(&capsule, &statement, &key.verifying_key(), None);
        // The splice's whole point: every previously-gated boolean passes...
        assert!(report.capsule_id_ok);
        assert!(report.cose_ok);
        assert!(report.payload_matches_capsule);
        assert!(report.chain_ok);
        // ...and only the subject gate stands between it and a green verdict.
        assert!(!report.subject_matches_capsule_id);
        assert!(!report.ok(), "a spliced subject must not verify green");
        assert!(report.findings.iter().any(|f| f.contains("COSE subject")));
    }
}
