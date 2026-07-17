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

    let state = arcturusd::AppState::from_environment()?;
    let address: SocketAddr = env::var("ARCTURUSD_LISTEN")
        .unwrap_or_else(|_| "127.0.0.1:9190".to_owned())
        .parse()?;
    let listener = tokio::net::TcpListener::bind(address).await?;
    info!(%address, "Arcturus Rust control-plane preview listening");

    axum::serve(listener, arcturusd::app(state))
        .with_graceful_shutdown(shutdown_signal())
        .await?;
    Ok(())
}

async fn shutdown_signal() {
    if let Err(error) = tokio::signal::ctrl_c().await {
        tracing::error!(%error, "failed to install shutdown signal handler");
    }
}
