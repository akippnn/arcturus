use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::sync::{Arc, Mutex};

use arcturus_contracts::{
    ObservedError, ObservedPhase, ObservedState, ServiceName, WorkerAssignment,
    WorkerAssignmentAction, WorkerAssignmentSet, WorkerId,
};
use rusqlite::{Connection, OptionalExtension, params};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum AgentError {
    #[error("agent state failed: {0}")]
    Database(String),
    #[error("assignment conflict for {0}")]
    AssignmentConflict(String),
    #[error("assignment set is invalid: {0}")]
    AssignmentSet(String),
    #[error("agent state lock is poisoned")]
    LockPoisoned,
    #[error("lifecycle request failed: {0}")]
    Lifecycle(String),
}

impl From<rusqlite::Error> for AgentError {
    fn from(value: rusqlite::Error) -> Self {
        Self::Database(value.to_string())
    }
}

impl From<serde_json::Error> for AgentError {
    fn from(value: serde_json::Error) -> Self {
        Self::Database(value.to_string())
    }
}

#[derive(Clone)]
pub struct AgentStore {
    connection: Arc<Mutex<Connection>>,
}

impl AgentStore {
    pub fn open(path: impl AsRef<Path>) -> Result<Self, AgentError> {
        let path = path.as_ref();
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).map_err(|error| AgentError::Database(error.to_string()))?;
            fs::set_permissions(parent, fs::Permissions::from_mode(0o700))
                .map_err(|error| AgentError::Database(error.to_string()))?;
        }
        let connection = Connection::open(path)?;
        fs::set_permissions(path, fs::Permissions::from_mode(0o600))
            .map_err(|error| AgentError::Database(error.to_string()))?;
        connection.execute_batch(
            "PRAGMA journal_mode=WAL;
             CREATE TABLE IF NOT EXISTS agent_metadata(key TEXT PRIMARY KEY,value INTEGER NOT NULL);
             CREATE TABLE IF NOT EXISTS agent_assignments(
               workload_name TEXT PRIMARY KEY,generation INTEGER NOT NULL,payload_json TEXT NOT NULL
             );
             CREATE TABLE IF NOT EXISTS agent_observations(
               workload_name TEXT PRIMARY KEY,sequence INTEGER NOT NULL,payload_json TEXT NOT NULL
             );
             INSERT OR IGNORE INTO agent_metadata(key,value) VALUES ('assignmentSetRevision',0);
             INSERT OR IGNORE INTO agent_metadata(key,value) VALUES ('pressureSequence',0);",
        )?;
        Ok(Self {
            connection: Arc::new(Mutex::new(connection)),
        })
    }

    pub fn assignment_set_revision(&self) -> Result<u64, AgentError> {
        let connection = self
            .connection
            .lock()
            .map_err(|_| AgentError::LockPoisoned)?;
        Ok(connection.query_row(
            "SELECT value FROM agent_metadata WHERE key='assignmentSetRevision'",
            [],
            |row| row.get(0),
        )?)
    }

    pub fn next_pressure_sequence(&self) -> Result<u64, AgentError> {
        let mut connection = self
            .connection
            .lock()
            .map_err(|_| AgentError::LockPoisoned)?;
        let transaction = connection.transaction()?;
        let prior: u64 = transaction.query_row(
            "SELECT value FROM agent_metadata WHERE key='pressureSequence'",
            [],
            |row| row.get(0),
        )?;
        let next = prior
            .checked_add(1)
            .ok_or_else(|| AgentError::Database("pressure sequence exhausted".into()))?;
        transaction.execute(
            "UPDATE agent_metadata SET value=? WHERE key='pressureSequence'",
            params![next],
        )?;
        transaction.commit()?;
        Ok(next)
    }

    /// Accepts valid workload entries independently. The set cursor advances only
    /// when every entry is valid; unchanged or absent entries never remove state.
    pub fn accept_assignment_set(
        &self,
        expected_worker: &WorkerId,
        set: &WorkerAssignmentSet,
    ) -> Result<Vec<AgentError>, AgentError> {
        if &set.worker_id != expected_worker {
            return Err(AgentError::AssignmentSet(
                "response worker identity does not match this agent".into(),
            ));
        }
        let current_revision = self.assignment_set_revision()?;
        if set.revision < current_revision {
            return Err(AgentError::AssignmentSet(format!(
                "revision {} is older than accepted revision {current_revision}",
                set.revision
            )));
        }
        if !set.changed {
            if set.revision != current_revision || !set.assignments.is_empty() {
                return Err(AgentError::AssignmentSet(
                    "unchanged response has an unexpected revision or assignments".into(),
                ));
            }
            return Ok(Vec::new());
        }
        let mut connection = self
            .connection
            .lock()
            .map_err(|_| AgentError::LockPoisoned)?;
        let transaction = connection.transaction()?;
        let mut errors = Vec::new();
        let mut seen = BTreeSet::new();
        for assignment in &set.assignments {
            if &assignment.worker_id != expected_worker
                || !seen.insert(assignment.workload_name.clone())
            {
                errors.push(AgentError::AssignmentConflict(
                    assignment.workload_name.to_string(),
                ));
                continue;
            }
            let payload = serde_json::to_string(assignment)?;
            let prior: Option<(u64, String)> = transaction
                .query_row(
                    "SELECT generation,payload_json FROM agent_assignments WHERE workload_name=?",
                    params![assignment.workload_name.as_str()],
                    |row| Ok((row.get(0)?, row.get(1)?)),
                )
                .optional()?;
            if set.revision == current_revision
                && prior.as_ref().is_none_or(|(_, prior)| prior != &payload)
            {
                errors.push(AgentError::AssignmentConflict(
                    assignment.workload_name.to_string(),
                ));
                continue;
            }
            match prior {
                Some((generation, _)) if assignment.assignment_generation < generation => continue,
                Some((generation, prior))
                    if assignment.assignment_generation == generation && prior != payload =>
                {
                    errors.push(AgentError::AssignmentConflict(
                        assignment.workload_name.to_string(),
                    ));
                }
                Some((generation, _)) if assignment.assignment_generation == generation => {}
                _ => {
                    transaction.execute(
                        "INSERT INTO agent_assignments(workload_name,generation,payload_json) VALUES (?,?,?)
                         ON CONFLICT(workload_name) DO UPDATE SET generation=excluded.generation,payload_json=excluded.payload_json",
                        params![assignment.workload_name.as_str(), assignment.assignment_generation, payload],
                    )?;
                }
            }
        }
        if errors.is_empty() {
            transaction.execute(
                "UPDATE agent_metadata SET value=? WHERE key='assignmentSetRevision'",
                params![set.revision],
            )?;
        }
        transaction.commit()?;
        Ok(errors)
    }

    pub fn assignments(&self) -> Result<Vec<WorkerAssignment>, AgentError> {
        let connection = self
            .connection
            .lock()
            .map_err(|_| AgentError::LockPoisoned)?;
        let mut statement = connection
            .prepare("SELECT payload_json FROM agent_assignments ORDER BY workload_name")?;
        statement
            .query_map([], |row| row.get::<_, String>(0))?
            .map(|row| Ok(serde_json::from_str(&row?)?))
            .collect()
    }

    pub fn accepted_generations(&self) -> Result<BTreeMap<ServiceName, u64>, AgentError> {
        Ok(self
            .assignments()?
            .into_iter()
            .map(|assignment| (assignment.workload_name, assignment.assignment_generation))
            .collect())
    }

    pub fn next_observation_sequence(&self, workload: &ServiceName) -> Result<u64, AgentError> {
        let connection = self
            .connection
            .lock()
            .map_err(|_| AgentError::LockPoisoned)?;
        let sequence = connection
            .query_row(
                "SELECT sequence FROM agent_observations WHERE workload_name=?",
                params![workload.as_str()],
                |row| row.get::<_, u64>(0),
            )
            .optional()?
            .unwrap_or(0);
        Ok(sequence + 1)
    }

    pub fn save_observation(&self, observation: &ObservedState) -> Result<(), AgentError> {
        let connection = self
            .connection
            .lock()
            .map_err(|_| AgentError::LockPoisoned)?;
        connection.execute(
            "INSERT INTO agent_observations(workload_name,sequence,payload_json) VALUES (?,?,?)
             ON CONFLICT(workload_name) DO UPDATE SET sequence=excluded.sequence,payload_json=excluded.payload_json",
            params![observation.workload_name.as_str(), observation.observation_sequence, serde_json::to_string(observation)?],
        )?;
        Ok(())
    }
}

