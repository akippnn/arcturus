from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
NETWORK_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
IMAGE_RE = re.compile(
    r"^(?P<repository>[a-z0-9][a-z0-9._-]*(?::[0-9]+)?(?:/[a-z0-9._-]+)+)"
    r"@(?P<digest>sha256:[0-9a-f]{64})$"
)
SECRET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,126}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SENSITIVE_RE = re.compile(
    r"(?i)(authorization|bearer|token|password|passwd|secret|api[_-]?key|registry[_-]?auth)"
)


def _validate_name(value: str, label: str = "name") -> str:
    if not NAME_RE.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase DNS-style name")
    return value


def redact(value: Any, key: str = "") -> Any:
    """Return a JSON-compatible value with secret-like fields removed."""
    if SENSITIVE_RE.search(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item, key) for item in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)(authorization:\s*bearer\s+)\S+", r"\1<redacted>", value)
        value = re.sub(r"(?i)(--password(?:=|\s+))\S+", r"\1<redacted>", value)
        value = re.sub(
            r"(?i)\b(token|password|passwd|secret|api[_-]?key|registry[_-]?auth)(\s*[=:]\s*)\S+",
            r"\1\2<redacted>",
            value,
        )
    return value


def audit(event: str, **fields: Any) -> None:
    payload = {"event": event, "timestamp": int(time.time()), **fields}
    print(json.dumps(redact(payload), sort_keys=True, separators=(",", ":")), flush=True)


class SecretRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    target: str | None = None
    type: Literal["file", "env"] = "file"

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not SECRET_NAME_RE.fullmatch(value):
            raise ValueError("secret name contains invalid characters")
        return value

    @model_validator(mode="after")
    def validate_target(self) -> "SecretRef":
        if self.type == "env" and (not self.target or not ENV_NAME_RE.fullmatch(self.target)):
            raise ValueError("environment secrets require a valid target variable")
        if self.target and ("\n" in self.target or "\x00" in self.target):
            raise ValueError("secret target contains invalid characters")
        return self


class PortRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    container: int = Field(ge=1, le=65535)
    host: int | None = Field(default=None, ge=1, le=65535)
    hostIp: str | None = None
    protocol: Literal["tcp", "udp"] = "tcp"


class VolumeRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    target: str
    type: Literal["bind", "volume"] = "bind"
    readOnly: bool = False
    external: bool = True
    selinuxRelabel: Literal["private", "shared"] | None = None

    @model_validator(mode="after")
    def validate_paths(self) -> "VolumeRef":
        if not self.target.startswith("/") or "\n" in self.target:
            raise ValueError("volume target must be an absolute container path")
        if self.type == "bind" and not self.source.startswith("/"):
            raise ValueError("bind volume source must be absolute")
        if self.type == "volume" and not NETWORK_RE.fullmatch(self.source):
            raise ValueError("named volume source must be a lowercase Podman name")
        return self


class HealthCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str
    interval: str = "10s"
    timeout: str = "5s"
    retries: int = Field(default=5, ge=1, le=30)
    startPeriod: str = "10s"

    @field_validator("command")
    @classmethod
    def validate_command(cls, value: str) -> str:
        if not value.strip() or "\n" in value or "\x00" in value:
            raise ValueError("health command must be a non-empty single line")
        return value


class Schedule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    onCalendar: str
    persistent: bool = True
    randomizedDelaySeconds: int = Field(default=0, ge=0, le=86400)
    runOnDeploy: bool = False

    @field_validator("onCalendar")
    @classmethod
    def validate_calendar(cls, value: str) -> str:
        if not value.strip() or "\n" in value or "\x00" in value:
            raise ValueError("schedule onCalendar must be a non-empty single line")
        return value


