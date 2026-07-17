use std::env;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use arcturus_auth::{AuthError, ControlTokenVerifier, GrantStore, JwkSet, RegistryTokenIssuer};
use arcturus_contracts::{ApiErrorBody, ApiErrorResponse, ArtifactUploadRequest, HealthResponse};
use axum::extract::{RawQuery, State};
use axum::http::header::{AUTHORIZATION, CACHE_CONTROL, PRAGMA, WWW_AUTHENTICATE};
use axum::http::{HeaderMap, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::{Json, Router, routing::get, routing::post};
use base64::Engine;
use base64::engine::general_purpose::STANDARD;
use tower_http::trace::TraceLayer;

#[derive(Clone, Debug)]
pub struct AppConfig {
    pub registry: String,
    pub upload_ttl_seconds: i64,
}

#[derive(Clone)]
pub struct AppState {
    pub config: AppConfig,
    pub control_tokens: ControlTokenVerifier,
    pub grants: GrantStore,
    pub token_issuer: RegistryTokenIssuer,
}

impl AppState {
    pub fn new(
        config: AppConfig,
        control_tokens: ControlTokenVerifier,
        grants: GrantStore,
        token_issuer: RegistryTokenIssuer,
    ) -> Self {
        Self {
            config,
            control_tokens,
            grants,
            token_issuer,
        }
    }

    pub fn from_environment() -> Result<Self, AuthError> {
        let home = env::var("HOME").unwrap_or_else(|_| ".".to_owned());
        let state_db = env::var("ARCTURUSD_STATE_DB")
            .unwrap_or_else(|_| format!("{home}/.local/share/arcturus-deployer/oci-auth.sqlite3"));
        let token_file = env::var("RUNNER_TOKENS_FILE")
            .unwrap_or_else(|_| format!("{home}/.config/arcturus/tokens.json"));
        let signing_key = env::var("ARCTURUS_OCI_SIGNING_KEY").map_err(|_| {
            AuthError::InvalidSigningKey("ARCTURUS_OCI_SIGNING_KEY is required".into())
        })?;
        let jwks_file = env::var("ARCTURUS_OCI_JWKS_FILE")
            .unwrap_or_else(|_| format!("{home}/.local/share/arcturus-deployer/oci-jwks.json"));
        let issuer =
            env::var("ARCTURUS_OCI_TOKEN_ISSUER").unwrap_or_else(|_| "arcturusd".to_owned());
        let audience =
            env::var("ARCTURUS_OCI_TOKEN_SERVICE").unwrap_or_else(|_| "arcturus-oci".to_owned());
        let registry =
            env::var("ARCTURUS_OCI_REGISTRY").unwrap_or_else(|_| "127.0.0.1:9443".to_owned());
        let upload_ttl_seconds = env::var("ARCTURUS_OCI_UPLOAD_TTL_SECONDS")
            .unwrap_or_else(|_| "600".to_owned())
            .parse::<i64>()
            .map_err(|error| AuthError::Serialization(error.to_string()))?;
        if !(60..=900).contains(&upload_ttl_seconds) {
            return Err(AuthError::InvalidGrantLifetime);
        }
        let token_issuer = RegistryTokenIssuer::from_seed_file(signing_key, issuer, audience)?;
        token_issuer.write_jwks(jwks_file)?;
        Ok(Self::new(
            AppConfig {
                registry,
                upload_ttl_seconds,
            },
            ControlTokenVerifier::new(PathBuf::from(token_file)),
            GrantStore::open(state_db)?,
            token_issuer,
        ))
    }
}

pub fn health_response() -> HealthResponse {
    HealthResponse {
        status: "ok".to_owned(),
        service: "arcturusd".to_owned(),
        version: env!("CARGO_PKG_VERSION").to_owned(),
        features: vec![
            "rust-control-plane-preview".to_owned(),
            "oci-ingress-contracts".to_owned(),
            "oci-upload-grants-preview".to_owned(),
            "registry-token-issuer-preview".to_owned(),
            "python-compatibility-boundary".to_owned(),
        ],
    }
}

async fn healthz() -> Json<HealthResponse> {
    Json(health_response())
}

async fn jwks(State(state): State<Arc<AppState>>) -> Json<JwkSet> {
    Json(state.token_issuer.jwks())
}

async fn create_artifact_upload(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(request): Json<ArtifactUploadRequest>,
) -> Result<Response, ApiFailure> {
    let authorization = header_text(&headers, AUTHORIZATION)?.to_owned();
    let service = request.service.to_string();
    let verifier = state.control_tokens.clone();
    tokio::task::spawn_blocking(move || verifier.authorize(&authorization, &service))
        .await
        .map_err(|_| ApiFailure::internal("control token verification task failed"))?
        .map_err(ApiFailure::control_auth)?;
    request
        .validate()
        .map_err(|error| ApiFailure::bad_request(error.to_string()))?;

    let store = state.grants.clone();
    let registry = state.config.registry.clone();
    let ttl = state.config.upload_ttl_seconds;
    let grant = tokio::task::spawn_blocking(move || {
        store.create(&request, &registry, ttl, unix_timestamp())
    })
    .await
    .map_err(|_| ApiFailure::internal("upload grant task failed"))??;
    Ok(no_store_json(StatusCode::CREATED, grant))
}

async fn registry_token(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    RawQuery(raw_query): RawQuery,
) -> Result<Response, ApiFailure> {
    let (username, secret) = basic_credentials(&headers)?;
    let query = TokenQuery::parse(raw_query.as_deref().unwrap_or_default())?;
    let store = state.grants.clone();
    let now = unix_timestamp();
    let grant = tokio::task::spawn_blocking(move || store.authenticate(&username, &secret, now))
        .await
        .map_err(|_| ApiFailure::internal("upload grant authentication task failed"))?
        .map_err(ApiFailure::registry_auth)?;
    let response = state
        .token_issuer
        .issue(&grant, &query.scopes, &query.service, now)?;
    Ok(no_store_json(StatusCode::OK, response))
}

pub fn app(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/v1/rust/healthz", get(healthz))
        .route("/v1/artifact-uploads", post(create_artifact_upload))
        .route("/auth/token", get(registry_token))
        .route("/v1/oci/jwks.json", get(jwks))
        .with_state(Arc::new(state))
        .layer(TraceLayer::new_for_http())
}

fn unix_timestamp() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs() as i64)
        .unwrap_or_default()
}

