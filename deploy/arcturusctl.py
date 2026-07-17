#!/usr/bin/env python3
"""Project-neutral CLI for Arcturus release and lifecycle operations."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Literal

from release import DeploymentRequest, HealthCheck, QuadletRenderer, ServiceRelease
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


REPOSITORY_RE = re.compile(
    r"^[a-z0-9][a-z0-9._-]*(?::[0-9]+)?(?:/[a-z0-9._-]+)+$"
)
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class ProjectBuild(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    context: str = "."
    containerfile: str = "Containerfile"
    validationTargets: list[str] = Field(default_factory=list)
    releaseTarget: str | None = None
    components: list[str]
    componentRepositories: dict[str, str] = Field(default_factory=dict)

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        if not REPOSITORY_RE.fullmatch(value):
            raise ValueError("repository must be fully qualified and contain no tag")
        return value

    @field_validator("componentRepositories")
    @classmethod
    def validate_component_repositories(cls, value: dict[str, str]) -> dict[str, str]:
        for component, repository in value.items():
            if not NAME_RE.fullmatch(component):
                raise ValueError(f"invalid component repository key: {component}")
            if not REPOSITORY_RE.fullmatch(repository):
                raise ValueError(f"invalid component repository for {component}")
        return value

    @field_validator("context", "containerfile")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or any(char in value for char in "\n\t\x00"):
            raise ValueError("build paths must be safe repository-relative paths")
        return value

    @field_validator("validationTargets", "releaseTarget")
    @classmethod
    def validate_targets(cls, value):
        values = value if isinstance(value, list) else [value] if value else []
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", item) for item in values):
            raise ValueError("build targets contain invalid characters")
        return value


class ProjectTestIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["command", "container-target", "waived"] = "container-target"
    reason: str | None = None

    @model_validator(mode="after")
    def validate_waiver(self) -> "ProjectTestIntent":
        if self.mode == "waived" and not (self.reason and self.reason.strip()):
            raise ValueError("waived test intent requires a reason")
        return self


class ProjectCI(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["gitea", "github", "generic", "none"] = "github"
    apiUrl: str
    storage: Literal["isolated", "shared"] = "isolated"
    deployTokenSecret: str = "ARCTURUS_DEPLOY_TOKEN"
    testIntent: ProjectTestIntent = Field(default_factory=ProjectTestIntent)

    @field_validator("apiUrl")
    @classmethod
    def validate_api_url(cls, value: str) -> str:
        if not re.fullmatch(r"https?://[^\s/]+(?::[0-9]+)?(?:/[^\s]*)?", value):
            raise ValueError("apiUrl must be an http(s) URL without whitespace")
        return value.rstrip("/")

    @field_validator("deployTokenSecret")
    @classmethod
    def validate_deploy_secret(cls, value: str) -> str:
        if value in {"DEPLOY_WEBHOOK_SECRET", "DEPLOY_SECRET"}:
            raise ValueError("obsolete deployment secret name")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", value):
            raise ValueError("deployment secret name must be uppercase shell syntax")
        return value


class ProjectRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["external", "owned"] = "external"
    host: str
    origin: str | None = None
    userSecret: str = "REGISTRY_USER"
    tokenSecret: str = "REGISTRY_TOKEN"

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*(?::[0-9]+)?", value):
            raise ValueError("registry host is invalid")
        return value

    @field_validator("origin")
    @classmethod
    def validate_origin(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not re.fullmatch(r"https://[a-z0-9][a-z0-9.-]*(?::[0-9]+)?", value):
            raise ValueError("owned registry origin must be a lowercase HTTPS origin without a path")
        return value.rstrip("/")

    @field_validator("userSecret", "tokenSecret")
    @classmethod
    def validate_secret_name(cls, value: str) -> str:
        if value in {"REGISTRY_PASSWORD", "REGISTRY_AUTH"}:
            raise ValueError("obsolete registry secret name")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", value):
            raise ValueError("registry secret name must be uppercase shell syntax")
        return value

    @model_validator(mode="after")
    def validate_mode(self) -> "ProjectRegistry":
        if self.mode == "owned":
            if not self.origin:
                raise ValueError("owned registry mode requires origin")
            origin_host = self.origin.removeprefix("https://")
            if origin_host != self.host:
                raise ValueError("owned registry origin hostname must match registry host")
        return self


class ProjectCompatibility(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifestApis: list[Literal["arcturus.u128.org/v1", "arcturus.u128.org/v2"]] = Field(
        default_factory=lambda: ["arcturus.u128.org/v2"]
    )
    v1Mode: Literal["disabled", "routing-mirror"] = "disabled"
    v1Manifest: str = ".arcturus/compat-v1.json"

    @field_validator("manifestApis")
    @classmethod
    def validate_manifest_apis(cls, value: list[str]) -> list[str]:
        if not value or len(value) != len(set(value)):
            raise ValueError("manifestApis must be non-empty and contain no duplicates")
        if "arcturus.u128.org/v2" not in value:
            raise ValueError("manifest v2 must remain the authoritative deployment API")
        return value

    @field_validator("v1Manifest")
    @classmethod
    def validate_v1_manifest_path(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or any(char in value for char in "\n\t\x00"):
            raise ValueError("v1Manifest must be a safe repository-relative path")
        return value

    @model_validator(mode="after")
    def validate_v1_mode(self) -> "ProjectCompatibility":
        has_v1 = "arcturus.u128.org/v1" in self.manifestApis
        if self.v1Mode == "routing-mirror" and not has_v1:
            raise ValueError("routing-mirror mode requires manifest v1 in manifestApis")
        if has_v1 and self.v1Mode != "routing-mirror":
            raise ValueError("manifest v1 is supported only as a routing-mirror")
        return self


class ProjectVerification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    publicUrl: str | None = None
    publicMode: Literal["http-success", "cloudflare-challenge", "skip"] = "skip"
    requireRouting: bool = True

    @model_validator(mode="after")
    def validate_public_policy(self) -> "ProjectVerification":
        if self.publicMode != "skip" and not self.publicUrl:
            raise ValueError("public verification mode requires publicUrl")
        if self.publicUrl and not re.fullmatch(r"https?://[^\s]+", self.publicUrl):
            raise ValueError("publicUrl must be an http(s) URL")
        return self


class ProjectDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    apiVersion: Literal["arcturus.u128.org/project/v1"]
    service: str
    manifest: str = "arcturus.release.json"
    ci: ProjectCI
    registry: ProjectRegistry
    builds: dict[str, ProjectBuild]
    fixedComponents: list[str] = Field(default_factory=list)
    compatibility: ProjectCompatibility = Field(default_factory=ProjectCompatibility)
    verification: ProjectVerification = Field(default_factory=ProjectVerification)

    @field_validator("service")
    @classmethod
    def validate_service(cls, value: str) -> str:
        if not NAME_RE.fullmatch(value):
            raise ValueError("service must be a lowercase DNS-style name")
        return value

    @field_validator("manifest")
    @classmethod
    def validate_manifest_path(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("manifest must be a repository-relative path")
        return value

    @model_validator(mode="after")
    def validate_build_names(self) -> "ProjectDefinition":
        for name in self.builds:
            if not NAME_RE.fullmatch(name):
                raise ValueError(f"invalid build name: {name}")
        return self


def load_project(path: Path) -> tuple[ProjectDefinition, Path, ServiceRelease]:
    project = ProjectDefinition.model_validate_json(path.read_text())
    root = path.parent.parent if path.parent.name == ".arcturus" else path.parent
    manifest_path = root / project.manifest
    manifest = ServiceRelease.model_validate_json(manifest_path.read_text())
    if manifest.metadata.name != project.service:
        raise SystemExit("project service does not match release manifest")
    component_builds: dict[str, str] = {}
    for build_name, build in project.builds.items():
        if not build.components:
            raise SystemExit(f"build {build_name} must map at least one component")
        unknown_repository_mappings = set(build.componentRepositories) - set(build.components)
        if unknown_repository_mappings:
            raise SystemExit(
                f"build {build_name} has repository mappings for unknown components: {sorted(unknown_repository_mappings)}"
            )
        if project.registry.mode == "owned" and set(build.componentRepositories) != set(build.components):
            raise SystemExit(
                f"owned registry build {build_name} requires a componentRepositories entry for every component"
            )
        for component in build.components:
            if component in component_builds:
                raise SystemExit(
                    f"component {component} is mapped by both {component_builds[component]} and {build_name}"
                )
            component_builds[component] = build_name
            if component not in manifest.spec.components:
                raise SystemExit(f"build {build_name} maps unknown component {component}")
            repository = manifest.spec.components[component].image.split("@", 1)[0]
            expected_repository = build.componentRepositories.get(component, build.repository)
            if repository != expected_repository:
                raise SystemExit(
                    f"component {component} repository {repository} does not match build {build_name} expected repository {expected_repository}"
                )
            if project.registry.mode == "owned":
                owned_repository = f"{project.registry.host}/{project.service}/{component}"
                if repository != owned_repository:
                    raise SystemExit(
                        f"owned registry component {component} must use repository {owned_repository}"
                    )
    fixed = set(project.fixedComponents)
    unknown_fixed = fixed - set(manifest.spec.components)
    if unknown_fixed:
        raise SystemExit(f"fixed components do not exist: {sorted(unknown_fixed)}")
    overlap = fixed & set(component_builds)
    if overlap:
        raise SystemExit(f"components cannot be both built and fixed: {sorted(overlap)}")
    missing = set(manifest.spec.components) - fixed - set(component_builds)
    if missing:
        raise SystemExit(f"components have no image source: {sorted(missing)}")
    for component in fixed:
        if manifest.spec.components[component].image.endswith("sha256:" + "0" * 64):
            raise SystemExit(f"fixed component {component} still uses a placeholder digest")
    for route_name, route in manifest.spec.routing.items():
        if "internal_routing" not in manifest.spec.components[route.component].networks:
            raise SystemExit(
                f"route {route_name} component {route.component} must join internal_routing"
            )
    return project, root, manifest


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def write_secure_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.chmod(0o600)
    os.replace(temporary, path)


def load_token(args: argparse.Namespace) -> str:
    token_file = args.token_file or os.getenv("ARCTURUS_TOKEN_FILE")
    if token_file:
        return Path(token_file).read_text().strip()
    token = os.getenv("ARCTURUS_DEPLOY_TOKEN", "")
    if token:
        return token
    raise SystemExit("deployment token missing; set ARCTURUS_TOKEN_FILE or ARCTURUS_DEPLOY_TOKEN")


def api_request(
    args: argparse.Namespace,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    authenticated: bool = True,
) -> dict[str, Any]:
    base_url = (args.api_url or os.getenv("ARCTURUS_API_URL", "http://127.0.0.1:9090")).rstrip("/")
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if authenticated:
        headers["Authorization"] = f"Bearer {load_token(args)}"
    request = urllib.request.Request(
        base_url + path,
        data=data,
        method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        message = exc.read().decode(errors="replace")
        try:
            parsed = json.loads(message)
            detail = json.dumps(parsed, indent=2, sort_keys=True)
        except json.JSONDecodeError:
            parsed = None
            detail = message.strip() or "<empty response body>"
        hint = ""
        if exc.code == 401:
            hint = "\nThe API is reachable, but ARCTURUS_DEPLOY_TOKEN is missing or invalid."
        elif exc.code == 403:
            hint = "\nThe token is valid but is not scoped to the requested service."
        elif exc.code == 424:
            hint = (
                "\nThe API and token are valid, but required host resources are missing. "
                "Create the listed Podman secrets, external volumes, or external networks "
                "as the Arcturus host user before deploying."
            )
        elif (
            exc.code == 502
            and path == "/v1/deployments"
            and isinstance(parsed, dict)
            and parsed.get("status") == "failed"
        ):
            hint = (
                "\nThe deployment API authenticated the request, but activation failed and "
                "Arcturus attempted rollback. This is not an API-key failure. Inspect the "
                "error and rollback fields above, then check the generated service journals "
                "on the host."
            )
        elif exc.code in {500, 502, 503, 504}:
            hint = (
                "\nThe deployment API or one of its runtime dependencies failed. On the host, "
                "check `systemctl --user status 'arcturus-deployer@*'` and "
                "`journalctl --user -u 'arcturus-deployer@*' -n 200 --no-pager`."
            )
        raise SystemExit(f"Arcturus API returned HTTP {exc.code}:\n{detail}{hint}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Arcturus API request failed: {exc.reason}") from exc
    if result.get("status") == "failed":
        raise SystemExit(json.dumps(result, sort_keys=True))
    return result


def command_validate(args: argparse.Namespace) -> None:
    manifest = ServiceRelease.model_validate_json(Path(args.manifest).read_text())
    print(json.dumps({"status": "valid", "manifest_digest": manifest.digest()}))


def command_render(args: argparse.Namespace) -> None:
    raw = json.loads(Path(args.template).read_text())
    raw["metadata"]["name"] = args.service
    raw["metadata"]["revision"] = args.revision.lower()
    images: dict[str, str] = {}
    for assignment in args.image:
        name, separator, image = assignment.partition("=")
        if not separator:
            raise SystemExit("--image must use component=repository@sha256:digest")
        images[name] = image
    missing = set(raw["spec"]["components"]) - set(images)
    if missing:
        raise SystemExit(f"missing immutable images for components: {sorted(missing)}")
    for name, image in images.items():
        if name not in raw["spec"]["components"]:
            raise SystemExit(f"unknown component in --image: {name}")
        raw["spec"]["components"][name]["image"] = image
    manifest = ServiceRelease.model_validate(raw)
    request = DeploymentRequest(
        service=args.service,
        commit_sha=args.revision,
        manifest=manifest,
    )
    write_json(Path(args.output), manifest.model_dump(mode="json"))
    write_json(Path(args.request_output), request.model_dump(mode="json"))


def command_project_validate(args: argparse.Namespace) -> None:
    project, _, manifest = load_project(Path(args.project))
    print(json.dumps({
        "status": "valid",
        "service": project.service,
        "builds": sorted(project.builds),
        "components": sorted(manifest.spec.components),
    }))


def command_project_plan(args: argparse.Namespace) -> None:
    project, _, _ = load_project(Path(args.project))
    if args.format == "json":
        print(json.dumps(project.model_dump(mode="json")["builds"], indent=2, sort_keys=True))
        return
    for name, build in sorted(project.builds.items()):
        fields = [
            name,
            build.repository,
            build.context,
            build.containerfile,
            ",".join(build.validationTargets) or "-",
            build.releaseTarget or "-",
            ",".join(build.components),
            json.dumps(build.componentRepositories, sort_keys=True, separators=(",", ":")) or "{}",
        ]
        print("\t".join(fields))


def command_project_render(args: argparse.Namespace) -> None:
    project, _, manifest = load_project(Path(args.project))
    digest_paths: dict[str, Path] = {}
    for assignment in args.digest:
        name, separator, value = assignment.partition("=")
        if not separator or name not in project.builds:
            raise SystemExit("--digest must use known-build=path")
        digest_paths[name] = Path(value)
    missing = set(project.builds) - set(digest_paths)
    if missing:
        raise SystemExit(f"missing digest files for builds: {sorted(missing)}")
    raw = manifest.model_dump(mode="json", exclude_none=True)
    raw["metadata"]["revision"] = args.revision.lower()
    for build_name, build in project.builds.items():
        digest = digest_paths[build_name].read_text().strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise SystemExit(f"invalid registry digest for build {build_name}")
        for component in build.components:
            repository = build.componentRepositories.get(component, build.repository)
            raw["spec"]["components"][component]["image"] = f"{repository}@{digest}"
    rendered = ServiceRelease.model_validate(raw)
    request = DeploymentRequest(
        service=project.service,
        commit_sha=args.revision,
        manifest=rendered,
    )
    write_json(Path(args.output), rendered.model_dump(mode="json", exclude_none=True))
    write_json(Path(args.request_output), request.model_dump(mode="json", exclude_none=True))


def command_project_verify(args: argparse.Namespace) -> None:
    project, _, _ = load_project(Path(args.project))
    release = ServiceRelease.model_validate_json(Path(args.release).read_text())
    if release.metadata.name != project.service:
        raise SystemExit("rendered release service does not match project")
    expected_domains: list[str] = []
    expected_upstreams: list[str] = []
    for route in release.spec.routing.values():
        component = release.spec.components[route.component]
        expected_domains.extend(route.domains)
        expected_upstreams.append(
            f"{component.containerName or f'arcturus-{project.service}-{route.component}'}:{route.port}"
        )
    verify_args = argparse.Namespace(
        service=project.service,
        revision=release.metadata.revision,
        image=sorted({component.image for component in release.spec.components.values()}),
        require_routing=project.verification.requireRouting,
        public_url=project.verification.publicUrl,
        public_mode=project.verification.publicMode,
        expected_domain=sorted(set(expected_domains)),
        expected_upstream=sorted(set(expected_upstreams)),
        api_url=args.api_url or project.ci.apiUrl,
        token_file=args.token_file,
        timeout=args.timeout,
    )
    command_verify(verify_args)


def project_api_args(args: argparse.Namespace, project: ProjectDefinition) -> argparse.Namespace:
    return argparse.Namespace(
        api_url=getattr(args, "api_url", None) or project.ci.apiUrl,
        token_file=getattr(args, "token_file", None),
        timeout=getattr(args, "timeout", 330),
    )


def command_project_deploy(args: argparse.Namespace) -> None:
    project, _, _ = load_project(Path(args.project))
    request = DeploymentRequest.model_validate_json(Path(args.request).read_text())
    if request.service != project.service:
        raise SystemExit("deployment request service does not match project")
    api_args = project_api_args(args, project)
    api_args.request = args.request
    command_deploy(api_args)


def command_project_preflight(args: argparse.Namespace) -> None:
    project, _, release = load_project(Path(args.project))
    if getattr(args, "release", None):
        release = ServiceRelease.model_validate_json(Path(args.release).read_text())
        if release.metadata.name != project.service:
            raise SystemExit("preflight release service does not match project")
    api_args = project_api_args(args, project)
    readiness = api_request(api_args, "GET", "/healthz", authenticated=False)
    required_features = {"authenticated-preflight", "legacy-compose-handoff"}
    if project.registry.mode == "owned":
        required_features.update(
            {
                "oci-upload-grants",
                "artifact-verification-and-receipts",
                "receipt-enforced-owned-registry",
            }
        )
    if project.compatibility.v1Mode == "routing-mirror":
        required_features.update({"manifest-v1-safe-routing-mirror", "manifest-v1-provenance-routing"})
    reported_features = set(readiness.get("features", [])) if isinstance(readiness, dict) else set()
    missing_features = sorted(required_features - reported_features)
    if missing_features:
        version = readiness.get("version", "unknown") if isinstance(readiness, dict) else "unknown"
        raise SystemExit(
            "Arcturus host is incompatible with this blueprint "
            f"(host version: {version}; missing features: {', '.join(missing_features)}). "
            "Upgrade the host to an Arcturus host with authenticated preflight support before deploying."
        )
    if getattr(args, "readiness_only", False):
        print(json.dumps({"status": "ok", "readiness": readiness}, indent=2, sort_keys=True))
        return
    preflight = api_request(
        api_args,
        "POST",
        "/v1/preflight",
        {
            "service": project.service,
            "manifest": release.model_dump(mode="json", exclude_none=True),
        },
    )
    print(
        json.dumps(
            {"status": "ok", "readiness": readiness, "preflight": preflight},
            indent=2,
            sort_keys=True,
        )
    )


def command_project_status(args: argparse.Namespace) -> None:
    project, _, _ = load_project(Path(args.project))
    api_args = project_api_args(args, project)
    api_args.service = project.service
    command_status(api_args)


def command_project_lifecycle(args: argparse.Namespace) -> None:
    project, _, _ = load_project(Path(args.project))
    api_args = project_api_args(args, project)
    api_args.service = project.service
    api_args.action = args.action
    api_args.deployment_id = getattr(args, "deployment_id", None)
    command_lifecycle(api_args)


def command_project_rollback_probe(args: argparse.Namespace) -> None:
    project, _, _ = load_project(Path(args.project))
    api_args = project_api_args(args, project)
    api_args.service = project.service
    api_args.request = args.request
    command_rollback_probe(api_args)


def command_preview(args: argparse.Namespace) -> None:
    manifest = ServiceRelease.model_validate_json(Path(args.manifest).read_text())
    roots = [Path(item) for item in args.allowed_bind_root]
    output = Path(args.output)
    QuadletRenderer(roots).render(manifest, output)
    print(json.dumps({"status": "rendered", "output": str(output)}))


def command_deploy(args: argparse.Namespace) -> None:
    request = DeploymentRequest.model_validate_json(Path(args.request).read_text())
    result = api_request(args, "POST", "/v1/deployments", request.model_dump(mode="json"))
    if result.get("status") != "succeeded":
        raise SystemExit(json.dumps(result, sort_keys=True))
    print(json.dumps(result, indent=2, sort_keys=True))


def command_status(args: argparse.Namespace) -> None:
    print(json.dumps(api_request(args, "GET", f"/v1/services/{args.service}/active"), indent=2, sort_keys=True))


def command_verify(args: argparse.Namespace) -> None:
    result = api_request(args, "GET", f"/v1/services/{args.service}/active")
    if args.revision and result.get("commit_sha") != args.revision.lower():
        raise SystemExit(
            f"active revision mismatch: expected {args.revision.lower()}, got {result.get('commit_sha')}"
        )
    active_images = set(result.get("images", []))
    missing = set(args.image) - active_images
    if missing:
        raise SystemExit(f"active release is missing expected images: {sorted(missing)}")
    if result.get("status") != "succeeded" or result.get("health", {}).get("status") != "healthy":
        raise SystemExit("active release is not succeeded and healthy")
    routing = result.get("routing", {})
    if args.require_routing and routing.get("status") != "published":
        raise SystemExit(f"routing is not published: {json.dumps(routing, sort_keys=True)}")
    if args.require_routing and args.revision and routing.get("revision") != args.revision.lower():
        raise SystemExit("router receipt revision does not match the active release")
    if routing.get("deploymentId") and routing.get("deploymentId") != result.get("deployment_id"):
        raise SystemExit("router receipt deployment ID does not match the active release")
    missing_domains = set(getattr(args, "expected_domain", [])) - set(routing.get("domains", []))
    missing_upstreams = set(getattr(args, "expected_upstream", [])) - set(routing.get("upstreams", []))
    if missing_domains or missing_upstreams:
        raise SystemExit(
            f"router receipt mismatch: missing domains={sorted(missing_domains)}, "
            f"upstreams={sorted(missing_upstreams)}"
        )
    if args.public_url and args.public_mode != "skip":
        request = urllib.request.Request(args.public_url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=args.timeout) as response:
                status = response.status
                challenge = response.headers.get("cf-mitigated") == "challenge"
        except urllib.error.HTTPError as exc:
            status = exc.code
            challenge = exc.headers.get("cf-mitigated") == "challenge"
        if args.public_mode == "http-success" and not 200 <= status < 400:
            raise SystemExit(f"public verification returned HTTP {status}")
        if args.public_mode == "cloudflare-challenge" and not challenge:
            raise SystemExit("public verification did not receive the expected Cloudflare challenge")
    print(json.dumps({
        "status": "verified",
        "deployment_id": result.get("deployment_id"),
        "commit_sha": result.get("commit_sha"),
        "routing": routing,
    }, indent=2, sort_keys=True))


def command_rollback_probe(args: argparse.Namespace) -> None:
    original = api_request(args, "GET", f"/v1/services/{args.service}/active")
    request_payload = DeploymentRequest.model_validate_json(Path(args.request).read_text())
    if request_payload.service != args.service:
        raise SystemExit("rollback probe request service does not match")
    for component in request_payload.manifest.spec.components.values():
        if component.mode == "service":
            component.healthCheck = component.healthCheck or HealthCheck(
                command="false",
                interval="1s",
                timeout="1s",
                retries=1,
                startPeriod="0s",
            )
            component.healthCheck.command = "false"
            break
    base_url = (args.api_url or os.getenv("ARCTURUS_API_URL", "http://127.0.0.1:9090")).rstrip("/")
    http_request = urllib.request.Request(
        base_url + "/v1/deployments",
        data=json.dumps(request_payload.model_dump(mode="json")).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {load_token(args)}",
            "Content-Type": "application/json",
        },
    )
    try:
        urllib.request.urlopen(http_request, timeout=args.timeout)
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read().decode())
        if exc.code != 502 or body.get("rollback", {}).get("status") != "succeeded":
            raise SystemExit(f"rollback probe failed unexpectedly with HTTP {exc.code}") from exc
    else:
        raise SystemExit("rollback probe unexpectedly succeeded")
    restored = api_request(args, "GET", f"/v1/services/{args.service}/active")
    if restored.get("deployment_id") != original.get("deployment_id"):
        raise SystemExit("rollback probe did not restore the original deployment")
    if sorted(restored.get("images", [])) != sorted(original.get("images", [])):
        raise SystemExit("rollback probe did not restore the original image digests")
    original_route = original.get("routing", {})
    restored_route = restored.get("routing", {})
    if original_route.get("required") and restored_route.get("status") != "published":
        raise SystemExit("rollback probe did not republish routing")
    for field in ("revision", "domains", "upstreams"):
        if original_route.get(field) != restored_route.get(field):
            raise SystemExit(f"rollback probe restored different routing field: {field}")
    print(json.dumps({
        "status": "succeeded",
        "restored_deployment_id": restored.get("deployment_id"),
        "images": restored.get("images", []),
        "routing": restored_route,
    }, sort_keys=True))


def command_host_status(args: argparse.Namespace) -> None:
    units: dict[str, str] = {}
    for unit in args.critical_unit:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", unit],
            capture_output=True,
            text=True,
            check=False,
        )
        units[unit] = result.stdout.strip() or "inactive"
    failed = sorted(unit for unit, status in units.items() if status != "active")
    backup: dict[str, Any] = {"configured": bool(args.backup_unit)}
    if args.backup_unit:
        result = subprocess.run(
            ["systemctl", "--user", "show", args.backup_unit, "-p", "Result", "-p", "ExecMainExitTimestampMonotonic"],
            capture_output=True,
            text=True,
            check=False,
        )
        backup["unit"] = args.backup_unit
        backup["properties"] = dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )
        if backup["properties"].get("Result") != "success":
            failed.append(args.backup_unit)
        try:
            completed = int(backup["properties"].get("ExecMainExitTimestampMonotonic", "0"))
        except ValueError:
            completed = 0
        age_seconds = max(0.0, time.monotonic() - completed / 1_000_000) if completed else None
        backup["age_seconds"] = age_seconds
        if age_seconds is None or age_seconds > args.backup_max_age_hours * 3600:
            if args.backup_unit not in failed:
                failed.append(args.backup_unit)
    payload = {"status": "ready" if not failed else "failed", "units": units, "backup": backup, "failed": failed}
    print(json.dumps(payload, indent=2, sort_keys=True))
    if failed:
        raise SystemExit(1)


def command_operation(args: argparse.Namespace) -> None:
    print(json.dumps(api_request(args, "GET", f"/v1/operations/{args.operation_id}"), indent=2, sort_keys=True))


def command_lifecycle(args: argparse.Namespace) -> None:
    path = f"/v1/services/{args.service}"
    body = None
    method = "POST"
    if args.action == "rollback":
        path += "/rollback"
        body = {"deployment_id": args.deployment_id}
    elif args.action in {"enable", "disable"}:
        path += f"/{args.action}"
    else:
        method = "DELETE"
    print(json.dumps(api_request(args, method, path, body), indent=2, sort_keys=True))


def command_token_create(args: argparse.Namespace) -> None:
    database = Path(args.database).expanduser()
    output = Path(args.output).expanduser()
    if output.exists():
        raise SystemExit(f"refusing to overwrite token output: {output}")
    database.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"version": 2, "tokens": []}
    if database.exists():
        existing = json.loads(database.read_text())
        payload["tokens"] = existing if isinstance(existing, list) else existing.get("tokens", [])
    token = secrets.token_urlsafe(48)
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(token.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    token_id = args.token_id or secrets.token_hex(8)
    if any(item.get("id") == token_id for item in payload["tokens"]):
        raise SystemExit(f"token id already exists: {token_id}")
    payload["tokens"].append(
        {
            "id": token_id,
            "algorithm": "scrypt",
            "salt": base64.urlsafe_b64encode(salt).decode(),
            "hash": base64.urlsafe_b64encode(digest).decode(),
            "services": args.service,
        }
    )
    with tempfile.NamedTemporaryFile("w", dir=database.parent, delete=False) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.chmod(0o600)
    os.replace(temporary, database)
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(token + "\n")
    print(json.dumps({"status": "created", "token_id": token_id, "token_file": str(output)}))


def command_token_revoke(args: argparse.Namespace) -> None:
    database = Path(args.database).expanduser()
    payload = json.loads(database.read_text())
    records = payload if isinstance(payload, list) else payload.get("tokens", [])
    retained = [item for item in records if item.get("id") != args.token_id]
    if len(retained) == len(records):
        raise SystemExit(f"token id not found: {args.token_id}")
    write_secure_json(database, {"version": 2, "tokens": retained})
    print(json.dumps({"status": "revoked", "token_id": args.token_id}))


def add_api_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--api-url")
    parser.add_argument("--token-file")
    parser.add_argument("--timeout", type=int, default=330)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arcturusctl")
    subparsers = parser.add_subparsers(required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("manifest")
    validate.set_defaults(func=command_validate)
    render = subparsers.add_parser("render")
    render.add_argument("--template", required=True)
    render.add_argument("--service", required=True)
    render.add_argument("--revision", required=True)
    render.add_argument("--image", action="append", required=True)
    render.add_argument("--output", default="release.json")
    render.add_argument("--request-output", default="deployment-request.json")
    render.set_defaults(func=command_render)
    project = subparsers.add_parser("project")
    project_subparsers = project.add_subparsers(required=True)
    project_validate = project_subparsers.add_parser("validate")
    project_validate.add_argument("project")
    project_validate.set_defaults(func=command_project_validate)
    project_plan = project_subparsers.add_parser("plan")
    project_plan.add_argument("project")
    project_plan.add_argument("--format", choices=("json", "tsv"), default="json")
    project_plan.set_defaults(func=command_project_plan)
    project_render = project_subparsers.add_parser("render")
    project_render.add_argument("project")
    project_render.add_argument("--revision", required=True)
    project_render.add_argument("--digest", action="append", required=True)
    project_render.add_argument("--output", default="release.json")
    project_render.add_argument("--request-output", default="deployment-request.json")
    project_render.set_defaults(func=command_project_render)
    project_verify = project_subparsers.add_parser("verify")
    project_verify.add_argument("project")
    project_verify.add_argument("--release", default="release.json")
    add_api_options(project_verify)
    project_verify.set_defaults(func=command_project_verify)
    project_deploy = project_subparsers.add_parser("deploy")
    project_deploy.add_argument("project")
    project_deploy.add_argument("--request", default="deployment-request.json")
    add_api_options(project_deploy)
    project_deploy.set_defaults(func=command_project_deploy)
    project_preflight = project_subparsers.add_parser("preflight")
    project_preflight.add_argument("project")
    project_preflight.add_argument("--release")
    project_preflight.add_argument("--readiness-only", action="store_true")
    add_api_options(project_preflight)
    project_preflight.set_defaults(func=command_project_preflight)
    project_status = project_subparsers.add_parser("status")
    project_status.add_argument("project")
    add_api_options(project_status)
    project_status.set_defaults(func=command_project_status)
    for action in ("rollback", "enable", "disable", "remove"):
        project_lifecycle = project_subparsers.add_parser(action)
        project_lifecycle.add_argument("project")
        if action == "rollback":
            project_lifecycle.add_argument("--deployment-id")
        add_api_options(project_lifecycle)
        project_lifecycle.set_defaults(func=command_project_lifecycle, action=action)
    project_probe = project_subparsers.add_parser("rollback-probe")
    project_probe.add_argument("project")
    project_probe.add_argument("--request", default="deployment-request.json")
    add_api_options(project_probe)
    project_probe.set_defaults(func=command_project_rollback_probe)
    preview = subparsers.add_parser("preview")
    preview.add_argument("manifest")
    preview.add_argument("--output", default=".arcturus/build/quadlet")
    preview.add_argument("--allowed-bind-root", action="append", default=[])
    preview.set_defaults(func=command_preview)
    deploy = subparsers.add_parser("deploy")
    deploy.add_argument("request")
    add_api_options(deploy)
    deploy.set_defaults(func=command_deploy)
    status = subparsers.add_parser("status")
    status.add_argument("service")
    add_api_options(status)
    status.set_defaults(func=command_status)
    verify = subparsers.add_parser("verify")
    verify.add_argument("service")
    verify.add_argument("--revision")
    verify.add_argument("--image", action="append", default=[])
    verify.add_argument("--require-routing", action="store_true")
    verify.add_argument("--public-url")
    verify.add_argument("--expected-domain", action="append", default=[])
    verify.add_argument("--expected-upstream", action="append", default=[])
    verify.add_argument(
        "--public-mode",
        choices=("http-success", "cloudflare-challenge", "skip"),
        default="skip",
    )
    add_api_options(verify)
    verify.set_defaults(func=command_verify)
    rollback_probe = subparsers.add_parser("rollback-probe")
    rollback_probe.add_argument("service")
    rollback_probe.add_argument("request")
    add_api_options(rollback_probe)
    rollback_probe.set_defaults(func=command_rollback_probe)
    host_status = subparsers.add_parser("host-status")
    host_status.add_argument("--critical-unit", action="append", default=[])
    host_status.add_argument("--backup-unit")
    host_status.add_argument("--backup-max-age-hours", type=float, default=24)
    host_status.set_defaults(func=command_host_status)
    operation = subparsers.add_parser("operation")
    operation.add_argument("operation_id")
    add_api_options(operation)
    operation.set_defaults(func=command_operation)
    for action in ("rollback", "enable", "disable", "remove"):
        lifecycle = subparsers.add_parser(action)
        lifecycle.add_argument("service")
        if action == "rollback":
            lifecycle.add_argument("--deployment-id")
        add_api_options(lifecycle)
        lifecycle.set_defaults(func=command_lifecycle, action=action)
    token = subparsers.add_parser("token")
    token_subparsers = token.add_subparsers(required=True)
    create = token_subparsers.add_parser("create")
    create.add_argument("--database", required=True)
    create.add_argument("--output", required=True)
    create.add_argument("--token-id")
    create.add_argument("--service", action="append", required=True)
    create.set_defaults(func=command_token_create)
    revoke = token_subparsers.add_parser("revoke")
    revoke.add_argument("--database", required=True)
    revoke.add_argument("--token-id", required=True)
    revoke.set_defaults(func=command_token_revoke)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
