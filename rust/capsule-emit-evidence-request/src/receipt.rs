//! Offline verification of a witness's COSE Receipt for a checkpoint.
//!
//! A checkpoint may carry receipts from transparency services that
//! registered it. Each receipt is a COSE_Sign1 signed by the service, whose
//! detached payload is a Merkle tree root and whose unprotected header carries
//! an RFC 9162 SHA-256 inclusion proof for one leaf
//! (draft-ietf-cose-merkle-tree-proofs). The leaf a checkpoint's receipt must
//! prove is the checkpoint's entry, `SHA-256(checkpoint digest bytes)`, so a
//! receipt verifies for one checkpoint only.
//!
//! This is the Ed25519 subset of the RFC 9162 receipt verifier in
//! action-state-group/scitt-cose (`rust/scitt-cose`, Apache-2.0), kept here so
//! this crate has no dependency that is not published. Proof-array behavior and
//! shared vectors are pinned to scitt-cose commit
//! `172632db780df428c2157b0b77945e87c2ca4b7e`.

use coset::cbor::value::Value as CborValue;
use coset::iana::EnumI64 as _;
use coset::{CoseSign1, RegisteredLabelWithPrivate, TaggedCborSerializable};
use ed25519_dalek::{Signature, VerifyingKey};
use sha2::{Digest, Sha256};

const ALG_EDDSA: i64 = -8;
const HDR_VDS: i64 = 395;
const HDR_VDP: i64 = 396;
const VDP_INCLUSION_PROOFS: i64 = -1;
const VDS_RFC9162_SHA256: i64 = 1;
const MAX_INCLUSION_PROOFS: usize = 16;
const MAX_AUDIT_PATH: usize = 64;
const MAX_TREE_SIZE: u64 = 1 << 62;

/// Why a receipt does not verify.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum ReceiptError {
    /// Not a COSE_Sign1 receipt with an RFC 9162 inclusion proof that this
    /// verifier reads (Ed25519, detached payload).
    #[error("not a receipt this verifier reads: {0}")]
    Malformed(&'static str),
    /// The inclusion proof does not lead from this leaf to a root, or the
    /// service's signature does not cover the root it leads to: the receipt is
    /// not for this leaf, or not from this service.
    #[error("the receipt does not prove this leaf under this service's key")]
    NotForThisLeaf,
}

/// The parts of a receipt the checks need.
struct Parsed {
    sign1: CoseSign1,
    proofs: Vec<InclusionProof>,
}

struct InclusionProof {
    tree_size: u64,
    leaf_index: u64,
    path: Vec<[u8; 32]>,
}

fn parse(receipt: &[u8]) -> Result<Parsed, ReceiptError> {
    use ReceiptError::Malformed;
    let sign1 = CoseSign1::from_tagged_slice(receipt).map_err(|_| Malformed("not a COSE_Sign1"))?;
    if sign1.payload.is_some() {
        return Err(Malformed("the payload must be detached"));
    }
    let alg = match &sign1.protected.header.alg {
        Some(RegisteredLabelWithPrivate::Assigned(a)) => a.to_i64(),
        Some(RegisteredLabelWithPrivate::PrivateUse(i)) => *i,
        _ => return Err(Malformed("no integer alg")),
    };
    if alg != ALG_EDDSA {
        return Err(Malformed("alg is not EdDSA"));
    }
    let vds = sign1
        .protected
        .header
        .rest
        .iter()
        .find(|(l, _)| *l == coset::Label::Int(HDR_VDS))
        .and_then(|(_, v)| v.as_integer())
        .map(i128::from);
    if vds != Some(VDS_RFC9162_SHA256 as i128) {
        return Err(Malformed("vds is not RFC9162_SHA256"));
    }
    let proofs = sign1
        .unprotected
        .rest
        .iter()
        .find(|(l, _)| *l == coset::Label::Int(HDR_VDP))
        .and_then(|(_, v)| v.as_map())
        .and_then(|m| {
            m.iter()
                .find(|(k, _)| k.as_integer().map(i128::from) == Some(VDP_INCLUSION_PROOFS as i128))
        })
        .and_then(|(_, v)| v.as_array())
        .filter(|p| !p.is_empty() && p.len() <= MAX_INCLUSION_PROOFS)
        .ok_or(Malformed("no inclusion proof"))?;
    let proofs = proofs
        .iter()
        .map(parse_proof)
        .collect::<Result<Vec<_>, _>>()?;
    if sign1.signature.len() != 64 {
        return Err(Malformed("signature is not 64 bytes"));
    }
    Ok(Parsed { sign1, proofs })
}