#[derive(Clone)]
pub struct LifecycleClient {
    client: reqwest::Client,
    base_url: String,
    lifecycle_tokens_dir: std::path::PathBuf,
}

impl LifecycleClient {
    pub fn new(
        base_url: String,
        lifecycle_tokens_dir: std::path::PathBuf,
    ) -> Result<Self, AgentError> {
        let client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(330))
            .build()
            .map_err(|error| AgentError::Lifecycle(error.to_string()))?;
        Ok(Self {
            client,
            base_url: base_url.trim_end_matches('/').to_owned(),
            lifecycle_tokens_dir,
        })
    }

    async fn token(&self, service: &ServiceName) -> Result<String, AgentError> {
        let path = self
            .lifecycle_tokens_dir
            .join(format!("{}.token", service.as_str()));
        let metadata = fs::metadata(&path)
            .map_err(|error| AgentError::Lifecycle(format!("{}: {error}", path.display())))?;
        if metadata.permissions().mode() & 0o077 != 0 {
            return Err(AgentError::Lifecycle(format!(
                "{} must be mode 0600",
                path.display()
            )));
        }
        fs::read_to_string(path)
            .map(|value| value.trim().to_owned())
            .map_err(|error| AgentError::Lifecycle(error.to_string()))
    }

    pub async fn reconcile(
        &self,
        assignment: &WorkerAssignment,
        now: i64,
        observation_sequence: u64,
    ) -> ObservedState {
        let result = match &assignment.desired {
            WorkerAssignmentAction::EnsurePresent {
                release,
                release_digest,
                ..
            } => {
                self.ensure_present(assignment, release, release_digest)
                    .await
            }
            WorkerAssignmentAction::EnsureAbsent => self.ensure_absent(assignment).await,
        };
        match result {
            Ok(mut state) => {
                state.observed_at = now;
                state.observation_sequence = observation_sequence;
                state
            }
            Err(error) => ObservedState {
                worker_id: assignment.worker_id.clone(),
                workload_name: assignment.workload_name.clone(),
                assignment_generation: assignment.assignment_generation,
                observation_sequence,
                observed_at: now,
                phase: ObservedPhase::Failed,
                release_digest: None,
                deployment_id: None,
                units: BTreeMap::new(),
                routing_status: None,
                error: Some(ObservedError {
                    code: "reconcile_failed".into(),
                    message: error.to_string(),
                }),
            },
        }
    }

    async fn ensure_present(
        &self,
        assignment: &WorkerAssignment,
        release: &arcturus_contracts::ServiceReleaseEnvelope,
        release_digest: &arcturus_contracts::Sha256Digest,
    ) -> Result<ObservedState, AgentError> {
        let token = self.token(&assignment.workload_name).await?;
        let url = format!(
            "{}/v1/services/{}/active",
            self.base_url, assignment.workload_name
        );
        let active_response = self
            .client
            .get(&url)
            .bearer_auth(&token)
            .send()
            .await
            .map_err(|error| AgentError::Lifecycle(error.to_string()))?;
        let mut active: Option<serde_json::Value> =
            if active_response.status() == reqwest::StatusCode::NOT_FOUND {
                None
            } else {
                Some(
                    active_response
                        .error_for_status()
                        .map_err(|error| AgentError::Lifecycle(error.to_string()))?
                        .json()
                        .await
                        .map_err(|error| AgentError::Lifecycle(error.to_string()))?,
                )
            };
        let matches = active
            .as_ref()
            .and_then(|value| value.get("manifest_digest"))
            .and_then(serde_json::Value::as_str)
            == Some(release_digest.as_str());
        if !matches {
            let request = serde_json::json!({
                "service": assignment.workload_name,
                "commit_sha": release.metadata.revision,
                "manifest": release,
            });
            let response = self
                .client
                .post(format!("{}/v1/deployments", self.base_url))
                .bearer_auth(&token)
                .json(&request)
                .send()
                .await
                .map_err(|error| AgentError::Lifecycle(error.to_string()))?
                .error_for_status()
                .map_err(|error| AgentError::Lifecycle(error.to_string()))?;
            active = Some(
                response
                    .json()
                    .await
                    .map_err(|error| AgentError::Lifecycle(error.to_string()))?,
            );
        } else if active
            .as_ref()
            .and_then(|value| value.get("desired_state"))
            .and_then(serde_json::Value::as_str)
            == Some("disabled")
        {
            self.client
                .post(format!(
                    "{}/v1/services/{}/enable",
                    self.base_url, assignment.workload_name
                ))
                .bearer_auth(&token)
                .send()
                .await
                .map_err(|error| AgentError::Lifecycle(error.to_string()))?
                .error_for_status()
                .map_err(|error| AgentError::Lifecycle(error.to_string()))?;
        }
        let active = active.ok_or_else(|| {
            AgentError::Lifecycle("lifecycle returned no active deployment".into())
        })?;
        let actual_digest = active
            .get("manifest_digest")
            .and_then(serde_json::Value::as_str)
            .ok_or_else(|| AgentError::Lifecycle("active deployment has no manifest digest".into()))
            .and_then(|value| {
                arcturus_contracts::Sha256Digest::try_from(value.to_owned())
                    .map_err(|error| AgentError::Lifecycle(error.to_string()))
            })?;
        let digest_matches = &actual_digest == release_digest;
        let units: Vec<String> = active
            .get("health")
            .and_then(|value| value.get("units"))
            .and_then(serde_json::Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(serde_json::Value::as_str)
            .map(ToOwned::to_owned)
            .collect();
        let mut live_units = live_unit_states(units.clone()).await;
        let mut all_active =
            !live_units.is_empty() && live_units.values().all(|state| state == "active");
        if digest_matches && !all_active {
            self.client
                .post(format!(
                    "{}/v1/services/{}/enable",
                    self.base_url, assignment.workload_name
                ))
                .bearer_auth(&token)
                .send()
                .await
                .map_err(|error| AgentError::Lifecycle(error.to_string()))?
                .error_for_status()
                .map_err(|error| AgentError::Lifecycle(error.to_string()))?;
            live_units = live_unit_states(units).await;
            all_active =
                !live_units.is_empty() && live_units.values().all(|state| state == "active");
        }
        let routing = active
            .get("routing")
            .and_then(|value| value.get("status"))
            .and_then(serde_json::Value::as_str)
            .map(ToOwned::to_owned);
        Ok(ObservedState {
            worker_id: assignment.worker_id.clone(),
            workload_name: assignment.workload_name.clone(),
            assignment_generation: assignment.assignment_generation,
            observation_sequence: 0,
            observed_at: 0,
            phase: if digest_matches && all_active {
                ObservedPhase::Healthy
            } else {
                ObservedPhase::Degraded
            },
            release_digest: Some(actual_digest),
            deployment_id: active
                .get("deployment_id")
                .and_then(serde_json::Value::as_str)
                .map(ToOwned::to_owned),
            units: live_units,
            routing_status: routing,
            error: (!digest_matches).then_some(ObservedError {
                code: "release_digest_mismatch".into(),
                message: "active lifecycle release does not match the assignment".into(),
            }),
        })
    }

    async fn ensure_absent(
        &self,
        assignment: &WorkerAssignment,
    ) -> Result<ObservedState, AgentError> {
        let token = self.token(&assignment.workload_name).await?;
        let response = self
            .client
            .delete(format!(
                "{}/v1/services/{}",
                self.base_url, assignment.workload_name
            ))
            .bearer_auth(&token)
            .send()
            .await
            .map_err(|error| AgentError::Lifecycle(error.to_string()))?;
        if response.status() != reqwest::StatusCode::NOT_FOUND {
            response
                .error_for_status()
                .map_err(|error| AgentError::Lifecycle(error.to_string()))?;
        }
        Ok(ObservedState {
            worker_id: assignment.worker_id.clone(),
            workload_name: assignment.workload_name.clone(),
            assignment_generation: assignment.assignment_generation,
            observation_sequence: 0,
            observed_at: 0,
            phase: ObservedPhase::Absent,
            release_digest: None,
            deployment_id: None,
            units: BTreeMap::new(),
            routing_status: Some("withdrawn".into()),
            error: None,
        })
    }
}

