---
title: Documentation tooling
kind: guide
lifecycle: operational
authority: Documentation validation and generation procedure
summary: Install and run the repository-local documentation checks.
maintenance:
  - Tooling dependencies, commands, generated sections, or CI usage change.
---

# Documentation tooling

Install the pinned Node dependencies. uv creates and synchronizes the locked
Python environment automatically when a command is run:

```bash
npm --prefix docs/_tooling ci --no-audit --fund=false
```

Refresh only the marked navigation and status sections:

```bash
uv run --project docs/_tooling --frozen python \
  docs/_tooling/vertical_slices.py --repo . write
```

Run the same non-mutating checks used by CI:

```bash
uv run --project docs/_tooling --frozen python \
  docs/_tooling/test_vertical_slices.py
npm --prefix docs/_tooling test
uv run --project docs/_tooling --frozen python \
  docs/_tooling/vertical_slices.py --repo . check
```

`write` is deterministic and never authors contracts, evidence, runbooks,
architecture, ADRs, or roadmap prose. `check` validates metadata, authority
references, links, slice gate consistency, and freshness without editing files.
Project paths, schemas, projections, navigation, prose guards, and Mermaid
validation are declared in [`delivery-docs.yaml`](../delivery-docs.yaml).
