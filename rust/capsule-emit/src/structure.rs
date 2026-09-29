//! The Class 1 checks a capsule's own bytes must pass (§6), without a store.
//!
//! This is the Rust side of the reference verifier's gating checks
//! (`agent_action_capsule.verify`, v0.6.0), in the reference's order, with
//! its finding codes:
//!
//! 1. **Structural**: the REQUIRED members and their types, `capsule_id`
//!    spelling, `action_type`, the format-4 canonicalization declaration,
//!    block types, no floats and no integers outside the IEEE-754 safe range
//!    anywhere, the disposition's closed `approver` enum, `decision` and
//!    `human_disposed`, and the shape of each `references[]` entry.
//! 2. **Identity**: the `capsule_id` recomputes (only when the format-4
//!    profile is declared correctly).
//! 3. **Confirmed-effect binding**: a `confirmed` effect carries a
//!    `response_digest`.
//! 4. **Verdict/effect orthogonality**: a never-dispatch verdict has no
//!    effect.
//! 5. **Effect-attestation matrix**: `effect_attestation` present exactly
//!    when an effect was dispatched.
//! 6. **Chain block**: a well-formed parent and a `relation`; a reference
//!    must not duplicate the chain parent. (Parent existence needs a store
//!    and is not checked here.)
//! 7. **Assurance**: a stated `effect_mode` stronger than the effect supports.
//! 9. **Provenance mode**: the `provenance_mode` block's rules, including
//!    the time-laundering shape.
//!
//! What is reported: every **error** (each one fails [`StructureReport::ok`])
//! and the one defensive **warning** (`dishonest_human_disposed`). The
//! reference's informational findings (check 8's registry lookups, check 7's
//! unverifiable overclaims, the store-level note) do not gate anything and
//! are not reported.
//!
//! **Bounded work on untrusted input.** The walk for floats and unsafe
//! integers holds one iterator per open array or object, so its memory grows
//! with nesting depth and never with the number of siblings; it writes a path
//! only for a number that fails, keeps at most [`MAX_PATH_FINDINGS`] of each
//! kind (the verdict is decided by the first one), and no finding quotes more
//! than [`MAX_DETAIL_CHARS`] characters of the capsule. Everything else is
//! linear in the input, and nothing here panics.
//!
//! **Depth.** The walk itself is not recursive, but the identity check's
//! canonicalization (and dropping a `serde_json::Value`) is. Pass values
//! parsed by `serde_json::from_slice`/`from_str` with their default nesting
//! limit (128), as every caller in this crate does.

use serde_json::{Map, Value};

use crate::jcs::{compute_capsule_id, JcsError, MAX_SAFE_INTEGER};

/// The REQUIRED top-level members (§5.1), each a string.
pub(crate) const REQUIRED_FIELDS: [&str; 8] = [
    "spec_version",
    "format_version",
    "capsule_id",
    "action_id",
    "action_type",
    "operator",
    "developer",
    "timestamp",
];

/// `verdict_class` values that never dispatch an effect (§5.4.2).
pub(crate) const NEVER_DISPATCH_VERDICT_CLASSES: [&str; 9] = [
    "blocked",
    "hitl_dispatched",
    "denied",
    "engine_failure",
    "deferred",
    "needs_decision",
    "expired",
    "escalated",
    "resolved",
];

/// The closed `disposition.approver` enum (§5.4).
pub(crate) const VALID_APPROVERS: [&str; 3] = ["human", "policy", "counterparty"];

/// `provenance_mode.mode` and `time_rung` values (§5.3(bis)).
pub(crate) const PROVENANCE_MODES: [&str; 2] = ["contemporaneous", "backfilled"];
pub(crate) const TIME_RUNGS: [&str; 2] = ["self_attested", "witnessed"];

/// At most this many float (and, separately, unsafe-integer) findings are
/// reported; one is enough to fail the record.
pub const MAX_PATH_FINDINGS: usize = 64;

/// A finding's detail quotes at most this many characters of any value (or
/// path) taken from the capsule.
pub const MAX_DETAIL_CHARS: usize = 64;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[non_exhaustive]
pub enum Severity {
    /// Fails the record.
    Error,
    /// A defensive flag that does not fail the record.
    Warning,
}

