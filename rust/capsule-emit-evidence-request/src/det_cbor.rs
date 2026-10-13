//! Core deterministic encoding (RFC 8949 §4.2.1) of a CBOR request frame,
//! checked on the bytes as received (§10: the stream binding's frames are
//! deterministic CBOR).
//!
//! A request in the CBOR binding is accepted only in its one deterministic
//! form, so the same request has the same octets, and the same digest
//! (§4.2), whoever encoded it:
//! - every argument in its shortest form, and every length definite;
//! - map keys text strings, in the bytewise order of their encodings (a
//!   shorter key first), with no key repeated;
//! - no tags, no floating-point values, no byte strings and no simple
//!   values other than `false`, `true` and `null`;
//! - integers unsigned and at most 2^53 - 1, so that the JSON data model
//!   holds them exactly (`evidence-interop/0` §5.2).
//!
//! These hold at every depth, inside members this crate ignores too.

/// The deepest nesting checked. A -00 request nests three deep; the CBOR
/// parser that runs first stops well before this.
const MAX_DEPTH: usize = 512;

/// The largest integer accepted: 2^53 - 1.
const MAX_INTEGER: u64 = (1 << 53) - 1;

/// Check that `bytes` is exactly one data item in core deterministic
/// encoding, under the rules above. The error says which rule failed.
pub(crate) fn check(bytes: &[u8]) -> Result<(), &'static str> {
    let mut reader = Reader { bytes, at: 0 };
    reader.item(0)?;
    if reader.at != bytes.len() {
        return Err("bytes after the request item");
    }
    Ok(())
}

struct Reader<'a> {
    bytes: &'a [u8],
    at: usize,
}

impl Reader<'_> {
    fn take(&mut self, n: usize) -> Result<&[u8], &'static str> {
        let end = self
            .at
            .checked_add(n)
            .filter(|&end| end <= self.bytes.len())
            .ok_or("the request ends inside an item")?;
        let taken = &self.bytes[self.at..end];
        self.at = end;
        Ok(taken)
    }

    /// One item's head: its major type and argument, with the argument in
    /// its shortest form.
    fn head(&mut self) -> Result<(u8, u64), &'static str> {
        let initial = self.take(1)?[0];
        let (major, info) = (initial >> 5, initial & 0x1f);
        let (argument, smallest) = match info {
            0..=23 => (u64::from(info), 0),
            24 => (u64::from(self.take(1)?[0]), 24),
            25 => (u64::from(u16::from_be_bytes(self.array()?)), 1 << 8),
            26 => (u64::from(u32::from_be_bytes(self.array()?)), 1 << 16),
            27 => (u64::from_be_bytes(self.array()?), 1 << 32),
            31 => return Err("an indefinite length"),
            _ => return Err("a reserved additional-information value"),
        };
        // Major type 7 uses 25-27 for floats, whose width is not a length.
        if major != 7 && argument < smallest {
            return Err("an integer or length not in its shortest form");
        }
        Ok((major, argument))
    }

    fn array<const N: usize>(&mut self) -> Result<[u8; N], &'static str> {
        Ok(self
            .take(N)?
            .try_into()
            .expect("take returns exactly N bytes"))
    }

    fn length(&mut self, argument: u64) -> Result<usize, &'static str> {
        usize::try_from(argument).map_err(|_| "the request ends inside an item")
    }

    fn item(&mut self, depth: usize) -> Result<(), &'static str> {
        if depth > MAX_DEPTH {
            return Err("nested too deeply");
        }
        let start = self.at;
        let (major, argument) = self.head()?;
        match major {
            0 if argument <= MAX_INTEGER => Ok(()),
            0 => Err("an integer above 2^53 - 1"),
            1 => Err("a negative integer"),
            2 => Err("a byte string"),
            3 => {
                let n = self.length(argument)?;
                std::str::from_utf8(self.take(n)?).map_err(|_| "text that is not UTF-8")?;
                Ok(())
            }
            4 => {
                for _ in 0..argument {
                    self.item(depth + 1)?;
                }
                Ok(())
            }
            5 => {
                let mut previous: Option<&[u8]> = None;
                for _ in 0..argument {
                    let key_start = self.at;
                    let (key_major, _) = self.head()?;
                    self.at = key_start;
                    if key_major != 3 {
                        return Err("a map key that is not text");
                    }
                    self.item(depth + 1)?;
                    let key = &self.bytes[key_start..self.at];
                    match previous {
                        Some(p) if p == key => return Err("a duplicate map key"),
                        Some(p) if p > key => {
                            return Err("map keys not in the bytewise order of their encodings")
                        }
                        _ => previous = Some(key),
                    }
                    self.item(depth + 1)?;
                }
                Ok(())
            }
            6 => Err("a tag"),
            _ => match self.bytes[start] {
                0xf4..=0xf6 => Ok(()),
                0xf9..=0xfb => Err("a floating-point value"),
                _ => Err("a simple value other than false, true or null"),
            },
        }
    }
}

