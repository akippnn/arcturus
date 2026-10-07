use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::sync::{Arc, Mutex};

use arcturus_contracts::{
    AuthoritativeState, CandidateEvaluation, FLEET_API_VERSION, Movability, ObservedPhase,
    ObservedState, OverlapSafety, PlacementDecision, RequirementState, ResourceOwnership,
    ResourceRecord, ServiceName, TransitionClaim, WorkerAssignment, WorkerAssignmentAction,
    WorkerAssignmentSet, WorkerEnrollmentResponse, WorkerHeartbeat, WorkerId, WorkerRegistration,
    WorkerRegistrationRequest, WorkloadIntent,
};
use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use getrandom::fill;
use rusqlite::{Connection, OptionalExtension, params};
use scrypt::{Params as ScryptParams, scrypt};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;
use thiserror::Error;
use uuid::Uuid;

const WORKER_SECRET_DOMAIN: &[u8] = b"arcturus-worker-credential-v1\0";
const HEARTBEAT_FRESH_SECONDS: i64 = 30;
const POLL_AFTER_SECONDS: u16 = 5;

#[derive(Debug, Error)]
pub enum FleetError {
    #[error("fleet object was not found: {0}")]
    NotFound(String),
    #[error("fleet object conflicts with stored state: {0}")]
    Conflict(String),
    #[error("fleet object is invalid: {0}")]
    Invalid(String),
    #[error("fleet operation is not authorized")]
    Unauthorized,
    #[error("fleet persistence failed: {0}")]
    Database(String),
    #[error("fleet state lock is poisoned")]
    LockPoisoned,
}

impl From<rusqlite::Error> for FleetError {
    fn from(value: rusqlite::Error) -> Self {
        Self::Database(value.to_string())
    }
}

impl From<serde_json::Error> for FleetError {
    fn from(value: serde_json::Error) -> Self {
        Self::Invalid(value.to_string())
    }
}

