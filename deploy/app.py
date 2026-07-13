import os
import subprocess
import json
import re
import secrets
import sqlite3
import base64
import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Literal
from dotenv import load_dotenv

from fastapi import FastAPI, HTTPException, Header, Response
from pydantic import BaseModel, Field, field_validator

from release import DeploymentFailure, DeploymentRequest, ReleaseDeployer, redact

load_dotenv()

app = FastAPI(title="Arcturus Deploy Service")

STACKS_BASE = Path(os.getenv("STACKS_BASE_DIR", "/data/stacks"))
PORTAL_VHOSTS = Path(os.getenv("PORTAL_VHOSTS_DIR", "/data/portal/vhosts.d"))
PORTAL_STREAMS = Path(os.getenv("PORTAL_STREAMS_DIR", "/data/portal/streams.d"))
DOMAIN = os.getenv("CERT_DOMAIN", "example.org")
APEX_SERVICE = os.getenv("ARCTURUS_APEX_SERVICE", "")
NGINX_CONTAINER = os.getenv("NGINX_CONTAINER", "portal-nginx")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
TOKEN_FILE = Path(
    os.getenv("RUNNER_TOKENS_FILE", Path.home() / ".config/arcturus/tokens.json")
)
LEGACY_TOKEN_FILE = Path(
    os.getenv("LEGACY_RUNNER_TOKENS_FILE", Path.home() / "stacks/.runner-data/tokens.json")
)
V2_STATE_DB = Path(
    os.getenv(
        "ARCTURUS_V2_STATE_DB",
        Path.home() / ".local/share/arcturus-deployer/state.sqlite3",
    )
)

STACK_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
DEPLOY_TRIGGER_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
TCP_TARGET_RE = re.compile(r"^[A-Za-z0-9_.-]+:\d{1,5}$")

# ── Models ───────────────────────────────────────────────────────────────────

class DeployRequest(BaseModel):
    stack: str
    action: Literal["apply", "destroy"] = "apply"
    domain: str = ""  # e.g. app.example.org — used by the deprecated DNS integration
    deploy_trigger: str = ""  # New parameter

    @field_validator("stack")
    @classmethod
    def validate_stack(cls, value: str) -> str:
        if not STACK_RE.fullmatch(value):
            raise ValueError("stack must be a lowercase slug")
        return value

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, value: str) -> str:
        if value and not is_valid_domain(value):
            raise ValueError("domain must be a valid DNS name")
        if value and value != DOMAIN and not value.endswith(f".{DOMAIN}"):
            raise ValueError(f"domain must be {DOMAIN} or a subdomain of {DOMAIN}")
        return value

    @field_validator("deploy_trigger")
    @classmethod
    def validate_deploy_trigger(cls, value: str) -> str:
        if value and not DEPLOY_TRIGGER_RE.fullmatch(value):
            raise ValueError("deploy_trigger must be a 40-character git SHA")
        return value

class TCPRequest(BaseModel):
    name: str
    domain: str
    listen_port: int = Field(ge=1, le=65535)
    target: str
    protocol: Literal["tcp", "udp"] = "tcp"

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not STACK_RE.fullmatch(value):
            raise ValueError("name must be a lowercase slug")
        return value

    @field_validator("domain")
    @classmethod
    def validate_tcp_domain(cls, value: str) -> str:
        if not is_valid_domain(value):
            raise ValueError("domain must be a valid DNS name")
        if value != DOMAIN and not value.endswith(f".{DOMAIN}"):
            raise ValueError(f"domain must be {DOMAIN} or a subdomain of {DOMAIN}")
        return value

    @field_validator("target")
    @classmethod
    def validate_target(cls, value: str) -> str:
        if not TCP_TARGET_RE.fullmatch(value):
            raise ValueError("target must be host:port")
        host, port = value.rsplit(":", 1)
        if not (1 <= int(port) <= 65535):
            raise ValueError("target port must be between 1 and 65535")
        if not is_valid_host(host):
            raise ValueError("target host must be a valid DNS name or IP address")
        return value

class StatusRequest(BaseModel):
    stack: str

class DiscordNotifyRequest(BaseModel):
    message: str = Field(min_length=1, max_length=1800)
    level: Literal["info", "warn", "error"] = "info"

class DNSRequest(BaseModel):
    type: Literal["A", "AAAA", "CNAME", "TXT"] = "CNAME"
    name: str
    content: str = Field(min_length=1, max_length=2048)
    proxied: bool = True

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not is_valid_domain(value):
            raise ValueError("name must be a valid DNS name")
        if value != DOMAIN and not value.endswith(f".{DOMAIN}"):
            raise ValueError(f"name must be {DOMAIN} or a subdomain of {DOMAIN}")
        return value

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("content must not contain newlines")
        return value


