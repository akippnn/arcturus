use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use arcturus_contracts::{
    ArtifactLayerReceipt, ArtifactReceipt, ArtifactUploadCompletionRequest,
    ArtifactUploadCompletionResponse, ArtifactUploadGrant, ArtifactUploadRequest, ComponentName,
    Revision, ServiceName, Sha256Digest, UploadCredential,
};
use base64::Engine;
use base64::engine::general_purpose::{STANDARD, URL_SAFE, URL_SAFE_NO_PAD};
use ed25519_dalek::{Signer, SigningKey, VerifyingKey};
use rusqlite::{Connection, OptionalExtension, params};
use scrypt::{Params as ScryptParams, scrypt};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;
use thiserror::Error;
use time::OffsetDateTime;
use time::format_description::well_known::Rfc3339;
use uuid::Uuid;

const SECRET_HASH_DOMAIN: &[u8] = b"arcturus-upload-grant-v1\0";
const DEFAULT_TOKEN_SECONDS: i64 = 300;
const VERIFICATION_TOKEN_SECONDS: i64 = 900;
const MIN_TOKEN_SECONDS: i64 = 60;
const MAX_GRANT_SECONDS: i64 = 900;

#[derive(Debug, Error)]
pub enum AuthError {
    #[error("authorization header is missing or malformed")]
    InvalidAuthorization,
    #[error("control-plane token is invalid")]
    InvalidControlToken,
    #[error("control-plane token is not authorized for {0}")]
    ServiceForbidden(String),
    #[error("token database is invalid: {0}")]
    InvalidTokenDatabase(String),
    #[error("upload grant credentials are invalid")]
    InvalidGrantCredentials,
    #[error("upload grant is expired")]
    GrantExpired,
    #[error("upload grant has already been completed")]
    GrantCompleted,
    #[error("upload grant was not found")]
    GrantNotFound,
    #[error("artifact completion does not match the upload grant: {0}")]
    CompletionMismatch(String),
    #[error("stored authorization state is invalid: {0}")]
    InvalidStoredState(String),
    #[error("upload grant has insufficient lifetime remaining")]
    GrantNearExpiry,
    #[error("upload grant lifetime must be between 60 and 900 seconds")]
    InvalidGrantLifetime,
    #[error("registry service does not match the configured audience")]
    InvalidRegistryService,
    #[error("registry scope is invalid: {0}")]
    InvalidScope(String),
    #[error("signing key is invalid: {0}")]
    InvalidSigningKey(String),
    #[error("state database error: {0}")]
    Database(String),
    #[error("serialization error: {0}")]
    Serialization(String),
    #[error("randomness source failed")]
    Randomness,
    #[error("time conversion failed")]
    Time,
    #[error("authorization state lock is poisoned")]
    LockPoisoned,
}

impl From<rusqlite::Error> for AuthError {
    fn from(value: rusqlite::Error) -> Self {
        Self::Database(value.to_string())
    }
}

impl From<serde_json::Error> for AuthError {
    fn from(value: serde_json::Error) -> Self {
        Self::Serialization(value.to_string())
    }
}

#[derive(Clone)]
pub struct ControlTokenVerifier {
    path: PathBuf,
}

impl ControlTokenVerifier {
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self { path: path.into() }
    }

    pub fn authorize(&self, authorization: &str, service: &str) -> Result<(), AuthError> {
        let (_, token) = authorization
            .split_once(' ')
            .filter(|(scheme, token)| scheme.eq_ignore_ascii_case("Bearer") && !token.is_empty())
            .ok_or(AuthError::InvalidAuthorization)?;
        let metadata = fs::metadata(&self.path)
            .map_err(|error| AuthError::InvalidTokenDatabase(error.to_string()))?;
        if metadata.permissions().mode() & 0o077 != 0 {
            return Err(AuthError::InvalidTokenDatabase(
                "token database must not be group- or other-accessible".into(),
            ));
        }
        let payload: Value = serde_json::from_slice(
            &fs::read(&self.path)
                .map_err(|error| AuthError::InvalidTokenDatabase(error.to_string()))?,
        )?;
        let records = match payload {
            Value::Array(records) => records,
            Value::Object(mut object)
                if object.get("version").and_then(Value::as_u64) == Some(2) =>
            {
                object
                    .remove("tokens")
                    .and_then(|value| value.as_array().cloned())
                    .ok_or_else(|| {
                        AuthError::InvalidTokenDatabase("version 2 tokens must be an array".into())
                    })?
            }
            _ => {
                return Err(AuthError::InvalidTokenDatabase(
                    "unsupported token file format".into(),
                ));
            }
        };

        for record in records {
            let Value::Object(record) = record else {
                continue;
            };
            if !record_matches(&record, token)? {
                continue;
            }
            if record_allows_service(&record, service) {
                return Ok(());
            }
            return Err(AuthError::ServiceForbidden(service.to_owned()));
        }
        Err(AuthError::InvalidControlToken)
    }
}

fn record_matches(record: &serde_json::Map<String, Value>, token: &str) -> Result<bool, AuthError> {
    if let Some(candidate) = record.get("token").and_then(Value::as_str) {
        return Ok(constant_time_eq(candidate.as_bytes(), token.as_bytes()));
    }
    if record.get("algorithm").and_then(Value::as_str) != Some("scrypt") {
        return Ok(false);
    }
    let encoded_salt = record
        .get("salt")
        .and_then(Value::as_str)
        .ok_or_else(|| AuthError::InvalidTokenDatabase("scrypt salt is missing".into()))?;
    let encoded_hash = record
        .get("hash")
        .and_then(Value::as_str)
        .ok_or_else(|| AuthError::InvalidTokenDatabase("scrypt hash is missing".into()))?;
    let salt = URL_SAFE
        .decode(encoded_salt)
        .map_err(|error| AuthError::InvalidTokenDatabase(error.to_string()))?;
    let expected = URL_SAFE
        .decode(encoded_hash)
        .map_err(|error| AuthError::InvalidTokenDatabase(error.to_string()))?;
    if expected.is_empty() {
        return Err(AuthError::InvalidTokenDatabase(
            "scrypt hash must not be empty".into(),
        ));
    }
    let params = ScryptParams::new(14, 8, 1, expected.len())
        .map_err(|error| AuthError::InvalidTokenDatabase(error.to_string()))?;
    let mut actual = vec![0_u8; expected.len()];
    scrypt(token.as_bytes(), &salt, &params, &mut actual)
        .map_err(|error| AuthError::InvalidTokenDatabase(error.to_string()))?;
    Ok(constant_time_eq(&actual, &expected))
}