#[derive(Clone)]
pub struct FleetStore {
    connection: Arc<Mutex<Connection>>,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct WorkerView {
    #[serde(flatten)]
    pub registration: WorkerRegistration,
    pub last_heartbeat: Option<WorkerHeartbeat>,
    pub heartbeat_received_at: Option<i64>,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct WorkloadView {
    pub intent: WorkloadIntent,
    pub decision: PlacementDecision,
    pub assignments: Vec<WorkerAssignment>,
    pub observations: Vec<ObservedState>,
    pub movement_phase: Option<String>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct MoveRequest {
    pub expected_intent_generation: u64,
    pub worker_id: Option<WorkerId>,
    pub architecture: Option<String>,
}

impl FleetStore {
    pub fn open(path: impl AsRef<Path>) -> Result<Self, FleetError> {
        let path = path.as_ref();
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).map_err(|error| FleetError::Database(error.to_string()))?;
            fs::set_permissions(parent, fs::Permissions::from_mode(0o700))
                .map_err(|error| FleetError::Database(error.to_string()))?;
        }
        let connection = Connection::open(path)?;
        fs::set_permissions(path, fs::Permissions::from_mode(0o600))
            .map_err(|error| FleetError::Database(error.to_string()))?;
        connection.execute_batch(
            "PRAGMA journal_mode=WAL;
             PRAGMA foreign_keys=ON;
             CREATE TABLE IF NOT EXISTS fleet_workers (
               worker_id TEXT PRIMARY KEY,
               registration_json TEXT NOT NULL,
               credential_salt BLOB NOT NULL,
               credential_hash BLOB NOT NULL,
               heartbeat_json TEXT,
               heartbeat_received_at INTEGER
             );
             CREATE TABLE IF NOT EXISTS fleet_resources (
               resource_id TEXT PRIMARY KEY,
               generation INTEGER NOT NULL,
               payload_json TEXT NOT NULL
             );
             CREATE TABLE IF NOT EXISTS fleet_intents (
               workload_name TEXT PRIMARY KEY,
               generation INTEGER NOT NULL,
               payload_json TEXT NOT NULL
             );
             CREATE TABLE IF NOT EXISTS fleet_decisions (
               decision_id TEXT PRIMARY KEY,
               workload_name TEXT NOT NULL,
               intent_generation INTEGER NOT NULL,
               payload_json TEXT NOT NULL
             );
             CREATE TABLE IF NOT EXISTS fleet_assignments (
               worker_id TEXT NOT NULL,
               workload_name TEXT NOT NULL,
               generation INTEGER NOT NULL,
               payload_json TEXT NOT NULL,
               PRIMARY KEY(worker_id, workload_name)
             );
             CREATE TABLE IF NOT EXISTS fleet_worker_revisions (
               worker_id TEXT PRIMARY KEY,
               revision INTEGER NOT NULL
             );
             CREATE TABLE IF NOT EXISTS fleet_observations (
               worker_id TEXT NOT NULL,
               workload_name TEXT NOT NULL,
               observation_sequence INTEGER NOT NULL,
               assignment_generation INTEGER NOT NULL,
               payload_json TEXT NOT NULL,
               PRIMARY KEY(worker_id, workload_name)
             );
             CREATE TABLE IF NOT EXISTS fleet_movements (
               workload_name TEXT PRIMARY KEY,
               intent_generation INTEGER NOT NULL,
               target_worker_id TEXT NOT NULL,
               source_workers_json TEXT NOT NULL,
               phase TEXT NOT NULL
             );",
        )?;
        Ok(Self {
            connection: Arc::new(Mutex::new(connection)),
        })
    }

    pub fn enroll(
        &self,
        request: WorkerRegistrationRequest,
        now: i64,
    ) -> Result<WorkerEnrollmentResponse, FleetError> {
        validate_labels(
            &request.attestations.capabilities,
            &request.attestations.topology,
        )?;
        let registration = WorkerRegistration {
            worker_id: request.worker_id.clone(),
            display_name: request.display_name,
            enabled: true,
            attestations: request.attestations,
            enrolled_at: now,
        };
        let credential = random_credential()?;
        let (salt, hash) = hash_worker_credential(&credential)?;
        let mut connection = self
            .connection
            .lock()
            .map_err(|_| FleetError::LockPoisoned)?;
        let transaction = connection.transaction()?;
        transaction
            .execute(
                "INSERT INTO fleet_workers(worker_id,registration_json,credential_salt,credential_hash)
                 VALUES (?,?,?,?)",
                params![
                    registration.worker_id.as_str(),
                    serde_json::to_string(&registration)?,
                    salt,
                    hash
                ],
            )
            .map_err(|error| match error {
                rusqlite::Error::SqliteFailure(inner, _)
                    if inner.code == rusqlite::ErrorCode::ConstraintViolation =>
                {
                    FleetError::Conflict(format!("worker {} is already enrolled", registration.worker_id))
                }
                other => FleetError::from(other),
            })?;
        transaction.execute(
            "INSERT INTO fleet_worker_revisions(worker_id,revision) VALUES (?,0)",
            params![registration.worker_id.as_str()],
        )?;
        transaction.commit()?;
        Ok(WorkerEnrollmentResponse {
            worker: registration,
            credential,
        })
    }

    pub fn list_workers(&self) -> Result<Vec<WorkerView>, FleetError> {
        let connection = self
            .connection
            .lock()
            .map_err(|_| FleetError::LockPoisoned)?;
        let mut statement = connection.prepare(
            "SELECT registration_json,heartbeat_json,heartbeat_received_at
             FROM fleet_workers ORDER BY worker_id",
        )?;
        let rows = statement.query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, Option<String>>(1)?,
                row.get::<_, Option<i64>>(2)?,
            ))
        })?;
        rows.map(|row| {
            let (registration, heartbeat, received_at) = row?;
            Ok(WorkerView {
                registration: serde_json::from_str(&registration)?,
                last_heartbeat: heartbeat.as_deref().map(serde_json::from_str).transpose()?,
                heartbeat_received_at: received_at,
            })
        })
        .collect()
    }

    pub fn authorize_worker(
        &self,
        worker_id: &WorkerId,
        authorization: &str,
    ) -> Result<(), FleetError> {
        let token = bearer(authorization)?;
        let connection = self
            .connection
            .lock()
            .map_err(|_| FleetError::LockPoisoned)?;
        let stored = connection
            .query_row(
                "SELECT credential_salt,credential_hash FROM fleet_workers WHERE worker_id=?",
                params![worker_id.as_str()],
                |row| Ok((row.get::<_, Vec<u8>>(0)?, row.get::<_, Vec<u8>>(1)?)),
            )
            .optional()?;
        drop(connection);
        let Some((salt, expected)) = stored else {
            return Err(FleetError::Unauthorized);
        };
        let actual = derive_worker_hash(token, &salt, expected.len())?;
        if bool::from(actual.ct_eq(&expected)) {
            Ok(())
        } else {
            Err(FleetError::Unauthorized)
        }
    }

    pub fn heartbeat(&self, heartbeat: &WorkerHeartbeat, now: i64) -> Result<(), FleetError> {
        if heartbeat.worker_id.as_str().is_empty()
            || heartbeat.inventory.inventory_generation == 0
            || heartbeat.pressure.pressure_sequence == 0
            || heartbeat
                .pressure
                .cpu_utilization_basis_points
                .is_some_and(|value| value > 10_000)
        {
            return Err(FleetError::Invalid(
                "heartbeat generations and measurements are invalid".into(),
            ));
        }
        let connection = self
            .connection
            .lock()
            .map_err(|_| FleetError::LockPoisoned)?;
        let prior: Option<String> = connection
            .query_row(
                "SELECT heartbeat_json FROM fleet_workers WHERE worker_id=?",
                params![heartbeat.worker_id.as_str()],
                |row| row.get(0),
            )
            .optional()?
            .flatten();
        let Some(_) = connection
            .query_row(
                "SELECT 1 FROM fleet_workers WHERE worker_id=?",
                params![heartbeat.worker_id.as_str()],
                |row| row.get::<_, i64>(0),
            )
            .optional()?
        else {
            return Err(FleetError::NotFound(heartbeat.worker_id.to_string()));
        };
        if let Some(prior) = prior {
            let prior: WorkerHeartbeat = serde_json::from_str(&prior)?;
            if heartbeat.inventory.inventory_generation < prior.inventory.inventory_generation
                || heartbeat.pressure.pressure_sequence <= prior.pressure.pressure_sequence
            {
                return Err(FleetError::Conflict("heartbeat generation is stale".into()));
            }
        }
        connection.execute(
            "UPDATE fleet_workers SET heartbeat_json=?,heartbeat_received_at=? WHERE worker_id=?",
            params![
                serde_json::to_string(heartbeat)?,
                now,
                heartbeat.worker_id.as_str()
            ],
        )?;
        Ok(())
    }

    pub fn import_resource(&self, resource: &ResourceRecord) -> Result<ResourceRecord, FleetError> {
        if resource.api_version != FLEET_API_VERSION || resource.kind != "Resource" {
            return Err(FleetError::Invalid(
                "unsupported resource apiVersion or kind".into(),
            ));
        }
        if resource.ownership != ResourceOwnership::Imported {
            return Err(FleetError::Invalid(
                "managed resources are not implemented in DIST-001".into(),
            ));
        }
        if resource.generation == 0
            || resource.resource_type.is_empty()
            || resource.protocol.is_empty()
        {
            return Err(FleetError::Invalid(
                "resource generation, type, and protocol are required".into(),
            ));
        }
        let payload = serde_json::to_string(resource)?;
        let connection = self
            .connection
            .lock()
            .map_err(|_| FleetError::LockPoisoned)?;
        let existing: Option<(u64, String)> = connection
            .query_row(
                "SELECT generation,payload_json FROM fleet_resources WHERE resource_id=?",
                params![resource.resource_id.as_str()],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?;
        match existing {
            Some((generation, prior)) if generation == resource.generation && prior == payload => {
                return Ok(resource.clone());
            }
            Some((generation, _)) if resource.generation != generation + 1 => {
                return Err(FleetError::Conflict(format!(
                    "resource generation must be {}",
                    generation + 1
                )));
            }
            None if resource.generation != 1 => {
                return Err(FleetError::Conflict(
                    "initial resource generation must be 1".into(),
                ));
            }
            _ => {}
        }
        connection.execute(
            "INSERT INTO fleet_resources(resource_id,generation,payload_json) VALUES (?,?,?)
             ON CONFLICT(resource_id) DO UPDATE SET generation=excluded.generation,payload_json=excluded.payload_json",
            params![resource.resource_id.as_str(), resource.generation, payload],
        )?;
        Ok(resource.clone())
    }

    pub fn apply_intent(
        &self,
        intent: &WorkloadIntent,
        now: i64,
    ) -> Result<WorkloadView, FleetError> {
        self.apply_intent_inner(intent, now, false)
    }

    pub fn move_workload(
        &self,
        workload: &ServiceName,
        request: MoveRequest,
        now: i64,
    ) -> Result<WorkloadView, FleetError> {
        if request.worker_id.is_some() == request.architecture.is_some() {
            return Err(FleetError::Invalid(
                "move requires exactly one of workerId or architecture".into(),
            ));
        }
        let mut intent = self.intent(workload)?;
        if intent.intent_generation != request.expected_intent_generation {
            return Err(FleetError::Conflict(
                "expected intent generation does not match".into(),
            ));
        }
        intent.intent_generation += 1;
        intent.placement.worker_id = request.worker_id;
        intent.placement.architecture = request.architecture;
        self.apply_intent_inner(&intent, now, true)
    }

    fn apply_intent_inner(
        &self,
        intent: &WorkloadIntent,
        now: i64,
        exclude_current: bool,
    ) -> Result<WorkloadView, FleetError> {
        validate_intent(intent)?;
        let mut connection = self
            .connection
            .lock()
            .map_err(|_| FleetError::LockPoisoned)?;
        let transaction = connection.transaction()?;
        validate_resources(&transaction, intent)?;
        let payload = serde_json::to_string(intent)?;
        let existing: Option<(u64, String)> = transaction
            .query_row(
                "SELECT generation,payload_json FROM fleet_intents WHERE workload_name=?",
                params![intent.workload_name.as_str()],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?;
        match existing {
            Some((generation, prior))
                if generation == intent.intent_generation && prior == payload =>
            {
                transaction.rollback()?;
                drop(connection);
                return self.workload(intent.workload_name.clone());
            }
            Some((generation, _)) if intent.intent_generation != generation + 1 => {
                return Err(FleetError::Conflict(format!(
                    "intent generation must be {}",
                    generation + 1
                )));
            }
            None if intent.intent_generation != 1 => {
                return Err(FleetError::Conflict(
                    "initial intent generation must be 1".into(),
                ));
            }
            _ => {}
        }
        let active_movement: Option<String> = transaction
            .query_row(
                "SELECT phase FROM fleet_movements WHERE workload_name=? AND phase!='complete'",
                params![intent.workload_name.as_str()],
                |row| row.get(0),
            )
            .optional()?;
        if let Some(phase) = active_movement {
            return Err(FleetError::Conflict(format!(
                "workload movement is already in progress ({phase})"
            )));
        }

        let source_workers = present_workers(&transaction, &intent.workload_name)?;
        let (mut decision, selected) =
            place(&transaction, intent, now, &source_workers, exclude_current)?;
        if selected
            .as_ref()
            .is_some_and(|target| source_workers.iter().any(|source| source == target))
        {
            decision
                .transition_reasons
                .push("target_is_current_source".into());
        }
        if selected
            .as_ref()
            .is_some_and(|target| !source_workers.is_empty() && !source_workers.contains(target))
        {
            let reasons = transition_refusals(intent);
            if !reasons.is_empty() {
                decision.status = "refused".into();
                decision.selected_worker_id = None;
                decision.transition_reasons.extend(reasons);
            }
        }
        transaction.execute(
            "INSERT INTO fleet_intents(workload_name,generation,payload_json) VALUES (?,?,?)
             ON CONFLICT(workload_name) DO UPDATE SET generation=excluded.generation,payload_json=excluded.payload_json",
            params![intent.workload_name.as_str(), intent.intent_generation, payload],
        )?;
        transaction.execute(
            "INSERT INTO fleet_decisions(decision_id,workload_name,intent_generation,payload_json) VALUES (?,?,?,?)",
            params![decision.decision_id, intent.workload_name.as_str(), intent.intent_generation, serde_json::to_string(&decision)?],
        )?;
        if decision.status == "placed" {
            let target = decision
                .selected_worker_id
                .clone()
                .expect("placed decision has target");
            issue_assignment(
                &transaction,
                &target,
                intent,
                &decision,
                WorkerAssignmentAction::EnsurePresent {
                    release: intent.release.clone(),
                    release_digest: intent.release_digest.clone(),
                    resource_bindings: intent.resource_bindings.clone(),
                },
                now,
            )?;
            let movement_sources: Vec<_> = source_workers
                .into_iter()
                .filter(|source| source != &target)
                .collect();
            if !movement_sources.is_empty() {
                transaction.execute(
                    "INSERT INTO fleet_movements(workload_name,intent_generation,target_worker_id,source_workers_json,phase)
                     VALUES (?,?,?,?, 'awaitingTarget')
                     ON CONFLICT(workload_name) DO UPDATE SET intent_generation=excluded.intent_generation,
                       target_worker_id=excluded.target_worker_id,source_workers_json=excluded.source_workers_json,phase=excluded.phase",
                    params![intent.workload_name.as_str(), intent.intent_generation, target.as_str(), serde_json::to_string(&movement_sources)?],
                )?;
            }
        }
        transaction.commit()?;
        drop(connection);
        self.workload(intent.workload_name.clone())
    }

    pub fn assignment_set(
        &self,
        worker_id: &WorkerId,
        after_revision: u64,
    ) -> Result<WorkerAssignmentSet, FleetError> {
        let connection = self
            .connection
            .lock()
            .map_err(|_| FleetError::LockPoisoned)?;
        let revision: u64 = connection
            .query_row(
                "SELECT revision FROM fleet_worker_revisions WHERE worker_id=?",
                params![worker_id.as_str()],
                |row| row.get(0),
            )
            .optional()?
            .ok_or_else(|| FleetError::NotFound(worker_id.to_string()))?;
        if revision == after_revision {
            return Ok(WorkerAssignmentSet {
                worker_id: worker_id.clone(),
                revision,
                changed: false,
                assignments: Vec::new(),
                poll_after_seconds: POLL_AFTER_SECONDS,
            });
        }
        if after_revision > revision {
            return Err(FleetError::Conflict(
                "assignment-set revision is ahead of the control plane".into(),
            ));
        }
        let mut statement = connection.prepare(
            "SELECT payload_json FROM fleet_assignments WHERE worker_id=? ORDER BY workload_name",
        )?;
        let assignments = statement
            .query_map(params![worker_id.as_str()], |row| row.get::<_, String>(0))?
            .map(|row| Ok(serde_json::from_str(&row?)?))
            .collect::<Result<Vec<_>, FleetError>>()?;
        Ok(WorkerAssignmentSet {
            worker_id: worker_id.clone(),
            revision,
            changed: true,
            assignments,
            poll_after_seconds: POLL_AFTER_SECONDS,
        })
    }

    pub fn observe(
        &self,
        observation: &ObservedState,
        now: i64,
    ) -> Result<WorkloadView, FleetError> {
        let mut connection = self
            .connection
            .lock()
            .map_err(|_| FleetError::LockPoisoned)?;
        let transaction = connection.transaction()?;
        let assignment: WorkerAssignment = transaction
            .query_row(
                "SELECT payload_json FROM fleet_assignments WHERE worker_id=? AND workload_name=?",
                params![
                    observation.worker_id.as_str(),
                    observation.workload_name.as_str()
                ],
                |row| row.get::<_, String>(0),
            )
            .optional()?
            .ok_or_else(|| FleetError::NotFound("worker assignment".into()))
            .and_then(|value| serde_json::from_str(&value).map_err(FleetError::from))?;
        if observation.assignment_generation != assignment.assignment_generation {
            return Err(FleetError::Conflict(
                "observation assignment generation is stale".into(),
            ));
        }
        let prior_sequence: Option<u64> = transaction
            .query_row(
                "SELECT observation_sequence FROM fleet_observations WHERE worker_id=? AND workload_name=?",
                params![observation.worker_id.as_str(), observation.workload_name.as_str()],
                |row| row.get(0),
            )
            .optional()?;
        if prior_sequence.is_some_and(|prior| observation.observation_sequence <= prior) {
            return Err(FleetError::Conflict("observation sequence is stale".into()));
        }
        transaction.execute(
            "INSERT INTO fleet_observations(worker_id,workload_name,observation_sequence,assignment_generation,payload_json)
             VALUES (?,?,?,?,?) ON CONFLICT(worker_id,workload_name) DO UPDATE SET
             observation_sequence=excluded.observation_sequence,assignment_generation=excluded.assignment_generation,payload_json=excluded.payload_json",
            params![observation.worker_id.as_str(), observation.workload_name.as_str(), observation.observation_sequence,
                observation.assignment_generation, serde_json::to_string(observation)?],
        )?;
        advance_movement(&transaction, observation, now)?;
        transaction.commit()?;
        let workload = observation.workload_name.clone();
        drop(connection);
        self.workload(workload)
    }

    pub fn list_workloads(&self) -> Result<Vec<WorkloadView>, FleetError> {
        let names = {
            let connection = self
                .connection
                .lock()
                .map_err(|_| FleetError::LockPoisoned)?;
            let mut statement = connection
                .prepare("SELECT workload_name FROM fleet_intents ORDER BY workload_name")?;
            statement
                .query_map([], |row| row.get::<_, String>(0))?
                .collect::<Result<Vec<_>, _>>()?
        };
        names
            .into_iter()
            .map(|name| {
                ServiceName::try_from(name).map_err(|error| FleetError::Invalid(error.to_string()))
            })
            .map(|name| name.and_then(|name| self.workload(name)))
            .collect()
    }

    pub fn workload(&self, workload: ServiceName) -> Result<WorkloadView, FleetError> {
        let connection = self
            .connection
            .lock()
            .map_err(|_| FleetError::LockPoisoned)?;
        let intent: WorkloadIntent = connection
            .query_row(
                "SELECT payload_json FROM fleet_intents WHERE workload_name=?",
                params![workload.as_str()],
                |row| row.get::<_, String>(0),
            )
            .optional()?
            .ok_or_else(|| FleetError::NotFound(workload.to_string()))
            .and_then(|value| serde_json::from_str(&value).map_err(FleetError::from))?;
        let decision: PlacementDecision = connection.query_row(
            "SELECT payload_json FROM fleet_decisions WHERE workload_name=? ORDER BY intent_generation DESC LIMIT 1",
            params![workload.as_str()], |row| row.get::<_, String>(0),
        ).and_then(|value| serde_json::from_str(&value).map_err(|error| rusqlite::Error::ToSqlConversionFailure(Box::new(error))))?;
        let assignments = load_json_rows::<WorkerAssignment>(
            &connection,
            "SELECT payload_json FROM fleet_assignments WHERE workload_name=? ORDER BY worker_id",
            workload.as_str(),
        )?;
        let observations = load_json_rows::<ObservedState>(
            &connection,
            "SELECT payload_json FROM fleet_observations WHERE workload_name=? ORDER BY worker_id",
            workload.as_str(),
        )?;
        let movement_phase = connection
            .query_row(
                "SELECT phase FROM fleet_movements WHERE workload_name=?",
                params![workload.as_str()],
                |row| row.get(0),
            )
            .optional()?;
        Ok(WorkloadView {
            intent,
            decision,
            assignments,
            observations,
            movement_phase,
        })
    }

    fn intent(&self, workload: &ServiceName) -> Result<WorkloadIntent, FleetError> {
        let connection = self
            .connection
            .lock()
            .map_err(|_| FleetError::LockPoisoned)?;
        connection
            .query_row(
                "SELECT payload_json FROM fleet_intents WHERE workload_name=?",
                params![workload.as_str()],
                |row| row.get::<_, String>(0),
            )
            .optional()?
            .ok_or_else(|| FleetError::NotFound(workload.to_string()))
            .and_then(|value| serde_json::from_str(&value).map_err(FleetError::from))
    }
}

fn validate_intent(intent: &WorkloadIntent) -> Result<(), FleetError> {
    if intent.api_version != FLEET_API_VERSION
        || intent.kind != "WorkloadIntent"
        || intent.intent_generation == 0
    {
        return Err(FleetError::Invalid(
            "unsupported intent apiVersion/kind or zero generation".into(),
        ));
    }
    intent
        .release
        .validate()
        .map_err(|error| FleetError::Invalid(error.to_string()))?;
    if intent.workload_name != intent.release.metadata.name
        || intent.workload_name != intent.release_characteristics.service_name
        || intent.release_digest != intent.release_characteristics.release_digest
    {
        return Err(FleetError::Invalid(
            "intent, release, and characteristics identities differ".into(),
        ));
    }
    if intent.placement.worker_id.is_some() == intent.placement.architecture.is_some() {
        return Err(FleetError::Invalid(
            "placement requires exactly one of workerId or architecture".into(),
        ));
    }
    if intent
        .transition
        .attestation
        .as_ref()
        .is_some_and(|attestation| {
            attestation.claims.is_empty() || attestation.rationale.trim().is_empty()
        })
    {
        return Err(FleetError::Invalid(
            "transition attestation requires claims and a rationale".into(),
        ));
    }
    Ok(())
}

fn validate_resources(connection: &Connection, intent: &WorkloadIntent) -> Result<(), FleetError> {
    let secret_uses: BTreeSet<_> = intent
        .release_characteristics
        .secret_uses
        .iter()
        .map(|item| (item.component.clone(), item.secret_name.clone()))
        .collect();
    for binding in &intent.resource_bindings {
        let resource: ResourceRecord = connection
            .query_row(
                "SELECT payload_json FROM fleet_resources WHERE resource_id=?",
                params![binding.resource_id.as_str()],
                |row| row.get::<_, String>(0),
            )
            .optional()?
            .ok_or_else(|| {
                FleetError::Invalid(format!("resource {} is not imported", binding.resource_id))
            })
            .and_then(|value| serde_json::from_str(&value).map_err(FleetError::from))?;
        if resource.generation != binding.resource_generation
            || resource.ownership != ResourceOwnership::Imported
        {
            return Err(FleetError::Invalid(format!(
                "resource {} generation or ownership is invalid",
                binding.resource_id
            )));
        }
        for projection in &binding.projections {
            let material = resource
                .binding_material
                .get(&projection.material)
                .ok_or_else(|| {
                    FleetError::Invalid(format!(
                        "resource material {} is missing",
                        projection.material
                    ))
                })?;
            if material.secret_name != projection.secret_name
                || !secret_uses
                    .contains(&(projection.component.clone(), projection.secret_name.clone()))
            {
                return Err(FleetError::Invalid(format!(
                    "resource projection {} does not match a release secret",
                    projection.material
                )));
            }
        }
    }
    Ok(())
}

fn place(
    connection: &Connection,
    intent: &WorkloadIntent,
    now: i64,
    sources: &[WorkerId],
    exclude_current: bool,
) -> Result<(PlacementDecision, Option<WorkerId>), FleetError> {
    let mut statement = connection.prepare(
        "SELECT registration_json,heartbeat_json,heartbeat_received_at FROM fleet_workers ORDER BY worker_id",
    )?;
    let rows = statement.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, Option<String>>(1)?,
            row.get::<_, Option<i64>>(2)?,
        ))
    })?;
    let mut candidates = Vec::new();
    let mut eligible = Vec::new();
    for row in rows {
        let (registration, heartbeat, received_at) = row?;
        let registration: WorkerRegistration = serde_json::from_str(&registration)?;
        let heartbeat: Option<WorkerHeartbeat> =
            heartbeat.as_deref().map(serde_json::from_str).transpose()?;
        let mut refusals = Vec::new();
        if !registration.enabled {
            refusals.push("worker_disabled".into());
        }
        if intent
            .placement
            .worker_id
            .as_ref()
            .is_some_and(|wanted| wanted != &registration.worker_id)
        {
            refusals.push("worker_id_mismatch".into());
        }
        if exclude_current && sources.contains(&registration.worker_id) {
            refusals.push("current_source".into());
        }
        if received_at.is_none_or(|received| now - received > HEARTBEAT_FRESH_SECONDS) {
            refusals.push("heartbeat_stale".into());
        }
        if let Some(heartbeat) = &heartbeat {
            if intent
                .placement
                .architecture
                .as_ref()
                .is_some_and(|wanted| wanted != &heartbeat.inventory.architecture)
            {
                refusals.push("architecture_mismatch".into());
            }
            if intent
                .placement
                .minimum_total_memory_bytes
                .is_some_and(|minimum| heartbeat.inventory.total_memory_bytes < minimum)
            {
                refusals.push("total_memory_insufficient".into());
            }
            let retaining_current = !exclude_current && sources.contains(&registration.worker_id);
            if !retaining_current
                && intent
                    .placement
                    .minimum_available_memory_bytes
                    .is_some_and(|minimum| heartbeat.pressure.available_memory_bytes < minimum)
            {
                refusals.push("available_memory_insufficient".into());
            }
        } else {
            refusals.push("inventory_unavailable".into());
        }
        if !intent
            .placement
            .required_capabilities
            .is_subset(&registration.attestations.capabilities)
        {
            refusals.push("capability_missing".into());
        }
        if intent
            .placement
            .required_topology
            .iter()
            .any(|(key, value)| registration.attestations.topology.get(key) != Some(value))
        {
            refusals.push("topology_mismatch".into());
        }
        let is_eligible = refusals.is_empty();
        if is_eligible {
            eligible.push(registration.worker_id.clone());
        }
        candidates.push(CandidateEvaluation {
            worker_id: registration.worker_id,
            eligible: is_eligible,
            refusal_codes: refusals,
        });
    }
    let retained = if !exclude_current {
        sources
            .iter()
            .find(|source| eligible.contains(source))
            .cloned()
    } else {
        None
    };
    let selected = retained.or_else(|| eligible.into_iter().next());
    let status = if selected.is_some() {
        "placed"
    } else {
        "refused"
    };
    let decision = PlacementDecision {
        decision_id: Uuid::new_v4().to_string(),
        workload_name: intent.workload_name.clone(),
        intent_generation: intent.intent_generation,
        selected_worker_id: selected.clone(),
        status: status.into(),
        candidates,
        transition_reasons: Vec::new(),
        created_at: now,
    };
    Ok((decision, selected))
}

