use std::collections::{BTreeMap, BTreeSet};
use std::fmt::{self, Display};

use serde::{Deserialize, Serialize};
use serde_json::Value;
use thiserror::Error;

pub const SERVICE_RELEASE_API_VERSION: &str = "arcturus.u128.org/v2";
pub const SERVICE_RELEASE_KIND: &str = "ServiceRelease";
pub const MAX_ARTIFACT_UPLOAD_COMPONENTS: usize = 32;

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum ContractError {
    #[error("{field} must be a lowercase DNS-style name")]
    InvalidName { field: &'static str },
    #[error("revision must be a 40-character Git SHA")]
    InvalidRevision,
    #[error("artifact upload must contain at least one component")]
    EmptyComponents,
    #[error("artifact upload components must be unique")]
    DuplicateComponents,
    #[error("artifact upload must not contain more than 32 components")]
    TooManyComponents,
    #[error("digest must use lowercase sha256:<64 hex> format")]
    InvalidDigest,
    #[error("unsupported release apiVersion: {0}")]
    UnsupportedApiVersion(String),
    #[error("unsupported release kind: {0}")]
    UnsupportedKind(String),
}

fn is_ascii_lowercase_or_digit(byte: u8) -> bool {
    byte.is_ascii_lowercase() || byte.is_ascii_digit()
}

fn validate_name(value: &str, field: &'static str) -> Result<(), ContractError> {
    let bytes = value.as_bytes();
    let valid = !bytes.is_empty()
        && bytes.len() <= 63
        && is_ascii_lowercase_or_digit(bytes[0])
        && bytes
            .iter()
            .skip(1)
            .all(|byte| is_ascii_lowercase_or_digit(*byte) || *byte == b'-');
    if valid {
        Ok(())
    } else {
        Err(ContractError::InvalidName { field })
    }
}

macro_rules! dns_name_type {
    ($name:ident, $field:literal) => {
        #[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize, Deserialize)]
        #[serde(try_from = "String", into = "String")]
        pub struct $name(String);

        impl $name {
            pub fn as_str(&self) -> &str {
                &self.0
            }
        }

        impl TryFrom<String> for $name {
            type Error = ContractError;

            fn try_from(value: String) -> Result<Self, Self::Error> {
                validate_name(&value, $field)?;
                Ok(Self(value))
            }
        }

        impl From<$name> for String {
            fn from(value: $name) -> Self {
                value.0
            }
        }

        impl Display for $name {
            fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                self.0.fmt(formatter)
            }
        }
    };
}

dns_name_type!(ServiceName, "service");
dns_name_type!(ComponentName, "component");

#[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize, Deserialize)]
#[serde(try_from = "String", into = "String")]
pub struct Revision(String);

impl Revision {
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl TryFrom<String> for Revision {
    type Error = ContractError;

    fn try_from(value: String) -> Result<Self, Self::Error> {
        if value.len() != 40 || !value.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return Err(ContractError::InvalidRevision);
        }
        Ok(Self(value.to_ascii_lowercase()))
    }
}

impl From<Revision> for String {
    fn from(value: Revision) -> Self {
        value.0
    }
}

impl Display for Revision {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.fmt(formatter)
    }
}

#[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize, Deserialize)]
#[serde(try_from = "String", into = "String")]
pub struct Sha256Digest(String);

