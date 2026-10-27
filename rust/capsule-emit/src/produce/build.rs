//! [`build`]: validate an [`Input`], derive assurance, compute the Capsule ID
//! and return canonical bytes, as Go's `build.go` and `extension.go`.

use agent_action_capsule::canonical;
use agent_action_capsule::verify::is_hex64;
use chrono::{DateTime, Utc};
use serde_json::{json, Map, Value};

use super::constants::*;
use super::model::{
    BuiltPayload, ComputeAttestation, Disposition, Effect, Extension, Input, Model, ProvenanceMode,
};
use super::{Class1Error, ProduceError};

/// A typed digest reference: a carried artifact or a composed member.
#[derive(Clone, Debug)]
pub(super) struct DigestReference {
    pub reference_type: String,
    pub digest_alg: String,
    pub digest: String,
    pub slot: &'static str,
}

/// The bindings only [`super::carry`], [`super::received`] and
/// [`super::build_composition`] set, never a caller.
#[derive(Clone, Debug, Default)]
pub(super) struct Binding {
    pub carried_artifact: Option<DigestReference>,
    pub carried_input_digest: String,
    pub composed_members: Vec<DigestReference>,
}

/// Validate `input`, derive its assurance, compute `capsule_id` and return the
/// Capsule's canonical bytes. The built Capsule must pass AAC Class 1.
pub fn build(input: Input) -> Result<BuiltPayload, ProduceError> {
    build_with(input, None)
}

pub(super) fn build_with(
    input: Input,
    binding: Option<Binding>,
) -> Result<BuiltPayload, ProduceError> {
    validate_input(&input, binding.as_ref())?;
    let timestamp = input.timestamp.expect("validated");

    let mut payload = Map::new();
    payload.insert("spec_version".into(), json!(SPEC_VERSION));
    payload.insert("format_version".into(), json!(FORMAT_VERSION));
    payload.insert("canonicalization_id".into(), json!(CANONICALIZATION_ID));
    payload.insert("action_id".into(), json!(input.action_id));
    payload.insert("action_type".into(), json!(input.action_type));
    payload.insert("operator".into(), json!(input.operator));
    payload.insert("developer".into(), json!(input.developer));
    payload.insert("timestamp".into(), json!(format_timestamp(&timestamp)));
    payload.insert(
        "assurance".into(),
        assurance(input.effect.as_ref(), input.chain.is_some()),
    );
    insert_text(&mut payload, "epoch_id", &input.epoch_id);
    insert_text(&mut payload, "domain", &input.domain);
    insert_text(&mut payload, "provenance", &input.provenance);
    if let Some(mode) = &input.provenance_mode {
        payload.insert("provenance_mode".into(), provenance_mode_value(mode));
    }
    if let Some(disposition) = &input.disposition {
        payload.insert("disposition".into(), disposition_value(disposition));
    }
    if let Some(effect) = &input.effect {
        payload.insert("effect".into(), effect_value(effect));
    }
    if let Some(chain) = &input.chain {
        payload.insert(
            "chain".into(),
            json!({"parent_capsule_id": chain.parent_capsule_id, "relation": chain.relation}),
        );
    }
    if let Some(attestation) = model_attestation(
        input.model.as_ref(),
        input.compute.as_ref(),
        binding.as_ref(),
    ) {
        payload.insert("model_attestation".into(), attestation);
    }
    if let Some(references) = &input.references {
        let values = references
            .iter()
            .map(|reference| {
                let mut value = digest_reference_value(
                    &reference.reference_type,
                    &reference.digest_alg,
                    &reference.digest,
                    "",
                );
                insert_text(&mut value, "citation_purpose", &reference.citation_purpose);
                if let Some(coordinates) = &reference.log_coordinates {
                    value.insert("log_coordinates".into(), Value::Object(coordinates.clone()));
                }
                Value::Object(value)
            })
            .collect();
        payload.insert("references".into(), Value::Array(values));
    }
    for extension in &input.extensions {
        let value = extension_value(extension, &payload)?;
        payload.insert(extension.name.clone(), value);
    }

    let capsule_id = canonical::compute_capsule_id(&payload)
        .map_err(|e| ProduceError::invalid(format!("compute capsule id: {e}")))?;
    payload.insert("capsule_id".into(), json!(capsule_id));
    let record = Value::Object(payload);
    let class1 = agent_action_capsule::verify::verify(&record, None);
    if !class1.ok {
        return Err(ProduceError::BuiltClass1(Class1Error {
            findings: class1.findings,
        }));
    }
    let encoded = canonical::jcs(&record)
        .map_err(|e| ProduceError::invalid(format!("encode canonical capsule payload: {e}")))?;
    let Value::Object(value) = record else {
        unreachable!("built as an object")
    };
    Ok(BuiltPayload {
        capsule_id,
        value,
        json: encoded,
    })
}