fn record_allows_service(record: &serde_json::Map<String, Value>, service: &str) -> bool {
    let Some(scopes) = record.get("services") else {
        return true;
    };
    match scopes {
        Value::String(value) => value == "*" || value == service,
        Value::Array(values) => values
            .iter()
            .filter_map(Value::as_str)
            .any(|value| value == "*" || value == service),
        _ => false,
    }
}

fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    left.len() == right.len() && bool::from(left.ct_eq(right))
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StoredGrant {
    pub id: String,
    pub username: String,
    pub service: String,
    pub revision: String,
    pub repositories: BTreeSet<String>,
    pub expires_at: i64,
    pub completed_at: Option<i64>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct VerifiedArtifact {
    pub component: ComponentName,
    pub repository: String,
    pub manifest_digest: Sha256Digest,
    pub platform_os: String,
    pub platform_architecture: String,
    pub total_compressed_size: u64,
    pub layers: Vec<ArtifactLayerReceipt>,
}

#[derive(Clone)]
pub struct GrantStore {
    connection: Arc<Mutex<Connection>>,
}

impl GrantStore {
    pub fn open(path: impl AsRef<Path>) -> Result<Self, AuthError> {
        if let Some(parent) = path
            .as_ref()
            .parent()
            .filter(|parent| !parent.as_os_str().is_empty())
        {
            fs::create_dir_all(parent).map_err(|error| AuthError::Database(error.to_string()))?;
            fs::set_permissions(parent, fs::Permissions::from_mode(0o700))
                .map_err(|error| AuthError::Database(error.to_string()))?;
        }
        let connection = Connection::open(path.as_ref())?;
        fs::set_permissions(path.as_ref(), fs::Permissions::from_mode(0o600))
            .map_err(|error| AuthError::Database(error.to_string()))?;
        Self::from_connection(connection)
    }

    pub fn open_in_memory() -> Result<Self, AuthError> {
        Self::from_connection(Connection::open_in_memory()?)
    }

    fn from_connection(connection: Connection) -> Result<Self, AuthError> {
        connection.execute_batch(
            "PRAGMA foreign_keys=ON;\n\
             PRAGMA busy_timeout=5000;\n\
             PRAGMA journal_mode=WAL;\n\
             CREATE TABLE IF NOT EXISTS upload_grants (\n\
               id TEXT PRIMARY KEY,\n\
               username TEXT NOT NULL UNIQUE,\n\
               secret_hash BLOB NOT NULL,\n\
               service TEXT NOT NULL,\n\
               revision TEXT NOT NULL,\n\
               repositories_json TEXT NOT NULL,\n\
               created_at INTEGER NOT NULL,\n\
               expires_at INTEGER NOT NULL,\n\
               revoked_at INTEGER,\n\
               completed_at INTEGER\n\
             );\n\
             CREATE INDEX IF NOT EXISTS idx_upload_grants_username\n\
               ON upload_grants(username);\n\
             CREATE INDEX IF NOT EXISTS idx_upload_grants_expiry\n\
               ON upload_grants(expires_at);\n\
             CREATE TABLE IF NOT EXISTS artifact_receipts (\n\
               id TEXT PRIMARY KEY,\n\
               upload_id TEXT NOT NULL REFERENCES upload_grants(id),\n\
               service TEXT NOT NULL,\n\
               component TEXT NOT NULL,\n\
               repository TEXT NOT NULL,\n\
               revision TEXT NOT NULL,\n\
               manifest_digest TEXT NOT NULL,\n\
               platform_os TEXT NOT NULL,\n\
               platform_architecture TEXT NOT NULL,\n\
               total_compressed_size INTEGER NOT NULL,\n\
               status TEXT NOT NULL CHECK(status='accepted'),\n\
               accepted_at INTEGER NOT NULL,\n\
               UNIQUE(upload_id, component)\n\
             );\n\
             CREATE INDEX IF NOT EXISTS idx_artifact_receipts_deploy_lookup\n\
               ON artifact_receipts(service, component, repository, revision, manifest_digest, status);\n\
             CREATE TABLE IF NOT EXISTS artifact_layers (\n\
               receipt_id TEXT NOT NULL REFERENCES artifact_receipts(id) ON DELETE CASCADE,\n\
               layer_index INTEGER NOT NULL,\n\
               digest TEXT NOT NULL,\n\
               size INTEGER NOT NULL,\n\
               media_type TEXT NOT NULL,\n\
               PRIMARY KEY(receipt_id, layer_index)\n\
             );\n\
             CREATE TABLE IF NOT EXISTS deployment_artifacts (\n\
               deployment_id TEXT NOT NULL,\n\
               receipt_id TEXT NOT NULL REFERENCES artifact_receipts(id),\n\
               PRIMARY KEY(deployment_id, receipt_id)\n\
             );\n\
             CREATE TABLE IF NOT EXISTS artifact_pins (\n\
               service TEXT NOT NULL,\n\
               receipt_id TEXT NOT NULL REFERENCES artifact_receipts(id),\n\
               reason TEXT NOT NULL,\n\
               created_at INTEGER NOT NULL,\n\
               PRIMARY KEY(service, receipt_id, reason)\n\
             );\n\
             CREATE TABLE IF NOT EXISTS artifact_events (\n\
               id INTEGER PRIMARY KEY AUTOINCREMENT,\n\
               upload_id TEXT,\n\
               receipt_id TEXT,\n\
               event_type TEXT NOT NULL,\n\
               created_at INTEGER NOT NULL,\n\
               details_json TEXT NOT NULL DEFAULT '{}'\n\
             );",
        )?;
        let has_completed_at = {
            let mut statement = connection.prepare("PRAGMA table_info(upload_grants)")?;
            let columns = statement
                .query_map([], |row| row.get::<_, String>(1))?
                .collect::<Result<Vec<_>, _>>()?;
            columns.iter().any(|column| column == "completed_at")
        };
        if !has_completed_at {
            connection.execute("ALTER TABLE upload_grants ADD COLUMN completed_at INTEGER", [])?;
        }
        Ok(Self {
            connection: Arc::new(Mutex::new(connection)),
        })
    }

    pub fn create(
        &self,
        request: &ArtifactUploadRequest,
        registry: &str,
        ttl_seconds: i64,
        now: i64,
    ) -> Result<ArtifactUploadGrant, AuthError> {
        request
            .validate()
            .map_err(|error| AuthError::Serialization(error.to_string()))?;
        if !(MIN_TOKEN_SECONDS..=MAX_GRANT_SECONDS).contains(&ttl_seconds) {
            return Err(AuthError::InvalidGrantLifetime);
        }
        let id = Uuid::new_v4().to_string();
        let username = format!("upload-{id}");
        let secret = random_secret()?;
        let expires_at = now.checked_add(ttl_seconds).ok_or(AuthError::Time)?;
        let repositories: BTreeMap<ComponentName, String> = request
            .components
            .iter()
            .cloned()
            .map(|component| {
                let repository = format!("{}/{}", request.service, component);
                (component, repository)
            })
            .collect();
        let repository_set: BTreeSet<String> = repositories.values().cloned().collect();
        let encoded_repositories = serde_json::to_string(&repository_set)?;
        let secret_hash = upload_secret_hash(&username, &secret);
        let connection = self
            .connection
            .lock()
            .map_err(|_| AuthError::LockPoisoned)?;
        connection.execute(
            "INSERT INTO upload_grants\n\
             (id,username,secret_hash,service,revision,repositories_json,created_at,expires_at)\n\
             VALUES (?,?,?,?,?,?,?,?)",
            params![
                &id,
                &username,
                secret_hash.as_slice(),
                request.service.as_str(),
                request.revision.as_str(),
                encoded_repositories,
                now,
                expires_at,
            ],
        )?;
        let expires_at = OffsetDateTime::from_unix_timestamp(expires_at)
            .map_err(|_| AuthError::Time)?
            .format(&Rfc3339)
            .map_err(|_| AuthError::Time)?;
        Ok(ArtifactUploadGrant {
            upload_id: id,
            registry: registry.to_owned(),
            repositories,
            expires_at,
            credential: UploadCredential { username, secret },
        })
    }

    pub fn authenticate(
        &self,
        username: &str,
        secret: &str,
        now: i64,
    ) -> Result<StoredGrant, AuthError> {
        let connection = self
            .connection
            .lock()
            .map_err(|_| AuthError::LockPoisoned)?;
        let row = connection
            .query_row(
                "SELECT id,username,secret_hash,service,revision,repositories_json,expires_at,completed_at\n\
                 FROM upload_grants WHERE username=? AND revoked_at IS NULL",
                [username],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, Vec<u8>>(2)?,
                        row.get::<_, String>(3)?,
                        row.get::<_, String>(4)?,
                        row.get::<_, String>(5)?,
                        row.get::<_, i64>(6)?,
                        row.get::<_, Option<i64>>(7)?,
                    ))
                },
            )
            .optional()?
            .ok_or(AuthError::InvalidGrantCredentials)?;
        if row.7.is_some() {
            return Err(AuthError::GrantCompleted);
        }
        if row.6 <= now {
            return Err(AuthError::GrantExpired);
        }
        let actual = upload_secret_hash(username, secret);
        if !constant_time_eq(&actual, &row.2) {
            return Err(AuthError::InvalidGrantCredentials);
        }
        Ok(StoredGrant {
            id: row.0,
            username: row.1,
            service: row.3,
            revision: row.4,
            repositories: serde_json::from_str(&row.5)?,
            expires_at: row.6,
            completed_at: row.7,
        })
    }

    pub fn get_for_completion(&self, upload_id: &str, now: i64) -> Result<StoredGrant, AuthError> {
        let connection = self
            .connection
            .lock()
            .map_err(|_| AuthError::LockPoisoned)?;
        let grant = load_grant(&connection, upload_id)?.ok_or(AuthError::GrantNotFound)?;
        if grant.expires_at <= now && grant.completed_at.is_none() {
            return Err(AuthError::GrantExpired);
        }
        Ok(grant)
    }

    pub fn existing_completion(
        &self,
        grant: &StoredGrant,
        request: &ArtifactUploadCompletionRequest,
    ) -> Result<Option<ArtifactUploadCompletionResponse>, AuthError> {
        let connection = self
            .connection
            .lock()
            .map_err(|_| AuthError::LockPoisoned)?;
        load_completion(&connection, grant, request)
    }

    pub fn complete(
        &self,
        grant: &StoredGrant,
        request: &ArtifactUploadCompletionRequest,
        artifacts: &[VerifiedArtifact],
        started_at: i64,
        accepted_at: i64,
    ) -> Result<ArtifactUploadCompletionResponse, AuthError> {
        validate_completion_shape(grant, request, artifacts)?;
        let mut connection = self
            .connection
            .lock()
            .map_err(|_| AuthError::LockPoisoned)?;
        let transaction = connection.transaction()?;
        let current = load_grant(&transaction, &grant.id)?.ok_or(AuthError::GrantNotFound)?;
        if let Some(existing) = load_completion(&transaction, &current, request)? {
            transaction.commit()?;
            return Ok(existing);
        }
        if current.completed_at.is_some() {
            return Err(AuthError::GrantCompleted);
        }
        if current.expires_at <= started_at {
            return Err(AuthError::GrantExpired);
        }
        if accepted_at < started_at {
            return Err(AuthError::Time);
        }
        let accepted_at_text = format_timestamp(accepted_at)?;
        let service = ServiceName::try_from(current.service.clone())
            .map_err(|error| AuthError::InvalidStoredState(error.to_string()))?;
        let revision = Revision::try_from(current.revision.clone())
            .map_err(|error| AuthError::InvalidStoredState(error.to_string()))?;
        let mut receipts = Vec::with_capacity(artifacts.len());
        for artifact in artifacts {
            let receipt_id = Uuid::new_v4().to_string();
            let size = i64::try_from(artifact.total_compressed_size).map_err(|_| {
                AuthError::CompletionMismatch("artifact size exceeds SQLite range".into())
            })?;
            transaction.execute(
                "INSERT INTO artifact_receipts \n                 (id,upload_id,service,component,repository,revision,manifest_digest,platform_os,\n                  platform_architecture,total_compressed_size,status,accepted_at) \n                 VALUES (?,?,?,?,?,?,?,?,?,?, 'accepted', ?)",
                params![
                    &receipt_id,
                    &current.id,
                    &current.service,
                    artifact.component.as_str(),
                    &artifact.repository,
                    &current.revision,
                    artifact.manifest_digest.as_str(),
                    &artifact.platform_os,
                    &artifact.platform_architecture,
                    size,
                    accepted_at,
                ],
            )?;
            for (index, layer) in artifact.layers.iter().enumerate() {
                let layer_size = i64::try_from(layer.size).map_err(|_| {
                    AuthError::CompletionMismatch("layer size exceeds SQLite range".into())
                })?;
                transaction.execute(
                    "INSERT INTO artifact_layers \n                     (receipt_id,layer_index,digest,size,media_type) VALUES (?,?,?,?,?)",
                    params![
                        &receipt_id,
                        i64::try_from(index).map_err(|_| {
                            AuthError::CompletionMismatch("too many artifact layers".into())
                        })?,
                        layer.digest.as_str(),
                        layer_size,
                        &layer.media_type,
                    ],
                )?;
            }
            transaction.execute(
                "INSERT INTO artifact_events \n                 (upload_id,receipt_id,event_type,created_at,details_json) \n                 VALUES (?,?, 'artifact.accepted', ?, '{}')",
                params![&current.id, &receipt_id, accepted_at],
            )?;
            receipts.push(ArtifactReceipt {
                id: receipt_id,
                upload_id: current.id.clone(),
                service: service.clone(),
                component: artifact.component.clone(),
                repository: artifact.repository.clone(),
                revision: revision.clone(),
                manifest_digest: artifact.manifest_digest.clone(),
                platform_os: artifact.platform_os.clone(),
                platform_architecture: artifact.platform_architecture.clone(),
                total_compressed_size: artifact.total_compressed_size,
                accepted_at: accepted_at_text.clone(),
                layers: artifact.layers.clone(),
            });
        }
        transaction.execute(
            "UPDATE upload_grants SET completed_at=? WHERE id=? AND completed_at IS NULL",
            params![accepted_at, &current.id],
        )?;
        transaction.execute(
            "INSERT INTO artifact_events (upload_id,event_type,created_at,details_json) \n             VALUES (?, 'upload.completed', ?, '{}')",
            params![&current.id, accepted_at],
        )?;
        transaction.commit()?;
        receipts.sort_by(|left, right| left.component.cmp(&right.component));
        Ok(ArtifactUploadCompletionResponse {
            upload_id: current.id,
            status: "accepted".into(),
            receipts,
        })
    }
}

