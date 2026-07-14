# Migrating from Compose or Terraform application deployment

The migration goal is one production lifecycle owner: Arcturus-generated Quadlets and user systemd. Compose may remain for local development; Terraform may remain for long-lived infrastructure, but neither should recreate application containers after cutover.

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

## Recommended order

1. stateless internal workers
2. stateless public web services
3. scheduled jobs
4. multi-component services with persistent storage
5. databases and critical infrastructure
6. ingress and source-control services last, under explicit maintenance procedures

See [Legacy compatibility](legacy/README.md) for the deprecated architecture retained for migration reference.