fn transition_refusals(intent: &WorkloadIntent) -> Vec<String> {
    let mut reasons = Vec::new();
    if intent.transition.movability != Movability::Movable {
        reasons.push("transition_not_movable".into());
    }
    if intent.transition.overlap != OverlapSafety::Safe {
        reasons.push("transition_overlap_not_safe".into());
    }
    if matches!(
        intent.transition.authoritative_state,
        AuthoritativeState::NodeLocal | AuthoritativeState::Unknown
    ) {
        reasons.push("transition_authoritative_state_not_movable".into());
    }
    if intent.transition.migration != RequirementState::NotRequired {
        reasons.push("transition_migration_required_or_unknown".into());
    }
    if intent.transition.fencing != RequirementState::NotRequired {
        reasons.push("transition_fencing_required_or_unknown".into());
    }
    if intent.release_characteristics.has_legacy_migration {
        reasons.push("transition_legacy_migration".into());
    }
    let claims = intent
        .transition
        .attestation
        .as_ref()
        .map(|value| &value.claims);
    if intent
        .release_characteristics
        .component_modes
        .iter()
        .any(|mode| mode != "service")
        && !claims.is_some_and(|claims| claims.contains(&TransitionClaim::DuplicateExecutionSafe))
    {
        reasons.push("transition_duplicate_execution_unattested".into());
    }
    if intent.release_characteristics.read_only_local_volumes > 0
        || intent.release_characteristics.writable_local_volumes > 0
    {
        let reconstructable = intent.transition.authoritative_state
            == AuthoritativeState::ReconstructableLocal
            && claims
                .is_some_and(|claims| claims.contains(&TransitionClaim::LocalStateReconstructable));
        if !reconstructable {
            reasons.push("transition_local_state_unattested".into());
        }
    }
    reasons
}

