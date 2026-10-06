---
title: Delivery status
kind: status
lifecycle: generated
authority: Projection of product metadata and slice manifests
summary: Current product version, active slices, verdicts, and next gates.
maintenance:
  - VERSION, COMPATIBILITY.json, or a slice manifest changes.
generated_by: docs/_tooling/vertical_slices.py
generated_sections:
  - status
nav:
  section: Start here
  order: 2
---

# Arcturus delivery status

This page is generated. It deliberately does not repeat architecture,
implementation inventories, evidence details, or future feature lists.

<!-- BEGIN GENERATED:status -->
Product version: `4.0.0-alpha.1`  
Manifest APIs: `arcturus.u128.org/v1`, `arcturus.u128.org/v2`

| Slice | Outcome | State | Verdict | Next gate |
| --- | --- | --- | --- | --- |
| [DIST-001](slices/DIST-001/README.md) | Multi-workload distributed fleet foundation | `active` | `pending` | `DIST-001-HARDWARE` |
| [PATH-001](slices/PATH-001/README.md) | Four-root filesystem transition | `active` | `pending` | `PATH-001-PHYSICAL` |

The slice manifest owns each row. Evidence owns the underlying results and limitations.
<!-- END GENERATED:status -->

For what the system means, read [Architecture](architecture.md). For approved
future outcomes, read the [Roadmap](ROADMAP.md).