fn insert_text(map: &mut Map<String, Value>, key: &str, value: &str) {
    if !value.is_empty() {
        map.insert(key.into(), json!(value));
    }
}

/// The UTC wire form: whole seconds omit the fraction; any other instant is
/// truncated to microseconds and always written with six digits.
pub(super) fn format_timestamp(timestamp: &DateTime<Utc>) -> String {
    let seconds = timestamp.format("%Y-%m-%dT%H:%M:%S");
    match timestamp.timestamp_subsec_nanos() / 1_000 {
        0 => format!("{seconds}Z"),
        micros => format!("{seconds}.{micros:06}Z"),
    }
}

fn assurance(effect: Option<&Effect>, chained: bool) -> Value {
    let effect_mode = match effect {
        Some(effect) if effect.status == EFFECT_CONFIRMED => EFFECT_MODE_CONFIRMED,
        Some(effect) if effect.status != EFFECT_PLANNED => EFFECT_MODE_DISPATCHED_UNCONFIRMED,
        _ => EFFECT_MODE_NOT_APPLICABLE,
    };
    let ledger_mode = if chained {
        LEDGER_MODE_CHAINED
    } else {
        LEDGER_MODE_STANDALONE
    };
    json!({
        "effect_mode": effect_mode,
        "attestation_mode": ATTESTATION_MODE_SELF_ATTESTED,
        "ledger_mode": ledger_mode,
    })
}

fn model_attestation(
    model: Option<&Model>,
    compute: Option<&ComputeAttestation>,
    binding: Option<&Binding>,
) -> Option<Value> {
    let mut result = Map::new();
    if let Some(model) = model {
        insert_text(&mut result, "model_id", &model.model_id);
        insert_text(&mut result, "provider", &model.provider);
    }
    let mut attestation = Map::new();
    if let Some(compute) = compute {
        insert_text(
            &mut attestation,
            "agent_input_digest",
            &compute.agent_input_digest,
        );
        insert_text(
            &mut attestation,
            "agent_output_digest",
            &compute.agent_output_digest,
        );
        insert_text(&mut attestation, "runtime", &compute.runtime);
    }
    if let Some(binding) = binding {
        if let Some(artifact) = &binding.carried_artifact {
            attestation.insert(
                "carried_artifact".into(),
                Value::Object(reference_map(artifact)),
            );
            attestation.insert(
                "carried_input_digest".into(),
                json!(binding.carried_input_digest),
            );
        }
        if !binding.composed_members.is_empty() {
            let members = binding
                .composed_members
                .iter()
                .map(|member| Value::Object(reference_map(member)))
                .collect();
            attestation.insert("composed_members".into(), Value::Array(members));
        }
    }
    if !attestation.is_empty() {
        result.insert("compute_attestation".into(), Value::Object(attestation));
    }
    (!result.is_empty()).then_some(Value::Object(result))
}

fn reference_map(reference: &DigestReference) -> Map<String, Value> {
    digest_reference_value(
        &reference.reference_type,
        &reference.digest_alg,
        &reference.digest,
        reference.slot,
    )
}

fn digest_reference_value(
    reference_type: &str,
    digest_alg: &str,
    digest: &str,
    slot: &str,
) -> Map<String, Value> {
    let mut result = Map::new();
    result.insert("type".into(), json!(reference_type));
    result.insert("digest_alg".into(), json!(digest_alg));
    result.insert("digest".into(), json!(digest));
    insert_text(&mut result, "slot", slot);
    result
}

/// The §5.3(bis) wire shape: the mode plus whichever companions are set.
fn provenance_mode_value(mode: &ProvenanceMode) -> Value {
    let mut result = Map::new();
    result.insert("mode".into(), json!(mode.mode));
    if let Some(source) = &mode.source_ref {
        result.insert(
            "source_ref".into(),
            Value::Object(digest_reference_value(
                &source.reference_type,
                &source.digest_alg,
                &source.digest,
                "",
            )),
        );
    }
    insert_text(&mut result, "source_asserted_at", &mode.source_asserted_at);
    insert_text(&mut result, "import_batch", &mode.import_batch);
    insert_text(&mut result, "imported_at", &mode.imported_at);
    insert_text(&mut result, "time_rung", &mode.time_rung);
    Value::Object(result)
}

