//! The optional index extension (`docs/artifact-store.md`, "Optional index
//! extension"): a namespace declares JSON paths into its Capsules, the store
//! extracts their values when it admits a record, and [`IndexStore::find`]
//! returns matching Capsule IDs without reading record bodies.
//!
//! This module also holds what every backend shares: path validation, value
//! extraction from Capsule bytes, timestamp normalization, query checks,
//! cursors, and the in-memory index the `jsonl` and `memory` backends keep.

use std::collections::{BTreeMap, HashMap, HashSet};

use serde_json::Value;

use crate::error::{ArtifactError, Result};
use crate::record::decode_strict_json;
use crate::store::Store;

/// The most paths one namespace may declare.
pub const MAX_INDEX_PATHS: usize = 32;
/// The page size [`Query::default`] uses.
pub const DEFAULT_FIND_LIMIT: i64 = 100;
/// The largest page [`IndexStore::find`] returns.
pub const MAX_FIND_LIMIT: i64 = 1000;

/// Selects records by the namespace's declared index paths.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Query {
    /// Maps declared paths to the value each must hold. A record matches a
    /// path whose value is that string, or an array containing it. Every
    /// entry must match; an empty map matches every record.
    pub r#where: BTreeMap<String, String>,
    /// An RFC 3339 date-time; only records whose top-level timestamp is at or
    /// after it match.
    pub since: Option<String>,
    /// The [`Page::next`] of a previous page, or `None` for the first page.
    pub after: Option<String>,
    /// The page size, 1 to [`MAX_FIND_LIMIT`]; anything else is invalid.
    pub limit: i64,
}

impl Default for Query {
    fn default() -> Self {
        Query {
            r#where: BTreeMap::new(),
            since: None,
            after: None,
            limit: DEFAULT_FIND_LIMIT,
        }
    }
}

/// One page of [`IndexStore::find`] results.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Page {
    /// The matching Capsule IDs in the store's admission order. That is not
    /// the order of any CLL log.
    pub ids: Vec<String>,
    /// The cursor for the following page, `None` when no further match exists.
    pub next: Option<String>,
}

/// The optional index extension. Every built-in backend implements it, and
/// returns itself from [`Store::as_index_store`] so callers holding a
/// `dyn Store` can detect it; a backend of your own should do the same.
pub trait IndexStore: Store {
    /// Provisions storage as [`Store::init`] does, then declares `paths` and
    /// indexes the records already stored. Declaring the same set again does
    /// nothing; a different set is [`ArtifactError::Conflict`].
    fn init_index(&self, paths: &[&str]) -> Result<()>;
    /// Replaces the declared paths, which may differ, and rebuilds the index
    /// from the stored records.
    fn reindex(&self, paths: &[&str]) -> Result<()>;
    /// Returns one page of matching Capsule IDs.
    fn find(&self, query: &Query) -> Result<Page>;
}

fn invalid(why: impl Into<String>) -> ArtifactError {
    ArtifactError::Invalid(why.into())
}

/// Validates a declared path list and returns it sorted. The list is a set:
/// order does not matter and a repeated path is invalid.
pub(crate) fn normalize_paths<S: AsRef<str>>(paths: &[S]) -> Result<Vec<String>> {
    if paths.len() > MAX_INDEX_PATHS {
        return Err(invalid(format!("more than {MAX_INDEX_PATHS} index paths")));
    }
    let mut sorted: Vec<String> = paths.iter().map(|p| p.as_ref().to_string()).collect();
    sorted.sort();
    for (i, path) in sorted.iter().enumerate() {
        if !valid_path(path) {
            return Err(invalid(format!("index path {path:?}")));
        }
        if i > 0 && sorted[i - 1] == *path {
            return Err(invalid(format!("repeated index path {path:?}")));
        }
    }
    Ok(sorted)
}

fn valid_path(path: &str) -> bool {
    path.len() <= 255
        && path.split('.').count() <= 8
        && path.split('.').all(|segment| {
            (1..=64).contains(&segment.len())
                && segment
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
        })
}

/// The stored form of a normalized path list: a compact JSON array. Path
/// characters need no escaping, so every language writes the same text.
#[cfg_attr(not(any(feature = "sqlite", feature = "mysql")), allow(dead_code))]
pub(crate) fn encode_paths(paths: &[String]) -> String {
    let quoted: Vec<String> = paths.iter().map(|p| format!("\"{p}\"")).collect();
    format!("[{}]", quoted.join(","))
}

