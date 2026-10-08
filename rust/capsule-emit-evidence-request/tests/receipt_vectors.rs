//! Byte-identical derivatives of the donated scitt-cose conformance corpus.
use base64::{engine::general_purpose::STANDARD, Engine as _};
use capsule_emit_evidence_request::receipt::{self, ReceiptError};
use ed25519_dalek::VerifyingKey;
use sha2::{Digest, Sha256};
use std::path::PathBuf;

fn vector_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/vectors/scitt-proof-array")
}
fn verify_pinned_checksums() {
    let dir = vector_dir();
    let sums = std::fs::read_to_string(dir.join("SHA256SUMS")).unwrap();
    let mut checked = 0;
    for line in sums.lines() {
        let (want, name) = line.split_once("  ").unwrap();
        let bytes = std::fs::read(dir.join(name)).unwrap();
        assert_eq!(
            hex::encode(Sha256::digest(bytes)),
            want,
            "{name}: pinned checksum mismatch"
        );
        checked += 1;
    }
    assert_eq!(checked, 7, "all donated consumer files must be pinned");
}
fn vector(case: &str, file: &str) -> Vec<u8> {
    verify_pinned_checksums();
    std::fs::read(vector_dir().join(case).join(file)).unwrap()
}
#[test]
fn donated_files_match_pinned_checksums() {
    verify_pinned_checksums();
}
fn inputs(case: &str) -> (Vec<u8>, VerifyingKey) {
    let expected: serde_json::Value =
        serde_json::from_slice(&vector(case, "expected.json")).unwrap();
    let leaf = hex::decode(expected["leaf_entry"].as_str().unwrap()).unwrap();
    let pem = String::from_utf8(vector(case, "log-key.pub")).unwrap();
    let der = STANDARD
        .decode(
            pem.lines()
                .filter(|l| !l.starts_with("-----"))
                .collect::<String>(),
        )
        .unwrap();
    assert_eq!(
        &der[..12],
        &[0x30, 0x2a, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70, 0x03, 0x21, 0x00]
    );
    (
        leaf,
        VerifyingKey::from_bytes(der[12..].try_into().unwrap()).unwrap(),
    )
}
#[test]
fn donated_target_proof_at_index_one_and_swapped_pair_verify() {
    let case = "valid-eddsa-multi-proof";
    let (leaf, key) = inputs(case);
    for filename in ["receipt.cose", "receipt-reordered.cose"] {
        let bytes = vector(case, filename);
        assert_eq!(receipt::check_form(&bytes), Ok(()));
        assert_eq!(receipt::verify(&bytes, &leaf, &key), Ok(()), "{filename}");
    }
}
#[test]
fn donated_zero_tree_check_form_returns_malformed() {
    let bytes = vector("fail-zero-size-proof", "receipt.cose");
    assert!(matches!(
        receipt::check_form(&bytes),
        Err(ReceiptError::Malformed(_))
    ));
}
#[test]
fn donated_zero_tree_verify_with_key_returns_malformed() {
    let case = "fail-zero-size-proof";
    let (leaf, key) = inputs(case);
    let bytes = vector(case, "receipt.cose");
    assert!(matches!(
        receipt::verify(&bytes, &leaf, &key),
        Err(ReceiptError::Malformed(_))
    ));
}
