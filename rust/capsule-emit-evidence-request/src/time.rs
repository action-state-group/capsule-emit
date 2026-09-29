//! RFC 3339 UTC times, the form the vectors use for `issued_at`, `deadline`,
//! `until` and a time-valued `min_freshness` (ambiguities A3, A5).

/// A UTC instant, as seconds since the Unix epoch plus nanoseconds.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct UtcTime {
    pub seconds: i64,
    pub nanos: u32,
}

/// Parse `YYYY-MM-DDTHH:MM:SS[.fraction]Z` (upper-case `T` and `Z`, UTC
/// only). Anything else is `None`.
pub fn parse_utc(s: &str) -> Option<UtcTime> {
    let b = s.as_bytes();
    if b.len() < 20
        || b[4] != b'-'
        || b[7] != b'-'
        || b[10] != b'T'
        || b[13] != b':'
        || b[16] != b':'
    {
        return None;
    }
    let num = |range: std::ops::Range<usize>| -> Option<u32> {
        let part = s.get(range)?;
        if part.bytes().all(|c| c.is_ascii_digit()) {
            part.parse().ok()
        } else {
            None
        }
    };
    let (year, month, day) = (num(0..4)?, num(5..7)?, num(8..10)?);
    let (hour, minute, second) = (num(11..13)?, num(14..16)?, num(17..19)?);
    let rest = &s[19..];
    let (fraction, zone) = match rest.strip_prefix('.') {
        Some(after) => {
            let digits = after.bytes().take_while(u8::is_ascii_digit).count();
            if digits == 0 || digits > 9 {
                return None;
            }
            (&after[..digits], &after[digits..])
        }
        None => ("", rest),
    };
    if zone != "Z" {
        return None;
    }
    if !(1..=12).contains(&month) || hour > 23 || minute > 59 || second > 59 {
        return None;
    }
    let leap = (year % 4 == 0 && year % 100 != 0) || year % 400 == 0;
    let days_in_month = [
        31,
        if leap { 29 } else { 28 },
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ];
    if day == 0 || day > days_in_month[month as usize - 1] {
        return None;
    }
    let days = days_from_civil(year as i64, month, day);
    let seconds =
        days * 86_400 + i64::from(hour) * 3_600 + i64::from(minute) * 60 + i64::from(second);
    let nanos = if fraction.is_empty() {
        0
    } else {
        format!("{fraction:0<9}").parse().ok()?
    };
    Some(UtcTime { seconds, nanos })
}

// Days since 1970-01-01 for a proleptic Gregorian date (Howard Hinnant's
// algorithm).
fn days_from_civil(year: i64, month: u32, day: u32) -> i64 {
    let y = if month <= 2 { year - 1 } else { year };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let m = i64::from(month);
    let doy = (153 * (if m > 2 { m - 3 } else { m + 9 }) + 2) / 5 + i64::from(day) - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_utc_and_orders_instants() {
        let a = parse_utc("2026-09-27T00:00:00Z").unwrap();
        let b = parse_utc("2026-09-27T12:00:00Z").unwrap();
        let c = parse_utc("2026-09-27T12:00:00.5Z").unwrap();
        assert!(a < b && b < c);
        assert_eq!(
            parse_utc("1970-01-01T00:00:00Z").unwrap(),
            UtcTime {
                seconds: 0,
                nanos: 0
            }
        );
        assert_eq!(
            parse_utc("2000-03-01T00:00:00Z").unwrap().seconds,
            951_868_800
        );
    }

    #[test]
    fn refuses_anything_but_rfc3339_utc() {
        for bad in [
            "2026-09-27",
            "2026-09-27T00:00:00",
            "2026-09-27T00:00:00+00:00",
            "2026-09-27t00:00:00z",
            "2026-02-30T00:00:00Z",
            "2026-13-01T00:00:00Z",
            "2026-09-27T24:00:00Z",
            "2026-09-27T00:00:00.Z",
            "yesterday",
        ] {
            assert_eq!(parse_utc(bad), None, "{bad}");
        }
    }
}