fn header_text(
    headers: &HeaderMap,
    name: axum::http::header::HeaderName,
) -> Result<&str, ApiFailure> {
    headers
        .get(name)
        .and_then(|value| value.to_str().ok())
        .ok_or_else(|| ApiFailure::unauthorized("authorization header is missing or malformed"))
}

fn basic_credentials(headers: &HeaderMap) -> Result<(String, String), ApiFailure> {
    let authorization = header_text(headers, AUTHORIZATION)?;
    let (scheme, encoded) = authorization
        .split_once(' ')
        .filter(|(scheme, encoded)| scheme.eq_ignore_ascii_case("Basic") && !encoded.is_empty())
        .ok_or_else(|| {
            ApiFailure::registry_unauthorized(
                "registry token endpoint requires Basic authentication",
            )
        })?;
    let _ = scheme;
    let decoded = STANDARD
        .decode(encoded)
        .map_err(|_| ApiFailure::registry_unauthorized("registry credentials are malformed"))?;
    let decoded = String::from_utf8(decoded)
        .map_err(|_| ApiFailure::registry_unauthorized("registry credentials are malformed"))?;
    let (username, secret) = decoded
        .split_once(':')
        .filter(|(username, secret)| !username.is_empty() && !secret.is_empty())
        .ok_or_else(|| ApiFailure::registry_unauthorized("registry credentials are malformed"))?;
    Ok((username.to_owned(), secret.to_owned()))
}

#[derive(Default)]
struct TokenQuery {
    service: String,
    scopes: Vec<String>,
}

impl TokenQuery {
    fn parse(raw: &str) -> Result<Self, ApiFailure> {
        let mut result = Self::default();
        for (key, value) in url::form_urlencoded::parse(raw.as_bytes()) {
            match key.as_ref() {
                "service" => {
                    if !result.service.is_empty() {
                        return Err(ApiFailure::bad_request("registry service must appear once"));
                    }
                    result.service = value.into_owned();
                }
                "scope" => result.scopes.push(value.into_owned()),
                "client_id" | "offline_token" => {}
                _ => {}
            }
        }
        if result.service.is_empty() {
            return Err(ApiFailure::bad_request("registry service is required"));
        }
        Ok(result)
    }
}

#[derive(Debug)]
pub struct ApiFailure {
    status: StatusCode,
    code: &'static str,
    message: String,
    challenge: Option<&'static str>,
}

impl ApiFailure {
    fn bad_request(message: impl Into<String>) -> Self {
        Self {
            status: StatusCode::BAD_REQUEST,
            code: "invalid_request",
            message: message.into(),
            challenge: None,
        }
    }