fn present_workers(
    connection: &Connection,
    workload: &ServiceName,
) -> Result<Vec<WorkerId>, FleetError> {
    let mut statement = connection.prepare(
        "SELECT payload_json FROM fleet_assignments WHERE workload_name=? ORDER BY worker_id",
    )?;
    let assignments =
        statement.query_map(params![workload.as_str()], |row| row.get::<_, String>(0))?;
    let mut workers = Vec::new();
    for assignment in assignments {
        let assignment: WorkerAssignment = serde_json::from_str(&assignment?)?;
        if matches!(
            assignment.desired,
            WorkerAssignmentAction::EnsurePresent { .. }
        ) {
            workers.push(assignment.worker_id);
        }
    }
    Ok(workers)
}

fn issue_assignment(
    connection: &Connection,
    worker: &WorkerId,
    intent: &WorkloadIntent,
    decision: &PlacementDecision,
    desired: WorkerAssignmentAction,
    now: i64,
) -> Result<WorkerAssignment, FleetError> {
    let generation: u64 = connection
        .query_row(
            "SELECT generation FROM fleet_assignments WHERE worker_id=? AND workload_name=?",
            params![worker.as_str(), intent.workload_name.as_str()],
            |row| row.get(0),
        )
        .optional()?
        .unwrap_or(0)
        + 1;
    let assignment = WorkerAssignment {
        assignment_id: Uuid::new_v4().to_string(),
        worker_id: worker.clone(),
        workload_name: intent.workload_name.clone(),
        assignment_generation: generation,
        intent_generation: intent.intent_generation,
        placement_decision_id: decision.decision_id.clone(),
        issued_at: now,
        desired,
    };
    connection.execute(
        "INSERT INTO fleet_assignments(worker_id,workload_name,generation,payload_json) VALUES (?,?,?,?)
         ON CONFLICT(worker_id,workload_name) DO UPDATE SET generation=excluded.generation,payload_json=excluded.payload_json",
        params![worker.as_str(), intent.workload_name.as_str(), generation, serde_json::to_string(&assignment)?],
    )?;
    connection.execute(
        "UPDATE fleet_worker_revisions SET revision=revision+1 WHERE worker_id=?",
        params![worker.as_str()],
    )?;
    Ok(assignment)
}

