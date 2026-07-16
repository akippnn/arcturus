from __future__ import annotations

import os
import re

from fastapi import Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app import ARCTURUS_FEATURES, app, authorize_service

DEFAULT_MAX_IMAGE_SIZE_BYTES = 805_306_368  # 768 MiB
SERVICE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,511}$")


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return value


MAX_IMAGE_SIZE_BYTES = _positive_int_env(
    "ARCTURUS_MAX_IMAGE_SIZE_BYTES", DEFAULT_MAX_IMAGE_SIZE_BYTES
)

if "image-size-policy" not in ARCTURUS_FEATURES:
    ARCTURUS_FEATURES.append("image-size-policy")


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
                "max_image_size_bytes": MAX_IMAGE_SIZE_BYTES,
            },
        )
    return {
        "status": "accepted",
        "service": request.service,
        "image": request.image,
        "size_bytes": request.size_bytes,
        "max_image_size_bytes": MAX_IMAGE_SIZE_BYTES,
    }