    fn unauthorized(message: impl Into<String>) -> Self {
        Self {
            status: StatusCode::UNAUTHORIZED,
            code: "unauthorized",
            message: message.into(),
            challenge: None,
        }
    }

    fn control_auth(error: AuthError) -> Self {
        let mut failure = Self::from(error);
        if failure.status == StatusCode::UNAUTHORIZED {
            failure.challenge = Some("Bearer realm=\"Arcturus control plane\"");
        }
        failure
    }

    fn registry_unauthorized(message: impl Into<String>) -> Self {
        Self {
            status: StatusCode::UNAUTHORIZED,
            code: "unauthorized",
            message: message.into(),
            challenge: Some("Basic realm=\"Arcturus OCI upload grant\""),
        }
    }

    fn registry_auth(error: AuthError) -> Self {
        match error {
            AuthError::InvalidGrantCredentials
            | AuthError::GrantExpired
            | AuthError::GrantNearExpiry => Self::registry_unauthorized(error.to_string()),
            other => Self::from(other),
        }
    }

    fn forbidden(message: impl Into<String>) -> Self {
        Self {
            status: StatusCode::FORBIDDEN,
            code: "forbidden",
            message: message.into(),
            challenge: None,
        }
    }

    fn internal(message: impl Into<String>) -> Self {
        Self {
            status: StatusCode::INTERNAL_SERVER_ERROR,
            code: "internal_error",
            message: message.into(),
            challenge: None,
        }
    }
}

impl From<AuthError> for ApiFailure {
    fn from(error: AuthError) -> Self {
        match error {
            AuthError::InvalidAuthorization
            | AuthError::InvalidControlToken
            | AuthError::InvalidGrantCredentials
            | AuthError::GrantExpired
            | AuthError::GrantNearExpiry => Self::unauthorized(error.to_string()),
            AuthError::ServiceForbidden(_) => Self::forbidden(error.to_string()),
            AuthError::InvalidRegistryService | AuthError::InvalidScope(_) => {
                Self::bad_request(error.to_string())
            }
            _ => Self::internal(error.to_string()),
        }
    }
}

impl IntoResponse for ApiFailure {
    fn into_response(self) -> Response {
        let payload = ApiErrorResponse {
            status: "failed".into(),
            error: ApiErrorBody {
                code: self.code.into(),
                message: self.message,
            },
        };
        let mut response = (self.status, Json(payload)).into_response();
        response
            .headers_mut()
            .insert(CACHE_CONTROL, HeaderValue::from_static("no-store"));
        if let Some(challenge) = self.challenge {
            response
                .headers_mut()
                .insert(WWW_AUTHENTICATE, HeaderValue::from_static(challenge));
        }
        response
    }
}

