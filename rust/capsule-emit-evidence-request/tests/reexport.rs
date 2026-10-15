//! The 0.0.4 paths still resolve, to the evidence-request items themselves.

use capsule_emit_evidence_request::digest::request_digest;
use capsule_emit_evidence_request::registry::Reason;
use capsule_emit_evidence_request::{answer, receipt, refusal, request, resolve};

#[test]
fn the_old_paths_are_the_new_items() {
    assert_eq!(
        request_digest(b"{}"),
        evidence_request::digest::request_digest(b"{}")
    );
    assert_eq!(Reason::NoSuchSubject.token(), "no_such_subject");
    assert!(request::parse_json(br#"{"subject":{"record":"x"}}"#).is_err());
    // The answer path and witness receipts come through, as in 0.0.4.
    assert_eq!(answer::MAX_RECORDS, evidence_request::answer::MAX_RECORDS);
    let _: fn(&[u8]) -> Result<(), receipt::ReceiptError> = receipt::check_form;
    let _ = refusal::sign;
    let _: fn(&request::Request, &dyn resolve::Responder) -> resolve::Resolution = resolve::resolve;
}
