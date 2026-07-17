from __future__ import annotations

import json
import os
import re
import stat
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

import httpx
from fastapi import Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

import app as deploy_app
from release import PodmanClient as BasePodmanClient
from release import ReleaseDeployer as BaseReleaseDeployer
from release import ServiceRelease, redact

DEFAULT_MAX_IMAGE_SIZE_BYTES = 805_306_368  # 768 MiB
SERVICE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,511}$")
REGISTRY_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*(?::[0-9]+)?$")
AUTH_FIELDS = ("auth", "identitytoken", "registrytoken")


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return value


def _registry_host(image: str) -> str:
    return image.split("/", 1)[0].lower()


def _normalize_auth_host(value: str) -> str:
    candidate = value.strip()
    if "://" in candidate:
        candidate = urlsplit(candidate).netloc
    else:
        candidate = candidate.split("/", 1)[0]
    return candidate.lower()


def _private_registries_from_environment() -> set[str]:
    values = {
        item.strip().lower()
        for item in os.getenv("ARCTURUS_PRIVATE_REGISTRIES", "").split(",")
        if item.strip()
    }
    invalid = sorted(value for value in values if not REGISTRY_RE.fullmatch(value))
    if invalid:
        raise RuntimeError(
            "ARCTURUS_PRIVATE_REGISTRIES contains invalid registry hosts: "
            + ", ".join(invalid)
        )
    return values


def _pull_messages(raw: str) -> list[Any]:
    text = raw.strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        messages: list[Any] = []
        for number, line in enumerate(raw.splitlines(), 1):
            if not line.strip():
                continue
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"Podman image pull returned invalid JSON progress at line {number}"
                ) from exc
        return messages
    return payload if isinstance(payload, list) else [payload]


def _pull_error(messages: Iterable[Any]) -> str | None:
    for message in messages:
        if not isinstance(message, dict):
            continue
        detail = message.get("errorDetail")
        if isinstance(detail, dict):
            text = detail.get("message") or detail.get("error")
            if text:
                return str(redact(str(text)))
        elif detail:
            return str(redact(str(detail)))
        error = message.get("error")
        if isinstance(error, dict):
            text = error.get("message") or error.get("error")
            if text:
                return str(redact(str(text)))
        elif error:
            return str(redact(str(error)))
    return None


