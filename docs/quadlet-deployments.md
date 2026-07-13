# Manifest-driven Quadlet deployments

This document describes the mechanics behind the current Arcturus release path. For the field-by-field contract, see [Manifest reference](manifest-reference.md).

## Release input

CI submits a service name, 40-character source revision, and `arcturus.u128.org/v2` `ServiceRelease`. Every component image must be a fully qualified digest. The request identity, manifest metadata, and authenticated token scope must agree.

Validation, authorization, and lock conflicts return 4xx responses. Activation failure with successful restoration returns a failed deployment response; rollback failure is reported separately. Automation must use failure-aware HTTP handling and verify the JSON operation state.

## Rendering

The release engine renders generated artifacts into a deployment-specific archive:

- `.container` units for all components
- `.network` units for release-owned networks
- `.volume` units where generated volumes are supported
- `.service` and `.timer` units for scheduled components
- a service-level `.target` for lifecycle ownership and ordering

Images are pre-pulled and inspected before activation. The Podman systemd generator validates the staged units. Generated artifacts are not hand-maintained configuration.

## Activation

The active Quadlet selection is a symlink to one immutable release directory. Arcturus switches it atomically, reloads the user manager, and activates the service target.

Readiness rules:

- `service`: active unit plus successful Podman health when configured
- `oneshot`: successful completion
- `scheduled`: active timer; optionally a successful initial run when `runOnDeploy` is set

The release is published as active only after all required checks pass.

## Rollback

When activation fails and rollback is enabled, Arcturus restores the prior active selection, reloads systemd, restarts the previous target, and validates it. The failed candidate remains in release history for diagnosis.

Rollback changes generated runtime ownership only. Persistent bind data, external volumes, Podman secrets, and release archives are retained.

## Routing publication

The active release is written beneath `active-manifests/<service>/arcturus.json`. The registry prefers this current manifest over legacy stack state. The router generates vhosts, validates and reloads nginx, and writes a status receipt tied to the release revision and deployment ID.

CI verification should require the matching routing receipt for public services.