fn load_grant(
    connection: &Connection,
    upload_id: &str,
) -> Result<Option<StoredGrant>, AuthError> {
    let row = connection
        .query_row(
            "SELECT id,username,service,revision,repositories_json,expires_at,completed_at \n             FROM upload_grants WHERE id=? AND revoked_at IS NULL",
            [upload_id],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, String>(4)?,
                    row.get::<_, i64>(5)?,
                    row.get::<_, Option<i64>>(6)?,
                ))
            },
        )
        .optional()?;
    row.map(|row| {
        Ok(StoredGrant {
            id: row.0,
            username: row.1,
            service: row.2,
            revision: row.3,
            repositories: serde_json::from_str(&row.4)?,
            expires_at: row.5,
            completed_at: row.6,
        })
    })
    .transpose()
}

fn validate_completion_shape(
    grant: &StoredGrant,
    request: &ArtifactUploadCompletionRequest,
    artifacts: &[VerifiedArtifact],
) -> Result<(), AuthError> {
    request
        .validate()
        .map_err(|error| AuthError::CompletionMismatch(error.to_string()))?;
    let requested: BTreeSet<String> = request
        .components
        .keys()
        .map(|component| format!("{}/{}", grant.service, component))
        .collect();
    if requested != grant.repositories {
        return Err(AuthError::CompletionMismatch(
            "completion components must exactly match the granted repositories".into(),
        ));
    }
    let verified: BTreeSet<String> = artifacts
        .iter()
        .map(|artifact| artifact.repository.clone())
        .collect();
    if verified != grant.repositories || artifacts.len() != grant.repositories.len() {
        return Err(AuthError::CompletionMismatch(
            "verified artifacts must exactly match the granted repositories".into(),
        ));
    }
    for artifact in artifacts {
        let expected_repository = format!("{}/{}", grant.service, artifact.component);
        if artifact.repository != expected_repository {
            return Err(AuthError::CompletionMismatch(format!(
                "component {} does not own repository {}",
                artifact.component, artifact.repository
            )));
        }
        let submitted = request
            .components
            .get(&artifact.component)
            .ok_or_else(|| {
                AuthError::CompletionMismatch("verified component was not submitted".into())
            })?;
        if submitted.digest != artifact.manifest_digest {
            return Err(AuthError::CompletionMismatch(format!(
                "verified digest does not match component {}", artifact.component
            )));
        }
    }
    Ok(())
}