fn advance_movement(
    connection: &Connection,
    observation: &ObservedState,
    now: i64,
) -> Result<(), FleetError> {
    let movement: Option<(u64, String, String, String)> = connection.query_row(
        "SELECT intent_generation,target_worker_id,source_workers_json,phase FROM fleet_movements WHERE workload_name=?",
        params![observation.workload_name.as_str()], |row| Ok((row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?)),
    ).optional()?;
    let Some((intent_generation, target, sources, phase)) = movement else {
        return Ok(());
    };
    let sources: Vec<WorkerId> = serde_json::from_str(&sources)?;
    let target_assignment: Option<WorkerAssignment> = connection
        .query_row(
            "SELECT payload_json FROM fleet_assignments WHERE worker_id=? AND workload_name=?",
            params![target, observation.workload_name.as_str()],
            |row| row.get::<_, String>(0),
        )
        .optional()?
        .map(|value| serde_json::from_str(&value))
        .transpose()?;
    let target_release_matches = target_assignment.is_some_and(|assignment| {
        matches!(
            assignment.desired,
            WorkerAssignmentAction::EnsurePresent { release_digest, .. }
                if observation.release_digest.as_ref() == Some(&release_digest)
        )
    });
    if phase == "awaitingTarget"
        && observation.worker_id.as_str() == target
        && observation.phase == ObservedPhase::Healthy
        && target_release_matches
    {
        let intent: WorkloadIntent = connection
            .query_row(
                "SELECT payload_json FROM fleet_intents WHERE workload_name=?",
                params![observation.workload_name.as_str()],
                |row| row.get::<_, String>(0),
            )
            .and_then(|value| {
                serde_json::from_str(&value)
                    .map_err(|error| rusqlite::Error::ToSqlConversionFailure(Box::new(error)))
            })?;
        let decision: PlacementDecision = connection.query_row(
            "SELECT payload_json FROM fleet_decisions WHERE workload_name=? AND intent_generation=?",
            params![observation.workload_name.as_str(), intent_generation], |row| row.get::<_, String>(0),
        ).and_then(|value| serde_json::from_str(&value).map_err(|error| rusqlite::Error::ToSqlConversionFailure(Box::new(error))))?;
        for source in &sources {
            issue_assignment(
                connection,
                source,
                &intent,
                &decision,
                WorkerAssignmentAction::EnsureAbsent,
                now,
            )?;
        }
        connection.execute(
            "UPDATE fleet_movements SET phase='awaitingSource' WHERE workload_name=?",
            params![observation.workload_name.as_str()],
        )?;
    } else if phase == "awaitingSource"
        && observation.phase == ObservedPhase::Absent
        && sources.contains(&observation.worker_id)
    {
        let mut all_absent = true;
        for source in &sources {
            let current: Option<String> = connection.query_row(
                "SELECT o.payload_json FROM fleet_observations o JOIN fleet_assignments a
                 ON a.worker_id=o.worker_id AND a.workload_name=o.workload_name AND a.generation=o.assignment_generation
                 WHERE o.worker_id=? AND o.workload_name=?",
                params![source.as_str(), observation.workload_name.as_str()], |row| row.get(0),
            ).optional()?;
            let absent = current
                .as_deref()
                .map(serde_json::from_str::<ObservedState>)
                .transpose()?
                .is_some_and(|state| state.phase == ObservedPhase::Absent);
            all_absent &= absent;
        }
        if all_absent {
            connection.execute(
                "UPDATE fleet_movements SET phase='complete' WHERE workload_name=?",
                params![observation.workload_name.as_str()],
            )?;
        }
    }
    Ok(())
}

