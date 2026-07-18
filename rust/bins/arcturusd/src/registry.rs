use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;
use std::time::Duration;

use arcturus_auth::{AuthError, RegistryTokenIssuer, StoredGrant, VerifiedArtifact};
use arcturus_contracts::{
    ArtifactLayerReceipt, ArtifactUploadCompletionRequest, ComponentName, Sha256Digest,
};
use serde::Deserialize;
use serde_json::Value;
use sha2::{Digest, Sha256};
use thiserror::Error;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::sync::Semaphore;
use tokio::task::JoinSet;
use tokio::time::timeout;
use url::Url;

const MAX_RESPONSE_HEADERS: usize = 64 * 1024;
const MAX_MANIFEST_BYTES: u64 = 8 * 1024 * 1024;
const MAX_CONFIG_BYTES: u64 = 8 * 1024 * 1024;
const MAX_INDEX_DESCRIPTORS: usize = 256;
const MAX_LAYERS: usize = 256;
const OCI_REVISION_LABEL: &str = "org.opencontainers.image.revision";
const COMPONENT_QUEUE_TIMEOUT: Duration = Duration::from_secs(240);
const COMPONENT_VERIFICATION_TIMEOUT: Duration = Duration::from_secs(300);
const OCI_INDEX_MEDIA_TYPE: &str = "application/vnd.oci.image.index.v1+json";
const DOCKER_INDEX_MEDIA_TYPE: &str = "application/vnd.docker.distribution.manifest.list.v2+json";
const OCI_MANIFEST_MEDIA_TYPE: &str = "application/vnd.oci.image.manifest.v1+json";
const DOCKER_MANIFEST_MEDIA_TYPE: &str = "application/vnd.docker.distribution.manifest.v2+json";
const OCI_CONFIG_MEDIA_TYPE: &str = "application/vnd.oci.image.config.v1+json";
const DOCKER_CONFIG_MEDIA_TYPE: &str = "application/vnd.docker.container.image.v1+json";

#[derive(Clone, Debug)]
pub struct RegistryPolicy {
    pub expected_os: String,
    pub expected_architecture: String,
    pub max_layer_bytes: u64,
    pub max_artifact_bytes: u64,
}

#[derive(Clone, Debug)]
pub struct RegistryVerifier {
    endpoint: Url,
    policy: RegistryPolicy,
}

