//! Signing identities, [`sign`] and [`seal`], as Go's `sign.go`.

use coset::{CoseSign1, Header, HeaderBuilder, ProtectedHeader, TaggedCborSerializable};
use ed25519_dalek::{Signature, Signer, SigningKey, Verifier, VerifyingKey};
use serde_json::Value;
use std::sync::Arc;

use super::build::build;
use super::constants::CONTENT_TYPE;
use super::construction::build_composition;
use super::model::{BuiltPayload, ComputeAttestation, SealInput, Sealed};
use super::{digest_json, verify_capsule, ProduceError};

/// An EdDSA (Ed25519) signer, such as a KMS or HSM client, that signs the
/// Producer Envelope's COSE `Sig_structure` bytes.
pub trait EnvelopeSigner: Send + Sync {
    /// The 64-byte Ed25519 signature over `message`.
    fn sign_ed25519(&self, message: &[u8]) -> Result<[u8; 64], String>;
}

impl EnvelopeSigner for SigningKey {
    fn sign_ed25519(&self, message: &[u8]) -> Result<[u8; 64], String> {
        Ok(Signer::sign(self, message).to_bytes())
    }
}

/// An immutable signer and its raw Ed25519 public key. Keeping the pair
/// together means the key in the envelope's `kid` is the key that signed.
#[derive(Clone)]
pub struct SigningIdentity {
    signer: Arc<dyn EnvelopeSigner>,
    public_key: [u8; 32],
}

impl SigningIdentity {
    /// A local Ed25519 identity.
    pub fn from_ed25519(signing_key: SigningKey) -> Self {
        let public_key = signing_key.verifying_key().to_bytes();
        Self {
            signer: Arc::new(signing_key),
            public_key,
        }
    }

    /// Binds an external Ed25519 signer to its raw public key. A signer whose
    /// signatures do not verify under `public_key` is refused when it signs.
    pub fn new(signer: Arc<dyn EnvelopeSigner>, public_key: [u8; 32]) -> Self {
        Self { signer, public_key }
    }

    /// The raw 32-byte Ed25519 public key carried as the envelope's `kid`.
    pub fn public_key(&self) -> [u8; 32] {
        self.public_key
    }
}

impl std::fmt::Debug for SigningIdentity {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SigningIdentity")
            .field("public_key", &hex::encode(self.public_key))
            .finish_non_exhaustive()
    }
}

/// Create one Producer Envelope over an already-built Capsule's ID, after
/// checking that the Capsule bytes verify and carry that ID.
pub fn sign(built: &BuiltPayload, identity: &SigningIdentity) -> Result<Vec<u8>, ProduceError> {
    if !agent_action_capsule::verify::is_hex64(&built.capsule_id) {
        return Err(ProduceError::invalid(
            "Capsule ID must be 64 lowercase hexadecimal characters",
        ));
    }
    let matches = verify_capsule(&built.json)
        .is_ok_and(|verified| verified.capsule_id.as_deref() == Some(built.capsule_id.as_str()));
    if !matches {
        return Err(ProduceError::invalid(
            "built Capsule does not match Capsule ID",
        ));
    }
    let payload = hex::decode(&built.capsule_id).expect("checked as 64 hex");
    sign_capsule_id(payload, identity)
}

fn sign_capsule_id(payload: Vec<u8>, identity: &SigningIdentity) -> Result<Vec<u8>, ProduceError> {
    let header = HeaderBuilder::new()
        .algorithm(coset::iana::Algorithm::EdDSA)
        .content_type(CONTENT_TYPE.to_string())
        .key_id(identity.public_key.to_vec())
        .build();
    let mut envelope = CoseSign1 {
        protected: ProtectedHeader {
            original_data: Some(producer_protected_headers(&identity.public_key)),
            header,
        },
        unprotected: Header::default(),
        payload: Some(payload),
        signature: Vec::new(),
    };
    let to_be_signed = envelope.tbs_data(b"");
    let signature = identity
        .signer
        .sign_ed25519(&to_be_signed)
        .map_err(|e| ProduceError::invalid(format!("sign Producer Envelope: {e}")))?;
    let verified = VerifyingKey::from_bytes(&identity.public_key).is_ok_and(|key| {
        key.verify(&to_be_signed, &Signature::from_bytes(&signature))
            .is_ok()
    });
    if !verified {
        return Err(ProduceError::invalid(
            "Producer Envelope signer does not match public key",
        ));
    }
    envelope.signature = signature.to_vec();
    envelope
        .to_tagged_vec()
        .map_err(|e| ProduceError::invalid(format!("encode Producer Envelope: {e}")))
}

/// The protected header in the byte order the cross-runtime corpus froze
/// (content type, raw-public-key `kid`, then EdDSA `alg`), which the Go and
/// TypeScript producers emit. `coset` would write canonical label order, so
/// the bytes are laid out here and carried as the header's original data;
/// `coset` signs and serializes exactly these bytes.
fn producer_protected_headers(public_key: &[u8; 32]) -> Vec<u8> {
    // 0x78 is a text string with a one-byte length: valid while the content
    // type is 24..=255 bytes long. It is a 35-byte constant.
    const _: () = assert!(CONTENT_TYPE.len() >= 24 && CONTENT_TYPE.len() <= 255);
    let mut protected = vec![0xa3, 0x03, 0x78, CONTENT_TYPE.len() as u8];
    protected.extend_from_slice(CONTENT_TYPE.as_bytes());
    protected.extend_from_slice(&[0x04, 0x58, 32]);
    protected.extend_from_slice(public_key);
    protected.extend_from_slice(&[0x01, 0x27]);
    protected
}

/// Digest-commit caller-owned JSON, build one signature-free Capsule, and
/// sign one Producer Envelope over it, through [`digest_json`], [`build`] or
/// [`build_composition`], and [`sign`]. Writes nothing.
pub fn seal(input: SealInput) -> Result<Sealed, ProduceError> {
    if input.capsule.model.is_some() || input.capsule.compute.is_some() {
        return Err(ProduceError::invalid(
            "SealInput model and compute metadata must use Model and Runtime fields",
        ));
    }
    let mut capsule = input.capsule;
    capsule.model = input.model;
    let built = if let Some(members) = &input.members {
        if input.payload.is_some() || input.agent_output.is_some() {
            return Err(ProduceError::invalid(
                "composition Seal must not include payload or agent output",
            ));
        }
        if !input.runtime.is_empty() {
            capsule.compute = Some(ComputeAttestation {
                runtime: input.runtime,
                ..ComputeAttestation::default()
            });
        }
        build_composition(capsule, members)?
    } else {
        let digest = |value: &Option<Value>, label: &str| -> Result<String, ProduceError> {
            match value {
                None => Ok(String::new()),
                Some(value) => digest_json(value)
                    .map_err(|e| ProduceError::invalid(format!("digest {label}: {e}"))),
            }
        };
        let compute = ComputeAttestation {
            agent_input_digest: digest(&input.payload, "agent input")?,
            agent_output_digest: digest(&input.agent_output, "agent output")?,
            runtime: input.runtime,
        };
        if compute != ComputeAttestation::default() {
            capsule.compute = Some(compute);
        }
        build(capsule)?
    };
    let envelope = sign(&built, &input.identity)?;
    Ok(Sealed {
        capsule_id: built.capsule_id,
        payload: built.json,
        envelope,
    })
}