fn load_json_rows<T: serde::de::DeserializeOwned>(
    connection: &Connection,
    sql: &str,
    value: &str,
) -> Result<Vec<T>, FleetError> {
    let mut statement = connection.prepare(sql)?;
    statement
        .query_map(params![value], |row| row.get::<_, String>(0))?
        .map(|row| Ok(serde_json::from_str(&row?)?))
        .collect()
}

fn validate_labels(
    capabilities: &BTreeSet<String>,
    topology: &BTreeMap<String, String>,
) -> Result<(), FleetError> {
    let valid = |value: &str| {
        !value.is_empty()
            && value.len() <= 128
            && value
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || b"._:/-".contains(&byte))
    };
    if capabilities.iter().any(|value| !valid(value))
        || topology
            .iter()
            .any(|(key, value)| !valid(key) || !valid(value))
    {
        return Err(FleetError::Invalid(
            "capability or topology label is invalid".into(),
        ));
    }
    Ok(())
}

fn bearer(authorization: &str) -> Result<&str, FleetError> {
    authorization
        .split_once(' ')
        .filter(|(scheme, token)| scheme.eq_ignore_ascii_case("Bearer") && !token.is_empty())
        .map(|(_, token)| token)
        .ok_or(FleetError::Unauthorized)
}

fn random_credential() -> Result<String, FleetError> {
    let mut bytes = [0_u8; 48];
    fill(&mut bytes).map_err(|_| FleetError::Database("randomness source failed".into()))?;
    Ok(URL_SAFE_NO_PAD.encode(bytes))
}

fn hash_worker_credential(credential: &str) -> Result<(Vec<u8>, Vec<u8>), FleetError> {
    let mut salt = [0_u8; 16];
    fill(&mut salt).map_err(|_| FleetError::Database("randomness source failed".into()))?;
    let hash = derive_worker_hash(credential, &salt, 32)?;
    Ok((salt.to_vec(), hash))
}

fn derive_worker_hash(credential: &str, salt: &[u8], length: usize) -> Result<Vec<u8>, FleetError> {
    let mut domain_salt = Sha256::new();
    domain_salt.update(WORKER_SECRET_DOMAIN);
    domain_salt.update(salt);
    let domain_salt = domain_salt.finalize();
    let parameters = ScryptParams::new(14, 8, 1, length)
        .map_err(|error| FleetError::Database(error.to_string()))?;
    let mut output = vec![0_u8; length];
    scrypt(
        credential.as_bytes(),
        &domain_salt,
        &parameters,
        &mut output,
    )
    .map_err(|error| FleetError::Database(error.to_string()))?;
    Ok(output)
}

