//! The contract's error codes.

/// A store failure. Every variant except [`ArtifactError::Storage`] carries one
/// of the contract's error codes ([`ArtifactError::code`]).
#[derive(Debug, thiserror::Error)]
pub enum ArtifactError {
    /// Malformed input or configuration, or a record that fails authentication.
    #[error("invalid artifact record: {0}")]
    Invalid(String),
    /// A different record already exists under this Capsule ID.
    #[error("immutable record conflict")]
    Conflict,
    /// No record with this ID in the namespace.
    #[error("capsule not found")]
    NotFound,
    /// The originals were purged, or the caller tried to write a purged state.
    #[error("originals were purged")]
    Purged,
    /// A bound original does not match the Capsule's digest.
    #[error("original does not match capsule digest: {0}")]
    DigestMismatch(String),
    /// The Producer Envelope's signer is not trusted.
    #[error("producer signer is not trusted")]
    UntrustedSigner,
    /// A stored or supplied checksum does not match the bytes.
    #[error("stored record integrity failure: {0}")]
    Corrupt(String),
    /// A storage or driver failure, outside the contract's codes.
    #[error("storage: {0}")]
    Storage(#[source] Box<dyn std::error::Error + Send + Sync>),
}

impl ArtifactError {
    /// The contract error code, or `None` for a storage failure.
    pub fn code(&self) -> Option<&'static str> {
        Some(match self {
            ArtifactError::Invalid(_) => "invalid",
            ArtifactError::Conflict => "conflict",
            ArtifactError::NotFound => "not_found",
            ArtifactError::Purged => "purged",
            ArtifactError::DigestMismatch(_) => "digest_mismatch",
            ArtifactError::UntrustedSigner => "untrusted_signer",
            ArtifactError::Corrupt(_) => "corrupt",
            ArtifactError::Storage(_) => return None,
        })
    }

    pub(crate) fn storage(error: impl std::error::Error + Send + Sync + 'static) -> Self {
        ArtifactError::Storage(Box::new(error))
    }
}

/// The result of a store operation.
pub type Result<T> = std::result::Result<T, ArtifactError>;
