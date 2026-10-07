---
title: PATH-001 source implementation evidence
kind: evidence
lifecycle: immutable
authority: Captured four-root source evidence
summary: Cross-language and installer evidence for centralized path resolution.
maintenance:
  - Never rewrite captured results; add a new evidence record for a new attempt.
slice: PATH-001
captured_at: "2026-09-13"
source:
  repository: arcturus
  base_revision: 9d82dbd1853553e3c494d7238b4c8759f97d75fa
  state: dirty-working-copy
  change_identity: not captured by the original run
gates:
  PATH-001-ROOTS: pass
  PATH-001-COMPAT: pass
---

# PATH-001 source implementation evidence

## What changed

- Configuration, persistent data, reconstructable cache, and runtime use
  independent roots with explicit Arcturus overrides, XDG values, then rootless
  defaults.
- Python and Rust share the same FHS fixture. Installers resolve paths once and
  pass final locations to Node entrypoints and generated user units.
- The updater retains layout and fleet-role arguments and uses a protected
  locator to rediscover custom state.
- A selected root that would abandon recorded or historical state fails closed.
- Raspberry Pi provisioning includes the resolver for staged and first-boot
  bundle paths.
- Podman remains the workload image cache; no duplicate cache or automatic
  pruning was introduced.

## What passed

The captured run passed 106 Python tests, 41 locked Rust tests, strict Rust
formatting and Clippy, available Node compilation and suites, host updater and
OCI shell suites, and version consistency at `4.0.0-alpha.1`.

## Compatibility and limitation

Ordinary rootless defaults remain unchanged when XDG variables are unset.
Existing state is never relocated silently; operators must stop services, copy
state with metadata preserved, retain rollback locations, and validate the new
layout.

The original run did not capture an immutable dirty-worktree identity. Clean
and upgraded AlmaLinux installs, live XDG migration, exact locked Node
toolchains, and RPM/system packaging remain unproved.