fn disposition_value(disposition: &Disposition) -> Value {
    let mut result = Map::new();
    result.insert("decision".into(), json!(disposition.decision));
    result.insert("approver".into(), json!(disposition.approver));
    result.insert("human_disposed".into(), json!(disposition.human_disposed));
    insert_text(&mut result, "verdict_class", &disposition.verdict_class);
    insert_text(&mut result, "reason_digest", &disposition.reason_digest);
    Value::Object(result)
}

fn effect_value(effect: &Effect) -> Value {
    let mut result = Map::new();
    result.insert("type".into(), json!(effect.effect_type));
    result.insert("status".into(), json!(effect.status));
    result.insert(
        "irreversibility_class".into(),
        json!(effect.irreversibility_class),
    );
    insert_text(
        &mut result,
        "effect_attestation",
        &effect.effect_attestation,
    );
    insert_text(&mut result, "request_digest", &effect.request_digest);
    insert_text(&mut result, "response_digest", &effect.response_digest);
    insert_text(&mut result, "external_ref", &effect.external_ref);
    Value::Object(result)
}

fn invalid<T>(text: impl Into<String>) -> Result<T, ProduceError> {
    Err(ProduceError::invalid(text))
}

fn validate_input(input: &Input, binding: Option<&Binding>) -> Result<(), ProduceError> {
    for (name, value) in [
        ("action id", &input.action_id),
        ("operator", &input.operator),
        ("developer", &input.developer),
    ] {
        if value.trim().is_empty() {
            return invalid(format!("{name} is required"));
        }
    }
    if input.timestamp.is_none() {
        return invalid("timestamp is required");
    }
    if input.action_type != ACTION_TYPE_FYI && input.action_type != ACTION_TYPE_DECIDE {
        return invalid(format!(
            "action type must be {ACTION_TYPE_FYI:?} or {ACTION_TYPE_DECIDE:?}"
        ));
    }
    if input.action_type == ACTION_TYPE_DECIDE && input.disposition.is_none() {
        return invalid("decide action requires a disposition");
    }
    if let Some(disposition) = &input.disposition {
        validate_disposition(disposition)?;
    }
    if let Some(effect) = &input.effect {
        validate_effect(effect)?;
    }
    validate_attestation(input.model.as_ref(), input.compute.as_ref())?;
    let authored = input.compute.as_ref().is_some_and(|compute| {
        !compute.agent_input_digest.is_empty() || !compute.agent_output_digest.is_empty()
    });
    if binding.is_some() && authored {
        return invalid(
            "carried or composed binding must not include agent input or output digest",
        );
    }
    if let Some(chain) = &input.chain {
        if !is_hex64(&chain.parent_capsule_id) {
            return invalid("chain parent capsule id must be 64 lowercase hex characters");
        }
        if chain.relation.is_empty() {
            return invalid("chain relation is required");
        }
        if chain.relation == CHAIN_EPOCH_OPENS && input.epoch_id.is_empty() {
            return invalid("epoch_opens chain requires epoch id");
        }
    }
    validate_provenance_mode(input.provenance_mode.as_ref())
}

/// §5.3(bis): `backfilled` requires the four companions and permits a time
/// rung; `contemporaneous` permits none of them.
fn validate_provenance_mode(mode: Option<&ProvenanceMode>) -> Result<(), ProduceError> {
    let Some(mode) = mode else { return Ok(()) };
    if mode.mode != PROVENANCE_MODE_CONTEMPORANEOUS && mode.mode != PROVENANCE_MODE_BACKFILLED {
        return invalid(format!(
            "provenance mode must be {PROVENANCE_MODE_CONTEMPORANEOUS:?} or {PROVENANCE_MODE_BACKFILLED:?}"
        ));
    }
    if mode.source_ref.as_ref().is_some_and(|source| {
        !source.citation_purpose.is_empty() || source.log_coordinates.is_some()
    }) {
        return invalid(
            "provenance mode source ref must not carry citation purpose or log coordinates",
        );
    }
    let companions = mode.source_ref.is_some()
        || !mode.source_asserted_at.is_empty()
        || !mode.import_batch.is_empty()
        || !mode.imported_at.is_empty();
    if mode.mode == PROVENANCE_MODE_BACKFILLED {
        let mut missing = Vec::new();
        match &mode.source_ref {
            None => missing.push("source ref"),
            Some(source)
                if source.reference_type.trim().is_empty()
                    || source.digest_alg.trim().is_empty()
                    || source.digest.trim().is_empty() =>
            {
                return invalid("provenance mode source ref requires type, digest alg, and digest");
            }
            Some(_) => {}
        }
        for (name, value) in [
            ("source asserted at", &mode.source_asserted_at),
            ("import batch", &mode.import_batch),
            ("imported at", &mode.imported_at),
        ] {
            if value.trim().is_empty() {
                missing.push(name);
            }
        }
        if !missing.is_empty() {
            return invalid(format!(
                "backfilled provenance mode requires {}",
                missing.join(", ")
            ));
        }
    } else if companions || !mode.time_rung.is_empty() {
        return invalid(format!(
            "provenance mode source ref, source asserted at, import batch, imported at, and time rung are meaningful only when mode is {PROVENANCE_MODE_BACKFILLED:?}"
        ));
    }
    if !mode.time_rung.is_empty()
        && mode.time_rung != TIME_RUNG_SELF_ATTESTED
        && mode.time_rung != TIME_RUNG_WITNESSED
    {
        return invalid(format!(
            "provenance mode time rung must be {TIME_RUNG_SELF_ATTESTED:?} or {TIME_RUNG_WITNESSED:?}"
        ));
    }
    Ok(())
}

