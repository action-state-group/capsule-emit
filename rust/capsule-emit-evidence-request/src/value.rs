//! One value model for both bindings, so the JSON and CBOR request forms are
//! checked by the same rules.

use std::collections::BTreeMap;

#[derive(Clone, Debug, PartialEq)]
pub(crate) enum Val {
    Null,
    Bool(bool),
    /// An unsigned integer.
    UInt(u64),
    /// A negative integer, or any other number the rules never accept.
    OtherNumber,
    Text(String),
    Array(Vec<Val>),
    /// A map with text keys. A duplicate key makes the whole value
    /// [`Val::Unusable`] (the request is then not well formed).
    Map(BTreeMap<String, Val>),
    /// Byte strings, tags, floats, non-text map keys: nothing the rules
    /// accept anywhere.
    Unusable,
}

impl Val {
    pub(crate) fn from_json(v: &serde_json::Value) -> Val {
        match v {
            serde_json::Value::Null => Val::Null,
            serde_json::Value::Bool(b) => Val::Bool(*b),
            serde_json::Value::Number(n) => n.as_u64().map_or(Val::OtherNumber, Val::UInt),
            serde_json::Value::String(s) => Val::Text(s.clone()),
            serde_json::Value::Array(a) => Val::Array(a.iter().map(Val::from_json).collect()),
            serde_json::Value::Object(o) => Val::Map(
                o.iter()
                    .map(|(k, v)| (k.clone(), Val::from_json(v)))
                    .collect(),
            ),
        }
    }

    pub(crate) fn from_cbor(v: &ciborium::Value) -> Val {
        use ciborium::Value as C;
        match v {
            C::Null => Val::Null,
            C::Bool(b) => Val::Bool(*b),
            C::Integer(i) => u64::try_from(*i).map_or(Val::OtherNumber, Val::UInt),
            C::Text(s) => Val::Text(s.clone()),
            C::Array(a) => Val::Array(a.iter().map(Val::from_cbor).collect()),
            C::Map(entries) => {
                let mut map = BTreeMap::new();
                for (k, v) in entries {
                    let C::Text(key) = k else {
                        return Val::Unusable;
                    };
                    if map.insert(key.clone(), Val::from_cbor(v)).is_some() {
                        return Val::Unusable;
                    }
                }
                Val::Map(map)
            }
            _ => Val::Unusable,
        }
    }

    pub(crate) fn as_text(&self) -> Option<&str> {
        match self {
            Val::Text(s) => Some(s),
            _ => None,
        }
    }
}