class RollbackRequest(BaseModel):
    deployment_id: str | None = None


# ── Helpers ──────────────────────────────────────────────────────────────────

def _token_records() -> list[dict]:
    records: list[dict] = []
    for path in (TOKEN_FILE, LEGACY_TOKEN_FILE):
        if not path.exists():
            continue
        payload = json.loads(path.read_text())
        if isinstance(payload, list):
            records.extend(payload)
        elif isinstance(payload, dict) and payload.get("version") == 2:
            records.extend(payload.get("tokens", []))
        else:
            raise ValueError(f"unsupported token file format: {path}")
    return records


def _token_matches(record: dict, token: str) -> bool:
    candidate = record.get("token", "")
    if candidate:
        return secrets.compare_digest(candidate, token)
    encoded_hash = record.get("hash", "")
    encoded_salt = record.get("salt", "")
    if not encoded_hash or not encoded_salt or record.get("algorithm") != "scrypt":
        return False
    try:
        salt = base64.urlsafe_b64decode(encoded_salt.encode())
        expected = base64.urlsafe_b64decode(encoded_hash.encode())
        actual = hashlib.scrypt(
            token.encode(), salt=salt, n=2**14, r=8, p=1, dklen=len(expected)
        )
        return secrets.compare_digest(expected, actual)
    except (ValueError, TypeError):
        return False


def verify_auth(authorization: str | None = Header(None)):
    if not authorization:
        raise HTTPException(401, "Missing Authorization header")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        raise HTTPException(401, "Invalid authorization scheme")

    # 1. Check tokens.json if it exists
    if TOKEN_FILE.exists() or LEGACY_TOKEN_FILE.exists():
        try:
            for record in _token_records():
                if _token_matches(record, token):
                    return
        except Exception as e:
            print(json.dumps({"event": "token_file_error", "error": redact(str(e))}))

    # 2. Fallback to WEBHOOK_SECRET env var if configured
    if WEBHOOK_SECRET and secrets.compare_digest(token, WEBHOOK_SECRET):
        return  # Authorized!

    raise HTTPException(401, "Invalid token")


def authorize_service(authorization: str | None, service: str) -> None:
    """Authenticate a v2 request and enforce token scopes when present.

    Existing tokens without a services field remain global during migration.
    Newly issued tokens must carry an explicit services list.
    """
    if not authorization:
        raise HTTPException(401, "Missing Authorization header")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Invalid authorization scheme")

    if TOKEN_FILE.exists() or LEGACY_TOKEN_FILE.exists():
        try:
            for item in _token_records():
                if _token_matches(item, token):
                    scopes = item.get("services") or ["*"]
                    if "*" not in scopes and service not in scopes:
                        raise HTTPException(403, f"Token is not authorized for {service}")
                    return
        except HTTPException:
            raise
        except Exception as exc:
            print(json.dumps({"event": "token_file_error", "error": redact(str(exc))}))

    if WEBHOOK_SECRET and secrets.compare_digest(token, WEBHOOK_SECRET):
        return
    raise HTTPException(401, "Invalid token")


@lru_cache(maxsize=1)
def get_release_deployer() -> ReleaseDeployer:
    return ReleaseDeployer.from_environment()


def is_valid_domain(value: str) -> bool:
    if len(value) > 253 or value.endswith("."):
        return False
    return all(DNS_LABEL_RE.fullmatch(label) for label in value.split("."))


def is_valid_host(value: str) -> bool:
    if is_valid_domain(value):
        return True
    try:
        import ipaddress
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def resolve_stack_dir(stack: str) -> Path:
    base = STACKS_BASE.resolve()
    stack_dir = (base / stack).resolve()
    if base != stack_dir and base not in stack_dir.parents:
        raise HTTPException(400, "Stack path escapes STACKS_BASE_DIR")
    return stack_dir


def domain_to_cname(domain: str) -> str:
    if domain == DOMAIN:
        return "@"
    suffix = f".{DOMAIN}"
    return domain[:-len(suffix)] if domain.endswith(suffix) else domain


def cloudflare_record_name(name: str) -> str:
    if name == "@":
        return DOMAIN
    if name.endswith(f".{DOMAIN}") or name == DOMAIN:
        return name
    return f"{name}.{DOMAIN}"


