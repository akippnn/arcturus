use std::collections::{BTreeMap, BTreeSet};
use std::fmt::{self, Display};

use serde::{Deserialize, Serialize};
use serde_json::Value;
use thiserror::Error;

pub const SERVICE_RELEASE_API_VERSION: &str = "arcturus.u128.org/v2";
pub const SERVICE_RELEASE_KIND: &str = "ServiceRelease";
pub const FLEET_API_VERSION: &str = "infrastructure.arcturus.dev/v1alpha1";
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
dns_name_type!(WorkerId, "worker");
dns_name_type!(ResourceId, "resource");

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

/// Fleet contracts wrap ServiceRelease v2 without interpreting its spec in Rust.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct WorkerAttestations {
    #[serde(default)]
    pub capabilities: BTreeSet<String>,
    #[serde(default)]
    pub topology: BTreeMap<String, String>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct WorkerRegistrationRequest {
    pub worker_id: WorkerId,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub display_name: Option<String>,
    #[serde(flatten)]
    pub attestations: WorkerAttestations,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct WorkerRegistration {
    pub worker_id: WorkerId,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub display_name: Option<String>,
    pub enabled: bool,
    #[serde(flatten)]
    pub attestations: WorkerAttestations,
    pub enrolled_at: i64,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct WorkerEnrollmentResponse {
    pub worker: WorkerRegistration,
    pub credential: String,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct StoragePoolInventory {
    pub pool_id: String,
    pub total_bytes: u64,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct WorkerInventory {
    pub inventory_generation: u64,
    pub architecture: String,
    pub operating_system: String,
    pub logical_cpu_count: u32,
    pub total_memory_bytes: u64,
    #[serde(default)]
    pub storage_pools: Vec<StoragePoolInventory>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct WorkerPressure {
    pub pressure_sequence: u64,
    pub observed_at: i64,
    pub available_memory_bytes: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cpu_utilization_basis_points: Option<u16>,
    #[serde(default)]
    pub storage_free_bytes: BTreeMap<String, u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub thermal_state: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct WorkerHeartbeat {
    pub worker_id: WorkerId,
    pub agent_instance_id: String,
    pub inventory: WorkerInventory,
    pub pressure: WorkerPressure,
    pub accepted_assignment_set_revision: u64,
    #[serde(default)]
    pub accepted_assignments: BTreeMap<ServiceName, u64>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct PlacementRequirements {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub worker_id: Option<WorkerId>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub architecture: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub minimum_total_memory_bytes: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub minimum_available_memory_bytes: Option<u64>,
    #[serde(default)]
    pub required_capabilities: BTreeSet<String>,
    #[serde(default)]
    pub required_topology: BTreeMap<String, String>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub enum ResourceOwnership {
    Imported,
    Managed,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ExistingPodmanSecretMaterial {
    pub secret_name: String,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ResourceRecord {
    pub api_version: String,
    pub kind: String,
    pub resource_id: ResourceId,
    pub generation: u64,
    pub ownership: ResourceOwnership,
    pub resource_type: String,
    pub protocol: String,
    #[serde(default)]
    pub binding_material: BTreeMap<String, ExistingPodmanSecretMaterial>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ResourceProjection {
    pub material: String,
    pub component: ComponentName,
    pub secret_name: String,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ResourceBinding {
    pub name: String,
    pub resource_id: ResourceId,
    pub resource_generation: u64,
    #[serde(default)]
    pub projections: Vec<ResourceProjection>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ReleaseSecretUse {
    pub component: ComponentName,
    pub secret_name: String,
    pub secret_type: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ReleaseCharacteristics {
    pub service_name: ServiceName,
    pub release_digest: Sha256Digest,
    #[serde(default)]
    pub component_modes: BTreeSet<String>,
    pub read_only_local_volumes: u32,
    pub writable_local_volumes: u32,
    pub has_legacy_migration: bool,
    #[serde(default)]
    pub secret_uses: Vec<ReleaseSecretUse>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub enum Movability {
    Movable,
    Fixed,
    Unknown,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub enum OverlapSafety {
    Safe,
    Unsafe,
    Unknown,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub enum AuthoritativeState {
    NoneOrExternal,
    ReconstructableLocal,
    NodeLocal,
    Unknown,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub enum RequirementState {
    NotRequired,
    Required,
    Unknown,
}

#[derive(Clone, Debug, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub enum TransitionClaim {
    DuplicateExecutionSafe,
    LocalStateReconstructable,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TransitionAttestation {
    pub claims: BTreeSet<TransitionClaim>,
    pub rationale: String,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TransitionPolicy {
    pub movability: Movability,
    pub overlap: OverlapSafety,
    pub authoritative_state: AuthoritativeState,
    pub migration: RequirementState,
    pub fencing: RequirementState,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub attestation: Option<TransitionAttestation>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct WorkloadIntent {
    pub api_version: String,
    pub kind: String,
    pub workload_name: ServiceName,
    pub intent_generation: u64,
    pub release: ServiceReleaseEnvelope,
    pub release_digest: Sha256Digest,
    pub release_characteristics: ReleaseCharacteristics,
    pub placement: PlacementRequirements,
    pub transition: TransitionPolicy,
    #[serde(default)]
    pub resource_bindings: Vec<ResourceBinding>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CandidateEvaluation {
    pub worker_id: WorkerId,
    pub eligible: bool,
    #[serde(default)]
    pub refusal_codes: Vec<String>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct PlacementDecision {
    pub decision_id: String,
    pub workload_name: ServiceName,
    pub intent_generation: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub selected_worker_id: Option<WorkerId>,
    pub status: String,
    #[serde(default)]
    pub candidates: Vec<CandidateEvaluation>,
    #[serde(default)]
    pub transition_reasons: Vec<String>,
    pub created_at: i64,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "camelCase")]
pub enum WorkerAssignmentAction {
    EnsurePresent {
        release: ServiceReleaseEnvelope,
        release_digest: Sha256Digest,
        #[serde(default)]
        resource_bindings: Vec<ResourceBinding>,
    },
    EnsureAbsent,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct WorkerAssignment {
    pub assignment_id: String,
    pub worker_id: WorkerId,
    pub workload_name: ServiceName,
    pub assignment_generation: u64,
    pub intent_generation: u64,
    pub placement_decision_id: String,
    pub issued_at: i64,
    #[serde(flatten)]
    pub desired: WorkerAssignmentAction,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct WorkerAssignmentSet {
    pub worker_id: WorkerId,
    pub revision: u64,
    pub changed: bool,
    #[serde(default)]
    pub assignments: Vec<WorkerAssignment>,
    pub poll_after_seconds: u16,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub enum ObservedPhase {
    Pending,
    Reconciling,
    Healthy,
    Degraded,
    Failed,
    Absent,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ObservedError {
    pub code: String,
    pub message: String,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ObservedState {
    pub worker_id: WorkerId,
    pub workload_name: ServiceName,
    pub assignment_generation: u64,
    pub observation_sequence: u64,
    pub observed_at: i64,
    pub phase: ObservedPhase,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub release_digest: Option<Sha256Digest>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub deployment_id: Option<String>,
    #[serde(default)]
    pub units: BTreeMap<String, String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub routing_status: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<ObservedError>,
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