/// Reads [`encode_paths`] output back. Anything else, including a JSON list
/// that is not a valid declaration, is stored damage and so corrupt.
#[cfg_attr(not(any(feature = "sqlite", feature = "mysql")), allow(dead_code))]
pub(crate) fn decode_paths(stored: &str) -> Result<Vec<String>> {
    let corrupt =
        |e: &dyn std::fmt::Display| ArtifactError::Corrupt(format!("stored index paths: {e}"));
    let paths: Vec<String> = serde_json::from_str(stored).map_err(|e| corrupt(&e))?;
    normalize_paths(&paths).map_err(|e| corrupt(&e))
}

/// What a record contributes to the index.
#[derive(Debug, Default, PartialEq, Eq)]
pub(crate) struct Extracted {
    /// (path, value) pairs, one per string value and per distinct string
    /// array element.
    pub entries: Vec<(String, String)>,
    /// The top-level timestamp in UTC microseconds since the Unix epoch.
    pub ts: Option<i64>,
}

/// Reads the declared paths and the top-level timestamp from Capsule bytes.
/// A string value is one entry and each distinct string in an array is one
/// entry; any other value, or a missing member, gives none. The bytes were
/// verified at admission, so a decode failure is reported as corrupt.
pub(crate) fn extract(capsule: &[u8], paths: &[String]) -> Result<Extracted> {
    let root = decode_strict_json(capsule)
        .map_err(|e| ArtifactError::Corrupt(format!("capsule is not JSON: {e}")))?;
    let mut out = Extracted {
        ts: root
            .get("timestamp")
            .and_then(Value::as_str)
            .and_then(parse_timestamp),
        ..Extracted::default()
    };
    for path in paths {
        let mut value = Some(&root);
        for segment in path.split('.') {
            value = value
                .and_then(Value::as_object)
                .and_then(|o| o.get(segment));
        }
        match value {
            Some(Value::String(s)) => out.entries.push((path.clone(), s.clone())),
            Some(Value::Array(elements)) => {
                let mut seen = HashSet::new();
                for s in elements.iter().filter_map(Value::as_str) {
                    if seen.insert(s) {
                        out.entries.push((path.clone(), s.to_string()));
                    }
                }
            }
            _ => {}
        }
    }
    Ok(out)
}

/// Reads an RFC 3339 date-time (`YYYY-MM-DDTHH:MM:SS`, an optional `.` and 1
/// to 9 fraction digits, then `Z` or `+HH:MM`/`-HH:MM`; upper case, ASCII
/// digits only) and returns it in UTC microseconds since the Unix epoch,
/// truncating finer digits.
pub(crate) fn parse_timestamp(s: &str) -> Option<i64> {
    let b = s.as_bytes();
    let digits = |from: usize, len: usize| -> Option<i64> {
        let field = b.get(from..from + len)?;
        field.iter().try_fold(0i64, |n, &c| {
            c.is_ascii_digit().then(|| n * 10 + i64::from(c - b'0'))
        })
    };
    let at = |i: usize, c: u8| b.get(i) == Some(&c);
    if !(at(4, b'-') && at(7, b'-') && at(10, b'T') && at(13, b':') && at(16, b':')) {
        return None;
    }
    let (year, month, day) = (digits(0, 4)?, digits(5, 2)?, digits(8, 2)?);
    let (hour, minute, second) = (digits(11, 2)?, digits(14, 2)?, digits(17, 2)?);
    let mut i = 19;
    let mut micros = 0;
    if at(i, b'.') {
        let start = i + 1;
        let mut end = start;
        while b.get(end).is_some_and(u8::is_ascii_digit) {
            end += 1;
        }
        if !(1..=9).contains(&(end - start)) {
            return None;
        }
        // Microseconds: the first six fraction digits, zero-padded.
        let fraction = &b[start..end];
        for k in 0..6 {
            micros = micros * 10 + fraction.get(k).map_or(0, |c| i64::from(c - b'0'));
        }
        i = end;
    }
    let offset = match b.get(i..)? {
        b"Z" => 0,
        [sign @ (b'+' | b'-'), _, _, b':', _, _] => {
            let (oh, om) = (digits(i + 1, 2)?, digits(i + 4, 2)?);
            if oh > 23 || om > 59 {
                return None;
            }
            let minutes = oh * 60 + om;
            if *sign == b'-' {
                -minutes
            } else {
                minutes
            }
        }
        _ => return None,
    };
    if !(1..=12).contains(&month)
        || day < 1
        || day > days_in(year, month)
        || hour > 23
        || minute > 59
        || second > 59
    {
        return None;
    }
    let days = days_from_civil(year, month, day);
    let seconds = days * 86_400 + hour * 3600 + minute * 60 + second - offset * 60;
    Some(seconds * 1_000_000 + micros)
}