class Component(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image: str
    containerName: str | None = None
    mode: Literal["service", "oneshot", "scheduled"] = "service"
    command: list[str] = Field(default_factory=list)
    environment: dict[str, str] = Field(default_factory=dict)
    secrets: list[SecretRef] = Field(default_factory=list)
    ports: list[PortRef] = Field(default_factory=list)
    volumes: list[VolumeRef] = Field(default_factory=list)
    dependsOn: list[str] = Field(default_factory=list)
    networks: list[str] = Field(default_factory=lambda: ["internal_routing"])
    healthCheck: HealthCheck | None = None
    schedule: Schedule | None = None
    restart: Literal["always", "on-failure", "no"] = "always"

    @field_validator("image")
    @classmethod
    def validate_image(cls, value: str) -> str:
        if not IMAGE_RE.fullmatch(value):
            raise ValueError("image must be fully qualified and pinned to sha256 digest")
        return value

    @field_validator("containerName")
    @classmethod
    def validate_container_name(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_name(value, "containerName")
        return value

    @field_validator("environment")
    @classmethod
    def validate_environment(cls, value: dict[str, str]) -> dict[str, str]:
        for key, item in value.items():
            if not ENV_NAME_RE.fullmatch(key):
                raise ValueError(f"invalid environment variable: {key}")
            if SENSITIVE_RE.search(key):
                raise ValueError(f"secret-like environment variable {key} must use secrets")
            if "\n" in item or "\x00" in item:
                raise ValueError(f"environment variable {key} contains invalid characters")
        return value

    @model_validator(mode="after")
    def validate_mode(self) -> "Component":
        if self.mode in {"oneshot", "scheduled"} and self.healthCheck is not None:
            raise ValueError(f"{self.mode} components cannot declare health checks")
        if self.mode == "scheduled" and self.schedule is None:
            raise ValueError("scheduled components require schedule")
        if self.mode != "scheduled" and self.schedule is not None:
            raise ValueError("schedule is only valid for scheduled components")
        if self.mode in {"oneshot", "scheduled"} and self.restart != "no":
            self.restart = "no"
        return self


class NetworkRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    external: bool = True

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not NETWORK_RE.fullmatch(value):
            raise ValueError("network name contains invalid characters")
        return value


class RoutingService(BaseModel):
    model_config = ConfigDict(extra="forbid")

    component: str
    port: int = Field(ge=1, le=65535)
    protocol: Literal["http", "https", "tcp", "udp"] = "http"
    domains: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    websocket: bool = False
    maxBodySize: str = "1G"

    @field_validator("domains")
    @classmethod
    def validate_domains(cls, values: list[str]) -> list[str]:
        for domain in values:
            labels = domain.split(".")
            if (
                len(domain) > 253
                or domain.endswith(".")
                or any(
                    not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                    for label in labels
                )
            ):
                raise ValueError(f"invalid route domain: {domain}")
        return values

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, values: list[str]) -> list[str]:
        for alias in values:
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", alias):
                raise ValueError(f"invalid route alias: {alias}")
        return values


class DeploymentPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeoutSeconds: int = Field(default=300, ge=10, le=1800)
    rollbackOnFailure: bool = True


class ReleaseSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    components: dict[str, Component]
    networks: list[NetworkRef] = Field(
        default_factory=lambda: [NetworkRef(name="internal_routing", external=True)]
    )
    routing: dict[str, RoutingService] = Field(default_factory=dict)
    deployment: DeploymentPolicy = Field(default_factory=DeploymentPolicy)

    @model_validator(mode="after")
    def validate_references(self) -> "ReleaseSpec":
        names = set(self.components)
        networks = {network.name for network in self.networks}
        for name, component in self.components.items():
            _validate_name(name, "component name")
            missing = set(component.dependsOn) - names
            if missing:
                raise ValueError(f"component {name} has missing dependencies: {sorted(missing)}")
            scheduled_dependencies = [
                dependency
                for dependency in component.dependsOn
                if self.components[dependency].mode == "scheduled"
            ]
            if scheduled_dependencies:
                raise ValueError(
                    f"component {name} cannot depend on scheduled components: {scheduled_dependencies}"
                )
            missing_networks = set(component.networks) - networks
            if missing_networks:
                raise ValueError(f"component {name} has missing networks: {sorted(missing_networks)}")
        for name, route in self.routing.items():
            _validate_name(name, "route name")
            if route.component not in names:
                raise ValueError(f"route {name} references missing component {route.component}")
            if self.components[route.component].mode != "service":
                raise ValueError(f"route {name} must reference a long-running service component")
        self._check_cycles()
        return self

    def _check_cycles(self) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visiting:
                raise ValueError(f"dependency cycle includes {name}")
            if name in visited:
                return
            visiting.add(name)
            for dependency in self.components[name].dependsOn:
                visit(dependency)
            visiting.remove(name)
            visited.add(name)

        for name in self.components:
            visit(name)


class ReleaseMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    revision: str
    deploymentId: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _validate_name(value, "service name")

    @field_validator("revision")
    @classmethod
    def validate_revision(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{40}", value, re.IGNORECASE):
            raise ValueError("revision must be a 40-character Git SHA")
        return value.lower()

    @field_validator("deploymentId")
    @classmethod
    def validate_deployment_id(cls, value: str | None) -> str | None:
        if value is not None:
            uuid.UUID(value)
        return value


class ServiceRelease(BaseModel):
    model_config = ConfigDict(extra="forbid")

    apiVersion: Literal["arcturus.u128.org/v2"]
    kind: Literal["ServiceRelease"]
    metadata: ReleaseMetadata
    spec: ReleaseSpec

    def canonical_json(self) -> str:
        # Optional values are omitted so every consumer sees one portable
        # representation.  In particular, Zod's optional fields do not accept
        # an explicit JSON null unless they are also declared nullable.
        return json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            sort_keys=True,
            separators=(",", ":"),
        )

    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_json().encode()).hexdigest()


class DeploymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str
    commit_sha: str
    manifest: ServiceRelease

    @model_validator(mode="after")
    def validate_identity(self) -> "DeploymentRequest":
        _validate_name(self.service, "service")
        if not re.fullmatch(r"[0-9a-f]{40}", self.commit_sha, re.IGNORECASE):
            raise ValueError("commit_sha must be a 40-character Git SHA")
        self.commit_sha = self.commit_sha.lower()
        if self.manifest.metadata.name != self.service:
            raise ValueError("manifest service does not match request service")
        if self.manifest.metadata.revision != self.commit_sha:
            raise ValueError("manifest revision does not match commit_sha")
        return self


class CommandError(RuntimeError):
    def __init__(self, command: list[str], result: subprocess.CompletedProcess[str]):
        self.command = command
        self.returncode = result.returncode
        self.stdout = result.stdout
        self.stderr = result.stderr
        message = redact(result.stderr.strip() or result.stdout.strip() or "command failed")
        super().__init__(f"{command[0]} failed ({result.returncode}): {message}")