impl Sha256Digest {
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl TryFrom<String> for Sha256Digest {
    type Error = ContractError;

    fn try_from(value: String) -> Result<Self, Self::Error> {
        let Some(hex) = value.strip_prefix("sha256:") else {
            return Err(ContractError::InvalidDigest);
        };
        if hex.len() != 64
            || !hex
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        {
            return Err(ContractError::InvalidDigest);
        }
        Ok(Self(value))
    }
}

impl From<Sha256Digest> for String {
    fn from(value: Sha256Digest) -> Self {
        value.0
    }
}

impl Display for Sha256Digest {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.fmt(formatter)
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct HealthResponse {
    pub status: String,
    pub service: String,
    pub version: String,
    pub features: Vec<String>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct ServiceAccessResponse {
    pub status: String,
    pub service: ServiceName,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct ArtifactUploadRequest {
    pub service: ServiceName,
    pub revision: Revision,
    pub components: Vec<ComponentName>,
}

impl ArtifactUploadRequest {
    pub fn validate(&self) -> Result<(), ContractError> {
        if self.components.is_empty() {
            return Err(ContractError::EmptyComponents);
        }
        if self.components.len() > MAX_ARTIFACT_UPLOAD_COMPONENTS {
            return Err(ContractError::TooManyComponents);
        }
        let unique: BTreeSet<_> = self.components.iter().collect();
        if unique.len() != self.components.len() {
            return Err(ContractError::DuplicateComponents);
        }
        Ok(())
    }
}

#[derive(Clone, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct UploadCredential {
    pub username: String,
    pub secret: String,
}

#[derive(Clone, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ArtifactUploadGrant {
    pub upload_id: String,
    pub registry: String,
    pub repositories: BTreeMap<ComponentName, String>,
    pub expires_at: String,
    pub credential: UploadCredential,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ArtifactUploadComponentCompletion {
    pub digest: Sha256Digest,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ArtifactUploadCompletionRequest {
    pub components: BTreeMap<ComponentName, ArtifactUploadComponentCompletion>,
}

impl ArtifactUploadCompletionRequest {
    pub fn validate(&self) -> Result<(), ContractError> {
        if self.components.is_empty() {
            return Err(ContractError::EmptyComponents);
        }
        if self.components.len() > MAX_ARTIFACT_UPLOAD_COMPONENTS {
            return Err(ContractError::TooManyComponents);
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ArtifactLayerReceipt {
    pub digest: Sha256Digest,
    pub size: u64,
    pub media_type: String,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ArtifactReceipt {
    pub id: String,
    pub upload_id: String,
    pub service: ServiceName,
    pub component: ComponentName,
    pub repository: String,
    pub revision: Revision,
    pub manifest_digest: Sha256Digest,
    pub platform_os: String,
    pub platform_architecture: String,
    pub total_compressed_size: u64,
    pub accepted_at: String,
    pub layers: Vec<ArtifactLayerReceipt>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ArtifactUploadCompletionResponse {
    pub upload_id: String,
    pub status: String,
    pub receipts: Vec<ArtifactReceipt>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct ReleaseMetadata {
    pub name: ServiceName,
    pub revision: Revision,
    #[serde(rename = "deploymentId", skip_serializing_if = "Option::is_none")]
    pub deployment_id: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ServiceReleaseEnvelope {
    #[serde(rename = "apiVersion")]
    pub api_version: String,
    pub kind: String,
    pub metadata: ReleaseMetadata,
    pub spec: Value,
}

impl ServiceReleaseEnvelope {
    pub fn validate(&self) -> Result<(), ContractError> {
        if self.api_version != SERVICE_RELEASE_API_VERSION {
            return Err(ContractError::UnsupportedApiVersion(
                self.api_version.clone(),
            ));
        }
        if self.kind != SERVICE_RELEASE_KIND {
            return Err(ContractError::UnsupportedKind(self.kind.clone()));
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct ApiErrorBody {
    pub code: String,
    pub message: String,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct ApiErrorResponse {
    pub status: String,
    pub error: ApiErrorBody,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn revision_is_normalized_to_lowercase() {
        let revision = Revision::try_from("A".repeat(40)).expect("valid revision");
        assert_eq!(revision.as_str(), "a".repeat(40));
    }

    #[test]
    fn digest_must_be_canonical_lowercase_sha256() {
        assert!(Sha256Digest::try_from(format!("sha256:{}", "a".repeat(64))).is_ok());
        assert_eq!(
            Sha256Digest::try_from(format!("sha256:{}", "A".repeat(64))),
            Err(ContractError::InvalidDigest)
        );
    }

    #[test]
    fn upload_components_must_be_unique() {
        let request = ArtifactUploadRequest {
            service: ServiceName::try_from("example-service".to_owned()).unwrap(),
            revision: Revision::try_from("a".repeat(40)).unwrap(),
            components: vec![
                ComponentName::try_from("web".to_owned()).unwrap(),
                ComponentName::try_from("web".to_owned()).unwrap(),
            ],
        };
        assert_eq!(request.validate(), Err(ContractError::DuplicateComponents));
    }

    #[test]
    fn names_reject_registry_paths() {
        let error = ServiceName::try_from("example/service".to_owned()).unwrap_err();
        assert_eq!(error, ContractError::InvalidName { field: "service" });
    }
}
