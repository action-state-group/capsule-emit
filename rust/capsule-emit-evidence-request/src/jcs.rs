//! RFC 8785 (JCS) serialization for the values this protocol signs.
//!
//! Written out rather than relying on a JSON library's map order: member
//! names are sorted by UTF-16 code units, strings are escaped as RFC 8785
//! requires, and numbers are limited to integers (a refusal signs text; a
//! number that is not an integer is refused rather than risk a
//! serialization that differs from another implementation's).

use serde_json::Value;

/// Why a value has no RFC 8785 form here.
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum JcsError {
    #[error("a number that is not an integer has no canonical form in this crate")]
    NonIntegerNumber,
}

/// The RFC 8785 serialization of `value`.
pub fn to_string(value: &Value) -> Result<String, JcsError> {
    let mut out = String::new();
    write(value, &mut out)?;
    Ok(out)
}

fn write(value: &Value, out: &mut String) -> Result<(), JcsError> {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                out.push_str(&i.to_string());
            } else if let Some(u) = n.as_u64() {
                out.push_str(&u.to_string());
            } else {
                return Err(JcsError::NonIntegerNumber);
            }
        }
        Value::String(s) => write_string(s, out),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write(item, out)?;
            }
            out.push(']');
        }
        Value::Object(map) => {
            let mut members: Vec<(&String, &Value)> = map.iter().collect();
            members.sort_by(|(a, _), (b, _)| a.encode_utf16().cmp(b.encode_utf16()));
            out.push('{');
            for (i, (key, item)) in members.into_iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_string(key, out);
                out.push(':');
                write(item, out)?;
            }
            out.push('}');
        }
    }
    Ok(())
}

fn write_string(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn members_are_sorted_and_strings_escaped() {
        let v = json!({"reason": "x", "issued_at": "t", "request_digest": "d"});
        assert_eq!(
            to_string(&v).unwrap(),
            r#"{"issued_at":"t","reason":"x","request_digest":"d"}"#
        );
        let s = json!("a\"b\\c\n\u{1f}é/");
        assert_eq!(to_string(&s).unwrap(), "\"a\\\"b\\\\c\\n\\u001fé/\"");
    }

    #[test]
    fn sort_order_is_utf16_code_units() {
        // U+10000 (surrogate pair D800 DC00) sorts before U+FFFD in UTF-16,
        // after it in UTF-8 byte order.
        let v = json!({"\u{fffd}": 1, "\u{10000}": 2});
        assert_eq!(to_string(&v).unwrap(), "{\"\u{10000}\":2,\"\u{fffd}\":1}");
    }

    #[test]
    fn non_integer_numbers_are_refused() {
        assert_eq!(to_string(&json!(1.5)), Err(JcsError::NonIntegerNumber));
        assert_eq!(
            to_string(&json!([1, -2, [true, null]])).unwrap(),
            "[1,-2,[true,null]]"
        );
    }
}