#[derive(Clone, Debug, PartialEq, Eq)]
#[non_exhaustive]
pub struct Finding {
    /// The §6 check the finding belongs to, or `None` for the defensive
    /// warning, which is not one of the enumerated checks.
    pub check: Option<u8>,
    /// The reference verifier's stable code for it (`"approver_invalid"`,
    /// …): the same strings its conformance vectors use, so a caller can
    /// compare them directly. An open set: a later check adds codes.
    pub code: &'static str,
    pub severity: Severity,
    pub detail: String,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
#[non_exhaustive]
pub struct StructureReport {
    pub findings: Vec<Finding>,
}

impl StructureReport {
    /// No error: the record passes every check this module runs.
    pub fn ok(&self) -> bool {
        !self.findings.iter().any(|f| f.severity == Severity::Error)
    }

    pub fn errors(&self) -> impl Iterator<Item = &Finding> {
        self.findings
            .iter()
            .filter(|f| f.severity == Severity::Error)
    }
}

/// Run the checks over `capsule`. Never panics.
pub fn check_structure(record: &Value) -> StructureReport {
    let mut out = Findings::default();
    let Some(capsule) = record.as_object() else {
        out.error(1, "not_an_object", "Capsule is not a JSON object".into());
        return out.done();
    };

    let effect = object(capsule, "effect");
    let disposition = object(capsule, "disposition");
    let chain = object(capsule, "chain");
    let references = reference_findings(capsule, chain);

    // ---- Check 1: Structural ----------------------------------------------
    for field in REQUIRED_FIELDS {
        match capsule.get(field) {
            None => out.error(
                1,
                "missing_required_field",
                format!("{field} is REQUIRED (§5.1)"),
            ),
            Some(Value::String(_)) => {}
            Some(_) => out.error(
                1,
                "field_not_string",
                format!("{field} MUST be a string (§5.1)"),
            ),
        }
    }
    let carried_id = capsule
        .get("capsule_id")
        .and_then(Value::as_str)
        .filter(|id| is_hex64(id));
    if capsule.get("capsule_id").is_some_and(|v| !v.is_null()) && carried_id.is_none() {
        out.error(
            1,
            "capsule_id_malformed",
            "capsule_id MUST be 64 lowercase hex (§5.1)".into(),
        );
    }
    if let Some(action_type) = capsule.get("action_type").filter(|v| !v.is_null()) {
        if !matches!(action_type.as_str(), Some("fyi" | "decide")) {
            out.error(
                1,
                "action_type_invalid",
                "action_type MUST be 'fyi' or 'decide' (§5.1)".into(),
            );
        }
    }
    let format_version = capsule.get("format_version");
    match format_version.and_then(Value::as_str) {
        Some("4") => match capsule.get("canonicalization_id") {
            None => out.error(
                1,
                "canonicalization_id_missing",
                "format_version '4' REQUIRES canonicalization_id='jcs' (§5.1)".into(),
            ),
            Some(Value::String(c)) if c == "jcs" => {}
            Some(Value::String(_)) => out.error(
                1,
                "canonicalization_profile_mismatch",
                "format_version '4' REQUIRES canonicalization_id='jcs' (§5.1)".into(),
            ),
            Some(_) => out.error(
                1,
                "canonicalization_id_not_string",
                "canonicalization_id MUST be a string (§5.1)".into(),
            ),
        },
        Some(other) => out.error(
            1,
            "unsupported_format_version",
            format!(
                "format_version {} is not supported; expected \"4\" (§5.1)",
                shown_value(&Value::String(other.to_string()))
            ),
        ),
        None => {}
    }
    for field in [
        "effect",
        "assurance",
        "disposition",
        "chain",
        "cross_party",
        "self_reported_reasoning",
        "provenance_mode",
    ] {
        if capsule.get(field).is_some_and(|v| !v.is_object()) {
            out.error(
                1,
                "block_not_object",
                format!("{field} MUST be a JSON object when present"),
            );
        }
    }
    for (field, code) in [
        ("domain", "domain_not_string"),
        ("provenance", "provenance_not_string"),
    ] {
        if capsule.get(field).is_some_and(|v| !v.is_string()) {
            out.error(
                1,
                code,
                format!("{field} MUST be a string when present (§-02)"),
            );
        }
    }
    if capsule.get("constraints").is_some_and(|v| !v.is_array()) {
        out.error(
            1,
            "constraints_not_array",
            "constraints MUST be an array when present (§8.1)".into(),
        );
    }
    let (float_paths, unsafe_paths) = number_paths(record);
    for path in float_paths {
        out.error(
            1,
            "float_in_digest_field",
            format!("floating-point value at {path}; §5.1 forbids it"),
        );
    }
    for path in unsafe_paths {
        out.error(
            1,
            "unsafe_integer_in_digest_field",
            format!(
                "integer outside the JS-safe range (+/-{MAX_SAFE_INTEGER}) at {path}; large integers MUST be exact \
                 decimal strings"
            ),
        );
    }
    if let Some(disposition) = disposition {
        let approver = disposition.get("approver").filter(|v| !v.is_null());
        match approver {
            None => out.error(
                1,
                "missing_required_field",
                "disposition.approver is REQUIRED (§5.4)".into(),
            ),
            Some(a) if a.as_str().is_some_and(|a| VALID_APPROVERS.contains(&a)) => {}
            Some(a) => out.error(
                1,
                "approver_invalid",
                format!(
                    "disposition.approver MUST be human|policy|counterparty (§5.4); got {}",
                    shown_value(a)
                ),
            ),
        }
        if !disposition.contains_key("decision") {
            out.error(
                1,
                "missing_required_field",
                "disposition.decision is REQUIRED (§5.4)".into(),
            );
        }
        match disposition.get("human_disposed") {
            Some(Value::Bool(true)) if approver.and_then(Value::as_str) != Some("human") => out
                .push(Finding {
                    check: None,
                    code: "dishonest_human_disposed",
                    severity: Severity::Warning,
                    detail: "human_disposed=true with a non-human approver (§5.4)".into(),
                }),
            Some(Value::Bool(_)) => {}
            _ => out.error(
                1,
                "field_not_bool",
                "disposition.human_disposed is REQUIRED and boolean (§5.4)".into(),
            ),
        }
    }
    out.extend(references.iter().filter(|f| f.check == Some(1)).cloned());

    // ---- Check 2: Identity ------------------------------------------------
    let profile_valid = format_version.and_then(Value::as_str) == Some("4")
        && capsule.get("canonicalization_id").and_then(Value::as_str) == Some("jcs");
    if let (Some(carried), true) = (carried_id, profile_valid) {
        match compute_capsule_id(record) {
            Ok(recomputed) if recomputed != carried => out.error(
                2,
                "capsule_id_mismatch",
                format!("recomputed {recomputed} != carried {carried}"),
            ),
            Ok(_) | Err(JcsError::FloatInDigest | JcsError::UnsafeInteger(_)) => {}
            Err(e) => out.error(2, "capsule_id_uncomputable", e.to_string()),
        }
    }

    // ---- Check 3: Confirmed-effect binding --------------------------------
    if let Some(effect) = effect {
        if effect.get("status").and_then(Value::as_str) == Some("confirmed")
            && !effect
                .get("response_digest")
                .and_then(Value::as_str)
                .is_some_and(is_hex64)
        {
            out.error(
                3,
                "confirmed_without_response",
                "effect.status 'confirmed' requires a 64-hex response_digest (§5.2)".into(),
            );
        }
    }
    let effect_mode = derive_effect_mode(effect);

    // ---- Check 4: Verdict/effect orthogonality ----------------------------
    if let Some(verdict) = disposition
        .and_then(|d| d.get("verdict_class"))
        .and_then(Value::as_str)
    {
        if NEVER_DISPATCH_VERDICT_CLASSES.contains(&verdict) && effect_mode != "not_applicable" {
            out.error(
                4,
                "verdict_effect_conflict",
                format!("verdict_class {verdict:?} never dispatches, but derived effect_mode is {effect_mode:?} (§5.4.2)"),
            );
        }
    }

    // ---- Check 5: Effect-attestation matrix -------------------------------
    let attestation = effect
        .and_then(|e| e.get("effect_attestation"))
        .filter(|v| !v.is_null());
    if effect_mode == "not_applicable" {
        if attestation.is_some() {
            out.error(
                5,
                "effect_attestation_present",
                "effect_attestation MUST be absent for effect_mode 'not_applicable' (§5.2)".into(),
            );
        }
    } else if attestation.is_none() {
        out.error(
            5,
            "effect_attestation_missing",
            format!("effect_attestation REQUIRED for effect_mode {effect_mode:?} (§5.2)"),
        );
    }

    // ---- Check 6: Chain block (store-free part) ---------------------------
    if let Some(chain) = chain {
        if !chain
            .get("parent_capsule_id")
            .and_then(Value::as_str)
            .is_some_and(is_hex64)
        {
            out.error(
                6,
                "chain_parent_malformed",
                "chain.parent_capsule_id MUST be a 64-hex capsule_id (§5.4.4)".into(),
            );
        }
        if !chain.contains_key("relation") {
            out.error(
                6,
                "missing_required_field",
                "chain.relation is REQUIRED when a chain block is present (§5.4.4)".into(),
            );
        }
    }
    out.extend(references.iter().filter(|f| f.check == Some(6)).cloned());

    // ---- Check 7: Assurance (the one gating overclaim) ---------------------
    if let Some(stated) = object(capsule, "assurance")
        .and_then(|a| a.get("effect_mode"))
        .and_then(Value::as_str)
    {
        let rank = |mode: &str| match mode {
            "confirmed" => Some(1),
            "not_applicable" | "dispatched_unconfirmed" => Some(0),
            _ => None,
        };
        if rank(stated).is_some_and(|s| s > rank(effect_mode).unwrap_or(0)) {
            out.error(
                7,
                "assurance_overclaim",
                format!(
                    "claimed effect_mode {stated:?} but verifier derived {effect_mode:?} (§5.3)"
                ),
            );
        }
    }

    // ---- Check 9: Provenance mode -----------------------------------------
    if let Some(pm) = object(capsule, "provenance_mode") {
        provenance_mode(capsule, pm, &mut out);
    }

    out.done()
}

/// `effect_mode` as the effect supports it (§5.2).
pub(crate) fn derive_effect_mode(effect: Option<&Map<String, Value>>) -> &'static str {
    let Some(effect) = effect else {
        return "not_applicable";
    };
    match effect.get("status").and_then(Value::as_str) {
        Some("planned") => "not_applicable",
        Some("confirmed")
            if effect
                .get("response_digest")
                .and_then(Value::as_str)
                .is_some_and(is_hex64) =>
        {
            "confirmed"
        }
        _ => "dispatched_unconfirmed",
    }
}