#[cfg(test)]
mod tests {
    use super::*;
    use arcturus_contracts::{
        PlacementRequirements, ReleaseCharacteristics, ReleaseMetadata, Sha256Digest,
        TransitionPolicy,
    };
    use tempfile::TempDir;

    fn worker(value: &str) -> WorkerId {
        WorkerId::try_from(value.to_owned()).unwrap()
    }
    fn service(value: &str) -> ServiceName {
        ServiceName::try_from(value.to_owned()).unwrap()
    }
    fn digest() -> Sha256Digest {
        Sha256Digest::try_from(format!("sha256:{}", "a".repeat(64))).unwrap()
    }

    fn heartbeat(id: &str, architecture: &str, sequence: u64) -> WorkerHeartbeat {
        WorkerHeartbeat {
            worker_id: worker(id),
            agent_instance_id: "instance".into(),
            inventory: arcturus_contracts::WorkerInventory {
                inventory_generation: 1,
                architecture: architecture.into(),
                operating_system: "linux".into(),
                logical_cpu_count: 4,
                total_memory_bytes: 8_000_000_000,
                storage_pools: vec![],
            },
            pressure: arcturus_contracts::WorkerPressure {
                pressure_sequence: sequence,
                observed_at: 100,
                available_memory_bytes: 4_000_000_000,
                cpu_utilization_basis_points: Some(100),
                storage_free_bytes: BTreeMap::new(),
                thermal_state: None,
            },
            accepted_assignment_set_revision: 0,
            accepted_assignments: BTreeMap::new(),
        }
    }

    fn intent(name: &str, generation: u64, worker_id: Option<&str>) -> WorkloadIntent {
        let name = service(name);
        WorkloadIntent {
            api_version: FLEET_API_VERSION.into(),
            kind: "WorkloadIntent".into(),
            workload_name: name.clone(),
            intent_generation: generation,
            release: arcturus_contracts::ServiceReleaseEnvelope {
                api_version: arcturus_contracts::SERVICE_RELEASE_API_VERSION.into(),
                kind: arcturus_contracts::SERVICE_RELEASE_KIND.into(),
                metadata: ReleaseMetadata {
                    name: name.clone(),
                    revision: arcturus_contracts::Revision::try_from("1".repeat(40)).unwrap(),
                    deployment_id: None,
                },
                spec: serde_json::json!({"components": {}}),
            },
            release_digest: digest(),
            release_characteristics: ReleaseCharacteristics {
                service_name: name,
                release_digest: digest(),
                component_modes: BTreeSet::from(["service".into()]),
                read_only_local_volumes: 0,
                writable_local_volumes: 0,
                has_legacy_migration: false,
                secret_uses: vec![],
            },
            placement: PlacementRequirements {
                worker_id: worker_id.map(worker),
                architecture: if worker_id.is_none() {
                    Some("amd64".into())
                } else {
                    None
                },
                minimum_total_memory_bytes: None,
                minimum_available_memory_bytes: None,
                required_capabilities: BTreeSet::new(),
                required_topology: BTreeMap::new(),
            },
            transition: TransitionPolicy {
                movability: Movability::Movable,
                overlap: OverlapSafety::Safe,
                authoritative_state: AuthoritativeState::NoneOrExternal,
                migration: RequirementState::NotRequired,
                fencing: RequirementState::NotRequired,
                attestation: None,
            },
            resource_bindings: vec![],
        }
    }

    fn setup() -> (TempDir, FleetStore, BTreeMap<String, String>) {
        let temp = TempDir::new().unwrap();
        let store = FleetStore::open(temp.path().join("fleet.sqlite3")).unwrap();
        let mut credentials = BTreeMap::new();
        for (id, arch) in [
            ("worker-a", "amd64"),
            ("worker-b", "arm64"),
            ("worker-c", "amd64"),
        ] {
            let enrolled = store
                .enroll(
                    WorkerRegistrationRequest {
                        worker_id: worker(id),
                        display_name: None,
                        attestations: arcturus_contracts::WorkerAttestations {
                            capabilities: BTreeSet::new(),
                            topology: BTreeMap::new(),
                        },
                    },
                    90,
                )
                .unwrap();
            credentials.insert(id.into(), enrolled.credential);
            store.heartbeat(&heartbeat(id, arch, 1), 100).unwrap();
        }
        (temp, store, credentials)
    }

    #[test]
    fn registers_more_than_two_workers_and_assigns_multiple_workloads() {
        let (_temp, store, _) = setup();
        assert_eq!(store.list_workers().unwrap().len(), 3);
        store
            .apply_intent(&intent("alpha", 1, Some("worker-a")), 101)
            .unwrap();
        store
            .apply_intent(&intent("beta", 1, Some("worker-a")), 101)
            .unwrap();
        let set = store.assignment_set(&worker("worker-a"), 0).unwrap();
        assert_eq!(set.assignments.len(), 2);
        assert_eq!(set.assignments[0].workload_name, service("alpha"));
        assert_eq!(set.assignments[1].workload_name, service("beta"));
    }

    #[test]
    fn unchanged_snapshot_never_implies_removal() {
        let (_temp, store, _) = setup();
        store
            .apply_intent(&intent("alpha", 1, Some("worker-a")), 101)
            .unwrap();
        let first = store.assignment_set(&worker("worker-a"), 0).unwrap();
        let unchanged = store
            .assignment_set(&worker("worker-a"), first.revision)
            .unwrap();
        assert!(!unchanged.changed);
        assert!(unchanged.assignments.is_empty());
        let replay = store.assignment_set(&worker("worker-a"), 0).unwrap();
        assert!(matches!(
            replay.assignments[0].desired,
            WorkerAssignmentAction::EnsurePresent { .. }
        ));
    }