fn no_store_json<T: serde::Serialize>(status: StatusCode, value: T) -> Response {
    let mut response = (status, Json(value)).into_response();
    response
        .headers_mut()
        .insert(CACHE_CONTROL, HeaderValue::from_static("no-store"));
    response
        .headers_mut()
        .insert(PRAGMA, HeaderValue::from_static("no-cache"));
    response
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::os::unix::fs::PermissionsExt;

    use arcturus_auth::{RegistryClaims, RegistryTokenResponse};
    use axum::body::{Body, to_bytes};
    use axum::http::Request;
    use base64::engine::general_purpose::{STANDARD, URL_SAFE, URL_SAFE_NO_PAD};
    use ed25519_dalek::SigningKey;
    use scrypt::{Params as ScryptParams, scrypt};
    use serde_json::Value;
    use tempfile::TempDir;
    use tower::ServiceExt;

    use super::*;

    fn test_state(temp: &TempDir) -> AppState {
        let token = "control-plane-test-token";
        let salt = b"0123456789abcdef";
        let params = ScryptParams::new(14, 8, 1, 32).unwrap();
        let mut hash = [0_u8; 32];
        scrypt(token.as_bytes(), salt, &params, &mut hash).unwrap();
        let token_path = temp.path().join("tokens.json");
        fs::write(
            &token_path,
            serde_json::to_vec(&serde_json::json!({
                "version": 2,
                "tokens": [{
                    "id": "ci",
                    "algorithm": "scrypt",
                    "salt": URL_SAFE.encode(salt),
                    "hash": URL_SAFE.encode(hash),
                    "services": ["stellar-project"]
                }]
            }))
            .unwrap(),
        )
        .unwrap();
        fs::set_permissions(&token_path, fs::Permissions::from_mode(0o600)).unwrap();
        AppState::new(
            AppConfig {
                registry: "arcturus.internal:9443".into(),
                upload_ttl_seconds: 600,
            },
            ControlTokenVerifier::new(token_path),
            GrantStore::open_in_memory().unwrap(),
            RegistryTokenIssuer::from_signing_key(
                SigningKey::from_bytes(&[7_u8; 32]),
                "arcturusd",
                "arcturus-oci",
            ),
        )
    }

    #[test]
    fn health_contract_advertises_preview_boundary() {
        let response = health_response();
        assert_eq!(response.status, "ok");
        assert_eq!(response.service, "arcturusd");
        assert!(
            response
                .features
                .contains(&"registry-token-issuer-preview".to_owned())
        );
    }

    #[tokio::test]
    async fn grant_and_registry_token_flow_is_repository_scoped() {
        let temp = TempDir::new().unwrap();
        let router = app(test_state(&temp));
        let request = Request::builder()
            .method("POST")
            .uri("/v1/artifact-uploads")
            .header(AUTHORIZATION, "Bearer control-plane-test-token")
            .header("content-type", "application/json")
            .body(Body::from(
                serde_json::to_vec(&serde_json::json!({
                    "service": "stellar-project",
                    "revision": "a".repeat(40),
                    "components": ["api", "web"]
                }))
                .unwrap(),
            ))
            .unwrap();
        let response = router.clone().oneshot(request).await.unwrap();
        assert_eq!(response.status(), StatusCode::CREATED);
        let body = to_bytes(response.into_body(), 64 * 1024).await.unwrap();
        let grant: arcturus_contracts::ArtifactUploadGrant = serde_json::from_slice(&body).unwrap();
        assert_eq!(grant.registry, "arcturus.internal:9443");
        assert_eq!(grant.repositories.len(), 2);

        let basic = STANDARD.encode(format!(
            "{}:{}",
            grant.credential.username, grant.credential.secret
        ));
        let request = Request::builder()
            .uri(
                "/auth/token?service=arcturus-oci&scope=repository%3Astellar-project%2Fapi%3Apull%2Cpush&scope=repository%3Aother-service%2Fapi%3Apull%2Cpush",
            )
            .header(AUTHORIZATION, format!("Basic {basic}"))
            .body(Body::empty())
            .unwrap();
        let response = router.oneshot(request).await.unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let body = to_bytes(response.into_body(), 64 * 1024).await.unwrap();
        let token: RegistryTokenResponse = serde_json::from_slice(&body).unwrap();
        assert_eq!(token.token, token.access_token);
        let claims = token.token.split('.').nth(1).unwrap();
        let claims: RegistryClaims =
            serde_json::from_slice(&URL_SAFE_NO_PAD.decode(claims).unwrap()).unwrap();
        assert_eq!(claims.access.len(), 1);
        assert_eq!(claims.access[0].name, "stellar-project/api");
        assert_eq!(claims.access[0].actions, ["pull", "push"]);
    }

    #[tokio::test]
    async fn wrong_control_token_and_wrong_grant_secret_are_rejected() {
        let temp = TempDir::new().unwrap();
        let router = app(test_state(&temp));
        let request = Request::builder()
            .method("POST")
            .uri("/v1/artifact-uploads")
            .header(AUTHORIZATION, "Bearer wrong")
            .header("content-type", "application/json")
            .body(Body::from(
                r#"{"service":"stellar-project","revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","components":["api"]}"#,
            ))
            .unwrap();
        let response = router.clone().oneshot(request).await.unwrap();
        assert_eq!(response.status(), StatusCode::UNAUTHORIZED);

        let basic = STANDARD.encode("upload-not-a-real-grant:wrong-secret");
        let request = Request::builder()
            .uri("/auth/token?service=arcturus-oci&scope=repository%3Astellar-project%2Fapi%3Apush")
            .header(AUTHORIZATION, format!("Basic {basic}"))
            .body(Body::empty())
            .unwrap();
        let response = router.oneshot(request).await.unwrap();
        assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
        assert_eq!(
            response.headers().get(WWW_AUTHENTICATE).unwrap(),
            "Basic realm=\"Arcturus OCI upload grant\""
        );
    }

    #[tokio::test]
    async fn jwks_never_contains_private_key_material() {
        let temp = TempDir::new().unwrap();
        let router = app(test_state(&temp));
        let response = router
            .oneshot(
                Request::builder()
                    .uri("/v1/oci/jwks.json")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let body = to_bytes(response.into_body(), 64 * 1024).await.unwrap();
        let payload: Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(payload["keys"][0]["kty"], "OKP");
        assert_eq!(payload["keys"][0]["crv"], "Ed25519");
        assert!(payload["keys"][0].get("d").is_none());
    }
}
