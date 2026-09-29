//! Byte-for-byte check of this crate's `capsule_id` (JCS + SHA-256) against
//! the Agent Action Capsule conformance vectors' `canonical-*` cases. Each is
//! a full format-4 capsule whose `expected.json.capsule_id_recomputed` is the
//! JSON-DIGEST of the object with `capsule_id` (and the producer-envelope
//! fields `signature`/`key_id`) removed.
//!
//! The vectors are a pinned copy in `tests/vectors/aac-capsule/` (see
//! `tests/vectors/SOURCES.md`). `AAC_TEST_VECTORS_DIR` points the test at
//! another copy instead, e.g. a newer checkout of the spec repository.

use capsule_emit::jcs::compute_capsule_id;
use serde_json::Value;
use std::path::PathBuf;

#[test]
fn canonical_vectors_match_the_reference_byte_for_byte() {
    let dir = std::env::var("AAC_TEST_VECTORS_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| {
            PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/vectors/aac-capsule")
        });

    let manifest: Value = serde_json::from_slice(
        &std::fs::read(dir.join("vectors.json")).expect("read vectors.json"),
    )
    .expect("parse vectors.json");
    let cases = manifest["cases"].as_array().expect("cases array");
    let canonical_cases: Vec<&str> = cases
        .iter()
        .filter(|c| {
            c["name"]
                .as_str()
                .is_some_and(|n| n.starts_with("canonical-"))
        })
        .map(|c| c["name"].as_str().unwrap())
        .collect();
    assert!(
        !canonical_cases.is_empty(),
        "expected at least one canonical-prefixed vector"
    );

    let mut checked = 0;
    for name in &canonical_cases {
        let case_dir = dir.join(name);
        let input: Value = serde_json::from_slice(
            &std::fs::read(case_dir.join("input.json"))
                .unwrap_or_else(|e| panic!("read {}/input.json: {e}", case_dir.display())),
        )
        .unwrap_or_else(|e| panic!("parse {}/input.json: {e}", case_dir.display()));
        let expected: Value = serde_json::from_slice(
            &std::fs::read(case_dir.join("expected.json")).expect("read expected.json"),
        )
        .expect("parse expected.json");

        let expected_digest = expected["capsule_id_recomputed"]
            .as_str()
            .unwrap_or_else(|| panic!("{name}: expected.json has no capsule_id_recomputed"));

        match compute_capsule_id(&input) {
            Ok(digest) => assert_eq!(
                digest, expected_digest,
                "{name}: capsule_id mismatch (Rust vs Python reference)"
            ),
            Err(e) => panic!(
                "{name}: Rust rejected input the Python reference digested \
                 (expected {expected_digest}): {e}"
            ),
        }
        checked += 1;
    }
    eprintln!("{checked} canonical vectors matched byte-for-byte");
}