class CommandRunner:
    def run(
        self,
        command: list[str],
        *,
        timeout: int = 120,
        env: dict[str, str] | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        if check and result.returncode != 0:
            raise CommandError(command, result)
        return result


class PodmanClient:
    def __init__(self, socket_path: str | None = None):
        if socket_path is None:
            socket_path = f"/run/user/{os.getuid()}/arcturus/podman.sock"
        self.socket_path = socket_path

    def _client(self, timeout: int) -> httpx.Client:
        return httpx.Client(
            transport=httpx.HTTPTransport(uds=self.socket_path),
            base_url="http://localhost/v5.0.0/libpod",
            timeout=timeout,
        )

    def pull(self, image: str, timeout: int) -> None:
        with self._client(timeout) as client:
            response = client.post("/images/pull", params={"reference": image})
            if response.status_code >= 400:
                raise RuntimeError(
                    f"Podman image pull failed ({response.status_code}): {redact(response.text)}"
                )

    def inspect(self, image: str) -> dict[str, Any]:
        encoded = quote(image, safe="")
        with self._client(30) as client:
            response = client.get(f"/images/{encoded}/json")
            if response.status_code >= 400:
                raise RuntimeError(
                    f"Podman image inspect failed ({response.status_code}): {redact(response.text)}"
                )
            return response.json()


class DeploymentStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS deployments (
                    id TEXT PRIMARY KEY,
                    service TEXT NOT NULL,
                    commit_sha TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    previous_id TEXT,
                    health_json TEXT NOT NULL DEFAULT '{}',
                    rollback_json TEXT NOT NULL DEFAULT '{}',
                    error_json TEXT NOT NULL DEFAULT '{}',
                    requested_at INTEGER NOT NULL,
                    completed_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_deployments_service_requested
                    ON deployments(service, requested_at DESC);
                CREATE TABLE IF NOT EXISTS active_releases (
                    service TEXT PRIMARY KEY,
                    deployment_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS service_state (
                    service TEXT PRIMARY KEY,
                    desired_state TEXT NOT NULL DEFAULT 'enabled',
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY,
                    service TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source_deployment_id TEXT,
                    target_deployment_id TEXT,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error_json TEXT NOT NULL DEFAULT '{}',
                    requested_at INTEGER NOT NULL,
                    completed_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_operations_service_requested
                    ON operations(service, requested_at DESC);
                """
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def active(self, service: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT d.* FROM deployments d
                   JOIN active_releases a ON a.deployment_id=d.id
                   WHERE a.service=?""",
                (service,),
            ).fetchone()
        return self._row(row)

    def get(self, deployment_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM deployments WHERE id=?", (deployment_id,)
            ).fetchone()
        return self._row(row)

    def successful(self, service: str, deployment_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM deployments WHERE id=? AND service=? AND status='succeeded'",
                (deployment_id, service),
            ).fetchone()
        return self._row(row)

    def create(self, deployment_id: str, request: DeploymentRequest) -> dict[str, Any]:
        previous = self.active(request.service)
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO deployments
                   (id,service,commit_sha,manifest_digest,status,manifest_json,previous_id,requested_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    deployment_id,
                    request.service,
                    request.commit_sha,
                    request.manifest.digest(),
                    "requested",
                    request.manifest.canonical_json(),
                    previous["id"] if previous else None,
                    int(time.time()),
                ),
            )
        return self.get(deployment_id) or {}

    def activate(self, service: str, deployment_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO active_releases(service,deployment_id) VALUES (?,?)
                   ON CONFLICT(service) DO UPDATE SET deployment_id=excluded.deployment_id""",
                (service, deployment_id),
            )
        self.set_desired_state(service, "enabled")

    def clear_active(self, service: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM active_releases WHERE service=?", (service,))

    def set_desired_state(self, service: str, desired_state: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO service_state(service,desired_state,updated_at) VALUES (?,?,?)
                   ON CONFLICT(service) DO UPDATE SET
                     desired_state=excluded.desired_state,updated_at=excluded.updated_at""",
                (service, desired_state, int(time.time())),
            )

    def desired_state(self, service: str) -> str:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT desired_state FROM service_state WHERE service=?", (service,)
            ).fetchone()
        return row[0] if row else "enabled"

    def create_operation(
        self,
        service: str,
        action: str,
        *,
        source: str | None = None,
        target: str | None = None,
    ) -> str:
        operation_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO operations
                   (id,service,action,status,source_deployment_id,target_deployment_id,requested_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (operation_id, service, action, "running", source, target, int(time.time())),
            )
        return operation_id

    def finish_operation(
        self,
        operation_id: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute(
                """UPDATE operations SET status=?,result_json=?,error_json=?,completed_at=?
                   WHERE id=?""",
                (
                    status,
                    json.dumps(redact(result or {}), sort_keys=True),
                    json.dumps(redact(error or {}), sort_keys=True),
                    int(time.time()),
                    operation_id,
                ),
            )
        return self.get_operation(operation_id) or {}

    def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE id=?", (operation_id,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["result"] = json.loads(result.pop("result_json"))
        result["error"] = json.loads(result.pop("error_json"))
        return result

    def finish(
        self,
        deployment_id: str,
        status: str,
        *,
        health: dict[str, Any] | None = None,
        rollback: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        activate: bool = False,
    ) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute(
                """UPDATE deployments SET status=?,health_json=?,rollback_json=?,error_json=?,completed_at=?
                   WHERE id=?""",
                (
                    status,
                    json.dumps(redact(health or {}), sort_keys=True),
                    json.dumps(redact(rollback or {}), sort_keys=True),
                    json.dumps(redact(error or {}), sort_keys=True),
                    int(time.time()),
                    deployment_id,
                ),
            )
            if activate:
                service = connection.execute(
                    "SELECT service FROM deployments WHERE id=?", (deployment_id,)
                ).fetchone()[0]
                connection.execute(
                    """INSERT INTO active_releases(service,deployment_id) VALUES (?,?)
                       ON CONFLICT(service) DO UPDATE SET deployment_id=excluded.deployment_id""",
                    (service, deployment_id),
                )
                connection.execute(
                    """INSERT INTO service_state(service,desired_state,updated_at) VALUES (?,?,?)
                       ON CONFLICT(service) DO UPDATE SET
                         desired_state=excluded.desired_state,updated_at=excluded.updated_at""",
                    (service, "enabled", int(time.time())),
                )
        return self.get(deployment_id) or {}

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for key in ("manifest_json", "health_json", "rollback_json", "error_json"):
            result[key.removesuffix("_json")] = json.loads(result.pop(key))
        return result


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


class QuadletRenderer:
    def __init__(self, allowed_bind_roots: list[Path]):
        self.allowed_bind_roots = [root.resolve() for root in allowed_bind_roots]

    def render(self, manifest: ServiceRelease, destination: Path) -> list[str]:
        destination.mkdir(parents=True, exist_ok=True)
        service = manifest.metadata.name
        network_map = {network.name: network for network in manifest.spec.networks}
        generated_volumes: set[str] = set()

        for network in manifest.spec.networks:
            if not network.external:
                (destination / f"arcturus-{service}-{network.name}.network").write_text(
                    "[Network]\n"
                    f"NetworkName=arcturus-{service}-{network.name}\n"
                    f"Label=io.u128.arcturus.service={service}\n"
                )

        units: list[str] = []
        for component_name, component in manifest.spec.components.items():
            unit_base = f"arcturus-{service}-{component_name}"
            unit_name = f"{unit_base}.service"
            readiness_unit = unit_name
            dependencies = [f"arcturus-{service}-{name}.service" for name in component.dependsOn]
            lines = [
                "[Unit]",
                f"Description=Arcturus {service}/{component_name}",
                f"PartOf=arcturus-{service}.target",
            ]
            if dependencies:
                joined = " ".join(dependencies)
                lines.extend([f"Requires={joined}", f"After={joined}"])
            lines.extend(
                [
                    "",
                    "[Container]",
                    f"ContainerName={component.containerName or unit_base}",
                    f"Image={component.image}",
                    "Pull=never",
                    "LogDriver=journald",
                    f"Label=io.u128.arcturus.service={service}",
                    f"Label=io.u128.arcturus.component={component_name}",
                    f"Label=io.u128.arcturus.revision={manifest.metadata.revision}",
                ]
            )
            for network_name in component.networks:
                network = network_map[network_name]
                ref = network.name if network.external else f"arcturus-{service}-{network.name}.network"
                lines.append(f"Network={ref}")
            for key, value in sorted(component.environment.items()):
                lines.append(f"Environment={_quote(f'{key}={value}')}")
            for secret in component.secrets:
                options = [secret.name, f"type={secret.type}"]
                if secret.target:
                    options.append(f"target={secret.target}")
                lines.append(f"Secret={','.join(options)}")
            for port in component.ports:
                host = str(port.host) if port.host else ""
                prefix = f"{port.hostIp}:" if port.hostIp else ""
                lines.append(f"PublishPort={prefix}{host}:{port.container}/{port.protocol}")
            for volume in component.volumes:
                source = volume.source
                if volume.type == "bind":
                    self._validate_bind(source)
                elif not volume.external:
                    volume_unit = f"arcturus-{service}-{source}.volume"
                    if volume_unit not in generated_volumes:
                        generated_volumes.add(volume_unit)
                        (destination / volume_unit).write_text(
                            "[Volume]\n"
                            f"VolumeName=arcturus-{service}-{source}\n"
                            f"Label=io.u128.arcturus.service={service}\n"
                        )
                    source = volume_unit
                options: list[str] = []
                if volume.readOnly:
                    options.append("ro")
                if volume.selinuxRelabel == "private":
                    options.append("Z")
                elif volume.selinuxRelabel == "shared":
                    options.append("z")
                suffix = ":" + ",".join(options) if options else ""
                lines.append(f"Volume={source}:{volume.target}{suffix}")
            if component.command:
                lines.append("Exec=" + " ".join(shlex.quote(item) for item in component.command))
            if component.healthCheck:
                health = component.healthCheck
                lines.extend(
                    [
                        f"HealthCmd={health.command}",
                        f"HealthInterval={health.interval}",
                        f"HealthTimeout={health.timeout}",
                        f"HealthRetries={health.retries}",
                        f"HealthStartPeriod={health.startPeriod}",
                        "Notify=healthy",
                    ]
                )
            lines.extend(["", "[Service]"])
            if component.mode == "oneshot":
                lines.extend(["Type=oneshot", "RemainAfterExit=yes"])
            elif component.mode == "scheduled":
                lines.append("Type=oneshot")
            else:
                lines.append(f"Restart={component.restart}")
            lines.append(f"TimeoutStartSec={manifest.spec.deployment.timeoutSeconds}")
            (destination / f"{unit_base}.container").write_text("\n".join(lines) + "\n")

            if component.mode == "scheduled":
                schedule = component.schedule
                assert schedule is not None
                readiness_unit = f"{unit_base}.timer"
                timer_lines = [
                    "[Unit]",
                    f"Description=Arcturus schedule for {service}/{component_name}",
                    f"PartOf=arcturus-{service}.target",
                    "",
                    "[Timer]",
                    f"OnCalendar={schedule.onCalendar}",
                    f"Persistent={'true' if schedule.persistent else 'false'}",
                    f"Unit={unit_name}",
                ]
                if schedule.randomizedDelaySeconds:
                    timer_lines.append(
                        f"RandomizedDelaySec={schedule.randomizedDelaySeconds}"
                    )
                timer_lines.extend(["", "[Install]", "WantedBy=timers.target"])
                (destination / readiness_unit).write_text("\n".join(timer_lines) + "\n")
            units.append(readiness_unit)

        target = destination / f"arcturus-{service}.target"
        target.write_text(
            "[Unit]\n"
            f"Description=Arcturus service target for {service}\n"
            f"Wants={' '.join(units)}\n"
            f"After={' '.join(units)}\n"
            "\n[Install]\nWantedBy=default.target\n"
        )
        return units

    def _validate_bind(self, source: str) -> None:
        resolved = Path(source).resolve()
        if not any(root == resolved or root in resolved.parents for root in self.allowed_bind_roots):
            raise ValueError(f"bind source is outside allowed roots: {source}")


class DeploymentFailure(RuntimeError):
    def __init__(self, message: str, *, rollback: dict[str, Any]):
        super().__init__(message)
        self.rollback = rollback
        self.rollback_succeeded = rollback.get("status") in {"succeeded", "not_required"}


class ReleaseDeployer:
    def __init__(
        self,
        *,
        state_dir: Path,
        quadlet_dir: Path,
        systemd_dir: Path,
        active_manifest_dir: Path | None = None,
        route_status_file: Path | None = None,
        registry_socket: Path | None = None,
        allowed_bind_roots: list[Path],
        runner: CommandRunner | None = None,
        podman: PodmanClient | None = None,
        validate_generator: bool = True,
    ):
        self.state_dir = state_dir
        self.quadlet_dir = quadlet_dir
        self.systemd_dir = systemd_dir
        self.active_manifest_dir = active_manifest_dir or state_dir / "active-manifests"
        self.route_status_file = route_status_file
        self.registry_socket = registry_socket
        self.release_dir = state_dir / "releases"
        self.lock_dir = state_dir / "locks"
        self.store = DeploymentStore(state_dir / "state.sqlite3")
        self.renderer = QuadletRenderer(allowed_bind_roots)
        self.runner = runner or CommandRunner()
        self.podman = podman or PodmanClient(os.getenv("PODMAN_SOCKET"))
        self.validate_generator = validate_generator
        for directory in (
            self.release_dir,
            self.lock_dir,
            self.quadlet_dir,
            self.systemd_dir,
            self.active_manifest_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_environment(cls) -> "ReleaseDeployer":
        home = Path.home()
        roots = os.getenv(
            "ARCTURUS_ALLOWED_BIND_ROOTS",
            str(home / "stacks"),
        )
        return cls(
            state_dir=Path(os.getenv("ARCTURUS_STATE_DIR", home / ".local/share/arcturus-deployer")),
            quadlet_dir=Path(os.getenv("ARCTURUS_QUADLET_DIR", home / ".config/containers/systemd/arcturus")),
            systemd_dir=Path(os.getenv("ARCTURUS_SYSTEMD_DIR", home / ".config/systemd/user")),
            active_manifest_dir=Path(
                os.getenv(
                    "ARCTURUS_ACTIVE_MANIFEST_DIR",
                    home / ".local/share/arcturus-deployer/active-manifests",
                )
            ),
            route_status_file=(
                Path(value)
                if (value := os.getenv("ARCTURUS_ROUTER_STATUS_FILE"))
                else None
            ),
            registry_socket=(
                Path(value)
                if (value := os.getenv("ARCTURUS_REGISTRY_SOCKET"))
                else None
            ),
            allowed_bind_roots=[Path(item) for item in roots.split(",") if item],
            validate_generator=os.getenv("ARCTURUS_VALIDATE_QUADLET", "true").lower() == "true",
        )

    @contextmanager
    def lock(self, service: str):
        lock_path = self.lock_dir / f"{service}.lock"
        with lock_path.open("a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise FileExistsError(f"deployment already in progress for {service}") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def deploy(self, request: DeploymentRequest) -> dict[str, Any]:
        deployment_id = str(uuid.uuid4())
        with self.lock(request.service):
            record = self.store.create(deployment_id, request)
            previous = self.store.active(request.service)
            audit(
                "deployment.requested",
                deployment_id=deployment_id,
                service=request.service,
                commit_sha=request.commit_sha,
                manifest_digest=request.manifest.digest(),
            )
            release_path = self.release_dir / request.service / deployment_id
            quadlets = release_path / "quadlet"
            release_path.mkdir(parents=True, exist_ok=False)
            (release_path / "manifest.json").write_text(request.manifest.canonical_json() + "\n")
            try:
                images = self._pull_and_verify(request.manifest)
                units = self.renderer.render(request.manifest, quadlets)
                self._validate_quadlets(quadlets)
                self._activate(request.service, release_path, units, request.manifest.spec.deployment.timeoutSeconds)
                self._run_scheduled_on_deploy(request.manifest)
                self._publish_manifest(request.service, request.manifest, deployment_id)
                routing = self._wait_for_routing(
                    request.manifest,
                    request.manifest.spec.deployment.timeoutSeconds,
                    deployment_id,
                )
                health = {"status": "healthy", "units": units, "routing": routing}
                result = self.store.finish(
                    deployment_id,
                    "succeeded",
                    health=health,
                    activate=True,
                )
                audit(
                    "deployment.succeeded",
                    deployment_id=deployment_id,
                    service=request.service,
                    images=images,
                )
                return self._response_with_routing(result, images=images)
            except Exception as exc:
                rollback = (
                    self._rollback(request.service, previous)
                    if request.manifest.spec.deployment.rollbackOnFailure
                    else {"status": "disabled"}
                )
                error = {"code": "deployment_failed", "message": str(redact(str(exc)))}
                self.store.finish(
                    deployment_id,
                    "failed",
                    rollback=rollback,
                    error=error,
                )
                audit(
                    "deployment.failed",
                    deployment_id=deployment_id,
                    service=request.service,
                    error=error,
                    rollback=rollback,
                )
                raise DeploymentFailure(
                    error["message"],
                    rollback=rollback,
                ) from exc

    def get(self, deployment_id: str) -> dict[str, Any] | None:
        record = self.store.get(deployment_id)
        return self._response_with_routing(record) if record else None

    def active(self, service: str) -> dict[str, Any] | None:
        record = self.store.active(service)
        if not record:
            return None
        response = self._response_with_routing(record)
        response["desired_state"] = self.store.desired_state(service)
        return response

    def operation(self, operation_id: str) -> dict[str, Any] | None:
        record = self.store.get_operation(operation_id)
        return redact(record) if record else None

    def rollback(self, service: str, deployment_id: str | None = None) -> dict[str, Any]:
        with self.lock(service):
            current = self.store.active(service)
            if current is None:
                raise LookupError(f"active release not found for {service}")
            target_id = deployment_id or current.get("previous_id")
            if not target_id:
                raise LookupError(f"previous known-good release not found for {service}")
            target = self.store.successful(service, target_id)
            if target is None:
                raise LookupError(f"successful deployment {target_id} not found for {service}")
            operation_id = self.store.create_operation(
                service,
                "rollback",
                source=current["id"],
                target=target["id"],
            )
            try:
                result = self._activate_record(service, target)
                self.store.activate(service, target["id"])
                operation = self.store.finish_operation(
                    operation_id, "succeeded", result=result
                )
                audit("service.rollback.succeeded", service=service, operation_id=operation_id)
                return redact(operation)
            except Exception as exc:
                error = {"code": "rollback_failed", "message": str(redact(str(exc)))}
                self.store.finish_operation(operation_id, "failed", error=error)
                audit("service.rollback.failed", service=service, error=error)
                raise RuntimeError(error["message"]) from exc

    def disable(self, service: str) -> dict[str, Any]:
        with self.lock(service):
            current = self.store.active(service)
            if current is None:
                raise LookupError(f"active release not found for {service}")
            operation_id = self.store.create_operation(
                service, "disable", source=current["id"]
            )
            try:
                unit = f"arcturus-{service}.target"
                self.runner.run(["systemctl", "--user", "disable", "--now", unit], timeout=60)
                self._withdraw_manifest(service)
                routing = self._wait_for_withdrawal(
                    service,
                    current["manifest"].get("spec", {}).get("deployment", {}).get(
                        "timeoutSeconds", 300
                    ),
                )
                self.store.set_desired_state(service, "disabled")
                operation = redact(
                    self.store.finish_operation(
                        operation_id,
                        "succeeded",
                        result={"desired_state": "disabled", "routing": routing},
                    )
                )
                audit("service.disable.succeeded", service=service, operation_id=operation_id)
                return operation
            except Exception as exc:
                return self._fail_operation(operation_id, "disable_failed", exc)

    def enable(self, service: str) -> dict[str, Any]:
        with self.lock(service):
            current = self.store.active(service)
            if current is None:
                raise LookupError(f"active release not found for {service}")
            operation_id = self.store.create_operation(
                service, "enable", target=current["id"]
            )
            try:
                result = self._activate_record(service, current)
                self.store.set_desired_state(service, "enabled")
                operation = redact(self.store.finish_operation(operation_id, "succeeded", result=result))
                audit("service.enable.succeeded", service=service, operation_id=operation_id)
                return operation
            except Exception as exc:
                return self._fail_operation(operation_id, "enable_failed", exc)

    def remove(self, service: str) -> dict[str, Any]:
        with self.lock(service):
            current = self.store.active(service)
            if current is None:
                raise LookupError(f"active release not found for {service}")
            operation_id = self.store.create_operation(
                service, "remove", source=current["id"]
            )
            try:
                unit = f"arcturus-{service}.target"
                self.runner.run(
                    ["systemctl", "--user", "disable", "--now", unit],
                    timeout=60,
                    check=False,
                )
                (self.quadlet_dir / service).unlink(missing_ok=True)
                (self.systemd_dir / unit).unlink(missing_ok=True)
                for timer in self.systemd_dir.glob(f"arcturus-{service}-*.timer"):
                    timer.unlink(missing_ok=True)
                self._withdraw_manifest(service)
                routing = self._wait_for_withdrawal(
                    service,
                    current["manifest"].get("spec", {}).get("deployment", {}).get(
                        "timeoutSeconds", 300
                    ),
                )
                self.runner.run(["systemctl", "--user", "daemon-reload"], timeout=30)
                self.store.clear_active(service)
                self.store.set_desired_state(service, "removed")
                result = {
                    "desired_state": "removed",
                    "routing": routing,
                    "preserved": ["release archives", "audit metadata", "volumes", "secrets"],
                }
                operation = redact(self.store.finish_operation(operation_id, "succeeded", result=result))
                audit("service.remove.succeeded", service=service, operation_id=operation_id)
                return operation
            except Exception as exc:
                return self._fail_operation(operation_id, "remove_failed", exc)

    def _pull_and_verify(self, manifest: ServiceRelease) -> list[str]:
        images = sorted({component.image for component in manifest.spec.components.values()})
        timeout = manifest.spec.deployment.timeoutSeconds
        for image in images:
            self.podman.pull(image, timeout)
            info = self.podman.inspect(image)
            expected = IMAGE_RE.fullmatch(image).group("digest")  # type: ignore[union-attr]
            actual = info.get("Digest") or ""
            repo_digests = info.get("RepoDigests") or []
            if actual != expected and not any(item.endswith("@" + expected) for item in repo_digests):
                raise RuntimeError(f"pulled image digest mismatch for {image}")
        return images

    def _validate_quadlets(self, quadlets: Path) -> None:
        if not self.validate_generator:
            return
        generator = Path("/usr/lib/systemd/system-generators/podman-system-generator")
        if not generator.exists():
            raise RuntimeError("Podman Quadlet generator is not installed")
        env = os.environ.copy()
        env["QUADLET_UNIT_DIRS"] = str(quadlets)
        self.runner.run([str(generator), "--user", "--dryrun"], timeout=60, env=env)
        for timer in quadlets.glob("*.timer"):
            for line in timer.read_text().splitlines():
                if line.startswith("OnCalendar="):
                    self.runner.run(
                        ["systemd-analyze", "calendar", line.partition("=")[2]], timeout=30
                    )

    def _run_scheduled_on_deploy(self, manifest: ServiceRelease) -> None:
        timeout = manifest.spec.deployment.timeoutSeconds
        for name, component in manifest.spec.components.items():
            if component.mode == "scheduled" and component.schedule and component.schedule.runOnDeploy:
                self.runner.run(
                    ["systemctl", "--user", "start", f"arcturus-{manifest.metadata.name}-{name}.service"],
                    timeout=timeout,
                )

    def _publish_manifest(
        self,
        service: str,
        manifest: ServiceRelease,
        deployment_id: str,
    ) -> None:
        destination = self.active_manifest_dir / service
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / "arcturus.json"
        temporary = destination / f".arcturus.{uuid.uuid4().hex}.tmp"
        published = manifest.model_dump(mode="json", exclude_none=True)
        published["metadata"]["deploymentId"] = deployment_id
        temporary.write_text(json.dumps(published, sort_keys=True, separators=(",", ":")) + "\n")
        os.replace(temporary, target)

    def _request_registry_rescan(self) -> None:
        if self.registry_socket is None:
            return
        try:
            transport = httpx.HTTPTransport(uds=str(self.registry_socket))
            with httpx.Client(transport=transport, timeout=5) as client:
                response = client.post("http://arcturus-registry/rescan")
                response.raise_for_status()
        except Exception as exc:
            audit("routing.registry_rescan_failed", error=str(redact(str(exc))))

    def _routing_state(
        self,
        manifest: ServiceRelease,
        deployment_id: str | None = None,
    ) -> dict[str, Any]:
        required = bool(manifest.spec.routing)
        if not required:
            return {"required": False, "status": "not_applicable"}
        if self.route_status_file is None:
            return {
                "required": True,
                "status": "pending",
                "error": {"code": "routing_status_unconfigured"},
            }
        try:
            payload = json.loads(self.route_status_file.read_text())
        except FileNotFoundError:
            return {"required": True, "status": "pending"}
        except (OSError, json.JSONDecodeError) as exc:
            return {
                "required": True,
                "status": "failed",
                "error": {"code": "routing_status_invalid", "message": str(redact(str(exc)))},
            }
        record = payload.get("services", {}).get(manifest.metadata.name)
        if not isinstance(record, dict) or record.get("revision") != manifest.metadata.revision:
            return {"required": True, "status": "pending"}
        if deployment_id and record.get("deploymentId") not in {None, deployment_id}:
            return {"required": True, "status": "pending"}
        return redact({"required": True, **record})

    def _wait_for_routing(
        self,
        manifest: ServiceRelease,
        timeout: int,
        deployment_id: str | None = None,
    ) -> dict[str, Any]:
        state = self._routing_state(manifest, deployment_id)
        if not state["required"] or self.route_status_file is None:
            return state
        baseline_applied_at = state.get("appliedAt")
        self._request_registry_rescan()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self._routing_state(manifest, deployment_id)
            receipt_is_current = (
                not deployment_id
                or state.get("deploymentId") == deployment_id
                or (
                    "deploymentId" not in state
                    and state.get("appliedAt") != baseline_applied_at
                )
            )
            if state.get("status") == "published" and receipt_is_current:
                return state
            if state.get("status") == "failed" and receipt_is_current:
                raise RuntimeError(
                    f"router failed to publish {manifest.metadata.name}: "
                    f"{state.get('error', {}).get('message', 'unknown routing error')}"
                )
            time.sleep(0.25)
        raise TimeoutError(f"router publication timed out for {manifest.metadata.name}")

    def _wait_for_withdrawal(self, service: str, timeout: int) -> dict[str, Any]:
        if self.route_status_file is None:
            return {"required": False, "status": "not_applicable"}
        self._request_registry_rescan()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                payload = json.loads(self.route_status_file.read_text())
            except FileNotFoundError:
                return {"required": True, "status": "published", "withdrawn": True}
            except (OSError, json.JSONDecodeError):
                time.sleep(0.25)
                continue
            if service not in payload.get("services", {}):
                return {"required": True, "status": "published", "withdrawn": True}
            time.sleep(0.25)
        raise TimeoutError(f"router withdrawal timed out for {service}")

    def _withdraw_manifest(self, service: str) -> None:
        directory = self.active_manifest_dir / service
        (directory / "arcturus.json").unlink(missing_ok=True)
        try:
            directory.rmdir()
        except OSError:
            pass

    def _activate_record(self, service: str, record: dict[str, Any]) -> dict[str, Any]:
        release_path = self.release_dir / service / record["id"]
        quadlets = release_path / "quadlet"
        if not quadlets.exists():
            raise RuntimeError("release files are missing")
        timer_bases = {path.name.removesuffix(".timer") for path in quadlets.glob("*.timer")}
        units = [path.name for path in quadlets.glob("*.timer")]
        units.extend(
            path.name.removesuffix(".container") + ".service"
            for path in quadlets.glob("*.container")
            if path.name.removesuffix(".container") not in timer_bases
        )
        manifest = ServiceRelease.model_validate(record["manifest"])
        self._activate(
            service,
            release_path,
            sorted(units),
            manifest.spec.deployment.timeoutSeconds,
        )
        self._run_scheduled_on_deploy(manifest)
        self._publish_manifest(service, manifest, record["id"])
        routing = self._wait_for_routing(
            manifest,
            manifest.spec.deployment.timeoutSeconds,
            record["id"],
        )
        return {
            "deployment_id": record["id"],
            "desired_state": "enabled",
            "units": sorted(units),
            "routing": routing,
        }

    def _fail_operation(self, operation_id: str, code: str, exc: Exception) -> dict[str, Any]:
        error = {"code": code, "message": str(redact(str(exc)))}
        self.store.finish_operation(operation_id, "failed", error=error)
        audit("service.operation.failed", operation_id=operation_id, error=error)
        raise RuntimeError(error["message"]) from exc

    def _activate(self, service: str, release_path: Path, units: list[str], timeout: int) -> None:
        active_link = self.quadlet_dir / service
        temp_link = self.quadlet_dir / f".{service}.{uuid.uuid4().hex}.tmp"
        temp_link.symlink_to(release_path / "quadlet", target_is_directory=True)
        os.replace(temp_link, active_link)

        target_source = release_path / "quadlet" / f"arcturus-{service}.target"
        target_path = self.systemd_dir / f"arcturus-{service}.target"
        target_path.write_text(target_source.read_text())
        for existing_timer in self.systemd_dir.glob(f"arcturus-{service}-*.timer"):
            existing_timer.unlink(missing_ok=True)
        for timer_source in (release_path / "quadlet").glob("*.timer"):
            (self.systemd_dir / timer_source.name).write_text(timer_source.read_text())
        self.runner.run(["systemctl", "--user", "daemon-reload"], timeout=30)
        self.runner.run(
            ["systemctl", "--user", "enable", f"arcturus-{service}.target"], timeout=30
        )
        self.runner.run(
            ["systemctl", "--user", "restart", f"arcturus-{service}.target"], timeout=timeout
        )
        for unit in units:
            result = self.runner.run(
                ["systemctl", "--user", "is-active", unit], timeout=15, check=False
            )
            if result.returncode != 0 or result.stdout.strip() != "active":
                raise RuntimeError(f"unit did not become active: {unit}")

    def _rollback(self, service: str, previous: dict[str, Any] | None) -> dict[str, Any]:
        if previous is None:
            try:
                self.runner.run(
                    ["systemctl", "--user", "stop", f"arcturus-{service}.target"],
                    timeout=30,
                    check=False,
                )
                (self.quadlet_dir / service).unlink(missing_ok=True)
                (self.systemd_dir / f"arcturus-{service}.target").unlink(missing_ok=True)
                for timer in self.systemd_dir.glob(f"arcturus-{service}-*.timer"):
                    timer.unlink(missing_ok=True)
                self._withdraw_manifest(service)
                self._wait_for_withdrawal(service, 30)
                self.runner.run(["systemctl", "--user", "daemon-reload"], timeout=30)
                return {"status": "not_required"}
            except Exception as exc:
                return {"status": "failed", "message": str(redact(str(exc)))}
        try:
            self._activate_record(service, previous)
            return {"status": "succeeded", "deployment_id": previous["id"]}
        except Exception as exc:
            return {"status": "failed", "message": str(redact(str(exc)))}

    @staticmethod
    def _response(record: dict[str, Any] | None, images: list[str] | None = None) -> dict[str, Any]:
        if record is None:
            return {}
        result = {
            "deployment_id": record["id"],
            "service": record["service"],
            "commit_sha": record["commit_sha"],
            "manifest_digest": record["manifest_digest"],
            "status": record["status"],
            "previous_deployment_id": record.get("previous_id"),
            "health": record.get("health", {}),
            "rollback": record.get("rollback", {}),
            "error": record.get("error", {}),
            "requested_at": record["requested_at"],
            "completed_at": record.get("completed_at"),
        }
        if images is not None:
            result["images"] = images
        elif record.get("manifest"):
            components = record["manifest"].get("spec", {}).get("components", {})
            result["images"] = sorted(
                {component.get("image") for component in components.values() if component.get("image")}
            )
        return redact(result)

    def _response_with_routing(
        self,
        record: dict[str, Any] | None,
        images: list[str] | None = None,
    ) -> dict[str, Any]:
        response = self._response(record, images=images)
        if record and record.get("manifest"):
            try:
                manifest = ServiceRelease.model_validate(record["manifest"])
                response["routing"] = self._routing_state(manifest, record.get("id"))
            except Exception as exc:
                response["routing"] = {
                    "required": True,
                    "status": "failed",
                    "error": {"code": "routing_manifest_invalid", "message": str(redact(str(exc)))},
                }
        return redact(response)