    #[test]
    fn move_waits_for_target_health_before_source_tombstone() {
        let (_temp, store, _) = setup();
        store
            .apply_intent(&intent("alpha", 1, Some("worker-a")), 101)
            .unwrap();
        let moved = store
            .move_workload(
                &service("alpha"),
                MoveRequest {
                    expected_intent_generation: 1,
                    worker_id: Some(worker("worker-b")),
                    architecture: None,
                },
                102,
            )
            .unwrap();
        assert_eq!(moved.movement_phase.as_deref(), Some("awaitingTarget"));
        assert!(matches!(
            store
                .assignment_set(&worker("worker-a"), 0)
                .unwrap()
                .assignments[0]
                .desired,
            WorkerAssignmentAction::EnsurePresent { .. }
        ));
        let target = store
            .assignment_set(&worker("worker-b"), 0)
            .unwrap()
            .assignments[0]
            .clone();
        store
            .observe(
                &ObservedState {
                    worker_id: worker("worker-b"),
                    workload_name: service("alpha"),
                    assignment_generation: target.assignment_generation,
                    observation_sequence: 1,
                    observed_at: 103,
                    phase: ObservedPhase::Failed,
                    release_digest: None,
                    deployment_id: None,
                    units: BTreeMap::new(),
                    routing_status: None,
                    error: Some(arcturus_contracts::ObservedError {
                        code: "target_failed".into(),
                        message: "simulated".into(),
                    }),
                },
                103,
            )
            .unwrap();
        assert!(matches!(
            store
                .assignment_set(&worker("worker-a"), 0)
                .unwrap()
                .assignments[0]
                .desired,
            WorkerAssignmentAction::EnsurePresent { .. }
        ));
        store
            .observe(
                &ObservedState {
                    worker_id: worker("worker-b"),
                    workload_name: service("alpha"),
                    assignment_generation: target.assignment_generation,
                    observation_sequence: 2,
                    observed_at: 104,
                    phase: ObservedPhase::Healthy,
                    release_digest: Some(
                        Sha256Digest::try_from(format!("sha256:{}", "b".repeat(64))).unwrap(),
                    ),
                    deployment_id: Some("wrong-deployment".into()),
                    units: BTreeMap::new(),
                    routing_status: Some("published".into()),
                    error: None,
                },
                104,
            )
            .unwrap();
        assert!(matches!(
            store
                .assignment_set(&worker("worker-a"), 0)
                .unwrap()
                .assignments[0]
                .desired,
            WorkerAssignmentAction::EnsurePresent { .. }
        ));
        store
            .observe(
                &ObservedState {
                    worker_id: worker("worker-b"),
                    workload_name: service("alpha"),
                    assignment_generation: target.assignment_generation,
                    observation_sequence: 3,
                    observed_at: 105,
                    phase: ObservedPhase::Healthy,
                    release_digest: Some(digest()),
                    deployment_id: Some("deployment".into()),
                    units: BTreeMap::new(),
                    routing_status: Some("published".into()),
                    error: None,
                },
                105,
            )
            .unwrap();
        let source = store
            .assignment_set(&worker("worker-a"), 0)
            .unwrap()
            .assignments[0]
            .clone();
        assert!(matches!(
            source.desired,
            WorkerAssignmentAction::EnsureAbsent
        ));
        store
            .observe(
                &ObservedState {
                    worker_id: worker("worker-a"),
                    workload_name: service("alpha"),
                    assignment_generation: source.assignment_generation,
                    observation_sequence: 1,
                    observed_at: 106,
                    phase: ObservedPhase::Absent,
                    release_digest: None,
                    deployment_id: None,
                    units: BTreeMap::new(),
                    routing_status: Some("withdrawn".into()),
                    error: None,
                },
                106,
            )
            .unwrap();
        let completed = store.workload(service("alpha")).unwrap();
        assert_eq!(completed.movement_phase.as_deref(), Some("complete"));
        assert!(matches!(
            store
                .assignment_set(&worker("worker-a"), 0)
                .unwrap()
                .assignments[0]
                .desired,
            WorkerAssignmentAction::EnsureAbsent
        ));
    }

    #[test]
    fn unsafe_or_stateful_moves_fail_closed() {
        for case in [
            "fixed",
            "overlap",
            "state",
            "migration",
            "fencing",
            "unknown",
        ] {
            let (_temp, store, _) = setup();
            store
                .apply_intent(&intent("alpha", 1, Some("worker-a")), 101)
                .unwrap();
            let mut moved = intent("alpha", 2, Some("worker-b"));
            match case {
                "fixed" => moved.transition.movability = Movability::Fixed,
                "overlap" => moved.transition.overlap = OverlapSafety::Unsafe,
                "state" => moved.transition.authoritative_state = AuthoritativeState::NodeLocal,
                "migration" => moved.transition.migration = RequirementState::Required,
                "fencing" => moved.transition.fencing = RequirementState::Required,
                "unknown" => {
                    moved.transition.movability = Movability::Unknown;
                    moved.transition.overlap = OverlapSafety::Unknown;
                    moved.transition.authoritative_state = AuthoritativeState::Unknown;
                }
                _ => unreachable!(),
            }
            let view = store.apply_intent(&moved, 102).unwrap();
            assert_eq!(view.decision.status, "refused", "case {case}");
            assert!(view.decision.selected_worker_id.is_none(), "case {case}");
            assert!(matches!(
                store
                    .assignment_set(&worker("worker-a"), 0)
                    .unwrap()
                    .assignments[0]
                    .desired,
                WorkerAssignmentAction::EnsurePresent { .. }
            ));
            assert!(
                store
                    .assignment_set(&worker("worker-b"), 0)
                    .unwrap()
                    .assignments
                    .is_empty()
            );
        }
    }

    #[test]
    fn hard_refusal_reasons_are_recorded_for_operator_explanation() {
        let (_temp, store, _) = setup();
        let mut impossible = intent("alpha", 1, None);
        impossible.placement.architecture = Some("armv7".into());
        impossible.placement.minimum_total_memory_bytes = Some(9_000_000_000);
        impossible.placement.minimum_available_memory_bytes = Some(5_000_000_000);
        impossible
            .placement
            .required_capabilities
            .insert("durable-storage".into());
        impossible
            .placement
            .required_topology
            .insert("site".into(), "edge-a".into());
        let view = store.apply_intent(&impossible, 101).unwrap();
        assert_eq!(view.decision.status, "refused");
        for candidate in &view.decision.candidates {
            for reason in [
                "architecture_mismatch",
                "total_memory_insufficient",
                "available_memory_insufficient",
                "capability_missing",
                "topology_mismatch",
            ] {
                assert!(
                    candidate.refusal_codes.iter().any(|value| value == reason),
                    "{} lacks {reason}",
                    candidate.worker_id
                );
            }
        }
    }

    #[test]
    fn pressure_does_not_change_an_existing_assignment() {
        let (_temp, store, _) = setup();
        store
            .apply_intent(&intent("alpha", 1, Some("worker-a")), 101)
            .unwrap();
        let revision = store
            .assignment_set(&worker("worker-a"), 0)
            .unwrap()
            .revision;
        let mut pressure = heartbeat("worker-a", "amd64", 2);
        pressure.pressure.available_memory_bytes = 1;
        store.heartbeat(&pressure, 102).unwrap();
        assert!(
            !store
                .assignment_set(&worker("worker-a"), revision)
                .unwrap()
                .changed
        );

        let mut update = intent("alpha", 2, Some("worker-a"));
        update.placement.minimum_available_memory_bytes = Some(2);
        store.apply_intent(&update, 103).unwrap();
        let updated = store.assignment_set(&worker("worker-a"), revision).unwrap();
        assert!(updated.changed);
        assert_eq!(updated.assignments[0].intent_generation, 2);
    }
}