fn validate_attestation(
    model: Option<&Model>,
    compute: Option<&ComputeAttestation>,
) -> Result<(), ProduceError> {
    if let Some(model) = model {
        if model.provider.trim().is_empty() && model.model_id.trim().is_empty() {
            return invalid("model must include provider or model id");
        }
        if !model.provider.is_empty() && model.provider.trim().is_empty() {
            return invalid("model provider must be non-empty when present");
        }
        if !model.model_id.is_empty() && model.model_id.trim().is_empty() {
            return invalid("model id must be non-empty when present");
        }
    }
    let Some(compute) = compute else {
        return Ok(());
    };
    if !compute.runtime.is_empty() && compute.runtime.trim().is_empty() {
        return invalid("compute attestation runtime must be non-empty when present");
    }
    if compute.agent_input_digest.is_empty()
        && compute.agent_output_digest.is_empty()
        && compute.runtime.is_empty()
    {
        return invalid("compute attestation must include a digest or runtime");
    }
    for (name, digest) in [
        ("agent input", &compute.agent_input_digest),
        ("agent output", &compute.agent_output_digest),
    ] {
        if !digest.is_empty() && !is_hex64(digest) {
            return invalid(format!(
                "compute attestation {name} digest must be 64 lowercase hex characters"
            ));
        }
    }
    Ok(())
}

fn validate_disposition(disposition: &Disposition) -> Result<(), ProduceError> {
    if disposition.decision.is_empty() {
        return invalid("disposition decision is required");
    }
    if ![APPROVER_HUMAN, APPROVER_POLICY, APPROVER_COUNTERPARTY]
        .contains(&disposition.approver.as_str())
    {
        return invalid(format!(
            "disposition approver must be {APPROVER_HUMAN:?}, {APPROVER_POLICY:?}, or {APPROVER_COUNTERPARTY:?}"
        ));
    }
    if disposition.human_disposed && disposition.approver != APPROVER_HUMAN {
        return invalid("human-disposed decision requires human approver");
    }
    if !disposition.reason_digest.is_empty() && !is_hex64(&disposition.reason_digest) {
        return invalid("disposition reason digest must be 64 lowercase hex characters");
    }
    Ok(())
}

/// AAC's effect-status matrix: which digests and attestation each status
/// requires or forbids.
fn validate_effect(effect: &Effect) -> Result<(), ProduceError> {
    if effect.effect_type.trim().is_empty() {
        return invalid("effect type is required");
    }
    let status = effect.status.as_str();
    if ![
        EFFECT_PLANNED,
        EFFECT_DISPATCHED,
        EFFECT_CONFIRMED,
        EFFECT_FAILED,
        EFFECT_REVERTED,
    ]
    .contains(&status)
    {
        return invalid(format!("unsupported effect status {status:?}"));
    }
    if effect.irreversibility_class.is_empty() {
        return invalid("effect irreversibility class is required");
    }
    for (name, digest) in [
        ("request", &effect.request_digest),
        ("response", &effect.response_digest),
    ] {
        if !digest.is_empty() && !is_hex64(digest) {
            return invalid(format!(
                "effect {name} digest must be 64 lowercase hex characters"
            ));
        }
    }
    let attested = !effect.effect_attestation.is_empty();
    match status {
        EFFECT_PLANNED
            if !effect.request_digest.is_empty() || !effect.response_digest.is_empty() =>
        {
            invalid("planned effect must not carry request or response digests")
        }
        EFFECT_PLANNED if attested => invalid("planned effect must not carry effect attestation"),
        EFFECT_DISPATCHED if !effect.response_digest.is_empty() => {
            invalid("dispatched effect must not carry a response digest")
        }
        EFFECT_DISPATCHED if !attested => invalid("dispatched effect requires effect attestation"),
        EFFECT_CONFIRMED if effect.response_digest.is_empty() => {
            invalid("confirmed effect requires a response digest")
        }
        EFFECT_CONFIRMED if !attested => invalid("confirmed effect requires effect attestation"),
        EFFECT_FAILED | EFFECT_REVERTED if !attested => {
            invalid(format!("{status} effect requires effect attestation"))
        }
        _ => Ok(()),
    }
}