fn is_hex64(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn non_empty_string(value: Option<&Value>) -> bool {
    value.and_then(Value::as_str).is_some_and(|s| !s.is_empty())
}

fn object<'a>(capsule: &'a Map<String, Value>, key: &str) -> Option<&'a Map<String, Value>> {
    capsule.get(key).and_then(Value::as_object)
}

fn unsafe_integer(n: &serde_json::Number) -> bool {
    match (n.as_i64(), n.as_u64()) {
        (Some(i), _) => !(-MAX_SAFE_INTEGER..=MAX_SAFE_INTEGER).contains(&i),
        (None, Some(u)) => u > MAX_SAFE_INTEGER as u64,
        (None, None) => false,
    }
}

/// Where the walk stands in one open array or object: its remaining
/// children, and the key or index of the child being visited.
enum Level<'a> {
    Object(serde_json::map::Iter<'a>, Option<&'a str>),
    Array(
        std::iter::Enumerate<std::slice::Iter<'a, Value>>,
        Option<usize>,
    ),
}

/// The path to the child being visited (`a.b[2].c`; `<root>` for the root),
/// at most [`MAX_DETAIL_CHARS`] characters.
fn current_path(levels: &[Level<'_>]) -> String {
    // Written at most one character past the limit, so a key of any length
    // costs only what is kept; `clip` then marks the cut.
    let budget = MAX_DETAIL_CHARS + 1;
    let mut path = String::new();
    for level in levels {
        match level {
            Level::Object(_, Some(key)) if path.is_empty() => push_limited(&mut path, key, budget),
            Level::Object(_, Some(key)) => {
                push_limited(&mut path, ".", budget);
                push_limited(&mut path, key, budget);
            }
            Level::Array(_, Some(index)) => push_limited(&mut path, &format!("[{index}]"), budget),
            _ => {}
        }
        if path.chars().count() >= budget {
            break;
        }
    }
    if path.is_empty() {
        path.push_str("<root>");
    }
    clip(&path)
}

/// Append as much of `text` as keeps `path` within `budget` characters.
fn push_limited(path: &mut String, text: &str, budget: usize) {
    let room = budget.saturating_sub(path.chars().count());
    path.extend(text.chars().take(room));
}

/// The paths of every float and of every integer outside the safe range, in
/// the reference's depth-first order, at most [`MAX_PATH_FINDINGS`] of each.
///
/// One pass, holding one iterator per open array or object: memory grows
/// with nesting depth, never with how many siblings there are, and a path is
/// written only for a number that fails.
fn number_paths(root: &Value) -> (Vec<String>, Vec<String>) {
    fn judge(
        n: &serde_json::Number,
        levels: &[Level<'_>],
        floats: &mut Vec<String>,
        unsafe_ints: &mut Vec<String>,
    ) {
        if !n.is_i64() && !n.is_u64() {
            if floats.len() < MAX_PATH_FINDINGS {
                floats.push(current_path(levels));
            }
        } else if unsafe_integer(n) && unsafe_ints.len() < MAX_PATH_FINDINGS {
            unsafe_ints.push(current_path(levels));
        }
    }
    let mut floats = Vec::new();
    let mut unsafe_ints = Vec::new();
    let mut levels: Vec<Level<'_>> = Vec::new();
    match root {
        Value::Number(n) => judge(n, &levels, &mut floats, &mut unsafe_ints),
        Value::Object(map) => levels.push(Level::Object(map.iter(), None)),
        Value::Array(items) => levels.push(Level::Array(items.iter().enumerate(), None)),
        _ => {}
    }
    while let Some(top) = levels.last_mut() {
        let child = match top {
            Level::Object(children, at) => children.next().map(|(k, v)| {
                *at = Some(k.as_str());
                v
            }),
            Level::Array(children, at) => children.next().map(|(i, v)| {
                *at = Some(i);
                v
            }),
        };
        match child {
            None => {
                levels.pop();
            }
            Some(Value::Number(n)) => judge(n, &levels, &mut floats, &mut unsafe_ints),
            Some(Value::Object(map)) => levels.push(Level::Object(map.iter(), None)),
            Some(Value::Array(items)) => levels.push(Level::Array(items.iter().enumerate(), None)),
            Some(_) => {}
        }
        if floats.len() == MAX_PATH_FINDINGS && unsafe_ints.len() == MAX_PATH_FINDINGS {
            break;
        }
    }
    (floats, unsafe_ints)
}

/// At most [`MAX_DETAIL_CHARS`] characters of `text`, with `…` when cut:
/// a finding's detail never echoes a caller's value at full length.
fn clip(text: &str) -> String {
    match text.char_indices().nth(MAX_DETAIL_CHARS) {
        Some((at, _)) => format!("{}…", &text[..at]),
        None => text.to_string(),
    }
}

/// A caller's JSON value, for a finding's detail: clipped.
fn shown_value(value: &Value) -> String {
    clip(&value.to_string())
}

/// `references[]` (§5.5.5), over the raw bytes, without resolving anything:
/// the findings for checks 1 and 6.
fn reference_findings(
    capsule: &Map<String, Value>,
    chain: Option<&Map<String, Value>>,
) -> Vec<Finding> {
    let mut out = Findings::default();
    if capsule.get("format_version").and_then(Value::as_str) != Some("4") {
        return Vec::new();
    }
    let Some(raw) = capsule.get("references") else {
        return Vec::new();
    };
    let Some(references) = raw.as_array() else {
        out.error(
            1,
            "references_malformed",
            "references MUST be an array (§5.5.5)".into(),
        );
        return out.done().findings;
    };
    let parent = chain
        .and_then(|c| c.get("parent_capsule_id"))
        .and_then(Value::as_str)
        .filter(|p| !p.is_empty());
    for (i, reference) in references.iter().enumerate() {
        let path = format!("references[{i}]");
        let Some(reference) = reference.as_object() else {
            out.error(
                1,
                "reference_malformed",
                format!("{path} MUST be an object (§5.5.5)"),
            );
            continue;
        };
        for field in ["type", "digest_alg", "digest"] {
            if !non_empty_string(reference.get(field)) {
                out.error(
                    1,
                    "reference_malformed",
                    format!("{path}.{field} MUST be a non-empty string (§5.5.5)"),
                );
            }
        }
        let is_capsule_ref = reference.get("type").and_then(Value::as_str)
            == Some("agent-action-capsule")
            && reference.get("digest_alg").and_then(Value::as_str) == Some("SHA-256");
        if is_capsule_ref {
            let digest = reference
                .get("digest")
                .and_then(Value::as_str)
                .filter(|d| !d.is_empty());
            if digest.is_some_and(|d| !is_hex64(d)) {
                out.error(
                    1,
                    "reference_malformed",
                    format!("{path}.digest MUST be an AAC Capsule ID for agent-action-capsule/SHA-256 (§5.5.5)"),
                );
            }
            if parent.is_some() && digest == parent {
                out.error(
                    6,
                    "reference_duplicates_chain_parent",
                    format!("{path} duplicates chain.parent_capsule_id (§5.5.5)"),
                );
            }
        }
        if reference.contains_key("citation_purpose")
            && !non_empty_string(reference.get("citation_purpose"))
        {
            out.error(
                1,
                "reference_malformed",
                format!("{path}.citation_purpose MUST be a non-empty string (§5.5.5)"),
            );
        }
        if let Some(coordinates) = reference.get("log_coordinates") {
            let Some(coordinates) = coordinates.as_object() else {
                out.error(
                    1,
                    "reference_log_coordinates_malformed",
                    format!("{path}.log_coordinates MUST be an object (§5.5.5)"),
                );
                continue;
            };
            for field in ["log_id", "leaf_index", "inclusion_proof"] {
                if coordinates.get(field).is_none_or(Value::is_null) {
                    out.error(
                        1,
                        "reference_log_coordinates_malformed",
                        format!("{path}.log_coordinates requires {field} (§5.5.5)"),
                    );
                }
            }
        }
    }
    out.done().findings
}

/// Check 9 (§5.3(bis)): a backfilled record's claims, and orphaned fields on
/// a contemporaneous one.
fn provenance_mode(capsule: &Map<String, Value>, pm: &Map<String, Value>, out: &mut Findings) {
    let mode = pm
        .get("mode")
        .and_then(Value::as_str)
        .filter(|m| PROVENANCE_MODES.contains(m));
    if mode.is_none() {
        out.error(
            9,
            "provenance_mode_invalid",
            format!(
                "provenance_mode.mode MUST be one of {PROVENANCE_MODES:?} (§5.3(bis)); got {}",
                shown(pm.get("mode"))
            ),
        );
    }
    match mode {
        Some("backfilled") => {
            for field in [
                "source_ref",
                "source_asserted_at",
                "import_batch",
                "imported_at",
            ] {
                let missing = match pm.get(field) {
                    None | Some(Value::Null) => true,
                    Some(Value::String(s)) => s.is_empty(),
                    Some(_) => false,
                };
                if missing {
                    out.error(
                        9,
                        "provenance_mode_missing_required_field",
                        format!("provenance_mode.{field} is REQUIRED when mode='backfilled' (§5.3(bis))"),
                    );
                }
            }
            match pm.get("source_ref") {
                Some(Value::Object(source_ref)) => {
                    for field in ["type", "digest_alg", "digest"] {
                        if !non_empty_string(source_ref.get(field)) {
                            out.error(
                                9,
                                "provenance_mode_source_ref_malformed",
                                format!("provenance_mode.source_ref.{field} MUST be a non-empty string (§5.3(bis))"),
                            );
                        }
                    }
                }
                Some(_) => out.error(
                    9,
                    "provenance_mode_source_ref_malformed",
                    "provenance_mode.source_ref MUST be a JSON object when present (§5.3(bis))"
                        .into(),
                ),
                None => {}
            }
            if let (Some(Value::String(imported)), Some(Value::String(asserted))) =
                (pm.get("imported_at"), pm.get("source_asserted_at"))
            {
                if imported == asserted {
                    out.error(
                        9,
                        "provenance_time_laundering_shape",
                        "provenance_mode.imported_at equals source_asserted_at on a backfilled record (§5.3(bis))".into(),
                    );
                }
            }
            let mut time_rung = pm.get("time_rung").filter(|v| !v.is_null());
            if let Some(rung) = time_rung {
                if !rung.as_str().is_some_and(|r| TIME_RUNGS.contains(&r)) {
                    out.error(
                        9,
                        "provenance_mode_invalid",
                        format!("provenance_mode.time_rung MUST be one of {TIME_RUNGS:?} (§5.3(bis)); got {}", shown_value(rung)),
                    );
                    time_rung = None;
                }
            }
            let corroborated = capsule
                .get("references")
                .and_then(Value::as_array)
                .is_some_and(|refs| {
                    refs.iter().any(|r| {
                        r.get("citation_purpose").and_then(Value::as_str)
                            == Some("corroborates_source_time")
                            && ["type", "digest_alg", "digest"]
                                .iter()
                                .all(|k| non_empty_string(r.get(*k)))
                    })
                });
            if time_rung.and_then(Value::as_str) == Some("witnessed") && !corroborated {
                out.error(
                    9,
                    "provenance_time_rung_overclaim",
                    "provenance_mode.time_rung='witnessed' claimed without a references[] entry citing \
                     'corroborates_source_time' (§5.3(bis))"
                        .into(),
                );
            }
        }
        Some(_) => {
            let orphaned: Vec<&str> = [
                "source_ref",
                "source_asserted_at",
                "import_batch",
                "imported_at",
                "time_rung",
            ]
            .into_iter()
            .filter(|f| pm.get(*f).is_some_and(|v| !v.is_null()))
            .collect();
            if !orphaned.is_empty() {
                out.error(
                    9,
                    "provenance_mode_invalid",
                    format!("provenance_mode fields {orphaned:?} are meaningful only when mode='backfilled' (§5.3(bis))"),
                );
            }
        }
        None => {}
    }
}

fn shown(value: Option<&Value>) -> String {
    value.map_or_else(|| "nothing".to_string(), shown_value)
}

#[derive(Default)]
struct Findings(Vec<Finding>);

impl Findings {
    fn error(&mut self, check: u8, code: &'static str, detail: String) {
        self.0.push(Finding {
            check: Some(check),
            code,
            severity: Severity::Error,
            detail,
        });
    }

    fn push(&mut self, finding: Finding) {
        self.0.push(finding);
    }

    fn extend(&mut self, findings: impl IntoIterator<Item = Finding>) {
        self.0.extend(findings);
    }

    fn done(self) -> StructureReport {
        StructureReport { findings: self.0 }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn deep_nesting_and_many_floats_are_walked_without_recursion_and_capped() {
        let mut deep = json!(1.5);
        for _ in 0..100_000 {
            // Moved, not serialized: `json!` would recurse through it.
            deep = Value::Array(vec![deep]);
        }
        let (floats, unsafe_ints) = number_paths(&deep);
        assert_eq!(floats.len(), 1);
        assert!(unsafe_ints.is_empty());
        assert!(
            floats[0].chars().count() <= MAX_DETAIL_CHARS + 1,
            "the path is clipped"
        );
        // Dropping a 100k-deep value is itself recursive in serde_json; leak it.
        std::mem::forget(deep);
        let many = json!({"a": vec![json!(0.5); 10_000], "b": vec![json!(1u64 << 60); 10_000]});
        let (floats, unsafe_ints) = number_paths(&many);
        assert_eq!(
            (floats.len(), unsafe_ints.len()),
            (MAX_PATH_FINDINGS, MAX_PATH_FINDINGS)
        );
        assert_eq!(floats[0], "a[0]");
        assert_eq!(unsafe_ints[1], "b[1]");
    }

    #[test]
    fn paths_are_spelled_as_the_reference_spells_them() {
        let value = json!({"x": {"y": [1, {"z": 0.5}]}, "w": [[2.5]]});
        assert_eq!(number_paths(&value).0, ["x.y[1].z", "w[0][0]"]);
        assert_eq!(number_paths(&json!(0.5)).0, ["<root>"]);
        assert_eq!(number_paths(&json!([0.5])).0, ["[0]"]);
    }

    #[test]
    fn details_never_quote_a_caller_value_at_full_length() {
        let long = "x".repeat(10_000);
        let report = check_structure(
            &json!({"format_version": long, "disposition": {"approver": long, "decision": "d", "human_disposed": false}}),
        );
        for finding in &report.findings {
            assert!(
                finding.detail.chars().count() < 300,
                "{}: {} chars",
                finding.code,
                finding.detail.len()
            );
        }
        assert_eq!(clip("abc"), "abc");
        assert_eq!(clip(&"é".repeat(70)).chars().count(), MAX_DETAIL_CHARS + 1);
    }

    #[test]
    fn anything_that_is_not_an_object_fails_without_panicking() {
        for value in [json!(null), json!([]), json!("x"), json!(1), json!(true)] {
            let report = check_structure(&value);
            assert!(!report.ok());
            assert_eq!(report.findings[0].code, "not_an_object");
        }
    }
}