#[cfg(test)]
mod tests {
    use super::check;

    fn hex(s: &str) -> Vec<u8> {
        hex::decode(s.replace(' ', "")).unwrap()
    }

    #[test]
    fn deterministic_items_pass() {
        for ok in [
            "00",
            "17",
            "18 18",
            "19 0100",
            "1a 00010000",
            "1b 0000000100000000",
            "1b 001fffffffffffff",
            "60",
            "63 616263",
            "80",
            "82 01 02",
            "a0",
            // {"a": 1, "b": 2, "aa": 3}: a shorter key first.
            "a3 6161 01 6162 02 626161 03",
            "f4",
            "f5",
            "f6",
        ] {
            assert_eq!(check(&hex(ok)), Ok(()), "{ok}");
        }
    }

    #[test]
    fn each_rule_has_its_failure() {
        for (bad, why) in [
            ("18 17", "an integer or length not in its shortest form"),
            ("19 00ff", "an integer or length not in its shortest form"),
            (
                "1a 0000ffff",
                "an integer or length not in its shortest form",
            ),
            (
                "1b 00000000ffffffff",
                "an integer or length not in its shortest form",
            ),
            ("78 01 61", "an integer or length not in its shortest form"),
            ("1b 0020000000000000", "an integer above 2^53 - 1"),
            ("20", "a negative integer"),
            ("41 00", "a byte string"),
            ("62 c328", "text that is not UTF-8"),
            ("9f ff", "an indefinite length"),
            ("bf ff", "an indefinite length"),
            ("1c", "a reserved additional-information value"),
            ("a1 01 01", "a map key that is not text"),
            ("a2 6161 01 6161 02", "a duplicate map key"),
            (
                "a2 6162 01 6161 02",
                "map keys not in the bytewise order of their encodings",
            ),
            // {"aa": 1, "b": 2}: lexical order, not length-first.
            (
                "a2 626161 01 6162 02",
                "map keys not in the bytewise order of their encodings",
            ),
            ("c0 60", "a tag"),
            ("f9 0000", "a floating-point value"),
            ("fb 0000000000000000", "a floating-point value"),
            ("f7", "a simple value other than false, true or null"),
            ("f8 20", "a simple value other than false, true or null"),
            ("00 00", "bytes after the request item"),
            ("82 01", "the request ends inside an item"),
            ("63 6162", "the request ends inside an item"),
            ("", "the request ends inside an item"),
        ] {
            assert_eq!(check(&hex(bad)), Err(why), "{bad}");
        }
    }

    #[test]
    fn rules_hold_inside_nested_members() {
        // {"x": {"b": 1, "a": 2}}: an unknown member out of order inside.
        assert_eq!(
            check(&hex("a1 6178 a2 6162 01 6161 02")),
            Err("map keys not in the bytewise order of their encodings")
        );
        // {"x": [1.0]}
        assert_eq!(
            check(&hex("a1 6178 81 f9 3c00")),
            Err("a floating-point value")
        );
    }

    #[test]
    fn nesting_is_bounded() {
        let mut deep = vec![0x81; super::MAX_DEPTH + 2];
        deep.push(0x00);
        assert_eq!(check(&deep), Err("nested too deeply"));
    }
}
