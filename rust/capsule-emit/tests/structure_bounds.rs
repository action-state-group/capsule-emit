//! `structure::check_structure` does bounded work on hostile input: a wide
//! array under a long key (the shape that makes a naive path-building walk
//! allocate siblings × path length) is checked in memory proportional to the
//! input, measured here by a counting allocator. This file is its own test
//! binary so the allocator counts nothing else.

use std::alloc::{GlobalAlloc, Layout, System};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::{Duration, Instant};

use capsule_emit::structure::check_structure;
use serde_json::{json, Value};

struct Counting;

static LIVE: AtomicUsize = AtomicUsize::new(0);
static PEAK: AtomicUsize = AtomicUsize::new(0);

unsafe impl GlobalAlloc for Counting {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        let ptr = unsafe { System.alloc(layout) };
        if !ptr.is_null() {
            let live = LIVE.fetch_add(layout.size(), Ordering::Relaxed) + layout.size();
            PEAK.fetch_max(live, Ordering::Relaxed);
        }
        ptr
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        unsafe { System.dealloc(ptr, layout) };
        LIVE.fetch_sub(layout.size(), Ordering::Relaxed);
    }
}

#[global_allocator]
static ALLOCATOR: Counting = Counting;

/// The counters are process-wide: one measurement at a time.
static MEASURING: std::sync::Mutex<()> = std::sync::Mutex::new(());

/// Peak bytes allocated while `f` runs, above what was live before it.
fn peak_during(f: impl FnOnce()) -> usize {
    let _one_at_a_time = MEASURING
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let before = LIVE.load(Ordering::Relaxed);
    PEAK.store(before, Ordering::Relaxed);
    f();
    PEAK.load(Ordering::Relaxed).saturating_sub(before)
}

/// What one in-memory copy of `record` costs: the unit the bound is in.
fn one_copy(record: &Value) -> usize {
    peak_during(|| drop(record.clone()))
}

/// A well-formed record, with `member` added under a 500 KB key: 250,000
/// numbers in one array, a little over 1 MB of JSON in all.
fn wide_record(member: Value) -> Value {
    let key = "k".repeat(500_000);
    let mut record = json!({
        "spec_version": "draft-mih-scitt-agent-action-capsule-05",
        "format_version": "4",
        "canonicalization_id": "jcs",
        "action_id": "wide",
        "action_type": "fyi",
        "operator": "op",
        "developer": "dev@v1",
        "timestamp": "2026-09-29T00:00:00Z",
    });
    record["model_attestation"] = json!({ key: member });
    record["capsule_id"] =
        json!(capsule_emit::jcs::compute_capsule_id(&record).unwrap_or_default());
    record
}

#[test]
fn a_wide_array_under_a_long_key_is_checked_in_bounded_memory_and_time() {
    let record = wide_record(Value::Array(vec![json!(0); 250_000]));
    let input_bytes = record.to_string().len();
    assert!(input_bytes > 1_000_000, "{input_bytes} bytes of input");

    let copy = one_copy(&record);
    let started = Instant::now();
    let mut ok = false;
    let peak = peak_during(|| ok = check_structure(&record).ok());
    let took = started.elapsed();

    assert!(ok, "nothing in it fails a check");
    // The identity check re-canonicalizes the record (a copy of it, and its
    // canonical text); the walk itself adds one entry per level of nesting.
    // Before the fix this shape needed ~125 GB.
    assert!(
        peak < 3 * copy,
        "peak {peak} bytes; one copy of the record is {copy}"
    );
    assert!(took < Duration::from_secs(2), "took {took:?}");
}

#[test]
fn failing_numbers_under_a_long_key_cost_only_clipped_paths() {
    let record = wide_record(Value::Array(vec![json!(0.5); 250_000]));
    let copy = one_copy(&record);
    let mut findings = Vec::new();
    let peak = peak_during(|| findings = check_structure(&record).findings);
    assert!(!findings.is_empty());
    assert!(findings.iter().all(|f| f.detail.chars().count() < 300));
    assert!(
        peak < 3 * copy,
        "peak {peak} bytes; one copy of the record is {copy}"
    );
}