fn load_completion(
    connection: &Connection,
    grant: &StoredGrant,
    request: &ArtifactUploadCompletionRequest,
) -> Result<Option<ArtifactUploadCompletionResponse>, AuthError> {
    let mut statement = connection.prepare(
        "SELECT id,component,repository,manifest_digest,platform_os,platform_architecture,\n                total_compressed_size,accepted_at \n         FROM artifact_receipts WHERE upload_id=? AND status='accepted' ORDER BY component",
    )?;
    let rows = statement
        .query_map([&grant.id], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
                row.get::<_, String>(4)?,
                row.get::<_, String>(5)?,
                row.get::<_, i64>(6)?,
                row.get::<_, i64>(7)?,
            ))
        })?
        .collect::<Result<Vec<_>, _>>()?;
    if rows.is_empty() {
        return Ok(None);
    }
    if rows.len() != request.components.len() {
        return Err(AuthError::CompletionMismatch(
            "upload was already completed with a different component set".into(),
        ));
    }
    let service = ServiceName::try_from(grant.service.clone())
        .map_err(|error| AuthError::InvalidStoredState(error.to_string()))?;
    let revision = Revision::try_from(grant.revision.clone())
        .map_err(|error| AuthError::InvalidStoredState(error.to_string()))?;
    let mut receipts = Vec::with_capacity(rows.len());
    for row in rows {
        let component = ComponentName::try_from(row.1)
            .map_err(|error| AuthError::InvalidStoredState(error.to_string()))?;
        let digest = Sha256Digest::try_from(row.3)
            .map_err(|error| AuthError::InvalidStoredState(error.to_string()))?;
        if request.components.get(&component).map(|item| &item.digest) != Some(&digest) {
            return Err(AuthError::CompletionMismatch(
                "upload was already completed with different digests".into(),
            ));
        }
        let mut layer_statement = connection.prepare(
            "SELECT digest,size,media_type FROM artifact_layers \n             WHERE receipt_id=? ORDER BY layer_index",
        )?;
        let layer_rows = layer_statement
            .query_map([&row.0], |layer| {
                Ok((
                    layer.get::<_, String>(0)?,
                    layer.get::<_, i64>(1)?,
                    layer.get::<_, String>(2)?,
                ))
            })?
            .collect::<Result<Vec<_>, _>>()?;
        let layers = layer_rows
            .into_iter()
            .map(|layer| {
                Ok(ArtifactLayerReceipt {
                    digest: Sha256Digest::try_from(layer.0)
                        .map_err(|error| AuthError::InvalidStoredState(error.to_string()))?,
                    size: u64::try_from(layer.1).map_err(|_| {
                        AuthError::InvalidStoredState("negative layer size".into())
                    })?,
                    media_type: layer.2,
                })
            })
            .collect::<Result<Vec<_>, AuthError>>()?;
        receipts.push(ArtifactReceipt {
            id: row.0,
            upload_id: grant.id.clone(),
            service: service.clone(),
            component,
            repository: row.2,
            revision: revision.clone(),
            manifest_digest: digest,
            platform_os: row.4,
            platform_architecture: row.5,
            total_compressed_size: u64::try_from(row.6)
                .map_err(|_| AuthError::InvalidStoredState("negative artifact size".into()))?,
            accepted_at: format_timestamp(row.7)?,
            layers,
        });
    }
    Ok(Some(ArtifactUploadCompletionResponse {
        upload_id: grant.id.clone(),
        status: "accepted".into(),
        receipts,
    }))
}

