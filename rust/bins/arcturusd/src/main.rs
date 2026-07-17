use std::{env, error::Error, net::SocketAddr};

use tracing::info;
use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() -> Result<(), Box<dyn Error>> {
    tracing_subscriber::fmt()
        .json()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("arcturusd=info")),
        )
        .init();

    let upload_auth_enabled = match env::var("ARCTURUSD_UPLOAD_AUTH_ENABLED") {
        Ok(value) if matches!(value.to_ascii_lowercase().as_str(), "1" | "true") => true,
        Ok(value) if matches!(value.to_ascii_lowercase().as_str(), "0" | "false") => false,
        Ok(value) => {
            return Err(format!(
                "ARCTURUSD_UPLOAD_AUTH_ENABLED must be true, false, 1, or 0; got {value}"
            )
            .into());
        }
        Err(env::VarError::NotPresent) => false,
        Err(error) => return Err(error.into()),
    };
    let application = if upload_auth_enabled {
        arcturusd::app(arcturusd::AppState::from_environment()?)
    } else {
        arcturusd::health_app()
    };
    let address: SocketAddr = env::var("ARCTURUSD_LISTEN")
        .unwrap_or_else(|_| "127.0.0.1:9190".to_owned())
        .parse()?;
    let listener = tokio::net::TcpListener::bind(address).await?;
    info!(%address, upload_auth_enabled, "Arcturus Rust control-plane preview listening");

    axum::serve(listener, application)
        .with_graceful_shutdown(shutdown_signal())
        .await?;
    Ok(())
}

async fn shutdown_signal() {
    if let Err(error) = tokio::signal::ctrl_c().await {
        tracing::error!(%error, "failed to install shutdown signal handler");
    }
}