fn days_in(year: i64, month: i64) -> i64 {
    match month {
        2 if year % 4 == 0 && (year % 100 != 0 || year % 400 == 0) => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

/// Days since 1970-01-01 in the proleptic Gregorian calendar (Howard
/// Hinnant's days_from_civil).
fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let y = if month <= 2 { year - 1 } else { year };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let mp = (month + 9) % 12;
    let doy = (153 * mp + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// A validated query.
#[derive(Debug)]
pub(crate) struct Checked {
    /// The equalities, sorted by path.
    pub r#where: Vec<(String, String)>,
    pub since: Option<i64>,
    /// The admission position to continue after; 0 for the first page.
    pub after: i64,
    pub limit: i64,
}

/// Validates `query` against the declared paths.
pub(crate) fn check(query: &Query, declared: &[String]) -> Result<Checked> {
    if !(1..=MAX_FIND_LIMIT).contains(&query.limit) {
        return Err(invalid(format!("limit {}", query.limit)));
    }
    let mut r#where = Vec::with_capacity(query.r#where.len());
    for (path, value) in &query.r#where {
        if !declared.contains(path) {
            return Err(invalid(format!("{path:?} is not a declared index path")));
        }
        r#where.push((path.clone(), value.clone()));
    }
    // An empty since or after means not given, as in the other languages.
    let since = match query.since.as_deref() {
        None | Some("") => None,
        Some(s) => Some(
            parse_timestamp(s)
                .ok_or_else(|| invalid(format!("since {s:?} is not an RFC 3339 date-time")))?,
        ),
    };
    let after = match query.after.as_deref() {
        None | Some("") => 0,
        Some(cursor) => match cursor.parse::<i64>() {
            Ok(n) if n >= 1 && n.to_string() == *cursor => n,
            _ => return Err(invalid("cursor")),
        },
    };
    Ok(Checked {
        r#where,
        since,
        after,
        limit: query.limit,
    })
}

/// The in-memory index of the `jsonl` and `memory` backends. Positions start
/// at 1 and follow admission order. The backend holds its own lock.
#[derive(Debug, Default)]
pub(crate) struct MemoryIndex {
    /// `None` until paths are declared.
    paths: Option<Vec<String>>,
    /// By position - 1.
    ts: Vec<Option<i64>>,
    /// By position - 1.
    ids: Vec<String>,
    /// path -> value -> ascending positions.
    values: HashMap<String, HashMap<String, Vec<i64>>>,
    /// Capsule ID -> position.
    position: HashMap<String, i64>,
}

impl MemoryIndex {
    /// Records paths for a fresh index, or checks them against the existing
    /// declaration: the same set is a no-op, another is a conflict. Reports
    /// whether the caller must now add the existing records.
    pub fn declare(&mut self, paths: &[&str]) -> Result<bool> {
        let normalized = normalize_paths(paths)?;
        match &self.paths {
            Some(current) if *current != normalized => Err(ArtifactError::Conflict),
            Some(_) => Ok(false),
            None => {
                self.reset(normalized);
                Ok(true)
            }
        }
    }

    /// Whether paths were declared.
    pub fn is_declared(&self) -> bool {
        self.paths.is_some()
    }

    /// Replaces the declaration with normalized paths and empties the index.
    pub fn reset(&mut self, normalized: Vec<String>) {
        *self = MemoryIndex {
            paths: Some(normalized),
            ..MemoryIndex::default()
        };
    }

    /// Indexes one record at the next position. A record already indexed is
    /// left where it is. Does nothing before a declaration.
    pub fn add(&mut self, id: &str, capsule: &[u8]) -> Result<()> {
        let Some(paths) = &self.paths else {
            return Ok(());
        };
        if self.position.contains_key(id) {
            return Ok(());
        }
        let extracted = extract(capsule, paths)?;
        let position = self.ids.len() as i64 + 1;
        self.ids.push(id.to_string());
        self.position.insert(id.to_string(), position);
        self.ts.push(extracted.ts);
        for (path, value) in extracted.entries {
            self.values
                .entry(path)
                .or_default()
                .entry(value)
                .or_default()
                .push(position);
        }
        Ok(())
    }

    fn positions(&self, path: &str, value: &str) -> &[i64] {
        self.values
            .get(path)
            .and_then(|v| v.get(value))
            .map_or(&[], Vec::as_slice)
    }

    /// Answers `query`. Before a declaration it is invalid.
    pub fn find(&self, query: &Query) -> Result<Page> {
        let paths = self
            .paths
            .as_ref()
            .ok_or_else(|| invalid("no index paths declared"))?;
        let c = check(query, paths)?;
        if c.after >= self.ids.len() as i64 {
            // Also keeps `after + 1` below from overflowing.
            return Ok(Page::default());
        }
        let candidates: Vec<i64> = match c.r#where.first() {
            None => (c.after + 1..=self.ids.len() as i64).collect(),
            Some((path, value)) => {
                let all = self.positions(path, value);
                let start = all.partition_point(|&p| p <= c.after);
                all[start..].to_vec()
            }
        };
        let mut page = Page::default();
        let mut last = 0;
        for p in candidates {
            if !self.matches(p, &c) {
                continue;
            }
            if page.ids.len() as i64 == c.limit {
                page.next = Some(last.to_string());
                break;
            }
            page.ids.push(self.ids[(p - 1) as usize].clone());
            last = p;
        }
        Ok(page)
    }

    fn matches(&self, p: i64, c: &Checked) -> bool {
        if let Some(since) = c.since {
            match self.ts[(p - 1) as usize] {
                Some(ts) if ts >= since => {}
                _ => return false,
            }
        }
        c.r#where
            .iter()
            .skip(1)
            .all(|(path, value)| self.positions(path, value).binary_search(&p).is_ok())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The pinned index rows: what the SQL backends store must agree byte for
    /// byte across languages. Entry order is not part of a row set.
    #[test]
    fn index_row_vectors() {
        use sha2::{Digest, Sha256};
        let vectors: Value =
            serde_json::from_str(include_str!("conformance_vectors.json")).unwrap();
        let rows = vectors["index_rows"].as_array().unwrap();
        assert!(!rows.is_empty());
        for row in rows {
            let name = row["record"].as_str().unwrap();
            let declared: Vec<&str> = row["paths"]
                .as_array()
                .unwrap()
                .iter()
                .map(|p| p.as_str().unwrap())
                .collect();
            let paths = normalize_paths(&declared).unwrap();
            assert_eq!(
                encode_paths(&paths),
                row["stored"].as_str().unwrap(),
                "{name}"
            );
            let line = serde_json::to_vec(&vectors["records"][name]).unwrap();
            let record = crate::decode_record(&line).unwrap();
            let got = extract(&record.capsule, &paths).unwrap();
            let ts = got.ts.map(|t| t.to_string()).unwrap_or_default();
            assert_eq!(ts, row["ts"].as_str().unwrap(), "{name}");
            let mut entries: Vec<String> = got
                .entries
                .iter()
                .map(|(path, value)| {
                    format!("{path} {}", hex::encode(Sha256::digest(value.as_bytes())))
                })
                .collect();
            let mut want: Vec<String> = row["entries"]
                .as_array()
                .unwrap()
                .iter()
                .map(|e| {
                    format!(
                        "{} {}",
                        e["path"].as_str().unwrap(),
                        e["value_sha256"].as_str().unwrap()
                    )
                })
                .collect();
            entries.sort();
            want.sort();
            assert_eq!(entries, want, "{name}");
        }
    }

    #[test]
    fn timestamps() {
        let ok = |s: &str| parse_timestamp(s).unwrap_or_else(|| panic!("{s}"));
        assert_eq!(ok("1970-01-01T00:00:00Z"), 0);
        assert_eq!(ok("1970-01-01T00:00:00.000001Z"), 1);
        assert_eq!(ok("1970-01-01T00:00:00.1Z"), 100_000);
        assert_eq!(ok("1970-01-01T00:00:00.123456789Z"), 123_456);
        assert_eq!(ok("1969-12-31T23:59:59.999999Z"), -1);
        assert_eq!(ok("1970-01-01T01:00:00+01:00"), 0);
        assert_eq!(ok("1969-12-31T23:30:00-00:30"), 0);
        assert_eq!(ok("2026-10-16T00:00:00Z"), 1_792_108_800_000_000);
        assert_eq!(ok("2000-02-29T00:00:00Z"), 951_782_400_000_000);
        assert_eq!(ok("2024-02-29T23:59:59+23:59"), ok("2024-02-29T00:00:59Z"));
        assert_eq!(ok("0000-01-01T00:00:00Z"), -62_167_219_200_000_000);
        assert_eq!(ok("9999-12-31T23:59:59Z"), 253_402_300_799_000_000);
        for bad in [
            "",
            "2026-10-16",
            "2026-10-16T00:00:00",
            "2026-10-16T00:00:00z",
            "2026-10-16t00:00:00Z",
            "2026-10-16 00:00:00Z",
            "2026-10-16T00:00Z",
            "2026-10-16T00:00:00.Z",
            "2026-10-16T00:00:00.0000000001Z",
            "2026-10-16T00:00:00ZZ",
            "2026-10-16T00:00:00+0100",
            "2026-10-16T00:00:00+01:00Z",
            "2026-10-16T00:00:00+24:00",
            "2026-10-16T00:00:00+00:60",
            "2026-10-16T00:00:00+1:00",
            "2026-00-16T00:00:00Z",
            "2026-13-16T00:00:00Z",
            "2026-10-00T00:00:00Z",
            "2026-10-32T00:00:00Z",
            "2026-04-31T00:00:00Z",
            "2026-02-29T00:00:00Z",
            "1900-02-29T00:00:00Z",
            "2026-10-16T24:00:00Z",
            "2026-10-16T00:60:00Z",
            "2026-10-16T00:00:60Z",
            "+026-10-16T00:00:00Z",
            "2026-1a-16T00:00:00Z",
            "\u{ff12}026-10-16T00:00:00Z",
            "2026-10-16T00:00:00\u{ff3a}",
            "20261-10-16T00:00:00Z",
        ] {
            assert_eq!(parse_timestamp(bad), None, "{bad:?}");
        }
    }

    #[test]
    fn extraction() {
        let capsule = br#"{"timestamp":"2026-10-16T00:00:00Z","s":"x","n":1,"b":true,"z":null,
            "o":{"a":"in","arr":["p",1,"p",{"q":"r"},"q",null]},"arr":"top"}"#;
        let paths: Vec<String> = [
            "s",
            "n",
            "b",
            "z",
            "o",
            "o.a",
            "o.arr",
            "o.a.deeper",
            "s.x",
            "missing",
            "o.missing",
        ]
        .iter()
        .map(|s| s.to_string())
        .collect();
        let got = extract(capsule, &paths).unwrap();
        assert_eq!(got.ts, Some(1_792_108_800_000_000));
        let pair = |p: &str, v: &str| (p.to_string(), v.to_string());
        assert_eq!(
            got.entries,
            vec![
                pair("s", "x"),
                pair("o.a", "in"),
                pair("o.arr", "p"),
                pair("o.arr", "q")
            ]
        );
        // A non-object root, a non-string timestamp and a malformed one.
        assert_eq!(extract(b"[1]", &paths).unwrap(), Extracted::default());
        assert_eq!(extract(br#"{"timestamp":1}"#, &[]).unwrap().ts, None);
        assert_eq!(
            extract(br#"{"timestamp":"2026-10-16"}"#, &[]).unwrap().ts,
            None
        );
        for bad in [&b"{"[..], b"{\"a\":1,\"a\":2}", b"\xff"] {
            assert_eq!(extract(bad, &paths).unwrap_err().code(), Some("corrupt"));
        }
    }

    #[test]
    fn paths_and_cursors() {
        assert_eq!(
            normalize_paths(&["b", "a.c"]).unwrap(),
            vec!["a.c".to_string(), "b".to_string()]
        );
        assert_eq!(
            encode_paths(&normalize_paths(&["b", "a"]).unwrap()),
            r#"["a","b"]"#
        );
        assert_eq!(encode_paths(&[]), "[]");
        assert_eq!(decode_paths(r#"["b","a"]"#).unwrap(), vec!["a", "b"]);
        for bad in ["{", "[1]", r#"["a","a"]"#, r#"[""]"#, "null"] {
            assert!(decode_paths(bad).is_err(), "{bad}");
        }
        for bad in ["{", r#"["a","a"]"#, r#"["a b"]"#] {
            assert_eq!(
                decode_paths(bad).unwrap_err().code(),
                Some("corrupt"),
                "{bad}"
            );
        }
        let long = vec!["a".repeat(64); 4].join(".");
        assert!(long.len() == 259 && !valid_path(&long));
        assert!(valid_path(&vec!["a".repeat(63); 4].join(".")));
        let declared = vec!["a".to_string()];
        for (after, ok) in [
            ("1", true),
            ("9223372036854775807", true),
            ("+1", false),
            (" 1", false),
            ("0", false),
            ("9223372036854775808", false),
        ] {
            let q = Query {
                after: Some(after.into()),
                ..Query::default()
            };
            assert_eq!(check(&q, &declared).is_ok(), ok, "{after:?}");
        }
        for limit in [0, -1, 1001] {
            let q = Query {
                limit,
                ..Query::default()
            };
            assert!(check(&q, &declared).is_err(), "{limit}");
        }
        let q = Query {
            since: Some(String::new()),
            after: Some(String::new()),
            ..Query::default()
        };
        let c = check(&q, &declared).unwrap();
        assert_eq!((c.since, c.after), (None, 0));
    }
}