fn format_timestamp(timestamp: i64) -> Result<String, AuthError> {
    OffsetDateTime::from_unix_timestamp(timestamp)
        .map_err(|_| AuthError::Time)?
        .format(&Rfc3339)
        .map_err(|_| AuthError::Time)
}

fn random_secret() -> Result<String, AuthError> {
    let mut bytes = [0_u8; 32];
    getrandom::fill(&mut bytes).map_err(|_| AuthError::Randomness)?;
    Ok(URL_SAFE_NO_PAD.encode(bytes))
}

fn upload_secret_hash(username: &str, secret: &str) -> Vec<u8> {
    let mut hasher = Sha256::new();
    hasher.update(SECRET_HASH_DOMAIN);
    hasher.update(username.as_bytes());
    hasher.update([0]);
    hasher.update(secret.as_bytes());
    hasher.finalize().to_vec()
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct RegistryAccess {
    #[serde(rename = "type")]
    pub resource_type: String,
    pub name: String,
    pub actions: Vec<String>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct RegistryClaims {
    pub iss: String,
    pub sub: String,
    pub aud: String,
    pub exp: i64,
    pub nbf: i64,
    pub iat: i64,
    pub jti: String,
    pub access: Vec<RegistryAccess>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct RegistryTokenResponse {
    pub token: String,
    pub access_token: String,
    pub expires_in: i64,
    pub issued_at: String,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct Jwk {
    pub kty: String,
    pub crv: String,
    pub x: String,
    #[serde(rename = "use")]
    pub key_use: String,
    pub alg: String,
    pub kid: String,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct JwkSet {
    pub keys: Vec<Jwk>,
}

#[derive(Clone)]
pub struct RegistryTokenIssuer {
    signing_key: SigningKey,
    issuer: String,
    audience: String,
    key_id: String,
}

impl RegistryTokenIssuer {
    pub fn from_seed_file(
        path: impl AsRef<Path>,
        issuer: impl Into<String>,
        audience: impl Into<String>,
    ) -> Result<Self, AuthError> {
        let path = path.as_ref();
        let metadata =
            fs::metadata(path).map_err(|error| AuthError::InvalidSigningKey(error.to_string()))?;
        if metadata.permissions().mode() & 0o077 != 0 {
            return Err(AuthError::InvalidSigningKey(
                "signing key must not be group- or other-accessible".into(),
            ));
        }
        let encoded = fs::read_to_string(path)
            .map_err(|error| AuthError::InvalidSigningKey(error.to_string()))?;
        let raw = URL_SAFE
            .decode(encoded.trim())
            .or_else(|_| URL_SAFE_NO_PAD.decode(encoded.trim()))
            .or_else(|_| STANDARD.decode(encoded.trim()))
            .map_err(|error| AuthError::InvalidSigningKey(error.to_string()))?;
        let seed: [u8; 32] = raw.try_into().map_err(|_| {
            AuthError::InvalidSigningKey("Ed25519 seed must contain exactly 32 bytes".into())
        })?;
        Ok(Self::from_signing_key(
            SigningKey::from_bytes(&seed),
            issuer,
            audience,
        ))
    }

    pub fn from_signing_key(
        signing_key: SigningKey,
        issuer: impl Into<String>,
        audience: impl Into<String>,
    ) -> Self {
        let verifying_key = signing_key.verifying_key();
        let key_id = jwk_thumbprint(&verifying_key);
        Self {
            signing_key,
            issuer: issuer.into(),
            audience: audience.into(),
            key_id,
        }
    }

    pub fn audience(&self) -> &str {
        &self.audience
    }

    pub fn jwks(&self) -> JwkSet {
        let x = URL_SAFE_NO_PAD.encode(self.signing_key.verifying_key().to_bytes());
        JwkSet {
            keys: vec![Jwk {
                kty: "OKP".into(),
                crv: "Ed25519".into(),
                x,
                key_use: "sig".into(),
                alg: "EdDSA".into(),
                kid: self.key_id.clone(),
            }],
        }
    }

    pub fn write_jwks(&self, path: impl AsRef<Path>) -> Result<(), AuthError> {
        let path = path.as_ref();
        let parent = path.parent().unwrap_or_else(|| Path::new("."));
        fs::create_dir_all(parent)
            .map_err(|error| AuthError::InvalidSigningKey(error.to_string()))?;
        fs::set_permissions(parent, fs::Permissions::from_mode(0o700))
            .map_err(|error| AuthError::InvalidSigningKey(error.to_string()))?;
        let file_name = path
            .file_name()
            .and_then(|value| value.to_str())
            .ok_or_else(|| AuthError::InvalidSigningKey("JWKS path has no file name".into()))?;
        let temporary = parent.join(format!(".{file_name}.{}.tmp", Uuid::new_v4()));
        fs::write(&temporary, serde_json::to_vec_pretty(&self.jwks())?)
            .map_err(|error| AuthError::InvalidSigningKey(error.to_string()))?;
        fs::set_permissions(&temporary, fs::Permissions::from_mode(0o644))
            .map_err(|error| AuthError::InvalidSigningKey(error.to_string()))?;
        fs::rename(&temporary, path)
            .map_err(|error| AuthError::InvalidSigningKey(error.to_string()))?;
        Ok(())
    }

    pub fn issue(
        &self,
        grant: &StoredGrant,
        requested_scopes: &[String],
        service: &str,
        now: i64,
    ) -> Result<RegistryTokenResponse, AuthError> {
        if service != self.audience {
            return Err(AuthError::InvalidRegistryService);
        }
        let remaining = grant.expires_at - now;
        if remaining < MIN_TOKEN_SECONDS {
            return Err(AuthError::GrantNearExpiry);
        }
        let expires_in = remaining.min(DEFAULT_TOKEN_SECONDS);
        let claims = RegistryClaims {
            iss: self.issuer.clone(),
            sub: grant.username.clone(),
            aud: self.audience.clone(),
            exp: now + expires_in,
            nbf: now.saturating_sub(5),
            iat: now,
            jti: Uuid::new_v4().to_string(),
            access: authorized_access(grant, requested_scopes)?,
        };
        let token = self.sign(&claims)?;
        let issued_at = OffsetDateTime::from_unix_timestamp(now)
            .map_err(|_| AuthError::Time)?
            .format(&Rfc3339)
            .map_err(|_| AuthError::Time)?;
        Ok(RegistryTokenResponse {
            token: token.clone(),
            access_token: token,
            expires_in,
            issued_at,
        })
    }

    pub fn issue_for_verification(
        &self,
        grant: &StoredGrant,
        requested_scopes: &[String],
        now: i64,
    ) -> Result<RegistryTokenResponse, AuthError> {
        if grant.completed_at.is_some() {
            return Err(AuthError::GrantCompleted);
        }
        if grant.expires_at <= now {
            return Err(AuthError::GrantExpired);
        }
        let expires_in = VERIFICATION_TOKEN_SECONDS;
        let claims = RegistryClaims {
            iss: self.issuer.clone(),
            sub: format!("verification:{}", grant.id),
            aud: self.audience.clone(),
            exp: now.checked_add(expires_in).ok_or(AuthError::Time)?,
            nbf: now.saturating_sub(5),
            iat: now,
            jti: Uuid::new_v4().to_string(),
            access: authorized_access(grant, requested_scopes)?
                .into_iter()
                .map(|mut access| {
                    access.actions.retain(|action| action == "pull");
                    access
                })
                .filter(|access| !access.actions.is_empty())
                .collect(),
        };
        let token = self.sign(&claims)?;
        let issued_at = format_timestamp(now)?;
        Ok(RegistryTokenResponse {
            token: token.clone(),
            access_token: token,
            expires_in,
            issued_at,
        })
    }

    fn sign(&self, claims: &RegistryClaims) -> Result<String, AuthError> {
        #[derive(Serialize)]
        struct Header<'a> {
            alg: &'a str,
            typ: &'a str,
            kid: &'a str,
        }
        let header = URL_SAFE_NO_PAD.encode(serde_json::to_vec(&Header {
            alg: "EdDSA",
            typ: "JWT",
            kid: &self.key_id,
        })?);
        let claims = URL_SAFE_NO_PAD.encode(serde_json::to_vec(claims)?);
        let signing_input = format!("{header}.{claims}");
        let signature = self.signing_key.sign(signing_input.as_bytes());
        Ok(format!(
            "{signing_input}.{}",
            URL_SAFE_NO_PAD.encode(signature.to_bytes())
        ))
    }
}

fn jwk_thumbprint(verifying_key: &VerifyingKey) -> String {
    let x = URL_SAFE_NO_PAD.encode(verifying_key.to_bytes());
    let canonical = format!(r#"{{"crv":"Ed25519","kty":"OKP","x":"{x}"}}"#);
    URL_SAFE_NO_PAD.encode(Sha256::digest(canonical.as_bytes()))
}

pub fn authorized_access(
    grant: &StoredGrant,
    requested_scopes: &[String],
) -> Result<Vec<RegistryAccess>, AuthError> {
    let mut access = Vec::new();
    for scope in requested_scopes {
        for scope in scope.split_whitespace() {
            let mut parts = scope.splitn(3, ':');
            let resource_type = parts.next().unwrap_or_default();
            let name = parts.next().unwrap_or_default();
            let actions = parts.next().unwrap_or_default();
            if resource_type != "repository" || name.is_empty() || actions.is_empty() {
                return Err(AuthError::InvalidScope(scope.to_owned()));
            }
            if !grant.repositories.contains(name) {
                continue;
            }
            let actions: Vec<String> = actions
                .split(',')
                .filter(|action| matches!(*action, "pull" | "push"))
                .map(str::to_owned)
                .collect();
            if !actions.is_empty() {
                access.push(RegistryAccess {
                    resource_type: "repository".into(),
                    name: name.to_owned(),
                    actions,
                });
            }
        }
    }
    Ok(access)
}

#[cfg(test)]
mod tests {
    use super::*;
    use arcturus_contracts::{
        ArtifactLayerReceipt, ArtifactUploadComponentCompletion, ArtifactUploadCompletionRequest,
        ArtifactUploadRequest, ComponentName, Revision, ServiceName, Sha256Digest,
    };
    use tempfile::TempDir;

    fn request() -> ArtifactUploadRequest {
        ArtifactUploadRequest {
            service: ServiceName::try_from("stellar-project".to_owned()).unwrap(),
            revision: Revision::try_from("a".repeat(40)).unwrap(),
            components: vec![
                ComponentName::try_from("api".to_owned()).unwrap(),
                ComponentName::try_from("web".to_owned()).unwrap(),
            ],
        }
    }

    #[test]
    fn scoped_scrypt_control_token_is_verified() {
        let temp = TempDir::new().unwrap();
        let token = "high-entropy-test-token";
        let salt = b"0123456789abcdef";
        let params = ScryptParams::new(14, 8, 1, 32).unwrap();
        let mut hash = [0_u8; 32];
        scrypt(token.as_bytes(), salt, &params, &mut hash).unwrap();
        let payload = serde_json::json!({
            "version": 2,
            "tokens": [{
                "id": "test",
                "algorithm": "scrypt",
                "salt": URL_SAFE.encode(salt),
                "hash": URL_SAFE.encode(hash),
                "services": ["stellar-project"]
            }]
        });
        let path = temp.path().join("tokens.json");
        fs::write(&path, serde_json::to_vec(&payload).unwrap()).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        let verifier = ControlTokenVerifier::new(path);
        verifier
            .authorize(&format!("Bearer {token}"), "stellar-project")
            .unwrap();
        assert!(matches!(
            verifier.authorize(&format!("Bearer {token}"), "other-service"),
            Err(AuthError::ServiceForbidden(_))
        ));
    }

    #[test]
    fn grant_credentials_are_persisted_as_a_hash() {
        let store = GrantStore::open_in_memory().unwrap();
        let grant = store
            .create(&request(), "registry.internal:9443", 600, 1_700_000_000)
            .unwrap();
        assert_eq!(
            grant.repositories[&ComponentName::try_from("api".to_owned()).unwrap()],
            "stellar-project/api"
        );
        let authenticated = store
            .authenticate(
                &grant.credential.username,
                &grant.credential.secret,
                1_700_000_001,
            )
            .unwrap();
        assert!(authenticated.repositories.contains("stellar-project/web"));
        assert!(matches!(
            store.authenticate(&grant.credential.username, "wrong", 1_700_000_001),
            Err(AuthError::InvalidGrantCredentials)
        ));
    }

    #[test]
    fn completion_persists_immutable_receipts_and_disables_push_credentials() {
        let store = GrantStore::open_in_memory().unwrap();
        let grant_response = store
            .create(&request(), "registry.internal:9443", 600, 1_700_000_000)
            .unwrap();
        let grant = store
            .authenticate(
                &grant_response.credential.username,
                &grant_response.credential.secret,
                1_700_000_001,
            )
            .unwrap();
        let api = ComponentName::try_from("api".to_owned()).unwrap();
        let web = ComponentName::try_from("web".to_owned()).unwrap();
        let api_digest = Sha256Digest::try_from(format!("sha256:{}", "a".repeat(64))).unwrap();
        let web_digest = Sha256Digest::try_from(format!("sha256:{}", "b".repeat(64))).unwrap();
        let completion = ArtifactUploadCompletionRequest {
            components: BTreeMap::from([
                (
                    api.clone(),
                    ArtifactUploadComponentCompletion {
                        digest: api_digest.clone(),
                    },
                ),
                (
                    web.clone(),
                    ArtifactUploadComponentCompletion {
                        digest: web_digest.clone(),
                    },
                ),
            ]),
        };
        let layer = ArtifactLayerReceipt {
            digest: Sha256Digest::try_from(format!("sha256:{}", "c".repeat(64))).unwrap(),
            size: 42,
            media_type: "application/vnd.oci.image.layer.v1.tar+gzip".into(),
        };
        let verified = vec![
            VerifiedArtifact {
                component: api,
                repository: "stellar-project/api".into(),
                manifest_digest: api_digest,
                platform_os: "linux".into(),
                platform_architecture: "amd64".into(),
                total_compressed_size: 100,
                layers: vec![layer.clone()],
            },
            VerifiedArtifact {
                component: web,
                repository: "stellar-project/web".into(),
                manifest_digest: web_digest,
                platform_os: "linux".into(),
                platform_architecture: "amd64".into(),
                total_compressed_size: 101,
                layers: vec![layer],
            },
        ];
        let accepted = store
            .complete(&grant, &completion, &verified, 1_700_000_599, 1_700_000_700)
            .unwrap();
        assert_eq!(accepted.status, "accepted");
        assert_eq!(accepted.receipts.len(), 2);
        let current = store.get_for_completion(&grant.id, 1_700_000_701).unwrap();
        assert!(current.completed_at.is_some());
        assert_eq!(
            store.existing_completion(&current, &completion).unwrap(),
            Some(accepted)
        );
        assert!(matches!(
            store.authenticate(
                &grant_response.credential.username,
                &grant_response.credential.secret,
                1_700_000_701
            ),
            Err(AuthError::GrantCompleted)
        ));
    }

    #[test]
    fn registry_token_contains_only_exact_granted_repositories() {
        let store = GrantStore::open_in_memory().unwrap();
        let grant_response = store
            .create(&request(), "registry.internal:9443", 600, 1_700_000_000)
            .unwrap();
        let grant = store
            .authenticate(
                &grant_response.credential.username,
                &grant_response.credential.secret,
                1_700_000_001,
            )
            .unwrap();
        let issuer = RegistryTokenIssuer::from_signing_key(
            SigningKey::from_bytes(&[7_u8; 32]),
            "arcturusd",
            "arcturus-oci",
        );
        let token = issuer
            .issue(
                &grant,
                &[
                    "repository:stellar-project/api:pull,push".into(),
                    "repository:other-service/api:pull,push".into(),
                ],
                "arcturus-oci",
                1_700_000_001,
            )
            .unwrap();
        let claims = token.token.split('.').nth(1).unwrap();
        let claims: RegistryClaims =
            serde_json::from_slice(&URL_SAFE_NO_PAD.decode(claims).unwrap()).unwrap();
        assert_eq!(claims.access.len(), 1);
        assert_eq!(claims.access[0].name, "stellar-project/api");
        assert_eq!(claims.access[0].actions, ["pull", "push"]);
        assert_eq!(claims.aud, "arcturus-oci");
        assert!(token.expires_in >= 60);

        let verification = issuer
            .issue_for_verification(
                &grant,
                &["repository:stellar-project/api:pull,push".into()],
                1_700_000_599,
            )
            .unwrap();
        assert_eq!(verification.expires_in, VERIFICATION_TOKEN_SECONDS);
        let verification_claims: RegistryClaims = serde_json::from_slice(
            &URL_SAFE_NO_PAD
                .decode(verification.token.split('.').nth(1).unwrap())
                .unwrap(),
        )
        .unwrap();
        assert_eq!(verification_claims.exp, 1_700_000_599 + VERIFICATION_TOKEN_SECONDS);
        assert_eq!(verification_claims.access[0].actions, ["pull"]);
    }

    #[test]
    fn jwks_uses_the_same_key_id_as_the_jwt_header() {
        let issuer = RegistryTokenIssuer::from_signing_key(
            SigningKey::from_bytes(&[9_u8; 32]),
            "arcturusd",
            "arcturus-oci",
        );
        let store = GrantStore::open_in_memory().unwrap();
        let response = store
            .create(&request(), "localhost:9443", 600, 100)
            .unwrap();
        let grant = store
            .authenticate(
                &response.credential.username,
                &response.credential.secret,
                101,
            )
            .unwrap();
        let token = issuer
            .issue(
                &grant,
                &["repository:stellar-project/api:push".into()],
                "arcturus-oci",
                101,
            )
            .unwrap();
        let header: Value = serde_json::from_slice(
            &URL_SAFE_NO_PAD
                .decode(token.token.split('.').next().unwrap())
                .unwrap(),
        )
        .unwrap();
        assert_eq!(header["kid"], issuer.jwks().keys[0].kid);
    }
    #[test]
    fn token_and_signing_key_files_must_be_private() {
        let temp = TempDir::new().unwrap();
        let tokens = temp.path().join("tokens.json");
        fs::write(&tokens, b"[]").unwrap();
        fs::set_permissions(&tokens, fs::Permissions::from_mode(0o644)).unwrap();
        assert!(matches!(
            ControlTokenVerifier::new(&tokens).authorize("Bearer token", "service"),
            Err(AuthError::InvalidTokenDatabase(_))
        ));

        let key = temp.path().join("signing.key");
        fs::write(&key, URL_SAFE_NO_PAD.encode([3_u8; 32])).unwrap();
        fs::set_permissions(&key, fs::Permissions::from_mode(0o644)).unwrap();
        assert!(matches!(
            RegistryTokenIssuer::from_seed_file(&key, "issuer", "audience"),
            Err(AuthError::InvalidSigningKey(_))
        ));
    }

    #[test]
    fn jwks_is_written_atomically_without_private_material() {
        let temp = TempDir::new().unwrap();
        let issuer = RegistryTokenIssuer::from_signing_key(
            SigningKey::from_bytes(&[4_u8; 32]),
            "issuer",
            "audience",
        );
        let path = temp.path().join("state/registry-jwks.json");
        issuer.write_jwks(&path).unwrap();
        let payload: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        assert_eq!(payload["keys"][0]["kty"], "OKP");
        assert!(payload["keys"][0].get("d").is_none());
        assert_eq!(
            fs::metadata(path).unwrap().permissions().mode() & 0o777,
            0o644
        );
    }

    #[test]
    fn grant_lifetime_is_bounded() {
        let store = GrantStore::open_in_memory().unwrap();
        assert!(matches!(
            store.create(&request(), "localhost:9443", 59, 100),
            Err(AuthError::InvalidGrantLifetime)
        ));
        assert!(matches!(
            store.create(&request(), "localhost:9443", 901, 100),
            Err(AuthError::InvalidGrantLifetime)
        ));
    }
}
