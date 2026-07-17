use arcturus_contracts::HealthResponse;
use axum::{Json, Router, routing::get};
use tower_http::trace::TraceLayer;

pub fn health_response() -> HealthResponse {
    HealthResponse {
        status: "ok".to_owned(),
        service: "arcturusd".to_owned(),
        version: env!("CARGO_PKG_VERSION").to_owned(),
        features: vec![
            "rust-control-plane-preview".to_owned(),
            "oci-ingress-contracts".to_owned(),
            "python-compatibility-boundary".to_owned(),
        ],
    }
}

async fn healthz() -> Json<HealthResponse> {
    Json(health_response())
}

pub fn app() -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/v1/rust/healthz", get(healthz))
        .layer(TraceLayer::new_for_http())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn health_contract_advertises_preview_boundary() {
        let response = health_response();
        assert_eq!(response.status, "ok");
        assert_eq!(response.service, "arcturusd");
        assert!(
            response
                .features
                .contains(&"oci-ingress-contracts".to_owned())
        );
    }
}