class HardenedPodmanClient(BasePodmanClient):
    """Podman API client with bounded pull retries and host-owned registry auth."""

    def __init__(
        self,
        socket_path: str | None = None,
        *,
        registry_auth_file: str | Path | None = None,
        private_registries: Iterable[str] | None = None,
        pull_attempts: int = 3,
        retry_delay_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        super().__init__(socket_path)
        configured_auth_file = registry_auth_file or os.getenv("REGISTRY_AUTH_FILE")
        self.registry_auth_file = (
            Path(configured_auth_file).expanduser() if configured_auth_file else None
        )
        self.private_registries = {
            item.strip().lower()
            for item in (
                private_registries
                if private_registries is not None
                else _private_registries_from_environment()
            )
            if item.strip()
        }
        invalid = sorted(
            value for value in self.private_registries if not REGISTRY_RE.fullmatch(value)
        )
        if invalid:
            raise RuntimeError(
                "private registry configuration contains invalid hosts: "
                + ", ".join(invalid)
            )
        if pull_attempts < 1:
            raise ValueError("pull_attempts must be at least 1")
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds cannot be negative")
        self.pull_attempts = pul_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self.sleep = sleep

    def registry_requires_auth(self, image: str) -> bool:
        return _registry_host(image) in self.private_registries

    def registry_auth_available(self, registry: str) -> bool:
        path = self.registry_auth_file
        if path is None or not path.is_file() or not os.access(path, os.R_OK):
            return False
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & 0o077:
                return False
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return False
        if not isinstance(payload, dict) or not isinstance(payload.get("auths"), dict):
            return False
        for configured, record in payload["auths"].items():
            if _normalize_auth_host(str(configured)) != registry.lower():
                continue
            if isinstance(record, dict) and any(record.get(field) for field in AUTH_FIELDS):
                return True
        return False

    def pull(self, image: str, timeout: int) -> None:
        for attempt in range(1, self.pull_attempts + 1):
            try:
                with self._client(timeout) as client:
                    response = client.post("/images/pull", params={"reference": image})
            except httpx.TransportError as exc:
                if attempt < self.pull_attempts:
                    self.sleep(self.retry_delay_seconds * attempt)
                    continue
                raise RuntimeError(
                    f"Podman image pull transport failed after {attempt} attempts: "
                    f"{redact(str(exc))}"
                ) from exc

            if response.status_code >= 500:
                if attempt < self.pull_attempts:
                    self.sleep(self.retry_delay_seconds * attempt)
                    continue
                raise RuntimeError(
                    f"Podman image pull failed ({response.status_code}) after {attempt} attempts: "
                    f"{redact(response.text)}"
                )
            if response.status_code >= 400:
                raise RuntimeError(
                    f"Podman image pull failed ({response.status_code}): {redact(response.text)}"
                )

            messages = _pull_messages(response.text)
            if error := _pull_error(messages):
                raise RuntimeError(f"Podman image pull failed: {error}")
            return


class AuthenticatedReleaseDeployer(BaseReleaseDeployer):
    """Release deployer that validates host registry credentials before pulling."""

    def __init__(self, *args: Any, podman: Any | None = None, **kwargs: Any):
        if podman is None:
            podman = HardenedPodmanClient(os.getenv("PODMAN_SOCKET"))
        super().__init__(*args, podman=podman, **kwargs)

    def preflight(self, manifest: ServiceRelease) -> dict[str, Any]:
        result = super().preflight(manifest)
        images = sorted(
            {component.image for component in manifest.spec.components.values()}
        )
        required = sorted(
            {
                _registry_host(image)
                for image in images
                if hasattr(self.podman, "registry_requires_auth")
                and self.podman.registry_requires_auth(image)
            }
        )
        missing = [
            registry
            for registry in required
            if not self.podman.registry_auth_available(registry)
        ]
        if missing:
            raise RuntimeError(
                "host prerequisites missing: registryAuth=" + ",".join(missing)
            )
        result.setdefault("checked", {})["registryAuth"] = required
        return result


# The production unit starts image_policy_app:app. Replace the base deployer
# factory before exporting the already-registered FastAPI application so every
# v2 preflight and deployment request uses the hardened Podman boundary.
deploy_app.get_release_deployer.cache_clear()
deploy_app.get_release_deployer = lru_cache(maxsize=1)(
    AuthenticatedReleaseDeployer.from_environment
)

ARCTURUS_FEATURES = deploy_app.ARCTURUS_FEATURES
app = deploy_app.app
authorize_service = deploy_app.authorize_service

MAX_IMAGE_SIZE_BYTES = _positive_int_env(
    "ARCTURUS_MAX_IMAGE_SIZE_BYTES", DEFAULT_MAX_IMAGE_SIZE_BYTES
)

for feature in ("image-size-policy", "private-registry-auth", "podman-pull-errors"):
    if feature not in ARCTURUS_FEATURES:
        ARCTURUS_FEATURES.append(feature)


class ImagePolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str
    image: str = Field(min_length=1, max_length=512)
    size_bytes: int = Field(ge=0)

    @field_validator("service")
    @classmethod
    def validate_service(cls, value: str) -> str:
        if not SERVICE_RE.fullmatch(value):
            raise ValueError("service must be a lowercase DNS-style name")
        return value

    @field_validator("image")
    @classmethod
    def validate_image(cls, value: str) -> str:
        if not IMAGE_RE.fullmatch(value):
            raise ValueError("image contains invalid characters")
        return value


@app.post("/v1/image-policy")
def check_image_policy(
    request: ImagePolicyRequest,
    authorization: str | None = Header(None),
):
    """Reject an oversized release image before CI uploads it to the registry."""
    authorize_service(authorization, request.service)
    if request.size_bytes > MAX_IMAGE_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "image_too_large",
                "service": request.service,
                "image": request.image,
                "size_bytes": request.size_bytes,
                "max_image_size_bytes": MAX_IMAGE_SIZE_BYTES
            },
        )
    return {
        "status": "accepted",
        "service": request.service,
        "image": request.image,
        "size_bytes": request.size_bytes,
        "max_image_size_bytes": MAX_IMAGE_SIZE_BYTES
    }