/// The top-level member names the base profile defines, including the
/// local-only envelope fields. An extension may not use them.
const BASE_MEMBERS: [&str; 24] = [
    "spec_version",
    "format_version",
    "canonicalization_id",
    "capsule_id",
    "action_id",
    "action_type",
    "operator",
    "developer",
    "timestamp",
    "epoch_id",
    "domain",
    "provenance",
    "provenance_mode",
    "disposition",
    "effect",
    "chain",
    "assurance",
    "cross_party",
    "model_attestation",
    "references",
    "constraints",
    "compliance",
    "signature",
    "key_id",
];

fn is_extension_name(name: &str) -> bool {
    let mut bytes = name.bytes();
    bytes.next().is_some_and(|first| first.is_ascii_lowercase())
        && bytes.all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
}

/// Validate one extension against the payload built so far and return its
/// decoded value.
fn extension_value(
    extension: &Extension,
    payload: &Map<String, Value>,
) -> Result<Value, ProduceError> {
    let name = &extension.name;
    if !is_extension_name(name) {
        return invalid(format!(
            "extension name {name:?} must be lowercase letters, digits and underscores, starting with a letter"
        ));
    }
    if BASE_MEMBERS.contains(&name.as_str()) {
        return invalid(format!("extension {name:?} is a base-profile member"));
    }
    if payload.contains_key(name) {
        return invalid(format!("extension {name:?} is given more than once"));
    }
    if extension.value.is_empty() {
        return invalid(format!("extension {name:?} has no value"));
    }
    let value = super::decode_strict_json(&extension.value)
        .map_err(|e| ProduceError::invalid(format!("extension {name:?}: {e}")))?;
    if value.is_null() {
        return invalid(format!("extension {name:?} must not be null"));
    }
    check_extension_numbers(&value, &format!("$.{name}"))?;
    Ok(value)
}

/// Refuse a float or an integer outside the interoperable range anywhere in
/// an extension value (base profile §5.1).
fn check_extension_numbers(value: &Value, path: &str) -> Result<(), ProduceError> {
    match value {
        Value::Number(number) if canonical::is_float(number) => invalid(format!(
            "float at {path}: floating-point values are forbidden in a Capsule"
        )),
        Value::Number(number) if canonical::is_unsafe_int(number) => invalid(format!(
            "integer at {path} is outside the interoperable range"
        )),
        Value::Object(members) => {
            for (key, item) in members {
                check_extension_numbers(item, &format!("{path}.{key}"))?;
            }
            Ok(())
        }
        Value::Array(items) => {
            for (index, item) in items.iter().enumerate() {
                check_extension_numbers(item, &format!("{path}[{index}]"))?;
            }
            Ok(())
        }
        _ => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    #[test]
    fn timestamps_truncate_to_microseconds() {
        let whole = Utc.with_ymd_and_hms(2026, 9, 1, 12, 34, 56).unwrap();
        assert_eq!(format_timestamp(&whole), "2026-09-01T12:34:56Z");
        let sub_micro = whole + chrono::Duration::nanoseconds(999);
        assert_eq!(format_timestamp(&sub_micro), "2026-09-01T12:34:56Z");
        let micro = whole + chrono::Duration::nanoseconds(1_999);
        assert_eq!(format_timestamp(&micro), "2026-09-01T12:34:56.000001Z");
    }

    #[test]
    fn extension_names_follow_the_pattern() {
        assert!(is_extension_name("a"));
        assert!(is_extension_name("settlement_2"));
        for bad in ["", "_a", "1a", "A", "a-b", "é"] {
            assert!(!is_extension_name(bad), "{bad}");
        }
    }

    #[test]
    fn extension_numbers_are_checked_in_order() {
        let value =
            agent_action_capsule::verify::decode_capsule_json(br#"{"a":[1,{"b":2e1}],"c":1.5}"#)
                .unwrap();
        let err = check_extension_numbers(&value, "$.x").unwrap_err();
        assert_eq!(
            err.to_string(),
            "float at $.x.a[1].b: floating-point values are forbidden in a Capsule"
        );
    }
}