async fn live_unit_states(units: Vec<String>) -> BTreeMap<String, String> {
    let mut result = BTreeMap::new();
    for unit in units {
        let state = tokio::process::Command::new("systemctl")
            .args(["--user", "is-active", &unit])
            .output()
            .await
            .ok()
            .and_then(|output| String::from_utf8(output.stdout).ok())
            .map(|value| value.trim().to_owned())
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| "unknown".into());
        result.insert(unit, state);
    }
    result
}

pub fn unix_timestamp() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|value| value.as_secs() as i64)
        .unwrap_or_default()
}

pub fn worker(value: &str) -> Result<WorkerId, AgentError> {
    WorkerId::try_from(value.to_owned()).map_err(|error| AgentError::Lifecycle(error.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use arcturus_contracts::{ReleaseMetadata, Revision, Sha256Digest, WorkerAssignmentAction};
    use std::collections::BTreeMap;
    use tempfile::TempDir;

    fn assignment(name: &str, generation: u64) -> WorkerAssignment {
        let workload = ServiceName::try_from(name.to_owned()).unwrap();
        WorkerAssignment {
            assignment_id: format!("{name}-{generation}"),
            worker_id: WorkerId::try_from("worker-a".to_owned()).unwrap(),
            workload_name: workload.clone(),
            assignment_generation: generation,
            intent_generation: 1,
            placement_decision_id: "decision".into(),
            issued_at: 1,
            desired: WorkerAssignmentAction::EnsurePresent {
                release: arcturus_contracts::ServiceReleaseEnvelope {
                    api_version: arcturus_contracts::SERVICE_RELEASE_API_VERSION.into(),
                    kind: arcturus_contracts::SERVICE_RELEASE_KIND.into(),
                    metadata: ReleaseMetadata {
                        name: workload,
                        revision: Revision::try_from("1".repeat(40)).unwrap(),
                        deployment_id: None,
                    },
                    spec: serde_json::json!({"components": {}}),
                },
                release_digest: Sha256Digest::try_from(format!("sha256:{}", "a".repeat(64)))
                    .unwrap(),
                resource_bindings: vec![],
            },
        }
    }

    #[test]
    fn persists_multiple_workloads_and_ignores_empty_unchanged_poll() {
        let temp = TempDir::new().unwrap();
        let path = temp.path().join("state.sqlite3");
        let store = AgentStore::open(&path).unwrap();
        store
            .accept_assignment_set(
                &worker("worker-a").unwrap(),
                &WorkerAssignmentSet {
                    worker_id: worker("worker-a").unwrap(),
                    revision: 2,
                    changed: true,
                    assignments: vec![assignment("alpha", 1), assignment("beta", 1)],
                    poll_after_seconds: 5,
                },
            )
            .unwrap();
        store
            .accept_assignment_set(
                &worker("worker-a").unwrap(),
                &WorkerAssignmentSet {
                    worker_id: worker("worker-a").unwrap(),
                    revision: 2,
                    changed: false,
                    assignments: vec![],
                    poll_after_seconds: 5,
                },
            )
            .unwrap();
        drop(store);
        let reopened = AgentStore::open(path).unwrap();
        assert_eq!(reopened.assignments().unwrap().len(), 2);
        assert_eq!(reopened.assignment_set_revision().unwrap(), 2);
    }

    #[test]
    fn pressure_sequence_survives_agent_restart() {
        let temp = TempDir::new().unwrap();
        let path = temp.path().join("state.sqlite3");
        let store = AgentStore::open(&path).unwrap();
        assert_eq!(store.next_pressure_sequence().unwrap(), 1);
        assert_eq!(store.next_pressure_sequence().unwrap(), 2);
        drop(store);
        let reopened = AgentStore::open(path).unwrap();
        assert_eq!(reopened.next_pressure_sequence().unwrap(), 3);
    }

    #[test]
    fn foreign_or_stale_assignment_sets_fail_without_changing_cached_state() {
        let temp = TempDir::new().unwrap();
        let store = AgentStore::open(temp.path().join("state.sqlite3")).unwrap();
        let expected = worker("worker-a").unwrap();
        store
            .accept_assignment_set(
                &expected,
                &WorkerAssignmentSet {
                    worker_id: expected.clone(),
                    revision: 2,
                    changed: true,
                    assignments: vec![assignment("alpha", 1)],
                    poll_after_seconds: 5,
                },
            )
            .unwrap();
        let foreign = WorkerAssignmentSet {
            worker_id: worker("worker-b").unwrap(),
            revision: 3,
            changed: true,
            assignments: vec![assignment("beta", 1)],
            poll_after_seconds: 5,
        };
        assert!(store.accept_assignment_set(&expected, &foreign).is_err());
        let stale = WorkerAssignmentSet {
            worker_id: expected.clone(),
            revision: 1,
            changed: true,
            assignments: vec![assignment("beta", 1)],
            poll_after_seconds: 5,
        };
        assert!(store.accept_assignment_set(&expected, &stale).is_err());
        assert_eq!(store.assignment_set_revision().unwrap(), 2);
        assert_eq!(store.assignments().unwrap().len(), 1);
        assert_eq!(
            store.assignments().unwrap()[0].workload_name,
            ServiceName::try_from("alpha".to_owned()).unwrap()
        );
    }

    #[test]
    fn conflict_is_local_and_does_not_overwrite_other_workloads() {
        let temp = TempDir::new().unwrap();
        let store = AgentStore::open(temp.path().join("state.sqlite3")).unwrap();
        store
            .accept_assignment_set(
                &worker("worker-a").unwrap(),
                &WorkerAssignmentSet {
                    worker_id: worker("worker-a").unwrap(),
                    revision: 1,
                    changed: true,
                    assignments: vec![assignment("alpha", 1), assignment("beta", 1)],
                    poll_after_seconds: 5,
                },
            )
            .unwrap();
        let mut conflict = assignment("alpha", 1);
        conflict.assignment_id = "different".into();
        let errors = store
            .accept_assignment_set(
                &worker("worker-a").unwrap(),
                &WorkerAssignmentSet {
                    worker_id: worker("worker-a").unwrap(),
                    revision: 2,
                    changed: true,
                    assignments: vec![conflict, assignment("beta", 2)],
                    poll_after_seconds: 5,
                },
            )
            .unwrap();
        assert_eq!(errors.len(), 1);
        let generations: BTreeMap<_, _> = store
            .accepted_generations()
            .unwrap()
            .into_iter()
            .map(|(name, generation)| (name.to_string(), generation))
            .collect();
        assert_eq!(generations["alpha"], 1);
        assert_eq!(generations["beta"], 2);
        assert_eq!(store.assignment_set_revision().unwrap(), 1);
    }
}