def is_v2_managed(service: str) -> bool:
    if not V2_STATE_DB.exists():
        return False
    try:
        uri = f"file:{V2_STATE_DB}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            row = connection.execute(
                "SELECT 1 FROM active_releases WHERE service=?", (service,)
            ).fetchone()
        return row is not None
    except sqlite3.Error as exc:
        print(json.dumps({"event": "v2_state_read_error", "error": redact(str(exc))}))
        return False


def cloudflare_request(method: str, url: str, **kwargs):
    import httpx
    resp = httpx.request(method, url, timeout=15, **kwargs)
    resp.raise_for_status()
    data = resp.json()
    if data.get("success") is False:
        errors = data.get("errors") or []
        raise RuntimeError(f"Cloudflare API error: {errors}")
    return data


def read_stack_domain(stack_dir: Path) -> str:
    main_tf = stack_dir / "terraform" / "main.tf"
    if not main_tf.exists():
        return ""
    match = re.search(r'^\s*domain\s*=\s*"([^"]+)"', main_tf.read_text(), re.MULTILINE)
    return match.group(1) if match else ""



def run_terraform(stack_dir: Path, action: Literal["apply", "destroy"], deploy_trigger: str = "") -> dict:
    tf_dir = stack_dir / "terraform"
    if not tf_dir.exists():
        raise HTTPException(400, f"No terraform directory at {tf_dir}")
    result = subprocess.run(
        ["terraform", "init", "-input=false"],
        cwd=tf_dir, capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        return {"status": "error", "step": "init", "output": result.stderr}
    cmd = ["terraform", action, "-auto-approve", "-input=false"]
    if deploy_trigger:
        cmd += ["-var", f"deploy_trigger={deploy_trigger}"]
    result = subprocess.run(
        cmd, cwd=tf_dir, capture_output=True, text=True, timeout=300
    )
    return {
        "status": "ok" if result.returncode == 0 else "error",
        "action": action,
        "stack": stack_dir.name,
        "output": result.stdout + result.stderr,
    }


CF_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "")
CF_ZONE_ID = os.getenv("CLOUDFLARE_ZONE_ID", "")

def cloudflare_dns(name: str, content: str, action: str = "upsert"):
    """Create or delete a CNAME record via Cloudflare API."""
    if not CF_API_TOKEN or not CF_ZONE_ID:
        return
    headers = {
        "Authorization": f"Bearer {CF_API_TOKEN}",
        "Content-Type": "application/json",
    }
    if not content.endswith(f".{DOMAIN}"):
        content = f"{content}.{DOMAIN}"
    # Look up existing record
    record_name = cloudflare_record_name(name)
    existing = cloudflare_request(
        "GET",
        f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records",
        params={"name": record_name, "type": "CNAME"},
        headers=headers
    )
    records = existing.get("result", [])
    if action == "delete":
        for rec in records:
            cloudflare_request(
                "DELETE",
                f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records/{rec['id']}",
                headers=headers
            )
        return
    # upsert: update if exists, create if not
    body = {"type": "CNAME", "name": record_name, "content": content, "ttl": 120, "proxied": True}
    if records:
        rid = records[0]["id"]
        cloudflare_request(
            "PUT",
            f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records/{rid}",
            json=body, headers=headers
        )
    else:
        cloudflare_request(
            "POST",
            f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records",
            json=body, headers=headers
        )

def discord_notify(message: str, level: str = "info"):
    if not DISCORD_WEBHOOK_URL:
        return
    colors = {"info": 0x3498DB, "warn": 0xF1C40F, "error": 0xE74C3C}
    payload = {
        "embeds": [{
            "title": message,
            "color": colors.get(level, 0x3498DB),
        }]
    }
    try:
        import httpx
        httpx.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
    except Exception:
        pass


# ── Endpoints ────────────────────────────────────────────────────────────────

