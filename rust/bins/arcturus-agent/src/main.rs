use std::env;
use std::error::Error;
use std::fs;
use std::sync::Arc;
use std::time::Duration;

use arcturus_agent::{AgentStore, LifecycleClient, unix_timestamp, worker};
use arcturus_contracts::{
    StoragePoolInventory, WorkerAssignmentSet, WorkerHeartbeat, WorkerInventory, WorkerPressure,
};
use arcturus_paths::ArcturusPaths;
use tokio::sync::Semaphore;
use tracing::{error, info, warn};
use tracing_subscriber::EnvFilter;
use uuid::Uuid;

#[tokio::main]
async fn main() -> Result<(), Box<dyn Error + Send + Sync>> {
    tracing_subscriber::fmt()
        .json()
        .with_env_filter(
            EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| EnvFilter::new("arcturus_agent=info")),
        )
        .init();
    let paths = ArcturusPaths::from_environment()?;
    let control_url = required("ARCTURUS_CONTROL_PLANE_URL")?
        .trim_end_matches('/')
        .to_owned();
    let worker_id = worker(&required("ARCTURUS_WORKER_ID")?)?;
    let credential_file = env::var("ARCTURUS_WORKER_TOKEN_FILE")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|_| paths.config_dir.join("worker.token"));
    let credential = secure_token(&credential_file)?;
    let state_db = env::var("ARCTURUS_AGENT_STATE_DB")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|_| paths.agent_database());
    let lifecycle_url =
        env::var("ARCTURUS_LIFECYCLE_API_URL").unwrap_or_else(|_| "http://127.0.0.1:9090".into());
    let lifecycle_tokens = env::var("ARCTURUS_LIFECYCLE_TOKENS_DIR")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|_| paths.config_dir.join("lifecycle-tokens"));
    let store = AgentStore::open(state_db)?;
    let lifecycle = LifecycleClient::new(lifecycle_url, lifecycle_tokens)?;
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(30))
        .build()?;
    let instance = Uuid::new_v4().to_string();
    loop {
        let pressure_sequence = store.next_pressure_sequence()?;
        let heartbeat = heartbeat(&worker_id, &instance, pressure_sequence, &store)?;
        if let Err(error) = client
            .put(format!(
                "{control_url}/v1/fleet/workers/{worker_id}/heartbeat"
            ))
            .bearer_auth(&credential)
            .json(&heartbeat)
            .send()
            .await
            .and_then(reqwest::Response::error_for_status)
        {
            warn!(%error, "control plane unavailable; retaining cached assignments");
        }
        let revision = store.assignment_set_revision()?;
        match client
            .get(format!(
                "{control_url}/v1/fleet/workers/{worker_id}/assignments?afterRevision={revision}"
            ))
            .bearer_auth(&credential)
            .send()
            .await
        {
            Ok(response) if response.status().is_success() => {
                match response.json::<WorkerAssignmentSet>().await {
                    Ok(set) => match store.accept_assignment_set(&worker_id, &set) {
                        Ok(conflicts) => {
                            for conflict in conflicts {
                                error!(%conflict, "assignment conflict");
                            }
                        }
                        Err(error) => {
                            warn!(%error, "assignment snapshot rejected; retaining cached assignments")
                        }
                    },
                    Err(error) => {
                        warn!(%error, "assignment response is invalid; retaining cached assignments")
                    }
                }
            }
            Ok(response) => {
                warn!(status=%response.status(), "assignment poll rejected; retaining cached assignments")
            }
            Err(error) => warn!(%error, "assignment poll failed; retaining cached assignments"),
        }
        let slots = Arc::new(Semaphore::new(4));
        let mut tasks = Vec::new();
        for assignment in store.assignments()? {
            let permit = slots.clone().acquire_owned().await?;
            let lifecycle = lifecycle.clone();
            let store = store.clone();
            let client = client.clone();
            let control_url = control_url.clone();
            let credential = credential.clone();
            tasks.push(tokio::spawn(async move {
                let _permit = permit;
                let sequence = store.next_observation_sequence(&assignment.workload_name)?;
                let observation = lifecycle.reconcile(&assignment, unix_timestamp(), sequence).await;
                store.save_observation(&observation)?;
                let url = format!("{control_url}/v1/fleet/workers/{}/workloads/{}/observed-state", assignment.worker_id, assignment.workload_name);
                if let Err(error) = client.put(url).bearer_auth(credential).json(&observation).send().await.and_then(reqwest::Response::error_for_status) {
                    warn!(%error, workload=%assignment.workload_name, "observed state report failed");
                }
                Ok::<(), arcturus_agent::AgentError>(())
            }));
        }
        for task in tasks {
            if let Err(error) = task.await? {
                error!(%error, "workload reconciliation failed");
            }
        }
        info!(worker=%worker_id, "reconciliation pass complete");
        tokio::time::sleep(Duration::from_secs(5)).await;
    }
}

fn required(name: &str) -> Result<String, Box<dyn Error + Send + Sync>> {
    env::var(name).map_err(|_| format!("{name} is required").into())
}

fn secure_token(path: &std::path::Path) -> Result<String, Box<dyn Error + Send + Sync>> {
    use std::os::unix::fs::PermissionsExt;
    let metadata = fs::metadata(path)?;
    if metadata.permissions().mode() & 0o077 != 0 {
        return Err(format!("{} must be mode 0600", path.display()).into());
    }
    Ok(fs::read_to_string(path)?.trim().to_owned())
}

fn heartbeat(
    worker_id: &arcturus_contracts::WorkerId,
    instance: &str,
    sequence: u64,
    store: &AgentStore,
) -> Result<WorkerHeartbeat, Box<dyn Error + Send + Sync>> {
    let (total, available) = memory_bytes();
    Ok(WorkerHeartbeat {
        worker_id: worker_id.clone(),
        agent_instance_id: instance.into(),
        inventory: WorkerInventory {
            inventory_generation: 1,
            architecture: match std::env::consts::ARCH {
                "x86_64" => "amd64",
                "aarch64" => "arm64",
                other => other,
            }
            .into(),
            operating_system: std::env::consts::OS.into(),
            logical_cpu_count: std::thread::available_parallelism()
                .map(|value| value.get() as u32)
                .unwrap_or(1),
            total_memory_bytes: total,
            storage_pools: Vec::<StoragePoolInventory>::new(),
        },
        pressure: WorkerPressure {
            pressure_sequence: sequence,
            observed_at: unix_timestamp(),
            available_memory_bytes: available,
            cpu_utilization_basis_points: None,
            storage_free_bytes: Default::default(),
            thermal_state: None,
        },
        accepted_assignment_set_revision: store.assignment_set_revision()?,
        accepted_assignments: store.accepted_generations()?,
    })
}

fn memory_bytes() -> (u64, u64) {
    let values = fs::read_to_string("/proc/meminfo").unwrap_or_default();
    let parse = |name: &str| {
        values
            .lines()
            .find_map(|line| line.strip_prefix(name))
            .and_then(|value| value.split_whitespace().next())
            .and_then(|value| value.parse::<u64>().ok())
            .unwrap_or(0)
            * 1024
    };
    (parse("MemTotal:"), parse("MemAvailable:"))
}
