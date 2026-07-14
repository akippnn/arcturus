# ServiceRelease manifest reference

The current release API is `arcturus.u128.org/v2` with kind `ServiceRelease`. Unknown fields are rejected.

## Minimal example

```json
{
  "apiVersion": "arcturus.u128.org/v2",
  "kind": "ServiceRelease",
  "metadata": {
    "name": "my-api",
    "revision": "0123456789abcdef0123456789abcdef01234567"
  },
  "spec": {
    "components": {
      "web": {
        "image": "registry.example.org/team/my-api@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "containerName": "my-api",
        "mode": "service",
        "networks": ["internal_routing"],
        "restart": "always",
        "healthCheck": {
          "command": "curl --fail http://127.0.0.1:8080/health"
        }
      }
    },
    "networks": [{"name": "internal_routing", "external": true}],
    "routing": {
      "web": {
        "component": "web",
        "port": 8080,
        "protocol": "http",
        "domains": ["api.example.org"]
      }
    },
    "deployment": {
      "timeoutSeconds": 300,
      "rollbackOnFailure": true
    }
  }
}
```

## Metadata

| Field | Required | Rules |
| --- | --- | --- |
| `name` | yes | Lowercase DNS-style service name, up to 63 characters |
| `revision` | yes | Exactly 40 hexadecimal characters; must match the deployment request commit |
| `deploymentId` | no | UUID; normally assigned by the deployer rather than authored by the project |

## Components

`spec.components` is a non-empty object keyed by lowercase component names.

| Field | Default | Notes |
| --- | --- | --- |
| `image` | — | Required fully qualified `repository@sha256:<64 hex>` reference; tags are rejected |
| `containerName` | generated | Optional lowercase runtime name |
| `mode` | `service` | `service`, `oneshot`, or `scheduled` |
| `command` | `[]` | Argument array passed to the container |
| `environment` | `{}` | Non-secret string values; secret-like keys are rejected |
| `secrets` | `[]` | Podman secret references |
| `ports` | `[]` | Container and optional host port mappings |
| `volumes` | `[]` | Bind mounts or named volumes |
| `dependsOn` | `[]` | Component names; missing references and cycles are rejected |
| `networks` | `internal_routing` | Every name must appear in `spec.networks` |
| `healthCheck` | none | Podman health command for `service` mode only |
| `schedule` | none | Required for `scheduled`, invalid for other modes |
| `restart` | `always` | `always`, `on-failure`, or `no`; one-shot/scheduled modes are normalized to `no` |

### Secrets

```json
{"name":"my-api-signing-key","type":"file","target":"signing-key"}
```

or:

```json
{"name":"my-api-database-url","type":"env","target":"DATABASE_URL"}
```

Secret names are references to host-provisioned Podman secrets. Values never belong in the release manifest.

### Ports

```json
{"container":8080,"host":12780,"hostIp":"127.0.0.1","protocol":"tcp"}
```

`container` is required. `host`, `hostIp`, and `protocol` are optional. Public HTTP routing normally does not require a host port because ingress reaches the container through a Podman network.

### Volumes

Bind mount:

```json
{
  "source":"/home/appsvc/apps/my-api/data",
  "target":"/var/lib/my-api",
  "type":"bind",
  "readOnly":false,
  "external":true,
  "selinuxRelabel":"private"
}
```

Named volume:

```json
{"source":"my_api_data","target":"/var/lib/my-api","type":"volume","external":true}
```

Bind sources must be absolute and permitted by the host allowlist. `selinuxRelabel` maps to private/shared SELinux relabel behavior. External volumes are preserved by lifecycle operations.

### Health checks

```json
{
  "command":"curl --fail http://127.0.0.1:8080/health",
  "interval":"10s",
  "timeout":"5s",
  "retries":5,
  "startPeriod":"10s"
}
```

Without a health check, an active `service` unit is treated as ready. One-shot and scheduled components cannot declare health checks.

### Schedules

```json
{
  "onCalendar":"daily",
  "persistent":true,
  "randomizedDelaySeconds":300,
  "runOnDeploy":false
}
```

`onCalendar` uses systemd calendar syntax. `persistent` catches missed runs after downtime. `runOnDeploy` requires an initial successful execution before promotion.

## Networks

```json
{"name":"internal_routing","external":true}
```

External networks must already exist. Non-external networks are generated as release-owned Quadlet networks.

## Routing

Each routing entry names a component and container port.

| Field | Default | Notes |
| --- | --- | --- |
| `component` | — | Required component key |
| `port` | — | Required container port |
| `protocol` | `http` | `http`, `https`, `tcp`, or `udp` |
| `domains` | `[]` | Fully qualified DNS names |
| `aliases` | `[]` | DNS labels expanded beneath the configured base domain |
| `websocket` | `false` | Enables websocket proxy headers |
| `maxBodySize` | `1G` | Bounded nginx body-size value |

Only enabled, successfully activated releases are published to the registry/router.

## Deployment policy

- `timeoutSeconds`: 10–1800 seconds, default 300.
- `rollbackOnFailure`: default `true`.

## Migration policy

`spec.migration.legacyCompose` declares Compose projects that must be handed off during the first successful manifest release:

```json
{
  "migration": {
    "legacyCompose": [
      {"project":"legacy-project","required":false,"cleanup":"retain"}
    ]
  }
}
```

The deployer discovers containers by Compose project label, records their prior running state, and stops them immediately before Quadlet activation. If activation or routing fails, only containers that were previously running are restarted. A successful release either retains the old containers in a stopped state (`retain`, the safe default) or removes the containers without removing their volumes (`remove`). The policy is ignored after a manifest release is already active for the service.

`required: true` rejects the cutover when no matching Compose project is found. Use `false` when recovering a host that may already have partially stopped or removed the legacy project.

## Current limitations

The v0.99 schema does not yet expose CPU, memory, or PID limits, environment overlays, backup policy, deployment hooks, or blue-green strategies. These are tracked in the [roadmap](ROADMAP.md), and should not be emulated through unreviewed generated-unit edits.