fn parse_proof(proof: &CborValue) -> Result<InclusionProof, ReceiptError> {
    use ReceiptError::Malformed;
    let blob = proof
        .as_bytes()
        .ok_or(Malformed("inclusion proof is not bytes"))?;
    let mut reader = blob.as_slice();
    let value: CborValue = coset::cbor::de::from_reader(&mut reader)
        .map_err(|_| Malformed("inclusion proof is not CBOR"))?;
    if !reader.is_empty() {
        return Err(Malformed("trailing inclusion proof data"));
    }
    let arr = value.as_array().filter(|a| a.len() == 3).ok_or(Malformed(
        "inclusion proof is not [tree_size, leaf_index, path]",
    ))?;
    let uint = |v: &CborValue| {
        v.as_integer()
            .map(i128::from)
            .filter(|n| *n >= 0 && *n <= i128::from(u64::MAX))
            .map(|n| n as u64)
    };
    let tree_size = uint(&arr[0]).ok_or(Malformed("bad tree_size"))?;
    let leaf_index = uint(&arr[1]).ok_or(Malformed("bad leaf_index"))?;
    let raw_path = arr[2]
        .as_array()
        .filter(|p| p.len() <= MAX_AUDIT_PATH)
        .ok_or(Malformed("bad audit path"))?;
    let path = raw_path
        .iter()
        .map(|n| {
            n.as_bytes()
                .and_then(|b| <[u8; 32]>::try_from(b.as_slice()).ok())
        })
        .collect::<Option<Vec<_>>>()
        .ok_or(Malformed("audit path element is not 32 bytes"))?;
    if tree_size == 0 || tree_size > MAX_TREE_SIZE || leaf_index >= tree_size {
        return Err(Malformed("invalid tree size or leaf index"));
    }
    if path.len() as u64 != expected_path_len(tree_size, leaf_index) {
        return Err(Malformed("audit path length does not match tree"));
    }
    Ok(InclusionProof {
        tree_size,
        leaf_index,
        path,
    })
}

/// Check that `receipt` is a receipt this verifier reads, without a key.
pub fn check_form(receipt: &[u8]) -> Result<(), ReceiptError> {
    parse(receipt).map(|_| ())
}

/// Verify that `receipt` proves `leaf_entry` under `service_key`: the
/// inclusion proof leads from the leaf to a root, and the service's signature
/// covers that root.
pub fn verify(
    receipt: &[u8],
    leaf_entry: &[u8],
    service_key: &VerifyingKey,
) -> Result<(), ReceiptError> {
    let parsed = parse(receipt)?;
    let sig: [u8; 64] = parsed
        .sign1
        .signature
        .as_slice()
        .try_into()
        .map_err(|_| ReceiptError::Malformed("signature is not 64 bytes"))?;
    // Proof order is not authenticated. Any structurally valid candidate may
    // prove this entry under the service's signed root.
    for proof in &parsed.proofs {
        if let Some(root) =
            root_from_inclusion_proof(leaf_entry, proof.leaf_index, proof.tree_size, &proof.path)
        {
            let tbs = parsed.sign1.tbs_detached_data(&root, &[]);
            if service_key
                .verify_strict(&tbs, &Signature::from_bytes(&sig))
                .is_ok()
            {
                return Ok(());
            }
        }
    }
    Err(ReceiptError::NotForThisLeaf)
}

fn leaf_hash(entry: &[u8]) -> [u8; 32] {
    let mut h = Sha256::new();
    h.update([0x00]);
    h.update(entry);
    h.finalize().into()
}

fn node_hash(left: &[u8; 32], right: &[u8; 32]) -> [u8; 32] {
    let mut h = Sha256::new();
    h.update([0x01]);
    h.update(left);
    h.update(right);
    h.finalize().into()
}

fn largest_pow2_below(n: u64) -> u64 {
    let mut k = 1u64;
    while k * 2 < n {
        k *= 2;
    }
    k
}

fn expected_path_len(tree_size: u64, index: u64) -> u64 {
    let (mut n, mut size, mut m) = (0u64, tree_size, index);
    while size > 1 {
        let k = largest_pow2_below(size);
        if m < k {
            size = k;
        } else {
            size -= k;
            m -= k;
        }
        n += 1;
    }
    n
}

/// RFC 9162 §2.1.3.2: the root an inclusion proof leads to from `leaf_entry`.
pub fn root_from_inclusion_proof(
    leaf_entry: &[u8],
    index: u64,
    tree_size: u64,
    path: &[[u8; 32]],
) -> Option<[u8; 32]> {
    if tree_size == 0 || index >= tree_size || tree_size > MAX_TREE_SIZE {
        return None;
    }
    if path.len() as u64 != expected_path_len(tree_size, index) {
        return None;
    }
    fn fold(
        size: u64,
        m: u64,
        target: &[u8; 32],
        siblings: &mut Vec<[u8; 32]>,
    ) -> Option<[u8; 32]> {
        if size == 1 {
            return Some(*target);
        }
        let sibling = siblings.pop()?;
        let k = largest_pow2_below(size);
        if m < k {
            Some(node_hash(&fold(k, m, target, siblings)?, &sibling))
        } else {
            Some(node_hash(
                &sibling,
                &fold(size - k, m - k, target, siblings)?,
            ))
        }
    }
    let mut siblings = path.to_vec();
    let root = fold(tree_size, index, &leaf_hash(leaf_entry), &mut siblings)?;
    siblings.is_empty().then_some(root)
}
