---
title: Compose and Terraform migration
kind: guide
lifecycle: operational
authority: Supported application deployment migration procedure
summary: Move legacy Compose or Terraform deployments into ServiceRelease lifecycle.
maintenance:
  - Migration, rollback, or compatibility behavior changes.
nav:
  section: Operate Arcturus
  order: 26
---

# Migrating from Compose or Terraform application deployment

The migration goal is one production lifecycle owner: Arcturus-generated Quadlets and user systemd. Compose may remain for local development; Terraform may remain for long-lived infrastructure, but neither should recreate application containers after cutover.

This application-lifecycle migration is separate from changing Arcturus host
filesystem roots. For an existing host moving to custom XDG or FHS paths,
complete the stopped, non-destructive procedure in
[Filesystem layout](filesystem-layout.md) first. The installer never treats a
new empty path as permission to abandon an existing lifecycle database.

## 1. Inventory and checkpoint

Record:

- every container image and current digest
- commands, environment, secret sources, networks, ports, volumes, and health checks
- startup and migration dependencies
- public routes and TLS ownership
- current database/schema version
- persistent volume identities and backups
- the last known-good release and rollback credentials

Retired services should remain retired.

## 2. Model the application

Translate long-running containers to `service`, migrations/init work to `oneshot`, and scheduled tasks to `scheduled`. Use `dependsOn` rather than custom sleep loops. Map one built image to multiple components when appropriate.

Declare fixed infrastructure images with real digests. Do not introduce `latest` during migration.

## 3. Adopt data safely

External bind mounts and named volumes should be adopted in place. Never create a similarly named replacement without confirming the actual storage identity. Configure bind roots on the host before deployment.

Provision Podman secrets and replace `.env` interpolation or literal secret values with manifest references.

## 4. Prepare routing

Every routed component must join the operator's routing network. Preserve the existing domain and container port in release metadata. Do not hand-edit generated vhosts; wait for a routing receipt that matches the intended revision and deployment ID.

## 5. Cut over one lifecycle owner

Stop Watchtower and prevent Terraform provisioners from recreating the application. For a rootless Podman Compose deployment, declare the old project in the first release:

```json
"migration": {
  "legacyCompose": [
    {"project":"legacy-project","required":false,"cleanup":"retain"}
  ]
}
```

Arcturus then performs the critical handoff transaction itself: pull and validate the new release, stop the matching Compose containers, activate and verify Quadlets and routing, and restart the formerly running Compose containers if the new release fails. External named volumes are never removed by the handoff. Retain the stopped legacy containers until the new release and rollback behavior have been verified, then remove them deliberately.

After success, remove obsolete application resources from Terraform state without invoking destructive provisioners when necessary. Do not let Compose and Quadlet own the same production container concurrently.

## 6. Prove rollback and reboot

Deploy an intentionally unhealthy same-image release and require automatic rollback to restore the known-good revision, digests, and route. Reboot the host and verify declared critical targets and timers individually.

For database credential rotation, keep the old runtime role usable until two successful releases and rollback testing have completed on the new role.

## 7. Adopt into fleet control separately

Completing the Compose-to-Quadlet migration does not automatically adopt a
service into distributed fleet management. Existing host-local services and
their deployment history remain valid until an operator submits a
`WorkloadIntent` for that service.

Before adoption:

- preserve the complete `ServiceRelease v2` as the worker-local lifecycle and
  rollback boundary;
- identify the intended initial worker and confirm its architecture, memory,
  capability, topology, secret, network, and external-volume prerequisites;
- import external infrastructure into the fleet resource catalog without
  transferring lifecycle ownership or credential values;
- bind logical resource names to Podman secret references already declared by
  the release; and
- declare movement policy conservatively from the service's actual state and
  concurrency behavior.

Do not declare a service overlap-safe merely because its image is portable.
Writable local volumes, authoritative node-local state, legacy migration,
single-writer behavior, or a need for fencing can make target-first overlap
unsafe. DIST-001 refuses those moves and does not transfer volumes or execute a
fencing strategy.

For an overlap-safe service whose authoritative state is external, apply the
intent, inspect `fleet service explain`, and verify the initial observation
before considering explicit movement. Existing local lifecycle commands remain
the recovery authority on each worker, while fleet status combines desired and
observed placement.

## Recommended order

1. stateless internal workers
2. stateless public web services
3. scheduled jobs
4. multi-component services with persistent storage
5. databases and critical infrastructure
6. ingress and source-control services last, under explicit maintenance procedures

See [Legacy compatibility](legacy/README.md) for the deprecated architecture retained for migration reference.