@app.post("/deploy")
def deploy(
    req: DeployRequest,
    response: Response,
    authorization: str | None = Header(None),
):
    response.headers["Deprecation"] = "true"
    response.headers["Sunset"] = "Wed, 31 Dec 2026 23:59:59 GMT"
    response.headers["Link"] = '</v1/deployments>; rel="successor-version"'
    verify_auth(authorization)
    if req.domain == DOMAIN and req.stack != APEX_SERVICE:
        raise HTTPException(403, f"Only the configured apex service may bind to {DOMAIN}.")
    stack_dir = resolve_stack_dir(req.stack)
    if is_v2_managed(req.stack):
        raise HTTPException(
            409,
            f"Stack '{req.stack}' is managed by the v2 Quadlet deployer",
        )
    if not stack_dir.exists():
        raise HTTPException(404, f"Stack '{req.stack}' not found at {stack_dir}")

    # Git Sync: if stack directory is a git repository, fetch and sync it
    if (stack_dir / ".git").exists():
        print(f"Syncing git repository for stack: {req.stack}")
        try:
            git = ["git", "-c", f"safe.directory={stack_dir}"]
            subprocess.run(
                [*git, "fetch", "origin"],
                cwd=stack_dir,
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            target_ref = req.deploy_trigger if req.deploy_trigger else "origin/main"
            if req.deploy_trigger:
                subprocess.run(
                    [*git, "merge-base", "--is-ancestor", req.deploy_trigger, "origin/main"],
                    cwd=stack_dir,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            subprocess.run(
                [*git, "reset", "--hard", target_ref],
                cwd=stack_dir,
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            print(f"Git sync complete. Target ref: {target_ref}")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            detail = getattr(exc, "stderr", "") or str(exc)
            raise HTTPException(
                500,
                f"Git sync failed for {req.stack}: {redact(detail)}",
            ) from exc

    terraform_domain = read_stack_domain(stack_dir)
    if req.domain and terraform_domain and req.domain != terraform_domain:
        raise HTTPException(400, "Webhook domain does not match the stack Terraform domain")
    domain = req.domain or terraform_domain
    if domain and (
        not is_valid_domain(domain)
        or (domain != DOMAIN and not domain.endswith(f".{DOMAIN}"))
    ):
        raise HTTPException(400, f"Stack Terraform contains an invalid domain: {domain}")
    if domain == DOMAIN and req.stack != APEX_SERVICE:
        raise HTTPException(403, f"Only the configured apex service may bind to {DOMAIN}.")

    result = run_terraform(stack_dir, req.action, req.deploy_trigger)
    if result["status"] != "ok":
        discord_notify(f"Stack **{req.stack}**: terraform {req.action} ❌", "error")
        raise HTTPException(
            500,
            {
                "status": "error",
                "step": result.get("step", req.action),
                "output": redact(result.get("output", "Terraform failed")),
            },
        )
    if result["status"] == "ok":
        if req.action == "apply" and domain:
            cname = domain_to_cname(domain)
            cloudflare_dns(cname, DOMAIN, "upsert")
        elif req.action == "destroy" and domain:
            cname = domain_to_cname(domain)
            cloudflare_dns(cname, DOMAIN, "delete")
    discord_notify(f"Stack **{req.stack}**: terraform {req.action} {'✅' if result['status']=='ok' else '❌'}", "error" if result["status"]=="error" else "info")
    return result


@app.post("/v1/deployments")
def create_deployment(
    request: DeploymentRequest,
    response: Response,
    authorization: str | None = Header(None),
):
    authorize_service(authorization, request.service)
    try:
        result = get_release_deployer().deploy(request)
    except FileExistsError as exc:
        raise HTTPException(409, str(exc)) from exc
    except DeploymentFailure as exc:
        response.status_code = 502 if exc.rollback_succeeded else 500
        return {
            "status": "failed",
            "service": request.service,
            "rollback": exc.rollback,
            "error": {"code": "deployment_failed", "message": redact(str(exc))},
        }
    return result


@app.get("/v1/deployments/{deployment_id}")
def get_deployment(
    deployment_id: str,
    authorization: str | None = Header(None),
):
    verify_auth(authorization)
    result = get_release_deployer().get(deployment_id)
    if not result:
        raise HTTPException(404, "Deployment not found")
    return result


@app.get("/v1/services/{service}/active")
def get_active_release(
    service: str,
    authorization: str | None = Header(None),
):
    authorize_service(authorization, service)
    result = get_release_deployer().active(service)
    if not result:
        raise HTTPException(404, "Active release not found")
    return result


def _run_service_operation(service: str, action: str, authorization: str | None, **kwargs):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", service):
        raise HTTPException(400, "service must be a lowercase DNS-style name")
    authorize_service(authorization, service)
    deployer = get_release_deployer()
    try:
        return getattr(deployer, action)(service, **kwargs)
    except FileExistsError as exc:
        raise HTTPException(409, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(500, redact(str(exc))) from exc


@app.post("/v1/services/{service}/rollback")
def rollback_service(
    service: str,
    request: RollbackRequest,
    authorization: str | None = Header(None),
):
    return _run_service_operation(
        service,
        "rollback",
        authorization,
        deployment_id=request.deployment_id,
    )


@app.post("/v1/services/{service}/disable")
def disable_service(service: str, authorization: str | None = Header(None)):
    return _run_service_operation(service, "disable", authorization)


@app.post("/v1/services/{service}/enable")
def enable_service(service: str, authorization: str | None = Header(None)):
    return _run_service_operation(service, "enable", authorization)


@app.delete("/v1/services/{service}")
def remove_service(service: str, authorization: str | None = Header(None)):
    return _run_service_operation(service, "remove", authorization)


@app.get("/v1/operations/{operation_id}")
def get_operation(operation_id: str, authorization: str | None = Header(None)):
    verify_auth(authorization)
    result = get_release_deployer().operation(operation_id)
    if not result:
        raise HTTPException(404, "Operation not found")
    authorize_service(authorization, result["service"])
    return result


@app.post("/tcp-service")
def register_tcp(req: TCPRequest, authorization: str | None = Header(None)):
    verify_auth(authorization)
    PORTAL_STREAMS.mkdir(parents=True, exist_ok=True)
    conf_path = PORTAL_STREAMS / f"{req.name}.conf"
    previous = conf_path.read_text() if conf_path.exists() else None
    conf = f"""# Managed by Arcturus Deploy — do not edit manually
stream {{
    upstream {req.name} {{
        server {req.target};
    }}
    server {{
        listen {req.listen_port}{' udp' if req.protocol == 'udp' else ''};
        proxy_pass {req.name};
    }}
}}
"""
    conf_path.write_text(conf)
    try:
        test_result = subprocess.run(
            ["docker", "exec", NGINX_CONTAINER, "nginx", "-t"],
            capture_output=True, text=True, timeout=30
        )
        if test_result.returncode != 0:
            if previous is None:
                conf_path.unlink(missing_ok=True)
            else:
                conf_path.write_text(previous)
            return {"status": "error", "config_written": False, "nginx_test_error": test_result.stderr}

        reload_result = subprocess.run(
            ["docker", "exec", NGINX_CONTAINER, "nginx", "-s", "reload"],
            capture_output=True, text=True, timeout=30
        )
        if reload_result.returncode != 0:
            return {"status": "warning", "config_written": True, "nginx_reload_error": reload_result.stderr}
    except Exception as e:
        if previous is None:
            conf_path.unlink(missing_ok=True)
        else:
            conf_path.write_text(previous)
        return {"status": "warning", "config_written": False, "nginx_reload_error": str(e)}
    discord_notify(f"TCP service **{req.name}** registered on port {req.listen_port}", "info")
    return {"status": "created", "config": f"{req.name}.conf"}


@app.post("/dns")
def update_dns(req: DNSRequest, authorization: str | None = Header(None)):
    verify_auth(authorization)
    if not CF_API_TOKEN or not CF_ZONE_ID:
        raise HTTPException(400, "Cloudflare not configured (set CLOUDFLARE_API_TOKEN + CLOUDFLARE_ZONE_ID)")
    headers = {
        "Authorization": f"Bearer {CF_API_TOKEN}",
        "Content-Type": "application/json",
    }
    body = {
        "type": req.type,
        "name": req.name,
        "content": req.content,
        "ttl": 120,
    }
    if req.type in {"A", "AAAA", "CNAME"}:
        body["proxied"] = req.proxied

    try:
        existing = cloudflare_request(
            "GET",
            f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records",
            params={"name": req.name, "type": req.type},
            headers=headers,
        )
        records = existing.get("result", [])
        if records:
            result = cloudflare_request(
                "PUT",
                f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records/{records[0]['id']}",
                json=body,
                headers=headers,
            )
            operation = "updated"
        else:
            result = cloudflare_request(
                "POST",
                f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records",
                json=body,
                headers=headers,
            )
            operation = "created"
        return {"status": "ok", "operation": operation, "cloudflare_response": result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, str(e))


@app.get("/status/{stack}")
def status(stack: str, authorization: str | None = Header(None)):
    verify_auth(authorization)
    if not STACK_RE.fullmatch(stack):
        raise HTTPException(400, "stack must be a lowercase slug")
    stack_dir = resolve_stack_dir(stack)
    if not stack_dir.exists():
        raise HTTPException(404, f"Stack '{stack}' not found")
    compose_path = stack_dir / "compose.yaml"
    tf_dir = stack_dir / "terraform"
    return {
        "stack": stack,
        "compose_exists": compose_path.exists(),
        "terraform_dir": tf_dir.exists(),
    }


@app.post("/notify/discord")
def notify_discord(req: DiscordNotifyRequest, authorization: str | None = Header(None)):
    verify_auth(authorization)
    discord_notify(req.message, req.level)
    return {"status": "sent"}
