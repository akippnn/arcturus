use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use arcturus_contracts::{
    ArtifactUploadGrant, ArtifactUploadRequest, ComponentName, UploadCredential,
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
        let (scheme, token) = authorization
            .split_once(' ')
            .filter(|(scheme, token)| scheme.eq_ignore_ascii_case("Bearer") && !token.is_empty())
            .ok_or(AuthError::InvalidAuthorization)?;
        let _ = scheme;
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
}

#[derive(Clone)]
pub struct GrantStore {
    connection: Arc<Mutex<Connection>>,
}

impl GrantStore {
    pub fn open(path: impl AsRef<Path>) -> Result<Self, AuthError> {
        if let Some(parent) = path.as_ref().parent() {
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
             CREATE TABLE IF NOT EXISTS upload_grants (\n\
               id TEXT PRIMARY KEY,\n\
               username TEXT NOT NULL UNIQUE,\n\
               secret_hash BLOB NOT NULL,\n\
               service TEXT NOT NULL,\n\
               revision TEXT NOT NULL,\n\
               repositories_json TEXT NOT NULL,\n\
               created_at INTEGER NOT NULL,\n\
               expires_at INTEGER NOT NULL,\n\
               revoked_at INTEGER\n\
             );\n\
             CREATE INDEX IF NOT EXISTS idx_upload_grants_username\n\
               ON upload_grants(username);\n\
             CREATE INDEX IF NOT EXISTS idx_upload_grants_expiry\n\
               ON upload_grants(expires_at);",
        )?;
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
                "SELECT id,username,secret_hash,service,revision,repositories_json,expires_at\n\
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
                    ))
                },
            )
            .optional()?
            .ok_or(AuthError::InvalidGrantCredentials)?;
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
        })
    }
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
    use arcturus_contracts::{ArtifactUploadRequest, ComponentName, Revision, ServiceName};
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
