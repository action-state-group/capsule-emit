//! UTC RFC3339 timestamp helpers, matching the shape of `emit.py`'s
//! `_utc_now()` (`now.isoformat().replace("+00:00", "Z")`) closely enough for
//! producer-side event records (key rotation, ledger bookkeeping) -- not
//! itself part of any cross-language byte-conformance surface.
//!
//! **Committed times are minute-granular (Evidence Layer -00 §12.1, "What a
//! Checkpoint Reveals").** A time inside signed or committed bytes -- a
//! record's `timestamp`, a citing record's `received_at`, a checkpoint's
//! `timestamp` -- leaves this node whenever the record is disclosed or the
//! checkpoint is pushed or witnessed, and fine-grained times reveal the
//! node's activity pattern. It is coarsened HERE, when the bytes are
//! produced: coarsening a copy afterwards would break the signature or the
//! record's inclusion. Use [`utc_now_minute`] for every such value;
//! [`utc_now_iso8601`] is for local state that is neither committed nor
//! disclosed.
//!
//! **Committed times carry no fraction (`2026-09-27T17:08:00Z`).** A
//! checkpoint's time is its COSE statement's `issued_at`, and verifiers that
//! hold RFC 3339 times to one normalized form (the TypeScript CLL verifier:
//! no trailing-zero fraction) refuse `…:00.000Z`. Whole seconds with no
//! fraction is that form, and every RFC 3339 parser still reads it.

use chrono::{DateTime, Timelike, Utc};

/// The current time at full (millisecond) precision -- local state only.
pub fn utc_now_iso8601() -> String {
    format_millis(Utc::now())
}

/// The current time truncated to the minute, for any time a sealed record or
/// a checkpoint commits to: whole seconds, no fraction
/// (`2026-09-27T17:08:00Z`).
pub fn utc_now_minute() -> String {
    format_seconds(truncate_to_minute(Utc::now()))
}

/// `raw` (an RFC3339 time) truncated to the minute, in the same shape as
/// [`utc_now_minute`]. `None` when `raw` does not parse -- the caller decides
/// what an unparseable time means; it is never silently replaced.
pub fn coarsen_to_minute(raw: &str) -> Option<String> {
    let parsed = DateTime::parse_from_rfc3339(raw).ok()?.with_timezone(&Utc);
    Some(format_seconds(truncate_to_minute(parsed)))
}

/// True when `raw` is an RFC3339 time with zero seconds and sub-seconds.
pub fn is_minute_granular(raw: &str) -> bool {
    DateTime::parse_from_rfc3339(raw).is_ok_and(|t| t.second() == 0 && t.nanosecond() == 0)
}

fn truncate_to_minute(t: DateTime<Utc>) -> DateTime<Utc> {
    t.with_second(0)
        .and_then(|t| t.with_nanosecond(0))
        .expect("zero seconds and nanoseconds are always valid")
}

fn format_millis(t: DateTime<Utc>) -> String {
    t.to_rfc3339_opts(chrono::SecondsFormat::Millis, true)
}

fn format_seconds(t: DateTime<Utc>) -> String {
    t.to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn now_minute_has_no_seconds() {
        let t = utc_now_minute();
        assert!(t.ends_with(":00Z") && t.len() == 20, "{t}");
        assert!(is_minute_granular(&t));
    }

    #[test]
    fn coarsen_truncates_and_normalises_offsets() {
        assert_eq!(
            coarsen_to_minute("2026-09-27T17:08:44.123Z").as_deref(),
            Some("2026-09-27T17:08:00Z")
        );
        assert_eq!(
            coarsen_to_minute("2026-09-27T10:08:59.999-07:00").as_deref(),
            Some("2026-09-27T17:08:00Z")
        );
        assert_eq!(coarsen_to_minute("not a time"), None);
    }

    #[test]
    fn granularity_check() {
        assert!(is_minute_granular("2026-08-23T00:00:00Z"));
        assert!(!is_minute_granular("2026-08-23T00:00:01Z"));
        assert!(!is_minute_granular("2026-08-23T00:00:00.001Z"));
        assert!(!is_minute_granular("garbage"));
    }
}
