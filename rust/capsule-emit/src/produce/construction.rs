//! Foreign-artifact and composition bindings, as Go's `construction.go`.

use agent_action_capsule::verify::is_hex64;
use sha2::{Digest, Sha256};
use std::collections::HashMap;

use super::build::{build_with, Binding, DigestReference};
use super::model::{BuiltPayload, Input, Sealed};
use super::{verify_capsule, ProduceError};

const DIGEST_ALGORITHM_SHA256: &str = "SHA-256";
const FOREIGN_ARTIFACT_TYPE: &str = "foreign-artifact";
const CAPSULE_REFERENCE_TYPE: &str = "capsule";

/// Build a Capsule that binds an opaque foreign artifact exactly as received,
/// typed `foreign-artifact`, without parsing or canonicalizing its bytes.
pub fn carry(input: Input, artifact: &[u8]) -> Result<BuiltPayload, ProduceError> {
    received(input, artifact, FOREIGN_ARTIFACT_TYPE)
}

/// Build a Capsule that binds an opaque foreign artifact and its
/// caller-declared registered type. The SHA-256 of the exact bytes becomes
/// both `carried_artifact.digest` and `carried_input_digest`.
pub fn received(
    input: Input,
    artifact: &[u8],
    artifact_type: &str,
) -> Result<BuiltPayload, ProduceError> {
    if artifact.is_empty() {
        return Err(ProduceError::invalid("received artifact must not be empty"));
    }
    if artifact_type.trim().is_empty() {
        return Err(ProduceError::invalid(
            "received artifact type must be a non-empty string",
        ));
    }
    let digest = hex::encode(Sha256::digest(artifact));
    let binding = Binding {
        carried_artifact: Some(DigestReference {
            reference_type: artifact_type.to_string(),
            digest_alg: DIGEST_ALGORITHM_SHA256.to_string(),
            digest: digest.clone(),
            slot: "",
        }),
        carried_input_digest: digest,
        composed_members: Vec::new(),
    };
    build_with(input, Some(binding))
}

/// A built or sealed Capsule that can fill a composition slot.
pub trait Composable {
    fn capsule_id(&self) -> &str;
    /// The signature-free Capsule bytes.
    fn capsule_json(&self) -> &[u8];
}

impl Composable for BuiltPayload {
    fn capsule_id(&self) -> &str {
        &self.capsule_id
    }
    fn capsule_json(&self) -> &[u8] {
        &self.json
    }
}

impl Composable for Sealed {
    fn capsule_id(&self) -> &str {
        &self.capsule_id
    }
    fn capsule_json(&self) -> &[u8] {
        &self.payload
    }
}

/// The closed composition slots, in canonical order.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
enum Slot {
    Who,
    Can,
    Did,
    Audit,
}

impl Slot {
    fn name(self) -> &'static str {
        match self {
            Slot::Who => "who",
            Slot::Can => "can",
            Slot::Did => "did",
            Slot::Audit => "audit",
        }
    }
}

/// An existing Capsule bound to one composition role. Only [`who`], [`can`],
/// [`did`] and [`audit`] make one.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SlotMember {
    slot: Slot,
    capsule_id: String,
    json: Vec<u8>,
}

fn slot_member(slot: Slot, member: &impl Composable) -> SlotMember {
    SlotMember {
        slot,
        capsule_id: member.capsule_id().to_string(),
        json: member.capsule_json().to_vec(),
    }
}

/// Assign an existing Capsule to the identity or pedigree slot.
pub fn who(member: &impl Composable) -> SlotMember {
    slot_member(Slot::Who, member)
}

/// Assign an existing Capsule to the authority or mandate slot.
pub fn can(member: &impl Composable) -> SlotMember {
    slot_member(Slot::Can, member)
}

/// Assign an existing Capsule to the performed-action slot.
pub fn did(member: &impl Composable) -> SlotMember {
    slot_member(Slot::Did, member)
}

/// Assign an existing Capsule to the independent-review slot.
pub fn audit(member: &impl Composable) -> SlotMember {
    slot_member(Slot::Audit, member)
}

/// Build a Capsule that binds existing signature-free Capsules into the WHO,
/// CAN, DID and AUDIT roles. Members are written in slot order whatever the
/// argument order; empty membership, a repeated slot, a repeated Capsule ID
/// and a member that is not a matching verified format-4 Capsule are refused.
pub fn build_composition(
    input: Input,
    members: &[SlotMember],
) -> Result<BuiltPayload, ProduceError> {
    if members.is_empty() {
        return Err(ProduceError::invalid(
            "composition requires at least one slot member",
        ));
    }
    let mut by_slot: HashMap<Slot, &SlotMember> = HashMap::new();
    let mut seen_ids: HashMap<&str, Slot> = HashMap::new();
    for member in members {
        let slot = member.slot.name();
        if by_slot.contains_key(&member.slot) {
            return Err(ProduceError::invalid(format!(
                "composition duplicates {slot} slot"
            )));
        }
        if !is_hex64(&member.capsule_id) {
            return Err(ProduceError::invalid(format!(
                "composition {slot} member has malformed Capsule ID"
            )));
        }
        let matches = verify_capsule(&member.json).is_ok_and(|verified| {
            verified.capsule_id.as_deref() == Some(member.capsule_id.as_str())
        });
        if !matches {
            return Err(ProduceError::invalid(format!(
                "composition {slot} member is not a matching verified format-4 Capsule"
            )));
        }
        if let Some(prior) = seen_ids.get(member.capsule_id.as_str()) {
            return Err(ProduceError::invalid(format!(
                "composition {slot} slot duplicates Capsule ID {} from {} slot",
                member.capsule_id,
                prior.name()
            )));
        }
        seen_ids.insert(&member.capsule_id, member.slot);
        by_slot.insert(member.slot, member);
    }

    let mut ordered: Vec<&SlotMember> = by_slot.into_values().collect();
    ordered.sort_by_key(|member| member.slot);
    let composed_members = ordered
        .into_iter()
        .map(|member| DigestReference {
            reference_type: CAPSULE_REFERENCE_TYPE.to_string(),
            digest_alg: DIGEST_ALGORITHM_SHA256.to_string(),
            digest: member.capsule_id.clone(),
            slot: member.slot.name(),
        })
        .collect();
    build_with(
        input,
        Some(Binding {
            composed_members,
            ..Binding::default()
        }),
    )
}
