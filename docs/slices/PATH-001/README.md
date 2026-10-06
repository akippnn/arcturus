---
title: PATH-001 four-root filesystem transition
kind: slice-overview
lifecycle: stable
authority: Human overview of PATH-001
summary: Centralize configuration, data, cache, and runtime path ownership.
maintenance:
  - The filesystem contract or current evidence pointer changes.
id: PATH-001-overview
slice: PATH-001
nav:
  section: Delivery records
  order: 42
---

# PATH-001: Four-root filesystem transition

Arcturus now resolves configuration, persistent data, reconstructable cache,
and runtime paths through one cross-language contract.

```mermaid
flowchart LR
  E["Explicit ARCTURUS roots"] --> R["Central resolver"]
  X["XDG roots"] --> R
  D["Rootless defaults"] --> R
  R --> P["Python lifecycle"]
  R --> U["Rust services"]
  R --> N["Node entrypoints"]
  R --> I["Installer and units"]
```

The transition preserves ordinary rootless defaults and refuses silent state
abandonment. It enables—but does not yet prove—RPM and system-service layouts.

- [Filesystem contract](../../filesystem-layout.md)
- [Source implementation evidence](evidence/source-implementation.md)
- [Machine-readable state](manifest.yaml)