#[derive(Debug, Error)]
pub enum RegistryVerificationError {
    #[error("registry verifier configuration is invalid: {0}")]
    InvalidConfiguration(String),
    #[error("registry is unavailable: {0}")]
    Unavailable(String),
    #[error("registry returned an invalid response: {0}")]
    InvalidResponse(String),
    #[error("artifact verification failed: {0}")]
    Rejected(String),
    #[error(transparent)]
    Authorization(#[from] AuthError),
    #[error(transparent)]
    Json(#[from] serde_json::Error),
}

#[derive(Debug, Deserialize)]
struct Descriptor {
    #[serde(rename = "mediaType", default)]
    media_type: String,
    digest: Sha256Digest,
    size: u64,
    #[serde(default)]
    platform: Option<Platform>,
}

#[derive(Debug, Deserialize)]
struct Platform {
    os: String,
    architecture: String,
}

#[derive(Debug, Deserialize)]
struct ManifestDocument {
    #[serde(rename = "schemaVersion")]
    schema_version: u32,
    #[serde(rename = "mediaType", default)]
    media_type: String,
    #[serde(default)]
    config: Option<Descriptor>,
    #[serde(default)]
    layers: Option<Vec<Descriptor>>,
    #[serde(default)]
    manifests: Option<Vec<Descriptor>>,
}

struct ResponseHead {
    stream: TcpStream,
    status: u16,
    headers: BTreeMap<String, String>,
    body_prefix: Vec<u8>,
}

impl RegistryVerifier {
    pub fn new(endpoint: &str, policy: RegistryPolicy) -> Result<Self, RegistryVerificationError> {
        let endpoint = Url::parse(endpoint)
            .map_err(|error| RegistryVerificationError::InvalidConfiguration(error.to_string()))?;
        if endpoint.scheme() != "http" {
            return Err(RegistryVerificationError::InvalidConfiguration(
                "the internal Distribution endpoint must use loopback HTTP".into(),
            ));
        }
        let host = endpoint.host_str().ok_or_else(|| {
            RegistryVerificationError::InvalidConfiguration("registry host is missing".into())
        })?;
        if !matches!(host, "127.0.0.1" | "localhost" | "::1") {
            return Err(RegistryVerificationError::InvalidConfiguration(
                "the internal Distribution endpoint must be loopback-only".into(),
            ));
        }
        if endpoint.path() != "/" || endpoint.query().is_some() || endpoint.fragment().is_some() {
            return Err(RegistryVerificationError::InvalidConfiguration(
                "the internal Distribution endpoint must not contain a path, query, or fragment"
                    .into(),
            ));
        }
        if policy.max_layer_bytes == 0 || policy.max_artifact_bytes == 0 {
            return Err(RegistryVerificationError::InvalidConfiguration(
                "artifact size limits must be positive".into(),
            ));
        }
        if policy.max_artifact_bytes < policy.max_layer_bytes {
            return Err(RegistryVerificationError::InvalidConfiguration(
                "the artifact size limit must be at least the layer size limit".into(),
            ));
        }
        Ok(Self { endpoint, policy })
    }

    pub async fn verify(
        &self,
        grant: &StoredGrant,
        request: &ArtifactUploadCompletionRequest,
        token_issuer: &RegistryTokenIssuer,
        verification_slots: Arc<Semaphore>,
        now: i64,
    ) -> Result<Vec<VerifiedArtifact>, RegistryVerificationError> {
        request
            .validate()
            .map_err(|error| RegistryVerificationError::Rejected(error.to_string()))?;
        let requested_repositories: BTreeSet<String> = request
            .components
            .keys()
            .map(|component| format!("{}/{}", grant.service, component))
            .collect();
        if requested_repositories != grant.repositories {
            return Err(RegistryVerificationError::Rejected(
                "completion components must exactly match the upload grant".into(),
            ));
        }
        let mut tasks = JoinSet::new();
        for (component, completion) in &request.components {
            let verifier = self.clone();
            let component = component.clone();
            let repository = format!("{}/{}", grant.service, component);
            let digest = completion.digest.clone();
            let revision = grant.revision.clone();
            let scope = format!("repository:{repository}:pull");
            let token = token_issuer.issue_for_verification(grant, &[scope], now)?;
            let verification_slots = verification_slots.clone();
            tasks.spawn(async move {
                let _permit = timeout(COMPONENT_QUEUE_TIMEOUT, verification_slots.acquire_owned())
                    .await
                    .map_err(|_| {
                        RegistryVerificationError::Unavailable(format!(
                            "timed out waiting to verify {repository}"
                        ))
                    })?
                    .map_err(|_| {
                        RegistryVerificationError::Unavailable(
                            "artifact verification limiter is closed".into(),
                        )
                    })?;
                timeout(
                    COMPONENT_VERIFICATION_TIMEOUT,
                    verifier.verify_component(
                        &component,
                        &repository,
                        &digest,
                        &revision,
                        &token.token,
                    ),
                )
                .await
                .map_err(|_| {
                    RegistryVerificationError::Unavailable(format!(
                        "artifact verification timed out for {repository}"
                    ))
                })?
            });
        }

        let mut artifacts = Vec::with_capacity(request.components.len());
        while let Some(result) = tasks.join_next().await {
            match result {
                Ok(Ok(artifact)) => artifacts.push(artifact),
                Ok(Err(error)) => {
                    tasks.abort_all();
                    return Err(error);
                }
                Err(error) => {
                    tasks.abort_all();
                    return Err(RegistryVerificationError::Unavailable(format!(
                        "artifact verification task failed: {error}"
                    )));
                }
            }
        }
        artifacts.sort_by(|left, right| left.component.cmp(&right.component));
        Ok(artifacts)
    }

    async fn verify_component(
        &self,
        component: &ComponentName,
        repository: &str,
        submitted_digest: &Sha256Digest,
        revision: &str,
        token: &str,
    ) -> Result<VerifiedArtifact, RegistryVerificationError> {
        let top_path = format!("/v2/{repository}/manifests/{submitted_digest}");
        let (top_body, top_headers) = self
            .fetch_bytes(&top_path, token, MAX_MANIFEST_BYTES)
            .await?;
        verify_digest(&top_body, submitted_digest, "submitted manifest")?;
        verify_registry_digest_header(&top_headers, submitted_digest)?;
        let top: ManifestDocument = serde_json::from_slice(&top_body)?;
        if top.schema_version != 2 {
            return Err(RegistryVerificationError::Rejected(
                "OCI manifest schemaVersion must be 2".into(),
            ));
        }

        let manifest = if let Some(manifests) = top.manifests.as_ref() {
            ensure_index_descriptor_count(manifests.len())?;
            if !is_index_media_type(&top.media_type) {
                return Err(RegistryVerificationError::Rejected(format!(
                    "unsupported manifest index mediaType: {}",
                    top.media_type
                )));
            }
            if top.config.is_some() || top.layers.is_some() {
                return Err(RegistryVerificationError::Rejected(
                    "manifest index must not also contain image manifest fields".into(),
                ));
            }
            let matches: Vec<&Descriptor> = manifests
                .iter()
                .filter(|descriptor| {
                    descriptor.platform.as_ref().is_some_and(|platform| {
                        platform.os == self.policy.expected_os
                            && platform.architecture == self.policy.expected_architecture
                    })
                })
                .collect();
            if matches.len() != 1 {
                return Err(RegistryVerificationError::Rejected(format!(
                    "manifest index must contain exactly one {}/{} image; found {}",
                    self.policy.expected_os,
                    self.policy.expected_architecture,
                    matches.len()
                )));
            }
            let descriptor = matches[0];
            if !is_manifest_media_type(&descriptor.media_type) {
                return Err(RegistryVerificationError::Rejected(format!(
                    "unsupported selected manifest mediaType: {}",
                    descriptor.media_type
                )));
            }
            if descriptor.size > MAX_MANIFEST_BYTES {
                return Err(RegistryVerificationError::Rejected(
                    "selected image manifest exceeds the manifest size policy".into(),
                ));
            }
            let path = format!("/v2/{repository}/manifests/{}", descriptor.digest);
            let (body, headers) = self.fetch_bytes(&path, token, MAX_MANIFEST_BYTES).await?;
            verify_descriptor_bytes(&body, descriptor, "selected image manifest")?;
            verify_registry_digest_header(&headers, &descriptor.digest)?;
            let document: ManifestDocument = serde_json::from_slice(&body)?;
            document
        } else {
            top
        };
        if manifest.schema_version != 2 || manifest.manifests.is_some() {
            return Err(RegistryVerificationError::Rejected(
                "selected document is not an OCI/Docker image manifest".into(),
            ));
        }
        if !is_manifest_media_type(&manifest.media_type) {
            return Err(RegistryVerificationError::Rejected(format!(
                "unsupported image manifest mediaType: {}",
                manifest.media_type
            )));
        }
        let config = manifest.config.ok_or_else(|| {
            RegistryVerificationError::Rejected("image manifest config is missing".into())
        })?;
        let layers = manifest.layers.ok_or_else(|| {
            RegistryVerificationError::Rejected("image manifest layers are missing".into())
        })?;
        ensure_layer_count(layers.len())?;
        if !is_config_media_type(&config.media_type) {
            return Err(RegistryVerificationError::Rejected(format!(
                "unsupported image config mediaType: {}",
                config.media_type
            )));
        }
        for layer in &layers {
            if !is_layer_media_type(&layer.media_type) {
                return Err(RegistryVerificationError::Rejected(format!(
                    "unsupported layer mediaType for {}: {}",
                    layer.digest, layer.media_type
                )));
            }
        }
        if config.size > MAX_CONFIG_BYTES {
            return Err(RegistryVerificationError::Rejected(
                "image config exceeds the config size policy".into(),
            ));
        }
        let total_compressed_size = layers.iter().try_fold(config.size, |total, layer| {
            if layer.size > self.policy.max_layer_bytes {
                return Err(RegistryVerificationError::Rejected(format!(
                    "layer {} exceeds the per-layer size policy",
                    layer.digest
                )));
            }
            total
                .checked_add(layer.size)
                .ok_or_else(|| RegistryVerificationError::Rejected("artifact size overflow".into()))
        })?;
        if total_compressed_size > self.policy.max_artifact_bytes {
            return Err(RegistryVerificationError::Rejected(format!(
                "artifact compressed size {total_compressed_size} exceeds policy {}",
                self.policy.max_artifact_bytes
            )));
        }

        let config_path = format!("/v2/{repository}/blobs/{}", config.digest);
        let (config_body, _) = self
            .fetch_bytes(&config_path, token, MAX_CONFIG_BYTES)
            .await?;
        verify_descriptor_bytes(&config_body, &config, "image config")?;
        verify_image_config(
            &config_body,
            revision,
            &self.policy.expected_os,
            &self.policy.expected_architecture,
        )?;

        let mut layer_receipts = Vec::with_capacity(layers.len());
        for layer in layers {
            let path = format!("/v2/{repository}/blobs/{}", layer.digest);
            self.verify_streamed_blob(&path, token, &layer).await?;
            layer_receipts.push(ArtifactLayerReceipt {
                digest: layer.digest,
                size: layer.size,
                media_type: layer.media_type,
            });
        }
        Ok(VerifiedArtifact {
            component: component.clone(),
            repository: repository.to_owned(),
            manifest_digest: submitted_digest.clone(),
            platform_os: self.policy.expected_os.clone(),
            platform_architecture: self.policy.expected_architecture.clone(),
            total_compressed_size,
            layers: layer_receipts,
        })
    }

    async fn fetch_bytes(
        &self,
        path: &str,
        token: &str,
        maximum: u64,
    ) -> Result<(Vec<u8>, BTreeMap<String, String>), RegistryVerificationError> {
        let mut response = self.open_get(path, token).await?;
        ensure_success(response.status, path)?;
        let length = content_length(&response.headers)?;
        if length > maximum {
            return Err(RegistryVerificationError::Rejected(format!(
                "registry object at {path} exceeds the configured size limit"
            )));
        }
        if response.body_prefix.len() as u64 > length {
            return Err(RegistryVerificationError::InvalidResponse(
                "registry sent more bytes than Content-Length".into(),
            ));
        }
        let mut body = response.body_prefix;
        let remaining = length - body.len() as u64;
        let remaining = usize::try_from(remaining).map_err(|_| {
            RegistryVerificationError::InvalidResponse(
                "response length exceeds address space".into(),
            )
        })?;
        body.resize(body.len() + remaining, 0);
        if remaining > 0 {
            let start = body.len() - remaining;
            response
                .stream
                .read_exact(&mut body[start..])
                .await
                .map_err(|error| RegistryVerificationError::Unavailable(error.to_string()))?;
        }
        Ok((body, response.headers))
    }

    async fn verify_streamed_blob(
        &self,
        path: &str,
        token: &str,
        descriptor: &Descriptor,
    ) -> Result<(), RegistryVerificationError> {
        let mut response = self.open_get(path, token).await?;
        ensure_success(response.status, path)?;
        let length = content_length(&response.headers)?;
        if length != descriptor.size {
            return Err(RegistryVerificationError::Rejected(format!(
                "blob {} size mismatch: descriptor={}, registry={length}",
                descriptor.digest, descriptor.size
            )));
        }
        if length > self.policy.max_layer_bytes {
            return Err(RegistryVerificationError::Rejected(format!(
                "blob {} exceeds the per-layer size policy",
                descriptor.digest
            )));
        }
        if response.body_prefix.len() as u64 > length {
            return Err(RegistryVerificationError::InvalidResponse(
                "registry sent more bytes than Content-Length".into(),
            ));
        }
        let mut hasher = Sha256::new();
        hasher.update(&response.body_prefix);
        let mut remaining = length - response.body_prefix.len() as u64;
        let mut buffer = [0_u8; 64 * 1024];
        while remaining > 0 {
            let requested =
                usize::try_from(remaining.min(buffer.len() as u64)).unwrap_or(buffer.len());
            let read = response
                .stream
                .read(&mut buffer[..requested])
                .await
                .map_err(|error| RegistryVerificationError::Unavailable(error.to_string()))?;
            if read == 0 {
                return Err(RegistryVerificationError::InvalidResponse(
                    "registry closed the blob response early".into(),
                ));
            }
            hasher.update(&buffer[..read]);
            remaining -= read as u64;
        }
        let actual = digest_from_hasher(hasher);
        if actual != descriptor.digest.as_str() {
            return Err(RegistryVerificationError::Rejected(format!(
                "blob digest mismatch: expected {}, got {actual}",
                descriptor.digest
            )));
        }
        Ok(())
    }

    async fn open_get(
        &self,
        path: &str,
        token: &str,
    ) -> Result<ResponseHead, RegistryVerificationError> {
        if !path.starts_with('/') || path.contains('\r') || path.contains('\n') {
            return Err(RegistryVerificationError::InvalidConfiguration(
                "invalid internal registry request path".into(),
            ));
        }
        let host = self.endpoint.host_str().expect("validated endpoint host");
        let port = self.endpoint.port_or_known_default().unwrap_or(80);
        let address = if host.contains(':') {
            format!("[{host}]:{port}")
        } else {
            format!("{host}:{port}")
        };
        let mut stream = TcpStream::connect(address)
            .await
            .map_err(|error| RegistryVerificationError::Unavailable(error.to_string()))?;
        let host_name = if host.contains(':') {
            format!("[{host}]")
        } else {
            host.to_owned()
        };
        let host_header = if self.endpoint.port().is_some() {
            format!("{host_name}:{port}")
        } else {
            host_name
        };
        let request = format!(
            "GET {path} HTTP/1.1\r\nHost: {host_header}\r\nAuthorization: Bearer {token}\r\nAccept: application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json, application/octet-stream\r\nConnection: close\r\n\r\n"
        );
        stream
            .write_all(request.as_bytes())
            .await
            .map_err(|error| RegistryVerificationError::Unavailable(error.to_string()))?;
        let mut received = Vec::with_capacity(4096);
        let mut buffer = [0_u8; 4096];
        let header_end = loop {
            if let Some(index) = find_header_end(&received) {
                if index > MAX_RESPONSE_HEADERS {
                    return Err(RegistryVerificationError::InvalidResponse(
                        "registry response headers are too large".into(),
                    ));
                }
                break index;
            }
            if received.len() >= MAX_RESPONSE_HEADERS {
                return Err(RegistryVerificationError::InvalidResponse(
                    "registry response headers are too large".into(),
                ));
            }
            let read = stream
                .read(&mut buffer)
                .await
                .map_err(|error| RegistryVerificationError::Unavailable(error.to_string()))?;
            if read == 0 {
                return Err(RegistryVerificationError::InvalidResponse(
                    "registry closed the response before sending headers".into(),
                ));
            }
            received.extend_from_slice(&buffer[..read]);
        };
        let header_bytes = &received[..header_end];
        let header_text = std::str::from_utf8(header_bytes).map_err(|_| {
            RegistryVerificationError::InvalidResponse("registry headers are not UTF-8".into())
        })?;
        let mut lines = header_text.split("\r\n");
        let status_line = lines.next().ok_or_else(|| {
            RegistryVerificationError::InvalidResponse("registry status line is missing".into())
        })?;
        let mut status_parts = status_line.split_whitespace();
        let protocol = status_parts.next().unwrap_or_default();
        let status = status_parts
            .next()
            .and_then(|value| value.parse::<u16>().ok())
            .ok_or_else(|| {
                RegistryVerificationError::InvalidResponse("registry status is invalid".into())
            })?;
        if !protocol.starts_with("HTTP/1.") {
            return Err(RegistryVerificationError::InvalidResponse(
                "registry did not use HTTP/1.x".into(),
            ));
        }
        let mut headers = BTreeMap::new();
        for line in lines {
            if line.is_empty() {
                continue;
            }
            let (name, value) = line.split_once(':').ok_or_else(|| {
                RegistryVerificationError::InvalidResponse("registry header is malformed".into())
            })?;
            let normalized_name = name.trim().to_ascii_lowercase();
            if matches!(
                normalized_name.as_str(),
                "content-length" | "transfer-encoding" | "docker-content-digest"
            ) && headers.contains_key(&normalized_name)
            {
                return Err(RegistryVerificationError::InvalidResponse(format!(
                    "registry repeated security-sensitive header {normalized_name}"
                )));
            }
            headers.insert(normalized_name, value.trim().to_owned());
        }
        if headers.contains_key("transfer-encoding") {
            return Err(RegistryVerificationError::InvalidResponse(
                "chunked registry responses are not accepted on the controlled loopback endpoint"
                    .into(),
            ));
        }
        Ok(ResponseHead {
            stream,
            status,
            headers,
            body_prefix: received[(header_end + 4)..].to_vec(),
        })
    }
}

fn ensure_index_descriptor_count(descriptor_count: usize) -> Result<(), RegistryVerificationError> {
    if descriptor_count > MAX_INDEX_DESCRIPTORS {
        Err(RegistryVerificationError::Rejected(format!(
            "manifest index contains {descriptor_count} descriptors; maximum is {MAX_INDEX_DESCRIPTORS}"
        )))
    } else {
        Ok(())
    }
}

fn ensure_layer_count(layer_count: usize) -> Result<(), RegistryVerificationError> {
    if layer_count > MAX_LAYERS {
        Err(RegistryVerificationError::Rejected(format!(
            "image manifest contains {layer_count} layers; maximum is {MAX_LAYERS}"
        )))
    } else {
        Ok(())
    }
}

fn is_index_media_type(value: &str) -> bool {
    matches!(value, OCI_INDEX_MEDIA_TYPE | DOCKER_INDEX_MEDIA_TYPE)
}

fn is_manifest_media_type(value: &str) -> bool {
    matches!(value, OCI_MANIFEST_MEDIA_TYPE | DOCKER_MANIFEST_MEDIA_TYPE)
}

fn is_config_media_type(value: &str) -> bool {
    matches!(value, OCI_CONFIG_MEDIA_TYPE | DOCKER_CONFIG_MEDIA_TYPE)
}

fn is_layer_media_type(value: &str) -> bool {
    matches!(
        value,
        "application/vnd.oci.image.layer.v1.tar"
            | "application/vnd.oci.image.layer.v1.tar+gzip"
            | "application/vnd.oci.image.layer.v1.tar+zstd"
            | "application/vnd.oci.image.layer.nondistributable.v1.tar"
            | "application/vnd.oci.image.layer.nondistributable.v1.tar+gzip"
            | "application/vnd.oci.image.layer.nondistributable.v1.tar+zstd"
            | "application/vnd.docker.image.rootfs.diff.tar.gzip"
            | "application/vnd.docker.image.rootfs.foreign.diff.tar.gzip"
    )
}

fn find_header_end(bytes: &[u8]) -> Option<usize> {
    bytes.windows(4).position(|window| window == b"\r\n\r\n")
}

fn content_length(headers: &BTreeMap<String, String>) -> Result<u64, RegistryVerificationError> {
    headers
        .get("content-length")
        .and_then(|value| value.parse::<u64>().ok())
        .ok_or_else(|| {
            RegistryVerificationError::InvalidResponse(
                "registry response must contain a valid Content-Length".into(),
            )
        })
}

fn ensure_success(status: u16, path: &str) -> Result<(), RegistryVerificationError> {
    if status == 200 {
        Ok(())
    } else if status == 404 {
        Err(RegistryVerificationError::Rejected(format!(
            "registry object was not found at {path}"
        )))
    } else {
        Err(RegistryVerificationError::Unavailable(format!(
            "registry returned HTTP {status} for {path}"
        )))
    }
}

fn verify_descriptor_bytes(
    body: &[u8],
    descriptor: &Descriptor,
    label: &str,
) -> Result<(), RegistryVerificationError> {
    if body.len() as u64 != descriptor.size {
        return Err(RegistryVerificationError::Rejected(format!(
            "{label} size mismatch: descriptor={}, actual={}",
            descriptor.size,
            body.len()
        )));
    }
    verify_digest(body, &descriptor.digest, label)
}

fn verify_digest(
    body: &[u8],
    expected: &Sha256Digest,
    label: &str,
) -> Result<(), RegistryVerificationError> {
    let actual = digest_bytes(body);
    if actual == expected.as_str() {
        Ok(())
    } else {
        Err(RegistryVerificationError::Rejected(format!(
            "{label} digest mismatch: expected {expected}, got {actual}"
        )))
    }
}

fn verify_registry_digest_header(
    headers: &BTreeMap<String, String>,
    expected: &Sha256Digest,
) -> Result<(), RegistryVerificationError> {
    if let Some(actual) = headers.get("docker-content-digest") {
        if actual != expected.as_str() {
            return Err(RegistryVerificationError::Rejected(format!(
                "registry digest header mismatch: expected {expected}, got {actual}"
            )));
        }
    }
    Ok(())
}

fn verify_image_config(
    body: &[u8],
    expected_revision: &str,
    expected_os: &str,
    expected_architecture: &str,
) -> Result<(), RegistryVerificationError> {
    let value: Value = serde_json::from_slice(body)?;
    let os = value.get("os").and_then(Value::as_str).unwrap_or_default();
    let architecture = value
        .get("architecture")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if os != expected_os || architecture != expected_architecture {
        return Err(RegistryVerificationError::Rejected(format!(
            "image config platform {os}/{architecture} does not match expected {expected_os}/{expected_architecture}"
        )));
    }
    let labels = value
        .get("config")
        .and_then(Value::as_object)
        .and_then(|config| config.get("Labels").or_else(|| config.get("labels")))
        .and_then(Value::as_object);
    let revision = labels
        .and_then(|labels| labels.get(OCI_REVISION_LABEL))
        .and_then(Value::as_str)
        .unwrap_or_default();
    if revision != expected_revision {
        return Err(RegistryVerificationError::Rejected(format!(
            "image revision label must equal upload revision {expected_revision}"
        )));
    }
    Ok(())
}

fn digest_bytes(body: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(body);
    digest_from_hasher(hasher)
}

fn digest_from_hasher(hasher: Sha256) -> String {
    let digest = hasher.finalize();
    let mut result = String::with_capacity(71);
    result.push_str("sha256:");
    for byte in digest {
        use std::fmt::Write;
        write!(result, "{byte:02x}").expect("writing to a String cannot fail");
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn verifier_requires_loopback_http() {
        let policy = RegistryPolicy {
            expected_os: "linux".into(),
            expected_architecture: "amd64".into(),
            max_layer_bytes: 100,
            max_artifact_bytes: 200,
        };
        assert!(RegistryVerifier::new("http://127.0.0.1:5000", policy.clone()).is_ok());
        assert!(RegistryVerifier::new("https://registry.example.org", policy.clone()).is_err());
        assert!(RegistryVerifier::new("http://10.0.0.1:5000", policy).is_err());
    }

    #[test]
    fn manifest_descriptor_counts_are_bounded() {
        ensure_index_descriptor_count(MAX_INDEX_DESCRIPTORS).unwrap();
        assert!(ensure_index_descriptor_count(MAX_INDEX_DESCRIPTORS + 1).is_err());
        ensure_layer_count(MAX_LAYERS).unwrap();
        assert!(ensure_layer_count(MAX_LAYERS + 1).is_err());
    }

    #[test]
    fn image_config_binds_platform_and_revision() {
        let body = serde_json::to_vec(&serde_json::json!({
            "os": "linux",
            "architecture": "amd64",
            "config": {"Labels": {(OCI_REVISION_LABEL): "a".repeat(40)}}
        }))
        .unwrap();
        verify_image_config(&body, &"a".repeat(40), "linux", "amd64").unwrap();
        assert!(verify_image_config(&body, &"b".repeat(40), "linux", "amd64").is_err());
        assert!(verify_image_config(&body, &"a".repeat(40), "linux", "arm64").is_err());
    }
}
