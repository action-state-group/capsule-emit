//! Shared test inputs.
#![allow(dead_code)]

use capsule_emit::capsule::{CapsuleInput, ChainLink};
use serde_json::{json, Map};

/// A well-formed capsule input: `label` and `n` make each one distinct, and
/// `chain` links it to its parent. No extension members.
pub fn capsule_input(label: &str, n: usize, chain: Option<ChainLink>) -> CapsuleInput {
    CapsuleInput {
        action_id: format!("example/{label}/{n:08}"),
        action_type: "decide".to_string(),
        operator: "example-operator".to_string(),
        developer: format!("capsule-emit/{label}-test"),
        timestamp: "2026-09-28T00:00:00Z".to_string(),
        domain: Some("action".to_string()),
        provenance: Some("collector".to_string()),
        model_id: "example-model".to_string(),
        provider: "example-provider".to_string(),
        agent_input_digest: format!("{n:064x}"),
        agent_output_digest: Some(format!("{:064x}", n + 1)),
        tool_calls_digest: None,
        reasoning_digest: None,
        host_binding: None,
        runtime: json!({"name": format!("{label}-runtime")}),
        compute_attestation_extensions: Map::new(),
        effect_status: "confirmed".to_string(),
        effect_type: "inference_completion".to_string(),
        effect_request_digest: Some(format!("{n:064x}")),
        effect_response_digest: Some(format!("{:064x}", n + 1)),
        effect_attestation: "gate_executed".to_string(),
        disposition_decision: "accept".to_string(),
        disposition_approver: "policy".to_string(),
        disposition_human_disposed: false,
        disposition_verdict_class: "executed".to_string(),
        chain,
        store_nonce: format!("{:064x}", 0x5eed_0000 + n),
    }
}

/// `ChainLink` to `parent`, relation `follows`.
pub fn follows(parent: &str) -> ChainLink {
    ChainLink {
        parent_capsule_id: parent.to_string(),
        relation: "follows".to_string(),
    }
}
