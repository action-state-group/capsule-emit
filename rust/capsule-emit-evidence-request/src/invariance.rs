//! Caller invariance (§5): observations of the same subject under the same
//! anchor carry byte-identical artifacts, whoever asked, over whatever
//! transport, with whatever nonce or route. Verification material delivered
//! beside the artifact may differ. Observations under different anchors are
//! not comparable (A12: artifacts are compared as bytes).

/// One observed answer: the anchor it was resolved under and the artifact
/// bytes.
#[derive(Clone, Debug)]
pub struct Observation<'a> {
    pub resolved_anchor: &'a str,
    pub artifact: &'a [u8],
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Invariance {
    Holds,
    Violated,
    NotComparable,
}

impl Invariance {
    pub fn token(self) -> &'static str {
        match self {
            Invariance::Holds => "holds",
            Invariance::Violated => "violated",
            Invariance::NotComparable => "not_comparable",
        }
    }
}

/// Compare observations of one subject.
pub fn compare(observations: &[Observation<'_>]) -> Invariance {
    let Some(first) = observations.first() else {
        return Invariance::Holds;
    };
    if observations
        .iter()
        .any(|o| o.resolved_anchor != first.resolved_anchor)
    {
        return Invariance::NotComparable;
    }
    if observations.iter().all(|o| o.artifact == first.artifact) {
        Invariance::Holds
    } else {
        Invariance::Violated
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn compares_bytes_under_one_anchor() {
        let a = Observation {
            resolved_anchor: "x",
            artifact: b"{\"a\":1,\"b\":2}",
        };
        let reordered = Observation {
            resolved_anchor: "x",
            artifact: b"{\"b\":2,\"a\":1}",
        };
        let elsewhere = Observation {
            resolved_anchor: "y",
            artifact: b"{\"a\":1,\"b\":2}",
        };
        assert_eq!(compare(&[a.clone(), a.clone()]), Invariance::Holds);
        assert_eq!(compare(&[a.clone(), reordered]), Invariance::Violated);
        assert_eq!(compare(&[a, elsewhere]), Invariance::NotComparable);
    }
}
